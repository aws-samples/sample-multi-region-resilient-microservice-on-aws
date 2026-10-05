"""Contract tests for tag-based Resilience Hub (NGRH) discovery.

Each NGRH service in deployment/ngrh.yaml discovers its resources through one
input source: resources whose ``service`` tag is the service's own name or
``shared``. Discovery is only as good as the tags, so these tests pin them:

* every ``service`` tag value is one of the six NGRH service names or
  ``shared`` (a typo such as ``carts`` silently drops the resource from
  discovery);
* each ECS service, its task definition and its task role carry the name of
  the NGRH service they belong to, and the data stores and load balancer
  carry their owner's;
* the ECS cluster carries no ``service`` tag, at resource or stack level.
  All six services run on it, so tagging it makes every service discover all
  the others through the cluster (seen in July 2026 when the model was built);
* every alarm carries a tag, so alarms added later are discovered too;
* the Makefile passes the stack tag for every single-owner or shared stack,
  and none for the apps stack that holds the cluster.

Run with:  pytest tests/test_ngrh_tag_discovery.py -v
"""

import re
from pathlib import Path

import pytest
import yaml

DEPLOYMENT = Path(__file__).parent.parent / "deployment"

NGRH_SERVICES = {"ui", "catalog", "cart", "checkout", "orders", "assets"}
TAG_VALUES = NGRH_SERVICES | {"shared"}

# Templates that tag resources one by one, because their stacks mix owners.
RESOURCE_TAGGED_TEMPLATES = ["ecs.yaml", "canaries.yaml", "monitoring.yml", "regionalBaseInfra.yaml"]

# ECS logical-id prefix -> NGRH service name.
ECS_PREFIXES = {
    "Ui": "ui",
    "Catalog": "catalog",
    "Carts": "cart",
    "Checkout": "checkout",
    "Orders": "orders",
    "Assets": "assets",
}

# Stack tag the Makefile must pass for each stack it deploys (stack name
# without the Env suffix). CloudFormation copies a stack tag onto every
# resource in the stack that supports tags.
EXPECTED_STACK_TAGS = {
    "baseVpc": "service=shared",
    "codebuild": "service=shared",
    "baseInfra": "service=shared",
    "gr": "service=shared",
    "region-switch": "service=shared",
    "canaries": "service=shared",
    "monitoring": "service=shared",
    "arc-dns-status": "service=shared",
    "catalog-db-stack": "service=catalog",
    "orders-dsql-stack": "service=orders",
    "carts-db-stack": "service=cart",
}

# The apps stack holds the ECS cluster, so it must never get a stack tag.
UNTAGGED_STACKS = {"apps"}

# Stacks outside the NGRH model: deployment tooling, test clients, the
# hand-built chaos experiment, the NGRH stack itself and the catalog
# reconciliation and secret-rotation helpers. Their resources stay out of
# discovery. A new stack must be added to one of these three groups, so
# whether its resources are discovered is a deliberate choice.
OUTSIDE_MODEL_STACKS = {
    "chaos",
    "client",
    "convergence-watcher",
    "crdr-catalog-ssm",
    "crdr-roles",
    "load-generator",
    "ngrh",
    "reconciliation-catalog-ssm",
    "secrets-rotation",
    "self-update",
}


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


def _resources(name):
    return _load_template(DEPLOYMENT / name)["Resources"]


def _service_tag(resource):
    """The resource's ``service`` tag value, or None."""
    tags = (resource.get("Properties") or {}).get("Tags") or []
    values = [t.get("Value") for t in tags if isinstance(t, dict) and t.get("Key") == "service"]
    assert len(values) <= 1, f"more than one service tag: {values}"
    return values[0] if values else None


