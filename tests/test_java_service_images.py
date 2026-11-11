"""The carts, orders and ui images carry no copy of the ADOT Java agent.

The task definitions in ``deployment/ecs.yaml`` load the agent from a shared task
volume. An init container fills that volume from the ``adot-autoinstrumentation-java``
sidecar image, and ``JAVA_TOOL_OPTIONS`` points at the copy there.

The three Dockerfiles used to download ``aws-opentelemetry-agent.jar`` v1.24.0 into
``/opt`` as well, behind a commented-out ``ENV`` line, so nothing ever read it. Inspector
scanned it anyway: the jar bundles jackson-databind and jackson-core 2.14.2, which it
reported as 8 HIGH findings per image, 24 per Region across the three services.

``wget`` stays in the image. The ECS health check runs it (see
tests/test_container_health_checks.py).

Run with:  pytest tests/test_java_service_images.py -v
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SOURCE_DIR = REPO_ROOT / "source"
ECS_TEMPLATE = REPO_ROOT / "deployment" / "ecs.yaml"

JAVA_SERVICES = ("cart", "orders", "ui")
AGENT_FROM_SIDECAR = "-javaagent:/otel-auto-instrumentation/javaagent.jar"
BAKED_IN_AGENT_MARKERS = ("aws-opentelemetry-agent", "aws-otel-java-instrumentation")


def _instructions(dockerfile: Path) -> str:
    """The Dockerfile without comment lines."""
    return "\n".join(line for line in dockerfile.read_text().splitlines() if not line.lstrip().startswith("#"))


@pytest.mark.parametrize("service", JAVA_SERVICES)
def test_image_does_not_download_the_adot_agent(service):
    text = _instructions(SOURCE_DIR / service / "Dockerfile")
    found = [marker for marker in BAKED_IN_AGENT_MARKERS if marker in text]
    assert not found, (
        f"source/{service}/Dockerfile fetches the ADOT agent ({found}); the task definition already loads "
        "it from the sidecar volume, and the extra jar only adds findings (jackson 2.14.2)"
    )


def test_task_definitions_still_load_the_agent_from_the_sidecar_volume():
    template = ECS_TEMPLATE.read_text()
    assert len(re.findall(re.escape(AGENT_FROM_SIDECAR), template)) >= len(JAVA_SERVICES), (
        "ecs.yaml no longer points JAVA_TOOL_OPTIONS at the sidecar copy for every Java service; "
        "if the agent is meant to come from the image again, bring this test and the Dockerfiles back together"
    )
    assert "/opt/aws-opentelemetry-agent.jar" not in template
