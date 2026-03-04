#!/bin/bash
# safe-delete-db-stack.sh - Delete a database CloudFormation stack with orphan detection
# Usage: ./safe-delete-db-stack.sh <stack-name> <region> <resource-type> <resource-logical-id> <check-command>
#
# resource-type: "aurora" or "dsql"
# check-command: CLI command to verify if the resource still exists
#
# This script:
# 1. Deletes the stack and waits
# 2. If delete fails, checks if the stuck resource actually still exists
# 3. If resource is gone: retries with --retain-resources (safe)
# 4. If resource still exists: deletes it directly, then retries the stack delete

set -euo pipefail

STACK_NAME="$1"
REGION="$2"
RESOURCE_TYPE="$3"
LOGICAL_ID="$4"
shift 4
# Remaining args are the check command

echo "Deleting stack $STACK_NAME in $REGION..."
aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION" 2>/dev/null || true

echo "Waiting for $STACK_NAME delete to complete..."
if aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION" 2>/dev/null; then
    echo "$STACK_NAME deleted successfully"
    exit 0
fi

echo "$STACK_NAME delete timed out or failed. Checking stack status..."
STATUS=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST")

if [ "$STATUS" = "DOES_NOT_EXIST" ] || [ "$STATUS" = "DELETE_COMPLETE" ]; then
    echo "$STACK_NAME is already gone"
    exit 0
fi

if [ "$STATUS" != "DELETE_FAILED" ]; then
    echo "Stack is in $STATUS state, waiting longer..."
    aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION" 2>/dev/null || true
    STATUS=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
        --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST")
    if [ "$STATUS" = "DOES_NOT_EXIST" ] || [ "$STATUS" = "DELETE_COMPLETE" ]; then
        echo "$STACK_NAME deleted successfully on extended wait"
        exit 0
    fi
fi

echo "Stack $STACK_NAME is DELETE_FAILED. Checking if $LOGICAL_ID resource still exists..."

RESOURCE_EXISTS=false
if [ "$RESOURCE_TYPE" = "aurora" ]; then
    CLUSTER_ID=$(aws cloudformation describe-stack-resources --stack-name "$STACK_NAME" --region "$REGION" \
        --logical-resource-id "$LOGICAL_ID" --query 'StackResources[0].PhysicalResourceId' --output text 2>/dev/null || echo "")
    if [ -n "$CLUSTER_ID" ] && [ "$CLUSTER_ID" != "None" ]; then
        if aws rds describe-db-clusters --db-cluster-identifier "$CLUSTER_ID" --region "$REGION" >/dev/null 2>&1; then
            RESOURCE_EXISTS=true
            echo "Aurora cluster $CLUSTER_ID still exists. Deleting it directly..."
            # Delete instances first
            for INSTANCE in $(aws rds describe-db-clusters --db-cluster-identifier "$CLUSTER_ID" --region "$REGION" \
                --query 'DBClusters[0].DBClusterMembers[*].DBInstanceIdentifier' --output text 2>/dev/null); do
                echo "  Deleting instance $INSTANCE..."
                aws rds delete-db-instance --db-instance-identifier "$INSTANCE" --skip-final-snapshot --region "$REGION" --no-cli-pager 2>/dev/null || true
            done
            # Wait for instances to delete
            for INSTANCE in $(aws rds describe-db-clusters --db-cluster-identifier "$CLUSTER_ID" --region "$REGION" \
                --query 'DBClusters[0].DBClusterMembers[*].DBInstanceIdentifier' --output text 2>/dev/null); do
                echo "  Waiting for instance $INSTANCE to delete..."
                aws rds wait db-instance-deleted --db-instance-identifier "$INSTANCE" --region "$REGION" 2>/dev/null || true
            done
            echo "  Deleting cluster $CLUSTER_ID..."
            aws rds delete-db-cluster --db-cluster-identifier "$CLUSTER_ID" --skip-final-snapshot --region "$REGION" --no-cli-pager 2>/dev/null || true
            echo "  Waiting for cluster to delete..."
            sleep 60
        fi
    fi
elif [ "$RESOURCE_TYPE" = "dsql" ]; then
    CLUSTER_ID=$(aws cloudformation describe-stack-resources --stack-name "$STACK_NAME" --region "$REGION" \
        --logical-resource-id "$LOGICAL_ID" --query 'StackResources[0].PhysicalResourceId' --output text 2>/dev/null || echo "")
    if [ -n "$CLUSTER_ID" ] && [ "$CLUSTER_ID" != "None" ]; then
        if aws dsql get-cluster --identifier "$CLUSTER_ID" --region "$REGION" >/dev/null 2>&1; then
            RESOURCE_EXISTS=true
            echo "DSQL cluster $CLUSTER_ID still exists. Deleting it directly..."
            aws dsql delete-cluster --identifier "$CLUSTER_ID" --region "$REGION" --no-cli-pager 2>/dev/null || true
            echo "  Waiting for DSQL cluster to delete..."
            sleep 120
        fi
    fi
fi

if [ "$RESOURCE_EXISTS" = "false" ]; then
    echo "Resource $LOGICAL_ID is already gone. Retrying stack delete with --retain-resources..."
else
    echo "Resource deleted directly. Retrying stack delete with --retain-resources..."
fi

aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION" --retain-resources "$LOGICAL_ID"
aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"
echo "$STACK_NAME cleaned up successfully"
