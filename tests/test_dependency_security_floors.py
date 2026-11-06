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
  property, as it does for pgjdbc, netty, Tomcat and Jackson.

These tests keep the fixed versions from slipping back, for instance through a
lockfile regenerated from an old cache or a property removed in a tidy-up. They
check versions in the files; trivy itself is the check that the advisories are
gone.

If Spring Boot's BOM later manages a RabbitMQ client at or above the floor, drop
the property and the first test together.

On 2026-11-06 Inspector reported more advisories in the Java services that run in
test2, in the same dependencies and in log4j-core. Spring Boot 3.5.16 is the last
open-source 3.5 release, so none of them will reach the BOM, and the floors move with
the properties:

* netty (orders, ui): CVE-2026-75595 (CRITICAL), CVE-2026-75596 and CVE-2026-59903 are
  fixed in 4.2.17. The poms pin 4.2.19.Final, the netty that Spring Boot 4.0.9 manages.
* log4j-core (cart, orders, ui): CVE-2026-34478, CVE-2026-34479 and CVE-2026-34480 are
  fixed in 2.25.4. The BOM's 2.24.3 is below that, so each pom overrides ``log4j2.version``.
* RabbitMQ Java client (orders): CVE-2026-106121 (through 5.36.0) and CVE-2026-106122
  (through 5.35.0) are fixed in 5.37.0, which replaces the 5.34.0 floor.

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

RABBIT_CLIENT_FLOOR = (5, 37, 0)
NETTY_FLOOR = (4, 2, 17)
LOG4J2_FLOOR = (2, 25, 4)
PROXY_ADDR_FLOOR = (2, 0, 8)

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


def test_orders_overrides_the_rabbitmq_client_to_a_release_that_fixes_the_advisories():
    properties = ET.parse(ORDERS_POM).getroot().find("{*}properties")
    assert properties is not None, "orders/pom.xml has no <properties>"
    node = properties.find("{*}rabbit-amqp-client.version")
    assert node is not None and node.text, (
        f"orders/pom.xml does not override rabbit-amqp-client.version, so Spring Boot's BOM decides the "
        f"RabbitMQ client version, and 3.5.16's is 5.25.0, below {_dotted(RABBIT_CLIENT_FLOOR)}"
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
def test_netty_is_overridden_to_a_release_that_fixes_cve_2026_75595(service):
    value = _pom_property(POMS[service], "netty.version")
    assert value, (
        f"source/{service}/pom.xml does not override netty.version, so Spring Boot 3.5.16's BOM decides "
        f"the netty version, and its 4.1.135.Final is below the 4.1 fix and carries every 4.2 advisory"
    )
    # netty versions end in .Final
    assert _version(re.sub(r"\.Final$", "", value)) >= NETTY_FLOOR, (
        f"source/{service}/pom.xml sets netty.version {value}, below {_dotted(NETTY_FLOOR)}.Final, "
        f"which CVE-2026-75595 (CRITICAL), CVE-2026-75596 and CVE-2026-59903 need"
    )


@pytest.mark.parametrize("service", sorted(POMS))
def test_log4j_core_is_overridden_to_a_release_that_fixes_cve_2026_34478(service):
    value = _pom_property(POMS[service], "log4j2.version")
    assert value, (
        f"source/{service}/pom.xml does not override log4j2.version, so Spring Boot 3.5.16's BOM decides "
        f"the log4j-core version, and its 2.24.3 is below {_dotted(LOG4J2_FLOOR)}"
    )
    assert _version(value) >= LOG4J2_FLOOR, (
        f"source/{service}/pom.xml sets log4j2.version {value}, below {_dotted(LOG4J2_FLOOR)}, "
        f"which CVE-2026-34478, CVE-2026-34479 and CVE-2026-34480 need"
    )
