"""The NGRH findings ground truth agrees with itself and with the repository.

docs/ngrh-findings-ground-truth.md records what Resilience Hub (NGRH) reported for one deployment of this sample, so the
findings themselves cannot be checked against code. Everything around them can, and had drifted: the architecture bullets
still said "FARGATE_SPOT only" long after all six services ran on demand, the tier and policy text disagreed with the
template, and the old "a difference of two in the total is normal" had no measurement behind it.

What this file pins:

* the numbers add up: the total and severity counts in the metadata, the table per service, the headings and the finding
  blocks themselves agree, and every finding has all its fields;
* the theme table counts the finding titles the way it says (the patterns below are the specification), and each line of
  the improvement list quotes that table;
* the tiers, policies, journeys and assertion counts the document states are the template's;
* the architecture bullets are true of the templates (on-demand Fargate, two tasks, 200 percent pre-scaling, twelve
  canaries, the three tests, no chaos-engineering target);
* the note on cart finding 6 is true (the carts table is a strong-consistency global table), and the templates still leave
  off what the other findings say is missing, so the note is revisited if that changes;
* the file is public: no account number, ARN or id beyond the placeholders.
"""
import collections
import json
import re
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from test_ngrh_journeys import ALL_JOURNEYS, POLICIES, SERVICES, TEMPLATE_ASSERTIONS  # noqa: E402

REPO = TESTS.parent
DEPLOYMENT = REPO / "deployment"
DOC = (REPO / "docs" / "ngrh-findings-ground-truth.md").read_text()
ORDER = ["ui", "catalog", "cart", "checkout", "orders", "assets"]
SEVERITIES = ["HIGH", "MEDIUM", "LOW"]
CATEGORIES = {"MISCONFIGURATION_AND_BUGS", "EXCESSIVE_LOAD", "EXCESSIVE_LATENCY", "SINGLE_POINT_OF_FAILURE", "SHARED_FATE"}

# (key, label, pattern, count in the previous baseline of 2026-06-17). The first pattern that matches a title decides its theme.
THEMES = [
    ("fargate-spot", "ECS runs on Fargate Spot only", r"FARGATE_SPOT", 6),
    ("deletion-protection", "Deletion protection off (ALBs, Aurora, DSQL, DynamoDB table)", r"deletion protection", 6),
    ("keep-alive", "ALB client keep-alive left at the 3600 second default", r"keep-alive", 4),
    ("fail-open", "ALB target groups without fail-open", r"fail-open", 3),
    ("accelerated-recovery", "Route 53 accelerated recovery off", r"accelerated recovery", 1),
    ("grace-period", "ECS health check grace period of zero", r"grace period", 0),
    ("task-spread", "Two tasks per Region or two of three AZs used",
     r"2 of 3|only 2 AZs|only two tasks|limited to 2|span only|Insufficient ECS task count", 3),
    ("shared-alb", "One ALB shared by several services", r"shared ALB", 0),
    ("agent-sidecar", "CloudWatch agent sidecar marked essential", r"CloudWatch agent sidecar", 0),
    ("rto", "Failover time or step timeouts against the 10-minute RTO", r"RTO", 2),
    ("backup", "No AWS Backup plan or backup snapshots", r"backup", 4),
    ("redis", "ElastiCache Redis without replicas or failover", r"Redis", 1),
    ("rabbitmq", "RabbitMQ without high availability or cross-Region replication", r"RabbitMQ", 1),
    ("s3-versioning", "S3 buckets without versioning", r"versioning", 0),
    ("gray-az", "Gray AZ failures not handled", r"Gray AZ", 0),
]
OTHER_LABEL = "Other, one-off findings"
OTHER_BEFORE = 4
PREVIOUS_TOTAL = 35


def _theme(title):
    for key, _, pattern, _ in THEMES:
        if re.search(pattern, title, 0 if key == "rto" else re.IGNORECASE):
            return key
    return "other"


# ---------------------------------------------------------------------------
# Reading the document
# ---------------------------------------------------------------------------

def _metadata():
    block = DOC[DOC.index("## Metadata"):DOC.index("## Architecture Context")]
    rows = {}
    for line in block.splitlines():
        m = re.match(r"\| ([^|]+?) \| (.*) \|$", line)
        if m and m.group(1) != "Field":
            rows[m.group(1)] = m.group(2)
    return rows


def _table(start, stop):
    """The data rows of the first markdown table after `start` (before `stop`) as lists of cells."""
    block = DOC[DOC.index(start):DOC.index(stop)]
    rows = []
    for line in block.splitlines():
        if line.startswith("|") and not re.match(r"\|[-| ]+\|$", line):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows[1:]  # the header


