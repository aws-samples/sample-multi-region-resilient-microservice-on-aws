"""Tests for the Maven poms of the Java services.

On 2026-10-01 Dependabot moved ui's Maven wrapper from 3.9.16 to 3.10.0
(#180). Maven 3.10 rejects a pom that declares the same dependency twice,
which 3.9 only warned about, and ui's pom declared
``spring-boot-starter-validation`` twice (once with ``runtime`` scope, once
with the default ``compile`` scope). Its image build then stopped at
``./mvnw dependency:go-offline`` with "'dependencies.dependency.(groupId:
artifactId:type:classifier)' must be unique".

ui needs the compile-scoped declaration, because its controllers and payloads
import ``jakarta.validation``. This test keeps every pom free of duplicate
declarations, so the next wrapper bump on cart or orders can't break the same way.

Run with:  pytest tests/test_maven_poms.py -v
"""

import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
POMS = sorted((REPO_ROOT / "source").glob("*/pom.xml"))


def _text(node, tag: str, default: str = "") -> str:
    child = node.find(f"{{*}}{tag}")
    return child.text.strip() if child is not None and child.text else default


def test_there_are_poms_to_check():
    assert POMS


@pytest.mark.parametrize("pom", POMS, ids=lambda p: p.parent.name)
def test_no_dependency_is_declared_twice(pom):
    root = ET.parse(pom).getroot()
    for section in ("{*}dependencies", "{*}dependencyManagement/{*}dependencies"):
        deps = root.findall(f"{section}/{{*}}dependency")
        keys = Counter(
            (_text(d, "groupId"), _text(d, "artifactId"), _text(d, "type", "jar"), _text(d, "classifier"))
            for d in deps
        )
        duplicates = sorted(":".join(k for k in key if k) for key, n in keys.items() if n > 1)
        assert not duplicates, (
            f"{pom.relative_to(REPO_ROOT)} declares these dependencies more than once, "
            f"which Maven 3.10 rejects: {duplicates}"
        )
