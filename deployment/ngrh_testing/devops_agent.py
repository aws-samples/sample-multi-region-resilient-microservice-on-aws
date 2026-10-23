# SPDX-License-Identifier: MIT-0
"""Connect NGRH services to an AWS DevOps Agent Agent Space, disconnect them, or show their state
(make devops-agent-connect, make devops-agent-disconnect, make devops-agent-status). ``check`` is the one action that
needs no deployment: make deploy DEVOPS_AGENT_CONNECT=<services> runs it before it deploys anything.

Resilience Hub starts a DevOps Agent investigation for a service once the service names an Agent Space in its
DevOps Agent configuration. That configuration is set here, not in ngrh.yaml: CloudFormation cannot take it away
again (a template that leaves it out leaves the link as it is), and cfn-lint's schema for the service resource
does not have the property yet.

Connecting costs money. When a service is connected, Resilience Hub starts one investigation for each failure mode
finding the service has, open or resolved, all at once (DevOps Agent runs three at a time and queues the rest), and
DevOps Agent bills an investigation by the agent-second. Connecting orders-dev, which had 21 findings, started 21
investigations within four seconds. So a service is connected only when it is named, and the finding count of each
is shown before anything is changed.

Connecting needs the invoker role to have the DevOps Agent permissions, which ngrh.yaml adds when it is given the
Agent Space. The role can take a minute to get them, and a connection that cannot use them yet ends FAILED. That is
reported once, with Resilience Hub's reason, and left to be run again; it is not tried again here, because whether
connecting a service a second time starts its investigations a second time is not documented, and each costs money.
A service that is already connected to the Agent Space is left alone, which starts no investigation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

from .aws import AwsCli, AwsCliError
from .context import Environment

ACTIONS = ("connect", "disconnect", "status", "check")

# The services of ngrh.yaml, by the short names its outputs use (OrdersServiceArn, UiServiceArn, ...).
SERVICE_NAMES = ("ui", "catalog", "cart", "checkout", "orders", "assets")
ALL = "all"

CONNECTED = "CONNECTED"
CONNECTING = "CONNECTING"
FAILED = "FAILED"
DISCONNECTED = "DISCONNECTED"

# The first AWS CLI release whose update-service takes --dev-ops-agent-configuration.
MIN_CLI_VERSION = "2.38.0"

SETTLE_SECONDS = 120.0        # how long to wait for CONNECTING to end
DISCONNECT_SETTLE_SECONDS = 60.0
POLL_SECONDS = 5.0
CONFLICT_WAIT_SECONDS = 15.0  # update-service answers ConflictException while the service is being changed
UPDATE_ATTEMPTS = 5


class DevOpsAgentError(RuntimeError):
    """The connection cannot be made, or could not be read."""


class CliNotSupported(DevOpsAgentError):
    """The AWS CLI is too old to read or set a service's DevOps Agent configuration."""


@dataclass(frozen=True)
class Connection:
    """What Resilience Hub says about one service's DevOps Agent configuration, and how many findings it has."""

    name: str
    service_arn: str
    space_arn: str = ""
    status: str = ""
    reason: str = ""
    findings: int = 0

    @property
    def connected(self) -> bool:
        return self.status == CONNECTED

    @property
    def linked(self) -> bool:
        """The service still names an Agent Space."""
        return bool(self.space_arn) and self.status != DISCONNECTED


def _name(service_arn: str) -> str:
    return service_arn.rsplit("/", 1)[-1].split(":")[0]


def _view(service: Dict[str, Any], service_arn: str, findings: int = 0) -> Connection:
    config = service.get("devOpsAgentConfiguration") or {}
    return Connection(
        name=service.get("name") or _name(service_arn),
        service_arn=service_arn,
        space_arn=config.get("agentSpaceArn", ""),
        status=config.get("status", ""),
        reason=config.get("statusReason", ""),
        findings=findings,
    )


def _read(aws: AwsCli, env: Environment, service_arn: str, findings: int = 0) -> Connection:
    service = aws.call("resiliencehubv2", "get-service", env.ngrh_region, service_arn=service_arn)["service"]
    return _view(service, service_arn, findings)


