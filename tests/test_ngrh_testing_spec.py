"""Step 7: the test spec (deployment/ngrh-tests.json, design 5.8 and 6.1).

The schema is strict, and the shipped spec is checked against the templates it refers to: every alarm
it names exists in monitoring.yml, the broker lookup finds a resource ecs.yaml declares, and the outputs
the tool reads are outputs ngrh.yaml declares. A spec that passed the schema but named an alarm
monitoring.yml does not create would otherwise fail only at the first live reconcile.
"""

import copy
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

TESTS = Path(__file__).resolve().parent
DEPLOYMENT = TESTS.parent / "deployment"
sys.path.insert(0, str(DEPLOYMENT))
sys.path.insert(0, str(TESTS))

from ngrh_testing import context, spec  # noqa: E402

SPEC_FILE = DEPLOYMENT / "ngrh-tests.json"


def shipped():
    return json.loads(SPEC_FILE.read_text())


def problems_of(data):
    with pytest.raises(spec.SpecError) as e:
        spec.parse(data)
    return e.value.problems


def one(data):
    data = copy.deepcopy(data)
    return data, data["tests"][0]


# --- the shipped spec ---------------------------------------------------------------------------

class TestShippedSpec:

    def test_it_parses_and_holds_the_orders_dependency_test(self):
        parsed = spec.load(str(SPEC_FILE))
        assert parsed.names() == ["orders-broker-dependency"]
        test = parsed.tests[0]
        assert (test.service, test.template) == ("orders", "aws-dependency-validation:rtdep001")
        assert test.duration_minutes == 15
        assert test.parameters["dependencies"] == (spec.Lookup("mq-broker-host", "primary"),)
        assert [a.name for a in test.success_alarms] == ["journey-lcl-orders", "journey-global-orders"]
        assert [a.name for a in test.stop_alarms] == ["region-degraded"]
        assert len(test.success_alarms) + len(test.observability_alarms) <= spec.MAX_SOURCES

    def test_the_dependency_test_is_expected_to_pass_since_the_publish_is_best_effort(self):
        # Before step 9 the order-created publish ran on the request thread, so cutting the broker failed
        # the orders journeys (run bea5d9f4 of 2026-10-07 showed it). Step 9 moved it off the request path
        # and flipped this and the ground-truth doc together.
        assert spec.load(str(SPEC_FILE)).tests[0].expected == "PASS"

    def test_select_takes_one_test_or_all(self):
        parsed = spec.load(str(SPEC_FILE))
        assert [t.name for t in parsed.select("all")] == parsed.names()
        assert [t.name for t in parsed.select("orders-broker-dependency")] == ["orders-broker-dependency"]
        with pytest.raises(spec.SpecError, match="no test named 'nope'.*orders-broker-dependency"):
            parsed.select("nope")

    def test_a_missing_file_is_a_spec_error_not_a_traceback(self, tmp_path):
        with pytest.raises(spec.SpecError, match="cannot read"):
            spec.load(str(tmp_path / "absent.json"))


# --- the schema ---------------------------------------------------------------------------------

