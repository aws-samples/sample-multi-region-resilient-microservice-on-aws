"""deployment/github-oidc-role.yaml: what the e2e role needs to create the AWS DevOps Agent Agent Space.

The e2e workflow deploys with this role, and CloudFormation runs the resource handlers of
``AWS::DevOpsAgent::AgentSpace`` and ``AWS::DevOpsAgent::Association`` (the stack in ``devops-agent.yaml``,
``make devops-agent``) as the role that deploys. So the role needs the ``aidevops`` actions those handlers use. These
tests pin the change that grants them:

* the statement ``DevOpsAgentSpace`` allows the actions the two handlers declare for create, read, update and delete,
  minus the ones left out, each with a reason (one of them, ``aidevops:GetOperatorAppTeams``, is not an IAM action:
  IAM Access Analyzer reports it as nonexistent, as it does for an invented name);
* it applies to this account's Agent Spaces in us-east-1 and us-west-2, the two Regions the rest of the policy is
  limited to, and to nothing else: no wildcard action or resource, no condition, no other statement mentions
  ``aidevops``, and nothing grants ``sso:``;
* IAM needs no new action: the actions the stack's roles need are already in ``IAMForCFN``, and the calls the tool
  makes to connect a service (``resiliencehub``) are already granted;
* both managed policies stay under IAM's size limit, which a growing policy meets without any error until it does.

Run with:  pytest tests/test_github_oidc_role_devops_agent.py -v
"""

import json
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
DEPLOYMENT = REPO / "deployment"
ROLE_TEMPLATE = DEPLOYMENT / "github-oidc-role.yaml"
STACK_TEMPLATE = DEPLOYMENT / "devops-agent.yaml"
CONNECT_TOOL = DEPLOYMENT / "ngrh_testing" / "devops_agent.py"

# The aidevops permissions the handlers declare (aws cloudformation describe-type, 2026-10-16), by handler.
DECLARED = {
    "AgentSpace.create": ["CreateAgentSpace", "GetAgentSpace", "EnableOperatorApp", "GetOperatorApp", "GetOperatorAppTeams",
                          "TagResource", "ListTagsForResource"],
    "AgentSpace.read": ["GetAgentSpace", "GetOperatorApp", "GetOperatorAppTeams", "ListTagsForResource"],
    "AgentSpace.update": ["UpdateAgentSpace", "GetAgentSpace", "EnableOperatorApp", "UpdateOperatorAppIdpConfig",
                          "DisableOperatorApp", "GetOperatorApp", "GetOperatorAppTeams", "TagResource", "UntagResource",
                          "ListTagsForResource"],
    "AgentSpace.delete": ["DeleteAgentSpace", "GetAgentSpace", "DisableOperatorApp"],
    "AgentSpace.list": ["ListAgentSpaces"],
    "Association.create": ["AssociateService", "GetAgentSpace", "ListAssociations", "GetAssociation"],
    "Association.read": ["GetAssociation", "ListAssociations", "GetAgentSpace"],
    "Association.update": ["UpdateAssociation", "ListAssociations", "GetAgentSpace", "GetAssociation"],
    "Association.delete": ["DisassociateService", "GetAgentSpace", "GetAssociation"],
}

# Declared, and not granted.
LEFT_OUT = {
    "ListAgentSpaces": "only the list handler uses it, and no stack operation lists",
    "UpdateOperatorAppIdpConfig": "external identity provider sign-in; the stack uses IAM sign-in",
    "GetOperatorAppTeams": "not an IAM action (IAM Access Analyzer: INVALID_ACTION), and not in AIDevOpsAgentFullAccess",
}

GRANTED = {f"aidevops:{name}" for names in DECLARED.values() for name in names} - {f"aidevops:{name}" for name in LEFT_OUT}

REGIONS = ["us-east-1", "us-west-2"]
POLICY_SIZE_LIMIT = 6144  # characters without whitespace, for one managed policy


class _TaggedLoader(yaml.SafeLoader):
    """SafeLoader that keeps CloudFormation short-form tags: ``!Sub X`` becomes ``{"Fn::Sub": "X"}``."""


