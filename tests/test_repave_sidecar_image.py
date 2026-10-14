"""The weekly repave must leave the FIS sidecar image a working one.

The amazon-ssm-agent sidecar in every task definition runs an image the account
mirrors into its own ECR as ``amazon-ssm-agent<ENV>:latest``: the SSM agent's
public image plus the tools the sidecar and the FIS fault documents run in a
task (the task subnets have no internet route, so a task cannot install them).

On 2026-10-06 the repave overwrote that image with the bare public one. Step 2
of the repave mirrors every ``public.ecr.aws/<x>/<y>:<tag>`` it finds in
mirror-sidecar-buildspec.yml, and the FROM line of the sidecar's Dockerfile,
added to that file for the sidecar, is one. Every task started after the repave
ran a sidecar that exited 127 ("ps: command not found", "aws: command not
found"), so no task registered with SSM, and Resilience Hub's ECS fault failed
with "At least one ECS Task is not registered as a SSM managed instance". Nothing
failed in the repave, in CI, or in any alarm: the first sign was a fault test.

These tests run the buildspec's build-phase commands under ``sh`` against a stub
``docker``, the way CodeBuild runs them (stop at the first failing command), and
check where each pushed image came from. They hold:

* the repave pushes the sidecar repository an image it BUILT from the Dockerfile,
  never one it pulled, and still mirrors the observability images;
* an image missing any tool a task runs is not pushed, and the build ends before
  the services roll out;
* the Dockerfile the repave builds and the one mirror-sidecar-buildspec.yml
  writes for ``make mirror-sidecar-images`` are the same, and install every
  package the tools come from;
* a mirror list that comes out empty fails the build instead of mirroring nothing.

Run with:  pytest tests/test_repave_sidecar_image.py -v
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
# check that they fail against the version that overwrote the image.
SELF_UPDATE_TEMPLATE = Path(os.environ.get("SELF_UPDATE_TEMPLATE", DEPLOYMENT / "self-update.yaml"))
MIRROR_BUILDSPEC = DEPLOYMENT / "mirror-sidecar-buildspec.yml"
DOCKERFILE = DEPLOYMENT / "ssm-agent-sidecar.Dockerfile"

PRIMARY, STANDBY = "us-east-1", "us-west-2"
REGIONS = (PRIMARY, STANDBY)
ENV_SUFFIX = "-t"
TAG = "5d6e7f8"
ACCOUNT = "111111111111"
SIDECAR_REPO = "amazon-ssm-agent" + ENV_SUFFIX
BASE_IMAGE = "public.ecr.aws/amazon-ssm-agent/amazon-ssm-agent:latest"

# What each package puts in the image that something in a task runs: the
# commands it provides. python3-requests provides a module, not a command.
PROVIDES = {
    "jq": ["jq"],                      # sidecar start script
    "procps": ["ps", "pgrep"],         # start script (ps), AWSFIS-Run-Network-Packet-Loss (pgrep)
    "awscli": ["aws"],                 # start script
    "curl-minimal": ["curl"],          # start script
    "util-linux": ["setsid"],          # start script
    "at": ["at", "atd"],               # AWSFIS-Run-Network-Packet-Loss
    "bind-utils": ["dig"],             # AWSFIS-Run-Network-Packet-Loss
    "lsof": ["lsof"],                  # AWSFIS-Run-Network-Packet-Loss
    "iproute-tc": ["tc"],              # AWSFIS-Run-Network-Packet-Loss
    "python3": ["python3"],            # Resilience Hub's agent-install document
}
MODULES = {"python3-requests": "requests"}
REQUIRED_COMMANDS = sorted(c for commands in PROVIDES.values() for c in commands)

STUB_DOCKER = r'''#!/usr/bin/env python3
"""Stub `docker`. One JSON object per call in $STUB_LOG. `docker run ... -c "command -v X"`
fails for every X listed in $STUB_MISSING (and `import requests` when "requests" is
listed), like an image that lacks the tool."""
import json, os, re, sys

argv = sys.argv[1:]
entry = {"argv": argv}
if argv and argv[0] == "build" and argv[-1] == "-":
    entry["dockerfile"] = sys.stdin.read()
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps(entry) + "\n")

if argv and argv[0] == "run":
    missing = os.environ.get("STUB_MISSING", "").split()
    script = argv[-1]
    found = re.fullmatch(r"command -v (\S+)", script)
    if found and found.group(1) in missing:
        sys.exit(1)
    if "import requests" in script and "requests" in missing:
        sys.stderr.write("ModuleNotFoundError: No module named 'requests'\n")
        sys.exit(1)
sys.exit(0)
'''


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _build_commands():
    project = _load(SELF_UPDATE_TEMPLATE)["Resources"]["SelfUpdateProject"]
    return yaml.safe_load(project["Properties"]["Source"]["BuildSpec"])["phases"]["build"]["commands"]


def _load(path):
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


def _block(marker):
    blocks = [c for c in _build_commands() if marker in c]
    assert len(blocks) == 1, f"expected exactly one build command that mentions {marker}, found {len(blocks)}"
    return blocks[0]


def _dockerfile_lines(text):
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _installed_packages(lines):
    packages = set()
    for line in lines:
        for group in re.findall(r"dnf install -y ([^&]+)", line):
            packages.update(group.split())
    return packages


def _registry(region):
    return f"{ACCOUNT}.dkr.ecr.{region}.amazonaws.com"


@pytest.fixture
def repave(tmp_path):
    """Run build-phase commands under sh against the stub docker, in order, stopping at the first
    failure as CodeBuild does (the phase has on-failure: ABORT). Returns (return codes, docker calls)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "docker"
    stub.write_text(STUB_DOCKER)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "docker.log"

    def run(commands=None, missing=(), cwd=REPO):
        log.write_text("")
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", STUB_LOG=str(log), STUB_MISSING=" ".join(missing),
                   TAG=TAG, ENV_SUFFIX=ENV_SUFFIX, PRIMARY_REGION=PRIMARY, STANDBY_REGION=STANDBY, AWS_ACCOUNT_ID=ACCOUNT)
        codes = []
        for number, command in enumerate(_build_commands() if commands is None else commands):
            script = tmp_path / f"command-{number}.sh"
            script.write_text(command)
            # The buildspec sets no shell and its commands are written for POSIX sh.
            # Plain `sh` keeps them that way: it is dash on the Ubuntu CI runners.
            proc = subprocess.run(["sh", str(script)], env=env, cwd=cwd, capture_output=True, text=True, timeout=60)
            codes.append(proc.returncode)
            if proc.returncode != 0:
                break
        return codes, [json.loads(line) for line in log.read_text().splitlines()]

    return run


