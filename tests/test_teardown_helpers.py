"""Behavioural tests for the teardown helpers, run against a stub `aws`.

Two defects surfaced by e2e run 35783034631 (2026-09-23):

1. detach-global-cluster.sh issued the writer's RemoveFromGlobalCluster right
   after the readers'. The API is asynchronous, so the reader was still listed,
   Aurora answered "Can't remove writer cluster when there are other clusters",
   the failure was lost inside a `| while read` subshell, the script timed out
   polling for zero members, and `make destroy-all` aborted at
   destroy-databases-standby -- leaving every later target (secrets-rotation,
   codebuild, arc-dns-status, chaos, baseInfra, baseVpc) untouched.

2. The e2e Teardown guard recovered the databases but assumed destroy-all had
   already removed the small stacks, so eight stacks survived a green run.

A third from run 35887370433 (2026-09-23), the run that proved the first two:

3. destroy-apps-* emptied the canary bucket and then deleted the apps stack, but
   the ALB kept delivering access logs for a minute after the empty, so when
   destroy-infra reached baseVpc an hour and a half later CloudFormation failed
   the stack on canaryBucket ("The bucket you tried to delete is not empty").
   The guard's retry did not empty the bucket either. delete-vpc-stack.sh now
   empties it immediately before every attempt and retries a bucket-only
   failure; both destroy-infra and the guard go through it.

A fourth, found by counting Cloud Map namespaces in the e2e account on 2026-09-30:

4. destroy-cloudmap-namespace sent every `aws servicediscovery` error to
   /dev/null and printed "no namespace found" on failure. The e2e role had no
   servicediscovery permission, so every run since the target was added (08-18)
   reported a clean teardown while leaking the retail-store-ar namespace ECS
   Service Connect had created: 61 namespaces, against a quota of 50 per region
   at which EcsCluster creation fails. delete-cloudmap-namespace.sh now owns the
   deletion for both the Makefile and the guard, exits non-zero on any failure,
   and the role grants the three calls it makes.

A fifth, found reviewing the FIS sidecar change before its first e2e run
(2026-10-06):

5. regionalBaseInfra.yaml gained the amazon-ssm-agent repository the sidecar
   image is mirrored into, but neither destroy-ecr-* nor the guard
   force-deleted it. CloudFormation can't delete a repository that still holds
   images, so baseInfra would have failed to delete in both Regions. Both lists
   are now checked against the repositories baseInfra declares.

A sixth, from run 37401247653 (2026-10-06), the first run whose destroy-all got
all the way to its last target:

6. destroy-all deleted both Cloud Map namespaces, the guard ran the helper again
   33 s later while Cloud Map was still deleting them, and DeleteNamespace
   answered DuplicateRequest. The helper called that a failure, so a teardown
   that left nothing behind ended "Teardown incomplete". A delete already in
   progress now waits for the namespace to disappear, and is a failure only if
   it is still listed when the wait runs out.

A seventh, found counting what the passing run 37485943263 left in the e2e account
(2026-10-06):

7. The teardown deleted every stack and still left 28 CloudWatch log groups, 15 in
   us-east-1 and 13 in us-west-2, exactly as the run before it had. Lambda (the
   canaries and the custom resources), CodeBuild, Container Insights, RDS and the
   services create their log groups outside CloudFormation, and none of them
   expires. delete-run-log-groups.sh now deletes a passing run's own groups, in a
   step of their own that a failed run skips, so its logs survive for the
   post-mortem.

An eighth, found counting what the passing run 37533695833 left in the e2e account
(2026-10-07):

8. The log group step found 15 and 13 of a run's 23 and 21 groups. Two kinds of
   name don't carry the commit sha as a whole token. Synthetics builds a canary's
   Lambda function as cwsyn-<canary name cut to 21 characters>-<uuid>, which cuts
   the sha out of five of the twelve names (cwsyn-lcl-rgnl-catalog-3988-<uuid> for
   the run -398824f). And a RabbitMQ broker's three groups are named by the
   broker's id, under /aws/amazonmq/broker/, with the broker already deleted by the
   time the step runs. The helper now lists each canary's exact cut name, and takes
   the broker ids from a file that the step "Record the message brokers of this
   run" writes before the Teardown.

The stub `aws` below is a tiny state machine over a JSON file: RDS global
cluster membership with asynchronous removal, CloudFormation stacks with
asynchronous deletion and an optional first-attempt failure, S3 buckets
whose objects can "land" between an empty and CloudFormation's DeleteBucket,
Cloud Map namespaces with a per-region AccessDenied switch, CloudWatch log
groups answering the service's substring pattern and prefix filters (never
both at once) a page per line, and Amazon MQ brokers listed by name.
Every invocation is appended to a log so the tests can assert ORDER, not just
end state.

Run with:  pytest tests/test_teardown_helpers.py -v
"""

import json
import os
import re
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).parent.parent
DEPLOYMENT = REPO / "deployment"
DETACH = Path(os.environ.get("DETACH_SCRIPT", DEPLOYMENT / "detach-global-cluster.sh"))
DELETE_VPC = DEPLOYMENT / "delete-vpc-stack.sh"
DELETE_NS = DEPLOYMENT / "delete-cloudmap-namespace.sh"
MAKEFILE = DEPLOYMENT / "Makefile"
OIDC_ROLE = DEPLOYMENT / "github-oidc-role.yaml"
ECS_TEMPLATE = DEPLOYMENT / "ecs.yaml"
E2E_WORKFLOW = Path(os.environ.get("E2E_WORKFLOW", REPO / ".github" / "workflows" / "e2e.yml"))
# The stub_env fixture puts a fake `make` first on PATH (destroy-all bailing); the
# Makefile target tests need the real one, resolved before that PATH is built.
REAL_MAKE = shutil.which("make")

WRITER = "arn:aws:rds:us-east-1:111111111111:cluster:catalog-dbcluster-01-us-east-1-t"
READER = "arn:aws:rds:us-west-2:111111111111:cluster:catalog-dbcluster-02-us-west-2-t"