def _finding_count(aws: AwsCli, env: Environment, service_arn: str) -> int:
    """How many failure mode findings the service has, open and resolved: the number of investigations connecting starts."""
    response = aws.call("resiliencehubv2", "list-failure-mode-findings", env.ngrh_region, service_arn=service_arn)
    return len(response.get("findingsSummary") or [])


def _update(aws: AwsCli, env: Environment, service_arn: str, config: Dict[str, str],
            sleep: Callable[[float], None]) -> Connection:
    """Set the service's DevOps Agent configuration: an Agent Space connects it, an empty one disconnects it."""
    for attempt in range(1, UPDATE_ATTEMPTS + 1):
        try:
            response = aws.call("resiliencehubv2", "update-service", env.ngrh_region,
                                service_arn=service_arn, dev_ops_agent_configuration=config)
            return _view(response["service"], service_arn)
        except AwsCliError as e:
            if "ConflictException" in str(e) and attempt < UPDATE_ATTEMPTS:
                sleep(CONFLICT_WAIT_SECONDS)
                continue
            raise
    raise AssertionError("unreachable")  # pragma: no cover


def require_cli_support(aws: AwsCli) -> None:
    """Refuse an AWS CLI that does not know the option. Asking for the operation's skeleton makes no AWS call,
    so this needs no credentials and writes nothing."""
    try:
        skeleton = aws.call("resiliencehubv2", "update-service", None, generate_cli_skeleton="input")
    except AwsCliError as e:
        raise CliNotSupported(f"this AWS CLI cannot connect a service to DevOps Agent ({e}); "
                              f"install AWS CLI {MIN_CLI_VERSION} or later") from e
    if "devOpsAgentConfiguration" not in skeleton:
        raise CliNotSupported("this AWS CLI does not have update-service --dev-ops-agent-configuration; "
                              f"install AWS CLI {MIN_CLI_VERSION} or later (aws --version says which one this is)")


def all_services(env: Environment) -> List[str]:
    arns = env.service_arns()
    if not arns:
        raise DevOpsAgentError(f"stack ngrh{env.env} has no services (its ServiceArns output is empty); deploy it first (make ngrh)")
    return arns


def parse_service_names(names: str) -> List[str]:
    """The service names in ``names`` (separated by spaces or commas, in any case), in the order given and without repeats,
    or ``["all"]`` when ``all`` is among them. Refuses a list with nothing in it and a name that is not a service. This is the
    part of select_services that needs no deployment, so ``check`` can run it before there is one."""
    wanted = [n for n in re.split(r"[,\s]+", names.strip().lower()) if n]
    if not wanted:
        raise DevOpsAgentError(f"name the services to connect: {', '.join(SERVICE_NAMES)}, or {ALL}")
    if ALL in wanted:
        return [ALL]
    unknown = [n for n in wanted if n not in SERVICE_NAMES]
    if unknown:
        raise DevOpsAgentError(f"unknown service {', '.join(unknown)}: the services are {', '.join(SERVICE_NAMES)}")
    return list(dict.fromkeys(wanted))


def select_services(env: Environment, names: str) -> List[str]:
    """The ARNs of the services named in ``names`` (separated by spaces or commas), or of all of them for ``all``."""
    wanted = parse_service_names(names)
    if wanted == [ALL]:
        return all_services(env)
    arns: List[str] = []
    for n in wanted:
        arn = env.service_arn(n)
        if arn not in arns:
            arns.append(arn)
    return arns


def check(aws: AwsCli, services: str) -> List[str]:
    """What ``make deploy DEVOPS_AGENT_CONNECT=<services>`` asks before it deploys anything: the names are ones connect accepts
    and the AWS CLI can connect a service. Connect would refuse either after a deploy of about two hours, so both are found
    first. It reads nothing from the deployment, which does not exist yet on a first deploy, and it writes nothing. Returns the
    names, as parse_service_names gives them."""
    names = parse_service_names(services)
    require_cli_support(aws)
    return names


