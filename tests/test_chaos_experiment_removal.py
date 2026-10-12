"""The hand-built FIS experiment is gone from the default deploy (plan step 13); its destroy path stays.

`make chaos-engineering` used to deploy a plain FIS experiment template (stack `chaos${ENV}`, from
deployment/chaos/multi-region-scenario.yaml) in both Regions. The NGRH tests inject the same faults now and report what
happened, so the target and the template are removed. `destroy-chaos-engineering` stays: a deployment made before the
removal still has the stacks, and `destroy-infra` (so `destroy-all` and the e2e teardown) keeps calling it.

What this file pins:

* nothing in the deploy creates the experiment again (no target, no template, no FIS experiment template anywhere);
* the destroy path is still wired in;
* the destroy target works against a deployment that has no chaos stacks (every deployment from now on), still fails
  when a stack that exists cannot be deleted, and makes only the four calls it is expected to make;
* the README no longer sends a reader to the old experiment, describes the tests the spec defines, and its cost tables
  still add up.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from test_makefile_dependencies import get_ancestors, parse_makefile_targets  # noqa: E402

REPO = TESTS.parent
DEPLOYMENT = REPO / "deployment"
MAKEFILE = DEPLOYMENT / "Makefile"
README = (REPO / "README.md").read_text()
TARGETS = parse_makefile_targets(MAKEFILE)


# ---------------------------------------------------------------------------
# The experiment is not deployed
# ---------------------------------------------------------------------------

class TestTheExperimentIsNotDeployed:
    def test_there_is_no_chaos_engineering_target(self):
        assert "chaos-engineering" not in TARGETS

    def test_deploy_all_reaches_no_chaos_target(self):
        assert sorted(t for t in get_ancestors(TARGETS, "deploy-all") if "chaos" in t) == []

    def test_no_recipe_deploys_a_template_from_the_chaos_directory(self):
        lines = [line.strip() for line in MAKEFILE.read_text().splitlines()
                 if "chaos/" in line or ("cloudformation deploy" in line and "chaos" in line)]
        assert lines == []

    def test_the_template_and_its_directory_are_gone(self):
        assert not (DEPLOYMENT / "chaos").exists()

    def test_no_template_declares_an_fis_experiment(self):
        # Faults come from the NGRH tests (Resilience Hub creates their FIS experiments); a template of the sample's own
        # would be the old experiment under another name.
        found = sorted(str(path.relative_to(REPO)) for path in DEPLOYMENT.rglob("*.y*ml")
                       if "AWS::FIS::" in path.read_text())
        assert found == []


# ---------------------------------------------------------------------------
# The destroy path stays
# ---------------------------------------------------------------------------

class TestTheDestroyPathStays:
    def test_destroy_infra_still_calls_it(self):
        assert "destroy-chaos-engineering" in TARGETS["destroy-infra"]

    def test_destroy_all_reaches_it(self):
        assert "destroy-chaos-engineering" in get_ancestors(TARGETS, "destroy-all")


STUB_AWS = r'''#!/usr/bin/env python3
"""A stub `aws` for `make destroy-chaos-engineering`: records every call and answers as CloudFormation does.

STUB_STATE says what the stack looks like to CloudFormation:
  missing        there is no stack (every deployment made after the experiment was removed)
  deletes        the stack exists and deleting it works
  delete-failed  the stack exists and its deletion ends DELETE_FAILED
  denied         the caller may not delete stacks (AccessDenied, exit 254)
