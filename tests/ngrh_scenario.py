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
    ACCOUNT, BROKER_HOST, ENV, PRIMARY, STANDBY, FakeAws, alarm_arn, service_arn, template_arn,
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


def deployed_fake() -> FakeAws:
    """Every alarm, template, role and stack the shipped spec refers to, nothing reconciled yet."""
    fake = FakeAws()
    for name in SUCCESS + OBSERVABILITY + HOPS:
        fake.add_alarm(name)
    for name, region in COMPOSITES:
        fake.add_alarm(name, region, kind="CompositeAlarm")
    fake.add_ecs_service("orders")
    return fake


def environment(fake: FakeAws) -> context.Environment:
    return context.load_environment(fake, PRIMARY, STANDBY, ENV)


def spec_tests():
    return spec.load(str(SPEC_FILE)).select("all")


def reconciled_fake() -> FakeAws:
    """A deployment whose test already exists and matches the spec, ready for a run."""
    fake = deployed_fake()
    reconcile.reconcile(fake, environment(fake), spec_tests())
    fake.calls.clear()
    return fake
