"""The scenario the step 7 tests share: a fake deployment in which every alarm and resource the shipped
spec refers to exists, and the exact arguments the shipped spec should produce, written out independently
of the code under test."""

from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
DEPLOYMENT = TESTS.parent / "deployment"
sys.path.insert(0, str(DEPLOYMENT))

from ngrh_fake_aws import (  # noqa: E402
    ACCOUNT, BROKER_HOST, ENV, PLAN_ARN, PRIMARY, STANDBY, FakeAws, alarm_arn, catalog_endpoint, service_arn, template_arn,
)

from ngrh_testing import context, reconcile, spec  # noqa: E402

SPEC_FILE = DEPLOYMENT / "ngrh-tests.json"
TEMPLATE = "aws-dependency-validation:rtdep001"
NAME = "orders-broker-dependency"

SUCCESS = [f"journey-lcl-orders-{PRIMARY}{ENV}", f"journey-global-orders-{PRIMARY}{ENV}"]
# Only alarms tagged for orders or shared can be sources of an orders test: hop-checkout-errors is tagged checkout,
# so it is evidence (one of the ten HOPS) and not a source.
OBSERVABILITY = [f"hop-orders-slow-{PRIMARY}{ENV}", f"orders-created-zero-{PRIMARY}{ENV}"]
HOPS = [f"hop-{s}-{k}-{PRIMARY}{ENV}" for s in ("ui", "catalog", "carts", "checkout", "orders") for k in ("errors", "slow")]
STOP = f"region-degraded-{PRIMARY}{ENV}"
PEER_DEGRADED = f"region-degraded-{STANDBY}{ENV}"
COMPOSITES = [(STOP, PRIMARY), (PEER_DEGRADED, STANDBY)]

# What the shipped spec should ask Resilience Hub for.
DESIRED = {
    "service_arn": service_arn("orders"),
    "test_template_arn": template_arn(TEMPLATE),
    "parameters": {"region": [PRIMARY], "dependencies": [BROKER_HOST], "duration": ["15"]},
    "role_name": f"ngrh-test-experiment{ENV}",
    "logging_configuration": {"cloudWatchLogGroupArn": f"arn:aws:logs:{PRIMARY}:{ACCOUNT}:log-group:/aws/fis/ngrh-tests{ENV}"},
    "stop_conditions": [{"source": "aws:cloudwatch:alarm", "value": alarm_arn(STOP)}],
}
DESIRED_SOURCES = sorted(
    [("SUCCESS_CRITERIA", alarm_arn(n)) for n in SUCCESS] + [("OBSERVABILITY", alarm_arn(n)) for n in OBSERVABILITY]
)


# The recovery test: the catalog database in the primary Region is blocked, and the plan has to move the traffic.
RECOVERY = "catalog-recovery"
RECOVERY_TEMPLATE = "aws-multi-region-recovery:rtmr002"
JOURNEYS = ("home", "cart", "catalog", "orders")
RECOVERY_SUCCESS = [f"journey-global-{j}-{PRIMARY}{ENV}" for j in JOURNEYS]
RECOVERY_OBSERVABILITY = [f"hop-catalog-slow-{PRIMARY}{ENV}"]
LCL = [f"journey-lcl-{j}-{PRIMARY}{ENV}" for j in JOURNEYS]          # the primary's own view of each journey
RMT = [f"journey-rmt-{j}-{STANDBY}{ENV}" for j in JOURNEYS]          # the standby's view of the primary's journeys
RECOVERY_DESIRED = {
    "service_arn": service_arn("catalog"),
    "test_template_arn": template_arn(RECOVERY_TEMPLATE),
    "parameters": {"impairedRegion": [PRIMARY], "recoveryRegion": [STANDBY], "regionSwitchPlan": [PLAN_ARN],
                   "dependencies": [catalog_endpoint(PRIMARY, ENV), catalog_endpoint(PRIMARY, ENV, reader=True)], "duration": ["20"]},
    "role_name": f"ngrh-test-experiment{ENV}",
    "logging_configuration": {"cloudWatchLogGroupArn": f"arn:aws:logs:{PRIMARY}:{ACCOUNT}:log-group:/aws/fis/ngrh-tests{ENV}"},
}
RECOVERY_SOURCES = sorted(
    [("SUCCESS_CRITERIA", alarm_arn(n)) for n in RECOVERY_SUCCESS] + [("OBSERVABILITY", alarm_arn(n, PRIMARY)) for n in RECOVERY_OBSERVABILITY]
)


def deployed_fake() -> FakeAws:
    """Every alarm, template, role and stack the shipped spec refers to, nothing reconciled yet."""
    fake = FakeAws()
    for name in SUCCESS + OBSERVABILITY + HOPS + RECOVERY_SUCCESS + LCL:
        fake.add_alarm(name)
    for name in RMT:
        fake.add_alarm(name, STANDBY)
    for name, region in COMPOSITES:
        fake.add_alarm(name, region, kind="CompositeAlarm")
    fake.add_ecs_service("orders")
    fake.add_ecs_service("catalog")
    return fake


def environment(fake: FakeAws) -> context.Environment:
    return context.load_environment(fake, PRIMARY, STANDBY, ENV)


def spec_tests():
    """The orders test, which most of the tool's tests are about. The shipped spec has more; ``all_spec_tests`` is every one."""
    return spec.load(str(SPEC_FILE)).select(NAME)


def all_spec_tests():
    return spec.load(str(SPEC_FILE)).select("all")


def recovery_tests():
    return spec.load(str(SPEC_FILE)).select(RECOVERY)


def recovery_fake() -> FakeAws:
    """A deployment ready for the recovery test: both Regions run all six services (the scale-up lands in the standby),
    the plan has its triggers, and nothing was reconciled yet."""
    fake = deployed_fake()
    for service in ("ui", "catalog", "cart", "checkout", "orders", "assets"):
        for region in (PRIMARY, STANDBY):
            fake.add_ecs_service(service, region)
    return fake


def reconciled_fake() -> FakeAws:
    """A deployment whose test already exists and matches the spec, ready for a run."""
    fake = deployed_fake()
    reconcile.reconcile(fake, environment(fake), spec_tests())
    fake.calls.clear()
    return fake


def fully_reconciled_fake() -> FakeAws:
    """A recovery-ready deployment in which every test of the shipped spec exists and matches it."""
    fake = recovery_fake()
    reconcile.reconcile(fake, environment(fake), all_spec_tests())
    fake.calls.clear()
    return fake