STUB_FAIL_REGION limits delete-failed and denied to the calls made for that Region. With both Regions failing, a recipe line
that swallows its error would hide behind the other Region's line, which still fails the target.
DeleteStack succeeds even for a stack that does not exist (the e2e teardown's destroy-load-generator relies on it:
that stack is never deployed there). `wait stack-delete-complete` counts a missing stack as deleted (botocore's
StackDeleteComplete waiter accepts the ValidationError "does not exist" as success) and exits 255 on DELETE_FAILED.
DescribeStacks on a missing stack is a ValidationError, exit 254. Any other call exits 2, so a new call in the recipe
fails the test that did not expect it.
"""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
state = os.environ["STUB_STATE"]


def option(name):
    return args[args.index(name) + 1] if name in args else None


broken = os.environ.get("STUB_FAIL_REGION") in (None, option("--region"))

if args[:2] == ["sts", "get-caller-identity"]:
    print("123456789012\tarn:aws:iam::123456789012:user/stub\tAIDASTUB")
    sys.exit(0)
if args[:2] == ["cloudformation", "delete-stack"]:
    if state == "denied" and broken:
        sys.stderr.write("An error occurred (AccessDenied) when calling the DeleteStack operation: "
                         "User is not authorized to perform: cloudformation:DeleteStack\n")
        sys.exit(254)
    sys.exit(0)
if args[:3] == ["cloudformation", "wait", "stack-delete-complete"]:
    if state == "delete-failed" and broken:
        sys.stderr.write('Waiter StackDeleteComplete failed: Waiter encountered a terminal failure state: For expression '
                         '"Stacks[].StackStatus" we matched expected path: "DELETE_FAILED" at any position\n')
        sys.exit(255)
    sys.exit(0)
if args[:2] == ["cloudformation", "describe-stacks"] and state == "missing":
    sys.stderr.write("An error occurred (ValidationError) when calling the DescribeStacks operation: "
                     "Stack with id %s does not exist\n" % option("--stack-name"))
    sys.exit(254)
sys.stderr.write("stub aws: unexpected call %s\n" % " ".join(args))
sys.exit(2)
'''


def _destroy(tmp_path, state, *, fail_region=None, env_suffix="-t", primary="us-east-1", standby="us-west-2"):
    """Run `make destroy-chaos-engineering` with the stub first on PATH; returns (process, the cloudformation calls)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "aws"
    stub.write_text(STUB_AWS)
    stub.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", STUB_LOG=str(log), STUB_STATE=state)
    if fail_region:
        env["STUB_FAIL_REGION"] = fail_region
    done = subprocess.run(
        ["make", "-C", str(DEPLOYMENT), "destroy-chaos-engineering", f"ENV={env_suffix}",
         f"PRIMARY_REGION={primary}", f"STANDBY_REGION={standby}"],
        env=env, capture_output=True, text=True, timeout=120)
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return done, [call for call in calls if call[0] == "cloudformation"]


def _expected(suffix, primary, standby):
    stack = f"chaos{suffix}"
    return [
        ["cloudformation", "delete-stack", "--stack-name", stack, "--region", primary],
        ["cloudformation", "delete-stack", "--stack-name", stack, "--region", standby],
        ["cloudformation", "wait", "stack-delete-complete", "--stack-name", stack, "--region", primary],
        ["cloudformation", "wait", "stack-delete-complete", "--stack-name", stack, "--region", standby],
    ]


class TestDestroyChaosEngineering:
    @pytest.mark.parametrize("state", ["missing", "deletes"])
    def test_it_succeeds_whether_or_not_the_stacks_exist(self, tmp_path, state):
        done, _ = _destroy(tmp_path, state)
        assert done.returncode == 0, done.stderr

    @pytest.mark.parametrize("suffix,primary,standby", [
        ("-t", "us-east-1", "us-west-2"),
        ("-abc1234", "eu-west-1", "eu-central-1"),
        ("", "us-east-1", "us-west-2"),
    ])
    def test_it_deletes_and_waits_for_the_stack_of_the_env_in_both_regions(self, tmp_path, suffix, primary, standby):
        done, calls = _destroy(tmp_path, "missing", env_suffix=suffix, primary=primary, standby=standby)
        assert done.returncode == 0, done.stderr
        expected = _expected(suffix, primary, standby)
        assert sorted(calls) == sorted(expected)

    def test_each_region_waits_only_after_its_delete(self, tmp_path):
        _, calls = _destroy(tmp_path, "missing")
        for region in ("us-east-1", "us-west-2"):
            delete = calls.index(["cloudformation", "delete-stack", "--stack-name", "chaos-t", "--region", region])
            wait = calls.index(["cloudformation", "wait", "stack-delete-complete", "--stack-name", "chaos-t", "--region", region])
            assert delete < wait

    def test_it_asks_nothing_else_of_a_missing_stack(self, tmp_path):
        # A describe-stacks first would be an error against a missing stack (exit 254).
        _, calls = _destroy(tmp_path, "missing")
        assert {call[1] for call in calls} == {"delete-stack", "wait"}

    @pytest.mark.parametrize("fail_region", ["us-east-1", "us-west-2"])
    def test_a_stack_that_cannot_be_deleted_still_fails_the_target(self, tmp_path, fail_region):
        # Tolerating a missing stack must not swallow a real failure, in either Region.
        done, _ = _destroy(tmp_path, "delete-failed", fail_region=fail_region)
        assert done.returncode != 0
        assert "DELETE_FAILED" in done.stderr

    @pytest.mark.parametrize("fail_region", ["us-east-1", "us-west-2"])
    def test_a_refused_delete_fails_the_target_before_it_waits(self, tmp_path, fail_region):
        # Swallowing the error would send the waiter after a stack that is still there.
        done, calls = _destroy(tmp_path, "denied", fail_region=fail_region)
        assert done.returncode != 0
        assert "AccessDenied" in done.stderr
        assert [call for call in calls if call[1] == "wait"] == []