def _stack_deploys():
    """(stack name without Env suffix, --tags value or None) per stack deploy.

    A stack deploy is a command with both --stack-name and --template or
    --template-file; describe and delete commands carry no template, and
    package commands carry no stack name.
    """
    text = (DEPLOYMENT / "Makefile").read_text()
    logical, buf = [], ""
    for line in text.splitlines():
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        logical.append(buf + line)
        buf = ""
    deploys = []
    for command in logical:
        for part in re.split(r"&&|;", command):
            if "--stack-name" in part and re.search(r"--template(-file)?\s", part):
                stack = re.search(r"--stack-name\s+([A-Za-z0-9-]+)", part).group(1)
                tags = re.search(r"--tags\s+(\S+)", part)
                deploys.append((stack, tags.group(1) if tags else None))
    return deploys


# ---------------------------------------------------------------------------
# Resource tags
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template", RESOURCE_TAGGED_TEMPLATES)
def test_every_service_tag_names_an_ngrh_service_or_shared(template):
    bad = {
        lid: _service_tag(r)
        for lid, r in _resources(template).items()
        if _service_tag(r) is not None and _service_tag(r) not in TAG_VALUES
    }
    assert not bad, f"{template}: service tag values outside {sorted(TAG_VALUES)}: {bad}"


def test_ecs_cluster_has_no_service_tag():
    clusters = {lid: r for lid, r in _resources("ecs.yaml").items() if r["Type"] == "AWS::ECS::Cluster"}
    assert clusters, "ecs.yaml defines no ECS cluster"
    for lid, cluster in clusters.items():
        assert _service_tag(cluster) is None, f"{lid} must stay untagged"


@pytest.mark.parametrize("kind", ["Service", "TaskDefinition", "TaskExecutionRole"])
@pytest.mark.parametrize("prefix,service", sorted(ECS_PREFIXES.items()))
def test_each_ecs_service_and_its_task_carry_their_service(prefix, service, kind):
    resources = _resources("ecs.yaml")
    lid = f"{prefix}{kind}"
    assert lid in resources, f"ecs.yaml has no {lid}"
    assert _service_tag(resources[lid]) == service


@pytest.mark.parametrize(
    "lid,service",
    [
        ("OrdersMqBroker", "orders"),
        ("MqSecret", "orders"),
        ("MqPassword", "orders"),
        ("sgMq", "orders"),
        ("CheckoutEcCluster", "checkout"),
        ("sgRedis", "checkout"),
        ("Alb", "shared"),
        ("sgAlb", "shared"),
        ("sgTask", "shared"),
        ("AlbTargetGroup", "ui"),
    ],
)
def test_data_stores_and_load_balancer_carry_their_owner(lid, service):
    assert _service_tag(_resources("ecs.yaml")[lid]) == service


def test_every_alarm_carries_a_service_tag():
    alarms = {
        lid: r
        for lid, r in _resources("monitoring.yml").items()
        if r["Type"] in ("AWS::CloudWatch::Alarm", "AWS::CloudWatch::CompositeAlarm")
    }
    assert alarms
    untagged = sorted(lid for lid, r in alarms.items() if _service_tag(r) is None)
    assert not untagged, f"alarms without a service tag: {untagged}"


def test_journey_canaries_carry_their_service():
    canaries = {lid: r for lid, r in _resources("canaries.yaml").items() if r["Type"] == "AWS::Synthetics::Canary"}
    assert canaries
    for lid, canary in canaries.items():
        journey = re.sub(r"^(local|remote|global)SyntheticsCanary", "", lid)
        expected = {"Cart": "cart", "Catalog": "catalog", "Orders": "orders"}.get(journey)
        # Home canaries exercise the whole application; the canaries stack
        # tag (service=shared) covers them.
        assert _service_tag(canary) == expected, f"{lid}"


@pytest.mark.parametrize(
    "lid,service",
    [
        ("UIRepo", "ui"),
        ("CatalogRepo", "catalog"),
        ("CartsRepo", "cart"),
        ("CheckoutRepo", "checkout"),
        ("OrdersRepo", "orders"),
        ("AssetsRepo", "assets"),
    ],
)
def test_service_image_repositories_carry_their_service(lid, service):
    assert _service_tag(_resources("regionalBaseInfra.yaml")[lid]) == service


