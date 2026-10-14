"""The DynamoDB alarms in monitoring.yml see the errors the carts table returns.

On 2026-10-12 the ``cart-isolation`` test paused the carts table's replication. The table answered carts with HTTP 500
for four minutes (carts logged ``InternalServerErrorException``; CloudWatch counted 66, 88, 69, 12 and 3 system errors in
the minutes 11:47 to 11:51), all four journeys of us-east-1 failed, and ``dynamodb-errors-us-east-1`` never left ``OK``.
Both alarms under it named the table in a ``TableName`` dimension, and CloudWatch has no such series:

* ``SystemErrors`` exists only per table and operation (dimensions ``TableName`` and ``Operation``; for the carts table
  ``GetItem`` and ``Query`` had errors that day);
* ``UserErrors`` exists once per account and Region, with no dimension at all.

An alarm on a series that does not exist receives no data, and with ``TreatMissingData: notBreaching`` that is ``OK``
for ever. These tests pin the shape that works:

* ``dynamodb-system-errors`` is a metric math alarm, ``SUM(METRICS())`` over ``SystemErrors`` with the dimensions
  ``TableName`` and ``Operation`` for each operation the carts service issues (replayed with ``get-metric-data`` on the
  11:40 to 12:00 window of 2026-10-12 it gives exactly those five numbers);
* ``dynamodb-user-errors`` has no dimension;
* the operations in the alarm are the operations the carts source calls, read from the Java files, so a new call that the
  alarm does not cover fails here before it can fail silently in production;
* the table name is the one the table's stack creates;
* what the alarms did before stays: threshold 0 over one 60-second period, missing data not breaching, the ``cart`` tag,
  and the composite ``dynamodb-errors`` over the two.

Run with:  pytest tests/test_dynamodb_alarms.py -v
"""

import re
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
DEPLOYMENT = REPO / "deployment"
CART_SOURCE = REPO / "source" / "cart" / "src" / "main" / "java"

# The data-plane operations DynamoDB reports in the Operation dimension.
KNOWN_OPERATIONS = {
    "GetItem", "PutItem", "UpdateItem", "DeleteItem", "Query", "Scan",
    "BatchGetItem", "BatchWriteItem", "TransactGetItems", "TransactWriteItems",
}

