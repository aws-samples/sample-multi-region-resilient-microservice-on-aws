"""The NGRH model's journeys and policies: ngrh.yaml, the gamma script and the README say the same thing.

Two defects led to this file.

1. The cart service was linked to Manage Cart and Checkout & Place Order only. The ui asks the cart service for the cart on
   every page it renders (BaseController.populateCommon -> populateCart -> cartsService.getCart), so Browse Catalog and
   View Orders depend on it too: in test2 a stalled carts task failed the home page about half the time and catalog pages
   75 to 90 percent of the time. The model said those journeys did not use cart, so an assessment judged cart against
   two journeys instead of four. It is in all four now.

2. The README's tier text had drifted from ngrh.yaml since the model was added: it said every service was Tier-1 and that
   Tier-2 had a 15-minute RTO and applied to journeys, while the template gives Tier-2 (RTO 10 minutes) to catalog,
   checkout and assets and attaches each policy to a service.

What this file pins: which journeys each service is in and which policy it has (template, gamma script and README
agree), each policy's RTO and RPO (template, gamma script and README agree), cart and ui in every journey, and the code
fact behind the cart decision: every page controller of the ui calls populateCommon, which asks the cart service.
"""
import re
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from test_ngrh_tag_discovery import _load_template  # noqa: E402

REPO = TESTS.parent
DEPLOYMENT = REPO / "deployment"
UI_WEB = REPO / "source" / "ui" / "src" / "main" / "java" / "com" / "amazon" / "sample" / "ui" / "web"
README = (REPO / "README.md").read_text()
TEMPLATE_TEXT = (DEPLOYMENT / "ngrh.yaml").read_text()
GAMMA = (DEPLOYMENT / "ngrh-gamma-create.sh").read_text()
RESOURCES = _load_template(DEPLOYMENT / "ngrh.yaml")["Resources"]


# ---------------------------------------------------------------------------
# What the template says
# ---------------------------------------------------------------------------

