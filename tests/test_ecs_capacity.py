"""Every ECS service runs on on-demand Fargate.

With automatic failover, a fault that lasts past detection ends in a Region
failover, and the plan moves DNS only after the healthy Region has scaled up.
On Fargate Spot that scale-up depends on spare Spot capacity, and ECS does not
fall back to on-demand when Spot is short. Spot can also reclaim a task in the
middle of a resilience test, which then fails for a reason the test did not
inject (a carts task in test2 was reclaimed at 13:39 UTC on 2026-10-05).

The services keep the capacity provider strategy form rather than switching to
LaunchType: they have fixed names and Service Connect configuration, and moving
between the two forms is not a plain in-place update.

Run with:  pytest tests/test_ecs_capacity.py -v
"""

from pathlib import Path

import pytest
import yaml

DEPLOYMENT = Path(__file__).parent.parent / "deployment"

# The six application services, by ServiceName without the Env suffix.
APP_SERVICES = {"assets", "carts", "catalog", "checkout", "orders", "ui"}


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


def _ecs_services():
    """(template, logical id, properties) for every AWS::ECS::Service in deployment/."""
    found = []
    for path in sorted(DEPLOYMENT.rglob("*.y*ml")):
        text = path.read_text()
        if "AWS::ECS::Service" not in text:
            continue
        resources = (_load_template(path) or {}).get("Resources") or {}
        for lid, resource in resources.items():
            if isinstance(resource, dict) and resource.get("Type") == "AWS::ECS::Service":
                found.append((path.relative_to(DEPLOYMENT).as_posix(), lid, resource.get("Properties") or {}))
    return found


SERVICES = _ecs_services()


def test_finds_all_six_application_services():
    names = {props.get("ServiceName", "").replace("${Env}", "") for _, _, props in SERVICES}
    assert APP_SERVICES <= names, f"missing services: {sorted(APP_SERVICES - names)}"


@pytest.mark.parametrize("template,lid,props", SERVICES, ids=[f"{t}:{lid}" for t, lid, _ in SERVICES])
def test_service_runs_only_on_on_demand_fargate(template, lid, props):
    assert "LaunchType" not in props, f"{template} {lid}: use the capacity provider strategy, not LaunchType"
    strategy = props.get("CapacityProviderStrategy")
    assert strategy, f"{template} {lid}: no CapacityProviderStrategy"
    providers = [entry.get("CapacityProvider") for entry in strategy]
    assert providers == ["FARGATE"], f"{template} {lid}: capacity providers {providers}, expected only FARGATE"
    assert strategy[0].get("Weight", 0) >= 1, f"{template} {lid}: FARGATE needs a weight of at least 1"
