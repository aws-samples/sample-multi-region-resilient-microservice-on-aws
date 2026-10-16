# SPDX-License-Identifier: MIT-0
"""``python3 -m ngrh_testing devops-agent connect | disconnect | status`` (make devops-agent-connect and friends).

Everything runs against the shared fake AWS and a fake clock, so the wait for a connection is a loop over simulated
seconds. The fake models what Resilience Hub shows for a service's DevOps Agent configuration: a status that moves
from CONNECTING to CONNECTED or FAILED as the service is read, with a reason when it is FAILED, and a list of
failure mode findings for each service.

The rule these tests exist for: connecting a service starts one paid DevOps Agent investigation for each of its
findings (connecting orders-dev, which had 21, started 21 within four seconds, 2026-10-16). So nothing connects a
service unless it is named, the finding count of each is shown before anything is changed, a service already
connected to the Agent Space is left alone, and a connection that fails is reported once and not sent again.
"""

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
DEPLOYMENT = TESTS.parent / "deployment"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(DEPLOYMENT))

from ngrh_fake_aws import ENV, NGRH_SERVICES, PRIMARY, STANDBY, FakeAws, service_arn  # noqa: E402

from ngrh_testing import cli, context, devops_agent  # noqa: E402
from ngrh_testing.aws import AwsCliError  # noqa: E402

SPACE = "arn:aws:aidevops:us-east-1:111111111111:agentspace/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
OTHER_SPACE = "arn:aws:aidevops:us-east-1:111111111111:agentspace/ffffffff-0000-0000-0000-000000000000"
ARNS = {name: service_arn(name) for name in NGRH_SERVICES}
ALL_ARNS = list(ARNS.values())
ARGS = ["--primary-region", PRIMARY, "--standby-region", STANDBY, f"--env={ENV}"]
DENIED = ("FAILED", "AccessDeniedException: the invoker role is not allowed to call aidevops:GetAgentSpace")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.now > 3600:         # a wait that never ends fails the test instead of hanging it
            raise AssertionError("waited an hour of simulated time")


def environment(fake):
    return context.load_environment(fake, PRIMARY, STANDBY, ENV)


def connect(fake, services="orders", space=SPACE):
    clock, lines = Clock(), []
    result = devops_agent.connect(fake, environment(fake), space, services, lines.append, clock.sleep, clock)
    return result, lines, clock


def run_cli(fake, action, *extra):
    clock = Clock()
    code = cli.main(["devops-agent", *ARGS, action, *extra], aws=fake, sleep=clock.sleep, clock=clock)
    return code, clock


def updates(fake):
    return [c for c in fake.calls if (c[0], c[1]) == ("resiliencehubv2", "update-service")]


def real_updates(fake):
    """The update-service calls that change something (the capability probe only asks for the skeleton)."""
    return [c for c in updates(fake) if "generate_cli_skeleton" not in c[3]]


def updated_arns(fake):
    return [c[3]["service_arn"] for c in real_updates(fake)]


