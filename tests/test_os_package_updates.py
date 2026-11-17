"""The images take the newest Amazon Linux 2023 security updates when they are built.

Amazon Linux 2023 repositories are versioned. dnf in an image only sees the packages of the release the
image was built from, so a plain ``dnf upgrade`` finds nothing newer than the base image already holds.
Checked with ``docker build`` on 2026-11-17: an assets image on amazonlinux:2023.12.20261109.0 kept
rpm-libs 4.16.1.3-29.amzn2023.0.9 and sqlite-libs 3.40.0-1.amzn2023.0.8 with a plain ``dnf upgrade``, and
a ui, carts or orders image on amazoncorretto:17.0.20-al2023 kept glibc 2.34-231.amzn2023.0.5 and rpm
4.16.1.3-29.amzn2023.0.8. The fixed builds (sqlite-libs 3.40.0-1.amzn2023.0.9, glibc
2.34-231.amzn2023.0.6, rpm 4.16.1.3-29.amzn2023.0.10) were published on 2026-11-09 and 2026-11-12.
``dnf upgrade --releasever=latest`` moves the image to the newest release and installs them.

Every build therefore takes whatever security updates exist that day, so two builds of the same commit
can differ. That is intended: an image should not ship a package that has a fixed build.

The FIS sidecar's Dockerfile is also written out by mirror-sidecar-buildspec.yml, and
tests/test_repave_sidecar_image.py holds the two copies equal.

Run with:  pytest tests/test_os_package_updates.py -v
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

DOCKERFILES = {
    "ui": REPO_ROOT / "source" / "ui" / "Dockerfile",
    "cart": REPO_ROOT / "source" / "cart" / "Dockerfile",
    "orders": REPO_ROOT / "source" / "orders" / "Dockerfile",
    "assets": REPO_ROOT / "source" / "assets" / "Dockerfile",
    "ssm-agent-sidecar": REPO_ROOT / "deployment" / "ssm-agent-sidecar.Dockerfile",
}


def _dnf_commands(dockerfile: Path) -> list:
    """The dnf commands of the image that ships, in order: the last stage's RUN instructions,
    without comments, with line continuations joined and ``&&`` chains split."""
    text = re.sub(r"\\\n", " ", dockerfile.read_text())
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    last_stage = max(i for i, line in enumerate(lines) if re.match(r"FROM\s", line, re.IGNORECASE))
    commands = []
    for line in lines[last_stage:]:
        if line.startswith("RUN "):
            commands += [part.strip() for part in line[len("RUN "):].split("&&")]
    return [c for c in commands if re.match(r"dnf\s", c)]


@pytest.mark.parametrize("name", sorted(DOCKERFILES))
def test_the_image_upgrades_to_the_latest_release_before_it_installs(name):
    commands = _dnf_commands(DOCKERFILES[name])
    upgrades = [i for i, c in enumerate(commands) if re.search(r"\bupgrade\b", c)]
    installs = [i for i, c in enumerate(commands) if re.search(r"\binstall\b", c)]
    assert upgrades, f"{DOCKERFILES[name].relative_to(REPO_ROOT)} never runs dnf upgrade"
    assert installs and upgrades[0] < installs[0], "packages are installed from the base image's release, before the upgrade"
    assert "--releasever=latest" in commands[upgrades[0]], (
        f"{DOCKERFILES[name].relative_to(REPO_ROOT)}: `{commands[upgrades[0]]}` stays on the release of the base image "
        "(Amazon Linux 2023 repositories are versioned), so it finds no newer package; add --releasever=latest"
    )
