"""Behavioural tests for the weekly repave's rollout step (self-update.yaml).

The repave moves the apps stack to the new image Tag with UpdateStack and
--use-previous-template. UpdateStack is strict about the parameter list:

* a parameter left off the list falls back to its template default, so leaving
  off Env (default '') would rename every resource in the stack;
* a parameter left off that has no default fails the call ("Parameters:
  [VpcId] must have values");
* UsePreviousValue for a key the stack does not have fails the call.

The rollout used to name the parameters to keep: Env, PrimaryRegion,
StandbyRegion and KmsKey. ecs.yaml gained VpcId, which has no default, on
2026-07-10, a month before the repave was added on 2026-08-13. So the repave
could never move an apps stack deployed from main's own ecs.yaml to a new
commit: UpdateStack refused the call before anything rolled out. The
long-lived -dev install kept repaving only because its apps stack predates the
VpcId parameter. The rollout now reads the stack's own parameter list and keeps
every parameter except Tag.

A second hole of the same kind sat in the same-commit path: the service list
was read inside a for-loop's word list, where `set -e` does not see a failed
command, so a failed ListServices skipped every forced deployment and the build
still succeeded.

These tests run the rollout block of the deployed buildspec under `sh` against
a stub `aws` that enforces the three rules above. The stub stacks are built
from ecs.yaml's own Parameters section, so a parameter added there later is
covered without touching this file.

Run with:  pytest tests/test_repave_parameters.py -v
"""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).parent.parent
DEPLOYMENT = REPO / "deployment"
# Overridable so the tests can be pointed at an older copy of the template, to
# check that they fail against the hard-coded parameter list.
SELF_UPDATE_TEMPLATE = Path(os.environ.get("SELF_UPDATE_TEMPLATE", DEPLOYMENT / "self-update.yaml"))
ECS_TEMPLATE = DEPLOYMENT / "ecs.yaml"

PRIMARY, STANDBY = "us-east-1", "us-west-2"
ENV_SUFFIX = "-t"
STACK = "apps" + ENV_SUFFIX
ACCOUNT = "111111111111"
OLD_TAG, NEW_TAG = "1a2b3c4", "5d6e7f8"
SERVICES = ("catalog", "checkout", "ui", "carts", "assets", "orders")

# Values a live stack would hold. A template parameter not listed here gets
# "live-<name>", so a parameter added to ecs.yaml later still gets a value.
LIVE_VALUES = {
    "Env": ENV_SUFFIX,
    "PrimaryRegion": PRIMARY,
    "StandbyRegion": STANDBY,
    "Tag": OLD_TAG,
    "KmsKey": "retail-store-ar" + ENV_SUFFIX,
    "VpcId": "vpc-0a1b2c3d4e5f60718",
}

