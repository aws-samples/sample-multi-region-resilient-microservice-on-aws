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

The stub `aws` below is a tiny state machine over a JSON file: RDS global
cluster membership with asynchronous removal, CloudFormation stacks with
asynchronous deletion and an optional first-attempt failure, S3 buckets
whose objects can "land" between an empty and CloudFormation's DeleteBucket,
and Cloud Map namespaces with a per-region AccessDenied switch.
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

def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None

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
        if ns is None:
            fail("(NamespaceNotFound) when calling the DeleteNamespace operation: Namespace %s not found" % ns_id)
        if ns.get("services"):
            fail("(ResourceInUse) when calling the DeleteNamespace operation: Namespace %s still has services" % ns_id)
        if ns.get("delete_fails"):
            fail("(InternalServiceError) when calling the DeleteNamespace operation: stub-induced failure", 255)
        namespaces.remove(ns)
        save()
        print(json.dumps({"OperationId": "op-" + ns_id}))
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
    env = dict(os.environ)
    env.update(PATH=f"{bindir}:{env['PATH']}", STUB_STATE=str(state), STUB_LOG=str(log),
               POLL_ATTEMPTS="20", POLL_SLEEP="0")
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
    run = (run.replace("${{ env.ENV }}", ENV_SUFFIX)
              .replace("${{ env.AWS_REGION }}", PRIMARY)
              .replace("${{ env.STANDBY_REGION }}", STANDBY))
    assert "${{" not in run, "unsubstituted GitHub expression in Teardown"
    return run


def _run_teardown(env, tmp_path):
    script = tmp_path / "teardown.sh"
    script.write_text(_teardown_script())
    # GitHub runs `run:` blocks with `bash -e`; mirror that so a failing command
    # inside a function is treated the way the real job would treat it.
    return subprocess.run(["bash", "-e", str(script)], cwd=DEPLOYMENT, env=env,
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
        assert "./delete-cloudmap-namespace.sh %s" % NAMESPACE in _teardown_script()


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
            _teardown_script())
        assert loop, "the e2e Teardown no longer loops over the image repositories"
        missing = _declared_repositories() - set(loop.group(1).split())
        assert not missing, f"the e2e Teardown does not force-delete {sorted(missing)}"
