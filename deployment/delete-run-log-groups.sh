#!/bin/bash
# Delete the CloudWatch log groups one e2e run leaves behind in one region.
#
# Lambda (the Synthetics canaries and the custom resources), CodeBuild, ECS Container
# Insights, RDS, Amazon MQ and the services create their log groups outside CloudFormation,
# so deleting every stack of a deployment leaves them, and none of them expires. Run
# 37485943263 left 28, 15 in us-east-1 and 13 in us-west-2; the CI account held 3,895 and
# 2,374 log groups by then. The first version of this helper found 15 and 13 of a run's
# 23 and 21, because two kinds of name do not carry the commit sha as a whole token.
#
# A group is this run's if it is one of these (each is matched exactly, never guessed):
#
# 1. A name under one of the prefixes the services write under that carries the suffix as
#    a whole token (followed by "-", "/" or the end of the name): the groups of the CodeBuild
#    projects, the custom-resource functions, Container Insights, RDS and the services.
#
# 2. A canary's function, /aws/lambda/cwsyn-<name>-<uuid>. Lambda limits a function name
#    to 64 characters, and Synthetics builds it as "cwsyn-" + the canary name cut to 21
#    characters + "-" + a 36-character uuid, so lcl-rgnl-catalog-3988bd1 becomes
#    cwsyn-lcl-rgnl-catalog-3988-<uuid> and the sha is cut with it: five of the twelve
#    canaries lose part of it. Each canary's cut name is computed from CANARY_NAMES, the
#    names canaries.yaml declares (a test holds the two equal), and only groups under
#    that exact prefix followed by a uuid are taken. The cut can't tell apart two runs
#    whose shas start alike; the other run's groups are leftovers of a finished run, as the
#    e2e-deploy concurrency group runs one at a time.
#
# 3. An Amazon MQ broker's groups, /aws/amazonmq/broker/<broker id>/<log> (general,
#    federation and connection for RabbitMQ). They carry the broker's id and nothing of the
#    run, and the broker is gone by the time this runs, so the caller lists the ids first
#    (the e2e step "Record the message brokers of this run") and passes the file as the
#    third argument. A file that holds anything but broker ids is refused, and one that is
#    missing is reported; without the argument the helper leaves every Amazon MQ group alone.
#
# Anything else, a user's -dev environment included, is refused or left alone rather than
# guessed at. The suffix must be the commit-sha form the e2e workflow uses (-3c7091f).
#
# Why this is a script that FAILS rather than a best-effort loop: delete-cloudmap-namespace.sh
# was once a loop that sent every error to /dev/null, and a missing permission then read as
# a clean teardown for six weeks. A group this helper cannot list or delete is a leak, and a
# leak is something the caller has to see: exit 1, with the API error left on stderr.
#
# The listings are filtered by the service, by substring (rule 1) or by prefix (rules 2 and
# 3), so the account's thousands of groups are never listed; the two filters can't be
# combined in one request.
#
# Usage: delete-run-log-groups.sh <env-suffix> <region> [<file of broker ids>]
set -uo pipefail

USAGE='usage: delete-run-log-groups.sh <env-suffix> <region> [<file of broker ids>]'
ENV_SUFFIX=${1:?$USAGE}
REGION=${2:?$USAGE}
BROKER_FILE=${3:-}

# Hex only, so the suffix can go into a regular expression as it is.
if [[ ! "$ENV_SUFFIX" =~ ^-[0-9a-f]{7,40}$ ]]; then
    echo "$REGION: refusing to delete log groups for ENV '$ENV_SUFFIX': it is not a commit sha such as -3c7091f, so its names would not identify one run" >&2
    exit 2
fi

export AWS_MAX_ATTEMPTS=${AWS_MAX_ATTEMPTS:-8}

GROUP_PREFIXES='^/aws/(codebuild|lambda|service-events)/|^/aws/ecs/containerinsights/|^/aws/rds/cluster/'
SUFFIX_TOKEN="${ENV_SUFFIX}([-/]|\$)"

# The canaries canaries.yaml declares, without their ${Env} suffix.
CANARY_NAMES=(
    lcl-rgnl-home lcl-rgnl-cart lcl-rgnl-catalog lcl-rgnl-orders
    rmt-rgnl-home rmt-rgnl-cart rmt-rgnl-catalog rmt-rgnl-orders
    global-home global-cart global-catalog global-orders
)
CANARY_NAME_KEPT=21          # 64 - len("cwsyn-") - len("-") - len(uuid)
UUID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
BROKER_ID_RE='^b-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
NL=$'\n'

# The broker ids first, so a file that is wrong refuses before any API call is made.
brokers=()
broker_file_missing=0
if [ -n "$BROKER_FILE" ]; then
    if [ -r "$BROKER_FILE" ]; then
        while IFS= read -r id; do
            [ -z "$id" ] && continue
            if [[ ! "$id" =~ $BROKER_ID_RE ]]; then
                echo "$REGION: refusing to delete log groups: $BROKER_FILE holds '$id', which is not an Amazon MQ broker id such as b-3d43b3fe-0000-4000-8000-000000000000" >&2
                exit 2
            fi
            brokers+=("$id")
        done < <({ cat "$BROKER_FILE"; echo; } | tr -s '[:space:]' '\n')
    else
        broker_file_missing=1
    fi