def _findings():
    """(headings, findings): the (service, N) of each `## svc (N findings)` and every finding block in the document."""
    lines = DOC[DOC.index("\n## Findings\n"):].splitlines()
    headings, found, service, cur, field = [], [], None, None, None
    for line in lines:
        m = re.match(r"## (ui|catalog|cart|checkout|orders|assets) \((\d+) findings?\)$", line)
        if m:
            service = m.group(1)
            headings.append((service, int(m.group(2))))
            cur = field = None
            continue
        if line.startswith("## ") and service:
            break  # the first section after the services
        m = re.match(r"### (\d+)\. (.+)$", line)
        if m and service:
            cur = {"service": service, "number": int(m.group(1)), "title": m.group(2), "bullets": {}}
            found.append(cur)
            field = None
            continue
        if cur is None:
            continue
        m = re.match(r"- \*\*([A-Za-z ]+):\*\*\s*(.*)$", line)
        if m:
            field = m.group(1)
            cur[field] = m.group(2)
            cur["bullets"][field] = []
            continue
        m = re.match(r"  - (.+)$", line)
        if m and field:
            cur["bullets"][field].append(m.group(1))
    return headings, found


METADATA = _metadata()
HEADINGS, FINDINGS = _findings()
SEVERITY_TOTALS = collections.Counter(f["Severity"] for f in FINDINGS)


def _section(title):
    start = DOC.index(f"\n## {title}\n")
    following = DOC.find("\n## ", start + 4)
    return DOC[start:following if following != -1 else len(DOC)]


# ---------------------------------------------------------------------------
# The numbers add up
# ---------------------------------------------------------------------------

class TestTheFindingsAddUp:
    def test_the_services_come_in_the_fixed_order(self):
        assert [s for s, _ in HEADINGS] == ORDER

    @pytest.mark.parametrize("service", ORDER)
    def test_each_heading_gives_the_number_of_findings_under_it(self, service):
        (stated,) = [n for s, n in HEADINGS if s == service]
        under = [f for f in FINDINGS if f["service"] == service]
        assert stated == len(under)
        assert [f["number"] for f in under] == list(range(1, stated + 1))

    @pytest.mark.parametrize("finding", FINDINGS, ids=lambda f: f"{f['service']}-{f['number']}")
    def test_every_finding_has_all_its_fields(self, finding):
        assert finding["Severity"] in SEVERITIES
        assert finding["Category"] in CATEGORIES, finding["Category"]
        assert re.fullmatch(r"[A-Z_]+", finding["Policy Component"])
        assert len(finding["Description"]) > 60
        for field in ("Mitigations", "Observability", "Testing"):
            assert finding["bullets"].get(field), f"{field} has no bullet"

    def test_the_metadata_total_and_severities_are_the_findings(self):
        stated = re.fullmatch(r"(\d+) \(HIGH (\d+) · MEDIUM (\d+) · LOW (\d+)\)", METADATA["Total findings"])
        assert stated, METADATA["Total findings"]
        total, high, medium, low = map(int, stated.groups())
        assert total == len(FINDINGS) == sum(n for _, n in HEADINGS)
        assert (high, medium, low) == (SEVERITY_TOTALS["HIGH"], SEVERITY_TOTALS["MEDIUM"], SEVERITY_TOTALS["LOW"])

    def test_the_table_per_service_is_the_findings(self):
        rows = _table("## Findings at a glance", "The HIGH findings:")
        total_row = rows.pop()
        assert total_row[0] == "Total"
        assert [r[0] for r in rows] == ORDER
        for service, tier, journeys, count, high, medium, low in rows:
            mine = [f for f in FINDINGS if f["service"] == service]
            by = collections.Counter(f["Severity"] for f in mine)
            assert int(count) == len(mine), service
            assert (int(high), int(medium), int(low)) == (by["HIGH"], by["MEDIUM"], by["LOW"]), service
        assert [int(c) for c in total_row[3:]] == [len(FINDINGS), SEVERITY_TOTALS["HIGH"], SEVERITY_TOTALS["MEDIUM"],
                                                   SEVERITY_TOTALS["LOW"]]

    def test_the_list_of_high_findings_is_every_high_finding(self):
        listed = [(r[0], int(r[1]), r[2]) for r in _table("The HIGH findings:", "The findings fall into recurring themes")]
        assert listed == [(f["service"], f["number"], f["title"]) for f in FINDINGS if f["Severity"] == "HIGH"]


# ---------------------------------------------------------------------------
# The theme table and the improvement list
# ---------------------------------------------------------------------------

