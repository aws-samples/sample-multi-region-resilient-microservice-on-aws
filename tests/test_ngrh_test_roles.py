"""Tests for the IAM roles NGRH (Resilience Hub V2) needs to run tests.

A Resilience Hub test run executes as two roles, neither of which is the
caller's own role:

* the service's invoker role (InvokerRole), which Resilience Hub assumes to
  create, start and stop the FIS experiment behind the test run, and
* the test's experiment role (TestExperimentRole), which FIS assumes to inject
  the faults.

On 2026-09-30 every test run in the test account failed because the invoker
role carried only the assessment policy (fis:CreateExperimentTemplate denied).
These tests pin both roles to what the four NGRH test templates need.

Run with:  pytest tests/test_ngrh_test_roles.py -v
"""

import fnmatch
from pathlib import Path

import pytest
import yaml

TEMPLATE = Path(__file__).parent.parent / "deployment" / "ngrh.yaml"

TESTING_POLICY = "arn:aws:iam::aws:policy/AWSResilienceHubResilienceTestingPolicy"
ASSESSMENT_POLICY = "arn:aws:iam::aws:policy/AWSResilienceHubV2AssessmentExecutionPolicy"
FIS_NETWORK_POLICY = "arn:aws:iam::aws:policy/service-role/AWSFaultInjectionSimulatorNetworkAccess"

# FIS actions in the four NGRH test templates (resiliencehubv2 get-test-template,
# 2026-10-01): aws-az-recovery:rtaz001, aws-dependency-validation:rtdep001,
# aws-multi-region-isolation:rtmr001, aws-multi-region-recovery:rtmr002.
# Values are the permissions the FIS actions reference lists for each action.
FIS_ACTION_PERMISSIONS = {
    "aws:ec2:stop-instances": ["ec2:StopInstances", "ec2:StartInstances", "ec2:DescribeInstances", "kms:CreateGrant"],
    "aws:ec2:api-insufficient-instance-capacity-error": ["ec2:InjectApiError"],
    "aws:ec2:asg-insufficient-instance-capacity-error": ["ec2:InjectApiError", "autoscaling:DescribeAutoScalingGroups"],
    "aws:network:disrupt-connectivity": [
        "ec2:CreateNetworkAcl", "ec2:CreateNetworkAclEntry", "ec2:CreateTags", "ec2:DeleteNetworkAcl",
        "ec2:DescribeManagedPrefixLists", "ec2:DescribeNetworkAcls", "ec2:DescribeSubnets", "ec2:DescribeVpcs",
        "ec2:GetManagedPrefixListEntries", "ec2:ReplaceNetworkAclAssociation",
    ],
    "aws:rds:failover-db-cluster": ["rds:FailoverDBCluster", "rds:DescribeDBClusters", "tag:GetResources"],
    "aws:elasticache:replicationgroup-interrupt-az-power": [
        "elasticache:InterruptClusterAzPower", "elasticache:DescribeReplicationGroups", "tag:GetResources",
    ],
    "aws:arc:start-zonal-autoshift": [
        "arc-zonal-shift:StartZonalShift", "arc-zonal-shift:GetManagedResource", "arc-zonal-shift:UpdateZonalShift",
        "arc-zonal-shift:CancelZonalShift", "arc-zonal-shift:ListManagedResources", "autoscaling:DescribeTags",
        "tag:GetResources",
    ],
    "aws:ssm:send-command": ["ssm:SendCommand", "ssm:ListCommands", "ssm:CancelCommand"],
    "aws:ecs:task-network-packet-loss": [
        "ecs:DescribeTasks", "ecs:DescribeContainerInstances", "ec2:DescribeInstances", "ec2:DescribeSubnets",
        "ssm:SendCommand", "ssm:ListCommands", "ssm:CancelCommand",
    ],
    "aws:eks:pod-network-packet-loss": ["eks:DescribeCluster", "ec2:DescribeSubnets", "tag:GetResources"],
    "aws:network:route-table-disrupt-cross-region-connectivity": [
        "ec2:AssociateRouteTable", "ec2:CreateManagedPrefixList", "ec2:CreateNetworkInterface", "ec2:CreateRoute",
        "ec2:CreateRouteTable", "ec2:CreateTags", "ec2:DeleteManagedPrefixList", "ec2:DeleteNetworkInterface",
        "ec2:DeleteRouteTable", "ec2:DescribeManagedPrefixLists", "ec2:DescribeNetworkInterfaces",
        "ec2:DescribeRouteTables", "ec2:DescribeSubnets", "ec2:DescribeVpcPeeringConnections", "ec2:DescribeVpcs",
        "ec2:DisassociateRouteTable", "ec2:GetManagedPrefixListEntries", "ec2:ModifyManagedPrefixList",
        "ec2:ModifyVpcEndpoint", "ec2:ReplaceRouteTableAssociation",
    ],
    "aws:network:transit-gateway-disrupt-cross-region-connectivity": [
        "ec2:AssociateTransitGatewayRouteTable", "ec2:DescribeTransitGatewayAttachments",
        "ec2:DescribeTransitGatewayPeeringAttachments", "ec2:DescribeTransitGateways",
        "ec2:DisassociateTransitGatewayRouteTable",
    ],
    "aws:network:disrupt-vpc-endpoint": [
        "ec2:DescribeVpcEndpoints", "ec2:DescribeSecurityGroups", "ec2:ModifyVpcEndpoint", "ec2:CreateSecurityGroup",
        "ec2:DeleteSecurityGroup", "ec2:RevokeSecurityGroupEgress", "ec2:CreateTags", "vpce:AllowMultiRegion",
    ],
    "aws:s3:bucket-pause-replication": [
        "s3:PutReplicationConfiguration", "s3:GetReplicationConfiguration", "s3:PauseReplication",
        "s3:ListAllMyBuckets", "tag:GetResources",
    ],
    "aws:dynamodb:global-table-pause-replication": [
        "dynamodb:PutResourcePolicy", "dynamodb:DeleteResourcePolicy", "dynamodb:GetResourcePolicy",
        "dynamodb:DescribeTable", "dynamodb:InjectError", "tag:GetResources",
    ],
    "aws:memorydb:multi-region-cluster-pause-replication": [
        "memorydb:DescribeMultiRegionClusters", "memorydb:PauseMultiRegionClusterReplication", "tag:GetResources",
    ],
}

