"""Tests for the Spring Boot parent the Maven services build on.

``cart``, ``orders`` and ``ui`` inherit their dependency versions from
``org.springframework.boot:spring-boot-starter-parent``. Spring Boot patch
releases are where the Spring Framework, Micrometer and Tomcat security fixes
arrive, so the parent version decides what ``trivy fs source`` reports for the
three pom files.

On 2026-09-28 the trivy gate flagged five HIGH findings against the poms
(Spring Framework 6.2.18: CVE-2026-41850, CVE-2026-41842, CVE-2026-41845;
Micrometer 1.15.11: CVE-2026-40983, CVE-2026-40984). Spring Boot 3.5.15 had
carried the fixed versions since 2026-06-10, but the parent was pinned at
3.5.14 because ``.github/dependabot.yml`` ignored every update to it, patch
releases included, after the 4.x bumps broke the services in July.

On 2026-11-06 Inspector reported CRITICAL findings in Spring Framework 6.2.19
(CVE-2026-47884, CVE-2026-47890, CVE-2026-47892) for the services running in
test2. Spring Boot 3.5.16 is the last open-source 3.5 release and manages that
Spring Framework; the open-source fix is 7.0.9. The services therefore build on
the 4.0 line. 4.0.9 is the first 4.0 release whose BOM also carries the Tomcat
and Jackson fixes the poms used to override by hand (4.0.8 manages Spring
Framework 7.0.9 but Tomcat 11.0.24 and Jackson 3.1.5). 4.1.0 manages Spring
Framework 7.0.8, which the advisories still cover, so the parent stays below it.

These tests pin the facts that let the earlier gap happen: the Maven services
share one parent version at or above the release carrying those fixes, and
Dependabot may propose patch releases of the parent (major and minor bumps
stay blocked and need a deliberate migration). They also keep the parent in
the 4.0 line until a 4.1 release carries the Spring Framework fix.

Run with:  pytest tests/test_spring_boot_parent.py -v
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
SOURCE_DIR = REPO_ROOT / "source"
DEPENDABOT_CONFIG = REPO_ROOT / ".github" / "dependabot.yml"

PARENT_GROUP = "org.springframework.boot"
PARENT_ARTIFACT = "spring-boot-starter-parent"
PARENT_DEPENDENCY = f"{PARENT_GROUP}:{PARENT_ARTIFACT}"

# Spring Boot 4.0.9 manages Spring Framework 7.0.10 (CVE-2026-47884, CVE-2026-47890 and
# CVE-2026-47892 are fixed in 7.0.9), Micrometer 1.16.8 (CVE-2026-40983 and CVE-2026-40984
# reach 1.16.5), Tomcat 11.0.27 and Jackson 3.1.7 / 2.21.7. The Tomcat and Jackson floors
# live in tests/test_dependency_security_floors.py.
MINIMUM_PARENT_VERSION = (4, 0, 9)

# Spring Boot 4.1.0 manages Spring Framework 7.0.8, which the same advisories still cover.
PARENT_VERSION_CEILING = (4, 1, 0)

VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _pom_paths() -> list[Path]:
    return sorted(SOURCE_DIR.glob("*/pom.xml"))


def _parent(pom: Path) -> tuple[str, str, str]:
    """Return (groupId, artifactId, version) of the pom's parent."""
    root = ET.parse(pom).getroot()
    parent = root.find("{*}parent")
    assert parent is not None, f"{pom.relative_to(REPO_ROOT)} declares no <parent>"
    fields = []
    for tag in ("groupId", "artifactId", "version"):
        node = parent.find(f"{{*}}{tag}")
        assert node is not None and node.text, (
            f"{pom.relative_to(REPO_ROOT)}: <parent> has no <{tag}>"
        )
        fields.append(node.text.strip())
    return tuple(fields)


def _version_tuple(version: str) -> tuple[int, int, int]:
    match = VERSION_RE.match(version)
    assert match, f"parent version {version!r} is not MAJOR.MINOR.PATCH"
    return tuple(int(part) for part in match.groups())


