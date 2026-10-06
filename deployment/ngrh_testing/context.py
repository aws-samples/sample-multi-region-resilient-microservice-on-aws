# SPDX-License-Identifier: MIT-0
"""Where the tests run, and how the spec's names become API arguments (design 5.8, 5.9).

An ``Environment`` is what the Makefile passes (the two Regions and ENV) plus what AWS says about the
deployment: the account, the partition and the ngrh stack's outputs. ``resolve_all`` turns spec tests
into ``ResolvedTest``s, the exact arguments CreateTest, UpdateTest and PutTestSources take, and names
every reference it cannot resolve before anything is written (design section 7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from . import spec
from .aws import AwsCli, AwsCliError

SOURCE_SUCCESS = "SUCCESS_CRITERIA"
SOURCE_OBSERVABILITY = "OBSERVABILITY"
STOP_SOURCE_ALARM = "aws:cloudwatch:alarm"

# The FIS log group each Region's monitoring stack creates (monitoring.yml).
FIS_LOG_GROUP = "/aws/fis/ngrh-tests"


class ContextError(RuntimeError):
    """The deployment, or something the spec refers to, is not as the tests need. ``problems``
    holds one line for each thing wrong."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class Environment:
    primary_region: str
    standby_region: str
    env: str  # the Makefile's ENV, for example -dev; empty for an unsuffixed deployment
    account_id: str
    partition: str
    outputs: Mapping[str, str]  # the ngrh<ENV> stack's outputs

    @property
    def ngrh_region(self) -> str:
        """Resilience Hub is called in the primary Region, as the Makefile's own ngrh targets do."""
        return self.primary_region

    @property
    def regions(self) -> Tuple[str, str]:
        return (self.primary_region, self.standby_region)

    def region(self, ref: str) -> str:
        """A spec Region: the placeholders primary and standby, or a Region name as it stands."""
        if ref == "primary":
            return self.primary_region
        if ref == "standby":
            return self.standby_region
        return ref

    def output(self, key: str) -> str:
        value = self.outputs.get(key)
        if not value:
            raise ContextError([f"stack ngrh{self.env} has no output {key}; deploy the current ngrh.yaml (make ngrh)"])
        return value

    def service_arn(self, service: str) -> str:
        return self.output(service.capitalize() + "ServiceArn")

    def service_arns(self) -> List[str]:
        """Every NGRH service of the stack, from its ServiceArns output."""
        return [arn for arn in self.outputs.get("ServiceArns", "").split(",") if arn]

    @property
    def experiment_role_name(self) -> str:
        return self.output("TestExperimentRoleName")

    @property
    def invoker_role_name(self) -> str:
        return self.output("InvokerRoleName")

    def alarm_name(self, name: str, region: str) -> str:
        """monitoring.yml names every alarm <name>-<Region><Env>."""
        return f"{name}-{self.region(region)}{self.env}"

    def alarm_arn(self, full_name: str, region: str) -> str:
        return f"arn:{self.partition}:cloudwatch:{region}:{self.account_id}:alarm:{full_name}"

    def template_arn(self, template: str) -> str:
        return f"arn:{self.partition}:resiliencehub:{self.ngrh_region}:aws:test-template/{template}"

    @property
    def log_group_name(self) -> str:
        return f"{FIS_LOG_GROUP}{self.env}"

    def log_group_arn(self, region: str) -> str:
        return f"arn:{self.partition}:logs:{region}:{self.account_id}:log-group:{self.log_group_name}"


def stack_outputs(aws: AwsCli, region: str, stack_name: str) -> Optional[Dict[str, str]]:
    """A stack's outputs, or None when the stack does not exist."""
    try:
        described = aws.call("cloudformation", "describe-stacks", region, stack_name=stack_name)
    except AwsCliError as e:
        if "does not exist" in str(e):
            return None
        raise
    return {o["OutputKey"]: o["OutputValue"] for o in described["Stacks"][0].get("Outputs", [])}


def ngrh_outputs(aws: AwsCli, region: str, env: str) -> Optional[Dict[str, str]]:
    """The ngrh<ENV> stack's outputs, or None when the stack does not exist."""
    return stack_outputs(aws, region, f"ngrh{env}")


def load_environment(aws: AwsCli, primary_region: str, standby_region: str, env: str) -> Environment:
    identity = aws.call("sts", "get-caller-identity")
    outputs = ngrh_outputs(aws, primary_region, env)
    if outputs is None:
        raise ContextError([f"stack ngrh{env} does not exist in {primary_region}; deploy it first (make ngrh)"])
    return Environment(primary_region, standby_region, env, identity["Account"], identity["Arn"].split(":")[1], outputs)


# --- lookups ----------------------------------------------------------------------------------

