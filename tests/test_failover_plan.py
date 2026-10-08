# SPDX-License-Identifier: MIT-0
"""Step 10 (design 5.6 and 5.10): the Region Switch plan in failover.yaml.

The deactivate workflow scales the remaining Region up, moves DNS, and only then switches the catalog database over,
so traffic moves before the database does. The plan carries the recovery time objective, the associated alarms
(the journey alarms monitoring.yml defines), the report configuration, and, while automatic failover is enabled,
the permission for its execution role to start the plan. Nothing here fails a database over with data loss on its
own: the one Aurora block is a switchover, and the ungraceful choice stays a person's.
"""

import re
from pathlib import Path

import pytest
import yaml

DEPLOYMENT = Path(__file__).resolve().parent.parent / "deployment"
FAILOVER = DEPLOYMENT / "failover.yaml"
MONITORING = DEPLOYMENT / "monitoring.yml"
MAKEFILE = DEPLOYMENT / "Makefile"


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


TEMPLATE = _load_template(FAILOVER)
RESOURCES = TEMPLATE["Resources"]
PLAN = RESOURCES["RegionSwitchPlan"]["Properties"]
ROLE = RESOURCES["RegionSwitchExecutionRole"]
MONITORING_TEMPLATE = _load_template(MONITORING)

SERVICES = ["ui", "catalog", "carts", "checkout", "orders", "assets"]
JOURNEYS = ["home", "cart", "catalog", "orders"]
ROLES = {"primary": "PrimaryRegion", "standby": "StandbyRegion"}
ACCOUNT = "111111111111"
REGIONS = {"PrimaryRegion": "us-east-1", "StandbyRegion": "us-west-2"}
ENV = "-abc1234"


def _workflow(action):
    return next(w for w in PLAN["Workflows"] if w["WorkflowTargetAction"] == action)


def _blocks(steps):
    """Every execution block under these steps, parallel children included."""
    for step in steps:
        yield step
        config = step.get("ExecutionBlockConfiguration", {})
        if "ParallelConfig" in config:
            yield from _blocks(config["ParallelConfig"]["Steps"])


def _sub(value):
    """The string a !Sub gives, with this test's account, partition, Regions and Env."""
    assert set(value) == {"!Sub"}, value
    names = {"AWS::AccountId": ACCOUNT, "AWS::Partition": "aws", "Env": ENV, **REGIONS}

    def fill(match):
        return names[match.group(1)]

    return re.sub(r"\$\{([^}]+)\}", fill, value["!Sub"])


def _policy_statements(role):
    (policy,) = role["Properties"]["Policies"]
    return policy["PolicyDocument"]["Statement"]


# --- the deactivate workflow --------------------------------------------------------------------


