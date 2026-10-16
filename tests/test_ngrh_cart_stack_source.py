"""The cart service's second input source: the stack that creates the carts table (step 12).

Tag discovery lists the carts DynamoDB global table as one plain ``AWS::DynamoDB::Table`` per Region, and the
multi-Region isolation test's DynamoDB pause acts on ``AWS::DynamoDB::GlobalTable``. With tags alone the pause is
skipped ("Target resolution returned empty set") and ``cart-isolation`` ends ``PASSED`` without touching the table
(test2, 2026-10-09, run 2ede1ccb). Reading the stack that creates the table, ``carts-db-stack<ENV>``, makes discovery
list the global table too (test2, 2026-10-09, about a minute after the source was added: 24 resources became 25, the
new one an ``AWS::DynamoDB::GlobalTable``).

These tests pin how ``ngrh.yaml`` and ``make ngrh`` carry that stack to the cart service, and that nothing else changes:

* the template takes one optional parameter, ``CartsDbStackArn``, empty by default, that accepts a stack ARN or nothing;
* a condition says whether it is set, and only then does the cart service get a second input source, the stack;
* no other service reads a stack, and nothing else uses the parameter;
* ``make ngrh`` looks the stack ``carts-db-stack<ENV>`` up in the primary Region, passes its ARN, and still deploys
  (with an empty value and a warning) when the stack does not exist.

The Makefile tests run ``make ngrh`` against a stub ``aws`` that records its calls; nothing reaches AWS.

Run with:  pytest tests/test_ngrh_cart_stack_source.py -v
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
DEPLOYMENT = REPO / "deployment"
TEMPLATE = DEPLOYMENT / "ngrh.yaml"

# A StackId in the form `aws cloudformation describe-stacks` prints it, with a made-up account and stack id.
STACK_ARN = "arn:aws:cloudformation:us-east-1:123456789012:stack/carts-db-stack-t/11111111-2222-3333-4444-555555555555"


class _TaggedLoader(yaml.SafeLoader):
    """SafeLoader that keeps CloudFormation short-form tags: ``!Ref X`` becomes ``{"Ref": "X"}``, ``!If [..]``
    becomes ``{"Fn::If": [..]}``. (tests/test_ngrh_tag_discovery.py drops the tag names; these tests need them.)"""


def _tagged(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {"Ref" if suffix == "Ref" else f"Fn::{suffix}": value}


_TaggedLoader.add_multi_constructor("!", _tagged)


def _template():
    # Drive the SafeLoader subclass directly rather than passing it to yaml.load (see tests/test_yaml_loading.py).
    loader = _TaggedLoader(TEMPLATE.read_text())
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _services():
    resources = _template()["Resources"]
    return {lid: r for lid, r in resources.items() if r["Type"] == "AWS::ResilienceHubV2::Service"}


# ---------------------------------------------------------------------------
# The template
# ---------------------------------------------------------------------------

class TestTemplate:
    def test_the_parameter_is_optional(self):
        param = _template()["Parameters"]["CartsDbStackArn"]
        assert param["Type"] == "String"
        assert param["Default"] == ""

    def test_the_parameter_takes_a_stack_arn_or_nothing(self):
        pattern = re.compile(_template()["Parameters"]["CartsDbStackArn"]["AllowedPattern"])
        assert pattern.fullmatch(""), "empty must be allowed: it means tag discovery only"
        assert pattern.fullmatch(STACK_ARN)
        assert pattern.fullmatch(STACK_ARN.replace("arn:aws:", "arn:aws-us-gov:"))
        for refused in [
            "carts-db-stack-t",  # a stack name is not an ARN
            "arn:aws:s3:::reports",
            "arn:aws:cloudformation:us-east-1:123456789012:stack/carts-db-stack-t",  # no stack id
            "arn:aws:cloudformation:us-east-1:1234567890:stack/carts-db-stack-t/11111111-2222",  # not an account id
            " " + STACK_ARN,
            STACK_ARN + " ",
            STACK_ARN + "\n" + STACK_ARN,
        ]:
            assert not pattern.fullmatch(refused), refused

    def test_a_condition_says_the_parameter_is_set(self):
        # HasDevOpsAgentSpace belongs to the invoker role's DevOps Agent policy; see tests/test_ngrh_devops_agent.py.
        assert _template()["Conditions"] == {
            "HasCartsDbStack": {"Fn::Not": [{"Fn::Equals": [{"Ref": "CartsDbStackArn"}, ""]}]},
            "HasDevOpsAgentSpace": {"Fn::Not": [{"Fn::Equals": [{"Ref": "DevOpsAgentSpaceArn"}, ""]}]},
        }

    def test_the_cart_service_reads_the_stack_as_a_second_source_when_the_parameter_is_set(self):
        sources = _services()["CartService"]["Properties"]["InputSources"]
        assert sources == [
            {"ResourceConfiguration": {"ResourceTags": [{"Key": "service", "Values": ["cart", "shared"]}]}},
            {
                "Fn::If": [
                    "HasCartsDbStack",
                    {"ResourceConfiguration": {"CfnStackArn": {"Ref": "CartsDbStackArn"}}},
                    {"Ref": "AWS::NoValue"},
                ]
            },
        ]

    @pytest.mark.parametrize("lid", sorted(set(_services()) - {"CartService"}))
    def test_no_other_service_reads_a_stack(self, lid):
        name = _services()[lid]["Properties"]["Name"]["Fn::Sub"].replace("${Env}", "")
        assert _services()[lid]["Properties"]["InputSources"] == [
            {"ResourceConfiguration": {"ResourceTags": [{"Key": "service", "Values": [name, "shared"]}]}}
        ]

    def test_only_the_cart_service_uses_the_parameter(self):
        users = sorted(lid for lid, resource in _template()["Resources"].items()
                       if "CartsDbStackArn" in json.dumps(resource))
        assert users == ["CartService"]

    def test_the_template_fits_the_request_body_limit(self):
        # `make ngrh` sends the file inline (no --s3-bucket), and CloudFormation refuses a body over 51,200 bytes.
        assert len(TEMPLATE.read_bytes()) < 51200

    def test_the_description_fits_the_description_limit(self):
        assert len(_template()["Description"].encode()) <= 1024


# ---------------------------------------------------------------------------
# make ngrh
# ---------------------------------------------------------------------------

STUB_AWS = r'''#!/usr/bin/env python3
"""A stub `aws` for `make ngrh`: records every call, knows one stack, accepts everything else."""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")


def option(name):
    return args[args.index(name) + 1] if name in args else None


if args[:2] == ["sts", "get-caller-identity"]:
    print("123456789012\tarn:aws:iam::123456789012:user/stub\tAIDASTUB")
elif args[:2] == ["cloudformation", "describe-stacks"]:
    known = option("--stack-name") == os.environ["STUB_STACK"] and option("--region") == os.environ["STUB_REGION"]
    if known and os.environ.get("STUB_ARN") and option("--query") == "Stacks[0].StackId" and option("--output") == "text":
        print(os.environ["STUB_ARN"])
    else:
        sys.stderr.write("An error occurred (ValidationError) when calling the DescribeStacks operation: "
                         "Stack with id %s does not exist\n" % option("--stack-name"))
        sys.exit(254)
sys.exit(0)
'''


def _make_ngrh(tmp_path, *, arn=STACK_ARN, region="us-east-1", standby="us-west-2", env_suffix="-t"):
    """Run `make ngrh` with the stub first on PATH; returns (completed process, recorded aws calls)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "aws"
    stub.write_text(STUB_AWS)
    stub.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", STUB_LOG=str(log),
               STUB_STACK=f"carts-db-stack{env_suffix}", STUB_REGION=region, STUB_ARN=arn)
    done = subprocess.run(
        ["make", "-C", str(DEPLOYMENT), "ngrh", f"ENV={env_suffix}", f"PRIMARY_REGION={region}", f"STANDBY_REGION={standby}"],
        env=env, capture_output=True, text=True, timeout=120)
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return done, calls


