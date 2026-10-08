"""An in-memory stand-in for ngrh_testing.aws.AwsCli, shared by the tool's tests.

``FakeAws.call(service, operation, region=None, **params)`` answers like the AWS CLI's JSON output for
the services the tool uses: the same operation and response names the real CLI's bundled service models
have (checked against resiliencehubv2 2026-02-17, arc-region-switch 2022-07-26 and fis 2020-12-01),
with just enough behaviour to test the tool's decisions: Resilience Hub tests and their sources, one test
per service and template, a run that moves through statuses as it is polled, alarms with a state, stacks
with outputs, ECS services and task definitions, FIS experiments, ARC plan executions (listed, and started and
polled), the plan itself (its triggers and scaling blocks), the plan's Route 53 health checks, Application Auto
Scaling targets, Container Insights task counts and the catalog Aurora global cluster with its switchover.

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
BROKER_ID = "b-11111111-2222-4333-8444-555555555555"
BROKER_HOST = f"{BROKER_ID}.mq.{PRIMARY}.on.aws"
PLAN_ARN = f"arn:aws:arc-region-switch::{ACCOUNT}:plan/mr-rs-plan{ENV}:abc123"
CLUSTER = f"apps{ENV}-EcsCluster-AbCdEfGh1234"

# Operations that change something in AWS: a test asserting "nothing was written" checks for these.
WRITE_OPERATIONS = frozenset({
    "create-test", "update-test", "put-test-sources", "delete-test-sources", "delete-test",
    "start-test-run", "stop-test-run",
    "start-plan-execution", "update-service", "register-scalable-target", "switchover-global-cluster",
})

TERMINAL = ("PASSED", "FAILED", "STOPPED", "ERROR")
Call = Tuple[str, str, Optional[str], Dict[str, Any]]


def service_arn(service: str, account: str = ACCOUNT, region: str = PRIMARY) -> str:
    return f"arn:aws:resiliencehub:{region}:{account}:service/{service}-id"


def template_arn(template: str, region: str = PRIMARY) -> str:
    return f"arn:aws:resiliencehub:{region}:aws:test-template/{template}"


def alarm_arn(name: str, region: str = PRIMARY, account: str = ACCOUNT) -> str:
    return f"arn:aws:cloudwatch:{region}:{account}:alarm:{name}"


def task_definition_arn(service: str, region: str = PRIMARY, revision: int = 7) -> str:
    return f"arn:aws:ecs:{region}:{ACCOUNT}:task-definition/apps{ENV}-{service}:{revision}"


def db_cluster_arn(region: str, env: str = ENV, account: str = ACCOUNT) -> str:
    """The catalog Aurora cluster of a Region, as databases.yaml names it."""
    return f"arn:aws:rds:{region}:{account}:cluster:catalog-dbcluster-{'01' if region == PRIMARY else '02'}-{region}{env}"


def catalog_cluster_id(region: str, env: str = ENV) -> str:
    """The identifier of the catalog Aurora cluster of a Region (the physical id of the DBCluster in catalog-db-stack)."""
    return f"catalog-dbcluster-{'01' if region == PRIMARY else '02'}-{region}{env}"


def catalog_endpoint(region: str, env: str = ENV, reader: bool = False) -> str:
    return f"{catalog_cluster_id(region, env)}.cluster-{'ro-' if reader else ''}abcdefgh.{region}.rds.amazonaws.com"


def default_triggers(env_regions: Tuple[str, str] = (PRIMARY, STANDBY), delay: int = 60) -> List[Dict[str, Any]]:
    """The plan's eight triggers as GetPlan returns them (failover.yaml): for each Region A, its peer B and journey J,
    deactivate A when J fails in A, B confirms it, and B is healthy."""
    out = []
    for a, b, role_a, role_b in ((env_regions[0], env_regions[1], "primary", "standby"), (env_regions[1], env_regions[0], "standby", "primary")):
        for journey in ("home", "cart", "catalog", "orders"):
            out.append({"targetRegion": a, "action": "deactivate", "minDelayMinutesBetweenExecutions": delay,
                        "description": f"Deactivate {a}: {journey}",
                        "conditions": [{"associatedAlarmName": f"journey-lcl-{journey}-{role_a}", "condition": "red"},
                                       {"associatedAlarmName": f"journey-rmt-{journey}-{role_b}", "condition": "red"},
                                       {"associatedAlarmName": f"region-degraded-{role_b}", "condition": "green"}]})
    return out


def default_alarm_service_tag(name: str) -> str:
    """The ``service`` tag monitoring.yml gives an alarm: the service a hop alarm watches, orders for
    orders-created-zero, and otherwise the monitoring stack's own tag, shared."""
    if name.startswith("hop-"):
        service = name.split("-")[1]           # hop-<service>-<errors|slow>-<Region><Env>
        return "cart" if service == "carts" else service
    if name.startswith("orders-created-zero"):
        return "orders"
    return "shared"


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
        self.stacks: Dict[Tuple[str, str], Dict[str, str]] = {(PRIMARY, f"region-switch{env}"): {"RegionSwitchPlanArn": PLAN_ARN}}
        self.templates = {template_arn(t) for t in TEMPLATES}
        self.alarms: Dict[Tuple[str, str], Dict[str, str]] = {}  # (region, name) -> {"state", "type"}
        self.alarm_tags: Dict[Tuple[str, str], Dict[str, str]] = {}  # (region, name) -> the alarm's tags
        self.service_scopes: Dict[str, List[Dict[str, Any]]] = {}  # service ARN -> input sources, where not the default
        self.alarm_history: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}  # (region, name) -> history items
        self.brokers: Dict[str, List[str]] = {BROKER_ID: [f"amqps://{BROKER_HOST}:5671"]}
        self.tests: Dict[str, Dict[str, Any]] = {}  # test id -> the Test structure
        self.sources: Dict[str, List[Tuple[str, str]]] = {}  # test id -> [(kind, alarm ARN)]
        self.runs: Dict[str, Dict[str, Any]] = {}  # run id -> the run (see add_run and _start_test_run)
        self.run_script: List[str] = ["INITIALIZING", "RUNNING", "RUNNING", "FAILED"]  # statuses successive polls see
        self.stop_script: List[str] = ["STOPPED"]  # what polls of a run that was asked to stop see
        self.run_events: List[Dict[str, Any]] = []
        self.run_source_events: Dict[str, List[Dict[str, Any]]] = {}
        self.resolved_targets: List[Dict[str, Any]] = []
        self.dependencies: List[Dict[str, Any]] = []
        self.run_error: Optional[str] = None
        self.role_policies = {f"ngrh-invoker{env}": ["AWSResilienceHubV2AssessmentExecutionPolicy", "AWSResilienceHubResilienceTestingPolicy"]}
        self.roles = {f"ngrh-test-experiment{env}", f"ngrh-invoker{env}"}
        self.fis: Dict[str, List[Dict[str, Any]]] = {PRIMARY: [], STANDBY: []}  # Region -> experiment summaries
        self.plan_executions: Dict[str, List[Dict[str, Any]]] = {PRIMARY: [], STANDBY: []}  # Region endpoint -> executions
        self.health_checks: Dict[str, str] = {PRIMARY: "healthy", STANDBY: "healthy"}  # ARC's Route 53 health check status by Region
        self.execution_script: List[str] = ["inProgress", "completed"]  # the states successive polls of a started execution see
        self.started_executions: Dict[str, Dict[str, Any]] = {}  # execution id -> what start-plan-execution was asked
        self.scalable_targets: Dict[Tuple[str, str], Dict[str, Any]] = {}  # (region, resource id) -> Application Auto Scaling target
        self.plan_triggers: List[Dict[str, Any]] = default_triggers()  # what GetPlan returns; [] is a plan deployed with the switch off
        self.plan_target_percent: Optional[int] = 200                   # TargetPercent of every ECS scaling block; None leaves it out (ARC's default is 100)
        self.task_peaks: Dict[Tuple[str, str], float] = {}              # (region, service name) -> most tasks in 24 h (Container Insights)
        self.global_cluster_status = "available"
        self.global_writer: Optional[str] = PRIMARY  # the Region of the catalog global cluster's writer
        self.global_members: Dict[str, str] = {PRIMARY: "available", STANDBY: "available"}  # Region -> its cluster's status
        self.switchover_script: List[str] = ["switching-over", "available"]  # global cluster statuses successive polls see after a switchover
        self._switchover: Optional[Dict[str, Any]] = None
        self._global_polls = 0
        self._global_changes: List[Tuple[int, Callable[[], None]]] = []
        self.ecs_services: Dict[Tuple[str, str], Dict[str, Any]] = {}  # (region, service name) -> service
        self.ecs_tasks: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}  # (region, service name) -> running tasks
        self.ssm_instances: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}  # (region, task ARN) -> instances tagged with it
        self.task_definitions: Dict[str, Dict[str, Any]] = {}
        self._next = 0
        self._handlers: Dict[Tuple[str, str], Callable[..., Dict[str, Any]]] = {
            ("sts", "get-caller-identity"): self._identity,
            ("cloudformation", "describe-stacks"): self._describe_stacks,
            ("cloudformation", "describe-stack-resource"): self._describe_stack_resource,
            ("mq", "describe-broker"): self._describe_broker,
            ("cloudwatch", "describe-alarms"): self._describe_alarms,
            ("cloudwatch", "describe-alarm-history"): self._describe_alarm_history,
            ("cloudwatch", "list-tags-for-resource"): self._list_tags_for_resource,
            ("iam", "list-attached-role-policies"): self._list_attached_role_policies,
            ("iam", "get-role"): self._get_role,
            ("fis", "list-experiments"): self._list_experiments,
            ("fis", "get-experiment"): self._get_experiment,
            ("arc-region-switch", "list-plan-executions"): self._list_plan_executions,
            ("arc-region-switch", "list-route53-health-checks"): self._list_route53_health_checks,
            ("arc-region-switch", "start-plan-execution"): self._start_plan_execution,
            ("arc-region-switch", "get-plan-execution"): self._get_plan_execution,
            ("arc-region-switch", "get-plan"): self._get_plan,
            ("cloudwatch", "get-metric-statistics"): self._get_metric_statistics,
            ("ecs", "update-service"): self._update_service,
            ("application-autoscaling", "describe-scalable-targets"): self._describe_scalable_targets,
            ("application-autoscaling", "register-scalable-target"): self._register_scalable_target,
            ("rds", "describe-global-clusters"): self._describe_global_clusters,
            ("rds", "describe-db-clusters"): self._describe_db_clusters,
            ("rds", "switchover-global-cluster"): self._switchover_global_cluster,
            ("ecs", "describe-services"): self._describe_services,
            ("ecs", "list-tasks"): self._list_tasks,
            ("ecs", "describe-tasks"): self._describe_tasks,
            ("ssm", "describe-instance-information"): self._describe_instance_information,
            ("ecs", "describe-task-definition"): self._describe_task_definition,
            ("resiliencehubv2", "list-test-templates"): self._list_test_templates,
            ("resiliencehubv2", "list-services"): self._list_services,
            ("resiliencehubv2", "list-input-sources"): self._list_input_sources,
            ("resiliencehubv2", "list-tests"): self._list_tests,
            ("resiliencehubv2", "get-test"): self._get_test,
            ("resiliencehubv2", "create-test"): self._create_test,
            ("resiliencehubv2", "update-test"): self._update_test,
            ("resiliencehubv2", "delete-test"): self._delete_test,
            ("resiliencehubv2", "list-test-sources"): self._list_test_sources,
            ("resiliencehubv2", "put-test-sources"): self._put_test_sources,
            ("resiliencehubv2", "delete-test-sources"): self._delete_test_sources,
            ("resiliencehubv2", "list-test-runs"): self._list_test_runs,
            ("resiliencehubv2", "start-test-run"): self._start_test_run,
            ("resiliencehubv2", "get-test-run"): self._get_test_run,
            ("resiliencehubv2", "stop-test-run"): self._stop_test_run,
            ("resiliencehubv2", "list-test-run-events"): self._list_test_run_events,
            ("resiliencehubv2", "list-test-run-sources"): self._list_test_run_sources,
            ("resiliencehubv2", "list-test-run-source-events"): self._list_test_run_source_events,
            ("resiliencehubv2", "list-resolved-test-run-target-resources"): self._list_resolved_targets,
            ("resiliencehubv2", "list-test-run-dependencies"): self._list_dependencies,
        }

    # --- test setup -------------------------------------------------------------------------

    def add_alarm(self, name: str, region: str = PRIMARY, state: str = "OK", kind: str = "MetricAlarm",
                  service_tag: Optional[str] = None) -> None:
        """An alarm that exists, tagged as monitoring.yml would tag it unless ``service_tag`` says otherwise."""
        self.alarms[(region, name)] = {"state": state, "type": kind}
        self.alarm_tags[(region, name)] = {"service": service_tag or default_alarm_service_tag(name)}

    def add_test(self, service: str, template: str, **fields: Any) -> str:
        """Put a test on a service directly (as if an earlier reconcile, or a person, made it)."""
        test_id = self._id("test")
        self.tests[test_id] = {
            "testId": test_id, "serviceArn": service_arn(service, self.account), "testTemplateArn": template_arn(template),
            "name": f"{service}-{test_id}", "totalTestRuns": 0, "successfulTestRuns": 0, "creationTime": 1.0, **fields,
        }
        self.sources[test_id] = []
        return test_id

    def add_run(self, service: str, test_id: str, status: str = "RUNNING", started: str = "2026-10-06T16:00:00+00:00") -> str:
        """A run that is already in some status and stays there."""
        run_id = self._id("run")
        self.runs[run_id] = {"testRunId": run_id, "testId": test_id, "status": status, "startedAt": started,
                             "serviceArn": service_arn(service, self.account), "testTemplateArn": self.tests[test_id]["testTemplateArn"],
                             "script": [status], "polls": 0}
        return run_id

    def add_ecs_service(self, service: str, region: str = PRIMARY, sidecar: bool = True, pid_mode: Optional[str] = "task",
                        fault_injection: bool = True, providers: Tuple[str, ...] = ("FARGATE",), exec_on: bool = False,
                        status: str = "ACTIVE", tasks: int = 2) -> None:
        """An ECS service running a task definition that is (or is not) ready for the fault, with ``tasks``
        running tasks, each registered with SSM when the task definition has the sidecar."""
        name = ("carts" if service == "cart" else service) + self.env
        arn = task_definition_arn(name, region)
        containers = [{"name": service}] + ([{"name": "amazon-ssm-agent"}] if sidecar else [])
        self.task_definitions[arn] = {"family": f"apps{self.env}-{name}", "revision": 7, "taskDefinitionArn": arn,
                                      "containerDefinitions": containers, "enableFaultInjection": fault_injection,
                                      **({"pidMode": pid_mode} if pid_mode else {})}
        self.ecs_services[(region, name)] = {"serviceName": name, "status": status, "taskDefinition": arn,
                                             "capacityProviderStrategy": [{"capacityProvider": p, "weight": 1} for p in providers],
                                             "enableExecuteCommand": exec_on, "desiredCount": tasks}
        self.task_peaks[(region, name)] = float(tasks)    # Container Insights: the most tasks it ran lately
        resource_id = f"service/{CLUSTER}/{name}"       # ecs.yaml: minimum 2 and maximum 10 for every service
        self.scalable_targets[(region, resource_id)] = {"ServiceNamespace": "ecs", "ResourceId": resource_id,
                                                        "ScalableDimension": "ecs:service:DesiredCount", "MinCapacity": 2, "MaxCapacity": 10}
        for old in self.ecs_tasks.get((region, name), []):   # replacing a service replaces its tasks and what they registered
            self.ssm_instances.pop((region, old["taskArn"]), None)
        self.ecs_tasks[(region, name)] = []
        for number in range(tasks):
            self.add_task(service, region, sidecar_running=sidecar, registered=sidecar, number=number)

    def add_task(self, service: str, region: str = PRIMARY, sidecar_running: bool = True, registered: bool = True,
                 number: int = 0, ping: str = "Online", last_status: str = "RUNNING") -> str:
        """A task of an ECS service. By default its sidecar is running and an Online SSM managed instance is
        registered for it, tagged with the task's ARN as the real sidecar tags it. Returns the task ARN."""
        name = ("carts" if service == "cart" else service) + self.env
        task_id = f"{name}{number:02d}".ljust(32, "0")[:32]
        task_arn = f"arn:aws:ecs:{region}:{ACCOUNT}:task/{CLUSTER}/{task_id}"
        containers = [{"name": service, "lastStatus": "RUNNING"}]
        if (region, name) in self.ecs_services and any(c["name"] == "amazon-ssm-agent"
                                                       for c in self.task_definitions[self.ecs_services[(region, name)]["taskDefinition"]]["containerDefinitions"]):
            containers.append({"name": "amazon-ssm-agent", "lastStatus": "RUNNING" if sidecar_running else "STOPPED",
                               **({} if sidecar_running else {"exitCode": 127})})
        self.ecs_tasks.setdefault((region, name), []).append({"taskArn": task_arn, "lastStatus": last_status, "containers": containers})
        self.ssm_instances.pop((region, task_arn), None)
        if registered:
            self.ssm_instances.setdefault((region, task_arn), []).append({"InstanceId": f"mi-{task_id[:17]}", "PingStatus": ping})
        return task_arn

    def at_global_poll(self, polls: int, change: Callable[[], None]) -> None:
        """Run ``change`` on the ``polls``-th describe-global-clusters call: the old primary rejoining, or becoming available."""
        self._global_changes.append((polls, change))

    def fail_on(self, service: str, operation: str, message: str, times: int = 1) -> None:
        self._errors.setdefault((service, operation), []).extend([message] * times)

    # --- what tests assert on ---------------------------------------------------------------

    def calls_of(self, operation: str) -> List[Dict[str, Any]]:
        return [params for _, op, _, params in self.calls if op == operation]

    def writes(self) -> List[Call]:
        return [c for c in self.calls if c[1] in WRITE_OPERATIONS]

    # --- the AwsCli interface ---------------------------------------------------------------

    def call(self, service: str, operation: str, region: Optional[str] = None, /, **params: Any) -> Dict[str, Any]:
        self.calls.append((service, operation, region, params))
        queued = self._errors.get((service, operation))
        if queued:
            raise AwsCliError(f"aws {service} {operation} in {region} failed: {queued.pop(0)}")
        handler = self._handlers.get((service, operation))
        if handler is None:
            raise AssertionError(f"FakeAws has no handler for {service} {operation}")
        return handler(region, **params)

    def account_id(self) -> str:
        return self.call("sts", "get-caller-identity")["Account"]

    # --- handlers ---------------------------------------------------------------------------

    def _id(self, kind: str) -> str:
        self._next += 1
        return f"{kind}-{self._next:04d}"

    def _error(self, service: str, operation: str, code: str, message: str) -> AwsCliError:
        return AwsCliError(f"aws {service} {operation} failed: An error occurred ({code}) when calling the {operation} operation: {message}")

    def _identity(self, region: Optional[str]) -> Dict[str, Any]:
        return {"Account": self.account, "Arn": f"arn:aws:sts::{self.account}:assumed-role/tester/me", "UserId": "AROA:me"}

    def _describe_stacks(self, region: Optional[str], stack_name: str) -> Dict[str, Any]:
        if stack_name == f"ngrh{self.env}" and self.outputs is not None:
            outputs = self.outputs
        elif (region, stack_name) in self.stacks:
            outputs = self.stacks[(region, stack_name)]
        else:
            raise AwsCliError(f"aws cloudformation describe-stacks in {region} failed: An error occurred (ValidationError) when calling "
                              f"the DescribeStacks operation: Stack with id {stack_name} does not exist")
        return {"Stacks": [{"StackName": stack_name, "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()]}]}

    def _describe_stack_resource(self, region: Optional[str], stack_name: str, logical_resource_id: str) -> Dict[str, Any]:
        physical = {(f"apps{self.env}", "OrdersMqBroker"): BROKER_ID, (f"apps{self.env}", "EcsCluster"): CLUSTER,
                    (f"catalog-db-stack{self.env}", "DBCluster"): catalog_cluster_id(region or PRIMARY, self.env)}.get((stack_name, logical_resource_id))
        if physical is None:
            raise self._error("cloudformation", "describe-stack-resource", "ValidationError", f"Resource {logical_resource_id} does not exist")
        return {"StackResourceDetail": {"LogicalResourceId": logical_resource_id, "PhysicalResourceId": physical}}

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

    def _describe_alarm_history(self, region: Optional[str], alarm_name: str, alarm_types: Optional[List[str]] = None, **_: Any) -> Dict[str, Any]:
        alarm = self.alarms.get((region, alarm_name))
        if alarm is not None and alarm["type"] not in (alarm_types or ["MetricAlarm"]):
            return {"AlarmHistoryItems": []}  # the real API lists metric alarms only unless composites are asked for
        return {"AlarmHistoryItems": list(self.alarm_history.get((region, alarm_name), []))}

    def _list_tags_for_resource(self, region: Optional[str], resource_arn: str) -> Dict[str, Any]:
        name = resource_arn.rsplit(":alarm:", 1)[-1]
        if (region, name) not in self.alarms:
            raise self._error("cloudwatch", "list-tags-for-resource", "ResourceNotFoundException", f"alarm {name} not found")
        return {"Tags": [{"Key": k, "Value": v} for k, v in sorted(self.alarm_tags.get((region, name), {}).items())]}

    def _list_input_sources(self, region: Optional[str], service_arn: str) -> Dict[str, Any]:
        """A service discovers by the tag ``service`` in its own name and ``shared`` (ngrh.yaml), next to
        the design file every service in the account has."""
        if service_arn in self.service_scopes:
            return {"inputSourceSummaries": self.service_scopes[service_arn]}
        name = service_arn.rsplit("/", 1)[-1].rsplit("-id", 1)[0]
        return {"inputSourceSummaries": [
            {"inputSourceId": "design", "type": "DESIGN_FILE", "designFileS3Url": "s3://bucket/design/hld.md"},
            {"inputSourceId": "tags", "type": "TAGS", "resourceTags": [{"key": "service", "values": [name, "shared"]}]},
        ]}

    def _list_attached_role_policies(self, region: Optional[str], role_name: str) -> Dict[str, Any]:
        if role_name not in self.role_policies:
            raise self._error("iam", "list-attached-role-policies", "NoSuchEntity", f"The role with name {role_name} cannot be found.")
        return {"AttachedPolicies": [{"PolicyName": p} for p in self.role_policies[role_name]]}

    def _get_role(self, region: Optional[str], role_name: str) -> Dict[str, Any]:
        if role_name not in self.roles:
            raise self._error("iam", "get-role", "NoSuchEntity", f"The role with name {role_name} cannot be found.")
        return {"Role": {"RoleName": role_name}}

    def _list_experiments(self, region: Optional[str]) -> Dict[str, Any]:
        return {"experiments": list(self.fis.get(region or "", []))}

    def _get_experiment(self, region: Optional[str], id: str) -> Dict[str, Any]:  # noqa: A002 (the CLI's own name)
        for experiment in self.fis.get(region or "", []):
            if experiment["id"] == id:
                return {"experiment": {**experiment, "actions": {}, "targets": {}, "stopConditions": []}}
        raise self._error("fis", "get-experiment", "ResourceNotFoundException", "experiment not found")

    def _list_plan_executions(self, region: Optional[str], plan_arn: str) -> Dict[str, Any]:
        return {"items": list(self.plan_executions.get(region or "", []))}

    def _describe_services(self, region: Optional[str], cluster: str, services: List[str]) -> Dict[str, Any]:
        return {"services": [self.ecs_services[(region, s)] for s in services if (region, s) in self.ecs_services], "failures": []}

    def _describe_task_definition(self, region: Optional[str], task_definition: str) -> Dict[str, Any]:
        return {"taskDefinition": self.task_definitions[task_definition]}

    def _list_tasks(self, region: Optional[str], cluster: str, service_name: str, desired_status: str = "RUNNING") -> Dict[str, Any]:
        assert desired_status == "RUNNING", "the tool asks for running tasks only"
        return {"taskArns": [t["taskArn"] for t in self.ecs_tasks.get((region, service_name), [])]}

    def _describe_tasks(self, region: Optional[str], cluster: str, tasks: List[str]) -> Dict[str, Any]:
        known = {t["taskArn"]: t for ts in self.ecs_tasks.values() for t in ts}
        return {"tasks": [known[a] for a in tasks if a in known], "failures": []}

    def _describe_instance_information(self, region: Optional[str], filters: List[Dict[str, Any]]) -> Dict[str, Any]:
        (only,) = filters   # the tool asks for one task's instances at a time, by the tag FIS uses
        assert only["Key"] == "tag:ECS_TASK_ARN", only
        return {"InstanceInformationList": [i for arn in only["Values"] for i in self.ssm_instances.get((region, arn), [])]}

    def _list_test_templates(self, region: Optional[str]) -> Dict[str, Any]:
        return {"testTemplates": [{"testTemplateArn": a, "name": a.rsplit("/", 1)[-1], "description": "d"} for a in sorted(self.templates)]}

    def _list_services(self, region: Optional[str]) -> Dict[str, Any]:
        arns = [a for a in (self.outputs or {}).get("ServiceArns", "").split(",") if a]
        arns += sorted({r["serviceArn"] for r in self.runs.values()} - set(arns))      # someone else's services
        return {"serviceSummaries": [{"serviceArn": a, "name": a.rsplit("/", 1)[-1]} for a in arns]}

    def _known_service(self, service_arn: str) -> bool:
        return service_arn in (self.outputs or {}).get("ServiceArns", "").split(",") or any(r["serviceArn"] == service_arn for r in self.runs.values())

    def _list_tests(self, region: Optional[str], service_arn: str) -> Dict[str, Any]:
        if not self._known_service(service_arn):
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

    # --- runs ----------------------------------------------------------------------------------

    def _list_test_runs(self, region: Optional[str], service_arn: str, test_id: Optional[str] = None) -> Dict[str, Any]:
        if not self._known_service(service_arn):
            raise self._error("resiliencehubv2", "list-test-runs", "ResourceNotFoundException", "Service not found")
        keys = ("testRunId", "status", "startedAt", "endedAt", "testTemplateArn", "serviceArn", "errorMessage")
        return {"testRuns": [{k: r[k] for k in keys if k in r} for r in self.runs.values()
                             if r["serviceArn"] == service_arn and (test_id is None or r["testId"] == test_id)]}

    def _start_test_run(self, region: Optional[str], service_arn: str, test_id: str) -> Dict[str, Any]:
        if any(r["serviceArn"] == service_arn and r["status"] in ("INITIALIZING", "RUNNING", "STOPPING") for r in self.runs.values()):
            raise self._error("resiliencehubv2", "start-test-run", "ConflictException", "An active test run already exists for this service.")
        run_id = self._id("run")
        self.runs[run_id] = {"testRunId": run_id, "testId": test_id, "status": "INITIALIZING", "startedAt": "2026-10-06T16:00:00+00:00",
                             "serviceArn": service_arn, "testTemplateArn": self.tests[test_id]["testTemplateArn"],
                             "script": list(self.run_script), "polls": 0}
        return {"testRunId": run_id, "status": "INITIALIZING", "experimentArns": []}

    def _get_test_run(self, region: Optional[str], service_arn: str, test_run_id: str) -> Dict[str, Any]:
        run = self.runs.get(test_run_id)
        if run is None:
            raise self._error("resiliencehubv2", "get-test-run", "ResourceNotFoundException", "Test run not found")
        script = run["script"]
        if run["status"] not in TERMINAL:
            run["status"] = script[min(run["polls"], len(script) - 1)]
            run["polls"] += 1
        if run["status"] in TERMINAL and "endedAt" not in run:
            run["endedAt"] = "2026-10-06T16:20:00+00:00"
            if self.run_error:
                run["errorMessage"] = self.run_error
        test = self.tests[run["testId"]]
        body = {k: run[k] for k in ("testRunId", "testId", "status", "startedAt", "endedAt", "serviceArn", "testTemplateArn", "errorMessage") if k in run}
        body.update({k: test[k] for k in ("parameters", "roleName", "stopConditions", "loggingConfiguration") if k in test})
        body["experiments"] = [{"experimentArn": f"arn:aws:fis:{PRIMARY}:{self.account}:experiment/{e['id']}", "details": "d"}
                               for e in self.fis.get(PRIMARY, [])] if run["status"] != "INITIALIZING" else []
        return {"testRun": body}

    def _stop_test_run(self, region: Optional[str], service_arn: str, test_run_id: str) -> Dict[str, Any]:
        run = self.runs[test_run_id]
        if run["status"] in TERMINAL:
            raise self._error("resiliencehubv2", "stop-test-run", "ConflictException", "The run has ended.")
        run["status"] = "STOPPING"
        run["script"] = list(self.stop_script)
        run["polls"] = 0
        return {"testRunId": test_run_id, "status": "STOPPING"}

    def _list_test_run_events(self, region: Optional[str], service_arn: str, test_run_id: str) -> Dict[str, Any]:
        return {"events": list(self.run_events)}

    def _list_test_run_sources(self, region: Optional[str], service_arn: str, test_run_id: str) -> Dict[str, Any]:
        run = self.runs[test_run_id]
        outcomes = {"SUCCESS_CRITERIA": "FAILED" if run["status"] == "FAILED" else "PASSED"}
        out = []
        for kind, arn in self.sources.get(run["testId"], []):
            body = {"alarmArn": arn, "alarmName": arn.rsplit(":", 1)[-1], "region": arn.split(":")[3], "accountId": self.account}
            if kind == "SUCCESS_CRITERIA":
                out.append({"successCriteriaAlarm": {**body, "outcome": outcomes[kind], "outcomeReason": "alarm went to ALARM"}})
            else:
                out.append({"observabilityAlarm": body})
        return {"testRunSources": out}

    def _list_test_run_source_events(self, region: Optional[str], service_arn: str, test_run_id: str, source_arn: str) -> Dict[str, Any]:
        return {"testRunSourceEvents": list(self.run_source_events.get(source_arn, []))}

    def _list_resolved_targets(self, region: Optional[str], service_arn: str, test_run_id: str) -> Dict[str, Any]:
        return {"resolvedTargetResources": list(self.resolved_targets)}

    def _list_dependencies(self, region: Optional[str], service_arn: str, test_run_id: str) -> Dict[str, Any]:
        return {"dependencies": list(self.dependencies)}

    def _list_route53_health_checks(self, region: Optional[str], arn: str) -> Dict[str, Any]:
        return {"healthChecks": [{"hostedZoneId": "Z0000000000000", "recordName": f"store.demo{self.env}.io", "healthCheckId": f"hc-{r}",
                                  "status": status, "region": r} for r, status in self.health_checks.items()]}

    def _start_plan_execution(self, region: Optional[str], plan_arn: str, target_region: str, action: str,
                              mode: str = "graceful", comment: Optional[str] = None) -> Dict[str, Any]:
        execution_id = self._id("exec")
        self.started_executions[execution_id] = {"action": action, "target": target_region, "mode": mode, "polls": 0,
                                                 "endpoint": region, "comment": comment}
        other = STANDBY if target_region == PRIMARY else PRIMARY
        return {"executionId": execution_id, "plan": plan_arn, "planVersion": "1",
                "activateRegion": target_region if action == "activate" else other,
                "deactivateRegion": other if action == "activate" else target_region}

    def _get_plan_execution(self, region: Optional[str], plan_arn: str, execution_id: str) -> Dict[str, Any]:
        for listed in (x for items in self.plan_executions.values() for x in items):
            if listed["executionId"] == execution_id:     # an execution the test put in the list: its detail is what it was given
                return {"planArn": plan_arn, "mode": "graceful", **listed}
        e = self.started_executions[execution_id]
        state = self.execution_script[min(e["polls"], len(self.execution_script) - 1)]
        e["polls"] += 1
        if state in ("completed", "completedMonitoringApplicationHealth") and e["action"] == "activate":
            self.health_checks[e["target"]] = "healthy"       # what a finished activation does to DNS
        return {"planArn": plan_arn, "executionId": execution_id, "startTime": "2026-10-08T12:00:00+00:00", "mode": e["mode"],
                "executionState": state, "executionAction": e["action"], "executionRegion": e["target"]}

    def _get_plan(self, region: Optional[str], arn: str) -> Dict[str, Any]:
        """The plan as ARC returns it: the triggers it has now, and the deactivate workflow's parallel ECS scaling blocks."""
        blocks = [{"name": f"scale-{name}", "executionBlockType": "ECSServiceScaling",
                   "executionBlockConfiguration": {"ecsCapacityIncreaseConfig": {
                       "timeoutMinutes": 15, **({} if self.plan_target_percent is None else {"targetPercent": self.plan_target_percent}),
                       "capacityMonitoringApproach": "containerInsightsMaxInLast24Hours",
                       "services": [{"clusterArn": f"arn:aws:ecs:{r}:{ACCOUNT}:cluster/{CLUSTER}",
                                     "serviceArn": f"arn:aws:ecs:{r}:{ACCOUNT}:service/{CLUSTER}/{name}{self.env}"} for r in (PRIMARY, STANDBY)]}}}
                  for name in ("ui", "catalog", "carts", "checkout", "orders", "assets")]
        return {"plan": {"arn": arn, "name": f"mr-rs-plan{self.env}", "recoveryApproach": "activeActive", "primaryRegion": PRIMARY,
                         "regions": [PRIMARY, STANDBY], "recoveryTimeObjectiveMinutes": 10, "associatedAlarms": {},
                         "triggers": [dict(t) for t in self.plan_triggers],
                         "workflows": [{"workflowTargetAction": "deactivate", "steps": [
                             {"name": "scale-up-ecs-services", "executionBlockType": "Parallel",
                              "executionBlockConfiguration": {"parallelConfig": {"steps": blocks}}}]},
                             {"workflowTargetAction": "activate", "steps": []}]}}

    def _get_metric_statistics(self, region: Optional[str], namespace: str, metric_name: str, dimensions: List[Dict[str, str]],
                               start_time: str, end_time: str, period: int, statistics: List[str]) -> Dict[str, Any]:
        """RunningTaskCount of an ECS service from Container Insights: one hourly datapoint holding the peak the test set."""
        assert (namespace, metric_name, statistics, period) == ("ECS/ContainerInsights", "RunningTaskCount", ["Maximum"], 3600)
        by_name = {d["Name"]: d["Value"] for d in dimensions}
        assert by_name["ClusterName"] == CLUSTER, by_name
        peak = self.task_peaks.get((region, by_name["ServiceName"]))
        if peak is None:
            return {"Label": metric_name, "Datapoints": []}
        # An hourly maximum for each of three hours, the peak in the middle one and the others lower, in no useful order.
        return {"Label": metric_name, "Datapoints": [{"Timestamp": start_time, "Maximum": 1.0, "Unit": "Count"},
                                                     {"Timestamp": end_time, "Maximum": peak, "Unit": "Count"},
                                                     {"Timestamp": start_time, "Maximum": max(1.0, peak - 1), "Unit": "Count"}]}

    def _update_service(self, region: Optional[str], cluster: str, service: str, desired_count: int) -> Dict[str, Any]:
        self.ecs_services[(region, service)]["desiredCount"] = int(desired_count)
        return {"service": self.ecs_services[(region, service)]}

    def _describe_scalable_targets(self, region: Optional[str], service_namespace: str, scalable_dimension: str,
                                   resource_ids: List[str]) -> Dict[str, Any]:
        return {"ScalableTargets": [dict(self.scalable_targets[(region, r)]) for r in resource_ids if (region, r) in self.scalable_targets]}

    def _register_scalable_target(self, region: Optional[str], service_namespace: str, scalable_dimension: str,
                                  resource_id: str, min_capacity: int, max_capacity: int) -> Dict[str, Any]:
        self.scalable_targets[(region, resource_id)].update(MinCapacity=int(min_capacity), MaxCapacity=int(max_capacity))
        return {"ScalableTargetARN": f"arn:aws:application-autoscaling:{region}:{ACCOUNT}:scalable-target/0123456789"}

    def _global_cluster(self) -> Dict[str, Any]:
        return {"GlobalClusterIdentifier": f"catalog-global-db-cluster{self.env}", "Status": self.global_cluster_status,
                "GlobalClusterMembers": [{"DBClusterArn": db_cluster_arn(r, self.env), "IsWriter": r == self.global_writer}
                                         for r in self.global_members]}

    def _describe_global_clusters(self, region: Optional[str], global_cluster_identifier: str) -> Dict[str, Any]:
        if global_cluster_identifier != f"catalog-global-db-cluster{self.env}":
            raise self._error("rds", "describe-global-clusters", "GlobalClusterNotFoundFault", "Global cluster not found")
        self._global_polls += 1
        for polls, change in self._global_changes:
            if polls == self._global_polls:
                change()
        if self._switchover is not None:                  # a switchover moves one step with every look at the cluster
            step = self._switchover
            status = self.switchover_script[min(step["polls"], len(self.switchover_script) - 1)]
            step["polls"] += 1
            self.global_cluster_status = status
            if status == "available":
                self.global_writer = step["target"]
                self._switchover = None
        return {"GlobalClusters": [self._global_cluster()]}

    def _describe_db_clusters(self, region: Optional[str], db_cluster_identifier: str) -> Dict[str, Any]:
        """By ARN or by identifier, as the real call takes either."""
        for r, status in self.global_members.items():
            if db_cluster_identifier in (db_cluster_arn(r, self.env), catalog_cluster_id(r, self.env)):
                return {"DBClusters": [{"DBClusterArn": db_cluster_arn(r, self.env), "DBClusterIdentifier": catalog_cluster_id(r, self.env),
                                        "Status": status, "Endpoint": catalog_endpoint(r, self.env),
                                        "ReaderEndpoint": catalog_endpoint(r, self.env, reader=True)}]}
        raise self._error("rds", "describe-db-clusters", "DBClusterNotFoundFault", f"DBCluster {db_cluster_identifier} not found")

    def _switchover_global_cluster(self, region: Optional[str], global_cluster_identifier: str,
                                   target_db_cluster_identifier: str) -> Dict[str, Any]:
        target = next((r for r in self.global_members if db_cluster_arn(r, self.env) == target_db_cluster_identifier), None)
        if target is None:
            raise self._error("rds", "switchover-global-cluster", "DBClusterNotFoundFault", "DBCluster not found")
        if self.global_cluster_status != "available" or self.global_members[target] != "available":
            raise self._error("rds", "switchover-global-cluster", "InvalidGlobalClusterStateFault",
                              "The global cluster is in an invalid state and can't perform the requested operation.")
        self._switchover = {"target": target, "polls": 0}
        return {"GlobalCluster": self._global_cluster()}

    @staticmethod
    def _camel(name: str) -> str:
        head, *rest = name.split("_")
        return head + "".join(p.capitalize() for p in rest)
