"""The ADOT auto-instrumentation sidecars: one version in two places, and a floor.

The task definitions in ``deployment/ecs.yaml`` pull ``adot-autoinstrumentation-java`` and
``adot-autoinstrumentation-node`` from the account's own ECR repositories, at a pinned tag.
``deployment/mirror-sidecar-buildspec.yml`` copies the public images there at the tag it names, for the
first deployment and again for every weekly repave (self-update.yaml reads the list from the buildspec).
If the two disagree, ECS asks for a tag that was never mirrored and the tasks cannot start.

On 2026-11-06 the pinned tags were Java v2.29.0 and Node v0.12.0, and Inspector reported 15 and 9 findings
on them in each Region. Read from the public images that day:

* Java v2.29.0 bundles netty 4.1.135.Final and jackson 2.22.0. v2.32.0 bundles netty 4.1.137.Final, which
  fixes CVE-2026-75595 (CRITICAL, fixed in netty 4.1.137 and 4.2.17), and jackson 2.22.3.
* Node v0.13.0 moves grpc-js, protobufjs, brace-expansion and systeminformation forward. It does not clear
  every finding: a second copy of propagator-jaeger 2.8.0 and systeminformation 5.33.11 are still flagged.

Both images keep the layout the init containers rely on (``/javaagent.jar``, ``/autoinstrumentation``,
``/bin/cp``).

Run with:  pytest tests/test_adot_sidecar_images.py -v
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
ECS_TEMPLATE = REPO_ROOT / "deployment" / "ecs.yaml"
BUILDSPEC = REPO_ROOT / "deployment" / "mirror-sidecar-buildspec.yml"

FLOORS = {"java": (2, 32, 0), "node": (0, 13, 0)}

ECS_IMAGE_RE = re.compile(r"adot-autoinstrumentation-(java|node)\$\{Env\}:v(\d+\.\d+\.\d+)\b")
BUILDSPEC_IMAGE_RE = re.compile(r"public\.ecr\.aws/aws-observability/adot-autoinstrumentation-(java|node):v(\d+\.\d+\.\d+)\b")


def _tags(pattern, path: Path) -> dict:
    found = {}
    for flavour, version in pattern.findall(path.read_text()):
        found.setdefault(flavour, set()).add(version)
    return found


def _version(text: str) -> tuple:
    return tuple(int(part) for part in text.split("."))


def test_ecs_uses_one_tag_per_sidecar_flavour():
    tags = _tags(ECS_IMAGE_RE, ECS_TEMPLATE)
    assert set(tags) == {"java", "node"}, f"expected both ADOT sidecars in ecs.yaml, found {sorted(tags)}"
    mixed = {flavour: sorted(versions) for flavour, versions in tags.items() if len(versions) > 1}
    assert not mixed, f"ecs.yaml pins more than one tag for the same sidecar: {mixed}"


@pytest.mark.parametrize("flavour", sorted(FLOORS))
def test_the_buildspec_mirrors_the_tag_ecs_asks_for(flavour):
    in_ecs = _tags(ECS_IMAGE_RE, ECS_TEMPLATE)[flavour]
    in_buildspec = _tags(BUILDSPEC_IMAGE_RE, BUILDSPEC).get(flavour, set())
    assert in_buildspec == in_ecs, (
        f"adot-autoinstrumentation-{flavour}: ecs.yaml asks for {sorted(in_ecs)} but "
        f"mirror-sidecar-buildspec.yml mirrors {sorted(in_buildspec)}"
    )


@pytest.mark.parametrize("flavour", sorted(FLOORS))
def test_sidecar_is_at_or_above_the_floor(flavour):
    (version,) = _tags(ECS_IMAGE_RE, ECS_TEMPLATE)[flavour]
    floor = ".".join(str(part) for part in FLOORS[flavour])
    assert _version(version) >= FLOORS[flavour], (
        f"adot-autoinstrumentation-{flavour} is v{version}, below v{floor}, whose bundled libraries "
        "carry the fixes for the netty (CRITICAL), jackson and Node findings Inspector reported on 2026-11-06"
    )
