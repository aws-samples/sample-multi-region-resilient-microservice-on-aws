"""The Makefile side of the AWS DevOps Agent Agent Space: ``make ngrh``, ``devops-agent``, ``devops-agent-disconnect``,
``devops-agent-status``, ``destroy-ngrh`` and ``destroy-devops-agent``.

``make ngrh`` uses the first of these, and each is tested:

1. ``DEVOPS_AGENT_SPACE_ARN``, when it is set, even to nothing: the stack is given that value, and when it is
   nothing the services are disconnected first;
2. the Agent Space of the ``devops-agent<ENV>`` stack, when that stack exists;
3. none, in which case the deploy names no DevOps Agent parameter, so ``aws cloudformation deploy`` keeps the stack's
   value, and nothing is connected or disconnected. ``make deploy`` runs ``make ngrh``, so this is also what the
   e2e workflow gets and it must be exactly what ``make ngrh`` did before DevOps Agent.

``make ngrh`` never connects a service. Connecting starts one paid DevOps Agent investigation for each finding of
the service (21 for orders-dev; 111 for the six), so it is ``make devops-agent-connect``, which refuses unless
``DEVOPS_AGENT_SERVICES`` names the services and the ngrh stack has an Agent Space.

``make deploy DEVOPS_AGENT_CONNECT="orders"`` is the one command that does the deploy and then the connect. It is
opt-in, and TestDeployWithConnect pins what that means: the checks run first, before deploy-all, so a typo or an
AWS CLI too old to connect costs no deploy; the Agent Space, a wait for IAM and the connect follow the deploy; an
Agent Space you made yourself (``DEVOPS_AGENT_SPACE_ARN=<arn>``) is used and none is created; without the variable the
plan of ``make deploy`` is the one it was (that is also proved by hand, by comparing ``make -n deploy`` with the plan
of origin/main, which is identical), so the e2e workflow, which runs ``make deploy``, never connects. The deploy is
left out of these tests with ``make -o deploy-all``, because two hours of stacks are not what they are about.

The recipes run against a stub ``aws`` that keeps a few stacks (by name, with outputs and parameters) and a stub
``python3`` that records what the recipe asks ``python3 -m ngrh_testing`` to do (and a stub ``sleep`` that records the
wait and does not wait). What the tool then does is tests/test_ngrh_devops_agent_tool.py. Nothing reaches AWS.

Run with:  pytest tests/test_ngrh_devops_agent_make.py -v
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOYMENT = REPO / "deployment"
MAKEFILE = DEPLOYMENT / "Makefile"

SPACE = "arn:aws:aidevops:us-east-1:123456789012:agentspace/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
OWN_SPACE = "arn:aws:aidevops:us-east-1:123456789012:agentspace/11111111-2222-3333-4444-555555555555"
CARTS_STACK = "arn:aws:cloudformation:us-east-1:123456789012:stack/carts-db-stack-t/11111111-2222-3333-4444-555555555555"

BASE_OVERRIDES = {"Env": "-t", "PrimaryRegion": "us-east-1", "StandbyRegion": "us-west-2", "CartsDbStackArn": CARTS_STACK}

# A stub `aws`: every call is logged. It keeps the stacks named in STUB_STACKS (JSON: name -> {"outputs": {...},
# "parameters": {...}}), answers describe-stacks the way the CLI does for the --query the Makefile asks, and fails
# like the CLI for a stack that is not there. Deleting a stack removes it, so later calls see it gone.
STUB_AWS = r'''#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(["aws"] + args) + "\n")
stacks_path = os.environ["STUB_STACKS"]
stacks = json.load(open(stacks_path))


def option(name):
    return args[args.index(name) + 1] if name in args else None


if args[:2] == ["sts", "get-caller-identity"]:
    print("123456789012\tarn:aws:iam::123456789012:user/stub\tAIDASTUB")
elif args[:2] == ["cloudformation", "describe-stacks"]:
    name = option("--stack-name")
    if name not in stacks:
        sys.stderr.write("An error occurred (ValidationError) when calling the DescribeStacks operation: Stack with id %s does not exist\n" % name)
        sys.exit(254)
    query = option("--query") or ""
    stack = stacks[name]
    # What the real CLI prints with --output text (checked against test2, 2026-10-16): nothing at all when the list
    # is there and has no match, and None when the whole list is missing from the stack, as Outputs is for a stack
    # that failed to create.
    if query == "Stacks[0].StackId":
        print(stack.get("id", ""))
    elif "Outputs[?OutputKey==" in query:
        key = query.split("'")[1]
        outputs = stack.get("outputs")
        if outputs:
            if key in outputs:
                print(outputs[key])
        else:
            print("None")
    elif "Parameters[?ParameterKey==" in query:
        key = query.split("'")[1]
        parameters = stack.get("parameters")
        if parameters is None:
            print("None")
        elif key in parameters:
            print(parameters[key])
elif args[:2] == ["cloudformation", "delete-stack"]:
    stacks.pop(option("--stack-name"), None)
    json.dump(stacks, open(stacks_path, "w"))
elif args[:2] == ["cloudformation", "deploy"]:
    sys.exit(int(os.environ.get("STUB_DEPLOY_EXIT", "0")))
sys.exit(0)
'''

# A stub `python3`: `python3 -m ngrh_testing devops-agent <action> ...` is logged and succeeds, or exits with
# STUB_TOOL_EXIT_<ACTION> (one action) or STUB_TOOL_EXIT (all of them). `delete-tests` (destroy-ngrh runs it) is logged
# and succeeds. Anything else is a mistake in the recipe. It is Python so the log line is encoded by json.dumps, whatever
# the arguments contain.
STUB_PYTHON = r'''#!PYTHON
import json
import os
import sys

args = sys.argv[1:]
if args[:2] == ["-m", "ngrh_testing"] and args[2:3] == ["devops-agent"]:
    with open(os.environ["STUB_LOG"], "a") as log:
        log.write(json.dumps(["python3"] + args) + "\n")
    action = args[3] if len(args) > 3 else ""
    sys.exit(int(os.environ.get("STUB_TOOL_EXIT_" + action.upper(), os.environ.get("STUB_TOOL_EXIT", "0"))))
if args[:2] == ["-m", "ngrh_testing"] and args[2:3] == ["delete-tests"]:
    sys.exit(0)
sys.stderr.write("unexpected python3 call: %s\n" % " ".join(args))
sys.exit(99)
'''

# A stub `sleep`: the wait is logged and not waited, so a test can see that the Makefile waited and for how long.
STUB_SLEEP = r'''#!PYTHON
import json
import os
import sys

with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(["sleep"] + sys.argv[1:]) + "\n")
'''


class Run:
    def __init__(self, done, calls, stacks):
        self.done, self.calls, self.stacks = done, calls, stacks

    @property
    def out(self):
        return self.done.stdout + self.done.stderr

    def aws(self, *prefix):
        return [c for c in self.calls if c[0] == "aws" and c[1:1 + len(prefix)] == list(prefix)]

    def tool(self):
        """What the recipe asked the tool to do: [(action, space ARN or None)]."""
        out = []
        for call in self.calls:
            if call[0] == "python3":
                action = call[call.index("devops-agent") + 1]
                out.append((action, call[call.index("--space-arn") + 1] if "--space-arn" in call else None))
        return out

    def deploy_overrides(self, stack="ngrh-t"):
        (deploy,) = [c for c in self.calls if c[:3] == ["aws", "cloudformation", "deploy"] and c[c.index("--stack-name") + 1] == stack]
        return dict(item.split("=", 1) for item in deploy[deploy.index("--parameter-overrides") + 1:])

    def order(self):
        """The calls that matter, in order: deploys and deletes, the tool's actions, and waits."""
        out = []
        for call in self.calls:
            if call[0] == "sleep":
                out.append("sleep:" + call[1])
            elif call[0] == "python3":
                out.append("tool:" + call[call.index("devops-agent") + 1])
            elif call[:3] == ["aws", "cloudformation", "deploy"]:
                out.append("deploy:" + call[call.index("--stack-name") + 1])
            elif call[:3] == ["aws", "cloudformation", "delete-stack"]:
                out.append("delete:" + call[call.index("--stack-name") + 1])
        return out


