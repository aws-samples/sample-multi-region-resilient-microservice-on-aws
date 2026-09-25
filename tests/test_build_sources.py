"""Tests for the image-build path: the weekly repave and the deploy-time
CodeBuild project.

Two defects observed live on 2026-09-25 (build mr-app-self-update-dev:4822908b):

1. Docker Hub rate-limited the anonymous base-image pulls (HTTP 429) so five
   of six service images never built.
2. The buildspec had no ``on-failure`` on the build phase, so CodeBuild fell
   through to post_build, which rolled the apps stack forward to a Tag whose
   images did not exist. Both regions sat in UPDATE_IN_PROGRESS for two hours
   pulling images that were never pushed.

These tests pin the fixes: base images come from a named registry (ECR Public
Gallery), both build roles can authenticate to it, and a failed image build
aborts the repave before anything is rolled out.

Run with:  pytest tests/test_build_sources.py -v
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
DEPLOYMENT_DIR = REPO_ROOT / "deployment"
SOURCE_DIR = REPO_ROOT / "source"
SELF_UPDATE_TEMPLATE = DEPLOYMENT_DIR / "self-update.yaml"
CODEBUILD_TEMPLATE = DEPLOYMENT_DIR / "codebuild.yaml"

ECR_PUBLIC_LOGIN = "docker login --username AWS --password-stdin public.ecr.aws"
ECR_PUBLIC_ACTIONS = {"ecr-public:GetAuthorizationToken", "sts:GetServiceBearerToken"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags (!Sub, !Ref...).

    Tagged nodes are loaded as plain Python values; the tests only read the
    inline BuildSpec string and IAM action lists, which are untagged.
    """


def _cfn_tag(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_CfnLoader.add_multi_constructor("!", _cfn_tag)


def load_template(path):
    return yaml.load(path.read_text(), Loader=_CfnLoader)


def buildspec_of(template, project_logical_id):
    spec = template["Resources"][project_logical_id]["Properties"]["Source"]["BuildSpec"]
    return yaml.safe_load(spec)


def phase_commands(buildspec, phase):
    return "\n".join(buildspec["phases"][phase]["commands"])


def iam_actions(template, role_logical_id):
    actions = set()
    for policy in template["Resources"][role_logical_id]["Properties"]["Policies"]:
        for stmt in policy["PolicyDocument"]["Statement"]:
            action = stmt["Action"]
            actions.update([action] if isinstance(action, str) else action)
    return actions


def dockerfiles():
    return sorted(SOURCE_DIR.glob("*/Dockerfile"))


def from_images(dockerfile):
    """Yield the image reference of every FROM that is not a stage alias."""
    aliases = set()
    for line in dockerfile.read_text().splitlines():
        m = re.match(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+[Aa][Ss]\s+(\S+))?", line)
        if not m:
            continue
        image, alias = m.group(1), m.group(2)
        if image in aliases:
            continue
        if alias:
            aliases.add(alias)
        yield image


def registry_of(image):
    """Return the registry host of an image reference, or None for Docker Hub.

    ``golang:1.25`` and ``library/golang:1.25`` resolve to docker.io implicitly;
    an explicit host is the first path segment when it contains a dot or a port.
    """
    first = image.split("/", 1)[0]
    if "/" not in image or not ("." in first or ":" in first):
        return None
    return first


# ---------------------------------------------------------------------------
# Dockerfiles: no anonymous Docker Hub pulls
# ---------------------------------------------------------------------------

def test_dockerfiles_exist():
    assert len(dockerfiles()) >= 6, "expected one Dockerfile per service under source/"


@pytest.mark.parametrize("dockerfile", dockerfiles(), ids=lambda p: p.parent.name)
def test_dockerfile_bases_name_a_registry(dockerfile):
    """Every FROM must name its registry. A bare ``golang:1.25`` is an anonymous
    docker.io pull, which is what the 429s hit."""
    images = list(from_images(dockerfile))
    assert images, f"{dockerfile} has no FROM line"
    for image in images:
        registry = registry_of(image)
        assert registry is not None, (
            f"{dockerfile.relative_to(REPO_ROOT)}: FROM {image} is an implicit Docker Hub pull; "
            "pull the base from public.ecr.aws (or another named registry) instead"
        )
        assert registry != "docker.io", (
            f"{dockerfile.relative_to(REPO_ROOT)}: FROM {image} still pulls from Docker Hub"
        )


# ---------------------------------------------------------------------------
# self-update.yaml (weekly repave)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def self_update():
    return load_template(SELF_UPDATE_TEMPLATE)


@pytest.fixture(scope="module")
def repave_spec(self_update):
    return buildspec_of(self_update, "SelfUpdateProject")


def test_repave_build_phase_aborts_on_failure(repave_spec):
    """Without on-failure, CodeBuild runs post_build after a failed build phase
    (observed 2026-09-25: five images failed to build, post_build still updated
    apps-dev in both regions to a Tag with no images). ABORT stops the build
    before the rollout step."""
    assert repave_spec["phases"]["build"].get("on-failure") == "ABORT"


def test_repave_image_build_fails_fast(repave_spec):
    """The image loop must stop at the first failed build instead of tagging and
    pushing images that do not exist, then carrying on to the next service."""
    build_cmds = repave_spec["phases"]["build"]["commands"]
    image_loop = next(c for c in build_cmds if "docker build" in c)
    assert image_loop.lstrip().startswith("set -e"), "image-build block must start with set -e"


def test_repave_logs_in_to_ecr_public(repave_spec):
    """Authenticated ECR Public pulls get the higher rate limit; anonymous pulls
    from a shared CodeBuild egress IP do not."""
    assert ECR_PUBLIC_LOGIN in phase_commands(repave_spec, "pre_build")


def test_repave_role_can_authenticate_to_ecr_public(self_update):
    assert ECR_PUBLIC_ACTIONS <= iam_actions(self_update, "SelfUpdateRole")


# ---------------------------------------------------------------------------
# codebuild.yaml (deploy-time image builds)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def codebuild():
    return load_template(CODEBUILD_TEMPLATE)


@pytest.fixture(scope="module")
def docker_build_spec(codebuild):
    return buildspec_of(codebuild, "DockerBuildProject")


def test_deploy_build_logs_in_to_ecr_public(docker_build_spec):
    assert ECR_PUBLIC_LOGIN in phase_commands(docker_build_spec, "pre_build")


def test_deploy_build_role_can_authenticate_to_ecr_public(codebuild):
    assert ECR_PUBLIC_ACTIONS <= iam_actions(codebuild, "CodeBuildServiceRole")


def test_deploy_build_no_longer_rewrites_dockerfiles(docker_build_spec):
    """The registry now lives in the Dockerfiles themselves, so the sed rewrite
    that patched FROM lines at build time must be gone. Two sources of truth
    for the base registry is how the repave ended up pulling from Docker Hub
    while the e2e pulled from ECR Public."""
    assert "sed -i 's#FROM" not in phase_commands(docker_build_spec, "build")
