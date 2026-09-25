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

The stub `aws` below is a tiny state machine over a JSON file: RDS global
cluster membership with asynchronous removal, CloudFormation stacks with
asynchronous deletion and an optional first-attempt failure, and S3 buckets
whose objects can "land" between an empty and CloudFormation's DeleteBucket.
Every invocation is appended to a log so the tests can assert ORDER, not just
end state.

Run with:  pytest tests/test_teardown_helpers.py -v
"""

import json
import os
import re
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
E2E_WORKFLOW = Path(os.environ.get("E2E_WORKFLOW", REPO / ".github" / "workflows" / "e2e.yml"))

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
