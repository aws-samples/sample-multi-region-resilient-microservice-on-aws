#!/bin/sh
# Empties an S3 bucket of all objects, versions, and delete markers.
# Uses 'aws s3 rm --recursive' for current objects (handles special chars in keys)
# then cleans up old versions and delete markers via paginated API calls.
BUCKET=$1

if [ -z "$BUCKET" ]; then
  echo "Usage: cleanup.sh <bucket-name>"
  exit 1
fi

# Test if bucket exists
bucketExists=$(aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null && echo 1 || echo 0)

if [ "$bucketExists" -eq 1 ]; then
  echo "Bucket $BUCKET exists. Emptying..."

  # Step 1: Delete all current objects (handles special chars in keys)
  echo "  Removing current objects..."
  aws s3 rm "s3://$BUCKET" --recursive --quiet 2>/dev/null || true

  # Step 2: Delete old versions in batches
  while true; do
    VERSIONS=$(aws s3api list-object-versions --bucket "$BUCKET" --max-items 500 \
      --query 'Versions[].{Key:Key,VersionId:VersionId}' --output json 2>/dev/null)
    COUNT=$(echo "$VERSIONS" | jq 'length // 0')
    if [ "$COUNT" -eq 0 ] || [ "$COUNT" = "null" ]; then
      break
    fi
    echo "  Deleting $COUNT object versions..."
    echo "{\"Objects\": $VERSIONS, \"Quiet\": true}" > /tmp/s3-delete-batch.json
    aws s3api delete-objects --bucket "$BUCKET" --delete "file:///tmp/s3-delete-batch.json" --no-cli-pager 2>/dev/null || true
  done

  # Step 3: Delete delete markers in batches
  while true; do
    MARKERS=$(aws s3api list-object-versions --bucket "$BUCKET" --max-items 500 \
      --query 'DeleteMarkers[].{Key:Key,VersionId:VersionId}' --output json 2>/dev/null)
    COUNT=$(echo "$MARKERS" | jq 'length // 0')
    if [ "$COUNT" -eq 0 ] || [ "$COUNT" = "null" ]; then
      break
    fi
    echo "  Deleting $COUNT delete markers..."
    echo "{\"Objects\": $MARKERS, \"Quiet\": true}" > /tmp/s3-delete-batch.json
    aws s3api delete-objects --bucket "$BUCKET" --delete "file:///tmp/s3-delete-batch.json" --no-cli-pager 2>/dev/null || true
  done

  rm -f /tmp/s3-delete-batch.json
  echo "  Bucket $BUCKET emptied."
else
  echo "Bucket $BUCKET does not exist, skipping."
fi