class TestCliSupport:
    def test_an_aws_cli_without_the_option_is_refused_before_anything_is_written(self):
        fake = FakeAws()
        fake.devops_cli_supported = False
        with pytest.raises(devops_agent.DevOpsAgentError, match=r"does not have update-service --dev-ops-agent-configuration.*2\.38\.0"):
            connect(fake)
        assert real_updates(fake) == []

    def test_the_probe_is_the_cli_skeleton_which_makes_no_aws_call(self):
        fake = FakeAws()
        connect(fake)
        probe = updates(fake)[0]
        assert probe[3] == {"generate_cli_skeleton": "input"} and probe[2] is None

    def test_a_cli_that_cannot_even_run_the_probe_is_refused_with_the_same_advice(self):
        fake = FakeAws()
        fake.fail_on("resiliencehubv2", "update-service", "argument operation: Invalid choice")
        with pytest.raises(devops_agent.DevOpsAgentError, match=r"install AWS CLI 2\.38\.0 or later"):
            connect(fake)

    def test_status_and_disconnect_refuse_an_old_cli_too(self):
        # An old CLI drops the field it does not know, so every service would read as not connected.
        for action in (lambda f: devops_agent.status(f, environment(f)),
                       lambda f: devops_agent.disconnect(f, environment(f), lambda _: None, Clock().sleep, Clock())):
            fake = FakeAws()
            fake.devops_cli_supported = False
            with pytest.raises(devops_agent.DevOpsAgentError):
                action(fake)

    def test_an_old_cli_is_a_named_kind_of_refusal(self):
        # --if-supported catches this and nothing else
        fake = FakeAws()
        fake.devops_cli_supported = False
        with pytest.raises(devops_agent.CliNotSupported):
            devops_agent.require_cli_support(fake)
        fake = FakeAws()
        fake.fail_on("resiliencehubv2", "update-service", "argument operation: Invalid choice")
        with pytest.raises(devops_agent.CliNotSupported):
            devops_agent.require_cli_support(fake)

    def test_disconnect_asked_for_by_hand_refuses_an_old_cli_with_exit_one_and_the_advice(self, capsys):
        fake = FakeAws()
        fake.devops_cli_supported = False
        code, _ = run_cli(fake, "disconnect")
        assert code == 1 and "install AWS CLI 2.38.0 or later" in capsys.readouterr().err
        assert real_updates(fake) == []

    @pytest.mark.parametrize("old_cli", ["no option", "no operation"])
    def test_if_supported_turns_an_old_cli_into_a_skip_that_says_what_was_not_done(self, capsys, old_cli):
        # make destroy-ngrh asks for this: the teardown goes on either way, and a deployment that made its Agent Space with
        # make devops-agent and never connected a service has nothing to take off. It is not an error, so nothing on stderr.
        fake = FakeAws()
        if old_cli == "no option":
            fake.devops_cli_supported = False
        else:
            fake.fail_on("resiliencehubv2", "update-service", "argument operation: Invalid choice")
        code, _ = run_cli(fake, "disconnect", "--if-supported")
        out = capsys.readouterr()
        assert code == 0 and out.err == ""
        assert "Skipped" in out.out and "older than 2.38.0" in out.out and "no service was disconnected" in out.out
        assert "stays connected" in out.out, "it says what the skip leaves behind"
        assert real_updates(fake) == []

    def test_if_supported_changes_nothing_for_an_aws_cli_that_is_new_enough(self):
        fake = FakeAws()
        fake.devops = {arn: {"agentSpaceArn": SPACE, "status": "CONNECTED"} for arn in ALL_ARNS}
        code, _ = run_cli(fake, "disconnect", "--if-supported")
        assert code == 0 and fake.devops == {} and updated_arns(fake) == ALL_ARNS

    def test_if_supported_does_not_hide_a_failure_that_is_not_about_the_aws_cli(self, capsys):
        fake = FakeAws()
        fake.outputs["ServiceArns"] = ""
        code, _ = run_cli(fake, "disconnect", "--if-supported")
        assert code == 1 and "no services" in capsys.readouterr().err


class TestSelectingServices:
    def test_names_are_accepted_separated_by_spaces_or_commas_in_any_case(self):
        env = environment(FakeAws())
        assert devops_agent.select_services(env, "Orders, cart  ui") == [ARNS["orders"], ARNS["cart"], ARNS["ui"]]

    def test_all_means_every_service_of_the_stack(self):
        assert devops_agent.select_services(environment(FakeAws()), "all") == ALL_ARNS

    def test_a_service_named_twice_is_connected_once(self):
        assert devops_agent.select_services(environment(FakeAws()), "orders orders") == [ARNS["orders"]]

    def test_nothing_named_is_refused(self):
        with pytest.raises(devops_agent.DevOpsAgentError, match="name the services to connect"):
            devops_agent.select_services(environment(FakeAws()), "  ")

    def test_an_unknown_name_is_refused_and_the_known_ones_are_listed(self):
        with pytest.raises(devops_agent.DevOpsAgentError, match=r"unknown service orderz.*ui, catalog, cart, checkout, orders, assets"):
            devops_agent.select_services(environment(FakeAws()), "orders orderz")

    def test_the_names_are_the_ones_the_stacks_outputs_use(self):
        assert devops_agent.SERVICE_NAMES == NGRH_SERVICES