def _theme_rows():
    return {r[0]: r for r in _table("| Theme | 2026-10-13", "The one-off findings are:")}


class TestTheThemeTable:
    def test_every_theme_has_a_row_and_the_rows_add_up(self):
        rows = _theme_rows()
        assert list(rows) == [label for _, label, _, _ in THEMES] + [OTHER_LABEL]
        assert sum(int(r[1]) for r in rows.values()) == len(FINDINGS)
        assert sum(int(r[2]) for r in rows.values()) == PREVIOUS_TOTAL

    @pytest.mark.parametrize("key,label", [(k, l) for k, l, _, _ in THEMES] + [("other", OTHER_LABEL)])
    def test_a_row_counts_the_titles_that_match_its_pattern(self, key, label):
        row = _theme_rows()[label]
        mine = [f for f in FINDINGS if _theme(f["title"]) == key]
        assert int(row[1]) == len(mine)
        counted = collections.Counter(f["service"] for f in mine)
        assert row[3] == (", ".join(s if counted[s] == 1 else f"{s} ({counted[s]})" for s in ORDER if counted[s]) or "none")

    @pytest.mark.parametrize("key,label,before", [(k, l, b) for k, l, _, b in THEMES] + [("other", OTHER_LABEL, OTHER_BEFORE)])
    def test_the_previous_baseline_column_is_what_the_earlier_document_had(self, key, label, before):
        assert int(_theme_rows()[label][2]) == before

    def test_the_one_off_findings_are_listed_by_title(self):
        line = DOC[DOC.index("The one-off findings are:"):].split("\n\n")[0]
        for f in FINDINGS:
            if _theme(f["title"]) == "other":
                assert f'{f["service"]} {f["number"]} "{f["title"]}"' in line


class TestTheImprovementList:
    def _lines(self):
        found = {}
        for line in _section("Evaluation Criteria for Future Runs").splitlines():
            m = re.match(r"- \*\*(.+?)\.\*\* .*: up to (\d+) fewer findings?(?: \((\d+) of them HIGH\))?\.$", line)
            if m:
                found[m.group(1)] = (int(m.group(2)), int(m.group(3) or 0))
        return found

    def test_every_theme_that_can_be_fixed_has_a_line(self):
        labels = {label for key, label, _, _ in THEMES if key != "fargate-spot"}
        assert set(self._lines()) == labels

    @pytest.mark.parametrize("key,label", [(k, l) for k, l, _, _ in THEMES if k != "fargate-spot"])
    def test_a_line_quotes_the_count_and_the_high_findings_of_its_theme(self, key, label):
        mine = [f for f in FINDINGS if _theme(f["title"]) == key]
        assert self._lines()[label] == (len(mine), sum(1 for f in mine if f["Severity"] == "HIGH"))

    def test_the_regression_threshold_is_the_current_total(self):
        assert f"A total above {len(FINDINGS)} on an unchanged architecture" in _section("Evaluation Criteria for Future Runs")

    def test_a_service_without_a_high_finding_named_there_has_none(self):
        text = _section("Evaluation Criteria for Future Runs")
        (named,) = re.findall(r"a HIGH finding on a service that has none here \((\w+)\)", text)
        assert not [f for f in FINDINGS if f["service"] == named and f["Severity"] == "HIGH"]
        assert [s for s in ORDER if not [f for f in FINDINGS if f["service"] == s and f["Severity"] == "HIGH"]] == [named]


class TestWhatChanged:
    def test_the_previous_baseline_numbers_are_the_ones_in_the_theme_table(self):
        text = _section("What changed since the previous baseline")
        assert f"had {PREVIOUS_TOTAL} findings: HIGH 5 · MEDIUM 20 · LOW 10" in text
        assert f"the {THEMES[0][3]} Fargate Spot findings (four of them HIGH) are gone" in text
        assert int(_theme_rows()[THEMES[0][1]][1]) == 0

    def test_the_failover_order_it_describes_is_the_one_in_the_plan_now(self):
        plan = (DEPLOYMENT / "failover.yaml").read_text()
        deactivate = plan[plan.index("WorkflowTargetAction: deactivate"):plan.index("WorkflowTargetAction: activate")]
        blocks = re.findall(r"ExecutionBlockType: (ECSServiceScaling|Route53HealthCheck|AuroraGlobalDatabase)", deactivate)
        order = []
        for block in blocks:
            if block not in order:
                order.append(block)
        assert order == ["ECSServiceScaling", "Route53HealthCheck", "AuroraGlobalDatabase"]
        assert "scales up, moves DNS, then switches the catalog database over" in _section("What changed since the previous baseline")


