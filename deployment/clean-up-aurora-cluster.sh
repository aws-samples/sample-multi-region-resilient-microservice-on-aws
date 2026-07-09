#!/bin/bash
# Deletes the catalog reconciliation Aurora clusters (and their member instances)
# left behind by the cross-region snapshot-restore SSM automation.
#
# These resources are created by restore-reconcile-catalog-ssm.yaml with random,
# execution-id-based identifiers (catalog-recon-dbcluster-<EXECUTION_ID> /
# catalog-recon-dbinstance-<EXECUTION_ID>), so their names carry no ENV suffix.
# The ONLY ENV-bearing handle they share is the subnet group,
# catalog-recon-dbcluster-subnet-group${ENV}. We scope deletion by an EXACT match
# on that subnet group so a run only ever removes its own reconciliation DBs and
# never touches a concurrent e2e run's (or the default no-suffix deployment's).
#
# Usage: clean-up-aurora-cluster.sh <REGION> [ENV]
#   ENV is the deployment suffix (e.g. "-a1b2c3d"); empty for the default deployment.
set -uo pipefail

REGION="${1:?Usage: clean-up-aurora-cluster.sh <REGION> [ENV]}"
ENV="${2:-}"

SUBNET_GROUP="catalog-recon-dbcluster-subnet-group${ENV}"

echo "Cleaning up catalog reconciliation Aurora clusters in $REGION (subnet group: $SUBNET_GROUP)..."

# Select only clusters attached to this ENV's reconciliation subnet group (exact match).
clusters=$(aws rds describe-db-clusters --region "$REGION" \
  --query "DBClusters[?DBSubnetGroup=='${SUBNET_GROUP}'].DBClusterIdentifier" \
  --output text 2>/dev/null)

if [ -z "$clusters" ]; then
  echo "No reconciliation clusters found for subnet group $SUBNET_GROUP in $REGION. Nothing to do."
  exit 0
fi

for cluster in $clusters; do
  echo "Processing reconciliation cluster $cluster..."

  # Delete member instances first (a cluster can't be deleted while it has members).
  instances=$(aws rds describe-db-clusters --db-cluster-identifier "$cluster" --region "$REGION" \
    --query "DBClusters[0].DBClusterMembers[].DBInstanceIdentifier" --output text 2>/dev/null)

  for instance in $instances; do
    echo "  Deleting instance $instance..."
    aws rds delete-db-instance --db-instance-identifier "$instance" \
      --skip-final-snapshot --region "$REGION" --no-cli-pager >/dev/null 2>&1 || true
  done
  for instance in $instances; do
    echo "  Waiting for instance $instance to be deleted..."
    aws rds wait db-instance-deleted --db-instance-identifier "$instance" --region "$REGION" 2>/dev/null || true
  done

  echo "  Deleting cluster $cluster..."
  aws rds delete-db-cluster --db-cluster-identifier "$cluster" \
    --skip-final-snapshot --region "$REGION" --no-cli-pager >/dev/null 2>&1 || true
  echo "  Waiting for cluster $cluster to be deleted..."
  aws rds wait db-cluster-deleted --db-cluster-identifier "$cluster" --region "$REGION" 2>/dev/null || true
  echo "  Deleted $cluster"
done

echo "Done! All matching reconciliation Aurora clusters have been deleted from $REGION."
