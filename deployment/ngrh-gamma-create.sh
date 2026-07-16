#!/bin/bash
# Creates the NGRH (ResilienceHubV2) model from ngrh.yaml imperatively via the
# AWS CLI, targeting the NGRH *gamma* endpoint in us-east-2. The app CFN stacks
# themselves still live in PRIMARY_REGION/STANDBY_REGION (us-east-1/us-west-2);
# only the resiliencehubv2 control-plane calls are pointed at gamma.
#
# Mirrors deployment/ngrh.yaml:
#   - S3 bucket for reports (encryption, versioning, lifecycle, access block)
#   - ngrh-invoker IAM role (trusts resiliencehub.amazonaws.com)
#   - 2 policies (tier1/tier2), 1 system, 4 user journeys
#   - 6 services with TAG-BASED input sources (service in {<name>, shared}) + assertions
#
# Resource discovery is by resource tag (key "service"), NOT by CloudFormation
# stack, so each service only sees the resources it owns plus shared infra.
# DependencyDiscovery stays ENABLED so the tagged seed is expanded along real
# dependency edges and the service registers as a traffic source.
#
# Idempotent: resources are looked up by name and reused; input sources and
# assertions are skipped when already present. Safe to re-run.
#
# Usage: ENV=-ngrh ./ngrh-gamma-create.sh
set -euo pipefail

ENV="${ENV:-}"
PRIMARY_REGION="${PRIMARY_REGION:-us-east-1}"
STANDBY_REGION="${STANDBY_REGION:-us-west-2}"
NGRH_REGION="${NGRH_REGION:-us-east-2}"
NGRH_ENDPOINT="${NGRH_ENDPOINT:-https://gamma.us-east-2.proxy.digito.migration-services.aws.dev/}"

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="ngrh-gamma-${AWS_ACCOUNT_ID}${ENV}"
INVOKER_ROLE="ngrh-invoker-gamma${ENV}"

# All resiliencehubv2 calls go to the gamma endpoint.
rh() {
  aws resiliencehubv2 "$@" --region "$NGRH_REGION" --endpoint-url "$NGRH_ENDPOINT" --no-cli-pager
}

log() { echo ">>> $*"; }

# ---------------------------------------------------------------------------
# Resource discovery is tag-based (key "service"). No app stack ARNs are
# resolved here — each service's input source is a `service` tag filter
# matching its own name plus "shared". Ensure the app stacks are deployed
# WITH the `service` tags before running (make deploy ENV=$ENV).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# S3 bucket for NGRH reports (mirror of NgrhBucket in ngrh.yaml).
# ---------------------------------------------------------------------------
if aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  log "Bucket $BUCKET already exists; skipping creation."
else
  log "Creating bucket $BUCKET in $NGRH_REGION..."
  aws s3api create-bucket --bucket "$BUCKET" --region "$NGRH_REGION" \
    --create-bucket-configuration "LocationConstraint=$NGRH_REGION" --no-cli-pager
fi

log "Configuring bucket $BUCKET (public access block, encryption, versioning, lifecycle, logging)..."
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'
aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration '{
  "Rules": [
    {
      "ID": "expire-reports-90d",
      "Status": "Enabled",
      "Filter": {"Prefix": "reports/"},
      "Expiration": {"Days": 90},
      "NoncurrentVersionExpiration": {"NoncurrentDays": 7}
    },
    {
      "ID": "abort-incomplete-uploads",
      "Status": "Enabled",
      "Filter": {"Prefix": ""},
      "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
      "Expiration": {"ExpiredObjectDeleteMarker": true}
    }
  ]
}'
# Server access logging to the bucket itself (LogFilePrefix: access-logs/ in the
# template). New buckets require a policy grant for the logging service principal.
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Sid\": \"S3ServerAccessLogs\",
    \"Effect\": \"Allow\",
    \"Principal\": {\"Service\": \"logging.s3.amazonaws.com\"},
    \"Action\": \"s3:PutObject\",
    \"Resource\": \"arn:aws:s3:::${BUCKET}/access-logs/*\",
    \"Condition\": {\"StringEquals\": {\"aws:SourceAccount\": \"${AWS_ACCOUNT_ID}\"}}
  }]
}"
aws s3api put-bucket-logging --bucket "$BUCKET" --bucket-logging-status "{
  \"LoggingEnabled\": {\"TargetBucket\": \"${BUCKET}\", \"TargetPrefix\": \"access-logs/\"}
}"

