"""deployment/devops-agent.yaml: the AWS DevOps Agent Agent Space of the sample (``make devops-agent``).

It follows the CloudFormation template in the AWS DevOps Agent User Guide: an Agent Space, the role the agent
assumes to investigate the account, the role behind the operator web app, and the association that lets the Agent
Space investigate this account. These tests pin what makes it safe to deploy into someone's account and what the
rest of the sample relies on:

* both roles can be assumed only by the DevOps Agent service, for this account and for the Agent Spaces of this
  Region (the confused deputy conditions), and by nothing else;
* each role has exactly the managed policy the documentation gives it, and the only inline policy lets the service
  create one service-linked role (Resource Explorer's), not any role;
* the Agent Space can take no action in the account (elevated actions off, no third role) and signs people in
  through IAM only;
* the association covers this account and nothing else;
* the stack and the roles are named with Env, so deployments with different ENV do not collide, and its outputs are
  the ones ``make ngrh`` looks for.

Run with:  pytest tests/test_ngrh_devops_agent_stack.py -v
"""

import json
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
DEPLOYMENT = REPO / "deployment"
TEMPLATE = DEPLOYMENT / "devops-agent.yaml"

SOURCE_ARN = {"Fn::Sub": "arn:${AWS::Partition}:aidevops:${AWS::Region}:${AWS::AccountId}:agentspace/*"}
ACCOUNT = {"Ref": "AWS::AccountId"}


class _TaggedLoader(yaml.SafeLoader):
    """SafeLoader that keeps CloudFormation short-form tags: ``!Ref X`` becomes ``{"Ref": "X"}``."""


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


def _resources():
    return _template()["Resources"]


def _as_list(value):
    return value if isinstance(value, list) else [value]


class TestResources:
    def test_it_creates_the_agent_space_the_two_roles_and_the_association_and_nothing_else(self):
        assert {lid: r["Type"] for lid, r in _resources().items()} == {
            "AgentSpaceRole": "AWS::IAM::Role",
            "OperatorRole": "AWS::IAM::Role",
            "AgentSpace": "AWS::DevOpsAgent::AgentSpace",
            "MonitorAssociation": "AWS::DevOpsAgent::Association",
        }

    def test_the_only_parameter_is_the_env_suffix_and_it_is_empty_by_default(self):
        params = _template()["Parameters"]
        assert list(params) == ["Env"] and params["Env"]["Default"] == ""

    def test_the_template_fits_the_request_body_and_description_limits(self):
        assert len(TEMPLATE.read_bytes()) < 51200
        assert len(_template()["Description"].encode()) <= 1024