def _mutations():
    def top(key, value):
        def apply(d):
            d[key] = value
        return apply

    def test_key(key, value):
        def apply(d):
            d["tests"][0][key] = value
        return apply

    def drop(key):
        def apply(d):
            del d["tests"][0][key]
        return apply

    def param(key, value):
        def apply(d):
            d["tests"][0]["parameters"][key] = value
        return apply

    def many_alarms(key, n, prefix="journey-"):
        def apply(d):
            d["tests"][0][key] = [{"name": f"{prefix}x{i}", "region": "primary"} for i in range(n)]
        return apply

    return [
        ("unknown top-level key", top("colour", 1), "unknown key 'colour'"),
        ("wrong version", top("version", 2), "version must be 1"),
        ("no tests", top("tests", []), "tests must be a non-empty list"),
        ("unknown test key", test_key("colour", 1), "unknown key 'colour'"),
        ("missing success alarms", drop("successAlarms"), "missing 'successAlarms'"),
        ("missing expected", drop("expected"), "missing 'expected'"),
        ("bad name", test_key("name", "Orders_Test"), "name must be lower-case"),
        ("unknown service", test_key("service", "payments"), "service must be one of"),
        ("unknown template", test_key("template", "aws-az-recovery:rtaz001"), "is not one the suite uses"),
        ("malformed template", test_key("template", "orders"), "template must look like"),
        ("bad expected", test_key("expected", "MAYBE"), "expected must be one of PASS, FAIL, UNKNOWN"),
        ("parameter the template lacks", param("availabilityZone", ["use1-az1"]), "'availabilityZone' is not a parameter"),
        ("required parameter missing", lambda d: d["tests"][0]["parameters"].pop("region"), "requires 'region'"),
        ("single-valued parameter with two values", param("region", ["primary", "standby"]), "takes exactly one value"),
        ("empty parameter", param("dependencies", []), "non-empty list"),
        ("too many values", param("dependencies", [f"h{i}.example.com" for i in range(11)]), "at most 10 values"),
        ("duration zero", param("duration", ["0"]), "whole number of minutes"),
        ("duration too long", param("duration", ["721"]), "whole number of minutes"),
        ("duration not a number", param("duration", ["soon"]), "whole number of minutes"),
        ("region parameter that is no region", param("region", ["banana"]), "'primary', 'standby' or a Region name"),
        ("unknown lookup", param("dependencies", [{"lookup": "dns", "region": "primary"}]), "unknown lookup 'dns'"),
        ("lookup for a region parameter", param("region", [{"lookup": "mq-broker-host", "region": "primary"}]),
         "a lookup can't supply region"),
        ("lookup with a bad region", param("dependencies", [{"lookup": "mq-broker-host", "region": "moon"}]), "region must be"),
        ("placeholder in a host list", param("dependencies", ["primary"]), "placeholder is only meaningful"),
        ("value that is a number", param("dependencies", [5]), "non-empty string or an object"),
        ("success alarm that is no journey alarm",
         test_key("successAlarms", [{"name": "hop-orders-slow", "region": "primary"}]), "must start with 'journey-'"),
        ("no success alarm", test_key("successAlarms", []), "at least one success alarm"),
        ("more than five sources", many_alarms("observabilityAlarms", 4, "hop-"), "at most 5 sources"),
        ("more than five stop alarms", many_alarms("stopAlarms", 6), "at most 5 alarms"),
        ("alarm reference with an extra key",
         test_key("stopAlarms", [{"name": "region-degraded", "region": "primary", "x": 1}]), "exactly 'name' and 'region'"),
        ("alarm name with a suffix",
         test_key("stopAlarms", [{"name": "Region_Degraded", "region": "primary"}]), "lower-case alarm name"),
        ("alarm in no region", test_key("stopAlarms", [{"name": "region-degraded", "region": "both"}]), "region must be"),
        ("unknown evidence group", test_key("evidenceAlarms", [{"group": "all", "region": "primary"}]), "unknown group 'all'"),
        ("evidence with name and group",
         test_key("evidenceAlarms", [{"name": "x", "group": "hop", "region": "primary"}]), "either 'name' or 'group'"),
        ("run check nothing implements", test_key("runChecks", ["no-plan-execution"]), "none are implemented yet"),
        ("run checks that are not a list", test_key("runChecks", "none"), "runChecks must be a list"),
    ]


class TestSchema:

    @pytest.mark.parametrize("label,mutate,message", _mutations(), ids=[m[0] for m in _mutations()])
    def test_each_defect_is_named(self, label, mutate, message):
        data = copy.deepcopy(shipped())
        mutate(data)
        assert any(re.search(message, p) for p in problems_of(data)), f"{label}: {message!r} not found"

    def test_every_problem_is_reported_not_only_the_first(self):
        data, test = one(shipped())
        test["service"] = "payments"
        test["expected"] = "MAYBE"
        test["successAlarms"] = []
        assert len(problems_of(data)) >= 3

    def test_two_tests_may_not_share_a_name(self):
        data = shipped()
        other = copy.deepcopy(data["tests"][0])
        other["service"] = "cart"
        data["tests"].append(other)
        assert any("used more than once" in p for p in problems_of(data))

    def test_two_tests_may_not_share_a_service_and_template(self):
        # Resilience Hub allows one test per service and template, so these would overwrite each other.
        data = shipped()
        other = copy.deepcopy(data["tests"][0])
        other["name"] = "orders-broker-dependency-again"
        data["tests"].append(other)
        assert any("one test per service and template" in p for p in problems_of(data))

    def test_a_key_repeated_in_one_object_is_rejected(self):
        with pytest.raises(spec.SpecError, match="appears twice"):
            spec.load_text('{"version": 1, "version": 1, "tests": []}')

    def test_text_that_is_not_json_is_a_spec_error(self):
        with pytest.raises(spec.SpecError, match="not valid JSON"):
            spec.load_text("{")

    def test_evidence_groups_expand_to_the_ten_hop_alarms(self):
        group = spec.EvidenceRef("primary", group="hop")
        assert len(group.alarm_names()) == 10 and group.alarm_names()[0] == "hop-ui-errors"
        assert spec.EvidenceRef("standby", name="region-degraded").alarm_names() == ("region-degraded",)

    def test_the_duration_is_the_parameter_or_else_the_templates_default(self):
        test = spec.load(str(SPEC_FILE)).tests[0]
        assert test.duration_minutes == 15
        data, t = one(shipped())
        del t["parameters"]["duration"]
        assert spec.parse(data).tests[0].duration_minutes == 30
        assert {k: v.default_duration for k, v in spec.TEMPLATES.items()} == {
            "aws-dependency-validation:rtdep001": 30, "aws-multi-region-isolation:rtmr001": 180,
            "aws-multi-region-recovery:rtmr002": 30}