STUB_AWS = r'''#!/usr/bin/env python3
"""Stub `aws` for teardown tests. State in $STUB_STATE (JSON), call log in $STUB_LOG."""
import json, os, re, sys

state_path, log_path = os.environ["STUB_STATE"], os.environ["STUB_LOG"]
argv = sys.argv[1:]
with open(log_path, "a") as f:
    f.write(" ".join(argv) + "\n")
state = json.load(open(state_path))

def save():
    json.dump(state, open(state_path, "w"))

NEGATIVE_NUMBER = re.compile(r"^-\d+$|^-\d*\.\d+$")

def opt(name):
    """The value of an option, read the way the real CLI reads it: `--name=value`, or `--name value`.

    The real CLI (argparse) takes a separate value that starts with "-" for the next option, unless it
    looks like a negative number, so `--log-group-name-pattern -a1802e5` fails with exit 252 and
    "expected one argument" while `--log-group-name-pattern=-a1802e5` works. A stub that returned
    argv[index + 1] whatever it held passed the first form for as long as the helper used it, and the
    e2e run that finally met the real CLI left its log groups behind.
    """
    for i, arg in enumerate(argv):
        if arg.startswith(name + "="):
            return arg[len(name) + 1:]
        if arg == name:
            value = argv[i + 1] if i + 1 < len(argv) else None
            if value is None or (value.startswith("-") and not NEGATIVE_NUMBER.match(value)):
                sys.stderr.write("\naws: [ERROR]: An error occurred (ParamValidation): argument %s: expected one argument\n" % name)
                sys.exit(252)
            return value
    return None

def fail(msg, code=254):
    sys.stderr.write("\nAn error occurred " + msg + "\n")
    sys.exit(code)

svc, op = argv[0], argv[1]

if svc == "rds" and op == "describe-global-clusters":
    if state.get("global_gone"):
        fail("(GlobalClusterNotFoundFault) when calling the DescribeGlobalClusters operation: Global cluster not found")
    kept = []
    for m in state["members"]:
        if m.get("leaving") is not None:
            m["leaving"] -= 1
            if m["leaving"] <= 0:
                continue
        kept.append(m)
    state["members"] = kept
    save()
    for m in kept:
        print(m["arn"] + "\t" + ("True" if m["writer"] else "False"))
    sys.exit(0)

if svc == "rds" and op == "remove-from-global-cluster":
    arn = opt("--db-cluster-identifier")
    if arn in state.get("fail_remove", []):
        fail("(InvalidDBClusterStateFault) when calling the RemoveFromGlobalCluster operation: stub-induced failure")
    target = next((m for m in state["members"] if m["arn"] == arn), None)
    if target is None:
        fail("(InvalidParameterValue) when calling the RemoveFromGlobalCluster operation: The cluster is not a member")
    if target["writer"] and any(m is not target for m in state["members"]):
        fail("(InvalidParameterValue) when calling the RemoveFromGlobalCluster operation: Can't remove writer cluster when there are other clusters")
    target["leaving"] = state.get("leave_after", 2)   # stays listed for N more describes
    save()
    sys.exit(0)

if svc == "cloudformation":
    region = opt("--region")
    stacks = state["stacks"].setdefault(region, {})
    buckets = state.setdefault("buckets", {})

    def tick():
        # Asynchronous deletes: an in-progress stack finishes on the next listing.
        for name, st in list(stacks.items()):
            if st["status"] == "DELETE_IN_PROGRESS":
                if name in state.get("fail_first", []) and not st.get("failed_once"):
                    st.update(status="DELETE_FAILED", failed_once=True)
                elif st.get("blocked_on"):
                    st.update(status="DELETE_FAILED", failed=st.pop("blocked_on"))
                else:
                    buckets.pop(st.get("bucket", ""), None)   # the bucket goes with its stack
                    del stacks[name]
        save()

    if op == "describe-stacks":
        name = opt("--stack-name")
        query = opt("--query") or ""
        if name is not None:
            if name not in stacks:
                fail("(ValidationError) when calling the DescribeStacks operation: Stack with id %s does not exist" % name)
            print(stacks[name]["status"] if "StackStatus" in query else name)
            sys.exit(0)
        tick()
        m = re.search(r"ends_with\(StackName, '([^']+)'\)", query)
        names = [n for n in stacks if m is None or n.endswith(m.group(1))]
        if "!ends_with(StackStatus, '_IN_PROGRESS')" in query:
            names = [n for n in names if not stacks[n]["status"].endswith("_IN_PROGRESS")]
        elif "ends_with(StackStatus, '_IN_PROGRESS')" in query:
            names = [n for n in names if stacks[n]["status"].endswith("_IN_PROGRESS")]
        print("\t".join(names))
        sys.exit(0)

    if op == "describe-stack-resources":
        name = opt("--stack-name")
        query = opt("--query") or ""
        if name not in stacks:
            fail("(ValidationError) when calling the DescribeStackResources operation: Stack with id %s does not exist" % name)
        st = stacks[name]
        if opt("--logical-resource-id") == "canaryBucket":
            print(st.get("bucket") or "None")
        elif "DELETE_FAILED" in query:
            failed = st.get("failed", []) if st["status"] == "DELETE_FAILED" else []
            if "ResourceStatusReason" in query:
                for r in failed:
                    print(r + "\tThe bucket you tried to delete is not empty" if r == "canaryBucket" else r + "\tresource has a dependent object")
            else:
                print("\t".join(failed))
        sys.exit(0)

    if op == "delete-stack":
        name = opt("--stack-name")
        if name in stacks:
            st = stacks[name]
            st["status"] = "DELETE_IN_PROGRESS"
            st.pop("failed", None)
            b = st.get("bucket")
            if b in buckets:
                # Objects delivered between the emptier and CloudFormation's
                # DeleteBucket (ALB access-log flush, S3 server access logs) land now.
                buckets[b]["objects"] += buckets[b].pop("late_objects", 0)
                if buckets[b]["objects"] > 0:
                    st["blocked_on"] = ["canaryBucket"]
            if st.get("fail_resource"):
                st["blocked_on"] = [st["fail_resource"]]    # e.g. a Vpc that still has dependencies
            save()
        sys.exit(0)

    if op == "wait":
        name = opt("--stack-name")
        tick()
        if name not in stacks:
            sys.exit(0)
        if stacks[name]["status"] == "DELETE_FAILED":
            fail("Waiter StackDeleteComplete failed: terminal failure state DELETE_FAILED", 255)
        sys.exit(0)

if svc == "s3api":
    buckets = state.setdefault("buckets", {})
    bucket = opt("--bucket")
    if op == "head-bucket":
        if bucket not in buckets:
            fail("(404) when calling the HeadBucket operation: Not Found")
        print("{}")
        sys.exit(0)
    if op == "list-object-versions":
        n = buckets[bucket]["objects"]
        print(json.dumps({"Versions": [{"Key": "alb-access-logs/%d" % i, "VersionId": "null"} for i in range(n)]}))
        sys.exit(0)
    if op == "delete-objects":
        buckets[bucket]["objects"] = 0
        save()
        sys.exit(0)

if svc == "servicediscovery":
    region = opt("--region")
    namespaces = state.setdefault("namespaces", {}).setdefault(region, [])
    action = "".join(p.title() for p in op.split("-"))
    if region in state.get("cloudmap_denied", []):
        fail("(AccessDeniedException) when calling the %s operation: User: arn:aws:sts::111111111111:assumed-role/github-actions-microservice-e2e/e2e is not authorized to perform: servicediscovery:%s" % (action, action))
    if op == "list-namespaces":
        # DeleteNamespace is asynchronous. A namespace whose delete is already in
        # flight carries "deleting": it stays listed for that many further calls,
        # then it is gone.
        for n in list(namespaces):
            if "deleting" in n:
                if n["deleting"] <= 0:
                    namespaces.remove(n)
                else:
                    n["deleting"] -= 1
        save()
        m = re.search(r"Name=='([^']+)'", opt("--query") or "")
        hits = [n for n in namespaces if m is None or n["name"] == m.group(1)]
        print(hits[0]["id"] if hits else "None")
        sys.exit(0)
    if op == "list-services":
        m = re.search(r"Values=([^,]+)", opt("--filters") or "")
        ns = next((n for n in namespaces if m and n["id"] == m.group(1)), None)
        print(ns.get("services", 0) if ns else 0)
        sys.exit(0)
    if op == "delete-namespace":
        ns_id = opt("--id")
        ns = next((n for n in namespaces if n["id"] == ns_id), None)
        if ns is not None and ns.get("vanishes"):
            # The delete a previous destroy started finished between the caller's lookup and this call.
            namespaces.remove(ns)
            save()
            ns = None
        if ns is None:
            fail("(NamespaceNotFound) when calling the DeleteNamespace operation: Namespace %s not found" % ns_id)
        if "deleting" in ns:
            fail("(DuplicateRequest) when calling the DeleteNamespace operation: Another operation of type DeleteNamespace and id op-%s is in progress" % ns_id)
        if ns.get("services"):
            fail("(ResourceInUse) when calling the DeleteNamespace operation: Namespace %s still has services" % ns_id)
        if ns.get("delete_fails"):
            fail("(InternalServiceError) when calling the DeleteNamespace operation: stub-induced failure", 255)
        namespaces.remove(ns)
        save()
        print(json.dumps({"OperationId": "op-" + ns_id}))
        sys.exit(0)

if svc == "logs":
    region = opt("--region")
    groups = state.setdefault("log_groups", {}).setdefault(region, [])
    action = "".join(p.title() for p in op.split("-"))
    if region in state.get("logs_denied", []):
        fail("(AccessDeniedException) when calling the %s operation: User: arn:aws:sts::111111111111:assumed-role/github-actions-microservice-e2e/e2e is not authorized to perform: logs:%s" % (action, action))
    if op == "describe-log-groups":
        # --log-group-name-pattern is a case-sensitive substring match and
        # --log-group-name-prefix a case-sensitive prefix match, both done by the
        # service, and the two can't be combined. The text output has one page per
        # line, names tab-separated.
        pattern, prefix = opt("--log-group-name-pattern"), opt("--log-group-name-prefix")
        if pattern is not None and prefix is not None:
            fail("(InvalidParameterException) when calling the DescribeLogGroups operation: LogGroup name prefix and LogGroup name pattern are mutually exclusive parameters.")
        for bad in state.get("describe_fails_for_prefix", []):
            if prefix is not None and prefix.startswith(bad):
                fail("(ThrottlingException) when calling the DescribeLogGroups operation: Rate exceeded", 255)
        hits = [g for g in groups + state.get("ghosts", [])
                if (pattern is None or pattern in g) and (prefix is None or g.startswith(prefix))]
        page = state.get("page_size", 3)
        for i in range(0, len(hits), page):
            print("\t".join(hits[i:i + page]))
        if not hits and state.get("empty_as_none"):
            print("None")
        sys.exit(0)
    if op == "delete-log-group":
        name = opt("--log-group-name")
        if name in state.get("delete_denied", []):
            fail("(AccessDeniedException) when calling the DeleteLogGroup operation: User: arn:aws:sts::111111111111:assumed-role/github-actions-microservice-e2e/e2e is not authorized to perform: logs:DeleteLogGroup on resource: " + name)
        if name not in groups:
            fail("(ResourceNotFoundException) when calling the DeleteLogGroup operation: The specified log group does not exist.")
        groups.remove(name)
        save()
        sys.exit(0)

if svc == "mq":
    region = opt("--region")
    action = "".join(p.title() for p in op.split("-"))
    if region in state.get("mq_denied", []):
        fail("(ForbiddenException) when calling the %s operation: User: arn:aws:sts::111111111111:assumed-role/github-actions-microservice-e2e/e2e is not authorized to perform: mq:%s" % (action, action))
    if op == "list-brokers":
        # Only the name filter of the query is modelled: BrokerName=='<name>'. The text
        # output is the ids, tab-separated, and "None" when the CLI finds nothing.
        m = re.search(r"BrokerName=='([^']+)'", opt("--query") or "")
        hits = [b["id"] for b in state.setdefault("brokers", {}).get(region, []) if m is None or b["name"] == m.group(1)]
        if hits or not state.get("empty_as_none"):
            print("\t".join(hits))
        else:
            print("None")
        sys.exit(0)

# ecr delete-repository, secretsmanager delete-secret, anything else: accepted, no output.
sys.exit(0)
'''


