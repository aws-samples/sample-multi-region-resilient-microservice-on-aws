#!/bin/bash

REGION=$1

echo "Deleting Aurora orders-recon-dbinstance-* database instances in $REGION region..."

instances=$(aws rds describe-db-instances --region $REGION --query  "DBInstances[?starts_with(DBInstanceIdentifier, 'orders-recon-dbinstance-')].DBInstanceIdentifier"  --output text) 

for instance in $instances; do
    aws rds delete-db-instance --db-instance-identifier $instance --skip-final-snapshot --region $REGION
    echo "Deleting $instance"
    aws rds delete-db-instance --db-instance-identifier $instance --skip-final-snapshot --region $REGION
    echo "Deleted $instance"
    sleep 5
    echo "Waiting for $instance to be deleted"
    aws rds wait db-instance-deleted --db-instance-identifier $instance --region $REGION    
    echo "Deleted $instance"

done

echo "Deleting Aurora orders-recon-dbcluster-* database clusters in $REGION region..."

clusters=$(aws rds describe-db-clusters --region $REGION --query "DBClusters[?starts_with(DBClusterIdentifier, 'orders-recon-dbcluster-')].DBClusterIdentifier"  --output text)

for cluster in $clusters; do
    aws rds delete-db-cluster --db-cluster-identifier $cluster --skip-final-snapshot --region $REGION
    echo "Deleting $cluster"
    aws rds delete-db-cluster --db-cluster-identifier $cluster --skip-final-snapshot --region $REGION
    echo "Deleted $cluster"
    sleep 5
    echo "Waiting for $cluster to be deleted"
    aws rds wait db-cluster-deleted --db-cluster-identifier $cluster --region $REGION
    echo "Deleted $cluster"
done

echo "Done! All Aurora database instances and clusters have been deleted from the $REGION region."


    


        
        



    




