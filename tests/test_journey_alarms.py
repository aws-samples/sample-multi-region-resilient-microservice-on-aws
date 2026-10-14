# SPDX-License-Identifier: MIT-0
"""Step 6 (design 4.3 and 5.5): the journey, region-degraded and hop alarms in monitoring.yml,
the alarms they retire, and the observation path every signal travels through.

Every canary enters through the ALB and ui, so a journey alarm is an end-to-end verdict. The
chain that makes that true is pinned here too: canary run limit > ui's read timeout > the
back-ends' 3-second Service Connect limit, and ui's health check calls no back-end.
"""

import re
from pathlib import Path

import pytest
import yaml

DEPLOYMENT = Path(__file__).resolve().parent.parent / "deployment"

JOURNEYS = ["home", "cart", "catalog", "orders"]
# view -> canary name prefix (canaries.yaml)
VIEWS = {"lcl": "lcl-rgnl-", "rmt": "rmt-rgnl-", "global": "global-"}
# hop alarm service -> (NGRH service tag, Service Connect discovery name; None for ui)
HOPS = {
    "ui": ("ui", None),
    "catalog": ("catalog", "catalog"),
    "carts": ("cart", "carts"),
    "checkout": ("checkout", "checkout"),
    "orders": ("orders", "orders"),
}
RETIRED = [
    "ngrh-rmt-home-failed",
    "ngrh-rmt-cart-failed",
    "ngrh-rmt-catalog-failed",
    "ngrh-rmt-orders-failed",
    "ngrh-lcl-home-healthy",
    "ngrh-region-switch-trigger",
]


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


MONITORING = _load_template(DEPLOYMENT / "monitoring.yml")
CANARIES = _load_template(DEPLOYMENT / "canaries.yaml")
ECS = _load_template(DEPLOYMENT / "ecs.yaml")
MAKEFILE = (DEPLOYMENT / "Makefile").read_text()


def _alarms():
    """AlarmName (the !Sub string) -> properties, for every alarm in monitoring.yml."""
    out = {}
    for lid, r in MONITORING["Resources"].items():
        if r["Type"] in ("AWS::CloudWatch::Alarm", "AWS::CloudWatch::CompositeAlarm"):
            out[r["Properties"]["AlarmName"]["!Sub"]] = dict(r["Properties"], _logical_id=lid, _type=r["Type"])
    return out


ALARMS = _alarms()


def _tag(props):
    return {t["Key"]: t["Value"] for t in props.get("Tags", [])}.get("service")


def _canaries():
    """Canary name (the !Sub string) -> properties."""
    return {
        r["Properties"]["Name"]["!Sub"]: r["Properties"]
        for r in CANARIES["Resources"].values()
        if r["Type"] == "AWS::Synthetics::Canary"
    }


CANARY = _canaries()


def _journey(view, journey):
    return ALARMS[f"journey-{view}-{journey}-${{AWS::Region}}${{Env}}"]


# --- journey alarms ---------------------------------------------------------------------------

def test_there_are_exactly_twelve_journey_alarms():
    names = {n for n in ALARMS if n.startswith("journey-")}
    assert names == {f"journey-{v}-{j}-${{AWS::Region}}${{Env}}" for v in VIEWS for j in JOURNEYS}


@pytest.mark.parametrize("view", VIEWS)
@pytest.mark.parametrize("journey", JOURNEYS)
def test_journey_alarm_settings(view, journey):
    p = _journey(view, journey)
    assert p["_type"] == "AWS::CloudWatch::Alarm"
    assert (p["Namespace"], p["MetricName"]) == ("CloudWatchSynthetics", "SuccessPercent")
    assert p["Dimensions"] == [{"Name": "CanaryName", "Value": {"!Sub": f"{VIEWS[view]}{journey}${{Env}}"}}]
    assert (p["Statistic"], p["Period"]) == ("Average", 60)
    assert (p["EvaluationPeriods"], p["DatapointsToAlarm"]) == (3, 2)
    assert (p["ComparisonOperator"], p["Threshold"]) == ("LessThanThreshold", 50)
    assert p["TreatMissingData"] == "breaching"
    assert _tag(p) == "shared"


@pytest.mark.parametrize("view", VIEWS)
@pytest.mark.parametrize("journey", JOURNEYS)
def test_each_journey_alarm_watches_a_canary_that_runs_every_minute(view, journey):
    canary = CANARY[f"{VIEWS[view]}{journey}${{Env}}"]
    # A 60-second period with 2 of 3 needs one run per minute.
    assert canary["Schedule"]["Expression"] == "rate(1 minute)"