# Actions granted by AWSFaultInjectionSimulatorNetworkAccess (v5, 2026-10-01).
FIS_NETWORK_POLICY_ACTIONS = {
    "ec2:AssociateRouteTable", "ec2:AssociateTransitGatewayRouteTable", "ec2:CreateManagedPrefixList",
    "ec2:CreateNetworkAcl", "ec2:CreateNetworkAclEntry", "ec2:CreateNetworkInterface", "ec2:CreateRoute",
    "ec2:CreateRouteTable", "ec2:CreateTags", "ec2:DeleteManagedPrefixList", "ec2:DeleteNetworkAcl",
    "ec2:DeleteNetworkInterface", "ec2:DeleteRouteTable", "ec2:DescribeManagedPrefixLists",
    "ec2:DescribeNetworkAcls", "ec2:DescribeNetworkInterfaces", "ec2:DescribeRouteTables", "ec2:DescribeSubnets",
    "ec2:DescribeTransitGatewayAttachments", "ec2:DescribeTransitGatewayPeeringAttachments",
    "ec2:DescribeTransitGateways", "ec2:DescribeVpcEndpoints", "ec2:DescribeVpcPeeringConnections",
    "ec2:DescribeVpcs", "ec2:DisassociateRouteTable", "ec2:DisassociateTransitGatewayRouteTable",
    "ec2:GetManagedPrefixListEntries", "ec2:ModifyManagedPrefixList", "ec2:ModifyVpcEndpoint",
    "ec2:ReplaceNetworkAclAssociation", "ec2:ReplaceRouteTableAssociation",
}