def _tagged(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {"Ref" if suffix == "Ref" else f"Fn::{suffix}": value}


_TaggedLoader.add_multi_constructor("!", _tagged)


def _load(path):
    # Drive the SafeLoader subclass directly rather than passing it to yaml.load (see tests/test_yaml_loading.py).
    loader = _TaggedLoader(path.read_text())
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _as_list(value):
    return value if isinstance(value, list) else [value]


def _policies():
    resources = _load(ROLE_TEMPLATE)["Resources"]
    return {lid: r["Properties"]["PolicyDocument"] for lid, r in resources.items() if r["Type"] == "AWS::IAM::ManagedPolicy"}


def _statements():
    return [(lid, s) for lid, doc in _policies().items() for s in doc["Statement"]]


def _statement(sid):
    (found,) = [s for _, s in _statements() if s.get("Sid") == sid]
    return found


def _render(node):
    """The policy as IAM stores it for an account 123456789012: ``!Sub`` strings with the account filled in."""
    if isinstance(node, dict):
        if set(node) == {"Fn::Sub"}:
            return node["Fn::Sub"].replace("${AWS::AccountId}", "123456789012")
        return {k: _render(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_render(x) for x in node]
    return node


# ---------------------------------------------------------------------------
# The statement
# ---------------------------------------------------------------------------

class TestDevOpsAgentStatement:
    def test_there_is_one_and_it_is_in_the_regional_policy(self):
        owners = [lid for lid, s in _statements() if s.get("Sid") == "DevOpsAgentSpace"]
        assert owners == ["RegionalServicesPolicy"]

    def test_it_allows_the_actions_the_handlers_declare_minus_the_ones_left_out(self):
        assert set(_as_list(_statement("DevOpsAgentSpace")["Action"])) == GRANTED

    def test_that_is_fifteen_actions_with_no_duplicates(self):
        actions = _as_list(_statement("DevOpsAgentSpace")["Action"])
        assert len(actions) == len(set(actions)) == 15

    def test_each_action_left_out_has_a_reason_and_is_not_granted(self):
        granted = set(_as_list(_statement("DevOpsAgentSpace")["Action"]))
        for name, reason in LEFT_OUT.items():
            assert reason.strip(), name
            assert f"aidevops:{name}" not in granted, name

    def test_every_action_left_out_is_one_a_handler_declares(self):
        # So the list above cannot drift into excusing something no handler asked for.
        declared = {name for names in DECLARED.values() for name in names}
        assert set(LEFT_OUT) <= declared

    def test_the_comment_in_the_template_gives_the_same_reasons(self):
        text = ROLE_TEMPLATE.read_text()
        block = text[text.index("# AWS DevOps Agent, for the Agent Space stack"):text.index("- Sid: DevOpsAgentSpace")]
        for name in LEFT_OUT:
            assert name in block or name.replace("aidevops:", "") in block or name == "UpdateOperatorAppIdpConfig" and "sso:*" in block, name
        assert "GetOperatorAppTeams" in block and "nonexistent" in block

    def test_no_action_is_a_wildcard(self):
        for action in _as_list(_statement("DevOpsAgentSpace")["Action"]):
            assert re.fullmatch(r"aidevops:[A-Za-z]+", action), action

    def test_it_is_a_plain_allow_with_no_condition_notaction_or_principal(self):
        assert set(_statement("DevOpsAgentSpace")) == {"Sid", "Effect", "Action", "Resource"}
        assert _statement("DevOpsAgentSpace")["Effect"] == "Allow"


class TestScope:
    def test_it_names_this_accounts_agent_spaces_in_the_two_regions_and_nothing_else(self):
        assert _statement("DevOpsAgentSpace")["Resource"] == [
            {"Fn::Sub": f"arn:aws:aidevops:{region}:${{AWS::AccountId}}:agentspace/*"} for region in REGIONS
        ]

    def test_the_regions_are_the_ones_the_rest_of_the_policy_is_limited_to(self):
        condition = _statement("RegionalServiceAccess")["Condition"]
        assert condition == {"StringEquals": {"aws:RequestedRegion": REGIONS}}

    def test_no_resource_is_a_bare_wildcard_or_has_a_wildcard_account_or_region(self):
        for arn in _render(_statement("DevOpsAgentSpace")["Resource"]):
            parts = arn.split(":")
            assert arn != "*"
            assert parts[3] in REGIONS and parts[4] == "123456789012" and parts[5] == "agentspace/*", arn

    def test_rendered_for_an_account_it_matches_an_agent_space_arn_there_and_not_elsewhere(self):
        import fnmatch
        patterns = _render(_statement("DevOpsAgentSpace")["Resource"])

        def matches(arn):
            return any(fnmatch.fnmatchcase(arn, p) for p in patterns)

        space = "agentspace/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
        assert matches(f"arn:aws:aidevops:us-east-1:123456789012:{space}")
        assert matches(f"arn:aws:aidevops:us-west-2:123456789012:{space}")
        assert not matches(f"arn:aws:aidevops:eu-west-1:123456789012:{space}")
        assert not matches(f"arn:aws:aidevops:us-east-1:210987654321:{space}")
        assert not matches(f"arn:aws:aidevops:us-east-2:123456789012:{space}")


class TestNothingElseChanges:
    def test_no_other_statement_mentions_aidevops_or_sso(self):
        for lid, statement in _statements():
            if statement.get("Sid") == "DevOpsAgentSpace":
                continue
            actions = _as_list(statement.get("Action", []))
            assert not [a for a in actions if a.startswith(("aidevops:", "sso:", "sso-"))], (lid, statement["Sid"])

    def test_nothing_grants_identity_center(self):
        assert not [a for _, s in _statements() for a in _as_list(s.get("Action", [])) if a.startswith("sso")]

    def test_the_statements_are_the_old_ones_plus_this_one(self):
        # Regional: three before; Global: five, none touched.
        regional = [s["Sid"] for s in _policies()["RegionalServicesPolicy"]["Statement"]]
        assert regional == ["RegionalServiceAccess", "DSQLMultiRegionSetup", "DynamoDBGlobalTableWitness", "DevOpsAgentSpace"]
        assert [s["Sid"] for s in _policies()["GlobalServicesPolicy"]["Statement"]] == ["IAMForCFN", "Route53", "ARCRegionSwitch", "S3", "Identity"]

    def test_the_role_still_has_exactly_the_two_managed_policies_and_no_inline_policy(self):
        role = _load(ROLE_TEMPLATE)["Resources"]["E2ERole"]["Properties"]
        assert role["ManagedPolicyArns"] == [{"Ref": "RegionalServicesPolicy"}, {"Ref": "GlobalServicesPolicy"}]
        assert "Policies" not in role


# ---------------------------------------------------------------------------
# What the role already has
# ---------------------------------------------------------------------------

def _global_actions(sid):
    return set(_as_list(_statement(sid)["Action"]))


class TestIamIsAlreadyEnough:
    def _stack(self):
        return _load(STACK_TEMPLATE)["Resources"]

    def _needed(self):
        resources = self._stack()
        roles = [r["Properties"] for r in resources.values() if r["Type"] == "AWS::IAM::Role"]
        assert len(roles) == 2
        needed = {"iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateRole", "iam:UpdateAssumeRolePolicy",
                  "iam:TagRole", "iam:UntagRole", "iam:CreateServiceLinkedRole"}
        if any("ManagedPolicyArns" in r for r in roles):
            needed |= {"iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:ListAttachedRolePolicies"}
        if any("Policies" in r for r in roles):
            needed |= {"iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRolePolicy", "iam:ListRolePolicies"}
        space = next(r for r in resources.values() if r["Type"] == "AWS::DevOpsAgent::AgentSpace")["Properties"]
        association = next(r for r in resources.values() if r["Type"] == "AWS::DevOpsAgent::Association")["Properties"]
        if "OperatorAppRoleArn" in space["OperatorApp"]["Iam"] or "AssumableRoleArn" in association["Configuration"]["Aws"]:
            needed.add("iam:PassRole")
        return needed

    def test_the_stack_needs_the_iam_actions_this_test_expects(self):
        assert self._needed() == {
            "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateRole", "iam:UpdateAssumeRolePolicy", "iam:TagRole",
            "iam:UntagRole", "iam:CreateServiceLinkedRole", "iam:AttachRolePolicy", "iam:DetachRolePolicy",
            "iam:ListAttachedRolePolicies", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRolePolicy",
            "iam:ListRolePolicies", "iam:PassRole",
        }

    def test_iam_for_cfn_already_allows_every_one_of_them(self):
        missing = self._needed() - _global_actions("IAMForCFN")
        assert not missing, f"IAMForCFN lacks {sorted(missing)}: add them there"

    def test_iam_for_cfn_is_unchanged_in_the_way_that_matters_here(self):
        statement = _statement("IAMForCFN")
        assert statement["Resource"] == "*" and "Condition" not in statement

    def test_the_invoker_roles_new_inline_policy_needs_only_what_is_already_granted(self):
        assert "iam:PutRolePolicy" in _global_actions("IAMForCFN")


def _camel(kebab):
    return "".join(part.capitalize() for part in kebab.split("-"))


class TestConnectingServices:
    def test_the_calls_the_connect_tool_makes_are_resilience_hub_calls(self):
        calls = set(re.findall(r'aws\.call\("resiliencehubv2", "([a-z-]+)"', CONNECT_TOOL.read_text()))
        assert calls == {"get-service", "update-service", "list-failure-mode-findings"}

    def test_the_role_already_allows_them_through_resiliencehub_wildcard(self):
        statement = _statement("ARCRegionSwitch")
        assert "resiliencehub:*" in _as_list(statement["Action"]) and statement["Resource"] == "*"


# ---------------------------------------------------------------------------
# Size
# ---------------------------------------------------------------------------

class TestPolicySize:
    @pytest.mark.parametrize("lid", ["RegionalServicesPolicy", "GlobalServicesPolicy"])
    def test_each_managed_policy_is_under_the_iam_limit(self, lid):
        size = len(re.sub(r"\s", "", json.dumps(_render(_policies()[lid]))))
        assert size < POLICY_SIZE_LIMIT, f"{lid} is {size} characters without whitespace; IAM allows {POLICY_SIZE_LIMIT}"

    def test_the_regional_policy_has_room_to_spare(self):
        # 1,822 with this statement (1,303 without). A policy that outgrows the limit fails at deploy, after review.
        size = len(re.sub(r"\s", "", json.dumps(_render(_policies()["RegionalServicesPolicy"]))))
        assert size < POLICY_SIZE_LIMIT * 0.6, size