STUB_AWS = r'''#!/usr/bin/env python3
"""Stub `aws` for the repave rollout. State in $STUB_STATE (JSON), one JSON argv per
line in $STUB_LOG. UpdateStack applies CloudFormation's parameter rules: an omitted
parameter with a default is reset to it (recorded under "resets"), an omitted
parameter without one fails, and a key the stack does not have fails."""
import json, os, sys

argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\n")
state_path = os.environ["STUB_STATE"]
state = json.load(open(state_path))

def save():
    json.dump(state, open(state_path, "w"))

def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None

def fail(msg, code=254):
    sys.stderr.write("\nAn error occurred " + msg + "\n")
    sys.exit(code)

def unhandled():
    # Loud on purpose: a call or query this stub does not model is a change the
    # tests have not been taught about.
    sys.stderr.write("stub aws: unhandled call: %s\n" % json.dumps(argv))
    sys.exit(255)

svc, op = argv[0], argv[1]
region = opt("--region")

if svc == "cloudformation":
    name = opt("--stack-name")
    stack = state["stacks"].get(region, {}).get(name)
    if op in ("describe-stacks", "update-stack") and stack is None:
        fail("(ValidationError) when calling the %s operation: Stack with id %s does not exist"
             % ("DescribeStacks" if op == "describe-stacks" else "UpdateStack", name))

    if op == "describe-stacks":
        query = opt("--query")
        params = stack["parameters"]
        if query == "Stacks[0].Parameters[?ParameterKey=='Tag'].ParameterValue":
            print("\t".join(p["value"] for p in params if p["key"] == "Tag"))
        elif query == "Stacks[0].Parameters[].ParameterKey":
            print("\t".join(p["key"] for p in params))
        else:
            unhandled()
        sys.exit(0)

    if op == "update-stack":
        if region in state.get("update_fails", []):
            fail("(AccessDenied) when calling the UpdateStack operation: User is not authorized "
                 "to perform: cloudformation:UpdateStack (stub-induced)")
        if "--use-previous-template" not in argv:
            unhandled()   # the repave must never apply a template of its own
        known = {p["key"]: p for p in stack["parameters"]}
        given = {}
        i = argv.index("--parameters") + 1
        while i < len(argv) and not argv[i].startswith("--"):
            fields = dict(f.split("=", 1) for f in argv[i].split(","))
            key = fields.pop("ParameterKey")
            if key in given:
                fail("(ValidationError) when calling the UpdateStack operation: "
                     "Parameter %s is specified more than once" % key)
            given[key] = fields
            i += 1
        for key, fields in given.items():
            keep = fields.get("UsePreviousValue") == "true"
            if key not in known:
                if keep:
                    fail("(ValidationError) when calling the UpdateStack operation: Invalid input for "
                         "parameter key %s. Cannot specify usePreviousValue as true for a parameter key "
                         "not in the previous template" % key)
                fail("(ValidationError) when calling the UpdateStack operation: "
                     "Parameters: [%s] do not exist in the template" % key)
            if keep and "ParameterValue" in fields:
                fail("(ValidationError) when calling the UpdateStack operation: Invalid input for "
                     "parameter key %s. Cannot specify usePreviousValue as true and a value" % key)
        missing = [k for k in known if k not in given and "default" not in known[k]]
        if missing:
            fail("(ValidationError) when calling the UpdateStack operation: "
                 "Parameters: [%s] must have values" % ", ".join(missing))
        for key, p in known.items():
            fields = given.get(key)
            if fields is None:
                state.setdefault("resets", []).append([region, key, p["value"], p["default"]])
                p["value"] = p["default"]
            elif fields.get("UsePreviousValue") != "true":
                p["value"] = fields.get("ParameterValue", "")
        save()
        print(json.dumps({"StackId": "arn:aws:cloudformation:%s:111111111111:stack/%s/stub" % (region, name)}))
        sys.exit(0)

    if op == "wait" and argv[2] == "stack-update-complete":
        sys.exit(0)

if svc == "ecs":
    cluster = state["clusters"][region]
    if op == "list-clusters":
        print(cluster["arn"])
        sys.exit(0)
    if op == "list-services":
        if region in state.get("list_services_fails", []):
            fail("(AccessDeniedException) when calling the ListServices operation: "
                 "not authorized to perform: ecs:ListServices (stub-induced)")
        print("\t".join(cluster["services"]))
        sys.exit(0)
    if op == "update-service":
        print(opt("--service").rsplit("/", 1)[-1])
        sys.exit(0)

if svc == "stepfunctions" and op == "start-execution":
    print(json.dumps({"executionArn": opt("--state-machine-arn") + ":" + opt("--name")}))
    sys.exit(0)

unhandled()
'''


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags (!Sub, !Ref...)."""


def _cfn_tag(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_CfnLoader.add_multi_constructor("!", _cfn_tag)


def _load_template(path):
    # Drive the SafeLoader subclass directly rather than passing it to yaml.load
    # (see tests/test_yaml_loading.py).
    loader = _CfnLoader(path.read_text())
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _buildspec():
    project = _load_template(SELF_UPDATE_TEMPLATE)["Resources"]["SelfUpdateProject"]
    return project["Properties"]["Source"]["BuildSpec"]


def _rollout_block():
    commands = yaml.safe_load(_buildspec())["phases"]["post_build"]["commands"]
    blocks = [c for c in commands if "update-stack" in c]
    assert len(blocks) == 1, "expected exactly one post_build command that updates the apps stack"
    return blocks[0]


def _ecs_parameters():
    """The apps stack's parameters as DescribeStacks lists them for a stack deployed
    from ecs.yaml: every template parameter, its live value, and its default when the
    template has one."""
    params = []
    for name, spec in _load_template(ECS_TEMPLATE)["Parameters"].items():
        p = {"key": name, "value": LIVE_VALUES.get(name, "live-" + name)}
        if "Default" in spec:
            p["default"] = str(spec["Default"])
        params.append(p)
    return params


def _with_tag(params, tag):
    return [dict(p, value=tag) if p["key"] == "Tag" else p for p in params]


def _cluster(region):
    arn = f"arn:aws:ecs:{region}:{ACCOUNT}:cluster/{STACK}-EcsCluster-stub"
    return {"arn": arn, "services": [f"arn:aws:ecs:{region}:{ACCOUNT}:service/{STACK}-EcsCluster-stub/{s}"
                                     for s in SERVICES]}


def _state(primary=None, standby=None, **extra):
    return dict(
        stacks={PRIMARY: {STACK: {"parameters": primary if primary is not None else _ecs_parameters()}},
                STANDBY: {STACK: {"parameters": standby if standby is not None else _ecs_parameters()}}},
        clusters={region: _cluster(region) for region in (PRIMARY, STANDBY)},
        **extra,
    )


def _opt(argv, name):
    return argv[argv.index(name) + 1]


def _calls_of(calls, svc, op, region=None):
    return [c for c in calls if c[:2] == [svc, op] and (region is None or _opt(c, "--region") == region)]


def _parameters(argv):
    """{key: fields} for one update-stack call, e.g. {"Tag": {"ParameterValue": "..."}}."""
    i = argv.index("--parameters") + 1
    items = {}
    while i < len(argv) and not argv[i].startswith("--"):
        fields = dict(f.split("=", 1) for f in argv[i].split(","))
        items[fields.pop("ParameterKey")] = fields
        i += 1
    return items


def _live(final, region):
    return {p["key"]: p["value"] for p in final["stacks"][region][STACK]["parameters"]}


@pytest.fixture
def rollout(tmp_path):
    """Run the buildspec's rollout block against the stub; returns (proc, calls, final_state)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "aws"
    stub.write_text(STUB_AWS)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    script = tmp_path / "rollout.sh"
    script.write_text(_rollout_block())
    state_path, log_path = tmp_path / "state.json", tmp_path / "calls.log"

    def run(state):
        state_path.write_text(json.dumps(state))
        log_path.write_text("")
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
                   STUB_STATE=str(state_path), STUB_LOG=str(log_path),
                   TAG=NEW_TAG, ENV_SUFFIX=ENV_SUFFIX, PRIMARY_REGION=PRIMARY, STANDBY_REGION=STANDBY,
                   AWS_ACCOUNT_ID=ACCOUNT, CODEBUILD_BUILD_ID="mr-app-self-update-t:stub")
        # The buildspec sets no shell and its commands are written for POSIX sh.
        # Plain `sh` keeps them that way: it is dash on the Ubuntu CI runners.
        proc = subprocess.run(["sh", str(script)], env=env, capture_output=True, text=True, timeout=60)
        calls = [json.loads(line) for line in log_path.read_text().splitlines()]
        return proc, calls, json.loads(state_path.read_text())

    return run


