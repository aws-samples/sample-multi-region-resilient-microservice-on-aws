#!/bin/sh
# Empties an S3 bucket of all objects, versions, and delete markers.
#
# Each page of list-object-versions carries BOTH Versions and DeleteMarkers, and
# both are deleted together, so every API call makes forward progress. A bucket
# of N entries costs about N/1000 round trips.
#
# Do NOT reintroduce `aws s3 rm --recursive` here. On a versioning-enabled
# bucket a keyed delete removes no data -- it writes a delete marker over each
# current object. That leaves one extra marker per object for this script to
# clean up afterwards, roughly doubling the work.
#
# Do NOT filter a page down to Versions only. Delete markers accumulate at the
# front of the key space, so a versions-only pass has to scan past all of them
# on every iteration to find the next batch -- quadratic on large buckets.
BUCKET=$1

if [ -z "$BUCKET" ]; then
  echo "Usage: cleanup.sh <bucket-name>"
  exit 1
fi

# Test if bucket exists. Run head-bucket directly as the condition and discard its
# output: in AWS CLI v2, a successful head-bucket prints a JSON body to stdout, so
# capturing it into a variable and comparing with -eq breaks the integer test.
if ! aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  echo "Bucket $BUCKET does not exist, skipping."
  exit 0
fi

echo "Bucket $BUCKET exists. Emptying..."

# Per-invocation temp files so concurrent cleanups don't clobber each other.
PAGE_FILE=$(mktemp "${TMPDIR:-/tmp}/s3-page.XXXXXX.json")
BATCH_FILE=$(mktemp "${TMPDIR:-/tmp}/s3-delete-batch.XXXXXX.json")
trap 'rm -f "$PAGE_FILE" "$BATCH_FILE"' EXIT

TOTAL=0
while true; do
  # --max-keys caps the API page at one delete-objects batch; --no-paginate stops
  # the CLI from walking the entire bucket before we get a chance to delete.
  if ! aws s3api list-object-versions --bucket "$BUCKET" \
       --max-keys 1000 --no-paginate --output json > "$PAGE_FILE"; then
    echo "  ERROR: list-object-versions failed after $TOTAL deletions" >&2
    exit 1
  fi

  jq '{Objects: [(.Versions // [])[], (.DeleteMarkers // [])[] | {Key, VersionId}], Quiet: true}' \
    "$PAGE_FILE" > "$BATCH_FILE"

  COUNT=$(jq '.Objects | length' "$BATCH_FILE")
  if [ "$COUNT" -eq 0 ]; then
    break
  fi

  # Surface failures instead of swallowing them -- a silently failed delete here
  # resurfaces much later as a DELETE_FAILED stack on a non-empty bucket.
  if ! aws s3api delete-objects --bucket "$BUCKET" \
       --delete "file://$BATCH_FILE" --no-cli-pager >/dev/null; then
    echo "  ERROR: delete-objects failed after $TOTAL deletions" >&2
    exit 1
  fi

  TOTAL=$((TOTAL + COUNT))
  echo "  Deleted $COUNT entries (running total: $TOTAL)"
done

echo "  Bucket $BUCKET emptied. Total entries removed: $TOTAL"