# ---------------------------------------------------------------------------
# The metadata and architecture are the repository's
# ---------------------------------------------------------------------------

def _names(cell):
    return sorted(part.strip() for part in cell.split(","))


class TestMetadataIsTheTemplates:
    def test_the_services_are_the_six_of_the_template(self):
        stated = re.match(r"6 \((.*)\)$", METADATA["Services"]).group(1)
        assert stated.split(", ") == ORDER
        assert sorted(SERVICES) == sorted(ORDER)

    @pytest.mark.parametrize("row,tier", [("Tier1 services", "Tier-1"), ("Tier2 services", "Tier-2")])
    def test_each_tier_has_the_services_of_the_template(self, row, tier):
        assert _names(METADATA[row]) == sorted(name for name, (_, t) in SERVICES.items() if t == tier)

    def test_the_policies_are_the_templates(self):
        stated = {t: (int(rto), int(rpo)) for t, rto, rpo in re.findall(r"(Tier-\d): RTO (\d+) min, RPO (\d+)", METADATA["Policies"])}
        assert stated == {tier: (p["MultiRegion"]["RtoInMinutes"], p["MultiRegion"]["RpoInMinutes"]) for tier, p in POLICIES.items()}

    def test_the_journeys_are_the_templates_and_ui_and_cart_are_in_all_of_them(self):
        cell = METADATA["Journeys"]
        assert cell.startswith(f"{len(ALL_JOURNEYS)}: ")
        for journey in ALL_JOURNEYS:
            assert journey in cell
        assert "(ui and cart are in all four)" in cell
        assert SERVICES["ui"][0] == SERVICES["cart"][0] == ALL_JOURNEYS

    def test_the_table_per_service_has_the_tier_and_journey_count_of_the_template(self):
        for service, tier, journeys, *_ in _table("## Findings at a glance", "The HIGH findings:")[:-1]:
            assert f"Tier-{tier}" == SERVICES[service][1], service
            assert int(journeys) == len(SERVICES[service][0]), service

    def test_the_assertion_counts_are_the_templates_and_add_up(self):
        cell = METADATA["Assertions active"]
        m = re.match(r"(\d+): (\d+) written in `deployment/ngrh.yaml` \((.*)\) and (\d+) that Resilience Hub generated$", cell)
        assert m, cell
        total, written, per, generated = int(m.group(1)), int(m.group(2)), m.group(3), int(m.group(4))
        assert total == written + generated
        stated = {name: int(n) for name, n in re.findall(r"(\w+) (\d+)", per)}
        assert stated == {name: len(texts) for name, texts in TEMPLATE_ASSERTIONS.items()}
        assert written == sum(stated.values())


class TestArchitectureIsTheTemplates:
    ECS = (DEPLOYMENT / "ecs.yaml").read_text()
    FAILOVER = (DEPLOYMENT / "failover.yaml").read_text()
    ARCH = DOC[DOC.index("## Architecture Context"):DOC.index("## Findings at a glance")]

    def test_every_service_runs_on_demand_with_two_tasks(self):
        assert "FARGATE_SPOT" not in self.ECS
        assert self.ECS.count("CapacityProvider: FARGATE\n") == 6
        assert self.ECS.count("DesiredCount: 2\n") == 6
        assert "MinCapacity: 2\n" in self.ECS and "MaxCapacity: 10\n" in self.ECS
        assert "ECS Fargate on demand (`FARGATE`), two tasks per service in each Region (auto scaling 2 to 10)" in self.ARCH

    def test_the_plan_pre_scales_to_twice_the_daily_peak_with_fifteen_minutes(self):
        assert self.FAILOVER.count("TargetPercent: 200\n") == 6
        assert self.FAILOVER.count("CapacityMonitoringApproach: containerInsightsMaxInLast24Hours\n") == 6
        assert self.FAILOVER.count("TimeoutMinutes: 15\n") == 6
        assert "200% of containerInsightsMaxInLast24Hours before failover proceeds (15 minutes allowed)" in self.ARCH

    def test_the_arc_bullet_has_the_triggers_and_the_delay_of_the_plan(self):
        triggers = self.FAILOVER[self.FAILOVER.index("Triggers: !If"):]
        assert len(re.findall(r"^\s+(?:- )?- Action: deactivate$", triggers, flags=re.M)) == 8
        assert triggers.count("MinDelayMinutesBetweenExecutions: 60\n") == 8
        bullet = next(line for line in self.ARCH.splitlines() if line.startswith("- **ARC Region Switch**"))
        assert "eight alarm triggers" in bullet and "60 minutes" in bullet

    def test_there_are_twelve_canaries_four_pages_three_views(self):
        canaries = (DEPLOYMENT / "canaries.yaml").read_text()
        assert canaries.count("Type: 'AWS::Synthetics::Canary'") == 12
        names = set(re.findall(r"^\s+Name: !Sub ((?:lcl-rgnl|rmt-rgnl|global)-(?:home|cart|catalog|orders))\$\{Env\}$", canaries, flags=re.M))
        assert names == {f"{view}-{page}" for view in ("lcl-rgnl", "rmt-rgnl", "global") for page in ("home", "cart", "catalog", "orders")}
        assert "twelve CloudWatch Synthetics canaries per Region (four pages: home, cart, catalog and orders" in self.ARCH

    def test_the_three_tests_are_the_ones_in_the_spec_and_the_chaos_target_is_gone(self):
        spec = json.loads((DEPLOYMENT / "ngrh-tests.json").read_text())
        tests = spec["tests"] if isinstance(spec, dict) else spec
        names = sorted(t["name"] for t in tests)
        assert names == ["cart-isolation", "catalog-recovery", "orders-broker-dependency"]
        for name in names:
            assert f"`{name}`" in self.ARCH
        assert not re.search(r"^chaos-engineering:", (DEPLOYMENT / "Makefile").read_text(), flags=re.M)
        assert (REPO / "docs" / "ngrh-test-ground-truth.md").exists()
        assert "`docs/ngrh-test-ground-truth.md`" in self.ARCH

    def test_the_data_bullet_names_the_stores_the_templates_create(self):
        table = (DEPLOYMENT / "database" / "dynamodb" / "global-table.yml").read_text()
        assert "MultiRegionConsistency: STRONG" in table and "GlobalTableWitnesses" in table
        assert 'DeploymentMode: "SINGLE_INSTANCE"' in self.ECS
        assert "NumCacheClusters: 1\n" in self.ECS
        assert "multi-Region strong consistency and a witness Region (cart)" in self.ARCH


