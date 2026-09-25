#!/bin/bash
# Delete a baseVpc stack, emptying its canary bucket immediately before each attempt.
#
# Why the bucket has to be emptied HERE, not earlier in destroy-all:
#
# destroy-apps-* empties the bucket and then deletes the apps stack -- but the ALB
# it is about to delete keeps delivering access logs into the bucket for about a
# minute after its last request, and the bucket also logs its own S3 access
# (LoggingConfiguration with no destination), which S3 delivers best-effort up to
# a few hours later. destroy-infra reaches baseVpc an hour or two after that
# empty, once the databases are gone, and by then the bucket is not empty:
# CloudFormation fails the stack on canaryBucket ("The bucket you tried to delete
# is not empty"). Observed live on e2e run 35887370433 (2026-09-23): emptied at
# 18:44:20Z (698 objects), three ALB log objects landed at 18:44:53Z, baseVpc went
# DELETE_FAILED at 20:24Z on exactly those three, in a run that was otherwise clean.
#
# So the bucket is emptied right before delete-stack. If CloudFormation still finds
# an object -- a log delivery that landed in the seconds in between -- the bucket is
# emptied again and the delete retried, a bounded number of times. Any OTHER
# DELETE_FAILED resource is reported and is a hard failure: this helper only knows
# how to fix the bucket, and a VPC that still "has dependencies" needs a human.
#
# The bucket is resolved from the stack's own resource list rather than the
# canaryBucketName SSM parameter. On a stack that is already DELETE_FAILED the
# parameter resource has usually been deleted; the bucket has not.
#
# Usage: delete-vpc-stack.sh <stack-name> <region>
set -uo pipefail

STACK=${1:?usage: delete-vpc-stack.sh <stack-name> <region>}
REGION=${2:?usage: delete-vpc-stack.sh <stack-name> <region>}
ATTEMPTS=${ATTEMPTS:-3}
HERE=$(cd "$(dirname "$0")" && pwd)

status() {
    aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
        --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST"
}

failed_resources() {
    aws cloudformation describe-stack-resources --stack-name "$STACK" --region "$REGION" \
        --query 'StackResources[?ResourceStatus==`DELETE_FAILED`].LogicalResourceId' \
        --output text 2>/dev/null || true
}

empty_bucket() {
    local bucket
    bucket=$(aws cloudformation describe-stack-resources --stack-name "$STACK" --region "$REGION" \
        --logical-resource-id canaryBucket --query 'StackResources[0].PhysicalResourceId' \
        --output text 2>/dev/null || true)
    if [ -z "$bucket" ] || [ "$bucket" = "None" ]; then
        echo "$STACK has no canaryBucket resource; nothing to empty."
        return 0
    fi
    "$HERE/cleanup.sh" "$bucket"
}

if [ "$(status)" = "DOES_NOT_EXIST" ]; then
    echo "$STACK in $REGION is already gone."
    exit 0
fi

for attempt in $(seq 1 "$ATTEMPTS"); do
    echo "Emptying the canary bucket of $STACK in $REGION (attempt $attempt/$ATTEMPTS)..."
    empty_bucket || exit 1

    echo "Deleting stack $STACK in $REGION..."
    aws cloudformation delete-stack --stack-name "$STACK" --region "$REGION" || exit 1
    if aws cloudformation wait stack-delete-complete --stack-name "$STACK" --region "$REGION" 2>/dev/null; then
        echo "$STACK deleted successfully"
        exit 0
    fi

    st=$(status)
    case "$st" in
        *_IN_PROGRESS)
            # On a stack that was already DELETE_FAILED from an earlier attempt the
            # waiter can read that stale status before this attempt's
            # DELETE_IN_PROGRESS is visible. Wait once more before judging.
            aws cloudformation wait stack-delete-complete --stack-name "$STACK" --region "$REGION" 2>/dev/null || true
            st=$(status) ;;
    esac
    case "$st" in
        DOES_NOT_EXIST|DELETE_COMPLETE)
            echo "$STACK deleted successfully"
            exit 0 ;;
        DELETE_FAILED) ;;
        *)
            echo "ERROR: $STACK is $st after the delete wait; not retrying." >&2
            exit 1 ;;
    esac

    failed=$(failed_resources)
    if [ "$failed" != "canaryBucket" ]; then
        echo "ERROR: $STACK is DELETE_FAILED on [$failed]; only a non-empty canary bucket is retried here:" >&2
        aws cloudformation describe-stack-resources --stack-name "$STACK" --region "$REGION" \
            --query 'StackResources[?ResourceStatus==`DELETE_FAILED`].[LogicalResourceId,ResourceStatusReason]' \
            --output text >&2 2>/dev/null || true
        exit 1
    fi
    echo "$STACK is DELETE_FAILED on canaryBucket: an object landed after the empty. Emptying again and retrying..."
done

echo "ERROR: $STACK is still DELETE_FAILED on canaryBucket after $ATTEMPTS attempts." >&2
exit 1
