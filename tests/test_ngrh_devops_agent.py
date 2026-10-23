"""The invoker role's AWS DevOps Agent permissions in ngrh.yaml (off unless the deployment has an Agent Space), and
the README section about DevOps Agent.

Resilience Hub can hand a failure mode assessment finding or a test run to AWS DevOps Agent for investigation once
a service is connected to an Agent Space. It reaches DevOps Agent by assuming the service's invoker role, so that
role needs eight ``aidevops`` actions first. Without them the connection fails with access denied and the
service's DevOps Agent status becomes FAILED ("Add DevOps Agent permissions to the invoker role", Resilience Hub
User Guide).

These tests pin how ``ngrh.yaml`` carries that grant:

* the template takes one optional parameter, ``DevOpsAgentSpaceArn``, empty by default, that accepts the ARN of
  one Agent Space and nothing else (no wildcards), so the grant cannot reach more than the Agent Space it names;
* a condition says whether it is set, and only then does ``InvokerRole`` get a second inline policy;
* that policy has the documented eight actions on that Agent Space, and nothing else in the template mentions
  ``aidevops``;
* the broad managed policies the documentation warns against are not attached;
* the services do not read the parameter: they are connected after the deploy, by ``make ngrh``, because
  CloudFormation cannot disconnect a service again.

The Agent Space itself is tests/test_ngrh_devops_agent_stack.py, ``make`` is tests/test_ngrh_devops_agent_make.py
and the connecting is tests/test_ngrh_devops_agent_tool.py.

Run with:  pytest tests/test_ngrh_devops_agent.py -v
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
DEPLOYMENT = REPO / "deployment"
TEMPLATE = DEPLOYMENT / "ngrh.yaml"

AGENT_SPACE_ARN = "arn:aws:aidevops:us-east-1:123456789012:agentspace/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
STACK_ARN = "arn:aws:cloudformation:us-east-1:123456789012:stack/carts-db-stack-t/11111111-2222-3333-4444-555555555555"

# The name the AWS documentation gives the inline policy.
POLICY_NAME = "ResilienceHubDevOpsAgentAccess"

# The two statements of the policy in "Add DevOps Agent permissions to the invoker role" (2026-10-16): the actions
# Resilience Hub calls to start, follow and cancel an investigation, and the read-only ones it uses to validate the
# connection. The documentation's Resource is agentspace/* in any Region and account; the template names one.
INVESTIGATION_ACTIONS = [
    "aidevops:CreateBacklogTask",
    "aidevops:GetBacklogTask",
    "aidevops:ListExecutions",
    "aidevops:ListJournalRecords",
    "aidevops:UpdateBacklogTask",
]
VALIDATION_ACTIONS = [
    "aidevops:GetAgentSpace",
    "aidevops:GetOperatorApp",
    "aidevops:ListAssociations",
]


class _TaggedLoader(yaml.SafeLoader):
    """SafeLoader that keeps CloudFormation short-form tags: ``!Ref X`` becomes ``{"Ref": "X"}``, ``!If [..]``
    becomes ``{"Fn::If": [..]}``."""


def _tagged(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {"Ref" if suffix == "Ref" else f"Fn::{suffix}": value}


_TaggedLoader.add_multi_constructor("!", _tagged)


def _template():
    # Drive the SafeLoader subclass directly rather than passing it to yaml.load (see tests/test_yaml_loading.py).
    loader = _TaggedLoader(TEMPLATE.read_text())
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


_NO_VALUE = object()


def _render(node, space_arn):
    """Resolve ``HasDevOpsAgentSpace``, ``Ref DevOpsAgentSpaceArn`` and ``AWS::NoValue`` the way CloudFormation does
    for a deployment whose parameter is ``space_arn``."""
    if isinstance(node, list):
        rendered = [_render(item, space_arn) for item in node]
        return [item for item in rendered if item is not _NO_VALUE]
    if isinstance(node, dict):
        if node == {"Ref": "AWS::NoValue"}:
            return _NO_VALUE
        if node == {"Ref": "DevOpsAgentSpaceArn"}:
            return space_arn
        if set(node) == {"Fn::If"}:
            name, when_true, when_false = node["Fn::If"]
            assert name == "HasDevOpsAgentSpace", f"unexpected condition {name}"
            return _render(when_true if space_arn else when_false, space_arn)
        rendered = {key: _render(value, space_arn) for key, value in node.items()}
        return {key: value for key, value in rendered.items() if value is not _NO_VALUE}
    return node


def _invoker_policies(space_arn):
    """The inline policies the invoker role ends up with for a deployment whose parameter is ``space_arn``."""
    return _render(_template()["Resources"]["InvokerRole"]["Properties"]["Policies"], space_arn)


# ---------------------------------------------------------------------------
# The template
# ---------------------------------------------------------------------------

class TestTemplate:
    def test_the_parameter_is_optional(self):
        param = _template()["Parameters"]["DevOpsAgentSpaceArn"]
        assert param["Type"] == "String"
        assert param["Default"] == ""

    def test_the_parameter_takes_the_arn_of_one_agent_space_or_nothing(self):
        pattern = re.compile(_template()["Parameters"]["DevOpsAgentSpaceArn"]["AllowedPattern"])
        assert pattern.fullmatch(""), "empty must be allowed: it means no DevOps Agent permissions"
        assert pattern.fullmatch(AGENT_SPACE_ARN)
        assert pattern.fullmatch(AGENT_SPACE_ARN.replace("arn:aws:", "arn:aws-us-gov:"))
        assert pattern.fullmatch("arn:aws:aidevops:eu-west-1:123456789012:agentspace/" + "a" * 64)
        for refused in [
            "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",  # the Agent Space id, not its ARN
            "arn:aws:aidevops:*:*:agentspace/*",  # the documentation's own resource
            "arn:aws:aidevops:us-east-1:123456789012:agentspace/*",  # every Agent Space in the account
            "arn:aws:aidevops:us-east-1:*:agentspace/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
            "arn:aws:aidevops:*:123456789012:agentspace/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
            "arn:aws:aidevops:us-east-1:123456789012:agentspace/",  # no id
            "arn:aws:aidevops:us-east-1:123456789012:agentspace/" + "a" * 65,  # longer than the API takes
            "arn:aws:aidevops:us-east-1:1234567890:agentspace/abc",  # not an account id
            "arn:aws:aidevops:us-east-1:123456789012:association/abc",  # not an Agent Space
            "arn:aws:iam::123456789012:role/ngrh-invoker",  # not DevOps Agent at all
            AGENT_SPACE_ARN + "/*",
            AGENT_SPACE_ARN + ",arn:aws:aidevops:us-east-1:123456789012:agentspace/other",
            " " + AGENT_SPACE_ARN,
            AGENT_SPACE_ARN + " ",
            AGENT_SPACE_ARN + "\n" + AGENT_SPACE_ARN,
        ]:
            assert not pattern.fullmatch(refused), refused

    def test_a_condition_says_the_parameter_is_set(self):
        condition = _template()["Conditions"]["HasDevOpsAgentSpace"]
        assert condition == {"Fn::Not": [{"Fn::Equals": [{"Ref": "DevOpsAgentSpaceArn"}, ""]}]}

    def test_the_invoker_role_has_no_devops_agent_permissions_when_the_parameter_is_empty(self):
        policies = _invoker_policies("")
        assert [p["PolicyName"] for p in policies] == ["ngrh-bucket-access"]
        assert "aidevops" not in json.dumps(policies)

    def test_the_invoker_role_gets_one_more_inline_policy_when_the_parameter_is_set(self):
        policies = _invoker_policies(AGENT_SPACE_ARN)
        assert [p["PolicyName"] for p in policies] == ["ngrh-bucket-access", POLICY_NAME]

    def test_the_report_bucket_policy_is_the_same_either_way(self):
        assert _invoker_policies("")[0] == _invoker_policies(AGENT_SPACE_ARN)[0]

    def test_the_policy_has_the_documented_actions_on_the_named_agent_space_only(self):
        policy = _invoker_policies(AGENT_SPACE_ARN)[1]
        assert policy["PolicyDocument"]["Version"] == "2012-10-17"
        statements = {s["Sid"]: s for s in policy["PolicyDocument"]["Statement"]}
        assert sorted(statements) == ["ResilienceHubV2DevOpsAgentInvestigation", "ResilienceHubV2DevOpsAgentValidation"]
        expected = {
            "ResilienceHubV2DevOpsAgentInvestigation": INVESTIGATION_ACTIONS,
            "ResilienceHubV2DevOpsAgentValidation": VALIDATION_ACTIONS,
        }
        for sid, actions in expected.items():
            statement = statements[sid]
            assert set(statement) == {"Sid", "Effect", "Action", "Resource"}, "no condition, principal or NotAction"
            assert statement["Effect"] == "Allow"
            assert sorted(statement["Action"]) == sorted(actions)
            assert statement["Resource"] == AGENT_SPACE_ARN, "the one Agent Space named, never a wildcard"

    def test_the_policy_resource_follows_the_parameter(self):
        other = "arn:aws:aidevops:us-west-2:123456789012:agentspace/other-space"
        policy = _invoker_policies(other)[1]
        assert {s["Resource"] for s in policy["PolicyDocument"]["Statement"]} == {other}

    def test_the_broad_managed_policies_the_documentation_warns_against_are_not_attached(self):
        # AIDevOpsAgentFullAccess and AIDevOpsOperatorAppAccessPolicy grant actions that change an Agent Space,
        # which Resilience Hub must never call. The managed policies are attached whether or not the parameter is set.
        arns = _template()["Resources"]["InvokerRole"]["Properties"]["ManagedPolicyArns"]
        assert not [arn for arn in arns if "aidevops" in arn.lower()], arns

    def test_only_the_invoker_role_mentions_the_service(self):
        resources = _template()["Resources"]
        assert sorted(lid for lid, r in resources.items() if "aidevops" in json.dumps(r).lower()) == ["InvokerRole"]

    def test_only_the_invoker_role_uses_the_parameter(self):
        # The services are connected after the deploy (make ngrh), not by this template: no service reads the parameter.
        resources = _template()["Resources"]
        users = sorted(lid for lid, r in resources.items() if "DevOpsAgentSpaceArn" in json.dumps(r))
        assert users == ["InvokerRole"]

    def test_no_service_has_a_devops_agent_configuration(self):
        # cfn-lint's schema for the service does not have the property yet, and a template that leaves it out leaves
        # a service's link as it is, so a connection made here could never be taken away again.
        services = [r for r in _template()["Resources"].values() if r["Type"] == "AWS::ResilienceHubV2::Service"]
        assert len(services) == 6
        assert not [s for s in services if "DevOpsAgentConfiguration" in s["Properties"]]


# ---------------------------------------------------------------------------
# The README
# ---------------------------------------------------------------------------

def _readme():
    return (REPO / "README.md").read_text()


def _section():
    readme = _readme()
    return readme[readme.index("### Investigating with AWS DevOps Agent"):readme.index("### Testing resilience with NGRH")]


class TestReadme:
    def test_the_section_sits_between_running_the_tests_and_testing_with_ngrh(self):
        readme = _readme()
        assert (readme.index("### Running Resilience Hub tests")
                < readme.index("### Investigating with AWS DevOps Agent")
                < readme.index("### Testing resilience with NGRH"))

    def test_it_says_how_to_create_the_agent_space_and_what_that_creates(self):
        section = _section()
        assert "\nmake devops-agent\n" in section
        assert "devops-agent${ENV}" in section and "deployment/devops-agent.yaml" in section
        for name in ("multi-region-microservice${ENV}", "devops-agent-space${ENV}", "devops-agent-operator${ENV}",
                     "AIDevOpsAgentAccessPolicy", "AIDevOpsOperatorAppAccessPolicy"):
            assert name in section, name

    def test_it_says_the_agent_space_takes_no_action_in_the_account(self):
        assert "elevated actions off" in _section()

    def test_it_is_open_about_what_the_agent_role_can_do(self):
        # AIDevOpsAgentAccessPolicy allows cloudtrail:StartQuery and support:CreateCase besides reading.
        section = _section()
        assert "cloudtrail:StartQuery" in section and "support:CreateCase" in section

    def test_it_says_what_make_ngrh_does_with_the_agent_space_and_that_it_connects_nothing(self):
        section = _section()
        assert POLICY_NAME in section and "`ngrh-invoker${ENV}`" in section
        assert "Connecting a service is a separate step, because it starts paid work." in section
        assert "connects each of the six services" not in section

    def test_it_warns_that_connecting_starts_an_investigation_for_every_finding_and_gives_the_numbers(self):
        # Measured 2026-10-16: connecting orders-dev (21 findings) started 21 investigations in four seconds.
        section = _section()
        assert "Connecting starts one investigation for every finding" in section
        assert "open or resolved" in section and "all at once" in section
        assert "21 findings" in section and "21 investigations" in section and "111 findings" in section
        assert "three at a time" in section

    def test_it_says_how_to_connect_by_naming_services_and_how_to_see_the_counts_first(self):
        section = _section()
        assert "make devops-agent-status" in section
        assert 'make devops-agent-connect DEVOPS_AGENT_SERVICES="orders"' in section
        assert "refuses unless you name the services" in section and "Start with one service." in section
        assert "is left alone and starts nothing" in section and "is not sent again" in section

    def test_it_does_not_claim_more_than_was_checked(self):
        section = _section()
        assert "Nothing is billed" not in section and "nothing has been billed" not in section
        assert "not in the AWS documentation" not in section, "only five Resilience Hub pages were read"

    def test_it_says_how_to_deploy_and_connect_in_one_command_and_what_that_does_and_costs(self):
        # make deploy DEVOPS_AGENT_CONNECT="orders" (deployment/Makefile): the checks, then the deploy, then the Agent Space and
        # the connection. Everything the paragraph says is a behaviour tests/test_ngrh_devops_agent_make.py pins.
        paragraph = next(p for p in _section().split("\n\n") if p.startswith("**In one command.**"))
        assert '`make deploy DEVOPS_AGENT_CONNECT="orders"`' in paragraph
        assert "`make devops-agent`" in paragraph and "`make devops-agent-connect`" in paragraph
        assert "same names as `DEVOPS_AGENT_SERVICES`" in paragraph and "`all`" in paragraph
        assert "Without the variable `make deploy` does neither" in paragraph and "the `e2e` workflow does not set it" in paragraph
        assert "Before it deploys anything" in paragraph and "AWS CLI is 2.38.0 or later" in paragraph
        assert "`DEVOPS_AGENT_IAM_WAIT`" in paragraph and "a minute" in paragraph and "is not sent again" in paragraph
        assert "`make deploy` runs no assessment" in paragraph and "name one service first" in paragraph
        assert "`DEVOPS_AGENT_SPACE_ARN=<arn>`" in paragraph and "is not created" in paragraph
        assert "the deploy is done" in paragraph and "run `make devops-agent-connect` as above" in paragraph
        assert "\u2014" not in paragraph and " -- " not in paragraph

    def test_the_readme_and_the_makefile_name_the_same_variables(self):
        makefile = (DEPLOYMENT / "Makefile").read_text()
        for name in ("DEVOPS_AGENT_CONNECT", "DEVOPS_AGENT_IAM_WAIT"):
            assert name in makefile and name in _section(), name

    def test_it_gives_the_price_and_the_command_that_shows_the_usage(self):
        section = _section()
        assert "$0.0083 per agent-second" in section and "`aws devops-agent get-account-usage`" in section
        assert "every test run of a connected service starts one more" in section

    def test_it_reports_the_system_learning_task_as_seen_once_and_does_not_say_it_is_free(self):
        # test2, 2026-10-14: "Create Agent Space Understanding Skills", created 33.2 minutes after the Agent Space, 22.4 minutes
        # of execution, no service connected; get-account-usage then showed monthlyAccountSystemLearningHours 0.37.
        section = _section()
        assert "in one test account" in section and "about 33 minutes" in section and "ran for 22 minutes" in section
        assert "`monthlyAccountSystemLearningHours`" in section
        assert "The pricing page lists no charge for it" in section
        assert "free" not in section.lower().replace("freely", "")

    def test_it_names_the_eight_actions(self):
        section = _section()
        for action in INVESTIGATION_ACTIONS + VALIDATION_ACTIONS:
            assert action.split(":")[1] in section, action

    def test_it_says_the_permissions_cover_only_the_agent_space(self):
        section = _section()
        assert "agentspace/*" in section, "say how this differs from the AWS documentation's policy"
        assert "any Agent Space" in section

    def test_it_says_why_the_broad_managed_policies_are_left_out(self):
        section = _section()
        assert "AIDevOpsAgentFullAccess" in section and "AIDevOpsOperatorAppAccessPolicy" in section

    def test_it_says_what_is_needed_to_run_it(self):
        section = _section()
        assert "Supported Regions" in section
        assert "AWS CLI 2.38.0" in section
        assert "`AIDevOpsAgentFullAccess` has every DevOps Agent action the stack uses and no IAM actions" in section
        assert "`DevOpsAgentSpace`" in section and "`IAMForCFN`" in section, "say where the e2e role gets them"
        assert "GetOperatorAppTeams" not in section, "IAM has no such action: Access Analyzer reports it as nonexistent"

    def test_it_says_what_to_do_when_the_first_deploy_fails(self):
        section = _section()
        assert "ROLLBACK_COMPLETE" in section and "make destroy-devops-agent" in section

    def test_it_says_which_agent_space_wins(self):
        section = _section()
        for text in ("DEVOPS_AGENT_SPACE_ARN", "when that stack exists", "creates no Agent Space"):
            assert text in section, text

    def test_it_says_what_the_e2e_workflow_does_with_it_and_no_longer_says_its_role_has_no_access(self):
        # The e2e workflow deploys the Agent Space in a step of its own after the tests (.projenrc.ts), and its role has
        # the DevOpsAgentSpace statement. An earlier sentence said the opposite of both.
        section = _section()
        assert "its role has no DevOps Agent permissions" not in section, "the e2e role has them now"
        assert "never creates an Agent Space" not in section
        paragraph = next(p for p in section.split("\n\n") if p.startswith("**In the `e2e` workflow.**"))
        assert "`Deploy the DevOps Agent Agent Space`" in paragraph and "`make devops-agent`" in paragraph
        assert "connects no service, so no investigation starts" in paragraph
        assert "`DevOpsAgentSpace`" in paragraph and "`github-oidc-role.yaml`" in paragraph
        assert "`make destroy-all`" in paragraph and "system learning" in paragraph
        assert "`AccessDenied`" in paragraph, "a fork with an older e2e role needs to know why the step fails"

    def test_it_gives_both_system_learning_observations_and_still_does_not_say_it_is_free(self):
        section = _section()
        assert "counted 27 minutes of it" in section and "about two hours" in section
        assert "free" not in section.lower().replace("freely", "")

    def test_it_says_the_disconnect_in_the_teardown_is_quiet_on_an_old_aws_cli_and_the_one_you_ask_for_is_not(self):
        section = _section()
        assert "older than 2.38.0" in section and "prints that it skipped it and goes on" in section
        assert "`make devops-agent-disconnect`, which you ask for, still refuses an old AWS CLI" in section

    def test_it_says_how_to_take_it_away_and_that_deleting_is_for_good(self):
        section = _section()
        assert "`make ngrh DEVOPS_AGENT_SPACE_ARN=`" in section
        assert "`make destroy-devops-agent`" in section and "for good" in section
        assert "`make destroy-all`" in section

    def test_the_invoker_role_bullet_points_at_the_section(self):
        bullet = next(line for line in _readme().splitlines() if line.startswith("* **`ngrh-invoker${ENV}`**"))
        assert "(#investigating-with-aws-devops-agent)" in bullet

    def test_the_prerequisites_and_the_cleanup_and_the_costs_mention_it(self):
        readme = _readme()
        assert "AWS DevOps Agent (optional" in readme[readme.index("## Pre-requisites"):readme.index("### Regions")]
        assert "DevOps Agent investigation" in readme[readme.index("### Resilience Hub and test costs"):readme.index("### Cost-reduction levers")]
        cleanup = readme[readme.index("## Cleanup"):readme.index("## Cost")]
        assert "make devops-agent" in cleanup and "cannot be undone" in cleanup