# ---------------------------------------------------------------------------
# The buildspec names no apps parameter but the one it sets
# ---------------------------------------------------------------------------

def test_the_buildspec_names_no_apps_parameter_but_tag():
    spec = _buildspec()
    named = set(re.findall(r"ParameterKey=([A-Za-z0-9]+)", spec))
    assert named == {"Tag"}, (
        f"self-update.yaml hard-codes apps parameters {sorted(named - {'Tag'})}; "
        "read them from the stack instead"
    )
    assert re.findall(r"ParameterKey=(\S+?),UsePreviousValue=true", spec) == ["$K"]


# ---------------------------------------------------------------------------
# Tag moves, everything else keeps its value
# ---------------------------------------------------------------------------

def test_every_parameter_but_tag_keeps_its_value(rollout):
    proc, calls, final = rollout(_state())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    names = [p["key"] for p in _ecs_parameters()]
    for region in (PRIMARY, STANDBY):
        (call,) = _calls_of(calls, "cloudformation", "update-stack", region)
        params = _parameters(call)
        assert sorted(params) == sorted(names)
        assert params["Tag"] == {"ParameterValue": NEW_TAG}
        assert {k: v for k, v in params.items() if k != "Tag"} == \
            {n: {"UsePreviousValue": "true"} for n in names if n != "Tag"}
        expected = {p["key"]: p["value"] for p in _ecs_parameters()}
        assert _live(final, region) == dict(expected, Tag=NEW_TAG)
    assert final.get("resets", []) == []

    # The two-pass shape the rollout already had: both updates are submitted
    # before any wait, and each Region is handed to its watcher on the CFN path.
    updates = [i for i, c in enumerate(calls) if c[:2] == ["cloudformation", "update-stack"]]
    first_wait = next(i for i, c in enumerate(calls) if c[:2] == ["cloudformation", "wait"])
    assert len(updates) == 2 and max(updates) < first_wait
    starts = _calls_of(calls, "stepfunctions", "start-execution")
    assert {_opt(c, "--region"): json.loads(_opt(c, "--input"))["path"] for c in starts} == \
        {PRIMARY: "CFN", STANDBY: "CFN"}


