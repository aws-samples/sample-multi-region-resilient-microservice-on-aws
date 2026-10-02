"""Tests for container start order in deployment/ecs.yaml.

Four task definitions load a language agent from a shared task volume: an
``init`` container copies the agent into the volume, and the application
container's runtime loads it at startup (``JAVA_TOOL_OPTIONS=-javaagent:...``
for the Java services, ``NODE_OPTIONS=--require ...`` for checkout). Without a
``DependsOn`` between the two, ECS starts both containers at the same time and
the outcome depends on which one wins.

Observed live on 2026-09-28 in the long-lived install: seven ``ui`` tasks in
us-east-1 and one in us-west-2 exited within a second of starting with

    Error opening zip file or JAR manifest missing : /otel-auto-instrumentation/javaagent.jar
    agent library failed to init: instrument

and ECS replaced each one. Over the preceding 14 days the same log line
appeared 21 times across ``carts``, ``ui`` and ``orders``, always on days when
tasks were being placed. ``checkout`` has declared
``DependsOn: [{ContainerName: init, Condition: SUCCESS}]`` since the initial
commit and has never logged the failure.

These tests derive the consumer/producer pairs from the template instead of
naming services: any container whose runtime loads a file from a mounted
volume must depend, with ``Condition: SUCCESS``, on the non-essential container
that writes into that volume. A new service that copies the pattern without
the ordering fails here.

Run with:  pytest tests/test_ecs_init_order.py -v
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
ECS_TEMPLATE = REPO_ROOT / "deployment" / "ecs.yaml"

# Environment variables through which a runtime loads an agent before the
# application starts, and the option inside them that names the agent file.
AGENT_OPTIONS = {
    "JAVA_TOOL_OPTIONS": re.compile(r"-javaagent:(\S+)"),
    "NODE_OPTIONS": re.compile(r"--require\s+(\S+)"),
}

# The pairs the template is known to carry. The derivation below must find at
# least these, so a change that stops it from recognising the pattern cannot
# turn the ordering tests into a vacuous pass.
KNOWN_CONSUMERS = {
    ("CartsTaskDefinition", "carts"),
    ("CheckoutTaskDefinition", "checkout"),
    ("OrdersTaskDefinition", "orders"),
    ("UiTaskDefinition", "ui"),
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags (!Sub, !Ref...).

    Tagged nodes are loaded as plain Python values; the tests only read
    container names, mount points, environment variables and DependsOn lists,
    which are untagged.
    """


def _cfn_tag(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_CfnLoader.add_multi_constructor("!", _cfn_tag)


def load_template(path):
    # Drive the SafeLoader subclass directly rather than passing it to yaml.load
    # as the Loader argument: same parse, no yaml.load call. Bandit's B506 (and
    # the ACAT scan built on it) accepts only the literal SafeLoader name there,
    # so a safe subclass handed to yaml.load is reported as an unsafe load.
    loader = _CfnLoader(path.read_text())
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def task_definitions(template):
    return {
        name: resource["Properties"]
        for name, resource in template["Resources"].items()
        if resource.get("Type") == "AWS::ECS::TaskDefinition"
    }


def agent_paths(container):
    """Files the container's runtime loads at startup, from AGENT_OPTIONS."""
    paths = []
    for env in container.get("Environment", []):
        pattern = AGENT_OPTIONS.get(env.get("Name"))
        if pattern is not None:
            paths.extend(pattern.findall(str(env.get("Value", ""))))
    return paths


def volume_for(container, path):
    """SourceVolume of the mount that contains ``path``, or None."""
    for mount in container.get("MountPoints", []):
        if path.startswith(mount["ContainerPath"].rstrip("/") + "/"):
            return mount["SourceVolume"]
    return None


def mounts_volume(container, volume):
    return any(m["SourceVolume"] == volume for m in container.get("MountPoints", []))


def agent_consumers(template):
    """Every (task definition name, consumer container, shared volume) triple.

    A consumer is a container whose agent file lives on a task volume it
    mounts; the file has to be produced by some other container before the
    runtime starts.
    """
    triples = []
    for td_name, props in task_definitions(template).items():
        for container in props["ContainerDefinitions"]:
            volumes = {volume_for(container, p) for p in agent_paths(container)}
            for volume in sorted(v for v in volumes if v is not None):
                triples.append((td_name, container, volume))
    return triples


TEMPLATE = load_template(ECS_TEMPLATE)
CONSUMERS = agent_consumers(TEMPLATE)
CONSUMER_IDS = [f"{td}/{c['Name']}" for td, c, _ in CONSUMERS]


def producers_of(td_name, consumer, volume):
    """Other containers in the task that mount the same volume."""
    containers = task_definitions(TEMPLATE)[td_name]["ContainerDefinitions"]
    return [c for c in containers if c is not consumer and mounts_volume(c, volume)]


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_derivation_finds_the_known_consumers():
    found = {(td, c["Name"]) for td, c, _ in CONSUMERS}
    missing = KNOWN_CONSUMERS - found
    assert not missing, f"agent consumers no longer recognised: {sorted(missing)}"


@pytest.mark.parametrize("td_name, consumer, volume", CONSUMERS, ids=CONSUMER_IDS)
def test_agent_volume_has_one_non_essential_producer(td_name, consumer, volume):
    producers = producers_of(td_name, consumer, volume)
    names = [p["Name"] for p in producers]
    assert len(producers) == 1, (
        f"{td_name}: volume {volume!r} is mounted by {names}; exactly one other "
        f"container should populate it for {consumer['Name']!r}"
    )
    assert producers[0].get("Essential") is False, (
        f"{td_name}: {names[0]!r} must be Essential: false, otherwise the task "
        f"stops when the copy finishes and a SUCCESS dependency on it is invalid"
    )


@pytest.mark.parametrize("td_name, consumer, volume", CONSUMERS, ids=CONSUMER_IDS)
def test_consumer_waits_for_producer_success(td_name, consumer, volume):
    producers = producers_of(td_name, consumer, volume)
    assert producers, f"{td_name}: nothing populates {volume!r}"
    producer = producers[0]["Name"]
    depends_on = consumer.get("DependsOn", [])
    assert {"ContainerName": producer, "Condition": "SUCCESS"} in depends_on, (
        f"{td_name}: container {consumer['Name']!r} loads an agent from volume "
        f"{volume!r} but does not declare DependsOn {producer!r} SUCCESS; ECS "
        f"starts both containers at once and the runtime can start before the "
        f"agent file exists (DependsOn is {depends_on!r})"
    )
