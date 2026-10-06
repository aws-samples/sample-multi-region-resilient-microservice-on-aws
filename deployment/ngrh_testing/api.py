# SPDX-License-Identifier: MIT-0
"""The AWS calls the commands share, one function each, so the operation and response names appear
once. Resilience Hub calls go to the NGRH Region; the CLI layer merges paginated responses, so a list
function returns every item."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .aws import AwsCli
from .context import SOURCE_OBSERVABILITY, SOURCE_SUCCESS

SERVICE = "resiliencehubv2"

# TestRun statuses (GetTestRun): a run is active until it reaches one of the terminal ones.
ACTIVE_STATUSES = ("INITIALIZING", "RUNNING", "STOPPING")
TERMINAL_STATUSES = ("PASSED", "FAILED", "STOPPED", "ERROR")

ALARM_TYPES = ["MetricAlarm", "CompositeAlarm"]  # describe-alarms lists metric alarms only unless asked
DESCRIBE_ALARMS_LIMIT = 100  # names per call


def describe_alarms(aws: AwsCli, region: str, names: Sequence[str]) -> Dict[str, Dict[str, str]]:
    """The named alarms that exist in the Region, as {name: {"state": ..., "type": ...}}; the
    others are absent from the result."""
    found: Dict[str, Dict[str, str]] = {}
    names = list(names)
    for start in range(0, len(names), DESCRIBE_ALARMS_LIMIT):
        page = aws.call("cloudwatch", "describe-alarms", region, alarm_names=names[start:start + DESCRIBE_ALARMS_LIMIT],
                        alarm_types=ALARM_TYPES)
        for kind in ALARM_TYPES:
            for alarm in page.get(kind + "s", []):
                found[alarm["AlarmName"]] = {"state": alarm["StateValue"], "type": kind}
    return found


def list_test_templates(aws: AwsCli, region: str) -> Set[str]:
    return {t["testTemplateArn"] for t in aws.call(SERVICE, "list-test-templates", region).get("testTemplates", [])}


def list_tests(aws: AwsCli, region: str, service_arn: str) -> List[Dict[str, Any]]:
    return aws.call(SERVICE, "list-tests", region, service_arn=service_arn).get("tests", [])


def get_test(aws: AwsCli, region: str, service_arn: str, test_id: str) -> Dict[str, Any]:
    return aws.call(SERVICE, "get-test", region, service_arn=service_arn, test_id=test_id)["test"]


def list_sources(aws: AwsCli, region: str, service_arn: str, test_id: str) -> Set[Tuple[str, str]]:
    """The test's sources as (kind, alarm ARN) pairs."""
    out: Set[Tuple[str, str]] = set()
    for source in aws.call(SERVICE, "list-test-sources", region, service_arn=service_arn, test_id=test_id).get("testSources", []):
        if "successCriteriaAlarm" in source:
            out.add((SOURCE_SUCCESS, source["successCriteriaAlarm"]["alarmArn"]))
        if "observabilityAlarm" in source:
            out.add((SOURCE_OBSERVABILITY, source["observabilityAlarm"]["alarmArn"]))
    return out


def source_inputs(sources: Sequence[Tuple[str, str]]) -> List[Dict[str, Any]]:
    """(kind, alarm ARN) pairs in the shape PutTestSources and DeleteTestSources take."""
    key = {SOURCE_SUCCESS: "successCriteriaAlarm", SOURCE_OBSERVABILITY: "observabilityAlarm"}
    return [{key[kind]: {"alarmArn": arn}} for kind, arn in sorted(sources)]


def list_test_runs(aws: AwsCli, region: str, service_arn: str, test_id: Optional[str] = None) -> List[Dict[str, Any]]:
    params = {"test_id": test_id} if test_id else {}
    return aws.call(SERVICE, "list-test-runs", region, service_arn=service_arn, **params).get("testRuns", [])


def active_runs(aws: AwsCli, region: str, service_arns: Sequence[str]) -> List[Tuple[str, Dict[str, Any]]]:
    """(service ARN, run) for every run of these services that has not reached a terminal status."""
    found = []
    for arn in service_arns:
        found.extend((arn, run) for run in list_test_runs(aws, region, arn) if run["status"] in ACTIVE_STATUSES)
    return found


def list_service_arns(aws: AwsCli, region: str) -> List[str]:
    """Every NGRH service in the account, the sample's and other people's."""
    return [s["serviceArn"] for s in aws.call(SERVICE, "list-services", region).get("serviceSummaries", [])]