def test_a_parameter_added_to_the_template_later_is_kept(rollout):
    # One without a default (the VpcId case) and one with a default that differs
    # from its live value (the Env case, where an omission would reset it).
    extra = [{"key": "FutureSetting", "value": "live-setting"},
             {"key": "FutureFlag", "value": "on", "default": "off"}]
    params = _ecs_parameters() + extra
    proc, calls, final = rollout(_state(primary=params, standby=params))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for region in (PRIMARY, STANDBY):
        (call,) = _calls_of(calls, "cloudformation", "update-stack", region)
        named = _parameters(call)
        assert named["FutureSetting"] == {"UsePreviousValue": "true"}
        assert named["FutureFlag"] == {"UsePreviousValue": "true"}
        assert _live(final, region)["FutureFlag"] == "on"
    assert final.get("resets", []) == []


@pytest.mark.parametrize("keys", [
    # The -dev install's apps stack today, in the order DescribeStacks returned it
    # on 2026-10-02: an older template, without VpcId.
    ["Tag", "Env", "StandbyRegion", "PrimaryRegion", "KmsKey"],
    # A stack missing parameters the old hard-coded list named.
    ["Env", "PrimaryRegion", "Tag", "VpcId"],
], ids=["dev-install-without-vpcid", "without-standbyregion-and-kmskey"])
def test_only_the_stacks_own_parameters_are_named(rollout, keys):
    by_key = {p["key"]: p for p in _ecs_parameters()}
    params = [by_key[k] for k in keys]
    proc, calls, final = rollout(_state(primary=params, standby=params))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for region in (PRIMARY, STANDBY):
        (call,) = _calls_of(calls, "cloudformation", "update-stack", region)
        assert sorted(_parameters(call)) == sorted(keys)
        assert _live(final, region)["Tag"] == NEW_TAG
    assert final.get("resets", []) == []


def test_a_stack_listing_no_tag_parameter_is_left_alone(rollout):
    # A list without Tag is not this stack's list. Updating with it would reset
    # whatever it left out, so the rollout stops before submitting anything.
    no_tag = [p for p in _ecs_parameters() if p["key"] != "Tag"]
    proc, calls, final = rollout(_state(primary=no_tag))
    assert proc.returncode != 0
    assert "lists no Tag parameter" in proc.stderr
    assert not _calls_of(calls, "cloudformation", "update-stack")
    assert not _calls_of(calls, "stepfunctions", "start-execution")


# ---------------------------------------------------------------------------
# The rollout's existing safety checks still hold
# ---------------------------------------------------------------------------

def test_a_rejected_update_fails_the_build_before_the_handoff(rollout):
    proc, calls, final = rollout(_state(update_fails=[STANDBY]))
    assert proc.returncode != 0
    assert "stub-induced" in proc.stderr
    assert len(_calls_of(calls, "cloudformation", "update-stack", PRIMARY)) == 1
    assert not _calls_of(calls, "stepfunctions", "start-execution")


def test_a_region_already_on_the_commit_gets_forced_deployments(rollout):
    # Primary moves to the new commit through CloudFormation; standby is already
    # on it, so its services are forced onto the rebuilt images instead.
    proc, calls, final = rollout(_state(standby=_with_tag(_ecs_parameters(), NEW_TAG)))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(_calls_of(calls, "cloudformation", "update-stack", PRIMARY)) == 1
    assert not _calls_of(calls, "cloudformation", "update-stack", STANDBY)
    forced = _calls_of(calls, "ecs", "update-service")
    assert {_opt(c, "--region") for c in forced} == {STANDBY}
    assert len(forced) == len(SERVICES) and all("--force-new-deployment" in c for c in forced)
    starts = _calls_of(calls, "stepfunctions", "start-execution")
    assert {_opt(c, "--region"): json.loads(_opt(c, "--input"))["path"] for c in starts} == \
        {PRIMARY: "CFN", STANDBY: "ECS"}
    assert [_opt(c, "--region") for c in _calls_of(calls, "cloudformation", "wait")] == [PRIMARY]


def test_a_failed_service_listing_fails_the_build(rollout):
    # Both Regions are on the commit already. When the primary's service list
    # cannot be read, the build must fail rather than skip the forced deployments
    # and hand a no-op rollout to the watchers as a success.
    on_commit = _with_tag(_ecs_parameters(), NEW_TAG)
    proc, calls, final = rollout(_state(primary=on_commit, standby=on_commit, list_services_fails=[PRIMARY]))
    assert proc.returncode != 0
    assert "ListServices" in proc.stderr
    assert not _calls_of(calls, "ecs", "update-service")
    assert not _calls_of(calls, "stepfunctions", "start-execution")
