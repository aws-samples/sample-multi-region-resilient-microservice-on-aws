"""Tests for the base images the service Dockerfiles build on.

On 2026-09-30 Dependabot moved ``cart``, ``orders`` and ``ui`` from
``amazoncorretto:17-al2023`` to ``amazoncorretto:27-al2023`` (#165, #166,
#168). Lombok, which all three use, can't run inside javac 27: every image
build failed with ``java.lang.ExceptionInInitializerError:
com.sun.tools.javac.tree.EndPosTable``, so every e2e run stopped at "Build
images". The same bumps had been reverted in July (#21, #22, #24).

They came back because the Dependabot ignore rule named ``amazoncorretto``,
while Dependabot names an image by its path without the registry host:
``public.ecr.aws/docker/library/amazoncorretto`` is
``docker/library/amazoncorretto``. The ``node`` and ``golang`` rules matched
nothing for the same reason (#167 moved catalog's builder to ``golang:1.27``).

These tests pin both facts: the Java services build and run on the Corretto
major that matches their ``<java.version>``, and every Dependabot ignore rule
for a docker directory names an image that a Dockerfile actually uses.

Run with:  pytest tests/test_base_images.py -v
"""

import fnmatch
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
SOURCE_DIR = REPO_ROOT / "source"
DEPENDABOT_CONFIG = REPO_ROOT / ".github" / "dependabot.yml"

JAVA_SERVICES = ("cart", "orders", "ui")
CORRETTO = "docker/library/amazoncorretto"

FROM_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", re.IGNORECASE | re.MULTILINE)


def _images(dockerfile: Path) -> list:
    return FROM_RE.findall(dockerfile.read_text())


def _dependabot_name(image: str) -> str:
    """The name Dependabot gives an image: its path without registry host or tag."""
    ref = image.split("@", 1)[0]
    if ":" in ref.rsplit("/", 1)[-1]:
        ref = ref.rsplit(":", 1)[0]
    parts = ref.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]
    return "/".join(parts)


def _java_version(pom: Path) -> str:
    node = ET.parse(pom).getroot().find("{*}properties/{*}java.version")
    assert node is not None and node.text, f"{pom.relative_to(REPO_ROOT)} sets no <java.version>"
    return node.text.strip()


def _docker_updates() -> list:
    config = yaml.safe_load(DEPENDABOT_CONFIG.read_text())
    return [u for u in config["updates"] if u["package-ecosystem"] == "docker"]


def _images_in(directory: str) -> set:
    return {_dependabot_name(i) for i in _images(REPO_ROOT / directory.lstrip("/") / "Dockerfile")}


def test_dependabot_name_strips_registry_and_tag():
    assert _dependabot_name("public.ecr.aws/docker/library/amazoncorretto:17-al2023") == CORRETTO
    assert _dependabot_name("gcr.io/distroless/static:nonroot") == "distroless/static"
    assert _dependabot_name("amazoncorretto:17") == "amazoncorretto"


@pytest.mark.parametrize("service", JAVA_SERVICES)
def test_java_service_uses_corretto_matching_its_java_version(service):
    java_version = _java_version(SOURCE_DIR / service / "pom.xml")
    images = [i for i in _images(SOURCE_DIR / service / "Dockerfile") if _dependabot_name(i) == CORRETTO]
    assert images, f"source/{service}/Dockerfile has no Corretto stage"
    for image in images:
        major = image.rsplit(":", 1)[1].split("-", 1)[0]
        assert major == java_version, (
            f"source/{service}/Dockerfile uses {image}, but the service targets Java "
            f"{java_version}; change <java.version> and the images together, and check "
            "Lombok supports the new javac (JDK 27 fails with EndPosTable)"
        )


def test_every_docker_ignore_rule_names_an_image_in_use():
    used = set()
    for update in _docker_updates():
        used |= _images_in(update["directory"])
    dead = sorted({
        rule["dependency-name"]
        for update in _docker_updates()
        for rule in update.get("ignore", [])
        if not any(fnmatch.fnmatchcase(name, rule["dependency-name"]) for name in used)
    })
    assert not dead, f"Dependabot ignore rules that match no image in any Dockerfile: {dead}"


@pytest.mark.parametrize("service", JAVA_SERVICES)
def test_dependabot_blocks_corretto_major_bumps(service):
    update = next(u for u in _docker_updates() if u["directory"] == f"/source/{service}")
    rules = [r for r in update.get("ignore", []) if fnmatch.fnmatchcase(CORRETTO, r["dependency-name"])]
    # A rule without update-types ignores every update, majors included.
    assert any(
        "version-update:semver-major" in r.get("update-types", ["version-update:semver-major"])
        for r in rules
    ), f"Dependabot can still propose Corretto major bumps for /source/{service}"
