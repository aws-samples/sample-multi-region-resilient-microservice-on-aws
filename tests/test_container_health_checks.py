"""Contract tests for the back-end container health checks.

Without a container health check, ECS counts a new back-end task healthy as soon
as its containers start. During a rolling deployment it then stopped the old
tasks before the new application answered: carts took 132-238 s to start on 256
CPU units, so every deployment failed the journeys for 4-5 minutes (ui calls
carts on every page). And a task that stopped answering kept its share of
traffic, because nothing told ECS it was unhealthy.

With these checks ECS keeps the old tasks until new ones pass, and replaces a
task that stops answering. A check must therefore:

* run a command that exists in the image (CMD, no shell: catalog is distroless);
* probe an endpoint that checks no dependencies, or a database or broker outage
  would make ECS replace every task (orders' aggregate /actuator/health returns
  503 when RabbitMQ is unreachable; its readiness group stays 200);
* give the application long enough to start before failures count.

Run with:  pytest tests/test_container_health_checks.py -v
"""

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).parent.parent
DEPLOYMENT = REPO / "deployment"
SOURCE = REPO / "source"

# ECS API limit on a container health check's start period.
MAX_START_PERIOD = 300

# Longest start seen per service, in seconds. From test2's logs, 10-02 to 10-05:
# Spring's "process running for" when it logged "Started"; catalog's first
# request served after the task's first log line (an upper bound); checkout's
# first log line to "Nest application successfully started". assets has no
# start log line: a local run of its image served health.html within 3 s.
WORST_OBSERVED_START = {
    "carts": 238,    # on 256 CPU units (now 512, so expected to be shorter)
    "orders": 63,
    "catalog": 36,
    "checkout": 5,
    "assets": 3,
}

# Per back-end container: the probe command, and the source directory whose
# image must provide the command.
EXPECTED_COMMANDS = {
    "carts": ["CMD", "wget", "-q", "-O", "/dev/null", "http://localhost:8080/actuator/health/readiness"],
    "orders": ["CMD", "wget", "-q", "-O", "/dev/null", "http://localhost:8080/actuator/health/readiness"],
    "catalog": ["CMD", "/manager", "healthcheck"],
    "checkout": ["CMD", "wget", "-q", "-O", "/dev/null", "http://localhost:8080/health"],
    "assets": ["CMD", "curl", "-fsS", "-o", "/dev/null", "http://localhost:8080/health.html"],
}
SOURCE_DIR = {"carts": "cart", "orders": "orders", "catalog": "catalog", "checkout": "checkout", "assets": "assets"}


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that keeps CloudFormation short-form tags as {"!Tag": value}."""


def _cfn_tag(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {"!" + tag_suffix: value}


_CfnLoader.add_multi_constructor("!", _cfn_tag)


def _load_template(path):
    # Drive the SafeLoader subclass directly rather than passing it to yaml.load
    # (see tests/test_yaml_loading.py).
    loader = _CfnLoader(path.read_text())
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


RESOURCES = _load_template(DEPLOYMENT / "ecs.yaml")["Resources"]
TASK_DEFINITIONS = {lid: r["Properties"] for lid, r in RESOURCES.items() if r["Type"] == "AWS::ECS::TaskDefinition"}


def _app_container(name):
    """The container named `name` and its task definition's properties."""
    found = [
        (container, props)
        for props in TASK_DEFINITIONS.values()
        for container in props["ContainerDefinitions"]
        if container.get("Name") == name
    ]
    assert len(found) == 1, f"expected one {name} container, found {len(found)}"
    return found[0]


def _env(container):
    return {e["Name"]: e["Value"] for e in container.get("Environment", [])}


def _runtime_stage(dockerfile):
    """Lines of the Dockerfile's last stage, the one the task runs."""
    text = (SOURCE / dockerfile).read_text()
    stages = re.split(r"(?m)^FROM\s", text)
    return "FROM " + stages[-1]