class TestDeactivateWorkflow:

    def test_scale_up_comes_first_then_dns_then_the_database(self):
        names = [s["Name"] for s in _workflow("deactivate")["Steps"]]
        assert names == ["scale-up-ecs-services", "shift-dns-traffic-away", "switch-over-catalog-db"]

    def test_traffic_moves_before_the_database_does(self):
        names = [s["Name"] for s in _workflow("deactivate")["Steps"]]
        assert names.index("shift-dns-traffic-away") < names.index("switch-over-catalog-db")

    def test_the_first_step_scales_the_six_services_in_parallel(self):
        step = _workflow("deactivate")["Steps"][0]
        assert step["ExecutionBlockType"] == "Parallel"
        children = step["ExecutionBlockConfiguration"]["ParallelConfig"]["Steps"]
        assert sorted(c["Name"] for c in children) == sorted(f"scale-{s}" for s in SERVICES)
        assert {c["ExecutionBlockType"] for c in children} == {"ECSServiceScaling"}

    @pytest.mark.parametrize("service", SERVICES)
    def test_each_scaling_block_keeps_its_settings(self, service):
        # Unchanged by step 10: doubling the 24-hour maximum, 15 minutes each, both Regions' services.
        step = _workflow("deactivate")["Steps"][0]
        child = next(c for c in step["ExecutionBlockConfiguration"]["ParallelConfig"]["Steps"] if c["Name"] == f"scale-{service}")
        config = child["ExecutionBlockConfiguration"]["EcsCapacityIncreaseConfig"]
        assert config["TargetPercent"] == 200
        assert config["CapacityMonitoringApproach"] == "containerInsightsMaxInLast24Hours"
        assert config["TimeoutMinutes"] == 15
        assert [s["ClusterArn"] for s in config["Services"]] == [{"!Ref": "PrimaryClusterArn"}, {"!Ref": "StandbyClusterArn"}]
        for s, region in zip(config["Services"], ("PrimaryRegion", "StandbyRegion")):
            assert s["ServiceArn"]["!Sub"][0] == f"arn:aws:ecs:${{{region}}}:${{AWS::AccountId}}:service/${{ClusterName}}/{service}${{Env}}"

    def test_the_dns_step_is_unchanged(self):
        step = _workflow("deactivate")["Steps"][1]
        config = step["ExecutionBlockConfiguration"]["Route53HealthCheckConfig"]
        assert step["ExecutionBlockType"] == "Route53HealthCheck"
        assert config["TimeoutMinutes"] == 5
        assert config["RecordName"] == {"!Sub": "store.${DomainName}"}
        assert [(r["RecordSetIdentifier"], r["Region"]) for r in config["RecordSets"]] == [
            ("PrimaryRegion", {"!Ref": "PrimaryRegion"}), ("StandbyRegion", {"!Ref": "StandbyRegion"})]

    def test_the_database_step_is_a_switchover_with_ten_minutes(self):
        step = _workflow("deactivate")["Steps"][2]
        config = step["ExecutionBlockConfiguration"]["GlobalAuroraConfig"]
        assert step["ExecutionBlockType"] == "AuroraGlobalDatabase"
        assert config["Behavior"] == "switchoverOnly"
        assert config["TimeoutMinutes"] == 10
        assert config["GlobalClusterIdentifier"] == {"!Sub": "catalog-global-db-cluster${Env}"}
        assert config["DatabaseClusterArns"] == [{"!Ref": "PrimaryCatalogDbArn"}, {"!Ref": "StandbyCatalogDbArn"}]

    def test_the_ungraceful_choice_is_only_what_a_person_switches_to(self):
        # An automatic run never fails a database over with data loss: Behavior stays switchoverOnly, and
        # Ungraceful names what a person gets if they switch the paused run to ungraceful.
        config = _workflow("deactivate")["Steps"][2]["ExecutionBlockConfiguration"]["GlobalAuroraConfig"]
        assert config["Ungraceful"] == {"Ungraceful": "failover"}

    def test_no_block_anywhere_fails_a_database_over_by_default(self):
        for workflow in PLAN["Workflows"]:
            for block in _blocks(workflow["Steps"]):
                if block["ExecutionBlockType"] == "AuroraGlobalDatabase":
                    assert block["ExecutionBlockConfiguration"]["GlobalAuroraConfig"]["Behavior"] == "switchoverOnly", block["Name"]

    def test_the_activate_workflow_only_restores_dns(self):
        steps = _workflow("activate")["Steps"]
        assert [(s["Name"], s["ExecutionBlockType"]) for s in steps] == [("restore-dns-traffic", "Route53HealthCheck")]

    def test_the_plan_has_the_two_workflows_an_active_active_plan_needs(self):
        assert PLAN["RecoveryApproach"] == "activeActive"
        assert sorted(w["WorkflowTargetAction"] for w in PLAN["Workflows"]) == ["activate", "deactivate"]


# --- recovery objective, associated alarms ------------------------------------------------------


def _expected_alarms():
    """key -> (alarm type, alarm base name, role)."""
    out = {}
    for role in ROLES:
        for view in ("lcl", "rmt"):
            for j in JOURNEYS:
                out[f"journey-{view}-{j}-{role}"] = ("trigger", f"journey-{view}-{j}", role)
        out[f"region-degraded-{role}"] = ("trigger", "region-degraded", role)
        for j in JOURNEYS:
            out[f"journey-global-{j}-{role}"] = ("applicationHealth", f"journey-global-{j}", role)
    return out


