"""Floors for the two dependencies the trivy gate flagged on 2026-10-06.

pr-validation runs ``trivy fs source --severity HIGH,CRITICAL --ignore-unfixed``.
On 2026-10-06 it failed PR #178 on advisories that no commit of the PR had
caused: they were published after the pom and the lockfile were last written, so
every pull request into main would have failed the same way.

* ``source/checkout/package-lock.json`` locked ``proxy-addr`` 2.0.7, which
  CVE-2026-90711 (CRITICAL) fixes in 2.0.8. proxy-addr is express's dependency
  (``^2.0.7``), so the fix is a lockfile re-resolution, not a change to
  ``package.json``.
* ``source/orders/pom.xml`` resolved the RabbitMQ Java client 5.25.0 through
  ``spring-boot-starter-amqp``, from Spring Boot 3.5.16's BOM. Four HIGH findings
  need 5.34.0: CVE-2026-63337 (fixed in 5.33.0), CVE-2026-69219 and
  CVE-2026-69220 (5.33.1) and CVE-2026-75516 (5.34.0). The BOM manages the client
  through the ``rabbit-amqp-client.version`` property, so the pom overrides that
  property, as it then did for pgjdbc, netty, Tomcat and Jackson.

These tests keep the fixed versions from slipping back, for instance through a
lockfile regenerated from an old cache or a property removed in a tidy-up. They
check versions in the files; trivy itself is the check that the advisories are
gone.

If Spring Boot's BOM later manages a RabbitMQ client at or above the floor, drop
the property and the first test together.

On 2026-11-06 Inspector reported more advisories in the Java services that run in
test2, in the same dependencies and in log4j-core. Spring Boot 3.5.16 is the last
open-source 3.5 release, so none of the fixes would reach its BOM, and the services
moved to Spring Boot 4.0.9 (see tests/test_spring_boot_parent.py). The floors are the
releases that fix them:

* netty (orders, ui): CVE-2026-75595 (CRITICAL), CVE-2026-75596 and CVE-2026-59903 are
  fixed in 4.2.17.
* log4j-core (cart, orders, ui): CVE-2026-34478, CVE-2026-34479 and CVE-2026-34480 are
  fixed in 2.25.4.
* Tomcat (cart, orders): CVE-2026-65182 (security constraint bypass), CVE-2026-65905
  (DIGEST replay) and CVE-2026-68525 (FORM authentication method-constraint bypass) are
  fixed in 11.0.25.
* PostgreSQL JDBC driver (orders): CVE-2026-54291 (SCRAM-SHA-256-PLUS downgrade) is fixed
  in 42.7.12.
* Jackson (cart, orders, ui): CVE-2026-68497 (unbounded Duration and XMLGregorianCalendar
  parsing) is fixed in 3.1.6 and in 2.21.6. All three services carry both lines.
* RabbitMQ Java client (orders): CVE-2026-106121 (through 5.36.0) and CVE-2026-106122
  (through 5.35.0) are fixed in 5.37.0, which replaces the 5.34.0 floor.
* golang.org/x/net (catalog): CVE-2026-78661 is fixed in 0.62.0. catalog does not import
  x/net itself; go.mod carries it as an indirect requirement, so ``go get`` is what moves it.

Spring Boot 4.0.9's BOM manages netty, log4j-core, Tomcat, the PostgreSQL driver and both
Jackson lines at or above those floors, so the three poms no longer set the properties
and the tests below check the version the build resolves. ``BOM_MANAGED`` records what
the parent's BOM supplies; a pom that sets a property itself is checked against its own
value. The RabbitMQ client is the exception: the 4.0.9 BOM manages 5.27.1, so orders
keeps its override.

Run with:  pytest tests/test_dependency_security_floors.py -v
"""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
ORDERS_POM = REPO_ROOT / "source" / "orders" / "pom.xml"
CHECKOUT_LOCK = REPO_ROOT / "source" / "checkout" / "package-lock.json"
POMS = {name: REPO_ROOT / "source" / name / "pom.xml" for name in ("cart", "orders", "ui")}
CATALOG_GO_MOD = REPO_ROOT / "source" / "catalog" / "go.mod"

