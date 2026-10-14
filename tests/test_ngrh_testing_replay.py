# SPDX-License-Identifier: MIT-0
"""Step 6: deployment/ngrh_testing's AWS CLI layer and the replay command (design 5.9).

Everything runs against fakes: a fake AWS CLI runner for the CLI layer, and a fake AwsCli that
serves canned canary series for the replay. The model's settings are pinned to monitoring.yml so
the replay can't drift from the alarms it claims to replay.
"""

import ast
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

DEPLOYMENT = Path(__file__).resolve().parent.parent / "deployment"
PACKAGE = DEPLOYMENT / "ngrh_testing"
sys.path.insert(0, str(DEPLOYMENT))

from ngrh_testing import alarms, replay  # noqa: E402
from ngrh_testing.alarms import Episode  # noqa: E402
from ngrh_testing.aws import AwsCli, AwsCliError  # noqa: E402
from ngrh_testing.cli import main  # noqa: E402


# --- the model ------------------------------------------------------------------------------

def test_one_failed_run_never_alarms():
    assert alarms.alarm_states([100, 100, 0, 100, 100]) == [False, False, False]


def test_two_failures_within_three_minutes_alarm():
    # windows: [100,0,100] [0,100,0] [100,0,100] [0,100,100]
    assert alarms.alarm_states([100, 0, 100, 0, 100, 100]) == [False, True, False, False]


def test_a_minute_without_a_datapoint_counts_as_a_failure():
    # windows: [100,None,0] [None,0,100]: both hold two breaching minutes.
    assert alarms.alarm_states([100, None, 0, 100]) == [True, True]
    assert alarms.alarm_states([100, None, 100, 100]) == [False, False]


def test_two_minutes_without_datapoints_alarm_on_their_own():
    assert alarms.alarm_states([100, None, None, 100]) == [True, True]


def test_exactly_the_threshold_does_not_breach():
    assert alarms.alarm_states([50, 50, 50]) == [False]
    assert alarms.alarm_states([49.9, 49.9, 100]) == [True]


def test_the_first_reported_minute_uses_the_minutes_before_it():
    assert alarms.alarm_states([0, 0, 100]) == [True]


def test_alarm_states_need_a_full_window():
    with pytest.raises(ValueError):
        alarms.alarm_states([100, 100])


def test_episodes_include_one_still_open_at_the_window_end():
    assert alarms.episodes([False, True, True, False, True]) == [Episode(1, 2), Episode(4, 1)]
    assert alarms.episodes([True, True]) == [Episode(0, 2)]
    assert alarms.episodes([False, False]) == []


def test_a_trigger_needs_local_red_remote_red_and_the_peer_green():
    lcl = [True, True, True, False]
    rmt = [True, False, True, True]
    peer_degraded = [False, False, True, False]
    assert alarms.trigger_states(lcl, rmt, peer_degraded) == [True, False, False, False]


def test_a_trigger_starts_at_most_one_execution_per_hour():
    states = [False] * 200
    for i in list(range(0, 5)) + list(range(30, 35)) + [70, 71]:
        states[i] = True
    assert alarms.executions(states) == [0, 70]


def test_eight_triggers_each_region_confirmed_by_its_peer():
    ts = alarms.triggers("us-east-1", "us-west-2")
    assert len(ts) == 8
    assert {(t.region, t.peer) for t in ts} == {("us-east-1", "us-west-2"), ("us-west-2", "us-east-1")}
    assert sorted(t.journey for t in ts) == sorted(alarms.JOURNEYS * 2)


# --- the model matches monitoring.yml --------------------------------------------------------

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


MONITORING = {
    r["Properties"]["AlarmName"]["!Sub"]: r["Properties"]
    for r in _load_template(DEPLOYMENT / "monitoring.yml")["Resources"].values()
    if r["Type"] in ("AWS::CloudWatch::Alarm", "AWS::CloudWatch::CompositeAlarm")
}