def _dotted(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def _maven_updates() -> list[dict]:
    config = yaml.safe_load(DEPENDABOT_CONFIG.read_text())
    return [
        update
        for update in config["updates"]
        if update.get("package-ecosystem") == "maven"
    ]


def test_source_tree_has_maven_services():
    """Guard: the other tests are only meaningful while Maven services exist."""
    assert _pom_paths(), f"no source/*/pom.xml under {SOURCE_DIR}"


def test_maven_services_share_one_spring_boot_parent_version():
    """A partial bump leaves one service on the vulnerable parent."""
    parents = {pom.parent.name: _parent(pom) for pom in _pom_paths()}
    for service, (group, artifact, _version) in parents.items():
        assert (group, artifact) == (PARENT_GROUP, PARENT_ARTIFACT), (
            f"{service}: parent is {group}:{artifact}, expected {PARENT_DEPENDENCY}"
        )
    versions = {version for _group, _artifact, version in parents.values()}
    assert len(versions) == 1, (
        "Maven services pin different spring-boot-starter-parent versions: "
        + ", ".join(f"{s}={v[2]}" for s, v in sorted(parents.items()))
    )


@pytest.mark.parametrize("pom", _pom_paths(), ids=lambda p: p.parent.name)
def test_spring_boot_parent_carries_the_framework_and_micrometer_fixes(pom):
    _group, _artifact, version = _parent(pom)
    assert _version_tuple(version) >= MINIMUM_PARENT_VERSION, (
        f"{pom.relative_to(REPO_ROOT)} builds on Spring Boot {version}; "
        f"{_dotted(MINIMUM_PARENT_VERSION)} or later is needed for Spring Framework 7.0.9 or later "
        "(CVE-2026-47884, CVE-2026-47890, CVE-2026-47892), Micrometer 1.16.6 or later "
        "(CVE-2026-40983, CVE-2026-40984), and the Tomcat and Jackson releases that "
        "tests/test_dependency_security_floors.py checks"
    )


@pytest.mark.parametrize("pom", _pom_paths(), ids=lambda p: p.parent.name)
def test_spring_boot_parent_stays_in_the_4_0_line(pom):
    """4.1.0 manages Spring Framework 7.0.8, so it brings the advisories back."""
    _group, _artifact, version = _parent(pom)
    assert _version_tuple(version) < PARENT_VERSION_CEILING, (
        f"{pom.relative_to(REPO_ROOT)} builds on Spring Boot {version}. The 4.1.0 BOM manages Spring "
        "Framework 7.0.8, which CVE-2026-47884, CVE-2026-47890 and CVE-2026-47892 still cover. Moving "
        "to 4.1 means reading its BOM first: raise PARENT_VERSION_CEILING here and add the release to "
        "BOM_MANAGED in tests/test_dependency_security_floors.py once the BOM carries the fixes"
    )


def test_every_maven_service_has_a_dependabot_entry():
    directories = {update["directory"] for update in _maven_updates()}
    for pom in _pom_paths():
        directory = "/" + pom.parent.relative_to(REPO_ROOT).as_posix()
        assert directory in directories, (
            f"{directory} has no maven entry in {DEPENDABOT_CONFIG.relative_to(REPO_ROOT)}"
        )


@pytest.mark.parametrize(
    "update", _maven_updates(), ids=lambda u: u["directory"].rsplit("/", 1)[-1]
)
def test_dependabot_lets_spring_boot_parent_patch_releases_through(update):
    """An ignore rule with no update-types blocks every version of the parent."""
    rules = [
        rule
        for rule in update.get("ignore", [])
        if rule.get("dependency-name") == PARENT_DEPENDENCY
    ]
    for rule in rules:
        update_types = rule.get("update-types")
        assert update_types, (
            f"{update['directory']}: the ignore rule for {PARENT_DEPENDENCY} has no "
            "update-types, so it blocks patch releases too"
        )
        assert "version-update:semver-patch" not in update_types, (
            f"{update['directory']}: the ignore rule for {PARENT_DEPENDENCY} blocks "
            "patch releases"
        )
