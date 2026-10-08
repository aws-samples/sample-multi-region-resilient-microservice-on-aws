"""Contract tests for FIS fault injection on the sample's Fargate tasks.

The Resilience Hub test templates fault ECS tasks with
aws:ecs:task-network-packet-loss. On Fargate that needs, per
https://docs.aws.amazon.com/fis/latest/userguide/ecs-task-actions.html:

* an SSM agent sidecar that registers the task as an SSM managed instance
  tagged ECS_TASK_ARN (FIS finds tasks through that tag);
* pidMode task and enableFaultInjection true in the task definition;
* a task role that can create the activation and pass the managed-instance
  role, and a managed-instance role that can clean up after itself;
* ECS Exec turned off.

The task subnets have no internet route, so the sidecar image is mirrored into
the account's ECR with everything the sidecar and the fault documents run baked
in; installing packages from a task fails.

Run with:  pytest tests/test_fis_sidecar.py -v
"""

import re
from pathlib import Path

import pytest
import yaml

DEPLOYMENT = Path(__file__).parent.parent / "deployment"
SIDECAR = "amazon-ssm-agent"
SIDECAR_IMAGE = "${AWS::AccountId}.dkr.ecr.${AWS::Region}.amazonaws.com/amazon-ssm-agent${Env}:latest"