# ---------------------------------------------------------------------------
# The notes for evaluators
# ---------------------------------------------------------------------------

class TestNotesForEvaluators:
    NOTES = DOC[DOC.index("## Notes for evaluators"):DOC.index("## What changed since the previous baseline")]

    def test_the_cart_note_is_about_the_cart_finding_that_says_asynchronous(self):
        (finding,) = [f for f in FINDINGS if f["service"] == "cart" and f["number"] == 6]
        assert "async replication" in finding["title"]
        assert "cart finding 6" in self.NOTES

    def test_the_carts_table_is_a_strong_consistency_global_table(self):
        path = "deployment/database/dynamodb/global-table.yml"
        assert path in self.NOTES
        table = (REPO / path).read_text()
        assert "MultiRegionConsistency: STRONG" in table
        assert re.search(r"Region: !Ref WitnessRegion", table)

    def test_the_templates_leave_off_what_the_other_findings_say_is_missing(self):
        # If one of these is turned on, the finding behind it goes away and this note and the theme table need another look.
        files = [p for p in DEPLOYMENT.rglob("*") if p.suffix in (".yaml", ".yml")]
        text = "\n".join(p.read_text() for p in files)
        for needle in ("deletion_protection", "client_keep_alive", "load_balancing.fail_open", "HealthCheckGracePeriodSeconds",
                       "EnableAcceleratedRecovery", "AcceleratedRecovery"):
            assert needle not in text, f"{needle} is in a template now"
        for setting in re.findall(r"DeletionProtection(?:Enabled)?: *(\S+)", text):
            assert setting.lower() == "false", f"a template turns deletion protection on ({setting})"


# ---------------------------------------------------------------------------
# The file is public
# ---------------------------------------------------------------------------

class TestNothingPrivateIsInTheDocument:
    def test_the_only_account_number_is_the_placeholder(self):
        assert set(re.findall(r"(?<!\d)\d{12}(?!\d)", DOC)) <= {"123456789012"}

    def test_every_arn_uses_the_placeholder_account(self):
        for arn in re.findall(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:(\d*):", DOC):
            assert arn in ("", "123456789012"), arn

    def test_there_is_no_uuid(self):
        assert not re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", DOC)

    def test_no_email_address_or_local_path_is_named(self):
        assert not re.findall(r"@[a-z0-9-]+\.[a-z]{2,}", DOC, flags=re.IGNORECASE)
        assert not re.findall(r"/(?:local/)?home/[a-z0-9_-]+/", DOC)

    def test_the_placeholders_are_described(self):
        assert "123456789012" in DOC and "<health-check-id>" in DOC
        assert "the sandbox account number is written `123456789012`" in DOC