# ---------------------------------------------------------------------------
# Invoker IAM role (mirror of InvokerRole in ngrh.yaml).
# ---------------------------------------------------------------------------
# Trust both the prod and gamma NGRH service principals — the gamma stage
# assumes the invoker role as gamma.resiliencehub.amazonaws.com.
TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": [
      "resiliencehub.amazonaws.com",
      "gamma.resiliencehub.amazonaws.com"
    ]},
    "Action": "sts:AssumeRole"
  }]
}'
if aws iam get-role --role-name "$INVOKER_ROLE" >/dev/null 2>&1; then
  log "IAM role $INVOKER_ROLE already exists; updating trust policy."
  aws iam update-assume-role-policy --role-name "$INVOKER_ROLE" \
    --policy-document "$TRUST_POLICY"
else
  log "Creating IAM role $INVOKER_ROLE..."
  aws iam create-role --role-name "$INVOKER_ROLE" --no-cli-pager \
    --assume-role-policy-document "$TRUST_POLICY"
fi
aws iam attach-role-policy --role-name "$INVOKER_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/AWSResilienceHubV2AssessmentExecutionPolicy
aws iam put-role-policy --role-name "$INVOKER_ROLE" --policy-name ngrh-bucket-access \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": [\"s3:PutObject\", \"s3:GetObject\", \"s3:GetBucketLocation\", \"s3:ListBucket\"],
      \"Resource\": [\"arn:aws:s3:::${BUCKET}\", \"arn:aws:s3:::${BUCKET}/*\"]
    }]
  }"
# Fresh roles take ~60-90s to become assumable by NGRH; give a newly created
# role a head start before create-service references it.
ROLE_AGE=$(( $(date +%s) - $(date -d "$(aws iam get-role --role-name "$INVOKER_ROLE" --query 'Role.CreateDate' --output text)" +%s) ))
if [ "$ROLE_AGE" -lt 90 ]; then
  log "Role is ${ROLE_AGE}s old; waiting $((90 - ROLE_AGE))s for IAM propagation..."
  sleep $((90 - ROLE_AGE))
fi

# ---------------------------------------------------------------------------
# Policies (TierOnePolicy / TierTwoPolicy).
# ---------------------------------------------------------------------------
ensure_policy() { # name description slo rto rpo
  local name="$1" desc="$2" slo="$3" rto="$4" rpo="$5" arn
  arn=$(rh list-policies --query "policySummaries[?name=='${name}'].policyArn | [0]" --output text)
  if [ -n "$arn" ] && [ "$arn" != "None" ]; then
    log "Policy $name exists: $arn" >&2
  else
    log "Creating policy $name..." >&2
    arn=$(rh create-policy --name "$name" --description "$desc" \
      --availability-slo "target=$slo" \
      --multi-az '{}' \
      --multi-region "rtoInMinutes=$rto,rpoInMinutes=$rpo,disasterRecoveryApproach=ACTIVE_ACTIVE" \
      --query 'policy.policyArn' --output text)
  fi
  echo "$arn"
}

TIER1_ARN=$(ensure_policy "ngrh-tier1${ENV}" \
  "Tier-1 (revenue funnel): RTO 10m / RPO 0 -- zero data loss" 99.99 10 0)
TIER2_ARN=$(ensure_policy "ngrh-tier2${ENV}" \
  "Tier-2 (RPO-relaxed): RTO 10m / RPO 5m -- async or transient data (catalog, checkout)" 99.9 10 5)

# ---------------------------------------------------------------------------
# System.
# ---------------------------------------------------------------------------
SYSTEM_NAME="resilient-microservice${ENV}"
SYSTEM_ARN=$(rh list-systems --query "systemSummaries[?name=='${SYSTEM_NAME}'].systemArn | [0]" --output text)
if [ -n "$SYSTEM_ARN" ] && [ "$SYSTEM_ARN" != "None" ]; then
  log "System $SYSTEM_NAME exists: $SYSTEM_ARN"
else
  log "Creating system $SYSTEM_NAME..."
  SYSTEM_ARN=$(rh create-system --name "$SYSTEM_NAME" \
    --description "Multi-region active/active e-commerce microservice" \
    --query 'system.systemArn' --output text)
fi

