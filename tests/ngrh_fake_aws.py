"""An in-memory stand-in for ngrh_testing.aws.AwsCli, shared by the tool's tests.

``FakeAws.call(service, operation, region=None, **params)`` answers like the AWS CLI's JSON output for
the handful of services the tool uses: the same operation and response names the real CLI's bundled
service models have (checked against resiliencehubv2 2026-02-17), with just enough behaviour to test
the tool's decisions: Resilience Hub tests and their sources, one test per service and template,
a run that moves through statuses as it is polled, alarms with a state, stacks with outputs.

Every call is recorded in ``calls`` as (service, operation, region, params), so a test can assert what
was written, what was not, and in which order. ``fail_on`` makes the next call to an operation raise
AwsCliError with a message, the way the CLI reports an API error.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from ngrh_testing.aws import AwsCliError

PRIMARY, STANDBY = "us-east-1", "us-west-2"
ACCOUNT = "111111111111"
ENV = "-t"
NGRH_SERVICES = ("ui", "catalog", "cart", "checkout", "orders", "assets")
TEMPLATES = (
    "aws-dependency-validation:rtdep001",
    "aws-multi-region-isolation:rtmr001",
    "aws-multi-region-recovery:rtmr002",
)
BROKER_ID = "b-3d43b3fe-a971-4c26-bab4-5a5cae715f32"
BROKER_HOST = f"{BROKER_ID}.mq.{PRIMARY}.on.aws"

# Operations that change something in AWS: a test asserting "nothing was written" checks for these.
WRITE_OPERATIONS = frozenset({
    "create-test", "update-test", "put-test-sources", "delete-test-sources", "delete-test",
    "start-test-run", "stop-test-run",
})

Call = Tuple[str, str, Optional[str], Dict[str, Any]]


def service_arn(service: str, account: str = ACCOUNT, region: str = PRIMARY) -> str:
    return f"arn:aws:resiliencehub:{region}:{account}:service/{service}-id"


def template_arn(template: str, region: str = PRIMARY) -> str:
    return f"arn:aws:resiliencehub:{region}:aws:test-template/{template}"


def alarm_arn(name: str, region: str = PRIMARY, account: str = ACCOUNT) -> str:
    return f"arn:aws:cloudwatch:{region}:{account}:alarm:{name}"


class FakeAws:
    def __init__(self, env: str = ENV, account: str = ACCOUNT) -> None:
        self.env, self.account = env, account
        self.calls: List[Call] = []
        self._errors: Dict[Tuple[str, str], List[str]] = {}
        self.outputs: Optional[Dict[str, str]] = {
            "ServiceArns": ",".join(service_arn(s, account) for s in NGRH_SERVICES),
            "TestExperimentRoleName": f"ngrh-test-experiment{env}",
            "InvokerRoleName": f"ngrh-invoker{env}",
            **{s.capitalize() + "ServiceArn": service_arn(s, account) for s in NGRH_SERVICES},
        }
        self.templates = {template_arn(t) for t in TEMPLATES}
        self.alarms: Dict[Tuple[str, str], Dict[str, str]] = {}  # (region, name) -> {"state", "type"}
        self.brokers: Dict[str, List[str]] = {BROKER_ID: [f"amqps://{BROKER_HOST}:5671"]}
        self.tests: Dict[str, Dict[str, Any]] = {}  # test id -> the Test structure
        self.sources: Dict[str, List[Tuple[str, str]]] = {}  # test id -> [(kind, alarm ARN)]
        self.runs: Dict[str, Dict[str, Any]] = {}  # run id -> {"serviceArn", "testId", "status", ...}
        self._next = 0
        self._handlers: Dict[Tuple[str, str], Callable[..., Dict[str, Any]]] = {
            ("sts", "get-caller-identity"): self._identity,
            ("cloudformation", "describe-stacks"): self._describe_stacks,
            ("cloudformation", "describe-stack-resource"): self._describe_stack_resource,
            ("mq", "describe-broker"): self._describe_broker,
            ("cloudwatch", "describe-alarms"): self._describe_alarms,
            ("resiliencehubv2", "list-test-templates"): self._list_test_templates,
            ("resiliencehubv2", "list-tests"): self._list_tests,
            ("resiliencehubv2", "get-test"): self._get_test,
            ("resiliencehubv2", "create-test"): self._create_test,
            ("resiliencehubv2", "update-test"): self._update_test,
            ("resiliencehubv2", "delete-test"): self._delete_test,
            ("resiliencehubv2", "list-test-sources"): self._list_test_sources,
            ("resiliencehubv2", "put-test-sources"): self._put_test_sources,
            ("resiliencehubv2", "delete-test-sources"): self._delete_test_sources,
            ("resiliencehubv2", "list-test-runs"): self._list_test_runs,
        }

    # --- test setup -------------------------------------------------------------------------

    def add_alarm(self, name: str, region: str = PRIMARY, state: str = "OK", kind: str = "MetricAlarm") -> None:
        self.alarms[(region, name)] = {"state": state, "type": kind}

    def add_test(self, service: str, template: str, **fields: Any) -> str:
        """Put a test on a service directly (as if an earlier reconcile, or a person, made it)."""
        test_id = self._id("test")
        self.tests[test_id] = {
            "testId": test_id, "serviceArn": service_arn(service, self.account), "testTemplateArn": template_arn(template),
            "name": f"{service}-{test_id}", "totalTestRuns": 0, "successfulTestRuns": 0, "creationTime": 1.0, **fields,
        }
        self.sources[test_id] = []
        return test_id

    def add_run(self, service: str, test_id: str, status: str = "RUNNING") -> str:
        run_id = self._id("run")
        self.runs[run_id] = {"testRunId": run_id, "testId": test_id, "status": status, "startedAt": 1.0,
                             "serviceArn": service_arn(service, self.account), "testTemplateArn": self.tests[test_id]["testTemplateArn"]}
        return run_id

    def fail_on(self, service: str, operation: str, message: str, times: int = 1) -> None:
        self._errors.setdefault((service, operation), []).extend([message] * times)

    # --- what tests assert on ---------------------------------------------------------------

    def calls_of(self, operation: str) -> List[Dict[str, Any]]:
        return [params for _, op, _, params in self.calls if op == operation]

    def writes(self) -> List[Call]:
        return [c for c in self.calls if c[1] in WRITE_OPERATIONS]

    # --- the AwsCli interface ---------------------------------------------------------------

    def call(self, service: str, operation: str, region: Optional[str] = None, **params: Any) -> Dict[str, Any]:
        self.calls.append((service, operation, region, params))
        queued = self._errors.get((service, operation))
        if queued:
            raise AwsCliError(f"aws {service} {operation} in {region} failed: {queued.pop(0)}")
        handler = self._handlers.get((service, operation))
        if handler is None:
            raise AssertionError(f"FakeAws has no handler for {service} {operation}")
        return handler(region, **params)

    def account_id(self) -> str:
        return self.account

    # --- handlers ---------------------------------------------------------------------------

    def _id(self, kind: str) -> str:
        self._next += 1
        return f"{kind}-{self._next:04d}"

    def _error(self, service: str, operation: str, code: str, message: str) -> AwsCliError:
        return AwsCliError(f"aws {service} {operation} failed: An error occurred ({code}) when calling the {operation} operation: {message}")

    def _identity(self, region: Optional[str]) -> Dict[str, Any]:
        return {"Account": self.account, "Arn": f"arn:aws:sts::{self.account}:assumed-role/tester/me", "UserId": "AROA:me"}

    def _describe_stacks(self, region: Optional[str], stack_name: str) -> Dict[str, Any]:
        if stack_name != f"ngrh{self.env}" or self.outputs is None:
            raise AwsCliError(f"aws cloudformation describe-stacks in {region} failed: An error occurred (ValidationError) when calling "
                              f"the DescribeStacks operation: Stack with id {stack_name} does not exist")
        return {"Stacks": [{"StackName": stack_name, "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in self.outputs.items()]}]}

    def _describe_stack_resource(self, region: Optional[str], stack_name: str, logical_resource_id: str) -> Dict[str, Any]:
        if (stack_name, logical_resource_id) != (f"apps{self.env}", "OrdersMqBroker"):
            raise self._error("cloudformation", "describe-stack-resource", "ValidationError", f"Resource {logical_resource_id} does not exist")
        return {"StackResourceDetail": {"LogicalResourceId": logical_resource_id, "PhysicalResourceId": BROKER_ID}}

    def _describe_broker(self, region: Optional[str], broker_id: str) -> Dict[str, Any]:
        if broker_id not in self.brokers:
            raise self._error("mq", "describe-broker", "NotFoundException", "broker not found")
        return {"BrokerId": broker_id, "BrokerInstances": [{"Endpoints": [e]} for e in self.brokers[broker_id]]}

    def _describe_alarms(self, region: Optional[str], alarm_names: List[str], alarm_types: List[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {"MetricAlarms": [], "CompositeAlarms": []}
        for name in alarm_names:
            alarm = self.alarms.get((region, name))
            if alarm and alarm["type"] in alarm_types:
                out[alarm["type"] + "s"].append({"AlarmName": name, "StateValue": alarm["state"]})
        return out

    def _list_test_templates(self, region: Optional[str]) -> Dict[str, Any]:
        return {"testTemplates": [{"testTemplateArn": a, "name": a.rsplit("/", 1)[-1], "description": "d"} for a in sorted(self.templates)]}

    def _list_tests(self, region: Optional[str], service_arn: str) -> Dict[str, Any]:
        if not any(service_arn == a for a in (self.outputs or {}).get("ServiceArns", "").split(",")):
            raise self._error("resiliencehubv2", "list-tests", "ResourceNotFoundException", "Service not found")
        return {"tests": [{k: t[k] for k in ("testId", "testTemplateArn", "serviceArn", "totalTestRuns", "successfulTestRuns", "creationTime")}
                          for t in self.tests.values() if t["serviceArn"] == service_arn]}

    def _get_test(self, region: Optional[str], service_arn: str, test_id: str) -> Dict[str, Any]:
        if test_id not in self.tests:
            raise self._error("resiliencehubv2", "get-test", "ResourceNotFoundException", "Test not found")
        return {"test": dict(self.tests[test_id])}

    def _create_test(self, region: Optional[str], service_arn: str, test_template_arn: str, **settings: Any) -> Dict[str, Any]:
        if test_template_arn not in self.templates:
            raise self._error("resiliencehubv2", "create-test", "ValidationException", "unknown template")
        if any(t["serviceArn"] == service_arn and t["testTemplateArn"] == test_template_arn for t in self.tests.values()):
            raise self._error("resiliencehubv2", "create-test", "ConflictException", "A test already exists for this service and template")
        test_id = self._id("test")
        self.tests[test_id] = {"testId": test_id, "serviceArn": service_arn, "testTemplateArn": test_template_arn,
                               "name": f"test-{test_id}", "totalTestRuns": 0, "successfulTestRuns": 0, "creationTime": 1.0,
                               **{self._camel(k): v for k, v in settings.items()}}
        self.sources[test_id] = []
        return {"test": dict(self.tests[test_id])}

    def _update_test(self, region: Optional[str], service_arn: str, test_id: str, **settings: Any) -> Dict[str, Any]:
        if any(r["testId"] == test_id and r["status"] in ("INITIALIZING", "RUNNING", "STOPPING") for r in self.runs.values()):
            raise self._error("resiliencehubv2", "update-test", "ConflictException", "An active test run already exists for this service.")
        self.tests[test_id].update({self._camel(k): v for k, v in settings.items()})
        return {"test": dict(self.tests[test_id])}

    def _delete_test(self, region: Optional[str], service_arn: str, test_id: str) -> Dict[str, Any]:
        if test_id not in self.tests:
            raise self._error("resiliencehubv2", "delete-test", "ResourceNotFoundException", "Test not found")
        del self.tests[test_id]
        self.sources.pop(test_id, None)
        return {"testId": test_id}

    def _list_test_sources(self, region: Optional[str], service_arn: str, test_id: str) -> Dict[str, Any]:
        key = {"SUCCESS_CRITERIA": "successCriteriaAlarm", "OBSERVABILITY": "observabilityAlarm"}
        return {"testSources": [{key[kind]: {"alarmArn": arn, "alarmName": arn.rsplit(":", 1)[-1]}} for kind, arn in self.sources[test_id]]}

    @staticmethod
    def _source_pairs(test_sources: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
        pairs = []
        for source in test_sources:
            if "successCriteriaAlarm" in source:
                pairs.append(("SUCCESS_CRITERIA", source["successCriteriaAlarm"]["alarmArn"]))
            if "observabilityAlarm" in source:
                pairs.append(("OBSERVABILITY", source["observabilityAlarm"]["alarmArn"]))
        return pairs

    def _put_test_sources(self, region: Optional[str], service_arn: str, test_id: str, test_sources: List[Dict[str, Any]]) -> Dict[str, Any]:
        held = self.sources[test_id]
        for pair in self._source_pairs(test_sources):
            if pair in held:
                raise self._error("resiliencehubv2", "put-test-sources", "ConflictException", "source already present")
            held.append(pair)
        if len(held) > 5:
            raise self._error("resiliencehubv2", "put-test-sources", "ServiceQuotaExceededException", "at most five sources")
        return {}

    def _delete_test_sources(self, region: Optional[str], service_arn: str, test_id: str, test_sources: List[Dict[str, Any]]) -> Dict[str, Any]:
        for pair in self._source_pairs(test_sources):
            self.sources[test_id].remove(pair)
        return {}

    def _list_test_runs(self, region: Optional[str], service_arn: str, **_: Any) -> Dict[str, Any]:
        return {"testRuns": [dict(r) for r in self.runs.values() if r["serviceArn"] == service_arn]}

    @staticmethod
    def _camel(name: str) -> str:
        head, *rest = name.split("_")
        return head + "".join(p.capitalize() for p in rest)

