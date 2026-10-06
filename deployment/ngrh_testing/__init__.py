# SPDX-License-Identifier: MIT-0
"""NGRH test tooling (design 5.9), run as ``python3 -m ngrh_testing <command>`` from deployment/.

Python 3.9+ standard library only. AWS calls go through the AWS CLI v2 the Makefile already
requires (``aws.py``), so the tooling doesn't depend on a boto3 recent enough to know every
service it calls.

Commands:
  replay   how the journey alarms and the failover triggers would have behaved over past
           canary data (make ngrh-alarm-replay). Read-only.
"""