@pytest.mark.parametrize("view", alarms.VIEWS)
@pytest.mark.parametrize("journey", alarms.JOURNEYS)
def test_the_model_matches_each_journey_alarm_in_monitoring_yml(view, journey):
    p = MONITORING[alarms.journey_alarm_name(view, journey, "${AWS::Region}", "${Env}")]
    assert (p["Namespace"], p["MetricName"], p["Statistic"]) == (alarms.NAMESPACE, alarms.METRIC, alarms.STATISTIC)
    assert p["Dimensions"] == [{"Name": "CanaryName", "Value": {"!Sub": alarms.canary_name(view, journey, "${Env}")}}]
    assert p["Period"] == alarms.PERIOD_SECONDS
    assert (p["EvaluationPeriods"], p["DatapointsToAlarm"]) == (alarms.EVALUATION_PERIODS, alarms.DATAPOINTS_TO_ALARM)
    assert (p["ComparisonOperator"], p["Threshold"]) == ("LessThanThreshold", alarms.THRESHOLD)
    assert p["TreatMissingData"] == ("breaching" if alarms.MISSING_DATA_BREACHES else "notBreaching")


def test_the_model_matches_region_degraded_in_monitoring_yml():
    p = MONITORING[alarms.region_degraded_name("${AWS::Region}", "${Env}")]
    assert p["AlarmRule"]["!Sub"].count("ALARM(${JourneyLcl") == len(alarms.JOURNEYS)
    assert " AND " not in p["AlarmRule"]["!Sub"]


# --- the package stays Python 3.9+, standard library only ------------------------------------

MODULES = sorted(PACKAGE.glob("*.py"))


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.name)
def test_modules_parse_as_python_3_9(module):
    ast.parse(module.read_text(), feature_version=(3, 9))


@pytest.mark.parametrize("module", [m for m in MODULES if m.name not in ("__main__.py", "__init__.py")],
                         ids=lambda m: m.name)
def test_annotations_are_never_evaluated(module):
    # PEP 604 unions and builtin generics in annotations fail at import on 3.9 without this.
    assert "from __future__ import annotations" in module.read_text()


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.name)
def test_modules_import_only_the_standard_library(module):
    for node in ast.walk(ast.parse(module.read_text())):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module]
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            assert top == "__future__" or top in sys.stdlib_module_names, f"{module.name} imports {name}"


# --- the AWS CLI layer -----------------------------------------------------------------------

class FakeRunner:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        code, out, err = self.outcomes.pop(0)
        return subprocess.CompletedProcess(args, code, out, err)


THROTTLED = (255, "", "An error occurred (Throttling) when calling the GetMetricData operation: Rate exceeded")


def test_the_command_line_asks_for_json_without_a_pager():
    args = AwsCli.command("cloudwatch", "get-metric-data", "us-east-1",
                          metric_data_queries=[{"Id": "q0"}], scan_by="TimestampAscending")
    assert args[:8] == ["aws", "cloudwatch", "get-metric-data", "--output", "json", "--no-cli-pager",
                        "--region", "us-east-1"]
    assert json.loads(args[args.index("--metric-data-queries") + 1]) == [{"Id": "q0"}]
    assert args[args.index("--scan-by") + 1] == "TimestampAscending"


def test_calls_run_the_cli_directly_never_through_a_shell():
    runner = FakeRunner([(0, '{"Account": "111122223333"}', "")])
    assert AwsCli(runner=runner).account_id() == "111122223333"
    args, kwargs = runner.calls[0]
    assert args[:3] == ["aws", "sts", "get-caller-identity"] and not kwargs.get("shell")


def test_throttling_is_retried_with_backoff():
    runner = FakeRunner([THROTTLED, THROTTLED, (0, '{"MetricDataResults": []}', "")])
    sleeps = []
    out = AwsCli(runner=runner, sleep=sleeps.append).call("cloudwatch", "get-metric-data", "us-east-1")
    assert out == {"MetricDataResults": []}
    assert sleeps == [1.0, 2.0]


