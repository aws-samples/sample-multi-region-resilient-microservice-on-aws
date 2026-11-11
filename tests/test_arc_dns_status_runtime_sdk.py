"""app-dns-status runs on the boto3 that the python3.12 runtime ships, with no layer.

The function calls ``boto3.client('arc-region-switch')``. It used to carry a ``boto3-latest`` layer that
``make boto3-layer`` published once per Region, as version 1, from whatever Python the operator ran ``pip3``
with. The layer in test2 was built on 2026-06-12 from a Python 3.9 host, where boto3 pins urllib3 below 1.27,
so it shipped urllib3 1.26.20; Inspector reported 5 HIGH findings on the function in each Region, and the
layer could not be refreshed without publishing a new version and editing the template.

The runtime no longer needs it. On 2026-11-06 the official ``public.ecr.aws/lambda/python:3.12`` image, built
from the managed runtime, ships boto3 and botocore 1.42.97 and urllib3 2.8.0, and botocore has the
``arc-region-switch`` client with ``list_route53_health_checks``, the one call the function makes.

These tests keep an out-of-band layer from coming back unnoticed. If the function ever needs a newer SDK than
the runtime has, build the layer in CloudFormation or a pipeline, not with a host-dependent ``pip3 install``.

Run with:  pytest tests/test_arc_dns_status_runtime_sdk.py -v
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DEPLOYMENT = REPO_ROOT / "deployment"


def test_the_function_declares_no_layer():
    template = (DEPLOYMENT / "arc-dns-status.yaml").read_text()
    assert not re.search(r"^\s*Layers:", template, re.MULTILINE), (
        "arc-dns-status.yaml attaches a layer again; the python3.12 runtime's own boto3 has the "
        "arc-region-switch client, and a hand-published layer ages into Inspector findings"
    )
    assert "boto3-latest" not in template


def test_the_makefile_publishes_no_boto3_layer():
    makefile = (DEPLOYMENT / "Makefile").read_text()
    assert "boto3-layer" not in makefile and "boto3-latest" not in makefile
    assert "publish-layer-version" not in makefile, "the Makefile publishes a Lambda layer outside CloudFormation"


def test_the_function_still_makes_the_one_call_the_runtime_sdk_has():
    template = (DEPLOYMENT / "arc-dns-status.yaml").read_text()
    assert "boto3.client('arc-region-switch')" in template
    assert "arc.list_route53_health_checks(" in template
