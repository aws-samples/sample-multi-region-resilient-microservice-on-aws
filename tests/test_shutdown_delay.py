"""Contract tests for the back-end shutdown-delay containers.

During a deployment ECS stops the old back-end tasks, and callers' Service
Connect proxies can still send a stopping task requests for a few seconds after
its application gets the stop signal. Once the application has exited, the
task's own proxy answers those requests with 503, and ui turns them into failed
pages. In test2 on 2026-10-05 this happened up to ~5 s after an old task's stop
signal, for carts, checkout and orders.

A load balancer avoids this by deregistering a target and waiting before ECS
stops it, which is why ui (behind the ALB) never showed it. The back-ends are
reached only through Service Connect, so each of their task definitions has a
`shutdown-delay` container that depends on the application. A container
dependency reverses at shutdown (ECS stops a container only after every
container that depends on it has stopped), so the application gets its stop
signal only after the delay container exits, SHUTDOWN_DELAY_SECONDS after ECS
signals it, and keeps serving until then. In test2 the applications got their
stop signal ~26 s later than before: the 15 s delay, then ~11 s for ECS to move
on to the application.

Run with:  pytest tests/test_shutdown_delay.py -v
"""

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).parent.parent
DEPLOYMENT = REPO / "deployment"

DELAY = "shutdown-delay"

# Longest time after an old task's stop signal that ui still got 503s from it
# (test2, 2026-10-05), and the margin the delay must keep over it.
WORST_OBSERVED_STRAGGLER_SECONDS = 5
REQUIRED_MARGIN = 3