def test_backoff_is_capped():
    runner = FakeRunner([THROTTLED] * 7 + [(0, "{}", "")])
    sleeps = []
    AwsCli(runner=runner, sleep=sleeps.append, attempts=8, max_delay_seconds=30).call("sts", "get-caller-identity")
    assert sleeps == [1, 2, 4, 8, 16, 30, 30]


def test_other_errors_fail_at_once_with_the_cli_message():
    runner = FakeRunner([(254, "", "An error occurred (AccessDenied) when calling the GetMetricData operation")])
    with pytest.raises(AwsCliError, match=r"cloudwatch get-metric-data in us-east-1 failed: .*AccessDenied"):
        AwsCli(runner=runner, sleep=lambda s: None).call("cloudwatch", "get-metric-data", "us-east-1")
    assert len(runner.calls) == 1


def test_throttling_gives_up_after_its_attempts():
    runner = FakeRunner([THROTTLED] * 3)
    with pytest.raises(AwsCliError, match="after 3 attempts"):
        AwsCli(runner=runner, sleep=lambda s: None, attempts=3).call("sts", "get-caller-identity")
    assert len(runner.calls) == 3


# --- the replay, end to end against a fake CLI -----------------------------------------------

NOW = dt.datetime(2026, 10, 6, 14, 0, 30, tzinfo=dt.timezone.utc).timestamp()
WINDOW = replay.make_window(NOW, 1)
FIRST = WINDOW.start - replay.LEAD_MINUTES
EAST, WEST = "us-east-1", "us-west-2"
ENV = "-dev"
ACCOUNT = "111122223333"