def _display(journey_name):
    """BrowseCatalog -> Browse Catalog, CheckoutAndPlaceOrder -> Checkout & Place Order.

    Splits at every lower-to-upper boundary and drops nothing: a stray capital (ViewOrdersX) must not read as View Orders."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", journey_name).replace(" And ", " & ")


JOURNEYS = {lid: _display(r["Properties"]["Name"]) for lid, r in RESOURCES.items()
            if r["Type"] == "AWS::ResilienceHubV2::UserJourney"}
POLICY_TIER = {"TierOnePolicy": "Tier-1", "TierTwoPolicy": "Tier-2"}
POLICIES = {POLICY_TIER[lid]: r["Properties"] for lid, r in RESOURCES.items()
            if r["Type"] == "AWS::ResilienceHubV2::Policy"}


def _services():
    """service name -> (set of journey display names, tier label), from the template."""
    found = {}
    for r in RESOURCES.values():
        if r["Type"] != "AWS::ResilienceHubV2::Service":
            continue
        props = r["Properties"]
        name = props["Name"].replace("${Env}", "")
        (system,) = props["AssociatedSystems"]
        journeys = {JOURNEYS[ref.split(".")[0]] for ref in system["UserJourneyIds"]}
        found[name] = (journeys, POLICY_TIER[props["PolicyArn"].split(".")[0]])
    return found


SERVICES = _services()
ALL_JOURNEYS = set(JOURNEYS.values())


def test_the_template_has_four_journeys_two_policies_and_six_services():
    assert ALL_JOURNEYS == {"Browse Catalog", "Manage Cart", "Checkout & Place Order", "View Orders"}
    assert sorted(POLICIES) == ["Tier-1", "Tier-2"]
    assert sorted(SERVICES) == ["assets", "cart", "catalog", "checkout", "orders", "ui"]


def test_each_service_is_linked_to_a_journey_once():
    for r in RESOURCES.values():
        if r["Type"] == "AWS::ResilienceHubV2::Service":
            (system,) = r["Properties"]["AssociatedSystems"]
            ids = system["UserJourneyIds"]
            assert len(ids) == len(set(ids)), f"{r['Properties']['Name']}: a journey is listed twice"


# ---------------------------------------------------------------------------
# cart and ui are in every journey
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("service", ["ui", "cart"])
def test_the_service_every_page_depends_on_is_in_every_journey(service):
    journeys, _ = SERVICES[service]
    assert journeys == ALL_JOURNEYS, f"{service} is missing from {sorted(ALL_JOURNEYS - journeys)}"


# The ui's page controllers and the journey each one serves. A controller not listed here is a page nobody has placed in
# a journey: add it here, and check the cart (and every other service it asks) is linked to that journey.
PAGE_CONTROLLERS = {
    "HomeController": "Browse Catalog",
    "CatalogController": "Browse Catalog",
    "CartController": "Manage Cart",
    "CheckoutController": "Checkout & Place Order",
    "OrderController": "View Orders",
}


class TestTheUiAsksTheCartServiceOnEveryPage:
    def test_populate_common_asks_the_cart_service(self):
        base = (UI_WEB / "BaseController.java").read_text()
        common = re.search(r"void populateCommon\([^)]*\)\s*\{(.*?)\n    \}", base, re.S).group(1)
        cart = re.search(r"void populateCart\([^)]*\)\s*\{(.*?)\n    \}", base, re.S).group(1)
        assert "populateCart(" in common
        assert "cartsService.getCart(" in cart

    def test_every_page_controller_is_placed_in_a_journey(self):
        pages = sorted(p.stem for p in UI_WEB.glob("*Controller.java") if "extends BaseController" in p.read_text())
        assert pages == sorted(PAGE_CONTROLLERS)

    @pytest.mark.parametrize("controller,journey", sorted(PAGE_CONTROLLERS.items()))
    def test_each_page_controller_fills_in_the_cart_and_its_journey_lists_cart(self, controller, journey):
        assert journey in ALL_JOURNEYS
        assert "populateCommon(" in (UI_WEB / f"{controller}.java").read_text()
        assert journey in SERVICES["cart"][0]


# ---------------------------------------------------------------------------
# The README tables
# ---------------------------------------------------------------------------

def _section(start, end):
    return README[README.index(start):README.index(end)]


JOURNEY_SECTION = _section("### User journeys → services", "### Resiliency policies (tiers)")
POLICY_SECTION = _section("### Resiliency policies (tiers)", "### Which policy each service has")
SERVICE_SECTION = _section("### Which policy each service has", "### Region scope")


def _rows(text):
    """The cells of every table row in `text` (not the header rule), stripped of markdown emphasis and code ticks."""
    rows = []
    for line in text.splitlines():
        if line.startswith("|") and not re.match(r"^\|[\s\-|]+\|$", line):
            rows.append([cell.strip().strip("*").strip().replace("`", "") for cell in line.strip().strip("|").split("|")])
    return rows[1:]  # the header row


def _names(cell):
    return {name.strip() for name in cell.split(",")}


class TestReadmeJourneyTable:
    def test_it_lists_the_services_each_journey_has_in_the_template(self):
        expected = {journey: {name for name, (journeys, _) in SERVICES.items() if journey in journeys}
                    for journey in ALL_JOURNEYS}
        actual = {row[0]: _names(row[1]) for row in _rows(JOURNEY_SECTION)}
        assert actual == expected

    def test_it_has_one_row_per_journey(self):
        assert sorted(row[0] for row in _rows(JOURNEY_SECTION)) == sorted(ALL_JOURNEYS)

    def test_a_note_says_cart_is_in_every_journey_and_why(self):
        note = next(line for line in JOURNEY_SECTION.splitlines() if line.startswith("* `cart`"))
        assert "every journey" in note
        assert "BaseController.populateCommon" in note
        assert "populateCommon" in (UI_WEB / "BaseController.java").read_text()


class TestReadmePolicyTable:
    def test_it_has_the_rto_rpo_and_services_of_each_policy_in_the_template(self):
        expected = {}
        for tier, props in POLICIES.items():
            region = props["MultiRegion"]
            services = {name for name, (_, service_tier) in SERVICES.items() if service_tier == tier}
            expected[tier] = (region["RtoInMinutes"], region["RpoInMinutes"], services)
        actual = {}
        for row in _rows(POLICY_SECTION):
            tier = re.match(r"Tier-\d", row[0]).group(0)
            actual[tier] = (int(re.match(r"\d+", row[1]).group(0)), int(re.match(r"\d+", row[2]).group(0)), _names(row[3]))
        assert actual == expected

    def test_it_no_longer_attaches_policies_to_journeys(self):
        header = next(line for line in POLICY_SECTION.splitlines() if line.startswith("| Policy"))
        assert "Applied to services" in header
        assert "journeys" not in header

    def test_the_text_no_longer_claims_what_the_template_does_not_do(self):
        stale = ["strictest tier of any journey", "All six services resolve to Tier-1", "15 min", "revenue funnel",
                 "Tier-2 = important"]
        assert [text for text in stale if text in POLICY_SECTION + SERVICE_SECTION] == []

    def test_the_template_comment_no_longer_says_the_policy_follows_the_journeys(self):
        assert "PolicyArn = strictest tier" not in TEMPLATE_TEXT
        assert "PolicyArn is set per service, not" in TEMPLATE_TEXT


class TestReadmeServiceTable:
    def test_it_lists_each_services_journeys_and_policy_in_the_template(self):
        actual = {}
        for row in _rows(SERVICE_SECTION):
            journeys = ALL_JOURNEYS if row[1] == "all four" else _names(row[1])
            actual[row[0]] = (journeys, row[2])
        assert actual == SERVICES


# ---------------------------------------------------------------------------
# The gamma script builds the same model through the API
# ---------------------------------------------------------------------------

GAMMA_JOURNEY_VARS = dict(re.findall(r'^(\w+_ID)=\$\(ensure_journey "(\w+)"', GAMMA, flags=re.M))


class TestGammaScript:
    def test_it_creates_the_four_journeys_of_the_template(self):
        assert {_display(name) for name in GAMMA_JOURNEY_VARS.values()} == ALL_JOURNEYS

    def test_it_links_each_service_to_the_journeys_and_policy_of_the_template(self):
        found = {}
        for name, tier, ids in re.findall(r'ensure_service "(\w+)\$\{ENV\}" "\$TIER(\d)_ARN" "([^"]+)"', GAMMA):
            journeys = {_display(GAMMA_JOURNEY_VARS[var]) for var in re.findall(r"\$(\w+_ID)", ids)}
            found[name] = (journeys, f"Tier-{tier}")
        assert found == SERVICES

    def test_it_creates_the_policies_of_the_template(self):
        found = {}
        for tier, slo, rto, rpo in re.findall(r'ensure_policy "ngrh-tier(\d)\$\{ENV\}" \\\n\s+"[^"]*" ([\d.]+) (\d+) (\d+)\)', GAMMA):
            found[f"Tier-{tier}"] = (float(slo), int(rto), int(rpo))
        expected = {tier: (float(props["AvailabilitySlo"]["Target"]), props["MultiRegion"]["RtoInMinutes"],
                           props["MultiRegion"]["RpoInMinutes"]) for tier, props in POLICIES.items()}
        assert found == expected