# ---------------------------------------------------------------------------
# Stack tags
# ---------------------------------------------------------------------------

def test_makefile_stack_deploys_are_found():
    stacks = {stack for stack, _ in _stack_deploys()}
    assert set(EXPECTED_STACK_TAGS) | UNTAGGED_STACKS <= stacks


def test_makefile_passes_the_expected_stack_tag():
    wrong = [
        (stack, tags, EXPECTED_STACK_TAGS[stack])
        for stack, tags in _stack_deploys()
        if stack in EXPECTED_STACK_TAGS and tags != EXPECTED_STACK_TAGS[stack]
    ]
    assert not wrong, f"(stack, --tags, expected): {wrong}"


def test_apps_stack_gets_no_stack_tag():
    tagged = [(stack, tags) for stack, tags in _stack_deploys() if stack in UNTAGGED_STACKS and tags]
    assert not tagged, f"a stack tag would reach the ECS cluster: {tagged}"


def test_every_stack_deploy_is_classified():
    known = set(EXPECTED_STACK_TAGS) | UNTAGGED_STACKS | OUTSIDE_MODEL_STACKS
    unknown = sorted({stack for stack, _ in _stack_deploys()} - known)
    assert not unknown, (
        f"new stacks {unknown}: add each to EXPECTED_STACK_TAGS (and pass --tags), "
        "UNTAGGED_STACKS or OUTSIDE_MODEL_STACKS"
    )


# ---------------------------------------------------------------------------
# NGRH model
# ---------------------------------------------------------------------------

def _ngrh_services():
    resources = _load_template(DEPLOYMENT / "ngrh.yaml")["Resources"]
    return {lid: r for lid, r in resources.items() if r["Type"] == "AWS::ResilienceHubV2::Service"}


def test_ngrh_models_all_six_services():
    names = {r["Properties"]["Name"].replace("${Env}", "") for r in _ngrh_services().values()}
    assert names == NGRH_SERVICES


@pytest.mark.parametrize("lid", sorted(_ngrh_services()))
def test_each_service_discovers_by_its_tag_and_shared(lid):
    props = _ngrh_services()[lid]["Properties"]
    name = props["Name"].replace("${Env}", "")
    sources = props["InputSources"]
    assert len(sources) == 1, f"{lid}: expected one input source, got {sources}"
    config = sources[0]["ResourceConfiguration"]
    assert "CfnStackArn" not in config
    assert config["ResourceTags"] == [{"Key": "service", "Values": [name, "shared"]}]


def test_ngrh_template_takes_no_stack_arn_parameters():
    params = _load_template(DEPLOYMENT / "ngrh.yaml")["Parameters"]
    assert set(params) == {"Env", "PrimaryRegion", "StandbyRegion"}


def test_ngrh_outputs_one_arn_per_service():
    outputs = _load_template(DEPLOYMENT / "ngrh.yaml")["Outputs"]
    for lid in _ngrh_services():
        assert f"{lid}Arn" in outputs, f"missing output {lid}Arn"
        assert outputs[f"{lid}Arn"]["Value"] == f"{lid}.ServiceArn"


def test_gamma_script_uses_the_same_tag_input_sources():
    script = (DEPLOYMENT / "ngrh-gamma-create.sh").read_text()
    assert "cfnStackArn" not in script
    # The script builds the JSON inside a double-quoted shell string.
    unescaped = script.replace('\\"', '"')
    assert '"resourceTags":[{"key":"service","values":["$name","shared"]}]' in unescaped
    for name in NGRH_SERVICES:
        assert re.search(rf'add_tag_input_source "\$\w+_ARN" "{name}"', script), name
