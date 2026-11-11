"""Floor for the CloudWatch Synthetics runtime the canaries run on.

Amazon Inspector scans the Lambda function behind every canary, and with it the
AWS-managed ``Synthetics_Selenium`` layer that the canary's runtime version selects.
On 2026-11-06 all twelve canaries ran ``syn-python-selenium-12.0``. Its layer carries
urllib3 2.7.0, Brotli 1.2.0 and certifi 2024.7.4, which Inspector reported as 3
CRITICAL and 2 HIGH findings per canary: 36 CRITICAL and 24 HIGH per Region, about
half of all CRITICAL findings in the account.

The layer of each runtime, read on 2026-11-06 from the published layer zips:

* ``syn-python-selenium-12.0``: urllib3 2.7.0, Brotli 1.2.0, certifi 2024.7.4
* ``syn-python-selenium-13.0``: urllib3 2.8.0, Brotli 1.2.0, certifi 2024.7.4
* ``syn-python-selenium-14.0``: urllib3 2.8.0, Brotli 1.2.1, certifi 2026.10.19

14.0 is the first runtime with the fixed version of all three (urllib3 2.8.0, Brotli
1.2.1, certifi 2026.10.19). The package versions are not in the template, so this test
pins the runtime version that carries them. ``RuntimeVersion`` is an in-place update
for ``AWS::Synthetics::Canary``, and the canary scripts only use the
``aws_synthetics`` Selenium API, which 14.0 keeps (Python 3.12, Selenium 4.32.0).

When AWS publishes a newer runtime, raise the floor with the template.

Run with:  pytest tests/test_canary_runtime.py -v
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CANARIES = REPO_ROOT / "deployment" / "canaries.yaml"

RUNTIME_FLOOR = (14, 0)
RUNTIME_RE = re.compile(r"^\s*RuntimeVersion:\s*syn-python-selenium-(\d+)\.(\d+)\s*$", re.MULTILINE)


def _runtimes() -> list:
    return [(int(major), int(minor)) for major, minor in RUNTIME_RE.findall(CANARIES.read_text())]


def test_every_canary_declares_a_python_selenium_runtime():
    canaries = len(re.findall(r"^\s*Type:\s*['\"]?AWS::Synthetics::Canary['\"]?\s*$", CANARIES.read_text(), re.MULTILINE))
    assert canaries, "canaries.yaml defines no canaries"
    assert len(_runtimes()) == canaries, (
        f"{canaries} canaries, but {len(_runtimes())} RuntimeVersion lines of the form "
        "syn-python-selenium-MAJOR.MINOR; a canary on another runtime family needs its own floor"
    )


def test_canary_runtimes_carry_the_fixed_urllib3_brotli_and_certifi():
    too_old = sorted({f"syn-python-selenium-{major}.{minor}" for major, minor in _runtimes() if (major, minor) < RUNTIME_FLOOR})
    assert not too_old, (
        f"canaries on {too_old}, below syn-python-selenium-{RUNTIME_FLOOR[0]}.{RUNTIME_FLOOR[1]}: "
        "their Synthetics_Selenium layer ships a urllib3, Brotli or certifi that Inspector rates CRITICAL"
    )