# ---------------------------------------------------------------------------
# User Journeys.
# ---------------------------------------------------------------------------
ensure_journey() { # name description
  local name="$1" desc="$2" id
  id=$(rh list-user-journeys --system-arn "$SYSTEM_ARN" \
    --query "userJourneySummaries[?name=='${name}'].userJourneyId | [0]" --output text)
  if [ -n "$id" ] && [ "$id" != "None" ]; then
    log "Journey $name exists: $id" >&2
  else
    log "Creating user journey $name..." >&2
    id=$(rh create-user-journey --system-arn "$SYSTEM_ARN" --name "$name" \
      --description "$desc" --query 'userJourney.userJourneyId' --output text)
  fi
  echo "$id"
}

BROWSE_ID=$(ensure_journey "BrowseCatalog" "Home page, product listing, product detail")
CHECKOUT_ID=$(ensure_journey "CheckoutAndPlaceOrder" "Shipping -> delivery -> payment -> confirm -> order")
CART_ID=$(ensure_journey "ManageCart" "Add/remove items, view cart")
VIEWORDERS_ID=$(ensure_journey "ViewOrders" "Order history")

# ---------------------------------------------------------------------------
# Shared assertion texts (verbatim from ngrh.yaml). Tag-scoped discovery removes
# the need for negative "does not use X" assertions; only genuine
# compensating-control / strategy assertions are retained.
# ---------------------------------------------------------------------------
A_FAILOVER="Regional failover is automated via ARC Region Switch plan (mr-rs-plan-ngrh) which pre-scales ECS to 200% and shifts Route 53 DNS via data-plane health checks."
A_AZ="This architecture uses regional failover as the response to significant AZ impairments rather than AZ-level isolation mechanisms (zonal shift, zonal autoshift, ALB ATW). The ARC Region Switch plan pre-scales the surviving region to 200% and shifts DNS within the RTO, making AZ-scoped mitigations unnecessary for this design."
A_CATALOG_RPO="Catalog is read-only in-region; data is written by an external ingest process from an external system of record. On regional failover, any unreplicated catalog updates are recovered by re-running ingest, so there is no unrecoverable data loss. Aurora Global async replication is an accepted compensating control for the Tier-1 RPO target."
A_CATALOG_AURORA="Catalog uses Aurora Global Database as its only data store."
A_ORDERS_MQ="RabbitMQ is used only for post-commit event notification (OrderCreatedEvent published after DSQL transaction commits). The order of record is durably stored in Aurora DSQL (RPO 0) before any MQ publish. Lost MQ messages affect downstream notifications only, not order data integrity."
A_ASSETS_STATELESS="Assets is a stateless nginx container serving static files (images, CSS, JS). It has no data store, no session state, and no further dependencies."

# ---------------------------------------------------------------------------
# Services.
# ---------------------------------------------------------------------------
ensure_service() { # name policy_arn journey_ids_csv
  local name="$1" policy_arn="$2" journeys="$3" arn assoc
  arn=$(rh list-services --query "serviceSummaries[?name=='${name}'].serviceArn | [0]" --output text)
  if [ -n "$arn" ] && [ "$arn" != "None" ]; then
    log "Service $name exists: $arn" >&2
    echo "$arn"
    return
  fi
  log "Creating service $name..." >&2
  assoc=$(jq -n --arg sys "$SYSTEM_ARN" --arg ids "$journeys" \
    '[{systemArn: $sys, userJourneyIds: ($ids | split(","))}]')
  # NGRH may take several minutes to be able to assume a freshly created
  # invoker role; retry on that specific error.
  local tries=0 err
  while true; do
    err=$(mktemp)
    if arn=$(rh create-service --name "$name" \
        --regions "$PRIMARY_REGION" "$STANDBY_REGION" \
        --policy-arn "$policy_arn" \
        --dependency-discovery ENABLED \
        --permission-model "invokerRoleName=$INVOKER_ROLE" \
        --report-configuration "{\"reportOutputs\":[{\"s3\":{\"bucketPath\":\"${BUCKET}/reports/\",\"bucketOwner\":\"${AWS_ACCOUNT_ID}\"}}]}" \
        --associated-systems "$assoc" \
        --query 'service.serviceArn' --output text 2>"$err"); then
      rm -f "$err"
      break
    fi
    if grep -q "Cannot assume invoker role" "$err" && [ "$tries" -lt 20 ]; then
      tries=$((tries+1))
      log "  invoker role not assumable yet; retrying in 30s ($tries/20)..." >&2
      rm -f "$err"
      sleep 30
    else
      cat "$err" >&2; rm -f "$err"
      echo "ERROR: create-service $name failed" >&2
      exit 1
    fi
  done
  if [ -z "$arn" ] || [ "$arn" = "None" ]; then
    echo "ERROR: create-service $name returned no ARN" >&2
    exit 1
  fi
  echo "$arn"
}