class TestConnect:
    def test_it_connects_only_the_named_services(self):
        fake = FakeAws()
        states, _, _ = connect(fake, "orders cart")
        assert [c.service_arn for c in states] == [ARNS["orders"], ARNS["cart"]]
        assert all(c.connected and c.space_arn == SPACE for c in states)
        assert updated_arns(fake) == [ARNS["orders"], ARNS["cart"]]
        assert not set(fake.devops) - {ARNS["orders"], ARNS["cart"]}

    def test_it_sends_the_agent_space_in_the_primary_region_and_nothing_else(self):
        fake = FakeAws()
        connect(fake)
        (call,) = real_updates(fake)
        assert call[2] == PRIMARY
        assert call[3] == {"service_arn": ARNS["orders"], "dev_ops_agent_configuration": {"agentSpaceArn": SPACE}}

    def test_all_connects_every_service(self):
        fake = FakeAws()
        states, _, _ = connect(fake, "all")
        assert updated_arns(fake) == ALL_ARNS and all(c.connected for c in states)

    def test_it_shows_each_services_finding_count_and_the_total_before_it_changes_anything(self):
        fake = FakeAws()
        fake.findings = {ARNS["orders"]: 21, ARNS["cart"]: 18}
        order = []
        original = fake._rh_update_service

        def update(region, **kw):
            if "generate_cli_skeleton" not in kw:
                order.append("update")
            return original(region, **kw)

        fake._handlers[("resiliencehubv2", "update-service")] = update
        _, lines, _ = connect(fake, "orders cart")
        assert lines[0] == "  orders: 21 findings" and lines[1] == "  cart: 18 findings"
        assert "one DevOps Agent investigation for each finding" in lines[2] and "39 in all" in lines[2]
        assert order == ["update", "update"], "the lines above were produced before the first update"

    def test_a_connection_is_sent_once_whatever_happens_to_it(self):
        fake = FakeAws()
        fake.devops_scripts = [[DENIED]]
        states, _, _ = connect(fake, "orders cart")
        assert [c.status for c in states] == ["FAILED", "FAILED"]
        assert updated_arns(fake) == [ARNS["orders"], ARNS["cart"]], "a retry could start the investigations again, at a cost"

    def test_a_failed_connection_reports_resilience_hubs_reason(self):
        fake = FakeAws()
        fake.devops_scripts = [[DENIED]]
        states, _, _ = connect(fake)
        assert states[0].status == "FAILED" and "aidevops:GetAgentSpace" in states[0].reason

    def test_it_waits_while_a_connection_is_still_being_made(self):
        fake = FakeAws()
        fake.devops_scripts = [["CONNECTING", "CONNECTING", "CONNECTING", "CONNECTED"]]
        states, _, clock = connect(fake)
        assert states[0].connected and clock.now >= devops_agent.POLL_SECONDS * 3

    def test_it_stops_waiting_for_a_connection_that_never_finishes(self):
        fake = FakeAws()
        fake.devops_scripts = [["CONNECTING"]]
        states, _, clock = connect(fake)
        assert states[0].status == "CONNECTING" and devops_agent.SETTLE_SECONDS <= clock.now < devops_agent.SETTLE_SECONDS + 10

    def test_a_service_already_connected_to_the_agent_space_is_left_alone_and_starts_nothing(self):
        fake = FakeAws()
        fake.devops = {arn: {"agentSpaceArn": SPACE, "status": "CONNECTED"} for arn in ALL_ARNS}
        states, lines, _ = connect(fake, "orders cart")
        assert all(c.connected for c in states) and real_updates(fake) == []
        assert lines[0] == "  orders: already connected to this Agent Space, so nothing is started"
        assert not any("investigation for each finding" in line for line in lines)
        assert ("resiliencehubv2", "list-failure-mode-findings") not in [(c[0], c[1]) for c in fake.calls]

    def test_a_service_that_is_still_connecting_is_not_sent_again(self):
        fake = FakeAws()
        fake.devops = {ARNS["orders"]: {"agentSpaceArn": SPACE, "status": "CONNECTING"}}
        connect(fake)
        assert real_updates(fake) == []

    def test_only_the_services_not_yet_connected_are_sent_and_counted(self):
        fake = FakeAws()
        fake.devops = {ARNS["orders"]: {"agentSpaceArn": SPACE, "status": "CONNECTED"}}
        fake.findings = {ARNS["cart"]: 4}
        _, lines, _ = connect(fake, "orders cart")
        assert updated_arns(fake) == [ARNS["cart"]] and "4 in all" in lines[2]

    def test_a_service_connected_to_another_agent_space_is_moved(self):
        fake = FakeAws()
        fake.devops = {ARNS["orders"]: {"agentSpaceArn": OTHER_SPACE, "status": "CONNECTED"}}
        states, _, _ = connect(fake)
        assert states[0].space_arn == SPACE and states[0].connected and updated_arns(fake) == [ARNS["orders"]]

    def test_a_failed_service_is_connected_again_when_the_command_is_run_again(self):
        fake = FakeAws()
        fake.devops = {ARNS["orders"]: {"agentSpaceArn": SPACE, "status": "FAILED", "statusReason": "old"}}
        states, _, _ = connect(fake)
        assert states[0].connected and updated_arns(fake) == [ARNS["orders"]]

    def test_a_conflict_while_the_service_is_being_changed_is_waited_out(self):
        fake = FakeAws()
        conflicts = {"left": 2}
        original = fake._rh_update_service

        def update(region, **kw):
            if "generate_cli_skeleton" not in kw and conflicts["left"]:
                conflicts["left"] -= 1
                raise AwsCliError("aws resiliencehubv2 update-service failed: ConflictException: the service is being updated")
            return original(region, **kw)

        fake._handlers[("resiliencehubv2", "update-service")] = update
        states, _, clock = connect(fake)
        assert states[0].connected and clock.now >= 2 * devops_agent.CONFLICT_WAIT_SECONDS

    def test_any_other_error_from_update_service_stops_it_with_the_cli_message(self):
        fake = FakeAws()
        original = fake._rh_update_service

        def update(region, **kw):
            if "generate_cli_skeleton" not in kw:
                raise AwsCliError("aws resiliencehubv2 update-service failed: AccessDeniedException: nope")
            return original(region, **kw)

        fake._handlers[("resiliencehubv2", "update-service")] = update
        with pytest.raises(AwsCliError, match="AccessDeniedException"):
            connect(fake)

    def test_a_stack_without_services_is_an_error(self):
        fake = FakeAws()
        fake.outputs["ServiceArns"] = ""
        with pytest.raises(devops_agent.DevOpsAgentError, match="no services"):
            connect(fake, "all")


