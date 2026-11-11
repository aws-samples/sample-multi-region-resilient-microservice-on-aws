"""Configuration that Spring Boot 4 reads differently and that fails without an error.

Moving cart, orders and ui to Spring Boot 4.0.9 changed three things that the compiler, the unit tests and the
default profiles do not exercise. Each one only shows when a particular profile is active, and each one fails quietly:

* ``spring.autoconfigure.exclude`` lists auto-configuration classes by name, and Boot 4 moved the RabbitMQ and MongoDB
  ones to new packages. Boot skips an excluded name it cannot load, so an old name turns the exclusion off. orders in
  the default profile then tried to reach RabbitMQ and answered ``/actuator/health`` with 503, and cart would have
  tried to reach MongoDB.
* The MongoDB connection properties moved from ``spring.data.mongodb.*`` to ``spring.mongodb.*``.
  ``spring-boot-properties-migrator`` reports ``spring.data.mongodb.uri`` but does not remap it, so cart's ``mongo``
  profile connected to ``localhost:27017`` instead of ``carts-db`` and every request waited 30 seconds for a server.
* ``chaos-monkey-spring-boot`` 4.0.0 declares the RestClient starter as ``provided``. Without it on cart's classpath
  the library's RestTemplate configuration fails with ``NoClassDefFoundError: RestTemplateCustomizer`` and cart does
  not start when the ``chaos-monkey`` profile is active.

Run with:  pytest tests/test_spring_boot_4_config.py -v
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
SOURCE_DIR = REPO_ROOT / "source"
CART_POM = SOURCE_DIR / "cart" / "pom.xml"
PROFILE_FILES = sorted(SOURCE_DIR.glob("*/src/main/resources/application*.yml"))

# Boot 3 package prefix -> where Boot 4 moved those auto-configurations.
MOVED_AUTO_CONFIGURATION_PACKAGES = {
    "org.springframework.boot.autoconfigure.amqp.": "org.springframework.boot.amqp.autoconfigure.",
    "org.springframework.boot.autoconfigure.mongo.": "org.springframework.boot.mongodb.autoconfigure.",
    "org.springframework.boot.autoconfigure.data.mongo.": "org.springframework.boot.data.mongodb.autoconfigure.",
}

# spring.data.mongodb.* connection settings and the keys Boot 4 reads instead (from the deprecation entries in
# spring-boot-mongodb 4.0.9). auto-index-creation, field-naming-strategy and gridfs.database stay under
# spring.data.mongodb, so they are not listed.
MOVED_MONGODB_KEYS = {
    "additional-hosts": "additional-hosts",
    "authentication-database": "authentication-database",
    "database": "database",
    "host": "host",
    "password": "password",
    "port": "port",
    "protocol": "protocol",
    "replica-set-name": "replica-set-name",
    "ssl.bundle": "ssl.bundle",
    "ssl.enabled": "ssl.enabled",
    "uri": "uri",
    "username": "username",
    "uuid-representation": "representation.uuid",
}


def _flatten(node, prefix=""):
    """Yield (dotted key, value) for every leaf, so nested and dotted spellings of a key compare equal."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _flatten(value, f"{prefix}.{key}" if prefix else str(key))
    else:
        yield prefix, node


def _properties(path: Path) -> dict:
    found = {}
    for document in yaml.safe_load_all(path.read_text()):
        found.update(_flatten(document or {}))
    return found


def _label(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def test_profile_files_exist():
    """Guard: the other tests are only meaningful while the services have profile files."""
    assert PROFILE_FILES, f"no source/*/src/main/resources/application*.yml under {SOURCE_DIR}"


@pytest.mark.parametrize("path", PROFILE_FILES, ids=_label)
def test_excluded_auto_configurations_use_the_spring_boot_4_packages(path):
    excluded = _properties(path).get("spring.autoconfigure.exclude") or []
    if isinstance(excluded, str):
        excluded = [name.strip() for name in excluded.split(",")]
    stale = {
        name: prefix_new + name[len(prefix_old):]
        for name in excluded
        for prefix_old, prefix_new in MOVED_AUTO_CONFIGURATION_PACKAGES.items()
        if name.startswith(prefix_old)
    }
    assert not stale, (
        f"{_label(path)} excludes auto-configurations by their Spring Boot 3 names, which Boot 4 ignores "
        f"without an error, so the exclusion has no effect. Use: {stale}"
    )


@pytest.mark.parametrize("path", PROFILE_FILES, ids=_label)
def test_mongodb_connection_properties_use_the_spring_boot_4_prefix(path):
    properties = _properties(path)
    stale = {
        f"spring.data.mongodb.{old}": f"spring.mongodb.{new}"
        for old, new in MOVED_MONGODB_KEYS.items()
        if f"spring.data.mongodb.{old}" in properties
    }
    assert not stale, (
        f"{_label(path)} sets MongoDB connection properties under spring.data.mongodb, which Boot 4 ignores "
        f"(the properties migrator reports them but does not remap them), so the service connects to "
        f"localhost:27017. Rename: {stale}"
    )


def test_cart_mongo_profile_sets_the_connection_uri():
    """Guard for the test above: the rename must leave the profile with a URI."""
    properties = _properties(SOURCE_DIR / "cart" / "src" / "main" / "resources" / "application-mongo.yml")
    assert properties.get("spring.mongodb.uri"), "application-mongo.yml no longer sets spring.mongodb.uri"


def _dependencies(pom: Path) -> dict:
    """Return {artifactId: version or None} for the pom's own <dependencies>."""
    root = ET.parse(pom).getroot()
    found = {}
    for dependency in root.findall("{*}dependencies/{*}dependency"):
        artifact = dependency.find("{*}artifactId")
        version = dependency.find("{*}version")
        if artifact is not None and artifact.text:
            found[artifact.text.strip()] = version.text.strip() if version is not None and version.text else None
    return found


def test_cart_supplies_the_starter_that_chaos_monkey_4_expects():
    dependencies = _dependencies(CART_POM)
    version = dependencies.get("chaos-monkey-spring-boot")
    assert version, "cart no longer depends on chaos-monkey-spring-boot; drop this test if that is intended"
    if not re.match(r"^[4-9]\.", version):
        pytest.skip(f"chaos-monkey-spring-boot {version} predates the Spring Boot 4 starters")
    assert "spring-boot-starter-restclient" in dependencies, (
        "source/cart/pom.xml depends on chaos-monkey-spring-boot 4.x but not on spring-boot-starter-restclient, "
        "which the library declares as provided. Without it the library fails with NoClassDefFoundError for "
        "RestTemplateCustomizer and cart does not start when the chaos-monkey profile is active"
    )