fi

list_failed=0
mine_list=""
other_list=""
LISTED=""

# Puts the names of the log groups the service matches, one per line, in LISTED. A listing
# that fails is a leak the caller must see, and the ones after it would fail the same way,
# so the caller stops listing and deletes what it already has.
#
# The filter goes in as one word, --flag=value. The suffix starts with a dash (-a1802e5), and
# the AWS CLI reads a separate value that starts with a dash as the next option, unless it
# looks like a negative number: "argument --log-group-name-pattern: expected one argument",
# exit 252. Run 37679210579 listed nothing in either Region for that reason, and its log
# groups stayed.
list_groups() {
    local label=$1 flag=$2 value=$3 raw
    LISTED=""
    if ! raw=$(aws logs describe-log-groups --region "$REGION" "${flag}=${value}" \
            --query 'logGroups[].logGroupName' --output text); then
        echo "$REGION: could not list the log groups of $label (see the error above); they may be leaking"
        return 1
    fi
    # The CLI prints one page per line, tab-separated; "None" is its word for an empty result.
    LISTED=$(printf '%s\n' "$raw" | tr '\t' '\n' | grep -v -e '^$' -e '^None$' || true)
}

# 1. By the suffix, as a substring the service matches.
if list_groups "$ENV_SUFFIX" --log-group-name-pattern "$ENV_SUFFIX"; then
    while IFS= read -r name; do
        [ -z "$name" ] && continue
        if [[ "$name" =~ $GROUP_PREFIXES && "$name" =~ $SUFFIX_TOKEN ]]; then
            mine_list+="$name$NL"
        else
            other_list+="$name$NL"
        fi
    done <<< "$LISTED"
else
    list_failed=1
fi

# 2. By each canary's cut name.
if [ "$list_failed" -eq 0 ]; then
    for base in "${CANARY_NAMES[@]}"; do
        full="${base}${ENV_SUFFIX}"
        prefix="/aws/lambda/cwsyn-${full:0:$CANARY_NAME_KEPT}-"
        if ! list_groups "canary $base" --log-group-name-prefix "$prefix"; then
            list_failed=1
            break
        fi
        while IFS= read -r name; do
            [ -z "$name" ] && continue
            if [[ "$name" == "$prefix"* && "${name#"$prefix"}" =~ $UUID_RE ]]; then
                mine_list+="$name$NL"
            else
                other_list+="$name$NL"
            fi
        done <<< "$LISTED"
    done
fi

# 3. By each recorded broker id.
if [ "$list_failed" -eq 0 ]; then
    for id in ${brokers[@]+"${brokers[@]}"}; do
        prefix="/aws/amazonmq/broker/${id}/"
        if ! list_groups "Amazon MQ broker $id" --log-group-name-prefix "$prefix"; then
            list_failed=1
            break
        fi
        while IFS= read -r name; do
            [ -z "$name" ] && continue
            if [[ "$name" == "$prefix"* && -n "${name#"$prefix"}" && "${name#"$prefix"}" != */* ]]; then
                mine_list+="$name$NL"
            else
                other_list+="$name$NL"
            fi
        done <<< "$LISTED"
    done
fi

mine_text=$(printf '%s' "$mine_list" | sort -u)
mine=()
while IFS= read -r name; do
    [ -n "$name" ] && mine+=("$name")
done <<< "$mine_text"

# A name one rule refused and another took is this run's, not left alone.
while IFS= read -r name; do
    [ -z "$name" ] && continue
    case "$NL$mine_text$NL" in
        *"$NL$name$NL"*) ;;
        *) echo "$REGION: left alone, outside the prefixes this helper deletes under: $name" ;;
    esac
done < <(printf '%s' "$other_list" | sort -u)

rc=$list_failed
if [ "$broker_file_missing" -eq 1 ]; then
    echo "$REGION: the broker list $BROKER_FILE does not exist, so the Amazon MQ log groups of $ENV_SUFFIX may be leaking"
    rc=1
elif [ -n "$BROKER_FILE" ] && [ "${#brokers[@]}" -eq 0 ]; then
    echo "$REGION: no Amazon MQ brokers were recorded for $ENV_SUFFIX"
fi

if [ "${#mine[@]}" -eq 0 ]; then
    if [ "$rc" -eq 0 ]; then
        echo "$REGION: no log groups of $ENV_SUFFIX to delete"
    fi
    exit $rc
fi

deleted=0
for name in "${mine[@]}"; do
    if err=$(aws logs delete-log-group --region "$REGION" --log-group-name "$name" 2>&1 >/dev/null); then
        deleted=$((deleted + 1))
        echo "$REGION: deleted $name"
    elif [[ "$err" == *ResourceNotFoundException* ]]; then
        # Gone already: a service removed it, or an earlier pass did.
        echo "$REGION: $name was already gone"
    else
        echo "$err" >&2
        echo "$REGION: failed to delete log group $name"
        rc=1
    fi
done

echo "$REGION: deleted $deleted of ${#mine[@]} log groups of $ENV_SUFFIX"
exit $rc