class TestDisconnect:
    def connected(self):
        fake = FakeAws()
        fake.devops = {arn: {"agentSpaceArn": SPACE, "status": "CONNECTED"} for arn in ALL_ARNS}
        return fake

    def disconnect(self, fake):
        clock, lines = Clock(), []
        return devops_agent.disconnect(fake, environment(fake), lines.append, clock.sleep, clock), lines

    def test_it_sends_an_empty_configuration_to_every_connected_service(self):
        fake = self.connected()
        states, _ = self.disconnect(fake)
        assert not any(c.linked for c in states) and fake.devops == {}
        assert [(c[3]["service_arn"], c[3]["dev_ops_agent_configuration"]) for c in real_updates(fake)] == [(arn, {}) for arn in ALL_ARNS]

    def test_it_asks_for_no_findings_because_disconnecting_starts_nothing(self):
        fake = self.connected()
        self.disconnect(fake)
        assert ("resiliencehubv2", "list-failure-mode-findings") not in [(c[0], c[1]) for c in fake.calls]

    def test_a_failed_connection_is_disconnected_too(self):
        fake = FakeAws()
        fake.devops = {ARNS["ui"]: {"agentSpaceArn": SPACE, "status": "FAILED", "statusReason": "denied"}}
        states, _ = self.disconnect(fake)
        assert not states[0].linked and len(real_updates(fake)) == 1

    def test_services_with_no_connection_are_not_touched(self):
        fake = FakeAws()
        fake.devops = {ARNS["catalog"]: {"agentSpaceArn": SPACE, "status": "CONNECTED"}}
        self.disconnect(fake)
        assert updated_arns(fake) == [ARNS["catalog"]]

    def test_with_nothing_connected_it_says_so_and_writes_nothing(self):
        fake = FakeAws()
        states, lines = self.disconnect(fake)
        assert real_updates(fake) == [] and lines == ["No service is connected to an Agent Space."]

    def test_a_service_that_keeps_its_connection_is_reported_as_still_linked(self):
        fake = self.connected()
        fake.devops_stuck = {ARNS["checkout"]}
        states, _ = self.disconnect(fake)
        assert [c.linked for c in states] == [arn == ARNS["checkout"] for arn in ALL_ARNS]


class TestStatus:
    def test_it_reads_every_service_with_its_finding_count_and_writes_nothing(self):
        fake = FakeAws()
        fake.findings = {ARNS["orders"]: 21}
        fake.devops = {ARNS["ui"]: {"agentSpaceArn": SPACE, "status": "CONNECTED"},
                       ARNS["catalog"]: {"agentSpaceArn": SPACE, "status": "FAILED", "statusReason": "denied"}}
        states = devops_agent.status(fake, environment(fake))
        assert [c.status for c in states] == ["CONNECTED", "FAILED", "", "", "", ""]
        assert states[4].findings == 21 and states[0].findings == 3
        assert real_updates(fake) == []

    def test_each_line_names_the_service_its_status_the_reason_and_the_findings(self):
        fake = FakeAws()
        fake.devops = {ARNS["ui"]: {"agentSpaceArn": SPACE, "status": "FAILED", "statusReason": "denied"}}
        lines = devops_agent.render(devops_agent.status(fake, environment(fake)), findings=True)
        assert lines[0] == "  ui: FAILED (denied), 3 findings" and lines[1] == "  catalog: not connected, 3 findings"

    def test_without_findings_the_line_is_shorter(self):
        fake = FakeAws()
        assert devops_agent.render(devops_agent.status(fake, environment(fake)))[0] == "  ui: not connected"


