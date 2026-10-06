#!/bin/bash
# Delete the CloudWatch log groups one e2e run leaves behind in one region.
#
# Lambda (the Synthetics canaries and the custom resources), CodeBuild, ECS Container
# Insights, RDS and the services create their log groups outside CloudFormation, so
# deleting every stack of a deployment leaves them, and none of them expires. Run
# 37485943263 left 15 in us-east-1 and 13 in us-west-2, exactly as the run before it did;
# the CI account held 3,895 and 2,374 log groups by then.
#
# This touches only one run's own groups. The suffix must be the commit-sha form the e2e
# workflow uses (-3c7091f), and a group goes only if its name starts with one of the
# prefixes those services write under and carries the suffix as a whole token (followed by
# "-", "/" or the end of the name). Anything else, a user's -dev environment included, is
# refused or left alone rather than guessed at.
#
# Why this is a script that FAILS rather than a best-effort loop: delete-cloudmap-namespace.sh
# was once a loop that sent every error to /dev/null, and a missing permission then read as
# a clean teardown for six weeks. A group this helper cannot delete is a leak, and a leak is
# something the caller has to see: exit 1, with the API error left on stderr.
#
# The service-side name pattern is a case-sensitive substring match. It spares listing every
# log group in the account, which the plain listing takes dozens of throttled calls to do.
#
# Usage: delete-run-log-groups.sh <env-suffix> <region>
set -uo pipefail

ENV_SUFFIX=${1:?usage: delete-run-log-groups.sh <env-suffix> <region>}
REGION=${2:?usage: delete-run-log-groups.sh <env-suffix> <region>}

# Hex only, so the suffix can go into a regular expression as it is.
if [[ ! "$ENV_SUFFIX" =~ ^-[0-9a-f]{7,40}$ ]]; then
    echo "$REGION: refusing to delete log groups for ENV '$ENV_SUFFIX': it is not a commit sha such as -3c7091f, so its names would not identify one run" >&2
    exit 2
fi

export AWS_MAX_ATTEMPTS=${AWS_MAX_ATTEMPTS:-8}

GROUP_PREFIXES='^/aws/(codebuild|lambda|service-events)/|^/aws/ecs/containerinsights/|^/aws/rds/cluster/'
SUFFIX_TOKEN="${ENV_SUFFIX}([-/]|\$)"

if ! listed=$(aws logs describe-log-groups --region "$REGION" --log-group-name-pattern "$ENV_SUFFIX" \
        --query 'logGroups[].logGroupName' --output text); then
    echo "$REGION: could not list the log groups of $ENV_SUFFIX (see the error above); they may be leaking"
    exit 1
fi

mine=()
other=()
# The CLI prints one page per line, tab-separated; "None" is its word for an empty result.
while IFS= read -r name; do
    if [ -z "$name" ] || [ "$name" = "None" ]; then
        continue
    fi
    if [[ "$name" =~ $GROUP_PREFIXES && "$name" =~ $SUFFIX_TOKEN ]]; then
        mine+=("$name")
    else
        other+=("$name")
    fi
done < <(printf '%s\n' "$listed" | tr '\t' '\n')

for name in ${other[@]+"${other[@]}"}; do
    echo "$REGION: left alone, outside the prefixes this helper deletes under: $name"
done

if [ "${#mine[@]}" -eq 0 ]; then
    echo "$REGION: no log groups of $ENV_SUFFIX to delete"
    exit 0
fi

rc=0
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
