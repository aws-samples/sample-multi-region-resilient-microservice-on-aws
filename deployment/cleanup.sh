#!/bin/sh
#set -xv
BUCKET=$1

# Test if an S3 bucket exists and delete all objects and delete markers from it
# If it does not exist, just print a message
bucketExists=$(aws s3api list-buckets --query "Buckets[].Name" | grep -w "$BUCKET" | wc -l)

if [ "$bucketExists" -eq 1 ]; then
  echo "Bucket $BUCKET exists. Deleting all objects and delete markers..."

  # Delete object versions in batches
  while true; do
    objects=$(aws s3api list-object-versions --bucket "$BUCKET" --max-items 1000 --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' --output json)
    if [ "$(echo "$objects" | jq '.Objects | length')" -eq 0 ]; then
      break
    fi
    echo 'Deleting Object Versions...'
    echo "$objects" | jq '{Objects: .Objects}' > delete.json
    aws s3api delete-objects --bucket "$BUCKET" --delete file://delete.json --no-cli-pager
  done

  # Delete delete markers in batches
  while true; do
    deleteMarkers=$(aws s3api list-object-versions --bucket "$BUCKET" --max-items 1000 --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' --output json)
    if [ "$(echo "$deleteMarkers" | jq '.Objects | length')" -eq 0 ]; then
      break
    fi
    echo 'Deleting DeleteMarkers...'
    echo "$deleteMarkers" | jq '{Objects: .Objects}' > delete.json
    aws s3api delete-objects --bucket "$BUCKET" --delete file://delete.json --no-cli-pager
  done

  echo "All objects and delete markers in bucket $BUCKET have been deleted."
else
  echo "Bucket $BUCKET does not exist..."
fi

# Clean up
rm -f delete.json