def _settle(aws: AwsCli, env: Environment, states: Dict[str, Connection], arns: List[str], done: Callable[[Connection], bool],
            seconds: float, sleep: Callable[[float], None], clock: Callable[[], float]) -> Dict[str, Connection]:
    """Read the services until each one's state satisfies ``done``, or ``seconds`` have passed."""
    current = {arn: states[arn] for arn in arns}
    deadline = clock() + seconds
    while True:
        waiting = [arn for arn in arns if not done(current[arn])]
        if not waiting or clock() >= deadline:
            return current
        sleep(POLL_SECONDS)
        for arn in waiting:
            current[arn] = _read(aws, env, arn, current[arn].findings)


def _on_its_way(c: Connection, space_arn: str) -> bool:
    return c.space_arn == space_arn and c.status in (CONNECTED, CONNECTING)


def connect(aws: AwsCli, env: Environment, space_arn: str, services: str, say: Callable[[str], None],
            sleep: Callable[[float], None], clock: Callable[[], float]) -> List[Connection]:
    """Connect the named services to the Agent Space and wait until each is CONNECTED or FAILED. Says first how many
    investigations that starts. Each service is sent one update, whatever happens to it. Returns the final state of
    every named service."""
    require_cli_support(aws)
    arns = select_services(env, services)
    states: Dict[str, Connection] = {}
    new: List[str] = []
    for arn in arns:
        current = _read(aws, env, arn)
        if _on_its_way(current, space_arn):
            states[arn] = current
        else:
            states[arn] = _view({"name": current.name}, arn, _finding_count(aws, env, arn))
            new.append(arn)
    for arn in arns:
        if arn in new:
            say(f"  {states[arn].name}: {states[arn].findings} findings")
        else:
            say(f"  {states[arn].name}: already connected to this Agent Space, so nothing is started")
    if not new:
        return [states[arn] for arn in arns]
    say(f"Connecting starts one DevOps Agent investigation for each finding of a service, {sum(states[a].findings for a in new)} in all. "
        "DevOps Agent runs three at a time and bills each by the agent-second (README, \"Investigating with AWS DevOps Agent\").")
    for arn in new:
        updated = _update(aws, env, arn, {"agentSpaceArn": space_arn}, sleep)
        states[arn] = Connection(updated.name, arn, updated.space_arn, updated.status, updated.reason, states[arn].findings)
    states.update(_settle(aws, env, states, new, lambda c: c.status in (CONNECTED, FAILED), SETTLE_SECONDS, sleep, clock))
    return [states[arn] for arn in arns]


def disconnect(aws: AwsCli, env: Environment, say: Callable[[str], None],
               sleep: Callable[[float], None], clock: Callable[[], float]) -> List[Connection]:
    """Take every service off its Agent Space and wait until none names one. Returns the final state of every service."""
    require_cli_support(aws)
    arns = all_services(env)
    states: Dict[str, Connection] = {}
    changed: List[str] = []
    for arn in arns:
        current = _read(aws, env, arn)
        if current.linked:
            states[arn] = _update(aws, env, arn, {}, sleep)
            changed.append(arn)
        else:
            states[arn] = current
    if not changed:
        say("No service is connected to an Agent Space.")
        return [states[arn] for arn in arns]
    say(f"Disconnecting {len(changed)} services from their Agent Space")
    states.update(_settle(aws, env, states, changed, lambda c: not c.linked, DISCONNECT_SETTLE_SECONDS, sleep, clock))
    return [states[arn] for arn in arns]


def status(aws: AwsCli, env: Environment) -> List[Connection]:
    """Read every service's DevOps Agent state and finding count. Writes nothing. An AWS CLI too old to know the
    field would show every service as not connected, so it is refused as well."""
    require_cli_support(aws)
    return [_read(aws, env, arn, _finding_count(aws, env, arn)) for arn in all_services(env)]


def render(connections: List[Connection], findings: bool = False) -> List[str]:
    """One line for each service; with ``findings``, how many findings each has (connecting starts one investigation for each)."""
    lines = []
    for c in connections:
        if not c.space_arn and not c.status:
            state = "not connected"
        else:
            state = f"{c.status or 'no status'}" + (f" ({c.reason})" if c.reason else "")
        lines.append(f"  {c.name}: {state}" + (f", {c.findings} findings" if findings else ""))
    return lines