RABBIT_CLIENT_FLOOR = (5, 37, 0)
NETTY_FLOOR = (4, 2, 17)
LOG4J2_FLOOR = (2, 25, 4)
TOMCAT_FLOOR = (11, 0, 25)
POSTGRESQL_FLOOR = (42, 7, 12)
JACKSON3_FLOOR = (3, 1, 6)
JACKSON2_FLOOR = (2, 21, 6)
X_NET_FLOOR = (0, 62, 0)
PROXY_ADDR_FLOOR = (2, 0, 8)

# What Spring Boot's BOM manages, by the parent release it ships in. A pom that does not
# set one of these properties gets the BOM's value. A later patch release of the same minor
# line does not lower them, so a parent at or above an entry inherits at least that entry's
# versions. A parent in another minor line has no entry until someone reads its BOM and adds
# one; until then its poms have to set the properties themselves. Boot 4.0.9 names Jackson
# 3's BOM ``jackson-bom`` and Jackson 2's ``jackson-2-bom``.
BOM_MANAGED = {
    (4, 0, 9): {
        "netty.version": "4.2.19.Final",
        "log4j2.version": "2.25.5",
        "tomcat.version": "11.0.27",
        "postgresql.version": "42.7.14",
        "jackson-bom.version": "3.1.7",
        "jackson-2-bom.version": "2.21.7",
    },
}

VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _version(text: str) -> tuple:
    match = VERSION_RE.match(text.strip())
    assert match, f"not a plain MAJOR.MINOR.PATCH version: {text!r}"
    return tuple(int(part) for part in match.groups())


def _pom_property(pom: Path, name: str):
    properties = ET.parse(pom).getroot().find("{*}properties")
    node = properties.find(f"{{*}}{name}") if properties is not None else None
    return node.text.strip() if node is not None and node.text else None


def _dotted(version: tuple) -> str:
    return ".".join(str(part) for part in version)


def _parent_version(pom: Path) -> tuple:
    node = ET.parse(pom).getroot().find("{*}parent/{*}version")
    assert node is not None and node.text, f"{pom.relative_to(REPO_ROOT)} declares no <parent><version>"
    return _version(node.text)


def _resolved(pom: Path, name: str):
    """Return (version, source) the build uses for the BOM property ``name``, or (None, None)."""
    explicit = _pom_property(pom, name)
    if explicit:
        return explicit, f"the pom's own <{name}>"
    parent = _parent_version(pom)
    known = [release for release in BOM_MANAGED if release[:2] == parent[:2] and release <= parent]
    if not known:
        return None, None
    release = max(known)
    return BOM_MANAGED[release][name], f"Spring Boot {_dotted(release)}'s BOM"


def _assert_resolves_at_or_above(service: str, name: str, floor: tuple, advisories: str):
    pom = POMS[service]
    value, source = _resolved(pom, name)
    assert value, (
        f"source/{service}/pom.xml sets no {name}, and BOM_MANAGED has no entry for its Spring Boot parent "
        f"{_dotted(_parent_version(pom))}: set {name} in the pom, or read that parent's BOM and record "
        f"the versions it manages"
    )
    # netty versions end in .Final
    assert _version(re.sub(r"\.Final$", "", value)) >= floor, (
        f"source/{service}/pom.xml resolves {name} to {value} ({source}), below {_dotted(floor)}, "
        f"which {advisories} need"
    )


