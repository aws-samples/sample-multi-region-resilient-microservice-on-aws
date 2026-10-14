# SPDX-License-Identifier: MIT-0
"""NGRH test tooling (design 5.9), run as ``python3 -m ngrh_testing <command>`` from deployment/.

Python 3.9+ standard library only. AWS calls go through the AWS CLI v2 the Makefile already
requires (``aws.py``), so the tooling doesn't depend on a boto3 recent enough to know every
service it calls.

Commands:
  replay        how the journey alarms and the failover triggers would have behaved over past
                canary data (make ngrh-alarm-replay). Read-only.
  reconcile     create or update the tests in ngrh-tests.json and make their alarm sources match
                (make ngrh-tests); --check only says whether they have drifted.
  delete-tests  delete every test on the ngrh stack's services (make destroy-ngrh runs it first).
  preflight     refuse a run, with every reason, unless what it needs is in place (make
                ngrh-test-preflight); live or static.
  run           preflight, start one test's run, follow it to its end and write its report (make ngrh-test).
  stop          stop the test's active run (make ngrh-test-stop).
  report        collect a run's report as JSON and markdown (make ngrh-test-report).
"""