@pytest.mark.parametrize("name", sorted(EXPECTED_COMMANDS))
def test_back_end_has_a_health_check(name):
    container, _ = _app_container(name)
    assert container.get("Essential") is True, "ECS only counts health checks on essential containers"
    check = container.get("HealthCheck")
    assert check, f"{name} needs a container health check"
    assert check["Command"] == EXPECTED_COMMANDS[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_COMMANDS))
def test_health_check_timing(name):
    check = _app_container(name)[0]["HealthCheck"]
    assert check["Interval"] == 10
    assert check["Timeout"] == 5 and check["Timeout"] < check["Interval"]
    assert check["Retries"] == 3
    assert WORST_OBSERVED_START[name] <= check["StartPeriod"] <= MAX_START_PERIOD, (
        f"{name}: the start period must cover the slowest start seen and stay within the ECS limit"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_COMMANDS))
def test_health_check_skips_dependencies(name):
    url = _app_container(name)[0]["HealthCheck"]["Command"][-1]
    assert not url.rstrip("/").endswith("/actuator/health"), "the aggregate Spring health includes the database and broker"


@pytest.mark.parametrize("name", ["carts", "orders"])
def test_spring_probe_endpoints_are_enabled(name):
    container, _ = _app_container(name)
    # Without this the readiness URL is a 404, and every new task fails its check.
    assert _env(container).get("MANAGEMENT_ENDPOINT_HEALTH_PROBES_ENABLED") == "true"
    pom = (SOURCE / SOURCE_DIR[name] / "pom.xml").read_text()
    assert "spring-boot-starter-actuator" in pom


@pytest.mark.parametrize("name", ["carts", "orders"])
def test_spring_images_install_wget(name):
    stage = _runtime_stage(f"{SOURCE_DIR[name]}/Dockerfile")
    install = re.search(r"dnf install(?:[^\n\\]|\\\n)*", stage)
    assert install and re.search(r"\bwget\b", install.group(0)), "the health check runs wget from the image"


def test_checkout_image_has_busybox_wget():
    stage = _runtime_stage("checkout/Dockerfile")
    assert re.match(r"FROM \S*node:\S*alpine", stage), "BusyBox wget comes with the Alpine base image"


def test_checkout_health_endpoint_checks_nothing():
    controller = (SOURCE / "checkout/src/app.controller.ts").read_text()
    assert re.search(r"@Get\('health'\)\s*@HealthCheck\(\)\s*health\(\)\s*\{\s*return this\.healthCheckService\.check\(\[\]\);", controller)


def test_assets_image_serves_health_html_and_has_curl():
    stage = _runtime_stage("assets/Dockerfile")
    assert re.match(r"FROM \S*amazonlinux:2023", stage), "curl-minimal comes with the Amazon Linux 2023 base image"
    assert (SOURCE / "assets/public/health.html").is_file()


def test_catalog_binary_answers_its_own_health_check():
    stage = _runtime_stage("catalog/Dockerfile")
    assert 'ENTRYPOINT ["/manager"]' in stage
    main = (SOURCE / "catalog/main.go").read_text()
    assert re.search(r'os\.Args\[1\] == "healthcheck"', main), "/manager healthcheck must be handled before startup"
    # /health answers 200 without touching the database.
    assert re.search(r'r\.GET\("/health", func\(c \*gin\.Context\) \{\s*c\.String\(http\.StatusOK, "OK"\)\s*\}\)', main)


@pytest.mark.parametrize("lid", sorted(TASK_DEFINITIONS))
def test_jvm_tasks_have_room_for_the_service_connect_proxy(lid):
    # AWS recommends 256 CPU units for the Service Connect proxy on top of the
    # application. A JVM with the OpenTelemetry agent needs at least 256 more.
    props = TASK_DEFINITIONS[lid]
    jvm = any("-javaagent:" in str(_env(c).get("JAVA_TOOL_OPTIONS", "")) for c in props["ContainerDefinitions"])
    if jvm:
        assert int(props["Cpu"]) >= 512, f"{lid} runs a JVM on {props['Cpu']} CPU units"