def _install(dir_: Path, name: str, body: str) -> None:
    p = dir_ / name
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def stub_env(tmp_path):
    """PATH with stub aws/make/sleep first; returns (env, state_path, log_path)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _install(bindir, "aws", STUB_AWS)
    _install(bindir, "sleep", "#!/bin/sh\nexit 0\n")
    # destroy-all bailing at the database step, exactly as in run 35783034631.
    _install(bindir, "make", "#!/bin/sh\necho 'make: *** [Makefile:838: destroy-databases-standby] Error 1' >&2\nexit 2\n")
    state = tmp_path / "state.json"
    log = tmp_path / "calls.log"
    log.write_text("")
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    env = dict(os.environ)
    env.update(PATH=f"{bindir}:{env['PATH']}", STUB_STATE=str(state), STUB_LOG=str(log),
               POLL_ATTEMPTS="20", POLL_SLEEP="0", RUNNER_TEMP=str(runner_temp))
    return env, state, log


def _calls(log: Path):
    return [line.split() for line in log.read_text().splitlines()]


def _run_detach(env):
    return subprocess.run([str(DETACH), "catalog-global-db-cluster-t", "us-east-1"],
                          env=env, capture_output=True, text=True, timeout=60)


# ---------------------------------------------------------------------------
# detach-global-cluster.sh
# ---------------------------------------------------------------------------

class TestDetachGlobalCluster:

    def test_writer_detached_only_after_reader_has_left(self, stub_env):
        env, state, log = stub_env
        state.write_text(json.dumps({
            "members": [{"arn": WRITER, "writer": True}, {"arn": READER, "writer": False}],
            "leave_after": 3,   # the reader stays listed for three more describes
        }))
        r = _run_detach(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "All members detached" in r.stdout

        calls = _calls(log)
        removes = [c for c in calls if c[:2] == ["rds", "remove-from-global-cluster"]]
        assert [c[c.index("--db-cluster-identifier") + 1] for c in removes] == [READER, WRITER]

        # Between the two removes the script must have re-described the cluster
        # until the reader was gone -- i.e. it waited, it did not just fire.
        idx_reader = next(i for i, c in enumerate(calls) if READER in c and "remove-from-global-cluster" in c)
        idx_writer = next(i for i, c in enumerate(calls) if WRITER in c and "remove-from-global-cluster" in c)
        describes_between = [c for c in calls[idx_reader + 1:idx_writer] if c[1] == "describe-global-clusters"]
        assert len(describes_between) >= 3
        assert "Can't remove writer" not in r.stdout + r.stderr

    def test_reader_failure_stops_before_the_writer(self, stub_env):
        env, state, log = stub_env
        state.write_text(json.dumps({
            "members": [{"arn": WRITER, "writer": True}, {"arn": READER, "writer": False}],
            "fail_remove": [READER],
        }))
        r = _run_detach(env)
        assert r.returncode != 0
        assert "stub-induced failure" in r.stderr
        removes = [c for c in _calls(log) if c[:2] == ["rds", "remove-from-global-cluster"]]
        # The old `| while read` swallowed this failure and went on to the writer.
        assert len(removes) == 1 and READER in removes[0]

    def test_writer_only_and_already_gone_are_no_ops(self, stub_env):
        env, state, log = stub_env
        state.write_text(json.dumps({"members": [{"arn": WRITER, "writer": True}], "leave_after": 1}))
        r = _run_detach(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert sum(1 for c in _calls(log) if c[:2] == ["rds", "remove-from-global-cluster"]) == 1

        state.write_text(json.dumps({"members": [], "global_gone": True}))
        log.write_text("")
        r = _run_detach(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "does not exist" in r.stdout


# ---------------------------------------------------------------------------
# delete-vpc-stack.sh
# ---------------------------------------------------------------------------

VPC_STACK = "baseVpc-t"
BUCKET = "basevpc-t-canarybucket-jozk5jwh70zc"


def _run_delete_vpc(env, region="us-west-2"):
    return subprocess.run([str(DELETE_VPC), VPC_STACK, region], env=env,
                          capture_output=True, text=True, timeout=60)


def _bucket_ops(log: Path):
    """(kind, name) per relevant call, in order: ('empty', bucket) or ('delete', stack)."""
    ops = []
    for c in _calls(log):
        if c[:2] == ["s3api", "delete-objects"]:
            ops.append(("empty", c[c.index("--bucket") + 1]))
        elif c[:2] == ["cloudformation", "delete-stack"]:
            ops.append(("delete", c[c.index("--stack-name") + 1]))
    return ops


class TestDeleteVpcStack:

    def test_bucket_is_emptied_right_before_the_delete_and_again_when_a_log_lands(self, stub_env):
        env, state, log = stub_env
        # Three objects now, and two more (an ALB access-log flush) that land in
        # the window between the empty and CloudFormation's DeleteBucket.
        state.write_text(json.dumps({
            "stacks": {"us-west-2": {VPC_STACK: {"status": "CREATE_COMPLETE", "bucket": BUCKET}}},
            "buckets": {BUCKET: {"objects": 3, "late_objects": 2}},
        }))
        r = _run_delete_vpc(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "an object landed after the empty" in r.stdout
        assert _bucket_ops(log) == [("empty", BUCKET), ("delete", VPC_STACK),
                                    ("empty", BUCKET), ("delete", VPC_STACK)]
        final = json.loads(state.read_text())
        assert final["stacks"]["us-west-2"] == {} and BUCKET not in final["buckets"]

    def test_delete_failed_stack_with_its_ssm_parameter_gone_is_recovered(self, stub_env):
        env, state, log = stub_env
        # The exact residue of run 35887370433: destroy-all's attempt already left
        # the stack DELETE_FAILED on canaryBucket, every other resource (including
        # the canaryBucketName SSM parameter) gone, three ALB log objects in the
        # bucket. The stub has no SSM at all, so the bucket must be resolved from
        # the stack's own resource list.
        state.write_text(json.dumps({
            "stacks": {"us-west-2": {VPC_STACK: {"status": "DELETE_FAILED", "failed": ["canaryBucket"],
                                                 "bucket": BUCKET}}},
            "buckets": {BUCKET: {"objects": 3}},
        }))
        r = _run_delete_vpc(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "deleted successfully" in r.stdout
        assert _bucket_ops(log) == [("empty", BUCKET), ("delete", VPC_STACK)]
        assert not any(c[0] == "ssm" for c in _calls(log))
        assert json.loads(state.read_text())["stacks"]["us-west-2"] == {}

    def test_a_failure_on_any_other_resource_is_reported_and_not_retried(self, stub_env):
        env, state, log = stub_env
        state.write_text(json.dumps({
            "stacks": {"us-west-2": {VPC_STACK: {"status": "CREATE_COMPLETE", "bucket": BUCKET,
                                                 "fail_resource": "Vpc"}}},
            "buckets": {BUCKET: {"objects": 0}},
        }))
        r = _run_delete_vpc(env)
        assert r.returncode != 0
        assert "DELETE_FAILED on [Vpc]" in r.stderr
        assert "resource has a dependent object" in r.stderr
        assert [o for o in _bucket_ops(log) if o[0] == "delete"] == [("delete", VPC_STACK)]

    def test_already_gone_stack_is_a_no_op(self, stub_env):
        env, state, log = stub_env
        state.write_text(json.dumps({"stacks": {"us-west-2": {}}, "buckets": {}}))
        r = _run_delete_vpc(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "already gone" in r.stdout
        assert _bucket_ops(log) == []


# ---------------------------------------------------------------------------
# e2e Teardown step (rendered from .github/workflows/e2e.yml)
# ---------------------------------------------------------------------------

ENV_SUFFIX = "-abc1234"
PRIMARY, STANDBY = "us-east-1", "us-west-2"


def _teardown_script() -> str:
    wf = yaml.safe_load(E2E_WORKFLOW.read_text())
    job = next(iter(wf["jobs"].values()))
    run = next(s for s in job["steps"] if s.get("name") == "Teardown")["run"]
    assert "${{" not in run, "a GitHub expression in the Teardown run block: use the job's environment variables"
    return run


def _job_env(env):
    """The variables the job gives every step: ENV from $GITHUB_ENV, the Regions from the job's env."""
    return dict(env, ENV=ENV_SUFFIX, AWS_REGION=PRIMARY, STANDBY_REGION=STANDBY)


def _expanded(script):
    """The script with the job's variables filled in, for tests that match on what it would run."""
    return (script.replace("${ENV}", ENV_SUFFIX).replace("${AWS_REGION}", PRIMARY)
                  .replace("${STANDBY_REGION}", STANDBY))


def _run_teardown(env, tmp_path):
    script = tmp_path / "teardown.sh"
    script.write_text(_teardown_script())
    # GitHub runs `run:` blocks with `bash -e`; mirror that so a failing command
    # inside a function is treated the way the real job would treat it.
    return subprocess.run(["bash", "-e", str(script)], cwd=DEPLOYMENT, env=_job_env(env),
                          capture_output=True, text=True, timeout=120)


def _stack(name, status="CREATE_COMPLETE"):
    return name + ENV_SUFFIX, {"status": status}