def test_region_degraded_is_any_local_journey():
    p = ALARMS["region-degraded-${AWS::Region}${Env}"]
    assert p["_type"] == "AWS::CloudWatch::CompositeAlarm"
    terms = [t.strip() for t in p["AlarmRule"]["!Sub"].split(" OR ")]
    expected = {"ALARM(${%s})" % _journey("lcl", j)["_logical_id"] for j in JOURNEYS}
    assert set(terms) == expected and len(terms) == 4
    assert _tag(p) == "shared"


# --- hop alarms -------------------------------------------------------------------------------

def _per_request_timeout_seconds(discovery_name):
    for r in ECS["Resources"].values():
        if r["Type"] != "AWS::ECS::Service":
            continue
        for svc in r["Properties"].get("ServiceConnectConfiguration", {}).get("Services", []):
            # ECS uses the port name as the discovery name when none is set, and the
            # TargetDiscoveryName metric dimension carries that name.
            if svc.get("DiscoveryName", svc.get("PortName")) == discovery_name:
                return svc["Timeout"]["PerRequestTimeoutSeconds"]
    raise AssertionError(f"no Service Connect service named {discovery_name}")


def test_there_are_exactly_ten_hop_alarms():
    names = {n for n in ALARMS if n.startswith("hop-")}
    assert names == {f"hop-{s}-{k}-${{AWS::Region}}${{Env}}" for s in HOPS for k in ("errors", "slow")}


@pytest.mark.parametrize("service", HOPS)
@pytest.mark.parametrize("kind", ["errors", "slow"])
def test_hop_alarm_settings(service, kind):
    p = ALARMS[f"hop-{service}-{kind}-${{AWS::Region}}${{Env}}"]
    tag, discovery = HOPS[service]
    assert _tag(p) == tag
    assert (p["Period"], p["EvaluationPeriods"], p["DatapointsToAlarm"]) == (60, 3, 2)
    assert p["ComparisonOperator"] == "GreaterThanThreshold"
    assert p["TreatMissingData"] == "notBreaching"
    if discovery is None:
        assert p["Namespace"] == "AWS/ApplicationELB"
        assert p["Dimensions"] == [
            {"Name": "LoadBalancer", "Value": {"!Ref": "UiLoadBalancerFullName"}},
            {"Name": "TargetGroup", "Value": {"!Ref": "UiTargetGroupFullName"}},
        ]
    else:
        assert p["Namespace"] == "AWS/ECS"
        assert p["Dimensions"] == [{"Name": "TargetDiscoveryName", "Value": discovery}]
    if kind == "errors":
        assert (p["MetricName"], p["Statistic"], p["Threshold"]) == ("HTTPCode_Target_5XX_Count", "Sum", 0)
    else:
        assert (p["MetricName"], p["Statistic"]) == ("TargetResponseTime", "Maximum")
        # ALB reports seconds; Service Connect reports milliseconds. Both mean 3 s.
        assert p["Threshold"] == (3 if discovery is None else 3000)


@pytest.mark.parametrize("service", [s for s, (_, d) in HOPS.items() if d])
def test_back_end_slow_threshold_is_its_service_connect_limit(service):
    discovery = HOPS[service][1]
    p = ALARMS[f"hop-{service}-slow-${{AWS::Region}}${{Env}}"]
    assert p["Threshold"] == 1000 * _per_request_timeout_seconds(discovery)


def test_ui_hop_alarm_parameters_only_accept_full_names():
    params = MONITORING["Parameters"]
    assert re.fullmatch(params["UiLoadBalancerFullName"]["AllowedPattern"], "app/apps-dev-Alb-hUtZq9aF46Vo/95f1d8d144679a90")
    assert not re.fullmatch(params["UiLoadBalancerFullName"]["AllowedPattern"], "")
    assert re.fullmatch(params["UiTargetGroupFullName"]["AllowedPattern"],
                        "targetgroup/apps-d-AlbTa-2RMMTTANCYXQ/c86dcb6adef103e7")
    assert not re.fullmatch(params["UiTargetGroupFullName"]["AllowedPattern"], "")
    assert "Default" not in params["UiLoadBalancerFullName"] and "Default" not in params["UiTargetGroupFullName"]


def _monitoring_recipe():
    match = re.search(r"^monitoring:.*?\n((?:\t.*\n)+)", MAKEFILE, re.M)
    assert match, "no monitoring target"
    return match.group(1)


