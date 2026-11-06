"""The checkout image runs node and leaves npm behind.

``node:26-alpine`` bundles npm, and npm bundles its own copies of undici and brace-expansion under
``/usr/local/lib/node_modules/npm/node_modules``. On 2026-11-06 the newest node:26-alpine3.24 (node 26.13.0,
npm 11.20.0) still shipped undici 6.28.0 and brace-expansion 5.0.9, and Inspector rated them 1 CRITICAL and 7
HIGH on the running checkout image in each Region. A rebuild does not clear them, and the service never calls
npm after the build: the entrypoint is ``node dist/main.js``.

Inspector's SBOM generator applies the whiteouts of later layers, so removing npm in a later layer takes it out
of the findings (AWS documents that a path a layer deletes is no longer reported).

BusyBox ``wget`` stays: the ECS health check uses it (see tests/test_container_health_checks.py).

Run with:  pytest tests/test_checkout_image.py -v
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DOCKERFILE = REPO_ROOT / "source" / "checkout" / "Dockerfile"


def _instructions() -> str:
    return "\n".join(line for line in DOCKERFILE.read_text().splitlines() if not line.lstrip().startswith("#"))


def test_npm_is_removed_after_the_build_and_the_user_is_restored():
    text = _instructions()
    build = text.index("RUN npm run build")
    removal = re.search(r"^RUN rm -rf [^\n]*/usr/local/lib/node_modules/npm\b[^\n]*$", text, re.MULTILINE)
    assert removal, "checkout/Dockerfile no longer removes /usr/local/lib/node_modules/npm"
    assert removal.start() > build, "npm is removed before the build finishes"
    removed = removal.group(0)
    assert "/usr/local/bin/npm" in removed and "/usr/local/bin/npx" in removed, "remove the npm and npx launchers with it"
    after = text[removal.end():]
    root = text.rfind("USER root", 0, removal.start())
    assert root != -1 and root > build, "removing npm from /usr/local needs root, set just before the removal"
    assert re.search(r"^USER appuser\s*$", after, re.MULTILINE), "the image must drop back to appuser before ENTRYPOINT"


def test_nothing_after_the_removal_needs_npm():
    text = _instructions()
    removal = re.search(r"^RUN rm -rf [^\n]*/usr/local/lib/node_modules/npm\b[^\n]*$", text, re.MULTILINE)
    assert removal, "checkout/Dockerfile no longer removes /usr/local/lib/node_modules/npm"
    after = text[removal.end():]
    assert re.search(r"^ENTRYPOINT\s*\[\s*\"node\"", after, re.MULTILINE), "the entrypoint must be node itself"
    assert not re.search(r"\bnpm\b|\bnpx\b|\byarn\b", after), "an instruction after the removal calls npm"