# --- the spec against the templates ---------------------------------------------------------------

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


def _template(name):
    loader = _CfnLoader((DEPLOYMENT / name).read_text())
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _alarm_bases():
    """The alarm names monitoring.yml creates, without their -<Region><Env> suffix."""
    bases = set()
    for res in _template("monitoring.yml")["Resources"].values():
        if res["Type"] in ("AWS::CloudWatch::Alarm", "AWS::CloudWatch::CompositeAlarm"):
            name = res["Properties"]["AlarmName"]["!Sub"]
            assert name.endswith("-${AWS::Region}${Env}"), name
            bases.add(name[: -len("-${AWS::Region}${Env}")])
    return bases


class TestSpecAgainstTemplates:

    def test_every_alarm_the_spec_names_is_created_by_monitoring_yml(self):
        bases = _alarm_bases()
        for test in spec.load(str(SPEC_FILE)).tests:
            named = [a.name for a in test.success_alarms + test.observability_alarms + test.stop_alarms]
            named += [n for e in test.evidence_alarms for n in e.alarm_names()]
            missing = sorted(set(named) - bases)
            assert not missing, f"{test.name} names alarms monitoring.yml does not create: {missing}"

    def test_the_hop_group_is_the_ten_hop_alarms_monitoring_yml_creates(self):
        hops = {b for b in _alarm_bases() if b.startswith("hop-")}
        assert hops == set(spec.HOP_ALARMS)

    def test_the_lookups_the_spec_can_use_are_the_lookups_the_tool_has(self):
        assert set(spec.KNOWN_LOOKUPS) == set(context.LOOKUPS)

    def test_the_broker_lookup_reads_a_resource_ecs_yaml_declares(self):
        broker = _template("ecs.yaml")["Resources"]["OrdersMqBroker"]
        assert broker["Type"] == "AWS::AmazonMQ::Broker"

    def test_every_output_the_tool_reads_is_an_output_ngrh_yaml_declares(self):
        outputs = set(_template("ngrh.yaml")["Outputs"])
        needed = {"ServiceArns", "TestExperimentRoleName", "InvokerRoleName"}
        needed |= {s.capitalize() + "ServiceArn" for s in spec.SERVICES}
        assert needed <= outputs, sorted(needed - outputs)

    def test_the_services_the_spec_can_name_are_the_services_ngrh_yaml_declares(self):
        resources = _template("ngrh.yaml")["Resources"]
        declared = {name[: -len("Service")].lower() for name, r in resources.items()
                    if name.endswith("Service") and r["Type"] == "AWS::ResilienceHubV2::Service"}
        assert declared == set(spec.SERVICES)

    def test_every_source_alarm_carries_a_tag_its_service_discovers(self):
        # Resilience Hub takes only the alarms it discovered for a service as that service's test sources,
        # and discovers them by the service's tag input sources (ngrh.yaml). On 2026-10-07 StartTestRun
        # refused the orders test because hop-checkout-errors, tagged checkout, was one of its sources;
        # the report shows that alarm as evidence instead. An alarm with no tag of its own carries the
        # monitoring stack's, service=shared (test_ngrh_tag_discovery pins the Makefile passing it).
        scope = {}
        for name, resource in _template("ngrh.yaml")["Resources"].items():
            if resource["Type"] == "AWS::ResilienceHubV2::Service":
                filters = [(tag["Key"], set(tag["Values"]))
                           for source in resource["Properties"]["InputSources"]
                           for tag in source["ResourceConfiguration"]["ResourceTags"]]
                scope[name[: -len("Service")].lower()] = filters
        alarm_tags = {}
        for resource in _template("monitoring.yml")["Resources"].values():
            if resource["Type"] in ("AWS::CloudWatch::Alarm", "AWS::CloudWatch::CompositeAlarm"):
                base = resource["Properties"]["AlarmName"]["!Sub"][: -len("-${AWS::Region}${Env}")]
                alarm_tags[base] = {"service": "shared",
                                    **{t["Key"]: t["Value"] for t in resource["Properties"].get("Tags", [])}}
        for test in spec.load(str(SPEC_FILE)).tests:
            for alarm in test.success_alarms + test.observability_alarms:
                tags = alarm_tags[alarm.name]
                assert any(tags.get(key) in values for key, values in scope[test.service]), (
                    f"{test.name}: source alarm {alarm.name} is tagged {tags}, which service {test.service} does not "
                    f"discover ({scope[test.service]}); StartTestRun would refuse the run. Move it to the evidence alarms")

    def test_the_fault_region_parameter_of_every_template_is_one_it_takes(self):
        for template, shape in spec.TEMPLATES.items():
            assert shape.fault_region in shape.parameters and shape.fault_region in shape.required, template
            assert set(shape.required) <= set(shape.parameters), template
            assert set(shape.multi_valued) <= set(shape.parameters), template