def _deploys(calls):
    return [call for call in calls if call[:2] == ["cloudformation", "deploy"]]


def _overrides(deploy):
    return dict(item.split("=", 1) for item in deploy[deploy.index("--parameter-overrides") + 1:])


def _lookups(calls):
    """The lookups of the carts table's stack. The recipe also looks for the devops-agent stack (see
    tests/test_ngrh_devops_agent_make.py), which these tests are not about."""
    return [call for call in calls if call[:2] == ["cloudformation", "describe-stacks"]
            and call[call.index("--stack-name") + 1].startswith("carts-db-stack")]


class TestMakeNgrh:
    def test_it_passes_the_arn_of_the_carts_db_stack(self, tmp_path):
        done, calls = _make_ngrh(tmp_path)
        assert done.returncode == 0, done.stdout + done.stderr
        (deploy,) = _deploys(calls)
        assert _overrides(deploy) == {
            "Env": "-t", "PrimaryRegion": "us-east-1", "StandbyRegion": "us-west-2", "CartsDbStackArn": STACK_ARN}
        assert deploy[deploy.index("--stack-name") + 1] == "ngrh-t"
        assert deploy[deploy.index("--template") + 1] == "./ngrh.yaml"
        assert deploy[deploy.index("--region") + 1] == "us-east-1"
        assert "WARNING" not in done.stdout

    def test_it_looks_the_stack_up_by_its_name_and_reads_its_id(self, tmp_path):
        _, calls = _make_ngrh(tmp_path)
        (lookup,) = _lookups(calls)
        assert lookup[lookup.index("--stack-name") + 1] == "carts-db-stack-t"
        assert lookup[lookup.index("--query") + 1] == "Stacks[0].StackId"
        assert lookup[lookup.index("--region") + 1] == "us-east-1"

    def test_it_looks_in_the_primary_region(self, tmp_path):
        done, calls = _make_ngrh(tmp_path, region="eu-west-1", standby="eu-central-1")
        assert done.returncode == 0, done.stdout + done.stderr
        (deploy,) = _deploys(calls)
        assert _overrides(deploy)["CartsDbStackArn"] == STACK_ARN
        assert deploy[deploy.index("--region") + 1] == "eu-west-1"

    def test_without_the_stack_it_still_deploys_with_an_empty_value_and_says_what_that_means(self, tmp_path):
        done, calls = _make_ngrh(tmp_path, arn="")
        assert done.returncode == 0, done.stdout + done.stderr
        (deploy,) = _deploys(calls)
        assert _overrides(deploy)["CartsDbStackArn"] == ""
        assert "WARNING: stack carts-db-stack-t was not found in us-east-1" in done.stdout
        assert "tag only" in done.stdout

    def test_the_lookup_comes_before_the_deploy(self, tmp_path):
        _, calls = _make_ngrh(tmp_path)
        order = [call[1] for call in calls if call[0] == "cloudformation"
                 and not (call[1] == "describe-stacks" and not call[call.index("--stack-name") + 1].startswith("carts-db-stack"))]
        assert order == ["describe-stacks", "deploy"]


# ---------------------------------------------------------------------------
# The README
# ---------------------------------------------------------------------------

def test_the_readme_says_the_cart_service_also_reads_the_carts_table_stack():
    readme = (REPO / "README.md").read_text()
    section = readme[readme.index("### How each service finds its resources"):readme.index("### Running Resilience Hub tests")]
    assert "carts-db-stack" in section
    assert "AWS::DynamoDB::GlobalTable" in section
    assert "`make ngrh` looks the stack up and passes its ARN as the template parameter `CartsDbStackArn`" in section