class FakeAws:
    """Serves {region: {canary: {epoch_minute: value}}} through get-metric-data, splitting each
    query's datapoints over `pages` entries the way the CLI's merged pages do."""

    def __init__(self, series, pages=1):
        self.series = series
        self.pages = pages
        self.calls = []

    def account_id(self):
        return ACCOUNT

    def call(self, service, operation, region=None, **params):
        self.calls.append((service, operation, region, params))
        assert (service, operation) == ("cloudwatch", "get-metric-data")
        start, end = replay._epoch_minute(params["start_time"]), replay._epoch_minute(params["end_time"])
        results = []
        for q in params["metric_data_queries"]:
            name = q["MetricStat"]["Metric"]["Dimensions"][0]["Value"]
            points = sorted((m, v) for m, v in self.series[region].get(name, {}).items() if start <= m < end)
            size = max(1, -(-len(points) // self.pages))
            for i in range(0, max(len(points), 1), size):
                chunk = points[i:i + size]
                results.append({
                    "Id": q["Id"], "Label": name, "StatusCode": "Complete",
                    "Timestamps": [replay.iso(m).replace("Z", "+00:00") for m, _ in chunk],
                    "Values": [v for _, v in chunk],
                })
        return {"MetricDataResults": results, "Messages": []}


def _healthy():
    return {
        region: {
            alarms.canary_name(v, j, ENV): {m: 100.0 for m in range(FIRST, WINDOW.end)}
            for v in alarms.VIEWS for j in alarms.JOURNEYS
        }
        for region in (EAST, WEST)
    }


def _fail(series, region, view, journey, minutes, value=0.0):
    """Fail a canary for window-relative minutes (negative = the lead-in before the window)."""
    for m in minutes:
        series[region][alarms.canary_name(view, journey, ENV)][WINDOW.start + m] = value


def _drop(series, region, view, journey, minutes):
    for m in minutes:
        del series[region][alarms.canary_name(view, journey, ENV)][WINDOW.start + m]


def _replay(capsys, series, *extra, pages=1):
    aws = FakeAws(series, pages=pages)
    code = main(["replay", "--primary-region", EAST, "--standby-region", WEST, f"--env={ENV}", *extra],
                aws=aws, now_seconds=NOW)
    out = capsys.readouterr()
    return code, out.out, out.err, aws


def _line(report, prefix):
    return next(line.strip() for line in report.splitlines() if line.strip().startswith(prefix))


def test_a_quiet_window_meets_no_trigger(capsys):
    code, report, _, _ = _replay(capsys, _healthy(), "--days", "1")
    assert code == replay.EXIT_OK
    assert f"account {ACCOUNT}" in report
    assert "Verdict: 0 of 8 triggers met; nothing here blocks arming." in report
    assert "Journey alarms that went into ALARM (0 of 24)" in report
    assert _line(report, "region-degraded-us-east-1-dev").endswith("never")


def test_a_failure_seen_from_both_regions_meets_its_trigger(capsys):
    series = _healthy()
    _fail(series, EAST, "lcl", "orders", range(100, 105))
    _fail(series, WEST, "rmt", "orders", range(100, 105))
    code, report, _, _ = _replay(capsys, series, "--days", "1")
    assert code == replay.EXIT_TRIGGER_MET
    met = _line(report, "deactivate us-east-1, orders, confirmed from us-west-2")
    # 2 of 3 failing minutes: ALARM from minute 101 to 105, five minutes.
    assert f"met 1 time, 5 min; would have started up to 1 execution: {replay.when(WINDOW.start + 101)} (5 min)" in met
    assert _line(report, "deactivate us-west-2, orders").endswith("never met")
    assert "Verdict: 1 of 8 triggers met: don't arm" in report


def test_a_failure_in_both_regions_meets_no_trigger(capsys):
    series = _healthy()
    _fail(series, EAST, "lcl", "orders", range(100, 105))
    _fail(series, WEST, "rmt", "orders", range(100, 105))
    _fail(series, WEST, "lcl", "home", range(100, 105))  # us-west-2 degraded too
    code, report, _, _ = _replay(capsys, series, "--days", "1")
    assert code == replay.EXIT_OK
    assert "Verdict: 0 of 8 triggers met" in report
    assert "Journey alarms that went into ALARM (3 of 24)" in report
    assert _line(report, "region-degraded-us-west-2-dev").startswith("region-degraded-us-west-2-dev: 1 episode,")


def test_only_the_remote_view_failing_meets_no_trigger(capsys):
    # The isolation test's shape: the cross-Region path breaks, the Region itself doesn't.
    series = _healthy()
    _fail(series, WEST, "rmt", "cart", range(300, 400))
    code, report, _, _ = _replay(capsys, series, "--days", "1")
    assert code == replay.EXIT_OK
    # 100 failing minutes: ALARM from the second one through the minute after the last.
    assert "journey-rmt-cart-us-west-2-dev: 1 episode, 100 min" in report


def test_missing_datapoints_count_as_failures(capsys):
    series = _healthy()
    _drop(series, EAST, "lcl", "catalog", [200, 201])
    code, report, _, _ = _replay(capsys, series, "--days", "1")
    assert code == replay.EXIT_OK
    line = _line(report, "journey-lcl-catalog-us-east-1-dev")
    assert "1 episode, 2 min" in line and "2 minutes without a datapoint" in line
    assert "most for journey-lcl-catalog-us-east-1-dev (2)" in report


def test_failures_just_before_the_window_alarm_at_its_first_minute(capsys):
    series = _healthy()
    _fail(series, EAST, "lcl", "home", [-2, -1])
    code, report, _, _ = _replay(capsys, series, "--days", "1")
    line = _line(report, "journey-lcl-home-us-east-1-dev")
    assert f"longest 1 min at {replay.when(WINDOW.start)}" in line


def test_an_alarm_still_firing_at_the_window_end_is_counted(capsys):
    series = _healthy()
    _fail(series, EAST, "global", "cart", range(WINDOW.minutes - 4, WINDOW.minutes))
    code, report, _, _ = _replay(capsys, series, "--days", "1")
    line = _line(report, "journey-global-cart-us-east-1-dev")
    assert f"longest 3 min at {replay.when(WINDOW.end - 3)}" in line


def test_pages_of_one_query_are_merged(capsys):
    series = _healthy()
    _fail(series, EAST, "lcl", "orders", range(100, 105))
    _, one_page, _, _ = _replay(capsys, series, "--days", "1")
    _, three_pages, _, _ = _replay(capsys, series, "--days", "1", pages=3)
    assert three_pages == one_page


def test_a_canary_with_no_data_is_refused(capsys):
    series = _healthy()
    series[WEST][alarms.canary_name("global", "cart", ENV)] = {}
    code, report, err, _ = _replay(capsys, series, "--days", "1")
    assert code == replay.EXIT_ERROR and report == ""
    assert "no SuccessPercent datapoints in us-west-2" in err and "global-cart-dev" in err and "check ENV" in err


@pytest.mark.parametrize("days", ["0", "15"])
def test_days_beyond_one_minute_retention_are_refused(capsys, days):
    code, _, err, aws = _replay(capsys, _healthy(), "--days", days)
    assert code == replay.EXIT_ERROR and "DAYS must be 1 to 14" in err and not aws.calls


def test_the_same_region_twice_is_refused(capsys):
    code = main(["replay", "--primary-region", EAST, "--standby-region", EAST, f"--env={ENV}", "--days", "1"],
                aws=FakeAws(_healthy()), now_seconds=NOW)
    assert code == replay.EXIT_ERROR and "two different Regions" in capsys.readouterr().err


def test_the_query_matches_the_alarms_it_replays(capsys):
    _, _, _, aws = _replay(capsys, _healthy(), "--days", "1")
    assert [c[2] for c in aws.calls] == [EAST, WEST]
    for _, _, _, params in aws.calls:
        assert params["start_time"] == replay.iso(WINDOW.start - 2)  # the lead-in for a full first window
        assert params["end_time"] == replay.iso(WINDOW.end)
        assert params["scan_by"] == "TimestampAscending"
        stats = [q["MetricStat"] for q in params["metric_data_queries"]]
        assert {s["Metric"]["Dimensions"][0]["Value"] for s in stats} == {
            alarms.canary_name(v, j, ENV) for v in alarms.VIEWS for j in alarms.JOURNEYS}
        assert {(s["Period"], s["Stat"], s["Metric"]["Namespace"], s["Metric"]["MetricName"]) for s in stats} == {
            (60, "Average", "CloudWatchSynthetics", "SuccessPercent")}


def test_the_window_ends_a_few_minutes_before_now():
    window = replay.make_window(NOW, 14)
    assert window.minutes == 14 * 24 * 60
    assert int(NOW // 60) - window.end >= 2


def test_the_newest_unpublished_minutes_are_not_counted_as_missing(capsys):
    # Canary runs publish their datapoint as they finish, so the last two minutes have none yet.
    now_minute = int(NOW // 60)
    series = {
        region: {
            alarms.canary_name(v, j, ENV): {m: 100.0 for m in range(FIRST - 10, now_minute - 2)}
            for v in alarms.VIEWS for j in alarms.JOURNEYS
        }
        for region in (EAST, WEST)
    }
    code, report, _, _ = _replay(capsys, series, "--days", "1")
    assert code == replay.EXIT_OK
    assert "Journey alarms that went into ALARM (0 of 24)" in report
    assert "Minutes without a datapoint, which the alarms count as failures: 0 across" in report


# --- the make target -------------------------------------------------------------------------

@pytest.mark.parametrize("env", ["-dev", ""])
def test_env_values_parse_in_the_form_the_makefile_passes(env):
    from ngrh_testing.cli import parser

    args = parser().parse_args(["replay", "--primary-region", EAST, "--standby-region", WEST, f"--env={env}"])
    assert args.env == env and args.days == 14

def test_the_make_target_passes_the_makefile_variables():
    text = (DEPLOYMENT / "Makefile").read_text()
    assert re.search(r"^DAYS \?= 14$", text, re.M)
    recipe = re.search(r"^ngrh-alarm-replay:\n((?:\t.*\n)+)", text, re.M).group(1).replace("\\\n\t", " ")
    assert ("python3 -m ngrh_testing replay --primary-region $(PRIMARY_REGION) "
            "--standby-region $(STANDBY_REGION)") in recipe
    # --env=VALUE: ENV values start with a dash, which argparse would read as a flag.
    assert '--env="$(ENV)"' in recipe and "--days $(DAYS)" in recipe