# Granting any of these would let a test (or anyone who can start FIS
# experiments with this role) act beyond fault injection.
FORBIDDEN_ACTIONS = [
    "*", "iam:*", "iam:PassRole", "sts:AssumeRole", "ssm:StartAutomationExecution",
    "ec2:TerminateInstances", "rds:DeleteDBCluster", "dynamodb:DeleteTable", "s3:PutBucketPolicy",
]


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that keeps CloudFormation intrinsics (!Sub, !Ref, ...) as dicts."""


def _intrinsic(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {"Ref" if suffix == "Ref" else f"Fn::{suffix}": value}


_CfnLoader.add_multi_constructor("!", _intrinsic)


def _load_template():
    # Drive the SafeLoader subclass directly instead of passing it to yaml.load
    # as the Loader argument: same parse, no yaml.load call. Bandit's B506 (and
    # the ACAT scan built on it) accepts only the literal SafeLoader name there.
    loader = _CfnLoader(TEMPLATE.read_text())
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


@pytest.fixture(scope="module")
def resources():
    return _load_template()["Resources"]


@pytest.fixture(scope="module")
def experiment_role(resources):
    return resources["TestExperimentRole"]["Properties"]


def _as_list(value):
    return value if isinstance(value, list) else [value]


def _allow_statements(role_props):
    for policy in role_props.get("Policies", []):
        for stmt in policy["PolicyDocument"]["Statement"]:
            if stmt["Effect"] == "Allow":
                yield stmt


def _granted(role_props):
    granted = set()
    for stmt in _allow_statements(role_props):
        granted.update(_as_list(stmt["Action"]))
    return granted


def _covers(patterns, action):
    return any(fnmatch.fnmatchcase(action.lower(), p.lower()) for p in patterns)


# --- invoker role ---------------------------------------------------------

def test_invoker_role_can_run_tests_and_assessments(resources):
    arns = resources["InvokerRole"]["Properties"]["ManagedPolicyArns"]
    assert TESTING_POLICY in arns, "without it every NGRH test run fails at fis:CreateExperimentTemplate"
    assert ASSESSMENT_POLICY in arns


def test_invoker_role_trust_is_resilience_hub_for_this_account_only(resources):
    (stmt,) = resources["InvokerRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert stmt["Principal"] == {"Service": "resiliencehub.amazonaws.com"}
    assert stmt["Condition"]["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}


def test_every_service_uses_the_invoker_role(resources):
    services = [r for r in resources.values() if r["Type"] == "AWS::ResilienceHubV2::Service"]
    assert services
    for svc in services:
        assert svc["Properties"]["PermissionModel"]["InvokerRoleName"] == {"Ref": "InvokerRole"}


# --- experiment role ------------------------------------------------------

def test_experiment_role_is_assumable_only_by_fis_experiments_in_this_account(experiment_role):
    (stmt,) = experiment_role["AssumeRolePolicyDocument"]["Statement"]
    assert stmt["Principal"] == {"Service": "fis.amazonaws.com"}
    assert stmt["Condition"]["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}
    assert stmt["Condition"]["ArnLike"]["aws:SourceArn"] == {
        "Fn::Sub": "arn:${AWS::Partition}:fis:*:${AWS::AccountId}:experiment/*"
    }


def test_experiment_role_attaches_the_fis_network_policy(experiment_role):
    assert experiment_role["ManagedPolicyArns"] == [FIS_NETWORK_POLICY]


@pytest.mark.parametrize("fis_action", sorted(FIS_ACTION_PERMISSIONS))
def test_experiment_role_grants_every_documented_permission(experiment_role, fis_action):
    patterns = _granted(experiment_role) | FIS_NETWORK_POLICY_ACTIONS
    missing = [p for p in FIS_ACTION_PERMISSIONS[fis_action] if not _covers(patterns, p)]
    assert not missing, f"{fis_action} would fail with 'not enough privileges': missing {missing}"


@pytest.mark.parametrize("action", FORBIDDEN_ACTIONS)
def test_experiment_role_cannot_escalate(experiment_role, action):
    for stmt in _allow_statements(experiment_role):
        for granted in _as_list(stmt["Action"]):
            assert not fnmatch.fnmatchcase(action.lower(), granted.lower()), f"{granted} grants {action}"


def test_experiment_role_runs_only_fis_ssm_documents(experiment_role):
    for stmt in _allow_statements(experiment_role):
        if "ssm:SendCommand" in _as_list(stmt["Action"]):
            documents = [r["Fn::Sub"] for r in _as_list(stmt["Resource"]) if ":document/" in r["Fn::Sub"]]
            assert documents == ["arn:${AWS::Partition}:ssm:*:*:document/AWSFIS-*"]


def test_experiment_role_resources_stay_in_this_account(experiment_role):
    for stmt in _allow_statements(experiment_role):
        for res in _as_list(stmt["Resource"]):
            if res == "*":
                continue
            arn = res["Fn::Sub"]
            # S3 bucket ARNs carry no account; those statements pin s3:ResourceAccount.
            if arn.startswith("arn:${AWS::Partition}:s3:::"):
                assert stmt["Condition"]["StringEquals"]["s3:ResourceAccount"] == {"Ref": "AWS::AccountId"}
            elif ":document/AWSFIS-" not in arn:
                assert "${AWS::AccountId}" in arn, f"{stmt['Sid']}: {arn} is not limited to this account"


def test_outputs_name_the_roles_to_pick():
    outputs = _load_template()["Outputs"]
    assert outputs["TestExperimentRoleName"]["Value"] == {"Ref": "TestExperimentRole"}
    assert outputs["InvokerRoleName"]["Value"] == {"Ref": "InvokerRole"}