def make(tmp_path, *targets, stacks=None, variables=(), in_environment=None, tool_exit=0, deploy_exit=0,
         in_services=None, services_name="DEVOPS_AGENT_SERVICES", tool_exits=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (("aws", STUB_AWS), ("python3", STUB_PYTHON), ("sleep", STUB_SLEEP)):
        stub = bin_dir / name
        # The stubs are Python scripts: run them with the interpreter running the tests, not whatever python3 is on PATH.
        stub.write_text(body.replace("#!/usr/bin/env python3", f"#!{sys.executable}").replace("#!PYTHON", f"#!{sys.executable}"))
        stub.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    stacks_file = tmp_path / "stacks.json"
    stacks_file.write_text(json.dumps({"carts-db-stack-t": {"id": CARTS_STACK}, **(stacks or {})}))
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", STUB_LOG=str(log), STUB_STACKS=str(stacks_file),
               STUB_TOOL_EXIT=str(tool_exit), STUB_DEPLOY_EXIT=str(deploy_exit))
    env.pop("DEVOPS_AGENT_SPACE_ARN", None)  # a value exported in the developer's shell must not change the result
    env.pop("DEVOPS_AGENT_SERVICES", None)
    env.pop("DEVOPS_AGENT_CONNECT", None)
    env.pop("DEVOPS_AGENT_IAM_WAIT", None)
    env.pop("SERVICES", None)
    for action, code in (tool_exits or {}).items():
        env["STUB_TOOL_EXIT_" + action.upper()] = str(code)
    if in_services is not None:
        env[services_name] = in_services
    if in_environment is not None:
        env["DEVOPS_AGENT_SPACE_ARN"] = in_environment
    command = ["make", "-C", str(DEPLOYMENT), *targets, "ENV=-t", "PRIMARY_REGION=us-east-1", "STANDBY_REGION=us-west-2", *variables]
    done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=120)
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return Run(done, calls, json.loads(stacks_file.read_text()))


def agent_space_stack(arn=SPACE):
    return {"devops-agent-t": {"outputs": {"AgentSpaceArn": arn}}}


def ngrh_stack(space=None):
    return {"ngrh-t": {"parameters": {"DevOpsAgentSpaceArn": space} if space is not None else {}}}


# ---------------------------------------------------------------------------
# make ngrh, case 3: no Agent Space anywhere. This is what make deploy and the e2e workflow get.
# ---------------------------------------------------------------------------

