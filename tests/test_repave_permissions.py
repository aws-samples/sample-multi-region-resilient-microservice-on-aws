"""The weekly repave's role can create every tagged resource a Tag change creates.

The repave (deployment/self-update.yaml) rolls the apps stack onto new images
by changing only its Tag parameter. CloudFormation then registers a new
revision of every task definition whose image references Tag, and ECS
authorizes the tags on a new revision as ecs:TagResource. On 2026-10-05 the
first repave after the task definitions gained `service` tags rolled back in
both Regions because the repave role lacked that permission. On 2026-10-06 the
first repave after the task definitions switched to `!GetAtt <Role>.Arn`
rolled back the same way: CloudFormation reads the role to resolve the ARN,
with the repave role's permissions, and the role could not iam:GetRole.

Run with:  pytest tests/test_repave_permissions.py -v
"""

from pathlib import Path

import yaml

DEPLOYMENT = Path(__file__).parent.parent / "deployment"


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


def _uses_tag_parameter(node):
    """True if the node references the Tag parameter (Ref Tag or ${Tag} in a Sub)."""
    if isinstance(node, dict):
        if node.get("!Ref") == "Tag" or node.get("Ref") == "Tag":
            return True
        sub = node.get("!Sub", node.get("Fn::Sub"))
        if sub is not None:
            text = sub if isinstance(sub, str) else sub[0]
            if isinstance(text, str) and "${Tag}" in text:
                return True
        return any(_uses_tag_parameter(v) for v in node.values())
    if isinstance(node, list):
        return any(_uses_tag_parameter(v) for v in node)
    return False


def _repave_role_statements():
    resources = _load_template(DEPLOYMENT / "self-update.yaml")["Resources"]
    roles = [r for r in resources.values() if r.get("Type") == "AWS::IAM::Role"
             and r["Properties"].get("AssumeRolePolicyDocument", {}).get("Statement", [{}])[0]
             .get("Principal", {}).get("Service") == "codebuild.amazonaws.com"]
    assert len(roles) == 1, "expected one CodeBuild role in self-update.yaml"
    return [s for p in roles[0]["Properties"]["Policies"] for s in p["PolicyDocument"]["Statement"]]


def _sub_text(value):
    return value.get("!Sub") if isinstance(value, dict) else value


def test_a_tag_change_only_creates_task_definitions():
    """Only task definitions reference Tag, so they are what a repave creates."""
    resources = _load_template(DEPLOYMENT / "ecs.yaml")["Resources"]
    users = {lid: r["Type"] for lid, r in resources.items() if _uses_tag_parameter(r.get("Properties"))}
    assert users, "no resource references the Tag parameter"
    assert set(users.values()) == {"AWS::ECS::TaskDefinition"}, users


def test_repave_role_can_tag_task_definitions_at_registration():
    resources = _load_template(DEPLOYMENT / "ecs.yaml")["Resources"]
    tagged = [lid for lid, r in resources.items()
              if r["Type"] == "AWS::ECS::TaskDefinition" and (r.get("Properties") or {}).get("Tags")]
    if not tagged:
        return
    grants = [s for s in _repave_role_statements()
              if s.get("Effect") == "Allow" and "ecs:TagResource" in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])]
    assert grants, f"task definitions {tagged} carry tags but the repave role cannot ecs:TagResource"
    grant = grants[0]
    arns = sorted(_sub_text(r) for r in grant["Resource"])
    assert arns == sorted([
        "arn:aws:ecs:${PrimaryRegion}:${AWS::AccountId}:task-definition/apps${Env}-*",
        "arn:aws:ecs:${StandbyRegion}:${AWS::AccountId}:task-definition/apps${Env}-*",
    ]), arns
    assert grant.get("Condition") == {"StringEquals": {"ecs:CreateAction": "RegisterTaskDefinition"}}


# What the repave role must be allowed to call for CloudFormation to resolve
# !GetAtt <resource>.<attribute> on a task definition it updates, by the type
# of the resource read. A task definition that gains a !GetAtt on another type
# fails test_repave_role_can_resolve_the_get_atts_of_task_definitions until its
# read is added here and to the role.
GET_ATT_READS = {
    "AWS::IAM::Role": ("iam:GetRole", "arn:aws:iam::${AWS::AccountId}:role/apps${Env}-*"),
}


def _get_att_targets(node, out):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("!GetAtt", "Fn::GetAtt"):
                out.add((value if isinstance(value, str) else ".".join(value)).split(".")[0])
            else:
                _get_att_targets(value, out)
    elif isinstance(node, list):
        for value in node:
            _get_att_targets(value, out)
    return out


def test_repave_role_can_resolve_the_get_atts_of_task_definitions():
    """CloudFormation resolves !GetAtt with the caller's permissions by reading the resource.

    On 2026-10-06 the first repave after the task definitions switched from
    !Ref <Role> (the role name, which needs no call) to !GetAtt <Role>.Arn rolled
    back in both Regions: the repave role could not iam:GetRole.
    """
    resources = _load_template(DEPLOYMENT / "ecs.yaml")["Resources"]
    statements = [s for s in _repave_role_statements() if s.get("Effect") == "Allow"]
    checked = 0
    for lid, res in resources.items():
        if res["Type"] != "AWS::ECS::TaskDefinition" or not _uses_tag_parameter(res.get("Properties")):
            continue
        for target in sorted(_get_att_targets(res.get("Properties"), set())):
            target_type = resources[target]["Type"]
            assert target_type in GET_ATT_READS, \
                f"{lid} reads {target} ({target_type}) by !GetAtt; add the call the repave role needs"
            action, arn = GET_ATT_READS[target_type]
            # The pattern only covers names CloudFormation generates from the stack name.
            name = (resources[target].get("Properties") or {}).get("RoleName")
            assert name is None or _sub_text(name).startswith("apps${Env}-"), f"{target} is named {name}"
            grants = [s for s in statements
                      if action in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])]
            arns = {_sub_text(r) for s in grants
                    for r in (s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]])}
            assert arn in arns, f"{lid} reads {target} by !GetAtt, but the repave role cannot {action} on {arn}"
            assert all("Condition" not in s for s in grants), f"{action} must not be conditional"
            checked += 1
    assert checked, "no task definition reads anything by !GetAtt; is the check still needed?"