# ---------------------------------------------------------------------------
# The README
# ---------------------------------------------------------------------------

SECTION = README[README.index("## Injecting Chaos to simulate failures"):README.index("## Cleanup")]
COST = README[README.index("## Cost\n"):README.index("## Acknowledgement")]
SPEC = json.loads((DEPLOYMENT / "ngrh-tests.json").read_text())["tests"]


def _slug(heading):
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _dollars(text):
    return float(re.search(r"\$([\d,]+(?:\.\d+)?)", text).group(1).replace(",", ""))


def _rows(text):
    """The cells of every table row in `text`, stripped of markdown emphasis."""
    rows = []
    for line in text.splitlines():
        if line.startswith("|") and not re.match(r"^\|[\s\-|]+\|$", line):
            rows.append([cell.strip().strip("*").strip() for cell in line.strip().strip("|").split("|")])
    return rows


class TestReadmeSection:
    def test_nothing_sends_a_reader_to_the_old_experiment(self):
        stale = ["multi-region-scenario", "make chaos-engineering", "start-experiment", "list-experiment-templates",
                 "Cross-Region: Connectivity", "chaos experiment template is already deployed", "chaos experiments"]
        assert [text for text in stale if text in README] == []

    def test_it_describes_each_test_the_spec_defines(self):
        for test in SPEC:
            rows = [row for row in _rows(SECTION) if row[0] == f"`{test['name']}`"]
            assert len(rows) == 1, f"the table has no single row for {test['name']}"
            row = " ".join(rows[0])
            minutes = test["parameters"]["duration"][0]
            assert f"{minutes} minutes" in row, f"{test['name']}: the README does not say {minutes} minutes"
            expected = "Pass" if test["expected"] == "PASS" else "Fail"
            assert rows[0][2].startswith(expected + ":"), f"{test['name']}: the spec expects {test['expected']}"

    def test_a_duration_is_a_maximum_exactly_when_the_test_has_a_stop_condition(self):
        for test in SPEC:
            row = next(row for row in _rows(SECTION) if row[0] == f"`{test['name']}`")
            minutes = test["parameters"]["duration"][0]
            assert (f"up to {minutes} minutes" in row[1]) == bool(test["stopAlarms"]), (
                f"{test['name']}: stopAlarms is {test['stopAlarms']}, the README says {row[1]!r}")

    def test_the_sentence_about_stop_conditions_names_the_tests_that_have_none(self):
        sentence = next(s for s in re.split(r"(?<=\.)\s+", SECTION) if "no stop condition" in s)
        for test in SPEC:
            assert (f"`{test['name']}`" in sentence) == (not test["stopAlarms"]), (
                f"{test['name']}: stopAlarms is {test['stopAlarms']}, the sentence is {sentence!r}")

    def test_it_says_which_tests_stop_early(self):
        sentence = next(s for s in re.split(r"(?<=\.)\s+", SECTION) if "stop the fault early" in s)
        for test in SPEC:
            assert (f"`{test['name']}`" in sentence) == bool(test["stopAlarms"]), (
                f"{test['name']}: stopAlarms is {test['stopAlarms']}, the sentence is {sentence!r}")

    def test_it_tells_how_to_run_one(self):
        for command in ("make ngrh-tests", "make ngrh-test-preflight TEST=", "make ngrh-test TEST="):
            assert command in SECTION

    def test_every_in_page_link_has_a_heading(self):
        headings = {_slug(h) for h in re.findall(r"^#{1,6} (.+)$", README, flags=re.M)}
        links = re.findall(r"\]\(#([^)]+)\)", SECTION)
        assert links
        assert [link for link in links if link not in headings] == []

    def test_it_says_how_to_delete_the_stacks_of_an_older_deployment(self):
        assert "`make destroy-chaos-engineering`" in SECTION
        assert "`make destroy-all`" in SECTION


