#!/bin/bash
# Tears down everything ngrh-gamma-create.sh created: the six NGRH services,
# four user journeys, system, two policies (all via the gamma endpoint), plus
# the reports S3 bucket and the invoker IAM role. Safe to re-run; missing
# resources are skipped.
#
# Usage: ENV=-ngrh ./ngrh-gamma-destroy.sh
set -uo pipefail

ENV="${ENV:-}"
NGRH_REGION="${NGRH_REGION:-us-east-2}"
NGRH_ENDPOINT="${NGRH_ENDPOINT:-https://gamma.us-east-2.proxy.digito.migration-services.aws.dev/}"

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="ngrh-gamma-${AWS_ACCOUNT_ID}${ENV}"
INVOKER_ROLE="ngrh-invoker-gamma${ENV}"

rh() {
  aws resiliencehubv2 "$@" --region "$NGRH_REGION" --endpoint-url "$NGRH_ENDPOINT" --no-cli-pager
}

log() { echo ">>> $*"; }

# ---------------------------------------------------------------------------
# Services (deleting a service removes its input sources and assertions).
# ---------------------------------------------------------------------------
for name in "ui${ENV}" "catalog${ENV}" "cart${ENV}" "checkout${ENV}" "orders${ENV}" "assets${ENV}"; do
  arn=$(rh list-services --query "serviceSummaries[?name=='${name}'].serviceArn | [0]" --output text)
  if [ -n "$arn" ] && [ "$arn" != "None" ]; then
    log "Deleting service $name ($arn)..."
    rh delete-service --service-arn "$arn" || echo "  WARN: failed to delete service $name"
  else
    log "Service $name not found; skipping."
  fi
done

# ---------------------------------------------------------------------------
# User journeys, then the system.
# ---------------------------------------------------------------------------
SYSTEM_NAME="resilient-microservice${ENV}"
SYSTEM_ARN=$(rh list-systems --query "systemSummaries[?name=='${SYSTEM_NAME}'].systemArn | [0]" --output text)
if [ -n "$SYSTEM_ARN" ] && [ "$SYSTEM_ARN" != "None" ]; then
  for id in $(rh list-user-journeys --system-arn "$SYSTEM_ARN" \
      --query 'userJourneySummaries[].userJourneyId' --output text); do
    log "Deleting user journey $id..."
    rh delete-user-journey --system-arn "$SYSTEM_ARN" --user-journey-id "$id" \
      || echo "  WARN: failed to delete journey $id"
  done
  log "Deleting system $SYSTEM_NAME..."
  rh delete-system --system-arn "$SYSTEM_ARN" || echo "  WARN: failed to delete system"
else
  log "System $SYSTEM_NAME not found; skipping."
fi

# ---------------------------------------------------------------------------
# Policies.
# ---------------------------------------------------------------------------
for name in "ngrh-tier1${ENV}" "ngrh-tier2${ENV}"; do
  arn=$(rh list-policies --query "policySummaries[?name=='${name}'].policyArn | [0]" --output text)
  if [ -n "$arn" ] && [ "$arn" != "None" ]; then
    log "Deleting policy $name..."
    rh delete-policy --policy-arn "$arn" || echo "  WARN: failed to delete policy $name"
  else
    log "Policy $name not found; skipping."
  fi
done

# ---------------------------------------------------------------------------
# Reports bucket (empty via cleanup.sh, then delete).
# ---------------------------------------------------------------------------
if aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  log "Emptying and deleting bucket $BUCKET..."
  "$(dirname "$0")/cleanup.sh" "$BUCKET"
  aws s3api delete-bucket --bucket "$BUCKET" --region "$NGRH_REGION" \
    || echo "  WARN: failed to delete bucket $BUCKET"
else
  log "Bucket $BUCKET not found; skipping."
fi

# ---------------------------------------------------------------------------
# Invoker IAM role (detach/delete policies first).
# ---------------------------------------------------------------------------
if aws iam get-role --role-name "$INVOKER_ROLE" >/dev/null 2>&1; then
  log "Deleting IAM role $INVOKER_ROLE..."
  aws iam detach-role-policy --role-name "$INVOKER_ROLE" \
    --policy-arn arn:aws:iam::aws:policy/AWSResilienceHubV2AssessmentExecutionPolicy 2>/dev/null
  aws iam delete-role-policy --role-name "$INVOKER_ROLE" --policy-name ngrh-bucket-access 2>/dev/null
  aws iam delete-role --role-name "$INVOKER_ROLE" || echo "  WARN: failed to delete role $INVOKER_ROLE"
else
  log "IAM role $INVOKER_ROLE not found; skipping."
fi

log "NGRH gamma teardown complete."