def test_makefile_derives_ui_alarm_dimensions_from_the_apps_stack():
    recipe = _monitoring_recipe()
    for region in ("PRIMARY", "STANDBY"):
        assert re.search(
            rf"_{region}_ALB:=\$\(shell aws cloudformation describe-stack-resource --stack-name apps\$\(ENV\) "
            rf"--logical-resource-id Alb --region \$\({region}_REGION\).*sed 's\|\.\*:loadbalancer/\|\|'", recipe)
        assert re.search(
            rf"_{region}_UI_TG:=\$\(shell aws cloudformation describe-stack-resource --stack-name apps\$\(ENV\) "
            rf"--logical-resource-id AlbTargetGroup --region \$\({region}_REGION\).*sed 's\|\.\*:\|\|'", recipe)


def test_makefile_passes_each_region_its_own_ui_alarm_dimensions():
    deploys = re.findall(r"aws cloudformation deploy --region \$\{(\w+)_REGION\} --template \./monitoring\.yml(.*?)--tags",
                         _monitoring_recipe(), re.S)
    assert [r for r, _ in deploys] == ["PRIMARY", "STANDBY"]
    for region, args in deploys:
        assert f"UiLoadBalancerFullName=$(_{region}_ALB)" in args
        assert f"UiTargetGroupFullName=$(_{region}_UI_TG)" in args


def test_the_apps_stack_logical_ids_the_makefile_reads_exist():
    assert ECS["Resources"]["Alb"]["Type"] == "AWS::ElasticLoadBalancingV2::LoadBalancer"
    assert ECS["Resources"]["AlbTargetGroup"]["Type"] == "AWS::ElasticLoadBalancingV2::TargetGroup"


# --- retired and kept alarms ------------------------------------------------------------------

def test_superseded_alarms_are_retired():
    text = (DEPLOYMENT / "monitoring.yml").read_text()
    for name in RETIRED:
        assert name not in text
    assert "Outputs" not in MONITORING


def test_diagnostic_alarms_are_kept():
    for name in [
        "canary-success-low-${AWS::Region}${Env}",
        "aurora-degraded-${AWS::Region}${Env}",
        "dynamodb-errors-${AWS::Region}${Env}",
        "orders-created-zero-${AWS::Region}${Env}",
    ]:
        assert name in ALARMS


# --- observation path (design 4.3) ------------------------------------------------------------

def _script(canary):
    return canary["Code"]["Script"]["!Sub"]


@pytest.mark.parametrize("journey", JOURNEYS)
def test_each_canary_enters_through_the_alb_or_the_global_name(journey):
    hosts = {
        "lcl": "{{resolve:secretsmanager:Alb-${AWS::Region}${Env}:SecretString:DnsName}}",
        "rmt": "{{resolve:secretsmanager:Alb-${RemoteRegion}${Env}:SecretString:DnsName}}",
        "global": "{{resolve:secretsmanager:DNSRecordSecret${Env}}}",
    }
    for view, prefix in VIEWS.items():
        urls = re.findall(r"http://([^\"/]+)", _script(CANARY[f"{prefix}{journey}${{Env}}"]))
        assert urls and set(urls) == {hosts[view]}, (view, journey, urls)


def _ui_read_timeout_seconds():
    ui = ECS["Resources"]["UiTaskDefinition"]["Properties"]["ContainerDefinitions"]
    env = {e["Name"]: e["Value"] for c in ui if c["Name"] == "ui" for e in c.get("Environment", [])}
    return int(env["ENDPOINTS_TIMEOUT_READ_SECONDS"])


def test_canary_limit_exceeds_ui_read_timeout_which_exceeds_the_back_end_limit():
    ui_read = _ui_read_timeout_seconds()
    shortest_canary = min(c["RunConfig"]["TimeoutInSeconds"] for c in CANARY.values())
    longest_back_end = max(_per_request_timeout_seconds(d) for d in ("catalog", "carts", "checkout", "orders"))
    # A hung back-end hits its own limit first, ui turns that into an error page, and the
    # canary sees the page well inside its run.
    assert shortest_canary > ui_read > longest_back_end


def test_ui_health_check_calls_no_back_end():
    # /actuator only lists the endpoints; it runs no health indicators, so a back-end fault
    # never deregisters ui and the canaries see error pages, not a target-less ALB.
    assert ECS["Resources"]["AlbTargetGroup"]["Properties"]["HealthCheckPath"] == "/actuator"