def test_orders_overrides_the_rabbitmq_client_to_a_release_that_fixes_the_advisories():
    properties = ET.parse(ORDERS_POM).getroot().find("{*}properties")
    assert properties is not None, "orders/pom.xml has no <properties>"
    node = properties.find("{*}rabbit-amqp-client.version")
    assert node is not None and node.text, (
        f"orders/pom.xml does not override rabbit-amqp-client.version, so Spring Boot's BOM decides the "
        f"RabbitMQ client version, and 4.0.9's is 5.27.1, below {_dotted(RABBIT_CLIENT_FLOOR)}"
    )
    assert _version(node.text) >= RABBIT_CLIENT_FLOOR, (
        f"rabbit-amqp-client.version is {node.text.strip()}, below {_dotted(RABBIT_CLIENT_FLOOR)}, which "
        f"CVE-2026-63337, CVE-2026-69219, CVE-2026-69220, CVE-2026-75516, CVE-2026-106121 and "
        f"CVE-2026-106122 need"
    )


def test_checkout_locks_proxy_addr_at_a_release_that_fixes_cve_2026_90711():
    packages = json.loads(CHECKOUT_LOCK.read_text())["packages"]
    copies = {path: entry["version"] for path, entry in packages.items()
              if path == "node_modules/proxy-addr" or path.endswith("/node_modules/proxy-addr")}
    assert copies, "checkout's lockfile no longer contains proxy-addr; drop this test if express stopped needing it"
    too_old = {path: version for path, version in copies.items() if _version(version) < PROXY_ADDR_FLOOR}
    assert not too_old, f"proxy-addr below {_dotted(PROXY_ADDR_FLOOR)} (CVE-2026-90711, CRITICAL): {too_old}"


@pytest.mark.parametrize("service", ("orders", "ui"))
def test_netty_resolves_to_a_release_that_fixes_cve_2026_75595(service):
    _assert_resolves_at_or_above(
        service, "netty.version", NETTY_FLOOR,
        "CVE-2026-75595 (CRITICAL), CVE-2026-75596 and CVE-2026-59903",
    )


@pytest.mark.parametrize("service", sorted(POMS))
def test_log4j_core_resolves_to_a_release_that_fixes_cve_2026_34478(service):
    _assert_resolves_at_or_above(
        service, "log4j2.version", LOG4J2_FLOOR,
        "CVE-2026-34478, CVE-2026-34479 and CVE-2026-34480",
    )


@pytest.mark.parametrize("service", ("cart", "orders"))
def test_tomcat_resolves_to_a_release_that_fixes_cve_2026_65182(service):
    _assert_resolves_at_or_above(
        service, "tomcat.version", TOMCAT_FLOOR,
        "CVE-2026-65182, CVE-2026-65905 and CVE-2026-68525",
    )


def test_orders_postgresql_driver_resolves_to_a_release_that_fixes_cve_2026_54291():
    _assert_resolves_at_or_above("orders", "postgresql.version", POSTGRESQL_FLOOR, "CVE-2026-54291")


@pytest.mark.parametrize("service", sorted(POMS))
def test_jackson_3_resolves_to_a_release_that_fixes_cve_2026_68497(service):
    _assert_resolves_at_or_above(service, "jackson-bom.version", JACKSON3_FLOOR, "CVE-2026-68497")


@pytest.mark.parametrize("service", sorted(POMS))
def test_jackson_2_resolves_to_a_release_that_fixes_cve_2026_68497(service):
    _assert_resolves_at_or_above(service, "jackson-2-bom.version", JACKSON2_FLOOR, "CVE-2026-68497")


def test_catalog_requires_x_net_at_a_release_that_fixes_cve_2026_78661():
    match = re.search(r"^\s*golang\.org/x/net\s+v(\d+\.\d+\.\d+)\b", CATALOG_GO_MOD.read_text(), re.MULTILINE)
    assert match, "catalog's go.mod no longer requires golang.org/x/net; drop this test if no dependency needs it"
    assert _version(match.group(1)) >= X_NET_FLOOR, (
        f"golang.org/x/net v{match.group(1)} in source/catalog/go.mod, below v{_dotted(X_NET_FLOOR)}, "
        f"which CVE-2026-78661 (HIGH) needs"
    )
