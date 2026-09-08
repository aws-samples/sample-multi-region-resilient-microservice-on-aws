#!/bin/bash
# Detach every member from an Aurora global cluster -- readers first, writer last.
#
# Why the order cannot be hardcoded by region:
#
# Aurora refuses to remove the writer cluster while any other member remains
# ("Can't remove writer cluster when there are other clusters"). This project's
# entire purpose is regional failover, and after a failover the WRITER lives in
# STANDBY_REGION. The previous teardown code hardcoded "detach the standby
# cluster first", which is exactly backwards in that state -- and because it
# discarded the error with `2>/dev/null || true`, destroy-all continued as if the
# detach had succeeded. The cluster stayed a global member, CloudFormation could
# not delete its DB instance, catalog-db-stack landed in DELETE_FAILED, and the
# stack was then deleted with --retain-resources, leaving a live Aurora cluster
# orphaned and billing with no stack left to find it by.
#
# So: ask the API which member is the writer instead of assuming, and fail loudly
# rather than pretending success.
#
# Detaching every member (not just the standby) is correct here because this only
# runs from destroy-all -- both clusters are about to be deleted. A detached
# cluster simply becomes standalone read-write, which CloudFormation deletes
# normally.
#
# Usage: detach-global-cluster.sh <global-cluster-id> <region>
set -uo pipefail

GLOBAL_CLUSTER=${1:?usage: detach-global-cluster.sh <global-cluster-id> <region>}
REGION=${2:?usage: detach-global-cluster.sh <global-cluster-id> <region>}

POLL_ATTEMPTS=40
POLL_SLEEP=15

# Emits "<DBClusterArn>\t<IsWriter>" per member; empty output means no members.
# stderr is folded into stdout so callers can inspect the failure text.
members() {
    aws rds describe-global-clusters \
        --global-cluster-identifier "$GLOBAL_CLUSTER" \
        --region "$REGION" --output text \
        --query 'GlobalClusters[0].GlobalClusterMembers[].[DBClusterArn,IsWriter]' 2>&1
}

gone() {
    case "$1" in
        *GlobalClusterNotFound*|*not\ found*|*NotFound*) return 0 ;;
        *) return 1 ;;
    esac
}

detach_one() {
    local arn=$1 err
    echo "  detaching $arn"
    if ! err=$(aws rds remove-from-global-cluster \
            --global-cluster-identifier "$GLOBAL_CLUSTER" \
            --db-cluster-identifier "$arn" \
            --region "$REGION" --no-cli-pager 2>&1); then
        # Already detached is the only tolerable failure -- everything else must
        # stop the teardown rather than be swallowed.
        case "$err" in
            *"is not attached"*|*"not a member"*|*DBClusterNotFound*)
                echo "    already detached" ;;
            *)
                echo "ERROR: failed to detach $arn from $GLOBAL_CLUSTER:" >&2
                echo "$err" >&2
                return 1 ;;
        esac
    fi
}

if ! OUT=$(members); then
    if gone "$OUT"; then
        echo "Global cluster $GLOBAL_CLUSTER does not exist; nothing to detach."
        exit 0
    fi
    echo "ERROR: cannot describe global cluster $GLOBAL_CLUSTER in $REGION:" >&2
    echo "$OUT" >&2
    exit 1
fi

if [ -z "$OUT" ]; then
    echo "Global cluster $GLOBAL_CLUSTER has no members; nothing to detach."
    exit 0
fi

echo "Global cluster $GLOBAL_CLUSTER members:"
echo "$OUT" | sed 's/^/  /'

# Readers first. The writer is only removable once it is the last member.
echo "$OUT" | awk '$2=="False" {print $1}' | while read -r arn; do
    [ -n "$arn" ] && detach_one "$arn"
done
echo "$OUT" | awk '$2=="True" {print $1}' | while read -r arn; do
    [ -n "$arn" ] && detach_one "$arn"
done

# Confirm membership actually reached zero. The previous code used a bare
# `sleep 30` and assumed success, which is how the silent failure above went
# unnoticed for so long.
for _ in $(seq 1 "$POLL_ATTEMPTS"); do
    if ! OUT=$(members); then
        if gone "$OUT"; then
            echo "Global cluster $GLOBAL_CLUSTER no longer exists."
            exit 0
        fi
        echo "ERROR: cannot re-describe global cluster $GLOBAL_CLUSTER:" >&2
        echo "$OUT" >&2
        exit 1
    fi
    if [ -z "$OUT" ]; then
        echo "All members detached from $GLOBAL_CLUSTER."
        exit 0
    fi
    sleep "$POLL_SLEEP"
done

echo "ERROR: $GLOBAL_CLUSTER still has members after $((POLL_ATTEMPTS * POLL_SLEEP))s:" >&2
echo "$OUT" >&2
exit 1