class TestTrust:
    ROLES = {"AgentSpaceRole": ["sts:AssumeRole"], "OperatorRole": ["sts:AssumeRole", "sts:TagSession"]}

    def _trust(self, lid):
        (statement,) = _resources()[lid]["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        return statement

    def test_only_the_devops_agent_service_can_assume_each_role(self):
        for lid in self.ROLES:
            statement = self._trust(lid)
            assert statement["Effect"] == "Allow"
            assert statement["Principal"] == {"Service": "aidevops.amazonaws.com"}, lid

    def test_each_role_allows_exactly_the_actions_the_documentation_gives_it(self):
        # The operator web app also passes the signed-in person's identity as session tags.
        for lid, actions in self.ROLES.items():
            assert sorted(_as_list(self._trust(lid)["Action"])) == sorted(actions), lid

    def test_both_roles_are_limited_to_this_account_and_to_the_agent_spaces_of_this_region(self):
        for lid in self.ROLES:
            condition = self._trust(lid)["Condition"]
            assert condition == {"StringEquals": {"aws:SourceAccount": ACCOUNT}, "ArnLike": {"aws:SourceArn": SOURCE_ARN}}, lid

    def test_the_source_arn_is_not_open_to_other_regions_or_accounts(self):
        pattern = SOURCE_ARN["Fn::Sub"]
        assert "${AWS::Region}" in pattern and "${AWS::AccountId}" in pattern and ":*:" not in pattern


class TestPolicies:
    def test_the_agent_role_has_the_managed_policy_the_documentation_gives_it(self):
        assert _resources()["AgentSpaceRole"]["Properties"]["ManagedPolicyArns"] == ["arn:aws:iam::aws:policy/AIDevOpsAgentAccessPolicy"]

    def test_the_operator_role_has_the_managed_policy_the_documentation_gives_it_and_no_inline_policy(self):
        props = _resources()["OperatorRole"]["Properties"]
        assert props["ManagedPolicyArns"] == ["arn:aws:iam::aws:policy/AIDevOpsOperatorAppAccessPolicy"]
        assert "Policies" not in props

    def test_no_role_is_given_a_full_access_or_administrator_policy(self):
        for lid in ("AgentSpaceRole", "OperatorRole"):
            for arn in _resources()[lid]["Properties"]["ManagedPolicyArns"]:
                assert not re.search(r"FullAccess|Administrator|PowerUser", arn), arn

    def test_the_only_inline_policy_lets_the_service_create_resource_explorers_service_linked_role(self):
        (policy,) = _resources()["AgentSpaceRole"]["Properties"]["Policies"]
        assert policy["PolicyName"] == "AllowCreateServiceLinkedRoles"
        (statement,) = policy["PolicyDocument"]["Statement"]
        assert statement["Effect"] == "Allow" and statement["Action"] == "iam:CreateServiceLinkedRole"
        assert statement["Resource"] == {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/aws-service-role/"
                                                  "resource-explorer-2.amazonaws.com/AWSServiceRoleForResourceExplorer"}
        assert set(statement) == {"Sid", "Effect", "Action", "Resource"}

    def test_no_policy_statement_has_a_wildcard_resource(self):
        for lid in ("AgentSpaceRole", "OperatorRole"):
            for policy in _resources()[lid]["Properties"].get("Policies", []):
                for statement in policy["PolicyDocument"]["Statement"]:
                    assert "*" not in json.dumps(statement["Resource"]), lid


class TestAgentSpace:
    def test_it_is_named_with_the_env_suffix(self):
        assert _resources()["AgentSpace"]["Properties"]["Name"] == {"Fn::Sub": "multi-region-microservice${Env}"}

    def test_it_takes_no_action_in_the_account(self):
        props = _resources()["AgentSpace"]["Properties"]
        assert props["Preferences"] == {"ElevatedActionsEnabled": False}

    def test_the_operator_app_signs_in_through_iam_with_the_operator_role_and_nothing_else(self):
        app = _resources()["AgentSpace"]["Properties"]["OperatorApp"]
        assert app == {"Iam": {"OperatorAppRoleArn": {"Fn::GetAtt": "OperatorRole.Arn"}}}

    def test_the_agent_role_exists_before_the_agent_space(self):
        # AWS's reference template waits for both roles; the operator role is a GetAtt, so it waits anyway.
        space = _resources()["AgentSpace"]
        assert space["DependsOn"] == "AgentSpaceRole"
        assert space["Properties"]["OperatorApp"]["Iam"]["OperatorAppRoleArn"] == {"Fn::GetAtt": "OperatorRole.Arn"}

    def test_it_does_not_choose_an_encryption_key_or_a_language(self):
        props = _resources()["AgentSpace"]["Properties"]
        assert "KmsKeyArn" not in props and "Locale" not in props


class TestAssociation:
    def test_it_lets_the_agent_space_investigate_this_account_as_its_monitoring_account(self):
        props = _resources()["MonitorAssociation"]["Properties"]
        assert props["AgentSpaceId"] == {"Fn::GetAtt": "AgentSpace.AgentSpaceId"}
        assert props["ServiceId"] == "aws"
        assert props["Configuration"] == {"Aws": {
            "AssumableRoleArn": {"Fn::GetAtt": "AgentSpaceRole.Arn"},
            "AccountId": ACCOUNT,
            "AccountType": "monitor",
        }}

    def test_it_names_no_elevated_role(self):
        assert "AgentElevatedRoleArn" not in json.dumps(_resources()["MonitorAssociation"])

    def test_it_covers_no_other_account(self):
        assert "SourceAws" not in json.dumps(_resources()["MonitorAssociation"])


class TestNamesAndOutputs:
    def test_the_roles_are_named_with_the_env_suffix_so_two_deployments_do_not_collide(self):
        names = {lid: _resources()[lid]["Properties"]["RoleName"] for lid in ("AgentSpaceRole", "OperatorRole")}
        assert names == {"AgentSpaceRole": {"Fn::Sub": "devops-agent-space${Env}"},
                         "OperatorRole": {"Fn::Sub": "devops-agent-operator${Env}"}}

    def test_the_outputs_include_the_one_make_ngrh_looks_for(self):
        outputs = _template()["Outputs"]
        assert set(outputs) == {"AgentSpaceId", "AgentSpaceArn", "AgentSpaceRoleArn", "OperatorRoleArn"}
        assert outputs["AgentSpaceArn"]["Value"] == {"Fn::GetAtt": "AgentSpace.Arn"}
        assert outputs["AgentSpaceId"]["Value"] == {"Fn::GetAtt": "AgentSpace.AgentSpaceId"}

    def test_the_makefile_reads_that_output_of_the_stack_it_deploys(self):
        makefile = (DEPLOYMENT / "Makefile").read_text()
        assert "--stack-name devops-agent$(ENV)" in makefile
        assert "OutputKey=='AgentSpaceArn'" in makefile
        assert "--template ./devops-agent.yaml" in makefile

    def test_the_stack_is_created_in_the_region_the_agent_space_is_looked_up_in(self):
        # devops-agent deploys it, and ngrh and destroy-devops-agent look it up, all in PRIMARY_REGION.
        makefile = (DEPLOYMENT / "Makefile").read_text()
        for target in ("devops-agent", "destroy-devops-agent"):
            recipe = re.search(rf"^{target}:\n((?:\t.*\n)+)", makefile, re.M).group(1)
            assert "--region $(PRIMARY_REGION)" in recipe and "STANDBY_REGION" not in recipe, target