def _pushes(calls):
    """[(image reference, where the image came from)] for every docker push, in order. An image comes
    from a ("build", Dockerfile text), a ("pull", reference) or is ("unknown", ...) when the call that
    made it is not in the log (a real docker would refuse that tag)."""
    origin, pushed = {}, []
    for call in calls:
        argv = call["argv"]
        if argv[0] == "build":
            origin[argv[argv.index("-t") + 1]] = ("build", call.get("dockerfile"))
        elif argv[0] == "pull":
            origin[argv[1]] = ("pull", argv[1])
        elif argv[0] == "tag":
            origin[argv[2]] = origin.get(argv[1], ("unknown", argv[1]))
        elif argv[0] == "push":
            pushed.append((argv[1], origin.get(argv[1], ("unknown", argv[1]))))
    return pushed


def _mirrored_by_the_buildspec():
    """(repository, public image) for each image the mirror buildspec's own loop copies."""
    text = MIRROR_BUILDSPEC.read_text()
    found = re.findall(r'"([a-z-]+)\$\{ENV_SUFFIX\}:(public\.ecr\.aws/\S+?:[\w.]+)"', text)
    assert len(found) == 3, f"expected three mirrored images in {MIRROR_BUILDSPEC.name}, found {found}"
    return found


# ---------------------------------------------------------------------------
# the image the repave pushes
# ---------------------------------------------------------------------------

def test_the_repave_pushes_the_sidecar_repository_an_image_it_built(repave):
    codes, calls = repave()
    assert set(codes) == {0} and len(codes) == len(_build_commands())
    dockerfile = _dockerfile_lines(DOCKERFILE.read_text())
    for region in REGIONS:
        reference = f"{_registry(region)}/{SIDECAR_REPO}:latest"
        sources = [origin for image, origin in _pushes(calls) if image == reference]
        assert len(sources) == 1, f"{reference} pushed {len(sources)} times"
        kind, text = sources[0]
        assert kind == "build", f"{reference} was {kind}, not built from the Dockerfile: {text!r}"
        assert _dockerfile_lines(text) == dockerfile