class TestNgrhWithoutAnAgentSpace:
    def test_the_deploy_names_no_devops_agent_parameter_so_the_stack_keeps_what_it_has(self, tmp_path):
        run = make(tmp_path, "ngrh")
        assert run.done.returncode == 0, run.out
        assert run.deploy_overrides() == BASE_OVERRIDES

    def test_it_connects_and_disconnects_nothing(self, tmp_path):
        run = make(tmp_path, "ngrh")
        assert run.tool() == []

    def test_it_only_looks_for_the_devops_agent_stack_and_fails_for_no_reason_when_it_is_not_there(self, tmp_path):
        run = make(tmp_path, "ngrh")
        lookups = [c[c.index("--stack-name") + 1] for c in run.aws("cloudformation", "describe-stacks")]
        assert lookups == ["carts-db-stack-t", "devops-agent-t"]
        assert run.done.returncode == 0 and "does not exist" not in run.out

    def test_a_devops_agent_stack_without_the_output_decides_nothing(self, tmp_path):
        # A stack that failed to create has no Outputs at all, and the CLI prints None for that.
        run = make(tmp_path, "ngrh", stacks={"devops-agent-t": {"outputs": {}}})
        assert run.done.returncode == 0, run.out
        assert run.deploy_overrides() == BASE_OVERRIDES and run.tool() == []

    def test_a_devops_agent_stack_with_other_outputs_but_not_the_agent_space_decides_nothing(self, tmp_path):
        # The list is there and has no match: the CLI prints nothing at all.
        run = make(tmp_path, "ngrh", stacks={"devops-agent-t": {"outputs": {"AgentSpaceId": "abc"}}})
        assert run.done.returncode == 0, run.out
        assert run.deploy_overrides() == BASE_OVERRIDES and run.tool() == []

    def test_the_final_message_is_the_one_it_always_was(self, tmp_path):
        run = make(tmp_path, "ngrh")
        assert "NGRH model deployed (stack ngrh-t). Run 'make ngrh-assess' to run failure-mode assessments." in run.done.stdout


# ---------------------------------------------------------------------------
# make ngrh, case 2: the Agent Space of the devops-agent<ENV> stack
# ---------------------------------------------------------------------------

class TestNgrhWithTheAgentSpaceStack:
    def test_it_gives_the_stacks_agent_space_to_the_deploy(self, tmp_path):
        run = make(tmp_path, "ngrh", stacks=agent_space_stack())
        assert run.done.returncode == 0, run.out
        assert run.deploy_overrides() == {**BASE_OVERRIDES, "DevOpsAgentSpaceArn": SPACE}

    def test_it_never_connects_a_service(self, tmp_path):
        # Connecting starts a paid investigation for every finding of the service: it is its own step.
        run = make(tmp_path, "ngrh", stacks=agent_space_stack())
        assert run.tool() == [] and run.order() == ["deploy:ngrh-t"]

    def test_the_variable_wins_over_the_stack(self, tmp_path):
        run = make(tmp_path, "ngrh", stacks=agent_space_stack(), variables=[f"DEVOPS_AGENT_SPACE_ARN={OWN_SPACE}"])
        assert run.deploy_overrides()["DevOpsAgentSpaceArn"] == OWN_SPACE
        assert [c[c.index("--stack-name") + 1] for c in run.aws("cloudformation", "describe-stacks")] == ["carts-db-stack-t"], \
            "with the variable set the devops-agent stack is not even looked at"

    def test_a_failed_deploy_fails_make(self, tmp_path):
        run = make(tmp_path, "ngrh", stacks=agent_space_stack(), deploy_exit=1)
        assert run.done.returncode != 0 and "NGRH model deployed" not in run.done.stdout


# ---------------------------------------------------------------------------
# make ngrh, case 1: DEVOPS_AGENT_SPACE_ARN
# ---------------------------------------------------------------------------

class TestNgrhWithTheVariable:
    def test_on_the_command_line_it_gives_the_arn_to_the_deploy(self, tmp_path):
        run = make(tmp_path, "ngrh", variables=[f"DEVOPS_AGENT_SPACE_ARN={OWN_SPACE}"])
        assert run.done.returncode == 0, run.out
        assert run.deploy_overrides() == {**BASE_OVERRIDES, "DevOpsAgentSpaceArn": OWN_SPACE}
        assert run.tool() == []

    def test_in_the_environment_it_does_the_same(self, tmp_path):
        run = make(tmp_path, "ngrh", in_environment=OWN_SPACE)
        assert run.deploy_overrides()["DevOpsAgentSpaceArn"] == OWN_SPACE and run.tool() == []

    def test_set_to_nothing_it_passes_an_empty_value_which_takes_the_access_away(self, tmp_path):
        run = make(tmp_path, "ngrh", stacks=ngrh_stack(SPACE), variables=["DEVOPS_AGENT_SPACE_ARN="])
        assert run.done.returncode == 0, run.out
        assert run.deploy_overrides() == {**BASE_OVERRIDES, "DevOpsAgentSpaceArn": ""}

    def test_set_to_nothing_in_the_environment_does_the_same(self, tmp_path):
        run = make(tmp_path, "ngrh", stacks=ngrh_stack(SPACE), in_environment="")
        assert run.deploy_overrides()["DevOpsAgentSpaceArn"] == ""

    def test_set_to_nothing_it_disconnects_the_services_before_the_deploy_removes_the_access(self, tmp_path):
        run = make(tmp_path, "ngrh", stacks=ngrh_stack(SPACE), variables=["DEVOPS_AGENT_SPACE_ARN="])
        assert run.order() == ["tool:disconnect", "deploy:ngrh-t"]

    def test_set_to_nothing_even_a_stack_that_exists_does_not_bring_the_agent_space_back(self, tmp_path):
        run = make(tmp_path, "ngrh", stacks={**ngrh_stack(SPACE), **agent_space_stack()}, variables=["DEVOPS_AGENT_SPACE_ARN="])
        assert run.deploy_overrides()["DevOpsAgentSpaceArn"] == "" and run.tool() == [("disconnect", None)]

    def test_set_to_nothing_on_a_first_deploy_there_is_nothing_to_disconnect(self, tmp_path):
        run = make(tmp_path, "ngrh", variables=["DEVOPS_AGENT_SPACE_ARN="])
        assert run.done.returncode == 0, run.out
        assert run.tool() == [] and run.deploy_overrides()["DevOpsAgentSpaceArn"] == ""

    def test_a_failed_disconnect_stops_make_before_the_access_is_taken_away(self, tmp_path):
        run = make(tmp_path, "ngrh", stacks=ngrh_stack(SPACE), variables=["DEVOPS_AGENT_SPACE_ARN="], tool_exit=1)
        assert run.done.returncode != 0 and run.order() == ["tool:disconnect"]

    def test_a_value_full_of_shell_syntax_arrives_as_one_argument_and_runs_nothing(self, tmp_path):
        # The value reaches the shell as data, never as text to parse: CloudFormation then refuses it by pattern.
        ran = tmp_path / "ran"
        hostile = f"x y; touch {ran} `touch {ran}` \"q\" 's' *"
        run = make(tmp_path, "ngrh", variables=[f"DEVOPS_AGENT_SPACE_ARN={hostile}"])
        assert run.done.returncode == 0, run.out
        assert run.deploy_overrides()["DevOpsAgentSpaceArn"] == hostile
        assert not ran.exists()