add_tag_input_source() { # service_arn service_name
  local svc="$1" name="$2"
  # Idempotent: skip if a resourceTags input source already covers this service value.
  if rh list-input-sources --service-arn "$svc" --output json | jq -e --arg n "$name" \
      '.inputSourceSummaries[]? | select((.resourceConfiguration.resourceTags // [])[]? | (.key == "service") and ((.values // []) | index($n)))' >/dev/null; then
    return
  fi
  log "  input source: tag service in [$name, shared]"
  rh create-input-source --service-arn "$svc" \
    --resource-configuration "{\"resourceTags\":[{\"key\":\"service\",\"values\":[\"$name\",\"shared\"]}]}" >/dev/null
}

add_assertion() { # service_arn text
  local svc="$1" text="$2"
  if rh list-assertions --service-arn "$svc" --output json | jq -e --arg t "$text" \
      '.assertions[]? | select(.text == $t)' >/dev/null; then
    return
  fi
  log "  assertion: ${text:0:60}..."
  rh create-assertion --service-arn "$svc" --text "$text" >/dev/null
}

# --- ui: all four journeys, Tier-1 ---
UI_ARN=$(ensure_service "ui${ENV}" "$TIER1_ARN" "$BROWSE_ID,$CHECKOUT_ID,$CART_ID,$VIEWORDERS_ID")
add_tag_input_source "$UI_ARN" "ui"
for a in "$A_FAILOVER" "$A_AZ"; do
  add_assertion "$UI_ARN" "$a"
done

# --- catalog: Browse journey, Tier-2 ---
CATALOG_ARN=$(ensure_service "catalog${ENV}" "$TIER2_ARN" "$BROWSE_ID")
add_tag_input_source "$CATALOG_ARN" "catalog"
for a in "$A_CATALOG_RPO" "$A_CATALOG_AURORA" "$A_FAILOVER" "$A_AZ"; do
  add_assertion "$CATALOG_ARN" "$a"
done

# --- cart: Cart + Checkout journeys, Tier-1 ---
CART_ARN=$(ensure_service "cart${ENV}" "$TIER1_ARN" "$CART_ID,$CHECKOUT_ID")
add_tag_input_source "$CART_ARN" "cart"
for a in "$A_FAILOVER" "$A_AZ"; do
  add_assertion "$CART_ARN" "$a"
done

# --- checkout: Checkout journey, Tier-2 ---
CHECKOUT_ARN=$(ensure_service "checkout${ENV}" "$TIER2_ARN" "$CHECKOUT_ID")
add_tag_input_source "$CHECKOUT_ARN" "checkout"
for a in "$A_FAILOVER" "$A_AZ"; do
  add_assertion "$CHECKOUT_ARN" "$a"
done

# --- orders: Checkout + ViewOrders journeys, Tier-1 ---
ORDERS_ARN=$(ensure_service "orders${ENV}" "$TIER1_ARN" "$CHECKOUT_ID,$VIEWORDERS_ID")
add_tag_input_source "$ORDERS_ARN" "orders"
for a in "$A_FAILOVER" "$A_AZ" "$A_ORDERS_MQ"; do
  add_assertion "$ORDERS_ARN" "$a"
done

# --- assets: Browse journey, Tier-2 ---
ASSETS_ARN=$(ensure_service "assets${ENV}" "$TIER2_ARN" "$BROWSE_ID")
add_tag_input_source "$ASSETS_ARN" "assets"
for a in "$A_ASSETS_STATELESS" "$A_FAILOVER" "$A_AZ"; do
  add_assertion "$ASSETS_ARN" "$a"
done

# ---------------------------------------------------------------------------
# Summary (same shape as the CFN stack outputs).
# ---------------------------------------------------------------------------
echo ""
echo "=== NGRH gamma model created ==="
echo "SystemArn:        $SYSTEM_ARN"
echo "TierOnePolicyArn: $TIER1_ARN"
echo "TierTwoPolicyArn: $TIER2_ARN"
echo "NgrhBucketName:   $BUCKET"
echo "ServiceArns:      $UI_ARN,$CATALOG_ARN,$CART_ARN,$CHECKOUT_ARN,$ORDERS_ARN,$ASSETS_ARN"
echo ""
echo "Run assessments with:"
echo "  aws resiliencehubv2 start-failure-mode-assessment --service-arn <arn> \\"
echo "    --region $NGRH_REGION --endpoint-url $NGRH_ENDPOINT"