def test_step_two_does_not_touch_the_sidecar_repository(repave):
    """The overwrite itself: the mirror step took the sidecar's FROM line for an image to copy."""
    codes, calls = repave([_block("mirror-sidecar-buildspec.yml")])
    assert codes == [0]
    touched = [c["argv"] for c in calls if any("amazon-ssm-agent" in part for part in c["argv"])]
    assert touched == [], f"the mirror step handled the sidecar image: {touched}"


def test_the_observability_images_are_still_mirrored(repave):
    codes, calls = repave([_block("mirror-sidecar-buildspec.yml")])
    assert codes == [0]
    expected = {(f"{_registry(region)}/{repo}{ENV_SUFFIX}:{public.rsplit(':', 1)[1]}", ("pull", public))
                for repo, public in _mirrored_by_the_buildspec() for region in REGIONS}
    assert set(_pushes(calls)) == expected
    assert {c["argv"][1] for c in calls if c["argv"][0] == "pull"} == {public for _, public in _mirrored_by_the_buildspec()}


# ---------------------------------------------------------------------------
# an image without a tool is not pushed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("missing", REQUIRED_COMMANDS + ["requests"])
def test_an_image_missing_a_tool_is_not_pushed_and_ends_the_build(repave, missing):
    codes, calls = repave(missing=[missing])
    assert codes[-1] != 0, f"the build went on with {missing} missing from the sidecar image"
    pushed = [image for image, _ in _pushes(calls) if f"/{SIDECAR_REPO}:" in image]
    assert pushed == [], f"pushed the sidecar image although {missing} is missing: {pushed}"


def test_the_checked_commands_are_the_ones_the_packages_provide():
    block = _block("ssm-agent-sidecar.Dockerfile")
    checked = re.search(r"for TOOL in ([^;]+); do", block)
    assert checked, "step 3 checks no tools"
    assert sorted(checked.group(1).split()) == REQUIRED_COMMANDS
    assert 'import requests' in block


def test_the_check_runs_in_the_image_just_built_before_anything_is_pushed(repave):
    codes, calls = repave([_block("ssm-agent-sidecar.Dockerfile")])
    assert codes == [0]
    order = [c["argv"][0] for c in calls]
    assert order[0] == "build" and "--pull" in calls[0]["argv"]
    assert order.index("push") > max(i for i, verb in enumerate(order) if verb == "run")
    image = calls[0]["argv"][calls[0]["argv"].index("-t") + 1]
    assert {c["argv"][4] for c in calls if c["argv"][0] == "run"} == {image}


# ---------------------------------------------------------------------------
# a mirror list that comes out empty
# ---------------------------------------------------------------------------

def _tree_with(tmp_path, buildspec_text):
    (tmp_path / "tree" / "deployment").mkdir(parents=True)
    if buildspec_text is not None:
        (tmp_path / "tree" / "deployment" / "mirror-sidecar-buildspec.yml").write_text(buildspec_text)
    return tmp_path / "tree"


def test_a_buildspec_that_names_no_mirrored_image_fails_the_build(repave, tmp_path):
    only_the_sidecar = f'          echo "FROM {BASE_IMAGE}"\n'
    codes, calls = repave([_block("mirror-sidecar-buildspec.yml")], cwd=_tree_with(tmp_path, only_the_sidecar))
    assert codes[-1] != 0
    assert calls == []


def test_a_missing_buildspec_fails_the_build(repave, tmp_path):
    codes, calls = repave([_block("mirror-sidecar-buildspec.yml")], cwd=_tree_with(tmp_path, None))
    assert codes[-1] != 0
    assert calls == []


# ---------------------------------------------------------------------------
# one Dockerfile, two builds
# ---------------------------------------------------------------------------

def test_the_dockerfile_installs_every_package_the_tools_come_from():
    installed = _installed_packages(_dockerfile_lines(DOCKERFILE.read_text()))
    missing = sorted((set(PROVIDES) | set(MODULES)) - installed)
    assert not missing, f"not installed into the sidecar image: {missing}"


def test_the_dockerfile_builds_from_the_ssm_agent_image():
    assert _dockerfile_lines(DOCKERFILE.read_text())[0] == f"FROM {BASE_IMAGE}"


def test_the_mirror_buildspec_writes_the_dockerfile_the_repave_builds():
    written = re.findall(r'^\s*echo "((?:FROM|RUN) [^"]*)"\s*$', MIRROR_BUILDSPEC.read_text(), re.M)
    assert written == _dockerfile_lines(DOCKERFILE.read_text())