# ---------------------------------------------------------------------------
# make devops-agent
# ---------------------------------------------------------------------------

class TestDevOpsAgent:
    def test_it_deploys_the_agent_space_stack_in_the_primary_region_with_the_env(self, tmp_path):
        run = make(tmp_path, "devops-agent")
        assert run.done.returncode == 0, run.out
        (deploy,) = [c for c in run.calls if c[:3] == ["aws", "cloudformation", "deploy"]]
        assert deploy[deploy.index("--template") + 1] == "./devops-agent.yaml"
        assert deploy[deploy.index("--stack-name") + 1] == "devops-agent-t"
        assert deploy[deploy.index("--region") + 1] == "us-east-1"
        assert deploy[deploy.index("--capabilities") + 1] == "CAPABILITY_NAMED_IAM"
        assert deploy[deploy.index("--parameter-overrides") + 1:] == ["Env=-t"]

    def test_without_the_ngrh_stack_it_stops_there_and_says_what_to_run_next(self, tmp_path):
        run = make(tmp_path, "devops-agent")
        assert run.order() == ["deploy:devops-agent-t"]
        assert "Deploy the NGRH model (make ngrh) to give the invoker role access to it." in run.done.stdout

    def test_it_says_the_stack_is_deployed_not_that_it_was_created_because_a_rerun_changes_nothing(self, tmp_path):
        # Run twice on 2026-10-16, the second run printed "No changes to deploy" and then "Agent Space created".
        for stacks in ({}, {**ngrh_stack(), **agent_space_stack()}):
            run = make(tmp_path / str(len(stacks)), "devops-agent", stacks=stacks)
            assert "Agent Space stack devops-agent-t is deployed." in run.done.stdout
            assert "created" not in run.done.stdout

    def test_with_the_ngrh_stack_it_runs_make_ngrh_which_finds_the_new_agent_space(self, tmp_path):
        # The stub keeps no state across a deploy, so the Agent Space stack is there from the start, as it would be after the deploy.
        run = make(tmp_path, "devops-agent", stacks={**ngrh_stack(), **agent_space_stack()})
        assert run.done.returncode == 0, run.out
        assert run.order() == ["deploy:devops-agent-t", "deploy:ngrh-t"]
        assert run.deploy_overrides("ngrh-t")["DevOpsAgentSpaceArn"] == SPACE

    def test_it_connects_no_service_and_tells_you_the_next_step_and_what_it_costs(self, tmp_path):
        run = make(tmp_path, "devops-agent", stacks={**ngrh_stack(), **agent_space_stack()})
        assert run.tool() == []
        assert "make devops-agent-status" in run.done.stdout and "make devops-agent-connect" in run.done.stdout
        assert "paid investigation" in run.done.stdout

    def test_a_failed_wiring_fails_make_devops_agent(self, tmp_path):
        run = make(tmp_path, "devops-agent", stacks={**ngrh_stack(), **agent_space_stack()}, deploy_exit=1)
        assert run.done.returncode != 0


# ---------------------------------------------------------------------------
# make devops-agent-connect, -disconnect and -status
# ---------------------------------------------------------------------------