# Commands the sidecar start script and AWSFIS-Run-Network-Packet-Loss run in
# the task, plus what Resilience Hub's agent-install document needs, as the
# packages that provide them on Amazon Linux 2023.
REQUIRED_PACKAGES = {
    "jq", "procps", "awscli", "curl-minimal", "util-linux",  # sidecar start script
    "at", "bind-utils", "lsof", "iproute-tc",                 # atd, dig, lsof, pgrep, tc
    "python3", "python3-requests",                            # agent-install document
}


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that keeps CloudFormation short-form tags as {"!Tag": value}."""


def _cfn_tag(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {"!" + tag_suffix: value}


_CfnLoader.add_multi_constructor("!", _cfn_tag)


def _load_template(path):
    # Drive the SafeLoader subclass directly rather than passing it to yaml.load
    # (see tests/test_yaml_loading.py).
    loader = _CfnLoader(path.read_text())
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


RESOURCES = _load_template(DEPLOYMENT / "ecs.yaml")["Resources"]
TASK_DEFINITIONS = {lid: r["Properties"] for lid, r in RESOURCES.items() if r["Type"] == "AWS::ECS::TaskDefinition"}


def _sidecar(props):
    found = [c for c in props["ContainerDefinitions"] if c.get("Name") == SIDECAR]
    assert len(found) == 1, f"expected one {SIDECAR} container, found {len(found)}"
    return found[0]


def _role_of(arn_value):
    """Logical id of the role behind !GetAtt X.Arn (or !Ref X)."""
    if "!GetAtt" in arn_value:
        return arn_value["!GetAtt"].split(".")[0]
    return arn_value["!Ref"]


def test_all_six_task_definitions_are_checked():
    assert len(TASK_DEFINITIONS) == 6


@pytest.mark.parametrize("lid", sorted(TASK_DEFINITIONS))
def test_task_definition_runs_the_sidecar(lid):
    sidecar = _sidecar(TASK_DEFINITIONS[lid])
    assert sidecar["Essential"] is False, "the application must keep serving if the sidecar stops"
    assert sidecar["Image"] == {"!Sub": SIDECAR_IMAGE}, "the sidecar must come from the private mirror"
    env = {e["Name"]: e["Value"] for e in sidecar.get("Environment", [])}
    assert env.get("MANAGED_INSTANCE_ROLE_NAME") == {"!Ref": "FisSsmManagedInstanceRole"}
    command = sidecar["Command"]
    assert command[:2] == ["/bin/bash", "-c"]
    assert "aws ssm create-activation" in command[2] and "Key=FAULT_INJECTION_SIDECAR,Value=true" in command[2]
    assert not re.search(r"\b(dnf|yum)\b", command[2]), "packages must be baked into the image, not installed in the task"
    assert sidecar.get("LogConfiguration", {}).get("LogDriver") == "awslogs"


def test_every_sidecar_runs_the_same_script():
    scripts = {_sidecar(props)["Command"][2] for props in TASK_DEFINITIONS.values()}
    assert len(scripts) == 1


@pytest.mark.parametrize("lid", sorted(TASK_DEFINITIONS))
def test_task_definition_enables_fault_injection(lid):
    props = TASK_DEFINITIONS[lid]
    assert props.get("PidMode") == "task"
    assert props.get("EnableFaultInjection") is True


@pytest.mark.parametrize("lid", sorted(TASK_DEFINITIONS))
def test_task_has_room_for_the_sidecar(lid):
    assert int(TASK_DEFINITIONS[lid]["Memory"]) >= 1024


def test_no_service_enables_ecs_exec():
    """FIS cannot run the ECS task actions on a task with ECS Exec enabled."""
    services = {lid: r["Properties"] for lid, r in RESOURCES.items() if r["Type"] == "AWS::ECS::Service"}
    assert services
    enabled = sorted(lid for lid, p in services.items() if p.get("EnableExecuteCommand"))
    assert not enabled, f"ECS Exec enabled on {enabled}"


@pytest.mark.parametrize("lid", sorted(TASK_DEFINITIONS))
def test_task_role_gets_the_sidecar_policy(lid):
    role = RESOURCES[_role_of(TASK_DEFINITIONS[lid]["TaskRoleArn"])]["Properties"]
    assert {"!Ref": "FisTaskSsmManagedPolicy"} in role.get("ManagedPolicyArns", [])


@pytest.mark.parametrize("lid", sorted(TASK_DEFINITIONS))
def test_task_roles_keep_env_in_their_names(lid):
    """The repave re-registers task definitions and may pass only role/*${Env}* to ECS."""
    props = TASK_DEFINITIONS[lid]
    for key in ("TaskRoleArn", "ExecutionRoleArn"):
        role = RESOURCES[_role_of(props[key])]["Properties"]
        name = role.get("RoleName")
        # No RoleName: CloudFormation names the role after the apps${Env} stack.
        assert name is None or "${Env}" in str(name), f"{lid} {key}: role name {name} lacks ${{Env}}"


def test_sidecar_policy_grants_exactly_the_documented_permissions():
    statements = RESOURCES["FisTaskSsmManagedPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    grants = sorted((tuple(sorted(s["Action"] if isinstance(s["Action"], list) else [s["Action"]])), str(s["Resource"]))
                    for s in statements if s["Effect"] == "Allow")
    assert grants == sorted([
        (("ssm:AddTagsToResource", "ssm:CreateActivation"), "*"),
        (("iam:PassRole",), str({"!GetAtt": "FisSsmManagedInstanceRole.Arn"})),
    ])
    assert all(s["Effect"] == "Allow" for s in statements)


def test_managed_instance_role():
    props = RESOURCES["FisSsmManagedInstanceRole"]["Properties"]
    principals = [s["Principal"]["Service"] for s in props["AssumeRolePolicyDocument"]["Statement"]]
    assert principals == [["ssm.amazonaws.com"]]
    assert "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" in props["ManagedPolicyArns"]
    actions = sorted(a for p in props["Policies"] for s in p["PolicyDocument"]["Statement"] for a in s["Action"])
    assert actions == ["ssm:DeleteActivation", "ssm:DeregisterManagedInstance"]


def test_mirror_bakes_every_package_the_task_runs():
    buildspec = (DEPLOYMENT / "mirror-sidecar-buildspec.yml").read_text()
    assert 'FROM public.ecr.aws/amazon-ssm-agent/amazon-ssm-agent:' in buildspec
    installed = set()
    for line in re.findall(r"dnf install -y ([^&\"]+)", buildspec):
        installed.update(line.split())
    missing = sorted(REQUIRED_PACKAGES - installed)
    assert not missing, f"not baked into the sidecar image: {missing}"


def test_mirror_pushes_the_sidecar_to_both_regions():
    buildspec = (DEPLOYMENT / "mirror-sidecar-buildspec.yml").read_text()
    assert "REPO=amazon-ssm-agent${ENV_SUFFIX}" in buildspec
    assert 'REGIONS="$REGIONS $STANDBY_REGION"' in buildspec
    assert "docker push $AWS_ACCOUNT_ID.dkr.ecr.$R.amazonaws.com/$REPO:latest" in buildspec


def test_mirror_repository_exists():
    resources = _load_template(DEPLOYMENT / "regionalBaseInfra.yaml")["Resources"]
    names = [r["Properties"].get("RepositoryName") for r in resources.values() if r["Type"] == "AWS::ECR::Repository"]
    assert {"!Sub": "amazon-ssm-agent${Env}"} in names


def test_codebuild_can_push_the_mirror_in_both_regions():
    text = (DEPLOYMENT / "codebuild.yaml").read_text()
    for region in ("PrimaryRegion", "StandbyRegion"):
        assert f"arn:aws:ecr:${{{region}}}:${{AWS::AccountId}}:repository/amazon-ssm-agent${{Env}}" in text, region
