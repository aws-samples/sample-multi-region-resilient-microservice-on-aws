# SPDX-License-Identifier: MIT-0
"""The AWS calls the commands share, one function each, so the operation and response names appear
once. Resilience Hub calls go to the NGRH Region; the CLI layer merges paginated responses, so a list
function returns every item."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .aws import AwsCli, AwsCliError
from .context import SOURCE_OBSERVABILITY, SOURCE_SUCCESS

SERVICE = "resiliencehubv2"

# TestRun statuses (GetTestRun): a run is active until it reaches one of the terminal ones.
ACTIVE_STATUSES = ("INITIALIZING", "RUNNING", "STOPPING")
TERMINAL_STATUSES = ("PASSED", "FAILED", "STOPPED", "ERROR")

# ARC Region Switch execution states (ListPlanExecutions, GetPlanExecution): a plan still working, and one that is done.
RUNNING_PLAN = ("inProgress", "pausedByFailedStep", "pausedByOperator", "pendingManualApproval", "pending")
FINISHED_PLAN = ("completed", "completedWithExceptions", "completedMonitoringApplicationHealth")

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


def service_tag_scope(aws: AwsCli, region: str, service_arn: str) -> List[Tuple[str, Set[str]]]:
    """The tag filters of the service's TAGS input sources, as (tag key, accepted values); empty when
    no tag scopes the service. They decide which resources, alarms included, Resilience Hub discovers
    for the service."""
    scope: List[Tuple[str, Set[str]]] = []
    for source in aws.call(SERVICE, "list-input-sources", region, service_arn=service_arn).get("inputSourceSummaries", []):
        if source.get("type") == "TAGS":
            scope.extend((tag["key"], set(tag["values"])) for tag in source.get("resourceTags", []))
    return scope


def alarm_tags(aws: AwsCli, region: str, alarm_arn: str) -> Dict[str, str]:
    """The tags on a CloudWatch alarm."""
    tags = aws.call("cloudwatch", "list-tags-for-resource", region, resource_arn=alarm_arn).get("Tags", [])
    return {tag["Key"]: tag["Value"] for tag in tags}


def ecs_cluster(aws: AwsCli, region: str, env_suffix: str) -> str:
    """The apps stack's ECS cluster in a Region: its physical name, which ECS and Application Auto Scaling accept."""
    return aws.call("cloudformation", "describe-stack-resource", region, stack_name=f"apps{env_suffix}",
                    logical_resource_id="EcsCluster")["StackResourceDetail"]["PhysicalResourceId"]


def plan_executions(aws: AwsCli, plan_arn: str, regions: Sequence[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Every execution of the plan, merged from the endpoint of each Region and oldest first, and one line for
    each endpoint that could not be read. ARC lists an execution at both endpoints, so the merge is by id."""
    executions: Dict[str, Dict[str, Any]] = {}
    problems: List[str] = []
    for region in regions:
        try:
            items = aws.call("arc-region-switch", "list-plan-executions", region, plan_arn=plan_arn).get("items", [])
        except AwsCliError as e:
            problems.append(f"could not list plan executions at the {region} endpoint: {e}")
            continue
        executions.update({e["executionId"]: e for e in items})
    return sorted(executions.values(), key=lambda e: str(e["startTime"])), problems


def route53_health_checks(aws: AwsCli, plan_arn: str, region: str) -> Dict[str, List[str]]:
    """The statuses (healthy, unhealthy or unknown) of the Route 53 health checks the plan vends, by the Region
    each one stands for. DNS answers with a Region only while its checks are healthy."""
    found: Dict[str, List[str]] = {}
    for check in aws.call("arc-region-switch", "list-route53-health-checks", region, arn=plan_arn).get("healthChecks", []):
        found.setdefault(check["region"], []).append(check["status"])
    return found


def alarm_state_updates(aws: AwsCli, region: str, name: str, start: str, end: str) -> List[Dict[str, Any]]:
    """The alarm's state changes between two ISO-8601 times, as CloudWatch's history items. describe-alarm-history
    lists metric alarms only unless composite alarms are asked for too."""
    return aws.call("cloudwatch", "describe-alarm-history", region, alarm_name=name, alarm_types=ALARM_TYPES,
                    history_item_type="StateUpdate", start_date=start, end_date=end).get("AlarmHistoryItems", [])