class TestConnect:
    def connect(self, tmp_path, services="orders cart", **kw):
        variables = [f"DEVOPS_AGENT_SERVICES={services}"] if services is not None else []
        return make(tmp_path, "devops-agent-connect", stacks=ngrh_stack(SPACE), variables=variables, **kw)

    def test_it_asks_the_tool_to_connect_the_named_services_to_the_agent_space_the_stack_has(self, tmp_path):
        run = self.connect(tmp_path)
        assert run.done.returncode == 0, run.out
        (call,) = [c for c in run.calls if c[0] == "python3"]
        assert call[1:] == ["-m", "ngrh_testing", "devops-agent", "connect", "--primary-region", "us-east-1",
                            "--standby-region", "us-west-2", "--env=-t", "--space-arn", SPACE, "--services", "orders cart"]

    def test_it_deploys_and_changes_nothing_in_cloudformation(self, tmp_path):
        run = self.connect(tmp_path)
        assert run.order() == ["tool:connect"]

    def test_without_the_services_named_it_refuses_and_says_what_it_costs(self, tmp_path):
        run = self.connect(tmp_path, services=None)
        assert run.done.returncode != 0 and run.tool() == []
        assert "name the services to connect" in run.out and "paid DevOps Agent investigation" in run.out
        assert "make devops-agent-status" in run.out

    def test_with_blank_services_it_refuses_too(self, tmp_path):
        run = self.connect(tmp_path, services="")
        assert run.done.returncode != 0 and run.tool() == [] and "name the services to connect" in run.out

    def test_without_an_agent_space_on_the_ngrh_stack_it_refuses_and_says_how_to_get_one(self, tmp_path):
        for index, stacks in enumerate(({}, {"ngrh-t": {}}, ngrh_stack(), ngrh_stack(""))):
            run = make(tmp_path / str(index), "devops-agent-connect", stacks=stacks, variables=["DEVOPS_AGENT_SERVICES=orders"])
            assert run.done.returncode != 0 and run.tool() == [], stacks
            assert "has no Agent Space" in run.out and "make devops-agent" in run.out

    def test_a_services_variable_in_the_environment_connects_nothing_on_other_targets(self, tmp_path):
        # DEVOPS_AGENT_SERVICES is read only by devops-agent-connect: a value left in the shell cannot start investigations.
        run = make(tmp_path, "ngrh", stacks=agent_space_stack(), in_services="orders")
        assert run.tool() == [] and run.order() == ["deploy:ngrh-t"]

    def test_the_variable_has_its_own_name_so_a_stray_services_in_the_shell_cannot_trigger_it(self, tmp_path):
        run = make(tmp_path, "devops-agent-connect", stacks=ngrh_stack(SPACE), in_services="orders", services_name="SERVICES")
        assert run.done.returncode != 0 and run.tool() == []

    def test_a_value_full_of_shell_syntax_arrives_as_one_argument_and_runs_nothing(self, tmp_path):
        ran = tmp_path / "ran"
        hostile = f"orders; touch {ran} `touch {ran}` \"q\" 's' *"
        run = self.connect(tmp_path, services=hostile)
        (call,) = [c for c in run.calls if c[0] == "python3"]
        assert call[call.index("--services") + 1] == hostile and not ran.exists()

    def test_a_failing_tool_fails_make(self, tmp_path):
        run = self.connect(tmp_path, tool_exit=3)
        assert run.done.returncode != 0


class TestDisconnectAndStatus:
    def test_disconnect_asks_the_tool_when_the_ngrh_stack_has_an_agent_space(self, tmp_path):
        run = make(tmp_path, "devops-agent-disconnect", stacks=ngrh_stack(SPACE))
        assert run.done.returncode == 0, run.out
        assert run.tool() == [("disconnect", None)]

    @pytest.mark.parametrize("stacks", [{}, {"ngrh-t": {}}, ngrh_stack(), ngrh_stack("")],
                             ids=["no ngrh stack", "no parameters at all (None)", "no such parameter (nothing printed)", "empty parameter"])
    def test_disconnect_does_nothing_when_the_ngrh_stack_has_no_agent_space(self, tmp_path, stacks):
        # Safe on any deployment, which is how destroy-ngrh can run it: it never needs the new AWS CLI.
        run = make(tmp_path, "devops-agent-disconnect", stacks=stacks)
        assert run.done.returncode == 0, run.out
        assert run.tool() == [] and "no Agent Space to disconnect from" in run.done.stdout

    def test_status_asks_the_tool(self, tmp_path):
        run = make(tmp_path, "devops-agent-status")
        (call,) = [c for c in run.calls if c[0] == "python3"]
        assert call[1:] == ["-m", "ngrh_testing", "devops-agent", "status", "--primary-region", "us-east-1",
                            "--standby-region", "us-west-2", "--env=-t"]


# ---------------------------------------------------------------------------
# make deploy DEVOPS_AGENT_CONNECT=<services>
# ---------------------------------------------------------------------------

def deploy(tmp_path, *variables, stacks=None, wait="0", **kw):
    """make deploy with deploy-all left out (-o): its two hours of stacks are not what these tests are about. The stubs hold
    what a finished deploy leaves, the ngrh stack with the Agent Space the second make ngrh gave it, because they keep no
    state from one call to the next. ``wait`` is DEVOPS_AGENT_IAM_WAIT; None leaves the Makefile's own."""
    state = {**ngrh_stack(SPACE), **agent_space_stack()} if stacks is None else stacks
    extra = [] if wait is None else [f"DEVOPS_AGENT_IAM_WAIT={wait}"]
    return make(tmp_path, "-o", "deploy-all", "deploy", stacks=state, variables=[*extra, *variables], **kw)


def prerequisites_of_deploy(tmp_path, *variables):
    """What make itself says the prerequisites of deploy are (make -pq prints its database): the Makefile's line has a variable in it."""
    run = make(tmp_path, "-pq", "deploy", variables=list(variables))
    match = re.search(r"^deploy: (.*)$", run.done.stdout, re.M)
    assert match, run.out[-2000:]
    return match.group(1).split()


def tool_calls(run):
    return [c for c in run.calls if c[0] == "python3"]