ASSOCIATED = PLAN.get("AssociatedAlarms", {})      # .get: against an older template the tests fail one by one


def _monitoring_bases():
    """The base names (without the Region and Env) of the journey and region-degraded alarms monitoring.yml defines."""
    bases = set()
    for res in MONITORING_TEMPLATE["Resources"].values():
        if res["Type"] not in ("AWS::CloudWatch::Alarm", "AWS::CloudWatch::CompositeAlarm"):
            continue
        name = res["Properties"]["AlarmName"]["!Sub"]
        if name.startswith(("journey-", "region-degraded-")):
            assert name.endswith("-${AWS::Region}${Env}"), name
            bases.add(name[: -len("-${AWS::Region}${Env}")])
    return bases


class TestRecoveryObjectiveAndAlarms:

    def test_the_recovery_time_objective_is_ten_minutes(self):
        assert PLAN["RecoveryTimeObjectiveMinutes"] == 10

    def test_the_plan_associates_exactly_these_twenty_six_alarms(self):
        assert set(ASSOCIATED) == set(_expected_alarms())
        assert len(ASSOCIATED) == 26

    @pytest.mark.parametrize("key,expected", sorted(_expected_alarms().items()))
    def test_each_alarm_has_its_type_and_arn(self, key, expected):
        alarm_type, base, role = expected
        region = REGIONS[ROLES[role]]
        entry = ASSOCIATED[key]
        assert entry["AlarmType"] == alarm_type
        assert _sub(entry["ResourceIdentifier"]) == f"arn:aws:cloudwatch:{region}:{ACCOUNT}:alarm:{base}-{region}{ENV}"
        assert set(entry) == {"AlarmType", "ResourceIdentifier"}

    def test_keys_are_plain_labels(self):
        # A CloudFormation map key can't hold the Env or a Region parameter, so the key can't be the alarm's name.
        assert all(re.fullmatch(r"[a-z-]+-(primary|standby)", key) for key in ASSOCIATED)

    def test_application_health_alarms_are_the_global_ones_and_only_those(self):
        health = {k for k, v in ASSOCIATED.items() if v["AlarmType"] == "applicationHealth"}
        assert health == {k for k in ASSOCIATED if k.startswith("journey-global-")}
        assert len(health) == 8

    def test_every_journey_and_region_degraded_alarm_monitoring_defines_is_associated_and_nothing_else(self):
        # A journey added to monitoring.yml has to be added to the plan, and a hop alarm is evidence, not a plan input.
        bases = {base for _, base, _ in _expected_alarms().values()}
        assert bases == _monitoring_bases()

    def test_the_plan_names_only_alarms_that_monitoring_creates_in_that_region(self):
        # monitoring.yml names an alarm <base>-${AWS::Region}${Env}, and the stack runs in both Regions.
        for entry in ASSOCIATED.values():
            arn = _sub(entry["ResourceIdentifier"])
            _, _, service, region, _, resource = arn.split(":", 5)
            assert service == "cloudwatch" and region in REGIONS.values()
            base = resource[len("alarm:"):].removesuffix(f"-{region}{ENV}")
            assert base in _monitoring_bases(), arn


# --- execution reports --------------------------------------------------------------------------