def mq_broker_host(aws: AwsCli, env: Environment, region: str) -> List[str]:
    """The host names orders reaches its broker at: the hosts of the endpoints Amazon MQ reports for
    the apps stack's OrdersMqBroker. They are read from the broker rather than written down, because
    the name holds a broker id that changes with every deploy."""
    detail = aws.call("cloudformation", "describe-stack-resource", region,
                      stack_name=f"apps{env.env}", logical_resource_id="OrdersMqBroker")
    broker = aws.call("mq", "describe-broker", region, broker_id=detail["StackResourceDetail"]["PhysicalResourceId"])
    hosts: List[str] = []
    for instance in broker.get("BrokerInstances", []):
        for endpoint in instance.get("Endpoints", []):
            host = urlparse(endpoint).hostname
            if host and host not in hosts:
                hosts.append(host)
    return hosts


LOOKUPS: Dict[str, Callable[[AwsCli, Environment, str], List[str]]] = {"mq-broker-host": mq_broker_host}


# --- resolving a spec test ----------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedAlarm:
    name: str  # the full name, <name>-<Region><Env>
    region: str
    arn: str


@dataclass(frozen=True)
class ResolvedTest:
    test: spec.Test
    service_arn: str
    template_arn: str
    parameters: Dict[str, List[str]]  # as CreateTest and UpdateTest take them
    role_name: str
    fault_region: str
    logging_configuration: Dict[str, str]
    stop_conditions: List[Dict[str, str]]
    success: List[ResolvedAlarm]
    observability: List[ResolvedAlarm]
    stop: List[ResolvedAlarm]
    evidence: List[ResolvedAlarm]  # groups expanded; evidence in the faulted Region comes first

    @property
    def name(self) -> str:
        return self.test.name

    def sources(self) -> Set[Tuple[str, str]]:
        """What PutTestSources should hold: (kind, alarm ARN)."""
        return ({(SOURCE_SUCCESS, a.arn) for a in self.success}
                | {(SOURCE_OBSERVABILITY, a.arn) for a in self.observability})

    def alarms_by_region(self) -> Dict[str, List[str]]:
        """Every alarm the test refers to, by Region: the ones that must exist."""
        out: Dict[str, List[str]] = {}
        for a in self.success + self.observability + self.stop + self.evidence:
            if a.name not in out.setdefault(a.region, []):
                out[a.region].append(a.name)
        return out


def _alarm(env: Environment, name: str, region_ref: str) -> ResolvedAlarm:
    region = env.region(region_ref)
    full = env.alarm_name(name, region_ref)
    return ResolvedAlarm(full, region, env.alarm_arn(full, region))


def resolve(aws: AwsCli, env: Environment, test: spec.Test, problems: List[str]) -> Optional[ResolvedTest]:
    """Resolve one test, appending to ``problems`` for everything that doesn't resolve."""
    before = len(problems)
    parameters: Dict[str, List[str]] = {}
    for key, values in test.parameters.items():
        out: List[str] = []
        for value in values:
            if isinstance(value, spec.Lookup):
                region = env.region(value.region)
                try:
                    found = LOOKUPS[value.lookup](aws, env, region)
                except AwsCliError as e:
                    problems.append(f"{test.name}: lookup {value.lookup} in {region} failed: {e}")
                    continue
                if not found:
                    problems.append(f"{test.name}: lookup {value.lookup} in {region} returned nothing")
                out.extend(found)
            else:
                out.append(env.region(value) if key in spec.REGION_PARAMETERS else value)
        if len(out) > spec.MAX_PARAMETER_VALUES:
            problems.append(f"{test.name}: parameter {key} resolved to {len(out)} values; the limit is {spec.MAX_PARAMETER_VALUES}")
        parameters[key] = out
    try:
        service_arn, role_name = env.service_arn(test.service), env.experiment_role_name
    except ContextError as e:
        problems.extend(f"{test.name}: {p}" for p in e.problems)
        return None
    if len(problems) > before:
        return None

    fault_region = parameters[test.shape.fault_region][0]
    evidence: List[ResolvedAlarm] = []
    for ref in test.evidence_alarms:
        evidence.extend(_alarm(env, name, ref.region) for name in ref.alarm_names())
    stop = [_alarm(env, a.name, a.region) for a in test.stop_alarms]
    return ResolvedTest(
        test=test,
        service_arn=service_arn,
        template_arn=env.template_arn(test.template),
        parameters=parameters,
        role_name=role_name,
        fault_region=fault_region,
        logging_configuration={"cloudWatchLogGroupArn": env.log_group_arn(fault_region)},
        stop_conditions=[{"source": STOP_SOURCE_ALARM, "value": a.arn} for a in stop],
        success=[_alarm(env, a.name, a.region) for a in test.success_alarms],
        observability=[_alarm(env, a.name, a.region) for a in test.observability_alarms],
        stop=stop,
        evidence=evidence,
    )


def resolve_all(aws: AwsCli, env: Environment, tests: Sequence[spec.Test]) -> List[ResolvedTest]:
    """Resolve every test, or raise ContextError naming everything that didn't resolve."""
    problems: List[str] = []
    resolved = [r for r in (resolve(aws, env, t, problems) for t in tests) if r is not None]
    if problems:
        raise ContextError(problems)
    return resolved