class TestDeployWithConnect:
    """``make deploy DEVOPS_AGENT_CONNECT="orders"`` is the one-command way to use DevOps Agent: the checks first, before
    anything is deployed, then, after the deploy, the Agent Space and the connection. Without the variable ``make deploy``
    is what it was, which is what the e2e workflow runs."""

    def test_without_the_variable_deploy_does_none_of_it(self, tmp_path):
        run = deploy(tmp_path, stacks={})
        assert run.done.returncode == 0, run.out
        assert run.order() == [] and "Deployment Outputs" in run.done.stdout

    @pytest.mark.parametrize("value", ["", "   "])
    def test_a_blank_variable_is_the_same_as_none(self, tmp_path, value):
        run = deploy(tmp_path, f"DEVOPS_AGENT_CONNECT={value}", stacks={})
        assert run.done.returncode == 0, run.out
        assert run.order() == []

    @pytest.mark.parametrize("value", ["", "   ", " \t "])
    def test_a_blank_variable_in_the_environment_is_the_same_as_none(self, tmp_path, value):
        # make drops the spaces of a blank value given on its command line, but takes one from the environment as it is.
        run = deploy(tmp_path, in_services=value, services_name="DEVOPS_AGENT_CONNECT", stacks={})
        assert run.done.returncode == 0, run.out
        assert run.order() == []

    def test_without_the_variable_deploy_still_has_deploy_all_as_its_only_prerequisite(self, tmp_path):
        assert prerequisites_of_deploy(tmp_path) == ["deploy-all"]

    def test_with_the_variable_the_checks_are_a_prerequisite_listed_before_deploy_all(self, tmp_path):
        # make builds prerequisites left to right, as it does for the targets of deploy-all itself.
        assert prerequisites_of_deploy(tmp_path, "DEVOPS_AGENT_CONNECT=orders") == ["devops-agent-connect-preflight", "deploy-all"]

    def test_the_checks_run_first_then_the_agent_space_then_a_wait_for_iam_and_then_the_connection(self, tmp_path):
        run = deploy(tmp_path, "DEVOPS_AGENT_CONNECT=orders")
        assert run.done.returncode == 0, run.out
        assert run.order() == ["tool:check", "deploy:devops-agent-t", "deploy:ngrh-t", "sleep:0", "tool:connect"]

    def test_it_connects_the_services_it_checked_to_the_agent_space_the_ngrh_stack_has(self, tmp_path):
        run = deploy(tmp_path, "DEVOPS_AGENT_CONNECT=orders cart")
        check, connect = tool_calls(run)
        assert check[1:] == ["-m", "ngrh_testing", "devops-agent", "check", "--primary-region", "us-east-1",
                             "--standby-region", "us-west-2", "--env=-t", "--services", "orders cart"]
        assert connect[1:] == ["-m", "ngrh_testing", "devops-agent", "connect", "--primary-region", "us-east-1",
                               "--standby-region", "us-west-2", "--env=-t", "--space-arn", SPACE, "--services", "orders cart"]

    def test_the_variable_in_the_environment_does_the_same(self, tmp_path):
        run = deploy(tmp_path, in_services="orders", services_name="DEVOPS_AGENT_CONNECT")
        assert run.order() == ["tool:check", "deploy:devops-agent-t", "deploy:ngrh-t", "sleep:0", "tool:connect"]

    def test_the_value_reaches_the_recipes_even_when_a_makefile_sets_it(self, tmp_path):
        # make exports what came from the command line or the environment by itself, but not a variable a makefile sets: the
        # Makefile's own export is what carries it to the shell, so a local include (-include local.mk) works too.
        local = tmp_path / "local.mk"
        local.write_text("DEVOPS_AGENT_CONNECT = orders cart\n")
        run = make(tmp_path, "-f", str(local), "-f", str(MAKEFILE), "-o", "deploy-all", "deploy",
                   stacks={**ngrh_stack(SPACE), **agent_space_stack()}, variables=["DEVOPS_AGENT_IAM_WAIT=0"])
        assert run.done.returncode == 0, run.out
        assert [c[c.index("--services") + 1] for c in tool_calls(run)] == ["orders cart", "orders cart"]

    def test_all_is_passed_on_for_the_tool_to_expand(self, tmp_path):
        run = deploy(tmp_path, "DEVOPS_AGENT_CONNECT=all")
        assert [c[c.index("--services") + 1] for c in tool_calls(run)] == ["all", "all"]

    def test_the_wait_for_iam_is_a_minute_unless_told_otherwise_and_says_why(self, tmp_path):
        # A connection that cannot use the invoker role's new policy yet ends FAILED and is not sent again, because each
        # send can start paid investigations: so the step waits first, which costs a minute in a deploy of two hours.
        run = deploy(tmp_path, "DEVOPS_AGENT_CONNECT=orders", wait=None)
        assert "sleep:60" in run.order()
        assert "Waiting 60 seconds for IAM" in run.done.stdout

    def test_the_names_reach_both_tool_calls_as_one_argument_whatever_they_contain(self, tmp_path):
        ran = tmp_path / "ran"
        hostile = f"orders; touch {ran} `touch {ran}` \"q\" 's' *"
        run = deploy(tmp_path, f"DEVOPS_AGENT_CONNECT={hostile}")
        assert [c[c.index("--services") + 1] for c in tool_calls(run)] == [hostile, hostile]
        assert not ran.exists()

    def test_a_failing_check_stops_everything_and_deploy_all_never_starts(self, tmp_path):
        run = deploy(tmp_path, "DEVOPS_AGENT_CONNECT=orderz", tool_exits={"check": 1})
        assert run.done.returncode != 0
        assert run.order() == ["tool:check"] and "Deployment Outputs" not in run.done.stdout

    def test_a_failed_agent_space_stack_stops_before_the_connection(self, tmp_path):
        run = deploy(tmp_path, "DEVOPS_AGENT_CONNECT=orders", deploy_exit=1)
        assert run.done.returncode != 0
        assert run.order() == ["tool:check", "deploy:devops-agent-t"]

    def test_a_failed_connection_fails_make_but_only_after_the_outputs_were_printed(self, tmp_path):
        run = deploy(tmp_path, "DEVOPS_AGENT_CONNECT=orders", tool_exits={"connect": 3})
        assert run.done.returncode != 0
        assert run.order()[-1] == "tool:connect"
        assert run.done.stdout.index("Deployment Outputs") < run.done.stdout.index("Agent Space stack devops-agent-t is deployed")

    def test_with_an_agent_space_you_made_none_is_created_and_the_services_connect_to_yours(self, tmp_path):
        run = deploy(tmp_path, "DEVOPS_AGENT_CONNECT=orders", f"DEVOPS_AGENT_SPACE_ARN={OWN_SPACE}", stacks=ngrh_stack(OWN_SPACE))
        assert run.order() == ["tool:check", "sleep:0", "tool:connect"]
        connect = tool_calls(run)[-1]
        assert connect[connect.index("--space-arn") + 1] == OWN_SPACE

    def test_an_agent_space_variable_set_to_nothing_cannot_go_with_connecting_and_nothing_runs(self, tmp_path):
        # Set to nothing it takes the access away; connecting needs it. Found before the deploy, not after.
        run = deploy(tmp_path, "DEVOPS_AGENT_CONNECT=orders", "DEVOPS_AGENT_SPACE_ARN=", stacks=ngrh_stack(SPACE))
        assert run.done.returncode != 0 and run.order() == []
        assert "DEVOPS_AGENT_CONNECT" in run.out and "DEVOPS_AGENT_SPACE_ARN" in run.out

    def test_the_variable_means_nothing_to_the_other_targets(self, tmp_path):
        # Only make deploy reads it. ngrh, devops-agent and devops-agent-connect do what they always did.
        run = make(tmp_path, "ngrh", stacks=agent_space_stack(), variables=["DEVOPS_AGENT_CONNECT=orders"])
        assert run.order() == ["deploy:ngrh-t"]
        run = make(tmp_path / "connect", "devops-agent-connect", stacks=ngrh_stack(SPACE), variables=["DEVOPS_AGENT_CONNECT=orders"])
        assert run.done.returncode != 0 and run.tool() == [] and "name the services to connect" in run.out
        run = make(tmp_path / "space", "devops-agent", variables=["DEVOPS_AGENT_CONNECT=orders"])
        assert run.order() == ["deploy:devops-agent-t"]

    def test_make_dash_n_shows_the_steps_and_runs_none_of_them(self, tmp_path):
        # make runs a recipe line that has $(MAKE) in it even under -n, which would deploy and connect for real. The steps are
        # kept in a variable, so the recipe line has no $(MAKE) in it and -n only prints them.
        run = make(tmp_path, "-n", "-o", "deploy-all", "deploy", stacks={**ngrh_stack(SPACE), **agent_space_stack()},
                   variables=["DEVOPS_AGENT_CONNECT=orders", "DEVOPS_AGENT_IAM_WAIT=0"])
        assert run.done.returncode == 0, run.out
        assert run.order() == [], "nothing was run"
        assert "devops-agent-connect" in run.done.stdout and "devops-agent" in run.done.stdout

    def test_without_the_variable_the_whole_dry_run_plan_of_deploy_has_no_trace_of_the_new_steps(self, tmp_path):
        # The plan is every recipe of the deploy, about 300 lines. It does mention the devops-agent stack (make ngrh looks for it) and
        # has waits and sleeps of its own, so the checks are for the new steps: the tool's check, the connect, the wait for IAM.
        run = make(tmp_path, "-n", "deploy", stacks={})
        assert run.done.returncode == 0, run.out
        plan = run.done.stdout
        assert "Deployment Outputs" in plan and len(plan.splitlines()) > 100
        for trace in ("ngrh_testing devops-agent", "devops-agent-connect", "DEVOPS_AGENT_CONNECT", "for IAM to give the invoker role its new policy"):
            assert trace not in plan, trace
        assert run.order() == []

    def test_the_makefile_documents_the_wait_and_its_default(self):
        assert re.search(r"^DEVOPS_AGENT_IAM_WAIT \?= 60$", MAKEFILE.read_text(), re.M)

    def test_the_e2e_workflow_never_sets_it(self):
        # The e2e deploy runs make deploy for a run of about four hours in the CI account: a connection there would start
        # paid investigations on every run.
        workflow = (REPO / ".github" / "workflows" / "e2e.yml").read_text()
        assert "DEVOPS_AGENT_CONNECT" not in workflow and "make deploy" in workflow



# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------

class TestDestroyDevOpsAgent:
    def test_it_deletes_the_stack_and_waits(self, tmp_path):
        run = make(tmp_path, "destroy-devops-agent", stacks=agent_space_stack())
        assert run.done.returncode == 0, run.out
        assert run.order() == ["delete:devops-agent-t"] and "devops-agent-t" not in run.stacks
        (wait,) = run.aws("cloudformation", "wait", "stack-delete-complete")
        assert wait[wait.index("--stack-name") + 1] == "devops-agent-t"

    def test_no_stack_is_not_an_error(self, tmp_path):
        run = make(tmp_path, "destroy-devops-agent")
        assert run.done.returncode == 0, run.out
        assert run.order() == [] and "nothing to delete" in run.done.stdout

    def test_a_failed_stack_with_no_outputs_is_still_deleted(self, tmp_path):
        run = make(tmp_path, "destroy-devops-agent", stacks={"devops-agent-t": {"outputs": {}}})
        assert run.done.returncode == 0 and run.order() == ["delete:devops-agent-t"]

    def test_it_refuses_while_the_ngrh_stack_still_uses_the_agent_space(self, tmp_path):
        run = make(tmp_path, "destroy-devops-agent", stacks={**agent_space_stack(), **ngrh_stack(SPACE)})
        assert run.done.returncode != 0
        assert "still uses this Agent Space" in run.out and "make ngrh DEVOPS_AGENT_SPACE_ARN=" in run.out
        assert run.order() == [] and "devops-agent-t" in run.stacks

    @pytest.mark.parametrize("ngrh", [ngrh_stack(), ngrh_stack(""), ngrh_stack(OWN_SPACE)],
                             ids=["no parameter", "unwired", "another agent space"])
    def test_it_deletes_when_the_ngrh_stack_does_not_use_this_agent_space(self, tmp_path, ngrh):
        run = make(tmp_path, "destroy-devops-agent", stacks={**agent_space_stack(), **ngrh})
        assert run.done.returncode == 0, run.out
        assert run.order() == ["delete:devops-agent-t"]


