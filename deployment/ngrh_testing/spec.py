# SPDX-License-Identifier: MIT-0
"""The test spec, deployment/ngrh-tests.json (design 5.8 and 6.1): loading and validation.

Pure: no AWS calls, so the Makefile, CI and the unit tests can check a spec without credentials.
``parse`` and ``load`` raise SpecError listing every problem they find, not only the first, and
the schema is strict: an unknown key is a typo until proven otherwise.

Names in the spec are written the way the templates spell them. Regions are ``primary`` or
``standby`` (resolved from the Makefile's PRIMARY_REGION and STANDBY_REGION), or a Region name.
An alarm reference's name gets ``-<Region><Env>`` appended when it is resolved, which is how
monitoring.yml names every alarm.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

SPEC_VERSION = 1

PLACEHOLDERS = ("primary", "standby")
EXPECTED_VALUES = ("PASS", "FAIL", "UNKNOWN")

# The NGRH services ngrh.yaml declares, by the name the Outputs use (OrdersServiceArn and so on).
SERVICES = ("ui", "catalog", "cart", "checkout", "orders", "assets")

# The back-ends behind ui that have hop alarms (monitoring.yml): their Service Connect names.
HOP_SERVICES = ("ui", "catalog", "carts", "checkout", "orders")
HOP_KINDS = ("errors", "slow")
HOP_ALARMS = tuple(f"hop-{service}-{kind}" for service in HOP_SERVICES for kind in HOP_KINDS)

# An evidence entry can name a group of alarms instead of one: it expands to these names.
ALARM_GROUPS: Dict[str, Tuple[str, ...]] = {"hop": HOP_ALARMS}

# Lookups a parameter value can ask for; context.py holds the functions (a test keeps the two in step).
KNOWN_LOOKUPS = ("mq-broker-host",)

# Run-level checks beyond NGRH's verdict (design 5.8). None exist yet: steps 11 and 12 add each
# name together with the code that evaluates it, so a spec can't name a check that silently never runs.
KNOWN_RUN_CHECKS: Tuple[str, ...] = ()

# NGRH's limits: five sources (success plus observability) per test; FIS allows five stop conditions
# per experiment template. The design keeps every parameter to ten values at most.
MAX_SOURCES = 5
MAX_STOP_CONDITIONS = 5
MAX_PARAMETER_VALUES = 10
MAX_DURATION_MINUTES = 720  # FIS's limit for one experiment is 12 hours

TEST_NAME = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
ALARM_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
REGION_NAME = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]$")
TEMPLATE_ID = re.compile(r"^aws-[a-z-]+:rt[a-z]+[0-9]+$")
DURATION = re.compile(r"^[1-9][0-9]{0,3}$")

# Template parameters whose value is a Region: a placeholder or a Region name, never a lookup.
REGION_PARAMETERS = ("region", "isolatedRegion", "destinationRegion", "impairedRegion", "recoveryRegion")


@dataclass(frozen=True)
class TemplateShape:
    """What a test template accepts: its parameters (NGRH's names), which are required, which
    take a list, and which parameter names the Region the fault lands in."""

    parameters: Tuple[str, ...]
    required: Tuple[str, ...]
    multi_valued: Tuple[str, ...]
    fault_region: str


# The templates the suite uses (research 01). AZ recovery is not among them: the workload's single
# faults end in a Region failover, so there are no AZ tests (requirements Q4).
TEMPLATES: Dict[str, TemplateShape] = {
    "aws-dependency-validation:rtdep001": TemplateShape(
        parameters=("region", "dependencies", "duration"),
        required=("region",),
        multi_valued=("dependencies",),
        fault_region="region",
    ),
    "aws-multi-region-isolation:rtmr001": TemplateShape(
        parameters=("isolatedRegion", "destinationRegion", "dependencies", "duration"),
        required=("isolatedRegion", "destinationRegion"),
        multi_valued=("dependencies",),
        fault_region="isolatedRegion",
    ),
    "aws-multi-region-recovery:rtmr002": TemplateShape(
        parameters=("impairedRegion", "recoveryRegion", "regionSwitchPlan", "dependencies", "duration"),
        required=("impairedRegion", "recoveryRegion"),
        multi_valued=("dependencies",),
        fault_region="impairedRegion",
    ),
}


@dataclass(frozen=True)
class AlarmRef:
    name: str  # without the -<Region><Env> suffix
    region: str  # primary, standby or a Region name


@dataclass(frozen=True)
class EvidenceRef:
    """An alarm, or a group of alarms, whose history the report shows but NGRH doesn't watch."""

    region: str
    name: Optional[str] = None
    group: Optional[str] = None

    def alarm_names(self) -> Tuple[str, ...]:
        return ALARM_GROUPS[self.group] if self.group else (self.name or "",)


@dataclass(frozen=True)
class Lookup:
    """A parameter value found at run time: the lookup's name and the Region to look in."""

    lookup: str
    region: str


ParameterValue = Union[str, Lookup]


@dataclass(frozen=True)
class Test:
    name: str
    service: str
    template: str
    parameters: Mapping[str, Tuple[ParameterValue, ...]]
    success_alarms: Tuple[AlarmRef, ...]
    observability_alarms: Tuple[AlarmRef, ...]
    stop_alarms: Tuple[AlarmRef, ...]
    evidence_alarms: Tuple[EvidenceRef, ...]
    run_checks: Tuple[str, ...]
    expected: str

    @property
    def shape(self) -> TemplateShape:
        return TEMPLATES[self.template]

    @property
    def duration_minutes(self) -> Optional[int]:
        """The duration parameter, when the spec gives one as a plain number."""
        values = self.parameters.get("duration", ())
        return int(values[0]) if len(values) == 1 and isinstance(values[0], str) and values[0].isdigit() else None


@dataclass(frozen=True)
class Spec:
    version: int
    tests: Tuple[Test, ...]

    def names(self) -> List[str]:
        return [t.name for t in self.tests]

    def select(self, name: str) -> List[Test]:
        """One test by name, or every test for ``all``."""
        if name == "all":
            return list(self.tests)
        picked = [t for t in self.tests if t.name == name]
        if not picked:
            raise SpecError([f"no test named {name!r}; the spec has: {', '.join(self.names())}"])
        return picked


class SpecError(ValueError):
    """The spec is not valid. ``problems`` holds one line for each thing wrong with it."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def _no_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    seen: Dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise SpecError([f"the key {key!r} appears twice in one object"])
        seen[key] = value
    return seen


def load_text(text: str) -> Spec:
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as e:
        raise SpecError([f"not valid JSON: {e}"]) from e
    return parse(data)


def load(path: str) -> Spec:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise SpecError([f"cannot read {path}: {e.strerror or e}"]) from e
    return load_text(text)


def _is_region(value: str) -> bool:
    return value in PLACEHOLDERS or bool(REGION_NAME.match(value))


def _alarm_refs(
    where: str, items: Any, problems: List[str], prefix: Optional[str] = None, limit: Optional[int] = None
) -> Tuple[AlarmRef, ...]:
    if not isinstance(items, list):
        problems.append(f"{where}: must be a list")
        return ()
    if limit is not None and len(items) > limit:
        problems.append(f"{where}: at most {limit} alarms, found {len(items)}")
    refs = []
    for i, item in enumerate(items):
        here = f"{where}[{i}]"
        if not isinstance(item, dict) or set(item) != {"name", "region"}:
            problems.append(f"{here}: must be an object with exactly 'name' and 'region'")
            continue
        name, region = item["name"], item["region"]
        if not isinstance(name, str) or not ALARM_NAME.match(name):
            problems.append(f"{here}: name must be a lower-case alarm name without the Region or Env suffix")
            continue
        if not isinstance(region, str) or not _is_region(region):
            problems.append(f"{here}: region must be 'primary', 'standby' or a Region name")
            continue
        if prefix and not name.startswith(prefix):
            problems.append(f"{here}: {name!r} must start with {prefix!r}")
        refs.append(AlarmRef(name, region))
    return tuple(refs)


def _evidence_refs(where: str, items: Any, problems: List[str]) -> Tuple[EvidenceRef, ...]:
    if not isinstance(items, list):
        problems.append(f"{where}: must be a list")
        return ()
    refs = []
    for i, item in enumerate(items):
        here = f"{where}[{i}]"
        if not isinstance(item, dict) or set(item) not in ({"name", "region"}, {"group", "region"}):
            problems.append(f"{here}: must be an object with 'region' and either 'name' or 'group'")
            continue
        region = item["region"]
        if not isinstance(region, str) or not _is_region(region):
            problems.append(f"{here}: region must be 'primary', 'standby' or a Region name")
            continue
        if "group" in item:
            if item["group"] not in ALARM_GROUPS:
                problems.append(f"{here}: unknown group {item['group']!r}; known: {', '.join(sorted(ALARM_GROUPS))}")
                continue
            refs.append(EvidenceRef(region, group=item["group"]))
        else:
            name = item["name"]
            if not isinstance(name, str) or not ALARM_NAME.match(name):
                problems.append(f"{here}: name must be a lower-case alarm name without the Region or Env suffix")
                continue
            refs.append(EvidenceRef(region, name=name))
    return tuple(refs)


def _parameters(
    where: str, template: str, items: Any, problems: List[str]
) -> Mapping[str, Tuple[ParameterValue, ...]]:
    if not isinstance(items, dict):
        problems.append(f"{where}: must be an object")
        return {}
    shape = TEMPLATES[template]
    for key in items:
        if key not in shape.parameters:
            problems.append(f"{where}: {key!r} is not a parameter of {template}; it takes {', '.join(shape.parameters)}")
    for key in shape.required:
        if key not in items:
            problems.append(f"{where}: {template} requires {key!r}")
    out: Dict[str, Tuple[ParameterValue, ...]] = {}
    for key, values in items.items():
        here = f"{where}.{key}"
        if key not in shape.parameters:
            continue
        if not isinstance(values, list) or not values:
            problems.append(f"{here}: must be a non-empty list of values")
            continue
        if len(values) > MAX_PARAMETER_VALUES:
            problems.append(f"{here}: at most {MAX_PARAMETER_VALUES} values, found {len(values)}")
        if key not in shape.multi_valued and len(values) != 1:
            problems.append(f"{here}: takes exactly one value, found {len(values)}")
        parsed: List[ParameterValue] = []
        for i, value in enumerate(values):
            if isinstance(value, str) and value:
                if key in REGION_PARAMETERS and not _is_region(value):
                    problems.append(f"{here}[{i}]: must be 'primary', 'standby' or a Region name, found {value!r}")
                parsed.append(value)
            elif isinstance(value, dict) and set(value) == {"lookup", "region"}:
                if key in REGION_PARAMETERS or key == "duration":
                    problems.append(f"{here}[{i}]: a lookup can't supply {key}")
                elif value["lookup"] not in KNOWN_LOOKUPS:
                    problems.append(f"{here}[{i}]: unknown lookup {value['lookup']!r}; known: {', '.join(KNOWN_LOOKUPS)}")
                elif not isinstance(value["region"], str) or not _is_region(value["region"]):
                    problems.append(f"{here}[{i}]: region must be 'primary', 'standby' or a Region name")
                else:
                    parsed.append(Lookup(value["lookup"], value["region"]))
            else:
                problems.append(f"{here}[{i}]: must be a non-empty string or an object with 'lookup' and 'region'")
        if key in shape.multi_valued and any(isinstance(v, str) and v in PLACEHOLDERS for v in parsed):
            problems.append(f"{here}: a Region placeholder is only meaningful in a Region parameter")
        if key == "duration" and len(parsed) == 1:
            v = parsed[0]
            if not isinstance(v, str) or not DURATION.match(v) or int(v) > MAX_DURATION_MINUTES:
                problems.append(f"{here}: must be a whole number of minutes from 1 to {MAX_DURATION_MINUTES}")
        out[key] = tuple(parsed)
    return out


def _parse_test(index: int, data: Any, problems: List[str]) -> Optional[Test]:
    label = f"tests[{index}]"
    if not isinstance(data, dict):
        problems.append(f"{label}: must be an object")
        return None
    name = data.get("name")
    where = f"{label} ({name})" if isinstance(name, str) else label
    allowed = {"name", "service", "template", "parameters", "successAlarms", "observabilityAlarms",
               "stopAlarms", "evidenceAlarms", "runChecks", "expected"}
    required = {"name", "service", "template", "parameters", "successAlarms", "expected"}
    for key in sorted(set(data) - allowed):
        problems.append(f"{where}: unknown key {key!r}")
    for key in sorted(required - set(data)):
        problems.append(f"{where}: missing {key!r}")
    if not isinstance(name, str) or not TEST_NAME.match(name):
        problems.append(f"{where}: name must be lower-case letters, digits and dashes, 3 to 63 characters")
    service = data.get("service")
    if service not in SERVICES:
        problems.append(f"{where}: service must be one of {', '.join(SERVICES)}")
    template = data.get("template")
    if not isinstance(template, str) or not TEMPLATE_ID.match(template):
        problems.append(f"{where}: template must look like aws-dependency-validation:rtdep001")
    elif template not in TEMPLATES:
        problems.append(f"{where}: template {template!r} is not one the suite uses: {', '.join(sorted(TEMPLATES))}")
    expected = data.get("expected")
    if expected not in EXPECTED_VALUES:
        problems.append(f"{where}: expected must be one of {', '.join(EXPECTED_VALUES)}")

    success = _alarm_refs(f"{where}: successAlarms", data.get("successAlarms", []), problems, prefix="journey-")
    observability = _alarm_refs(f"{where}: observabilityAlarms", data.get("observabilityAlarms", []), problems)
    stop = _alarm_refs(f"{where}: stopAlarms", data.get("stopAlarms", []), problems, limit=MAX_STOP_CONDITIONS)
    evidence = _evidence_refs(f"{where}: evidenceAlarms", data.get("evidenceAlarms", []), problems)
    if not success:
        problems.append(f"{where}: at least one success alarm is needed; NGRH won't start a test without one")
    if len(success) + len(observability) > MAX_SOURCES:
        problems.append(f"{where}: NGRH takes at most {MAX_SOURCES} sources (success plus observability), "
                        f"found {len(success) + len(observability)}")
    checks = data.get("runChecks", [])
    if not isinstance(checks, list) or any(not isinstance(c, str) for c in checks):
        problems.append(f"{where}: runChecks must be a list of names")
        checks = []
    for check in checks:
        if check not in KNOWN_RUN_CHECKS:
            problems.append(f"{where}: unknown run check {check!r}; none are implemented yet")

    valid_template = isinstance(template, str) and template in TEMPLATES
    parameters: Mapping[str, Tuple[ParameterValue, ...]] = {}
    if valid_template:
        parameters = _parameters(f"{where}: parameters", template, data.get("parameters", {}), problems)
    if not isinstance(name, str) or service not in SERVICES or not valid_template:
        return None
    return Test(
        name=name, service=service, template=template, parameters=parameters, success_alarms=success,
        observability_alarms=observability, stop_alarms=stop, evidence_alarms=evidence,
        run_checks=tuple(checks), expected=expected if isinstance(expected, str) else "UNKNOWN",
    )


def parse(data: Any) -> Spec:
    problems: List[str] = []
    if not isinstance(data, dict):
        raise SpecError(["the spec must be a JSON object"])
    for key in sorted(set(data) - {"version", "tests"}):
        problems.append(f"unknown key {key!r}")
    if data.get("version") != SPEC_VERSION:
        problems.append(f"version must be {SPEC_VERSION}, found {data.get('version')!r}")
    raw_tests = data.get("tests")
    if not isinstance(raw_tests, list) or not raw_tests:
        problems.append("tests must be a non-empty list")
        raw_tests = []
    tests = [t for t in (_parse_test(i, d, problems) for i, d in enumerate(raw_tests)) if t is not None]
    names = [t.name for t in tests]
    for name in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"the test name {name!r} is used more than once")
    pairs = [(t.service, t.template) for t in tests]
    for service, template in sorted({p for p in pairs if pairs.count(p) > 1}):
        who = ", ".join(t.name for t in tests if (t.service, t.template) == (service, template))
        problems.append(f"{who} all test service {service} with {template}; Resilience Hub allows one test per service and template")
    if problems:
        raise SpecError(problems)
    return Spec(version=SPEC_VERSION, tests=tuple(tests))