class TestReports:

    def test_one_s3_output_under_executions(self):
        (output,) = PLAN["ReportConfiguration"]["ReportOutput"]
        assert output == {"S3Configuration": {"BucketOwner": {"!Ref": "AWS::AccountId"},
                                              "BucketPath": {"!Sub": "${ReportsBucket}/executions"}}}

    def test_the_bucket_blocks_public_access_encrypts_and_expires_reports(self):
        bucket = RESOURCES["ReportsBucket"]
        assert bucket["Type"] == "AWS::S3::Bucket"
        props = bucket["Properties"]
        assert props["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True}
        (rule,) = props["BucketEncryption"]["ServerSideEncryptionConfiguration"]
        assert rule["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
        expiry = [r for r in props["LifecycleConfiguration"]["Rules"] if "ExpirationInDays" in r]
        assert [(r["Status"], r["ExpirationInDays"]) for r in expiry] == [("Enabled", 90)]
        assert "Prefix" not in expiry[0], "the 90 days apply to everything in the bucket"

    def test_the_bucket_refuses_plain_http(self):
        policy = RESOURCES["ReportsBucketPolicy"]["Properties"]
        assert policy["Bucket"] == {"!Ref": "ReportsBucket"}
        (statement,) = policy["PolicyDocument"]["Statement"]
        assert statement["Effect"] == "Deny" and statement["Principal"] == "*" and statement["Action"] == "s3:*"
        assert statement["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
        assert {"!GetAtt": "ReportsBucket.Arn"} in statement["Resource"]
        assert {"!Sub": "${ReportsBucket.Arn}/*"} in statement["Resource"]

    def test_the_execution_role_writes_reports_under_executions_and_nowhere_else_in_s3(self):
        s3 = [s for s in _policy_statements(ROLE) if any(a.startswith("s3:") for a in _as_list(s["Action"]))]
        assert s3 == [{"Effect": "Allow", "Action": "s3:PutObject", "Resource": {"!Sub": "${ReportsBucket.Arn}/executions/*"}}]

    def test_the_stack_outputs_the_bucket_name_for_destroy_region_switch(self):
        assert TEMPLATE["Outputs"]["ReportsBucketName"]["Value"] == {"!Ref": "ReportsBucket"}


def _as_list(value):
    return value if isinstance(value, list) else [value]


# --- automatic failover switch and the self-start permission -------------------------------------


class TestAutomaticFailoverSwitch:

    def test_the_parameter_is_enabled_or_disabled_and_defaults_to_enabled(self):
        p = TEMPLATE["Parameters"]["AutomaticFailover"]
        assert (p["Type"], p["Default"], p["AllowedValues"]) == ("String", "enabled", ["enabled", "disabled"])

    def test_the_condition_follows_the_parameter(self):
        assert TEMPLATE["Conditions"]["AutomaticFailoverEnabled"] == {"!Equals": [{"!Ref": "AutomaticFailover"}, "enabled"]}

    def test_the_execution_role_may_start_this_plan_and_only_this_plan(self):
        policy = RESOURCES["RegionSwitchSelfStartPolicy"]
        assert policy["Type"] == "AWS::IAM::Policy"
        assert policy["Condition"] == "AutomaticFailoverEnabled"
        props = policy["Properties"]
        assert props["Roles"] == [{"!Ref": "RegionSwitchExecutionRole"}]
        (statement,) = props["PolicyDocument"]["Statement"]
        assert statement == {"Effect": "Allow", "Action": "arc-region-switch:StartPlanExecution",
                             "Resource": {"!Ref": "RegionSwitchPlan"}}

    def test_the_permission_is_a_separate_policy_because_the_plan_needs_the_role_first(self):
        # A role policy that named the plan would make the role and the plan wait for each other.
        for statement in _policy_statements(ROLE):
            assert "arc-region-switch:StartPlanExecution" not in _as_list(statement["Action"])
        assert PLAN["ExecutionRole"] == {"!GetAtt": "RegionSwitchExecutionRole.Arn"}

    def test_nothing_else_gives_the_role_the_permission(self):
        holders = [name for name, res in RESOURCES.items()
                   if "arc-region-switch:StartPlanExecution" in yaml.dump(res)]
        assert holders == ["RegionSwitchSelfStartPolicy"]


# --- the README ----------------------------------------------------------------------------------

README = (DEPLOYMENT.parent / "README.md").read_text()
SECTION = README[README.index("### 3. Automatic failover and fail-back"):]
SECTION = SECTION[:SECTION.index("\n## ")]


class TestReadme:
    """The README describes the plan's order, limits and runbook. What it says has to be what the template does."""

    def test_the_numbered_steps_are_in_the_plans_order(self):
        order = [SECTION.index(text) for text in ("It scales up the ECS services", "It moves DNS.", "It switches the catalog database over.")]
        assert order == sorted(order)

    def test_the_stated_limits_are_the_templates(self):
        steps = _workflow("deactivate")["Steps"]
        scale = steps[0]["ExecutionBlockConfiguration"]["ParallelConfig"]["Steps"][0]["ExecutionBlockConfiguration"]["EcsCapacityIncreaseConfig"]
        dns = steps[1]["ExecutionBlockConfiguration"]["Route53HealthCheckConfig"]
        database = steps[2]["ExecutionBlockConfiguration"]["GlobalAuroraConfig"]
        assert f"to twice the highest count they reached in the last 24 hours ({scale['TimeoutMinutes']} minutes allowed)" in SECTION
        assert scale["TargetPercent"] == 200
        assert f"({dns['TimeoutMinutes']} minutes allowed)" in SECTION and f"({database['TimeoutMinutes']} minutes allowed)" in SECTION

    def test_the_runbook_names_the_plans_database_step_and_the_api_actions(self):
        names = [s["Name"] for s in _workflow("deactivate")["Steps"]]
        used = re.findall(r"--step-name ([a-z-]+)", SECTION)
        assert used and set(used) == {"switch-over-catalog-db"} and "switch-over-catalog-db" in names
        for action in ("cancel-plan-execution", "--action-to-take skip", "--action-to-take switchToUngraceful"):
            assert action in SECTION

    def test_the_rationale_for_moving_traffic_first_is_there(self):
        assert "**Why traffic moves before the catalog database.**" in SECTION
        assert "The automation never makes that trade on its own." in SECTION

    def test_the_numbers_note_under_the_diagram_gives_the_real_order(self):
        assert "The plan runs them in the order 1, 2, 4, 3" in README

    def test_the_stated_objective_and_retention_are_the_templates(self):
        assert f"recovery time objective of {PLAN['RecoveryTimeObjectiveMinutes']} minutes" in SECTION
        days = next(r for r in RESOURCES["ReportsBucket"]["Properties"]["LifecycleConfiguration"]["Rules"] if "ExpirationInDays" in r)["ExpirationInDays"]
        assert f"Reports expire after {days} days" in SECTION


# --- the Makefile -------------------------------------------------------------------------------


def _recipe(target):
    lines = MAKEFILE.read_text().split("\n")
    start = next(i for i, line in enumerate(lines) if re.match(rf"^{re.escape(target)}\s*:", line))
    recipe = []
    for line in lines[start + 1:]:
        if line.startswith("\t"):
            recipe.append(line)
        elif line.strip() and not line.startswith("#"):
            break
    return "\n".join(recipe)


class TestMakefile:

    def test_the_variable_defaults_to_enabled(self):
        assert re.search(r"^AUTOMATIC_FAILOVER=enabled$", MAKEFILE.read_text(), re.M)

    def test_the_plan_deploy_passes_it_to_the_template(self):
        assert "AutomaticFailover=$(AUTOMATIC_FAILOVER)" in _recipe("region-switch-plan")

    def test_destroy_empties_the_reports_bucket_before_deleting_the_stack(self):
        recipe = _recipe("destroy-region-switch")
        looked_up = recipe.index("ReportsBucketName")
        emptied = recipe.index("./cleanup.sh $(REPORTS_BUCKET)")
        deleted = recipe.index("delete-stack --stack-name region-switch${ENV}")
        assert looked_up < emptied < deleted

    def test_destroy_skips_the_bucket_when_the_stack_has_none(self):
        # A deployment from before this change has no ReportsBucketName output: describe-stacks prints None or
        # nothing, and the recipe must go on to delete the stack.
        recipe = _recipe("destroy-region-switch")
        assert '[ -n "$(REPORTS_BUCKET)" ] && [ "$(REPORTS_BUCKET)" != "None" ]' in recipe