# --- the documents and templates around the tool -------------------------------------------------------

ROOT = DEPLOYMENT.parent
GROUND_TRUTH = ROOT / "docs" / "ngrh-test-ground-truth.md"


def _ground_truth():
    """{test name: expected result} from the '## <name>' sections holding an 'Expected result: X' line."""
    found = {}
    name = None
    for line in GROUND_TRUTH.read_text().splitlines():
        heading = re.match(r"^## (\S+)$", line)
        if heading:
            name = heading.group(1)
        expected = re.match(r"^Expected result: (PASS|FAIL|UNKNOWN)$", line)
        if expected:
            assert name not in found, f"{name} has two expected results"
            found[name] = expected.group(1)
    return found


class TestDocsAndTemplates:

    def test_the_ground_truth_doc_gives_each_tests_expected_result_as_the_spec_does(self):
        parsed = spec.load(str(SPEC_FILE))
        assert _ground_truth() == {t.name: t.expected for t in parsed.tests}

    def test_the_fis_log_group_the_tests_log_to_is_the_one_monitoring_yml_creates(self):
        group = _template("monitoring.yml")["Resources"]["FisLogGroup"]
        assert group["Type"] == "AWS::Logs::LogGroup"
        assert group["Properties"]["LogGroupName"] == {"!Sub": context.FIS_LOG_GROUP + "${Env}"}
        assert group["Properties"]["RetentionInDays"] == 30

    def test_the_experiment_role_can_set_up_log_delivery_as_cloudwatch_logs_documents(self):
        role = _template("ngrh.yaml")["Resources"]["TestExperimentRole"]
        statements = {s["Sid"]: s for p in role["Properties"]["Policies"] for s in p["PolicyDocument"]["Statement"]}
        create = statements["FisLogDeliveryCreate"]
        assert (create["Effect"], create["Action"], create["Resource"]) == ("Allow", "logs:CreateLogDelivery", "*")
        groups = statements["FisLogDeliveryGroups"]
        assert groups["Effect"] == "Allow"
        assert sorted(groups["Action"]) == ["logs:DescribeLogGroups", "logs:DescribeResourcePolicies", "logs:PutResourcePolicy"]
        assert groups["Resource"] == {"!Sub": "arn:${AWS::Partition}:logs:*:${AWS::AccountId}:log-group:*"}

    def test_every_make_target_of_the_tool_is_in_the_readme(self):
        readme = (ROOT / "README.md").read_text()
        makefile = (DEPLOYMENT / "Makefile").read_text()
        targets = sorted(re.findall(r"^(ngrh-test[a-z-]*):", makefile, flags=re.M))
        assert targets == ["ngrh-test", "ngrh-test-preflight", "ngrh-test-report", "ngrh-test-stop", "ngrh-tests"]
        for target in targets:
            assert re.search(rf"^\| `make {re.escape(target)}[ `]", readme, flags=re.M), f"the README table does not describe make {target}"

    def test_the_reports_directory_is_the_one_git_ignores(self):
        from ngrh_testing import report
        assert Path(report.REPORT_DIR) == DEPLOYMENT / "ngrh-test-reports"
        assert "deployment/ngrh-test-reports/" in (ROOT / ".gitignore").read_text().splitlines()
