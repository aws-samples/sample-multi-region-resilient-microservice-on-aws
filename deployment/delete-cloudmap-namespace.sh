#!/bin/bash
# Delete the Cloud Map namespace that ECS Service Connect created for one region.
#
# ecs.yaml sets ServiceConnectDefaults on the cluster, so ECS creates the
# retail-store-ar${ENV} HTTP namespace itself. CloudFormation never owns it, so
# deleting every stack of a deployment still leaves the namespace behind, one per
# region per deploy. At the default Cloud Map quota (50 namespaces per account per
# region) the leak stops every later deploy: EcsCluster CREATE_FAILED "number of
# namespaces has reached the maximum allowed limit".
#
# Why this is a script that FAILS rather than a best-effort Makefile loop: the
# previous loop sent every error to /dev/null and printed "no namespace found"
# when list-namespaces failed. The e2e role had no servicediscovery permission,
# so from the day the loop was added every run reported a clean teardown while
# leaking its namespace -- 61 of them by 2026-09-30, a day and a half from the
# quota. A namespace this helper cannot delete is a leak, and a leak is a failure
# the caller has to see: exit 1, with the API error left on stderr.
#
# The namespace is deleted only when it holds no services. Once the ECS stacks are
# gone, Service Connect has deregistered them; a namespace that still has
# services at that point is something the teardown did not remove, and that too
# is reported as a failure rather than "skipped".
#
# Usage: delete-cloudmap-namespace.sh <namespace-name> <region>
set -uo pipefail

NAME=${1:?usage: delete-cloudmap-namespace.sh <namespace-name> <region>}
REGION=${2:?usage: delete-cloudmap-namespace.sh <namespace-name> <region>}

if ! ns_id=$(aws servicediscovery list-namespaces --region "$REGION" \
        --query "Namespaces[?Name=='$NAME']|[0].Id" --output text); then
    echo "$REGION: could not list Cloud Map namespaces (see the error above); $NAME may be leaking"
    exit 1
fi

if [ -z "$ns_id" ] || [ "$ns_id" = "None" ]; then
    echo "$REGION: no $NAME namespace found"
    exit 0
fi

if ! svc_count=$(aws servicediscovery list-services --region "$REGION" \
        --filters "Name=NAMESPACE_ID,Values=$ns_id,Condition=EQ" \
        --query 'length(Services)' --output text); then
    echo "$REGION: could not list the services of namespace $ns_id (see the error above); $NAME not deleted"
    exit 1
fi

if [ "$svc_count" != "0" ]; then
    echo "$REGION: namespace $ns_id ($NAME) still has $svc_count services; not deleted"
    exit 1
fi

if ! err=$(aws servicediscovery delete-namespace --region "$REGION" --id "$ns_id" 2>&1 >/dev/null); then
    # The API error stays visible on stderr either way.
    echo "$err" >&2
    # DeleteNamespace is asynchronous: the namespace stays listed until Cloud Map
    # finishes, and a second DeleteNamespace meanwhile fails with DuplicateRequest.
    # destroy-all deletes the namespace and the e2e Teardown guard then runs this
    # helper again, so on a teardown that removed everything the guard can land
    # mid-delete (run 37401247653: both namespaces reported as leaked 33 s after
    # destroy-all had deleted them). Wait for that delete to finish instead of
    # calling it a leak; a namespace still listed when the wait runs out is one.
    if [[ "$err" == *DuplicateRequest* ]]; then
        attempts=${CLOUDMAP_DELETE_WAIT_ATTEMPTS:-24}
        interval=${CLOUDMAP_DELETE_WAIT_INTERVAL:-5}
        for ((i = 0; i < attempts; i++)); do
            if ! still=$(aws servicediscovery list-namespaces --region "$REGION" \
                    --query "Namespaces[?Name=='$NAME']|[0].Id" --output text); then
                echo "$REGION: could not list Cloud Map namespaces (see the error above); $NAME may be leaking"
                exit 1
            fi
            if [ -z "$still" ] || [ "$still" = "None" ]; then
                echo "$REGION: namespace $ns_id ($NAME) was already being deleted; it is gone now"
                exit 0
            fi
            sleep "$interval"
        done
        echo "$REGION: namespace $ns_id ($NAME) was already being deleted but is still present after $attempts checks $interval s apart"
        exit 1
    fi
    echo "$REGION: failed to delete namespace $ns_id ($NAME)"
    exit 1
fi

echo "$REGION: deleted namespace $ns_id ($NAME)"