class TestReadmeCosts:
    @staticmethod
    def _table(after, until):
        return _rows(COST[COST.index(after):COST.index(until)])

    def test_the_per_region_rows_add_up_to_the_subtotal(self):
        rows = self._table("### Per-Region Costs", "### Shared / Global Costs")
        subtotal = next(row for row in rows if row[0] == "Per-Region subtotal")
        parts = [_dollars(row[1]) for row in rows if row[0] not in ("Service", "Per-Region subtotal")]
        assert round(sum(parts)) == round(_dollars(subtotal[1]))

    def test_the_shared_rows_add_up_to_the_subtotal(self):
        rows = self._table("### Shared / Global Costs", "### Total")
        subtotal = next(row for row in rows if row[0] == "Global subtotal")
        parts = [_dollars(row[1]) for row in rows if row[0] not in ("Service", "Global subtotal")]
        assert round(sum(parts)) == round(_dollars(subtotal[1]))

    def test_the_total_is_both_regions_plus_the_shared_costs(self):
        def amounts(after, until):
            return {row[0]: _dollars(row[1]) for row in self._table(after, until) if "$" in row[1]}

        per_region = amounts("### Per-Region Costs", "### Shared / Global Costs")
        shared = amounts("### Shared / Global Costs", "### Total")
        total = amounts("### Total", "### Resilience Hub and test costs")
        assert total["US East (N. Virginia)"] == total["US West (Oregon)"] == per_region["Per-Region subtotal"]
        assert total["Shared / global"] == shared["Global subtotal"]
        assert total["Total"] == 2 * per_region["Per-Region subtotal"] + shared["Global subtotal"]

    def test_the_summary_follows_the_total(self):
        total = _dollars(next(row[1] for row in _rows(COST) if row[0] == "Total"))
        summary = {row[0]: _dollars(row[1]) for row in _rows(COST.split("## Summary")[1].split("## Detailed Estimate")[0])
                   if len(row) == 2 and "$" in row[1]}
        assert summary["Monthly Cost"] == total
        assert summary["Total 12 Months Cost"] == round(12 * total, -2)

    def test_there_is_no_fis_row_for_the_old_experiment(self):
        assert [row for row in _rows(COST) if row[0] == "AWS FIS"] == []

    # The optional costs. FIS bills $0.10 per action-minute for each action that ran; the actions that ran in test2:
    ACTIONS_RAN = {"orders-broker-dependency": 1, "catalog-recovery": 1, "cart-isolation": 2}

    @pytest.mark.parametrize("test", SPEC, ids=lambda test: test["name"])
    def test_a_run_costs_its_actions_times_its_duration(self, test):
        rows = _rows(COST[COST.index("### Resilience Hub and test costs"):COST.index("### Cost-reduction levers")])
        row = next(row for row in rows if row[0] == f"`{test['name']}` run")
        minutes = int(test["parameters"]["duration"][0])
        assert _dollars(row[1]) == pytest.approx(0.10 * self.ACTIONS_RAN[test["name"]] * minutes)
        assert f"{minutes} minutes" in row[2]
        # A stop condition can end the fault early, so the figure is a ceiling exactly then.
        assert row[1].startswith("up to ") == bool(test["stopAlarms"]), f"{test['name']}: {row[1]!r}"
        assert (f"up to {minutes} minutes" in row[2]) == bool(test["stopAlarms"]), f"{test['name']}: {row[2]!r}"

    def test_the_service_costs_follow_the_number_of_services(self):
        services = len(re.findall(r"^\s+Type: AWS::ResilienceHubV2::Service\s*$", (DEPLOYMENT / "ngrh.yaml").read_text(), flags=re.M))
        assert services == 6
        rows = _rows(COST[COST.index("### Resilience Hub and test costs"):COST.index("### Cost-reduction levers")])
        by_item = {row[0]: row for row in rows}
        assert _dollars(by_item["Resilience Hub services"][1]) == 15 * services
        assert _dollars(by_item["Dependency discovery"][1]) == 10 * services
        for item in ("Resilience Hub services", "Dependency discovery"):
            assert f"{services} services" in by_item[item][2]
