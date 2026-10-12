"""The weekly repave's service image builds (deployment/self-update.yaml, build step 1).

The builds download their dependencies, and a single download error ended two
of the three scheduled repaves from 2026-09-27 to 2026-10-11: Maven Central
reset the connection in the carts build (09-27) and gopkg.in answered 502 in
the catalog build (10-11). Step 1 now tries a failed build twice more before it
gives up. These tests run step 1 under ``sh`` against a stub ``docker`` and a
stub ``sleep`` (so the waits cost nothing) and pin that:

* a build that fails once or twice is retried, and the repave goes on;
* a build that keeps failing ends the repave after three attempts, before that
  service is pushed and before any later service is built;
* a clean run builds each service once and never waits.

Run with:  pytest tests/test_repave_image_builds.py -v
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).parent.parent
# Overridable so a mutated copy of the template can be tested against these tests.
SELF_UPDATE_TEMPLATE = Path(os.environ.get("SELF_UPDATE_TEMPLATE", REPO / "deployment" / "self-update.yaml"))

PRIMARY, STANDBY = "us-east-1", "us-west-2"
ENV_SUFFIX = "-t"
TAG = "5d6e7f8"
ACCOUNT = "111111111111"
SERVICES = ["catalog", "checkout", "ui", "carts", "assets", "orders"]

STUB_DOCKER = r'''#!/usr/bin/env python3
"""Stub `docker`: one JSON line per call in $STUB_LOG. For each SVC=N in $STUB_BUILD_FAILS
the first N `docker build -t SVC:...` calls fail, like a download error that clears."""
import json, os, sys

argv = sys.argv[1:]
entry = {"argv": argv, "code": 0}
if argv[:1] == ["build"]:
    service = argv[argv.index("-t") + 1].split(":")[0]
    failures = dict(item.split("=") for item in os.environ.get("STUB_BUILD_FAILS", "").split())
    with open(os.environ["STUB_LOG"]) as f:
        builds = sum(1 for line in f if json.loads(line).get("service") == service)
    entry.update(service=service, code=1 if builds < int(failures.get(service, 0)) else 0)
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps(entry) + "\n")
sys.exit(entry["code"])
'''

STUB_SLEEP = '#!/bin/sh\necho "$1" >> "$STUB_SLEEPS"\n'


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load(path):
    """The template, with CloudFormation short-form tags (!Ref, !Sub...) read as plain values."""
    class Loader(yaml.SafeLoader):
        pass

    def tag(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    Loader.add_multi_constructor("!", tag)
    loader = Loader(path.read_text())
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _step_one():
    project = _load(SELF_UPDATE_TEMPLATE)["Resources"]["SelfUpdateProject"]
    commands = yaml.safe_load(project["Properties"]["Source"]["BuildSpec"])["phases"]["build"]["commands"]
    blocks = [c for c in commands if "-t $SVC:$TAG source/$SRC" in c]
    assert len(blocks) == 1, f"expected one build command that builds the service images, found {len(blocks)}"
    return blocks[0]


def _builds(calls, service):
    """Exit codes of the docker builds of one service, in order."""
    return [c["code"] for c in calls if c.get("service") == service]


def _pushed(calls):
    """{service: [Regions it was pushed to, in order]}"""
    pushed = {}
    for call in calls:
        if call["argv"][:1] == ["push"]:
            registry, _, image = call["argv"][1].partition("/")
            service = image.split(":")[0].removesuffix(ENV_SUFFIX)
            pushed.setdefault(service, []).append(registry.split(".")[3])
    return pushed


@pytest.fixture
def step_one(tmp_path):
    """Run build step 1 under sh against the stubs. Returns (exit code, docker calls, waits in seconds)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, text in (("docker", STUB_DOCKER), ("sleep", STUB_SLEEP)):
        stub = bindir / name
        stub.write_text(text)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log, sleeps, script = tmp_path / "docker.log", tmp_path / "sleeps.log", tmp_path / "step-one.sh"
    script.write_text(_step_one())

    def run(build_fails=""):
        log.write_text("")
        sleeps.write_text("")
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", STUB_LOG=str(log), STUB_SLEEPS=str(sleeps),
                   STUB_BUILD_FAILS=build_fails, TAG=TAG, ENV_SUFFIX=ENV_SUFFIX, PRIMARY_REGION=PRIMARY,
                   STANDBY_REGION=STANDBY, AWS_ACCOUNT_ID=ACCOUNT)
        # The buildspec sets no shell and its commands are written for POSIX sh
        # (dash on the Ubuntu CI runners), as in test_repave_sidecar_image.py.
        proc = subprocess.run(["sh", str(script)], env=env, cwd=REPO, capture_output=True, text=True, timeout=60)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return proc.returncode, calls, [int(s) for s in sleeps.read_text().split()]

    return run


EVERY_SERVICE_IN_BOTH_REGIONS = {service: [PRIMARY, STANDBY] for service in SERVICES}


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_a_clean_run_builds_each_service_once_and_never_waits(step_one):
    code, calls, waits = step_one()
    assert code == 0
    assert [c["service"] for c in calls if "service" in c] == SERVICES
    assert _pushed(calls) == EVERY_SERVICE_IN_BOTH_REGIONS
    assert waits == []


def test_a_build_that_fails_once_is_retried_and_the_repave_goes_on(step_one):
    """The 2026-10-11 case: catalog's first build fails on a download, the next one works."""
    code, calls, waits = step_one(build_fails="catalog=1")
    assert code == 0, "the repave gave up on a build that worked the second time"
    assert _builds(calls, "catalog") == [1, 0]
    assert _pushed(calls) == EVERY_SERVICE_IN_BOTH_REGIONS
    assert len(waits) == 1 and waits[0] > 0


def test_the_third_attempt_counts(step_one):
    code, calls, waits = step_one(build_fails="ui=2")
    assert code == 0, "the repave gave up on a build that worked the third time"
    assert _builds(calls, "ui") == [1, 1, 0]
    assert _pushed(calls) == EVERY_SERVICE_IN_BOTH_REGIONS
    assert len(waits) == 2


@pytest.mark.parametrize("service", ["catalog", "ui", "orders"])
def test_a_build_that_keeps_failing_ends_the_repave_after_three_attempts(step_one, service):
    code, calls, waits = step_one(build_fails=f"{service}=99")
    assert code != 0, f"the repave went on although {service} never built"
    assert _builds(calls, service) == [1, 1, 1]
    earlier, later = SERVICES[:SERVICES.index(service)], SERVICES[SERVICES.index(service) + 1:]
    assert not [c for c in calls if c.get("service") in later], "built a service after the one that failed"
    assert _pushed(calls) == {s: [PRIMARY, STANDBY] for s in earlier}, f"pushed {service} although it never built"
    assert len(waits) == 2 and 0 < waits[0] < waits[1], f"waits between the attempts: {waits}"