class TestTeardownSweep:

    def test_rendered_block_parses(self, tmp_path):
        script = tmp_path / "t.sh"
        script.write_text(_teardown_script())
        r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_leftover_small_stacks_are_swept_before_the_vpc_stacks(self, stub_env, tmp_path):
        env, state, log = stub_env
        # The exact residue of run 35783034631: databases already gone, destroy-all
        # bailed, the small stacks never attempted, baseInfra/baseVpc DELETE_FAILED
        # on the subnets the secrets-rotation Lambda's ENIs were pinning. Plus run
        # 35887370433's twist: each baseVpc's canary bucket holds ALB log objects
        # that landed after destroy-apps emptied it, and one more lands after the
        # guard's own empty.
        primary_bucket, standby_bucket = "basevpc-abc1234-canarybucket-p", "basevpc-abc1234-canarybucket-s"
        state.write_text(json.dumps({
            "stacks": {
                PRIMARY: dict([_stack("chaos"), _stack("secrets-rotation"), _stack("arc-dns-status"),
                               _stack("codebuild"), _stack("baseInfra", "DELETE_FAILED"),
                               ("baseVpc" + ENV_SUFFIX, {"status": "DELETE_FAILED", "failed": ["canaryBucket"],
                                                         "bucket": primary_bucket})]),
                STANDBY: dict([_stack("chaos"), _stack("arc-dns-status"),
                               ("baseVpc" + ENV_SUFFIX, {"status": "DELETE_FAILED", "failed": ["canaryBucket"],
                                                         "bucket": standby_bucket})]),
            },
            "buckets": {primary_bucket: {"objects": 3}, standby_bucket: {"objects": 3, "late_objects": 1}},
            # Lambda ENIs still draining: the SG delete fails the first time.
            "fail_first": ["secrets-rotation" + ENV_SUFFIX],
            "members": [],
            "global_gone": True,
        }))
        r = _run_teardown(env, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Teardown complete" in r.stdout, r.stdout
        final = json.loads(state.read_text())
        assert final["stacks"] == {PRIMARY: {}, STANDBY: {}} and final["buckets"] == {}

        deletes = [(c[c.index("--region") + 1], c[c.index("--stack-name") + 1])
                   for c in _calls(log) if c[:2] == ["cloudformation", "delete-stack"]]
        first_vpc = next(i for i, (_, n) in enumerate(deletes) if n.startswith("baseInfra"))
        swept_before_vpc = {n for _, n in deletes[:first_vpc]}
        for small in ("chaos", "secrets-rotation", "arc-dns-status", "codebuild"):
            assert small + ENV_SUFFIX in swept_before_vpc, f"{small} not swept before baseInfra"
        assert (STANDBY, "chaos" + ENV_SUFFIX) in deletes[:first_vpc]
        # The DELETE_FAILED stack was retried by the second pass.
        assert sum(1 for _, n in deletes if n == "secrets-rotation" + ENV_SUFFIX) == 2
        # baseVpc: standby before primary (peering lives on the standby side).
        vpc = [(reg, n) for reg, n in deletes if n.startswith("baseVpc")]
        assert vpc[0][0] == STANDBY and vpc[-1][0] == PRIMARY
        # Each baseVpc delete is immediately preceded by an empty of ITS bucket, and
        # the standby's late-landing log made the guard empty and retry it once.
        ops = [o for o in _bucket_ops(log) if o[0] == "empty" or o[1].startswith("baseVpc")]
        assert ops == [("empty", standby_bucket), ("delete", "baseVpc" + ENV_SUFFIX),
                       ("empty", standby_bucket), ("delete", "baseVpc" + ENV_SUFFIX),
                       ("empty", primary_bucket), ("delete", "baseVpc" + ENV_SUFFIX)]

    def test_vpc_stacks_are_left_alone_while_a_database_stack_remains(self, stub_env, tmp_path):
        env, state, log = stub_env
        # carts-db-stack survives every delete (never leaves DELETE_IN_PROGRESS ->
        # fail_first makes it DELETE_FAILED and it is not retried by the guard).
        state.write_text(json.dumps({
            "stacks": {
                PRIMARY: dict([_stack("carts-db-stack", "DELETE_FAILED"), _stack("chaos"),
                               _stack("baseInfra", "DELETE_FAILED"), _stack("baseVpc", "DELETE_FAILED")]),
                STANDBY: {},
            },
            "fail_first": ["carts-db-stack" + ENV_SUFFIX],
            "members": [],
            "global_gone": True,
        }))
        r = _run_teardown(env, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Database stacks still present, leaving baseInfra and baseVpc in place" in r.stdout
        assert "Teardown incomplete" in r.stdout
        deleted = {c[c.index("--stack-name") + 1] for c in _calls(log) if c[:2] == ["cloudformation", "delete-stack"]}
        assert "chaos" + ENV_SUFFIX in deleted            # the sweep still ran
        assert not any(n.startswith(("baseInfra", "baseVpc")) for n in deleted)


# ---------------------------------------------------------------------------
# delete-cloudmap-namespace.sh -- the helper both destroy-all and the guard use
# ---------------------------------------------------------------------------

NAMESPACE = "retail-store-ar" + ENV_SUFFIX


def _namespace(ns_id="ns-abc1234", services=0, **extra):
    return [dict(id=ns_id, name=NAMESPACE, services=services, **extra)]


def _run_delete_ns(env, region=PRIMARY):
    return subprocess.run([str(DELETE_NS), NAMESPACE, region], env=env,
                          capture_output=True, text=True, timeout=60)


def _sd_ops(log: Path, region=None):
    """Cloud Map operations in call order, optionally for one region."""
    return [c[1] for c in _calls(log)
            if c[0] == "servicediscovery" and (region is None or c[c.index("--region") + 1] == region)]


class TestDeleteCloudMapNamespace:

    def test_an_empty_namespace_is_deleted(self, stub_env):
        env, state, log = stub_env
        state.write_text(json.dumps({"namespaces": {PRIMARY: _namespace()}}))
        r = _run_delete_ns(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "deleted namespace ns-abc1234" in r.stdout
        assert _sd_ops(log) == ["list-namespaces", "list-services", "delete-namespace"]
        assert json.loads(state.read_text())["namespaces"][PRIMARY] == []

    def test_access_denied_is_a_failure_with_the_api_error_left_visible(self, stub_env):
        # The defect: the old loop swallowed this error and reported the namespace
        # as absent, so 61 leaked namespaces read as 61 clean teardowns.
        env, state, log = stub_env
        state.write_text(json.dumps({"namespaces": {PRIMARY: _namespace()}, "cloudmap_denied": [PRIMARY]}))
        r = _run_delete_ns(env)
        assert r.returncode != 0
        assert "AccessDeniedException" in r.stderr and "servicediscovery:ListNamespaces" in r.stderr
        assert "may be leaking" in r.stdout
        assert "no %s namespace found" % NAMESPACE not in r.stdout
        assert "delete-namespace" not in _sd_ops(log)
        assert json.loads(state.read_text())["namespaces"][PRIMARY] == _namespace()

    def test_an_absent_namespace_is_a_no_op(self, stub_env):
        env, state, log = stub_env
        state.write_text(json.dumps({"namespaces": {}}))
        r = _run_delete_ns(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "no %s namespace found" % NAMESPACE in r.stdout
        assert _sd_ops(log) == ["list-namespaces"]

    def test_a_namespace_that_still_has_services_is_reported_not_skipped(self, stub_env):
        env, state, log = stub_env
        state.write_text(json.dumps({"namespaces": {PRIMARY: _namespace(services=2)}}))
        r = _run_delete_ns(env)
        assert r.returncode != 0
        assert "still has 2 services" in r.stdout
        assert "delete-namespace" not in _sd_ops(log)

    def test_a_failed_delete_is_reported(self, stub_env):
        env, state, log = stub_env
        state.write_text(json.dumps({"namespaces": {PRIMARY: _namespace(delete_fails=True)}}))
        r = _run_delete_ns(env)
        assert r.returncode != 0
        assert "failed to delete namespace ns-abc1234" in r.stdout
        assert "InternalServiceError" in r.stderr

    def test_a_namespace_that_vanishes_before_the_delete_is_not_a_failure(self, stub_env):
        # Run 37783783626 (us-west-2): the lookup found the namespace and DeleteNamespace answered NamespaceNotFound,
        # because the delete destroy-all had started finished in between. There is nothing left to delete or to wait for.
        env, state, log = stub_env
        state.write_text(json.dumps({"namespaces": {PRIMARY: _namespace(vanishes=True)}}))
        r = _run_delete_ns(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "namespace ns-abc1234 (%s) was gone before it could be deleted" % NAMESPACE in r.stdout
        assert "NamespaceNotFound" in r.stderr                    # the API answer stays visible
        assert _sd_ops(log) == ["list-namespaces", "list-services", "delete-namespace"]
        assert json.loads(state.read_text())["namespaces"][PRIMARY] == []

    def test_a_delete_already_in_progress_waits_for_the_namespace_to_go(self, stub_env):
        # The defect from run 37401247653: destroy-all deleted the namespace, the
        # guard ran the helper again 33 s later while Cloud Map was still deleting
        # it, got DuplicateRequest, and reported a leak that was not there.
        env, state, log = stub_env
        env = dict(env, CLOUDMAP_DELETE_WAIT_INTERVAL="0")
        state.write_text(json.dumps({"namespaces": {PRIMARY: _namespace(deleting=2)}}))
        r = _run_delete_ns(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "already being deleted" in r.stdout and "gone now" in r.stdout
        assert "DuplicateRequest" in r.stderr                    # the API answer stays visible
        assert _sd_ops(log) == ["list-namespaces", "list-services", "delete-namespace",
                                "list-namespaces", "list-namespaces"]
        assert json.loads(state.read_text())["namespaces"][PRIMARY] == []

    def test_a_delete_in_progress_that_never_finishes_is_a_failure(self, stub_env):
        env, state, log = stub_env
        env = dict(env, CLOUDMAP_DELETE_WAIT_INTERVAL="0", CLOUDMAP_DELETE_WAIT_ATTEMPTS="3")
        state.write_text(json.dumps({"namespaces": {PRIMARY: _namespace(deleting=50)}}))
        r = _run_delete_ns(env)
        assert r.returncode != 0
        assert "still present" in r.stdout
        assert _sd_ops(log).count("list-namespaces") == 4      # the first lookup plus three checks
        assert json.loads(state.read_text())["namespaces"][PRIMARY] != []


@pytest.mark.skipif(REAL_MAKE is None, reason="make not installed")
class TestDestroyCloudMapNamespaceTarget:

    def _run_make(self, env):
        # The Makefile shells out to `aws sts get-caller-identity` while parsing;
        # the stub answers that with nothing, which is fine for this target.
        return subprocess.run([REAL_MAKE, "-C", str(DEPLOYMENT), "destroy-cloudmap-namespace", "ENV=" + ENV_SUFFIX],
                              env=env, capture_output=True, text=True, timeout=120)

    def test_both_regions_are_attempted_and_one_failure_fails_the_target(self, stub_env):
        env, state, log = stub_env
        state.write_text(json.dumps({
            "namespaces": {PRIMARY: _namespace("ns-primary"), STANDBY: _namespace("ns-standby")},
            "cloudmap_denied": [PRIMARY],
        }))
        r = self._run_make(env)
        assert r.returncode != 0, r.stdout + r.stderr
        assert "AccessDeniedException" in r.stderr
        # The standby namespace was still deleted: one failing region must not
        # stop the other from being cleaned up.
        assert _sd_ops(log, STANDBY) == ["list-namespaces", "list-services", "delete-namespace"]
        assert json.loads(state.read_text())["namespaces"][STANDBY] == []

    def test_nothing_to_delete_in_either_region_is_a_clean_exit(self, stub_env):
        env, state, log = stub_env
        state.write_text(json.dumps({"namespaces": {}}))
        r = self._run_make(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert _sd_ops(log, PRIMARY) == ["list-namespaces"] and _sd_ops(log, STANDBY) == ["list-namespaces"]


# ---------------------------------------------------------------------------
# The role grant and the namespace name, derived from the helper and ecs.yaml
# ---------------------------------------------------------------------------

class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags (!Sub, !Ref...)."""


def _cfn_tag(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_CfnLoader.add_multi_constructor("!", _cfn_tag)


def _load_template(path):
    # Drive the SafeLoader subclass directly rather than passing it to yaml.load
    # as the Loader argument: same parse, no yaml.load call (bandit B506 accepts
    # only the literal SafeLoader name there).
    loader = _CfnLoader(path.read_text())
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _helper_api_calls():
    ops = set(re.findall(r"aws servicediscovery ([a-z-]+)", DELETE_NS.read_text()))
    assert ops, "delete-cloudmap-namespace.sh makes no servicediscovery calls?"
    return {"servicediscovery:" + "".join(p.title() for p in op.split("-")) for op in ops}


def _role_allows_everywhere():
    """Actions the e2e role's managed policies allow on Resource '*'."""
    template = _load_template(OIDC_ROLE)
    allowed = set()
    for res in template["Resources"].values():
        if res["Type"] != "AWS::IAM::ManagedPolicy":
            continue
        for st in res["Properties"]["PolicyDocument"]["Statement"]:
            resource = st.get("Resource")
            if st.get("Effect") != "Allow" or "*" not in (resource if isinstance(resource, list) else [resource]):
                continue
            actions = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
            allowed.update(actions)
    return allowed


class TestCloudMapRoleAndNaming:

    def test_the_e2e_role_grants_every_call_the_helper_makes(self):
        # Derived from the script, so a new call added there without a grant
        # fails here rather than in the next teardown.
        needed = _helper_api_calls()
        assert needed == {"servicediscovery:ListNamespaces", "servicediscovery:ListServices",
                          "servicediscovery:DeleteNamespace"}
        allowed = _role_allows_everywhere()
        missing = {a for a in needed if a not in allowed and "servicediscovery:*" not in allowed}
        assert not missing, f"github-oidc-role.yaml does not grant {sorted(missing)}"

    def test_the_helper_makes_no_call_beyond_the_three_it_is_granted(self):
        # The grant is deliberately narrow; widen both together or neither.
        assert _helper_api_calls() <= {"servicediscovery:ListNamespaces", "servicediscovery:ListServices",
                                       "servicediscovery:DeleteNamespace"}

    def test_the_makefile_and_the_guard_name_the_namespace_service_connect_creates(self):
        cluster = next(r for r in _load_template(ECS_TEMPLATE)["Resources"].values()
                       if r["Type"] == "AWS::ECS::Cluster")
        assert cluster["Properties"]["ServiceConnectDefaults"]["Namespace"] == "retail-store-ar${Env}"
        makefile = MAKEFILE.read_text()
        assert "./delete-cloudmap-namespace.sh retail-store-ar${ENV}" in makefile
        assert "2>/dev/null" not in makefile.split("destroy-cloudmap-namespace:")[1].split("\n\n")[0], \
            "the Cloud Map target must not hide its API errors again"
        assert "./delete-cloudmap-namespace.sh %s" % NAMESPACE in _expanded(_teardown_script())


# ---------------------------------------------------------------------------
# The guard: the namespace goes after the stacks, and a leftover is incomplete
# ---------------------------------------------------------------------------

class TestTeardownGuardCloudMap:

    def test_the_namespace_is_deleted_after_the_stacks_and_the_teardown_is_complete(self, stub_env, tmp_path):
        env, state, log = stub_env
        state.write_text(json.dumps({
            "stacks": {PRIMARY: dict([_stack("chaos")]), STANDBY: {}},
            "members": [], "global_gone": True,
            "namespaces": {PRIMARY: _namespace("ns-primary"), STANDBY: _namespace("ns-standby")},
        }))
        r = _run_teardown(env, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Teardown complete" in r.stdout, r.stdout
        calls = _calls(log)
        last_stack_delete = max(i for i, c in enumerate(calls) if c[:2] == ["cloudformation", "delete-stack"])
        ns_deletes = [i for i, c in enumerate(calls) if c[:2] == ["servicediscovery", "delete-namespace"]]
        assert len(ns_deletes) == 2 and min(ns_deletes) > last_stack_delete
        final = json.loads(state.read_text())
        assert final["namespaces"] == {PRIMARY: [], STANDBY: []}

    def test_namespaces_destroy_all_is_already_deleting_leave_the_teardown_complete(self, stub_env, tmp_path):
        # Run 37401247653's sequence: destroy-all reached destroy-cloudmap-namespace
        # and Cloud Map was still deleting both namespaces when the guard ran.
        env, state, log = stub_env
        env = dict(env, CLOUDMAP_DELETE_WAIT_INTERVAL="0")
        state.write_text(json.dumps({
            "stacks": {PRIMARY: {}, STANDBY: {}},
            "members": [], "global_gone": True,
            "namespaces": {PRIMARY: _namespace("ns-primary", deleting=1),
                           STANDBY: _namespace("ns-standby", deleting=1)},
        }))
        r = _run_teardown(env, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Teardown complete" in r.stdout, r.stdout
        assert "Teardown incomplete" not in r.stdout
        final = json.loads(state.read_text())
        assert final["namespaces"] == {PRIMARY: [], STANDBY: []}

    def test_a_namespace_that_vanishes_before_the_delete_leaves_the_teardown_complete(self, stub_env, tmp_path):
        # Run 37783783626, us-west-2: the lookup returned the namespace and DeleteNamespace then answered
        # NamespaceNotFound, because the delete destroy-all had started finished in between. The sweep found no
        # namespace afterwards, but the guard warned "Teardown incomplete".
        env, state, log = stub_env
        state.write_text(json.dumps({
            "stacks": {PRIMARY: {}, STANDBY: {}},
            "members": [], "global_gone": True,
            "namespaces": {PRIMARY: _namespace("ns-primary"), STANDBY: _namespace("ns-standby", vanishes=True)},
        }))
        r = _run_teardown(env, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Teardown complete" in r.stdout and "Teardown incomplete" not in r.stdout, r.stdout
        assert "NamespaceNotFound" in r.stderr, "the API answer stays visible"
        assert json.loads(state.read_text())["namespaces"] == {PRIMARY: [], STANDBY: []}

    def test_a_namespace_the_role_cannot_delete_makes_the_teardown_incomplete(self, stub_env, tmp_path):
        env, state, log = stub_env
        state.write_text(json.dumps({
            "stacks": {PRIMARY: {}, STANDBY: {}},
            "members": [], "global_gone": True,
            "namespaces": {PRIMARY: _namespace("ns-primary"), STANDBY: _namespace("ns-standby")},
            "cloudmap_denied": [PRIMARY],
        }))
        r = _run_teardown(env, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr          # the guard reports, it does not abort
        assert "Teardown incomplete" in r.stdout, r.stdout
        assert "Cloud Map namespaces still present: %s/%s" % (PRIMARY, NAMESPACE) in r.stdout
        assert "AccessDeniedException" in r.stderr              # the cause is in the log, not swallowed
        final = json.loads(state.read_text())
        assert final["namespaces"][PRIMARY] == _namespace("ns-primary") and final["namespaces"][STANDBY] == []


# ---------------------------------------------------------------------------
# delete-run-log-groups.sh, and the step that runs it after a passing run
# ---------------------------------------------------------------------------

DELETE_LOG_GROUPS = DEPLOYMENT / "delete-run-log-groups.sh"
LOG_GROUP_STEP = "Delete log groups of this run"

# What a run leaves behind, as seen in the e2e account after run 37485943263 (the sha and the
# random parts replaced).
RUN_LOG_GROUPS = [
    "/aws/codebuild/mr-app-docker-build-abc1234",
    "/aws/ecs/containerinsights/apps-abc1234-EcsCluster-EOkrwQEtbODb/performance",
    "/aws/lambda/app-dns-status-abc1234",
    "/aws/lambda/catalog-db-stack-abc1234-UpdateSecretFunction-SH45SbX6dagH",
    "/aws/lambda/cwsyn-global-cart-abc1234-dfa5cfb8-2214-486d-b09a-af94de038317",
    "/aws/rds/cluster/catalog-dbcluster-01-us-east-1-abc1234/error",
    "/aws/service-events/carts-abc1234",
]
# Not this run's: another run's, no suffix at all, a -dev environment's, an unrelated service's.
OTHER_LOG_GROUPS = [
    "/aws/lambda/app-dns-status-def5678",
    "/aws/lambda/app-dns-status",
    "/aws/ecs/containerinsights/apps-dev-EcsCluster-X/performance",
    "/aws/apigateway/welcome",
]
# The service's substring pattern returns these two for -abc1234, but they are not this run's
# to delete: the suffix is only the start of a longer token, or no deletable prefix leads the name.
LOOKALIKES = [
    "/aws/lambda/app-dns-status-abc12345",
    "/custom/thing-abc1234",
]

CANARIES_TEMPLATE = DEPLOYMENT / "canaries.yaml"
# Lambda allows 64 characters in a function name. Synthetics builds a canary's as
# cwsyn-<canary name cut to 21 characters>-<uuid>, so 64 - 6 - 1 - 36 = 21 characters of
# "<scope>-<page>${Env}" survive, and for a long name that is part of the commit sha.
CANARY_NAME_KEPT = 21
CANARY_UUIDS = ["%08x-0000-4000-8000-%012x" % (n, n) for n in range(1, 40)]


def _canary_names():
    """The canary names canaries.yaml declares, without their Env suffix."""
    names = []
    for res in _load_template(CANARIES_TEMPLATE)["Resources"].values():
        if res["Type"] != "AWS::Synthetics::Canary":
            continue
        name = res["Properties"]["Name"]
        assert name.endswith("${Env}"), f"{name}: canary names carry the Env suffix"
        names.append(name[: -len("${Env}")])
    assert names, "canaries.yaml declares no canaries?"
    return names


def _canary_group(base, suffix=ENV_SUFFIX, n=1):
    """The log group of a canary's function, as Synthetics names it."""
    return "/aws/lambda/cwsyn-%s-%s" % ((base + suffix)[:CANARY_NAME_KEPT], CANARY_UUIDS[n])


# The five of the twelve names Synthetics cut for the run -398824f, as the e2e account listed them
# after run 37533695833 (the uuids here are made up).
CUT_CANARY_GROUPS_SEEN = [
    "/aws/lambda/cwsyn-lcl-rgnl-catalog-3988-" + CANARY_UUIDS[1],
    "/aws/lambda/cwsyn-lcl-rgnl-orders-39882-" + CANARY_UUIDS[2],
    "/aws/lambda/cwsyn-rmt-rgnl-catalog-3988-" + CANARY_UUIDS[3],
    "/aws/lambda/cwsyn-rmt-rgnl-orders-39882-" + CANARY_UUIDS[4],
    "/aws/lambda/cwsyn-global-catalog-398824-" + CANARY_UUIDS[5],
]

# Amazon MQ names a RabbitMQ broker's three log groups by the broker's id.
BROKER_RUN = "b-3d43b3fe-0000-4000-8000-000000000001"
BROKER_RUN_STANDBY = "b-3d43b3fe-0000-4000-8000-000000000002"
BROKER_OTHER = "b-9c1d4a77-0000-4000-8000-000000000003"
BROKER_LOGS = ("general", "federation", "connection")


def _broker_groups(broker_id):
    return ["/aws/amazonmq/broker/%s/%s" % (broker_id, log) for log in BROKER_LOGS]


def _log_group_state(groups, standby=None, **extra):
    return json.dumps({"log_groups": {PRIMARY: list(groups), STANDBY: list(groups if standby is None else standby)},
                       **extra})


def _run_delete_log_groups(env, suffix=ENV_SUFFIX, region=PRIMARY, broker_file=None):
    cmd = [str(DELETE_LOG_GROUPS), suffix, region] + ([str(broker_file)] if broker_file is not None else [])
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)


def _broker_file(tmp_path, text, name="brokers.txt"):
    path = tmp_path / name
    path.write_text(text)
    return path


def _log_ops(log: Path):
    return [c[1] for c in _calls(log) if c[0] == "logs"]


def _describe_calls(log: Path):
    return [c for c in _calls(log) if c[:2] == ["logs", "describe-log-groups"]]


def _option(call, name):
    """The value a recorded call gives an option, as `--name=value` or `--name value`; None if it has none."""
    for i, arg in enumerate(call):
        if arg.startswith(name + "="):
            return arg[len(name) + 1:]
        if arg == name:
            return call[i + 1]
    return None


CLI_NEGATIVE_NUMBER = re.compile(r"^-\d+$|^-\d*\.\d+$")     # the one kind of dash value argparse takes as a value


def _dash_values_given_separately(call):
    """The `--option value` pairs of a recorded call whose value starts with a dash. The real CLI reads such a
    value as the next option (unless it is a negative number), so these calls fail with exit 252."""
    return [(before, word) for before, word in zip(call, call[1:])
            if before.startswith("--") and "=" not in before
            and word.startswith("-") and not word.startswith("--") and not CLI_NEGATIVE_NUMBER.match(word)]


def _groups_left(state: Path):
    return json.loads(state.read_text())["log_groups"]


class TestStubAwsReadsOptionsLikeTheRealCli:
    """The log group tests are worth only as much as the stub is faithful, and the stub once took what the
    CLI refuses. Each case here was run against aws-cli 2.36.47 and gave the result asserted (exit 252 and
    "expected one argument" for a parse failure; for the others the call got as far as the endpoint)."""

    def _aws(self, env, *args):
        return subprocess.run(["aws", "logs", "describe-log-groups", "--region", PRIMARY, *args],
                              env=env, capture_output=True, text=True, timeout=30)

    @pytest.mark.parametrize("value", ["-a1802e5", "-3c7091f", "-12e4567"])
    def test_a_separate_value_that_starts_with_a_dash_is_refused(self, stub_env, value):
        env, state, log = stub_env
        state.write_text(_log_group_state([]))
        r = self._aws(env, "--log-group-name-pattern", value)
        assert r.returncode == 252
        assert "argument --log-group-name-pattern: expected one argument" in r.stderr

    def test_an_option_with_no_value_after_it_is_refused(self, stub_env):
        env, state, log = stub_env
        state.write_text(_log_group_state([]))
        r = self._aws(env, "--log-group-name-pattern")
        assert r.returncode == 252 and "expected one argument" in r.stderr

    @pytest.mark.parametrize("words", [["--log-group-name-pattern=-a1802e5"],
                                       ["--log-group-name-pattern", "-1234567"]])
    def test_the_equals_form_and_a_negative_number_get_through(self, stub_env, words):
        env, state, log = stub_env
        state.write_text(_log_group_state([]))
        r = self._aws(env, *words)
        assert r.returncode == 0, r.stderr


class TestDeleteRunLogGroups:

    def test_only_this_runs_groups_are_deleted(self, stub_env):
        env, state, log = stub_env
        state.write_text(_log_group_state(RUN_LOG_GROUPS + OTHER_LOG_GROUPS + LOOKALIKES))
        r = _run_delete_log_groups(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "deleted 7 of 7 log groups of -abc1234" in r.stdout
        left = _groups_left(state)
        assert left[PRIMARY] == OTHER_LOG_GROUPS + LOOKALIKES
        assert left[STANDBY] == RUN_LOG_GROUPS + OTHER_LOG_GROUPS + LOOKALIKES   # the other region is not touched
        for name in LOOKALIKES:
            assert "left alone, outside the prefixes this helper deletes under: " + name in r.stdout
        assert "def5678" not in r.stdout                   # another run's groups are never even returned

    def test_the_service_filters_every_listing_so_the_account_is_never_listed(self, stub_env):
        env, state, log = stub_env
        state.write_text(_log_group_state(RUN_LOG_GROUPS))
        _run_delete_log_groups(env)
        describes = _describe_calls(log)
        patterns = [c for c in describes if _option(c, "--log-group-name-pattern") is not None]
        prefixes = [_option(c, "--log-group-name-prefix") for c in describes
                    if _option(c, "--log-group-name-prefix") is not None]
        # One substring listing for the suffix, then one prefix listing per canary; the service
        # rejects a request that carries both filters, so no call does.
        assert len(patterns) == 1 and _option(patterns[0], "--log-group-name-pattern") == ENV_SUFFIX
        assert len(describes) == len(patterns) + len(prefixes)
        assert _option(patterns[0], "--log-group-name-prefix") is None
        assert all(p.startswith("/aws/lambda/cwsyn-") and len(p) > len("/aws/lambda/cwsyn-") for p in prefixes)

    def test_a_value_that_starts_with_a_dash_is_sent_joined_to_its_option(self, stub_env):
        # The suffix is "-<sha>". The real CLI reads `--log-group-name-pattern -a1802e5` as an option
        # followed by another option and refuses it ("expected one argument", exit 252). Run 37679210579
        # met that: the listing failed in both Regions, the step only warned, and the run's log groups
        # stayed. No test caught it because the stub took the separate form, so the form is pinned on
        # the wire as well: no logs call gives a value that starts with a dash as a separate word.
        env, state, log = stub_env
        state.write_text(_log_group_state(RUN_LOG_GROUPS))
        _run_delete_log_groups(env)
        calls = [c for c in _calls(log) if c[0] == "logs"]
        assert calls
        for call in calls:
            assert _dash_values_given_separately(call) == [], " ".join(call)
        assert "--log-group-name-pattern=" + ENV_SUFFIX in [word for c in _describe_calls(log) for word in c]

    @pytest.mark.parametrize("suffix", ["-a1802e5", "-3c7091f", "-398824f", "-1234567",
                                        "-0123456789abcdef0123456789abcdef01234567"])
    def test_every_sha_shaped_suffix_is_listed_and_deleted(self, stub_env, suffix):
        # Letter first, digit first, digits only (the CLI takes that for a negative number, so it
        # passed in either form) and the 40-character form.
        env, state, log = stub_env
        groups = ["/aws/codebuild/mr-app-docker-build" + suffix,
                  "/aws/service-events/carts" + suffix,
                  "/aws/rds/cluster/catalog-dbcluster-01-us-east-1" + suffix + "/error"]
        groups += [_canary_group(name, suffix, n=i + 1) for i, name in enumerate(_canary_names())]
        state.write_text(_log_group_state(groups + OTHER_LOG_GROUPS))
        r = _run_delete_log_groups(env, suffix=suffix)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "deleted %d of %d log groups of %s" % (len(groups), len(groups), suffix) in r.stdout
        assert _groups_left(state)[PRIMARY] == OTHER_LOG_GROUPS

    def test_each_canary_is_listed_under_the_exact_name_synthetics_gives_its_function(self, stub_env):
        env, state, log = stub_env
        state.write_text(_log_group_state(RUN_LOG_GROUPS))
        _run_delete_log_groups(env)
        prefixes = sorted(_option(c, "--log-group-name-prefix") for c in _describe_calls(log)
                          if _option(c, "--log-group-name-prefix") is not None)
        assert prefixes == sorted("/aws/lambda/cwsyn-%s-" % (name + ENV_SUFFIX)[:CANARY_NAME_KEPT]
                                  for name in _canary_names())
        assert any(ENV_SUFFIX not in p for p in prefixes), "no canary name is cut; the premise of this rule is gone"

    def test_a_canary_group_is_deleted_whether_or_not_synthetics_cut_the_suffix_out_of_its_name(self, stub_env):
        env, state, log = stub_env
        canary_groups = [_canary_group(name, n=i + 1) for i, name in enumerate(_canary_names())]
        cut = [g for g in canary_groups if ENV_SUFFIX not in g]
        assert cut and len(cut) < len(canary_groups)        # a mix, so both paths are exercised
        state.write_text(_log_group_state(canary_groups + OTHER_LOG_GROUPS))
        r = _run_delete_log_groups(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "deleted %d of %d log groups of -abc1234" % (len(canary_groups), len(canary_groups)) in r.stdout
        assert _groups_left(state)[PRIMARY] == OTHER_LOG_GROUPS

    def test_the_cut_names_seen_in_the_e2e_account_are_deleted(self, stub_env):
        # Run 37533695833 (-398824f) left exactly these five of its twelve canary groups behind.
        env, state, log = stub_env
        whole = "/aws/lambda/cwsyn-lcl-rgnl-home-398824f-" + CANARY_UUIDS[6]
        state.write_text(_log_group_state(CUT_CANARY_GROUPS_SEEN + [whole] + OTHER_LOG_GROUPS))
        r = _run_delete_log_groups(env, suffix="-398824f")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "deleted 6 of 6 log groups of -398824f" in r.stdout
        assert _groups_left(state)[PRIMARY] == OTHER_LOG_GROUPS

    def test_a_canary_group_that_is_not_this_runs_is_left_alone(self, stub_env):
        env, state, log = stub_env
        mine = _canary_group("lcl-rgnl-catalog")
        prefix = mine[: -len(CANARY_UUIDS[1])]
        others = [
            _canary_group("lcl-rgnl-catalog", suffix="-abd1234", n=2),    # another run, differing within the kept part
            prefix + "not-a-uuid",
            prefix + CANARY_UUIDS[3] + "-extra",
            prefix + CANARY_UUIDS[9].upper(),                              # the uuid has letters in it, unlike n=3
        ]
        state.write_text(_log_group_state([mine] + others))
        r = _run_delete_log_groups(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "deleted 1 of 1 log groups of -abc1234" in r.stdout
        assert _groups_left(state)[PRIMARY] == others
        for name in others[1:]:
            assert "left alone, outside the prefixes this helper deletes under: " + name in r.stdout

    def test_a_name_one_rule_refuses_and_another_takes_is_deleted_and_not_reported_as_left_alone(self, stub_env):
        # The suffix stands as a whole token in it, so the first rule takes it; it has no uuid, so the
        # canary rule refuses it when it lists the canary's prefix.
        name = "/aws/lambda/cwsyn-global-home-abc1234-not-a-uuid"
        env, state, log = stub_env
        state.write_text(_log_group_state([name]))
        r = _run_delete_log_groups(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "deleted 1 of 1 log groups of -abc1234" in r.stdout
        assert "left alone" not in r.stdout
        assert _groups_left(state)[PRIMARY] == []

    @pytest.mark.parametrize("content", [
        "{a}\n{b}\n", "{a} {b}", "\n\n{a}\n  {b}  \n\n", "{a}\n{b}",
    ])
    def test_the_message_broker_groups_of_the_recorded_brokers_are_deleted(self, stub_env, tmp_path, content):
        env, state, log = stub_env
        recorded = [BROKER_RUN, BROKER_RUN_STANDBY]
        state.write_text(_log_group_state(
            RUN_LOG_GROUPS + [g for b in recorded + [BROKER_OTHER] for g in _broker_groups(b)] + OTHER_LOG_GROUPS))
        brokers = _broker_file(tmp_path, content.format(a=recorded[0], b=recorded[1]))
        r = _run_delete_log_groups(env, broker_file=brokers)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "deleted 13 of 13 log groups of -abc1234" in r.stdout            # 7 by the suffix, 3 per broker
        assert _groups_left(state)[PRIMARY] == _broker_groups(BROKER_OTHER) + OTHER_LOG_GROUPS

    def test_without_a_broker_file_every_message_broker_group_is_left_alone(self, stub_env):
        env, state, log = stub_env
        state.write_text(_log_group_state(RUN_LOG_GROUPS + _broker_groups(BROKER_RUN)))
        r = _run_delete_log_groups(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "deleted 7 of 7" in r.stdout
        assert _groups_left(state)[PRIMARY] == _broker_groups(BROKER_RUN)
        assert not any("amazonmq" in " ".join(c) for c in _calls(log))

    def test_an_empty_broker_file_is_a_clean_run_with_a_note(self, stub_env, tmp_path):
        env, state, log = stub_env
        state.write_text(_log_group_state(RUN_LOG_GROUPS + _broker_groups(BROKER_OTHER)))
        r = _run_delete_log_groups(env, broker_file=_broker_file(tmp_path, "\n"))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "no Amazon MQ brokers were recorded for -abc1234" in r.stdout
        assert _groups_left(state)[PRIMARY] == _broker_groups(BROKER_OTHER)

    @pytest.mark.parametrize("bad", [
        "*", "None", "b-123", "b-3d43b3fe", BROKER_RUN + "/general", "/" + BROKER_RUN, BROKER_RUN.upper(),
        "x" + BROKER_RUN, BROKER_RUN + "0", "An error occurred (AccessDenied) when calling the ListBrokers operation",
    ])
    def test_a_broker_file_holding_anything_but_broker_ids_is_refused_before_any_call(self, stub_env, tmp_path, bad):
        env, state, log = stub_env
        state.write_text(_log_group_state(RUN_LOG_GROUPS + _broker_groups(BROKER_RUN)))
        r = _run_delete_log_groups(env, broker_file=_broker_file(tmp_path, BROKER_RUN + "\n" + bad + "\n"))
        assert r.returncode == 2
        assert "is not an Amazon MQ broker id" in r.stderr
        assert _calls(log) == []
        assert _groups_left(state)[PRIMARY] == RUN_LOG_GROUPS + _broker_groups(BROKER_RUN)

    def test_a_missing_broker_file_is_a_failure_and_the_rest_is_still_deleted(self, stub_env, tmp_path):
        env, state, log = stub_env
        state.write_text(_log_group_state(RUN_LOG_GROUPS + _broker_groups(BROKER_RUN)))
        r = _run_delete_log_groups(env, broker_file=tmp_path / "never-written.txt")
        assert r.returncode != 0
        assert "never-written.txt does not exist" in r.stdout and "may be leaking" in r.stdout
        assert "deleted 7 of 7 log groups" in r.stdout
        assert _groups_left(state)[PRIMARY] == _broker_groups(BROKER_RUN)

    def test_only_a_recorded_brokers_log_groups_are_taken_under_its_prefix(self, stub_env, tmp_path):
        env, state, log = stub_env
        nested = "/aws/amazonmq/broker/%s/nested/log" % BROKER_RUN
        state.write_text(_log_group_state(_broker_groups(BROKER_RUN) + [nested]))
        r = _run_delete_log_groups(env, broker_file=_broker_file(tmp_path, BROKER_RUN))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "deleted 3 of 3 log groups" in r.stdout
        assert "left alone, outside the prefixes this helper deletes under: " + nested in r.stdout
        assert _groups_left(state)[PRIMARY] == [nested]

    def test_a_listing_that_fails_part_way_stops_listing_but_deletes_what_was_found(self, stub_env, tmp_path):
        env, state, log = stub_env
        state.write_text(_log_group_state(RUN_LOG_GROUPS + _broker_groups(BROKER_RUN),
                                          describe_fails_for_prefix=["/aws/lambda/cwsyn-lcl-rgnl-orders"]))
        r = _run_delete_log_groups(env, broker_file=_broker_file(tmp_path, BROKER_RUN))
        assert r.returncode != 0
        assert "ThrottlingException" in r.stderr                       # the cause is in the log
        assert "could not list the log groups of canary lcl-rgnl-orders" in r.stdout and "may be leaking" in r.stdout
        assert "deleted 7 of 7 log groups" in r.stdout                  # the suffix listing had already found these
        prefixes = [_option(c, "--log-group-name-prefix") for c in _describe_calls(log)
                    if _option(c, "--log-group-name-prefix") is not None]
        assert prefixes[-1].startswith("/aws/lambda/cwsyn-lcl-rgnl-orders")     # nothing was listed after it
        assert _groups_left(state)[PRIMARY] == _broker_groups(BROKER_RUN)

    def test_the_helper_knows_exactly_the_canaries_the_template_declares(self):
        # A canary added or renamed in canaries.yaml without this list changing would leave its
        # groups behind again, one run after another.
        block = re.search(r"CANARY_NAMES=\(([^)]*)\)", DELETE_LOG_GROUPS.read_text())
        assert block, "delete-run-log-groups.sh no longer lists the canaries"
        assert sorted(block.group(1).split()) == sorted(_canary_names())

    def test_every_canary_name_keeps_enough_of_the_suffix_to_tell_one_run_from_another(self):
        # The cut name is "<canary name>-<sha>" cut to 21 characters. Below the dash and three hex
        # digits, a prefix would also match the groups of a great many other runs.
        for name in _canary_names():
            assert CANARY_NAME_KEPT - len(name) >= 4, (
                f"canary '{name}' is {len(name)} characters, which leaves {CANARY_NAME_KEPT - len(name)} of the "
                f"suffix in its function name; shorten it or identify its groups another way")

    @pytest.mark.parametrize("bad", ["-dev", "abc1234", "-ABC1234", "-abc123", "-abc1234x", "-abc1234/", "-", "-*", ""])
    def test_anything_but_a_commit_sha_suffix_is_refused_before_any_call(self, stub_env, bad):
        env, state, log = stub_env
        state.write_text(_log_group_state(RUN_LOG_GROUPS))
        r = _run_delete_log_groups(env, suffix=bad)
        assert r.returncode != 0
        assert _calls(log) == []
        assert _groups_left(state)[PRIMARY] == RUN_LOG_GROUPS

    def test_access_denied_on_the_listing_is_a_failure_with_the_api_error_left_visible(self, stub_env):
        env, state, log = stub_env
        state.write_text(_log_group_state(RUN_LOG_GROUPS, logs_denied=[PRIMARY]))
        r = _run_delete_log_groups(env)
        assert r.returncode != 0
        assert "AccessDeniedException" in r.stderr and "logs:DescribeLogGroups" in r.stderr
        assert "may be leaking" in r.stdout
        assert "no log groups" not in r.stdout
        assert "delete-log-group" not in _log_ops(log)
        assert _groups_left(state)[PRIMARY] == RUN_LOG_GROUPS

    def test_a_group_that_cannot_be_deleted_is_reported_and_the_others_still_go(self, stub_env):
        env, state, log = stub_env
        stuck = RUN_LOG_GROUPS[3]
        state.write_text(_log_group_state(RUN_LOG_GROUPS, delete_denied=[stuck]))
        r = _run_delete_log_groups(env)
        assert r.returncode != 0
        assert "failed to delete log group " + stuck in r.stdout
        assert "AccessDeniedException" in r.stderr and "logs:DeleteLogGroup" in r.stderr
        assert "deleted 6 of 7 log groups" in r.stdout
        assert _groups_left(state)[PRIMARY] == [stuck]

    def test_a_group_gone_before_its_delete_is_not_a_failure(self, stub_env):
        env, state, log = stub_env
        ghost = "/aws/lambda/cwsyn-rmt-rgnl-home-abc1234-5e329f43-be83-4863-a979-18390aaa8977"
        state.write_text(_log_group_state(RUN_LOG_GROUPS, ghosts=[ghost]))
        r = _run_delete_log_groups(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert ghost + " was already gone" in r.stdout
        assert "deleted 7 of 8 log groups" in r.stdout

    @pytest.mark.parametrize("empty_as_none", [False, True])
    def test_nothing_to_delete_is_a_clean_exit(self, stub_env, empty_as_none):
        # The CLI's text output for an empty result has been seen as nothing and as "None".
        env, state, log = stub_env
        state.write_text(_log_group_state(OTHER_LOG_GROUPS, empty_as_none=empty_as_none))
        r = _run_delete_log_groups(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "no log groups of -abc1234 to delete" in r.stdout
        assert set(_log_ops(log)) == {"describe-log-groups"}            # listed, nothing deleted

    def test_every_page_of_names_is_read(self, stub_env):
        env, state, log = stub_env
        many = ["/aws/lambda/fn%02d-abc1234" % i for i in range(11)]       # three to a page, the last page two
        state.write_text(_log_group_state(many, page_size=3))
        r = _run_delete_log_groups(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "deleted 11 of 11 log groups" in r.stdout
        assert _groups_left(state)[PRIMARY] == []


def _log_helper_api_calls():
    ops = set(re.findall(r"aws logs ([a-z-]+)", DELETE_LOG_GROUPS.read_text()))
    assert ops, "delete-run-log-groups.sh makes no logs calls?"
    return {"logs:" + "".join(p.title() for p in op.split("-")) for op in ops}


def _e2e_steps():
    wf = yaml.safe_load(E2E_WORKFLOW.read_text())
    return next(iter(wf["jobs"].values()))["steps"]


def _log_group_step():
    return next(s for s in _e2e_steps() if s.get("name") == LOG_GROUP_STEP)


def _log_group_script() -> str:
    run = _log_group_step()["run"]
    assert "${{" not in run, "a GitHub expression in the log group run block: use the job's environment variables"
    return run


def _broker_list_path(env, region):
    """Where the record step leaves a Region's broker ids for the log group step."""
    return Path(env["RUNNER_TEMP"]) / f"mq-brokers-{region}.txt"


def _run_log_group_step(env, tmp_path, recorded=(PRIMARY, STANDBY)):
    """The step as GitHub runs it, after the record step left an (empty) list in each of `recorded`."""
    for region in recorded:
        _broker_list_path(env, region).write_text("")
    script = tmp_path / "log-groups.sh"
    script.write_text(_log_group_script())
    return subprocess.run(["bash", "-e", str(script)], cwd=DEPLOYMENT, env=_job_env(env),
                          capture_output=True, text=True, timeout=120)


RECORD_STEP = "Record the message brokers of this run"


def _record_step():
    return next(s for s in _e2e_steps() if s.get("name") == RECORD_STEP)


def _record_script() -> str:
    run = _record_step()["run"]
    assert "${{" not in run, "a GitHub expression in the record step: use the job's environment variables"
    return run


def _run_record_step(env, tmp_path):
    script = tmp_path / "record.sh"
    script.write_text(_record_script())
    return subprocess.run(["bash", "-e", str(script)], env=_job_env(env),
                          capture_output=True, text=True, timeout=120)


def _brokers_state(standby_name="retail-store-ar-ordersmq" + ENV_SUFFIX, **extra):
    """Each Region has this run's broker; the primary has another run's as well."""
    return {"brokers": {
        PRIMARY: [{"name": "retail-store-ar-ordersmq" + ENV_SUFFIX, "id": BROKER_RUN},
                  {"name": "retail-store-ar-ordersmq-def5678", "id": BROKER_OTHER}],
        STANDBY: [{"name": standby_name, "id": BROKER_RUN_STANDBY}],
    }, **extra}


class TestLogGroupStep:

    def test_the_e2e_role_grants_every_call_the_helper_makes(self):
        # Derived from the script, so a call added there without a grant fails here
        # rather than in the next teardown.
        needed = _log_helper_api_calls()
        assert needed == {"logs:DescribeLogGroups", "logs:DeleteLogGroup"}
        allowed = _role_allows_everywhere()
        missing = {a for a in needed if a not in allowed and "logs:*" not in allowed}
        assert not missing, f"github-oidc-role.yaml does not grant {sorted(missing)}"

    def test_it_runs_right_after_the_teardown_and_only_when_the_run_has_passed(self):
        names = [s.get("name") for s in _e2e_steps()]
        assert names.index(LOG_GROUP_STEP) == names.index("Teardown") + 1
        step = _log_group_step()
        # The Teardown step is `always()`: a failed run still tears down, but it keeps its
        # logs for the post-mortem, which is all that is left of it once the stacks are gone.
        assert step["if"] == "success()"
        assert step["working-directory"] == "deployment"

    def test_the_rendered_step_parses(self, tmp_path):
        script = tmp_path / "t.sh"
        script.write_text(_log_group_script())
        r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_it_deletes_the_runs_groups_in_both_regions(self, stub_env, tmp_path):
        env, state, log = stub_env
        state.write_text(_log_group_state(RUN_LOG_GROUPS + OTHER_LOG_GROUPS + LOOKALIKES))
        r = _run_log_group_step(env, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert r.stdout.count("deleted 7 of 7 log groups of -abc1234") == 2
        assert "::warning::" not in r.stdout
        left = _groups_left(state)
        assert left == {PRIMARY: OTHER_LOG_GROUPS + LOOKALIKES, STANDBY: OTHER_LOG_GROUPS + LOOKALIKES}

    def test_a_region_it_cannot_clean_is_a_warning_and_the_other_region_still_is(self, stub_env, tmp_path):
        env, state, log = stub_env
        state.write_text(_log_group_state(RUN_LOG_GROUPS, logs_denied=[PRIMARY]))
        r = _run_log_group_step(env, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr          # the teardown's verdict is not this step's
        assert "::warning::Log groups of this run could not all be deleted in: %s" % PRIMARY in r.stdout
        assert "AccessDeniedException" in r.stderr              # the cause is in the log, not swallowed
        left = _groups_left(state)
        assert left[PRIMARY] == RUN_LOG_GROUPS and left[STANDBY] == []

    def test_it_hands_each_region_the_broker_list_the_record_step_wrote_for_it(self, stub_env, tmp_path):
        env, state, log = stub_env
        state.write_text(_log_group_state(_broker_groups(BROKER_RUN) + _broker_groups(BROKER_OTHER),
                                          standby=_broker_groups(BROKER_RUN_STANDBY) + _broker_groups(BROKER_OTHER)))
        _broker_list_path(env, PRIMARY).write_text(BROKER_RUN + "\n")
        _broker_list_path(env, STANDBY).write_text(BROKER_RUN_STANDBY + "\n")
        r = _run_log_group_step(env, tmp_path, recorded=())
        assert r.returncode == 0, r.stdout + r.stderr
        assert "::warning::" not in r.stdout
        assert _groups_left(state) == {PRIMARY: _broker_groups(BROKER_OTHER), STANDBY: _broker_groups(BROKER_OTHER)}

    def test_a_region_whose_broker_list_was_never_recorded_is_a_warning(self, stub_env, tmp_path):
        env, state, log = stub_env
        state.write_text(_log_group_state(RUN_LOG_GROUPS))
        r = _run_log_group_step(env, tmp_path, recorded=(STANDBY,))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "::warning::Log groups of this run could not all be deleted in: %s" % PRIMARY in r.stdout
        assert "mq-brokers-%s.txt does not exist" % PRIMARY in r.stdout
        assert _groups_left(state) == {PRIMARY: [], STANDBY: []}          # the rest of the run's groups still went


class TestBrokerRecordStep:
    """An Amazon MQ broker's log groups are named by its id, and the broker is gone by the
    time the log group step runs, so this step writes the ids down before the Teardown."""

    def test_it_runs_right_before_the_teardown_whatever_the_outcome_of_the_run(self):
        names = [s.get("name") for s in _e2e_steps()]
        assert names.index(RECORD_STEP) == names.index("Teardown") - 1
        # A failed run still tears down its brokers, and the ids in the log tell whoever
        # cleans up its log groups by hand which ones are its.
        assert _record_step()["if"] == "always()"

    def test_the_rendered_step_parses(self, tmp_path):
        script = tmp_path / "t.sh"
        script.write_text(_record_script())
        r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_it_records_this_runs_brokers_in_both_regions_and_nobody_elses(self, stub_env, tmp_path):
        env, state, log = stub_env
        state.write_text(json.dumps(_brokers_state()))
        r = _run_record_step(env, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert _broker_list_path(env, PRIMARY).read_text().split() == [BROKER_RUN]
        assert _broker_list_path(env, STANDBY).read_text().split() == [BROKER_RUN_STANDBY]
        assert BROKER_RUN in r.stdout and BROKER_RUN_STANDBY in r.stdout and BROKER_OTHER not in r.stdout
        assert "::warning::" not in r.stdout

    def test_every_broker_of_the_run_is_recorded_one_per_line(self, stub_env, tmp_path):
        env, state, log = stub_env
        name = "retail-store-ar-ordersmq" + ENV_SUFFIX
        state.write_text(json.dumps({"brokers": {PRIMARY: [{"name": name, "id": BROKER_RUN},
                                                           {"name": name, "id": BROKER_OTHER}], STANDBY: []}}))
        r = _run_record_step(env, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert _broker_list_path(env, PRIMARY).read_text().splitlines() == [BROKER_RUN, BROKER_OTHER]

    @pytest.mark.parametrize("empty_as_none", [False, True])
    def test_a_region_without_a_broker_gets_an_empty_list_not_the_word_none(self, stub_env, tmp_path, empty_as_none):
        env, state, log = stub_env
        state.write_text(json.dumps({"brokers": {}, "empty_as_none": empty_as_none}))
        r = _run_record_step(env, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert _broker_list_path(env, PRIMARY).read_text().strip() == ""
        assert _broker_list_path(env, STANDBY).read_text().strip() == ""
        assert "::warning::" not in r.stdout

    def test_a_region_whose_brokers_cannot_be_listed_is_a_warning_and_an_empty_list(self, stub_env, tmp_path):
        env, state, log = stub_env
        state.write_text(json.dumps(_brokers_state(mq_denied=[PRIMARY])))
        r = _run_record_step(env, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr          # the run's verdict is not this step's
        assert "::warning::" in r.stdout and PRIMARY in r.stdout.split("::warning::")[1]
        assert "mq:ListBrokers" in r.stderr                      # the cause is in the log, not swallowed
        assert _broker_list_path(env, PRIMARY).read_text().strip() == ""      # present, so the next step can read it
        assert _broker_list_path(env, STANDBY).read_text().split() == [BROKER_RUN_STANDBY]

    def test_it_looks_for_the_broker_ecs_yaml_names(self):
        brokers = [r for r in _load_template(ECS_TEMPLATE)["Resources"].values() if r["Type"] == "AWS::AmazonMQ::Broker"]
        assert len(brokers) == 1
        name = brokers[0]["Properties"]["BrokerName"]
        assert name == "retail-store-ar-ordersmq${Env}"
        assert "BrokerName=='%s'" % name.replace("${Env}", "${ENV}") in _record_script()

    def test_the_e2e_role_grants_every_call_the_step_makes(self):
        needed = {"mq:" + "".join(p.title() for p in op.split("-")) for op in re.findall(r"aws mq ([a-z-]+)", _record_script())}
        assert needed == {"mq:ListBrokers"}
        allowed = _role_allows_everywhere()
        missing = {a for a in needed if a not in allowed and "mq:*" not in allowed}
        assert not missing, f"github-oidc-role.yaml does not grant {sorted(missing)}"

    def test_this_step_and_the_log_group_step_use_the_same_file_for_each_region(self):
        file_name = r"\$\{RUNNER_TEMP\}/(mq-brokers-\$\{region\}\.txt)"
        written, read = set(re.findall(file_name, _record_script())), set(re.findall(file_name, _log_group_script()))
        assert written and written == read

    def test_the_brokers_and_the_cut_canaries_of_a_run_are_cleaned_up_end_to_end(self, stub_env, tmp_path):
        env, state, log = stub_env
        canary_groups = [_canary_group(name, n=i + 1) for i, name in enumerate(_canary_names())]
        primary = (RUN_LOG_GROUPS + canary_groups + _broker_groups(BROKER_RUN) + _broker_groups(BROKER_OTHER)
                   + OTHER_LOG_GROUPS)
        standby = RUN_LOG_GROUPS + canary_groups + _broker_groups(BROKER_RUN_STANDBY) + OTHER_LOG_GROUPS
        state.write_text(_log_group_state(primary, standby=standby, **_brokers_state()))
        assert _run_record_step(env, tmp_path).returncode == 0
        r = _run_log_group_step(env, tmp_path, recorded=())
        assert r.returncode == 0, r.stdout + r.stderr
        assert "::warning::" not in r.stdout
        # Another broker's groups (the primary has one more, from another run) and the groups that
        # are not the run's at all stay.
        assert _groups_left(state) == {PRIMARY: _broker_groups(BROKER_OTHER) + OTHER_LOG_GROUPS,
                                       STANDBY: OTHER_LOG_GROUPS}


# ---------------------------------------------------------------------------
# Image repositories: every one baseInfra declares is force-deleted first
# ---------------------------------------------------------------------------

BASE_INFRA = DEPLOYMENT / "regionalBaseInfra.yaml"


def _declared_repositories():
    """Names, without the Env suffix, of the image repositories baseInfra creates."""
    names = set()
    for res in _load_template(BASE_INFRA)["Resources"].values():
        if res["Type"] != "AWS::ECR::Repository":
            continue
        name = res["Properties"]["RepositoryName"]
        assert name.endswith("${Env}"), f"{name}: repository names carry the Env suffix"
        names.add(name[: -len("${Env}")])
    assert names, "regionalBaseInfra.yaml declares no image repositories?"
    return names


def _recipe(target):
    """A Makefile target's recipe: the tab-indented lines after `target:`."""
    lines = MAKEFILE.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(target + ":"))
    recipe = []
    for line in lines[start + 1:]:
        if not line.startswith("\t"):
            break
        recipe.append(line)
    return "\n".join(recipe)


class TestImageRepositoryTeardown:
    """CloudFormation can't delete a repository that still holds images (no
    EmptyOnDelete), so baseInfra fails to delete unless every repository it
    declares has been force-deleted first."""

    @pytest.mark.parametrize("target,region", [("destroy-ecr-primary", "$(PRIMARY_REGION)"),
                                               ("destroy-ecr-standby", "$(STANDBY_REGION)")])
    def test_destroy_ecr_force_deletes_every_repository(self, target, region):
        deleted = set(re.findall(
            r"aws ecr delete-repository --force --repository-name (\S+)\$\{ENV\} --region " + re.escape(region),
            _recipe(target)))
        missing = _declared_repositories() - deleted
        assert not missing, f"{target} does not force-delete {sorted(missing)}"

    def test_the_guard_force_deletes_every_repository(self):
        loop = re.search(
            r"for repo in ([^;]+); do\n\s+aws ecr delete-repository --force --repository-name \$\{repo\}"
            + re.escape(ENV_SUFFIX),
            _expanded(_teardown_script()))
        assert loop, "the e2e Teardown no longer loops over the image repositories"
        missing = _declared_repositories() - set(loop.group(1).split())
        assert not missing, f"the e2e Teardown does not force-delete {sorted(missing)}"
