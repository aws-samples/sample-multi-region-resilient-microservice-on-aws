# The FIS fault-injection sidecar (the amazon-ssm-agent container in every task definition of ecs.yaml).
#
# The task subnets have no internet route, so everything the sidecar and the FIS fault documents run
# inside a task is baked into this image, which is built from the SSM agent's public image:
#   - the sidecar's start script: jq, ps (procps), aws (awscli), curl (curl-minimal), setsid (util-linux)
#   - AWSFIS-Run-Network-Packet-Loss: atd (at), dig (bind-utils), lsof, pgrep (procps), tc (iproute-tc)
#   - the agent-install document Resilience Hub runs on the task before a test: python3 and
#     python3-requests (observed in a July 2026 test run, where installing them at run time failed)
#
# Two builds make this image and must stay the same: mirror-sidecar-buildspec.yml (make
# mirror-sidecar-images, the first deployment) and self-update.yaml (the weekly repave, which keeps the
# base layers and packages current). tests/test_fis_sidecar.py holds this file and the buildspec's
# copy equal, and holds the repave to rebuilding the image rather than copying the bare base over it.
FROM public.ecr.aws/amazon-ssm-agent/amazon-ssm-agent:latest
RUN dnf upgrade -y && dnf install -y jq procps awscli curl-minimal util-linux && dnf clean all
RUN dnf install -y at bind-utils lsof iproute iproute-tc && dnf clean all
RUN dnf install -y python3 python3-requests && dnf clean all
