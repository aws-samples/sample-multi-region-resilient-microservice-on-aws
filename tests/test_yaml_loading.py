"""Guard against unsafe PyYAML loading anywhere in the repository.

AWS AppSec's ACAT scan (bandit rule B506) opened a finding on 2026-09-29
against ``tests/test_build_sources.py``: it called ``yaml.load(text,
Loader=_CfnLoader)`` with a ``yaml.SafeLoader`` subclass that tolerates
CloudFormation tags. The load was safe, but B506 accepts only the literal
``SafeLoader`` / ``CSafeLoader`` names as the ``Loader`` argument, so any
``yaml.load`` call with a custom loader is reported, and the pattern had already
been copied into a second test file.

Both call sites now drive the loader directly (``_CfnLoader(text)`` followed by
``get_single_data()``), which is what ``yaml.load`` does internally. This test
keeps the repository free of the call shapes the scanner reports, so the finding
does not come back with the next copy of the pattern.

Run with:  pytest tests/test_yaml_loading.py -v
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# yaml.load with any Loader argument, plus the two aliases that resolve to the
# unrestricted loader. yaml.safe_load and yaml.safe_load_all are fine.
UNSAFE_YAML_CALL = re.compile(r"\byaml\s*\.\s*(load|load_all|full_load|full_load_all|unsafe_load|unsafe_load_all)\s*\(")

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "cdk.out", "build", "dist"}


def _python_files():
    for path in sorted(REPO_ROOT.rglob("*.py")):
        if SKIP_DIRS.intersection(path.relative_to(REPO_ROOT).parts):
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        yield path


def test_repository_has_python_files():
    """Guard: the scan below is only meaningful while there is code to scan."""
    assert any(_python_files())


def test_no_python_file_calls_yaml_load():
    offenders = []
    for path in _python_files():
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if UNSAFE_YAML_CALL.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
    assert not offenders, (
        "unsafe PyYAML load found; use yaml.safe_load, or drive a "
        "yaml.SafeLoader subclass directly:\n  " + "\n  ".join(offenders)
    )