class TestCommand:
    def test_connect_exits_zero_prints_each_services_state_and_the_cost_warning(self, capsys):
        fake = FakeAws()
        code, _ = run_cli(fake, "connect", "--space-arn", SPACE, "--services", "orders")
        out = capsys.readouterr()
        assert code == 0 and "orders: CONNECTED" in out.out and "orders: 3 findings" in out.out
        assert "3 in all" in out.out and out.err == ""

    def test_connect_without_services_is_refused_and_says_why(self, capsys):
        fake = FakeAws()
        code, _ = run_cli(fake, "connect", "--space-arn", SPACE)
        err = capsys.readouterr().err
        assert code == 1 and real_updates(fake) == [] and updates(fake) == []
        assert "connect needs --services" in err and "paid investigation for each finding" in err and "make devops-agent-status" in err

    def test_connect_with_blank_services_is_refused_too(self, capsys):
        code, _ = run_cli(FakeAws(), "connect", "--space-arn", SPACE, "--services", "   ")
        assert code == 1 and "connect needs --services" in capsys.readouterr().err

    def test_connect_without_an_agent_space_is_an_error(self, capsys):
        code, _ = run_cli(FakeAws(), "connect", "--services", "orders")
        assert code == 1 and "connect needs --space-arn" in capsys.readouterr().err

    def test_connect_exits_three_and_says_what_to_check_when_a_service_stays_unconnected(self, capsys):
        fake = FakeAws()
        fake.devops_scripts = [[DENIED]]
        code, _ = run_cli(fake, "connect", "--space-arn", SPACE, "--services", "orders cart")
        out = capsys.readouterr()
        assert code == 3
        assert "orders: FAILED (AccessDeniedException" in out.out
        assert "2 of 2 services are not connected: orders, cart" in out.err
        assert "invoker role" in out.err and "make devops-agent-connect again" in out.err

    def test_an_old_aws_cli_is_exit_one_with_the_install_advice(self, capsys):
        fake = FakeAws()
        fake.devops_cli_supported = False
        code, _ = run_cli(fake, "connect", "--space-arn", SPACE, "--services", "orders")
        assert code == 1 and "2.38.0" in capsys.readouterr().err

    def test_disconnect_exits_zero_when_every_service_is_off_its_agent_space(self, capsys):
        fake = FakeAws()
        fake.devops = {arn: {"agentSpaceArn": SPACE, "status": "CONNECTED"} for arn in ALL_ARNS}
        code, _ = run_cli(fake, "disconnect")
        assert code == 0 and "ui: not connected" in capsys.readouterr().out

    def test_disconnect_exits_three_for_a_service_that_keeps_it(self, capsys):
        fake = FakeAws()
        fake.devops = {arn: {"agentSpaceArn": SPACE, "status": "CONNECTED"} for arn in ALL_ARNS}
        fake.devops_stuck = {ARNS["ui"]}
        code, _ = run_cli(fake, "disconnect")
        assert code == 3 and "ui still name an Agent Space" in capsys.readouterr().err

    def test_status_exits_zero_even_when_a_connection_failed_and_shows_findings_and_the_cost_rule(self, capsys):
        fake = FakeAws()
        fake.devops = {ARNS["ui"]: {"agentSpaceArn": SPACE, "status": "FAILED", "statusReason": "denied"}}
        code, _ = run_cli(fake, "status")
        out = capsys.readouterr().out
        assert code == 0 and "ui: FAILED (denied), 3 findings" in out
        assert "starts one paid DevOps Agent investigation for each of its findings" in out

    def test_a_deployment_without_the_ngrh_stack_is_told_to_deploy_it(self, capsys):
        fake = FakeAws()
        fake.outputs = None
        code, _ = run_cli(fake, "status")
        assert code == 1 and "make ngrh" in capsys.readouterr().err

    def test_the_makefile_uses_every_action(self):
        assert devops_agent.ACTIONS == ("connect", "disconnect", "status")
        makefile = (DEPLOYMENT / "Makefile").read_text()
        for action in devops_agent.ACTIONS:
            assert f"ngrh_testing devops-agent {action} " in makefile or f"ngrh_testing devops-agent {action}\n" in makefile, action