class TestDestroyNgrh:
    def test_a_stack_with_an_agent_space_has_its_services_disconnected_first_and_best_effort(self, tmp_path):
        run = make(tmp_path, "destroy-ngrh", stacks={"ngrh-t": {"parameters": {"DevOpsAgentSpaceArn": SPACE}, "outputs": {}}}, tool_exit=1)
        assert run.tool() == [("disconnect", None)], "it tried"
        assert run.order()[:1] == ["tool:disconnect"]
        # a failing disconnect (an old AWS CLI, say) must not stop the teardown
        assert "delete:ngrh-t" in run.order(), run.out

    def test_a_stack_without_one_never_runs_the_tool_so_an_old_aws_cli_still_works(self, tmp_path):
        run = make(tmp_path, "destroy-ngrh", stacks={"ngrh-t": {"parameters": {}, "outputs": {}}})
        assert run.tool() == []

    def test_the_teardown_asks_for_the_quiet_skip_on_an_old_aws_cli(self, tmp_path):
        # The e2e workflow's runner has an AWS CLI older than 2.38.0, and its Agent Space (make devops-agent) has no service
        # connected: without --if-supported every green run would print the refusal and make's "Error 1 (ignored)".
        run = make(tmp_path, "destroy-ngrh", stacks={"ngrh-t": {"parameters": {"DevOpsAgentSpaceArn": SPACE}, "outputs": {}}})
        (call,) = [c for c in run.calls if c[0] == "python3"]
        assert call[call.index("devops-agent") + 1:call.index("devops-agent") + 3] == ["disconnect", "--if-supported"]
        assert run.done.returncode == 0, run.out

    def test_the_options_variable_is_empty_on_the_other_paths_so_a_disconnect_you_ask_for_still_refuses(self, tmp_path):
        run = make(tmp_path, "devops-agent-disconnect", stacks=ngrh_stack(SPACE))
        (call,) = [c for c in run.calls if c[0] == "python3"]
        assert "--if-supported" not in call
        run = make(tmp_path / "set to nothing", "ngrh", variables=["DEVOPS_AGENT_SPACE_ARN="], stacks=ngrh_stack(SPACE))
        (call,) = [c for c in run.calls if c[0] == "python3"]
        assert call[call.index("devops-agent") + 1] == "disconnect" and "--if-supported" not in call


def _prerequisites(target):
    text = re.sub(r"\\\n\s*", " ", MAKEFILE.read_text())
    return next(m.group(1).split() for m in re.finditer(rf"^{re.escape(target)}\s*:\s*(.*)$", text, re.M) if not m.group(1).startswith("="))


class TestDestroyAll:
    def test_the_agent_space_stack_is_deleted_after_the_ngrh_stack_and_before_the_infrastructure(self):
        deps = _prerequisites("destroy-all")
        assert deps.index("destroy-ngrh") < deps.index("destroy-devops-agent") < deps.index("destroy-infra")

    def test_make_runs_them_in_that_order(self, tmp_path):
        # destroy-all itself runs everything; -n prints the order without running (no recipe of these has $(MAKE)).
        done = subprocess.run(["make", "-n", "-C", str(DEPLOYMENT), "destroy-ngrh", "destroy-devops-agent", "ENV=-t"],
                              capture_output=True, text=True, timeout=60, env=dict(os.environ, PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}"))
        text = done.stdout
        assert text.index("delete-stack --stack-name ngrh-t") < text.index("delete-stack --stack-name devops-agent-t")


class TestMakefileText:
    def test_no_recipe_line_of_ngrh_runs_a_sub_make(self):
        # make runs a line that has $(MAKE) in it even under -n, so make -n ngrh would deploy for real.
        recipe = re.search(r"^ngrh:\n((?:\t.*\n|\n)+)", MAKEFILE.read_text(), re.M).group(1)
        assert "$(MAKE)" not in recipe

    def test_the_devops_agent_targets_exist(self):
        targets = set(re.findall(r"^([a-z][a-z-]*):", MAKEFILE.read_text(), re.M))
        assert {"devops-agent", "devops-agent-disconnect", "devops-agent-status", "destroy-devops-agent"} <= targets