# ECS API limit on a container's stop timeout.
MAX_STOP_TIMEOUT = 120


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that keeps CloudFormation short-form tags as {"!Tag": value}."""


def _cfn_tag(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {"!" + tag_suffix: value}


_CfnLoader.add_multi_constructor("!", _cfn_tag)


def _load_template(path):
    # Drive the SafeLoader subclass directly rather than passing it to yaml.load
    # (see tests/test_yaml_loading.py).
    loader = _CfnLoader(path.read_text())
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


RESOURCES = _load_template(DEPLOYMENT / "ecs.yaml")["Resources"]
TASK_DEFINITIONS = {lid: r["Properties"] for lid, r in RESOURCES.items() if r["Type"] == "AWS::ECS::TaskDefinition"}
SERVICES = {lid: r["Properties"] for lid, r in RESOURCES.items() if r["Type"] == "AWS::ECS::Service"}


def _service_connect_only_back_ends():
    """{task definition logical id: application container name} for every
    service that serves through Service Connect and has no load balancer."""
    found = {}
    for props in SERVICES.values():
        served = (props.get("ServiceConnectConfiguration") or {}).get("Services") or []
        if not served or props.get("LoadBalancers"):
            continue
        task_definition = props["TaskDefinition"]["!Ref"]
        port_names = {s["PortName"] for s in served}
        apps = [
            c["Name"]
            for c in TASK_DEFINITIONS[task_definition]["ContainerDefinitions"]
            if {p.get("Name") for p in c.get("PortMappings", [])} & port_names
        ]
        assert len(apps) == 1, f"{task_definition}: expected one container serving {port_names}, found {apps}"
        found[task_definition] = apps[0]
    return found


BACK_ENDS = _service_connect_only_back_ends()


def _containers(lid):
    return {c["Name"]: c for c in TASK_DEFINITIONS[lid]["ContainerDefinitions"]}


def _delay(lid):
    delays = [c for c in TASK_DEFINITIONS[lid]["ContainerDefinitions"] if c["Name"] == DELAY]
    assert len(delays) == 1, f"{lid}: expected one {DELAY} container, found {len(delays)}"
    return delays[0]


def _delay_seconds(container):
    env = {e["Name"]: e["Value"] for e in container.get("Environment", [])}
    return int(env["SHUTDOWN_DELAY_SECONDS"])


def test_the_back_ends_are_the_five_service_connect_only_services():
    # Pins the derivation above: ui is behind the ALB, whose deregistration
    # covers its stops, so it needs no delay.
    assert sorted(BACK_ENDS.values()) == ["assets", "carts", "catalog", "checkout", "orders"]


def test_only_the_back_ends_have_a_delay():
    with_delay = {lid for lid in TASK_DEFINITIONS if DELAY in _containers(lid)}
    assert with_delay == set(BACK_ENDS)


@pytest.mark.parametrize("lid", sorted(BACK_ENDS))
def test_delay_holds_the_application(lid):
    delay = _delay(lid)
    assert delay["DependsOn"] == [{"ContainerName": BACK_ENDS[lid], "Condition": "START"}], (
        "the application must be the only container the delay depends on, so that it is the one held at shutdown"
    )
    assert delay["Essential"] is False, "the task must keep running if the delay container ever exits"
    assert "Memory" not in delay, "Service Connect does not support container memory limits"


@pytest.mark.parametrize("lid", sorted(BACK_ENDS))
def test_delay_outlasts_the_late_requests(lid):
    delay = _delay(lid)
    seconds = _delay_seconds(delay)
    assert seconds >= WORST_OBSERVED_STRAGGLER_SECONDS + REQUIRED_MARGIN
    assert seconds < delay["StopTimeout"] <= MAX_STOP_TIMEOUT, (
        "ECS kills the delay container at its stop timeout, which would release the application early"
    )


@pytest.mark.parametrize("lid", sorted(BACK_ENDS))
def test_delay_reuses_the_fis_sidecar_image(lid):
    # Every task already pulls this image, and it has bash and GNU sleep.
    containers = _containers(lid)
    assert _delay(lid)["Image"] == containers["amazon-ssm-agent"]["Image"]
    assert _delay(lid)["Command"][:2] == ["/bin/bash", "-c"]


def test_every_delay_is_the_same():
    delays = [_delay(lid) for lid in sorted(BACK_ENDS)]
    for key in ("Command", "Environment", "StopTimeout", "Image", "Cpu"):
        assert all(d.get(key) == delays[0].get(key) for d in delays), f"{DELAY} containers differ in {key}"


@pytest.mark.parametrize("lid", sorted(lid for lid in BACK_ENDS if "ecs-cwagent" in _containers(lid)))
def test_cloudwatch_agent_outlives_the_application(lid):
    # A START dependency on the agent makes ECS stop the agent after the
    # application, so traces and metrics cover the delay.
    app = _containers(lid)[BACK_ENDS[lid]]
    assert {"ContainerName": "ecs-cwagent", "Condition": "START"} in app["DependsOn"]


# The delay command itself, run with the host's bash.

COMMAND = _delay(sorted(BACK_ENDS)[0])["Command"][2]


def _start(seconds):
    env = dict(os.environ, SHUTDOWN_DELAY_SECONDS=str(seconds))
    # Its own process group, so the test can check that nothing is left behind.
    proc = subprocess.Popen(["bash", "-c", COMMAND], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    time.sleep(0.5)
    assert proc.poll() is None, "the delay container must keep running until it is asked to stop"
    return proc


def _group_is_gone(pgid, wait=2.0):
    # A killed child can stay in the process table for a moment until it is
    # reaped, so poll briefly.
    deadline = time.monotonic() + wait
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_command_holds_a_stop_then_exits_cleanly(stop_signal):
    proc = _start(2)
    try:
        asked = time.monotonic()
        proc.send_signal(stop_signal)
        out, err = proc.communicate(timeout=10)
        held = time.monotonic() - asked
        assert proc.returncode == 0, err
        assert 1.8 <= held < 6, f"held the stop for {held:.1f} s, expected about 2"
        assert "Stop requested: holding the application for 2 seconds" in out
        assert "Releasing the application" in out
        assert _group_is_gone(proc.pid), "the delay must not leave its sleep running"
    finally:
        if not _group_is_gone(proc.pid):
            os.killpg(proc.pid, signal.SIGKILL)


def test_command_rejects_a_bad_delay():
    env = dict(os.environ, SHUTDOWN_DELAY_SECONDS="15s")
    proc = subprocess.run(["bash", "-c", COMMAND], env=env, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 1
    assert "SHUTDOWN_DELAY_SECONDS must be a whole number of seconds" in proc.stderr