# What each DynamoDBMapper (AWS SDK v1) call can send. ``save`` uses UpdateItem by default and PutItem for other save
# behaviors; the batch calls send BatchGetItem and BatchWriteItem; ``count`` is a Query with a count-only select.
MAPPER_OPERATIONS = {
    "load": {"GetItem"},
    "query": {"Query"},
    "queryPage": {"Query"},
    "count": {"Query", "Scan"},
    "save": {"UpdateItem", "PutItem"},
    "delete": {"DeleteItem"},
    "batchDelete": {"BatchWriteItem"},
    "batchSave": {"BatchWriteItem"},
    "batchWrite": {"BatchWriteItem"},
    "batchLoad": {"BatchGetItem"},
    "scan": {"Scan"},
    "scanPage": {"Scan"},
    "parallelScan": {"Scan"},
    "transactionWrite": {"TransactWriteItems"},
    "transactionLoad": {"TransactGetItems"},
}
# Calls that build a request or manage the table: they are not data-plane operations and report no Operation.
MAPPER_IGNORED = {"generateCreateTableRequest", "generateDeleteTableRequest", "getTableModel", "marshallIntoObject"}
CLIENT_IGNORED = {"describeTable", "createTable", "deleteTable", "updateTable", "listTables", "waitForTableCreation"}
CLIENT_OPERATIONS = {
    "getItem": "GetItem", "putItem": "PutItem", "updateItem": "UpdateItem", "deleteItem": "DeleteItem",
    "query": "Query", "scan": "Scan", "batchGetItem": "BatchGetItem", "batchWriteItem": "BatchWriteItem",
    "transactGetItems": "TransactGetItems", "transactWriteItems": "TransactWriteItems",
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
    # Drive the SafeLoader subclass directly rather than passing it to yaml.load (see tests/test_yaml_loading.py).
    loader = _CfnLoader(path.read_text())
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


RESOURCES = _load_template(DEPLOYMENT / "monitoring.yml")["Resources"]
USER = RESOURCES["DynamoDBUserErrorsAlarm"]["Properties"]
SYSTEM = RESOURCES["DynamoDBSystemErrorsAlarm"]["Properties"]
COMPOSITE = RESOURCES["DynamoDBComposite"]["Properties"]


def _queries():
    return SYSTEM["Metrics"]


def _metric_queries():
    return [q for q in _queries() if "MetricStat" in q]


def _dimensions(query):
    return {d["Name"]: d["Value"] for d in query["MetricStat"]["Metric"]["Dimensions"]}


def _alarm_operations():
    return {_dimensions(q)["Operation"] for q in _metric_queries()}


def _operations_the_carts_source_calls():
    """The operations the carts Java code can send, and the calls this test does not know how to classify."""
    operations, unknown = set(), []
    for java in sorted(CART_SOURCE.rglob("*.java")):
        text = re.sub(r"//[^\n]*|/\*.*?\*/", "", java.read_text(), flags=re.S)
        for receiver, call in re.findall(r"\b(mapper|dynamoDB)\s*(?:\.|::)\s*(\w+)", text):
            if receiver == "mapper":
                if call in MAPPER_IGNORED:
                    continue
                if call in MAPPER_OPERATIONS:
                    operations |= MAPPER_OPERATIONS[call]
                else:
                    unknown.append(f"{java.name}: mapper.{call}")
            else:
                if call in CLIENT_IGNORED:
                    continue
                if call in CLIENT_OPERATIONS:
                    operations.add(CLIENT_OPERATIONS[call])
                else:
                    unknown.append(f"{java.name}: dynamoDB.{call}")
    return operations, unknown


# --- the system errors alarm -------------------------------------------------------------------------------------------

class TestSystemErrorsAlarm:
    def test_it_is_a_metric_math_alarm_with_no_top_level_metric(self):
        # CloudFormation rejects an alarm that has both Metrics and any of these.
        for key in ("MetricName", "Namespace", "Dimensions", "Statistic", "ExtendedStatistic", "Period", "Unit"):
            assert key not in SYSTEM, f"an alarm with Metrics must not also set {key}"

    def test_one_expression_sums_the_metrics_and_only_it_returns_data(self):
        expressions = [q for q in _queries() if "Expression" in q]
        assert len(expressions) == 1
        assert expressions[0]["Expression"] == "SUM(METRICS())"
        assert expressions[0]["ReturnData"] is True
        assert all(q["ReturnData"] is False for q in _metric_queries())

    def test_query_ids_are_unique_and_valid(self):
        ids = [q["Id"] for q in _queries()]
        assert len(ids) == len(set(ids))
        for query_id in ids:
            assert re.fullmatch(r"[a-z][A-Za-z0-9_]*", query_id), query_id

    def test_every_metric_is_the_per_operation_system_errors_series(self):
        # SystemErrors exists with TableName + Operation and with nothing else (list-metrics, test2, 2026-10-12): the
        # dimension set must be exactly that, or the alarm receives no data.
        assert _metric_queries(), "no metrics"
        for query in _metric_queries():
            metric = query["MetricStat"]["Metric"]
            assert metric["Namespace"] == "AWS/DynamoDB", query["Id"]
            assert metric["MetricName"] == "SystemErrors", query["Id"]
            assert set(_dimensions(query)) == {"TableName", "Operation"}, query["Id"]
            assert len(metric["Dimensions"]) == 2, query["Id"]
            assert query["MetricStat"]["Period"] == 60, query["Id"]
            assert query["MetricStat"]["Stat"] == "Sum", query["Id"]

    def test_every_metric_names_the_carts_table(self):
        for query in _metric_queries():
            assert _dimensions(query)["TableName"] == {"!Sub": "cartsTable${Env}"}, query["Id"]

    def test_the_table_name_is_the_one_its_stack_creates(self):
        # global-table.yml names the table '<NamingPrefix>Table<Env>' and make passes NamingPrefix=carts to it.
        table = _load_template(DEPLOYMENT / "database" / "dynamodb" / "global-table.yml")["Resources"]["DDBGlobalTable"]
        assert table["Properties"]["TableName"] == {"!Sub": "${NamingPrefix}Table${Env}"}
        makefile = (DEPLOYMENT / "Makefile").read_text()
        stack = re.search(r"--stack-name carts-db-stack\$\{ENV\}.*?NamingPrefix=(\w+)", makefile, flags=re.S)
        assert stack, "make deploys the carts table's stack with a NamingPrefix"
        assert _dimensions(_metric_queries()[0])["TableName"] == {"!Sub": stack.group(1) + "Table${Env}"}

    def test_operations_are_real_dynamodb_operations_without_duplicates(self):
        operations = [_dimensions(q)["Operation"] for q in _metric_queries()]
        assert len(operations) == len(set(operations))
        assert set(operations) <= KNOWN_OPERATIONS, sorted(set(operations) - KNOWN_OPERATIONS)

    def test_it_covers_every_operation_the_carts_source_calls(self):
        calls, unknown = _operations_the_carts_source_calls()
        assert not unknown, (
            "the carts source makes DynamoDB calls this test cannot classify: add each to MAPPER_OPERATIONS or "
            f"CLIENT_OPERATIONS (or the ignored sets) in tests/test_dynamodb_alarms.py: {unknown}"
        )
        assert calls, "no DynamoDB call found in the carts source: has the code moved?"
        missing = calls - _alarm_operations()
        assert not missing, (
            "the carts source can send these operations but dynamodb-system-errors does not watch them, so an error "
            f"on them would go unseen: {sorted(missing)}"
        )

    def test_the_operations_seen_failing_on_2026_10_12_are_watched(self):
        # GetItem and Query returned the errors that day; a cart that cannot be read is what failed the journeys.
        assert {"GetItem", "Query"} <= _alarm_operations()

    def test_it_alarms_on_any_error_in_one_minute_and_ignores_missing_data(self):
        assert SYSTEM["Threshold"] == 0
        assert SYSTEM["ComparisonOperator"] == "GreaterThanThreshold"
        assert SYSTEM["EvaluationPeriods"] == 1
        assert SYSTEM["TreatMissingData"] == "notBreaching"

    def test_name_and_tag_are_unchanged(self):
        assert SYSTEM["AlarmName"] == {"!Sub": "dynamodb-system-errors-${AWS::Region}${Env}"}
        assert SYSTEM["Tags"] == [{"Key": "service", "Value": "cart"}]


# --- the user errors alarm ---------------------------------------------------------------------------------------------

class TestUserErrorsAlarm:
    def test_it_has_no_dimension_because_the_series_has_none(self):
        # list-metrics for UserErrors returns one series with no dimensions (test2, 2026-10-12). A TableName dimension
        # selects no series.
        assert "Dimensions" not in USER
        assert "Metrics" not in USER

    def test_it_watches_the_user_errors_sum_per_minute(self):
        assert USER["Namespace"] == "AWS/DynamoDB"
        assert USER["MetricName"] == "UserErrors"
        assert USER["Statistic"] == "Sum"
        assert USER["Period"] == 60

    def test_it_alarms_on_any_error_in_one_minute_and_ignores_missing_data(self):
        assert USER["Threshold"] == 0
        assert USER["ComparisonOperator"] == "GreaterThanThreshold"
        assert USER["EvaluationPeriods"] == 1
        assert USER["TreatMissingData"] == "notBreaching"

    def test_name_and_tag_are_unchanged(self):
        assert USER["AlarmName"] == {"!Sub": "dynamodb-user-errors-${AWS::Region}${Env}"}
        assert USER["Tags"] == [{"Key": "service", "Value": "cart"}]

    def test_the_comment_says_why_it_is_account_wide(self):
        text = (DEPLOYMENT / "monitoring.yml").read_text()
        head = text.split("  DynamoDBUserErrorsAlarm:")[0].rsplit("\n\n", 1)[-1]
        assert "once per account and Region" in head
        assert "every table in the account" in head


# --- the composite -----------------------------------------------------------------------------------------------------

class TestComposite:
    def test_it_is_the_alarm_the_tests_and_the_report_name(self):
        assert COMPOSITE["AlarmName"] == {"!Sub": "dynamodb-errors-${AWS::Region}${Env}"}
        assert COMPOSITE["Tags"] == [{"Key": "service", "Value": "cart"}]

    def test_it_is_in_alarm_when_either_member_is(self):
        rule = COMPOSITE["AlarmRule"]["!Sub"]
        assert rule == "(ALARM(${DynamoDBUserErrorsAlarm}) OR ALARM(${DynamoDBSystemErrorsAlarm}))"

    def test_it_still_notifies_the_critical_topic(self):
        assert COMPOSITE["AlarmActions"] == [{"!Ref": "CriticalAlarmTopic"}]
        assert COMPOSITE["ActionsEnabled"] is True


# --- the source scan itself --------------------------------------------------------------------------------------------

class TestSourceScan:
    def test_it_finds_the_calls_the_carts_service_makes_today(self):
        calls, unknown = _operations_the_carts_source_calls()
        assert unknown == []
        # load, query, save, delete and batchDelete (DynamoDBCartService); the observed series of the table in test2 are
        # GetItem, Query, UpdateItem, DeleteItem and BatchWriteItem.
        assert {"GetItem", "Query", "UpdateItem", "DeleteItem", "BatchWriteItem"} <= calls

    def test_method_references_and_calls_are_both_seen(self):
        # DynamoDBCartService passes this.mapper::delete as a method reference.
        text = (CART_SOURCE / "com/amazon/sample/carts/services/DynamoDBCartService.java").read_text()
        assert "mapper::delete" in text
        assert "DeleteItem" in _operations_the_carts_source_calls()[0]

    @pytest.mark.parametrize("call, expected", [
        ("this.mapper.scan(X.class, e)", "Scan"),
        ("mapper.batchLoad(items)", "BatchGetItem"),
        ("dynamoDB.getItem(request)", "GetItem"),
    ])
    def test_a_new_call_is_classified_or_reported(self, tmp_path, monkeypatch, call, expected):
        source = tmp_path / "X.java"
        source.write_text(f"class X {{ void f() {{ {call}; }} }}")
        monkeypatch.setattr(sys.modules[__name__], "CART_SOURCE", tmp_path)
        operations, unknown = _operations_the_carts_source_calls()
        assert unknown == []
        assert operations == {expected}

    def test_an_unclassified_call_is_reported_not_ignored(self, tmp_path, monkeypatch):
        (tmp_path / "X.java").write_text("class X { void f() { mapper.somethingNew(a); dynamoDB.exportTableToPointInTime(b); } }")
        monkeypatch.setattr(sys.modules[__name__], "CART_SOURCE", tmp_path)
        operations, unknown = _operations_the_carts_source_calls()
        assert operations == set()
        assert unknown == ["X.java: mapper.somethingNew", "X.java: dynamoDB.exportTableToPointInTime"]

    def test_comments_are_not_calls(self, tmp_path, monkeypatch):
        (tmp_path / "X.java").write_text("class X { // mapper.scan(a)\n /* dynamoDB.putItem(b) */ void f() { mapper.load(c); } }")
        monkeypatch.setattr(sys.modules[__name__], "CART_SOURCE", tmp_path)
        assert _operations_the_carts_source_calls() == ({"GetItem"}, [])
