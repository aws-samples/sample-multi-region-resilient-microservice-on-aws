import { github } from 'projen';
import { GitHubProject } from 'projen/lib/github';

// GitHubProject is intentionally chosen over NodeProject — this repo isn't a
// node package, it just happens to use projen + ts-node to manage workflows.
// package.json is hand-maintained ('default' script runs ts-node directly).
const project = new GitHubProject({
  name: 'sample-multi-region-resilient-microservice-on-aws',
  githubOptions: {
    pullRequestLintOptions: {
      semanticTitleOptions: {
        types: ['feat', 'fix', 'chore', 'docs', 'refactor', 'test', 'ci'],
      },
    },
  },
});

project.gitignore.exclude(
  '.idea',
  '.vscode',
  '**/.DS_Store',
  '**/__pycache__/',
  '*.pyc',
  '**/target/',
  '**/dist/',
  '**/package/python/',
  '**/bucket-name.txt',
  '**/out.yaml',
  '/deployment/database/crdr-reconciliation/src/function/package/python/',
  '/venv/',
  '**/ash_output/**',
  '**/ash_output',
  '**/aggregated_results*',
  'deployment/.build-done/',
  '.ash/',
  '.claude/',
  '.kiro/',
  'scan-report.json',
  'cdk.out/',
  '.env',
  '.env.*',
  'cdk.context.json',
);

new github.Stale(project.github!, {
  pullRequest: { daysBeforeStale: 30, daysBeforeClose: 14 },
  issues: { daysBeforeStale: 60, daysBeforeClose: 30 },
});

project.github!.addDependabot({
  scheduleInterval: github.DependabotScheduleInterval.WEEKLY,
  ignoreProjen: false,
});

const PYTHON_VERSION = '3.12';
const E2E_PATHS = ['source/**', 'deployment/**', '.github/workflows/e2e.yml'];
const PR_VALIDATION_PATHS = ['source/**', 'deployment/**', '.github/workflows/pr-validation.yml'];

const prValidation = project.github!.addWorkflow('pr-validation');
prValidation.on({
  pullRequest: { branches: ['main'], paths: PR_VALIDATION_PATHS },
  workflowDispatch: {},
});
prValidation.addJobs({
  'cfn-lint': {
    runsOn: ['ubuntu-latest'],
    permissions: { contents: github.workflows.JobPermission.READ },
    steps: [
      { uses: 'actions/checkout@v4' },
      { uses: 'actions/setup-python@v5', with: { 'python-version': PYTHON_VERSION } },
      { name: 'Install cfn-lint', run: 'pip install cfn-lint' },
      {
        name: 'Run cfn-lint',
        run: 'cfn-lint deployment/**/*.yaml deployment/**/*.yml --ignore-templates deployment/mirror-sidecar-buildspec.yml --ignore-checks W3005,W2001,W8001,W1020,W7001,W1031,E1029,E3030',
      },
    ],
  },
  checkov: {
    runsOn: ['ubuntu-latest'],
    permissions: { contents: github.workflows.JobPermission.READ },
    steps: [
      { uses: 'actions/checkout@v4' },
      {
        name: 'Run checkov',
        uses: 'bridgecrewio/checkov-action@v12',
        with: {
          directory: 'deployment',
          framework: 'cloudformation',
          config_file: '.checkov.yaml',
          skip_path: 'deployment/github-oidc-role.yaml,deployment/mirror-sidecar-buildspec.yml',
          quiet: 'true',
        },
      },
    ],
  },
  'cfn-nag': {
    runsOn: ['ubuntu-latest'],
    permissions: { contents: github.workflows.JobPermission.READ },
    steps: [
      { uses: 'actions/checkout@v4' },
      {
        name: 'Run cfn-nag',
        uses: 'stelligent/cfn_nag@master',
        with: { input_path: 'deployment', extra_args: '--fail-on-warnings false' },
      },
    ],
  },
  pytest: {
    runsOn: ['ubuntu-latest'],
    permissions: { contents: github.workflows.JobPermission.READ },
    steps: [
      { uses: 'actions/checkout@v4' },
      { uses: 'actions/setup-python@v5', with: { 'python-version': PYTHON_VERSION } },
      { name: 'Install test deps', run: 'pip install pytest pyyaml' },
      { name: 'Run tests', run: 'pytest tests/' },
    ],
  },
  trivy: {
    runsOn: ['ubuntu-latest'],
    permissions: { contents: github.workflows.JobPermission.READ },
    steps: [
      { uses: 'actions/checkout@v4' },
      {
        name: 'Install trivy',
        run: [
          'TRIVY_VERSION=0.69.3',
          'curl -fsSL "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" \\',
          '  | sudo tar -xzC /usr/local/bin trivy',
          'trivy --version',
        ].join('\n'),
      },
      {
        name: 'Scan filesystem for vulnerabilities',
        run: 'trivy fs source --severity HIGH,CRITICAL --exit-code 1 --ignore-unfixed --no-progress',
      },
    ],
  },
});

// `concurrency` on GithubWorkflow is read-only after construction; pass it
// via the constructor options instead. addWorkflow() in this projen version
// doesn't take options, so instantiate GithubWorkflow directly.
const e2e = new github.GithubWorkflow(project.github!, 'e2e', {
  limitConcurrency: true,
  concurrencyOptions: {
    group: 'e2e-${{ github.ref }}',
    cancelInProgress: true,
  },
});
e2e.on({
  push: { branches: ['main'], paths: E2E_PATHS },
  pullRequest: { branches: ['main'], paths: E2E_PATHS },
  workflowDispatch: {},
});
e2e.addJob('e2e', {
  name: 'Build, Deploy, Test, Teardown',
  runsOn: ['ubuntu-latest'],
  timeoutMinutes: 300,
  permissions: {
    idToken: github.workflows.JobPermission.WRITE,
    contents: github.workflows.JobPermission.READ,
  },
  env: {
    AWS_REGION: 'us-east-1',
    STANDBY_REGION: 'us-west-2',
    // ENV is set from a short (7-char) git sha in the first step below. The
    // full sha pushes Aurora cluster identifiers past the 63-char limit now
    // enforced by CloudFormation's pre-provisioning property validation.
  },
  steps: [
    { name: 'Checkout', uses: 'actions/checkout@v4' },
    {
      name: 'Set ENV to short sha',
      run: 'echo "ENV=-${GITHUB_SHA:0:7}" >> $GITHUB_ENV',
    },
    {
      name: 'Configure AWS credentials',
      uses: 'aws-actions/configure-aws-credentials@v4',
      with: {
        'role-to-assume': '${{ secrets.E2E_ROLE_ARN }}',
        'aws-region': '${{ env.AWS_REGION }}',
        'role-duration-seconds': 28800,
      },
    },
    {
      name: 'Install dependencies',
      run: [
        'sudo apt-get update -qq && sudo apt-get install -y -qq jq zip',
        'pip3 install boto3 --quiet',
      ].join('\n'),
    },
    {
      name: 'Build images (both regions)',
      workingDirectory: 'deployment',
      run: 'make build-images ENV=${{ env.ENV }}',
    },
    {
      name: 'Deploy (full multi-region)',
      workingDirectory: 'deployment',
      run: 'make deploy ENV=${{ env.ENV }}',
    },
    {
      name: 'Capture failure diagnostics',
      if: 'failure()',
      run: [
        'set +e',
        'BAD_STATUSES="CREATE_FAILED ROLLBACK_IN_PROGRESS ROLLBACK_COMPLETE ROLLBACK_FAILED UPDATE_ROLLBACK_COMPLETE UPDATE_ROLLBACK_FAILED REVIEW_IN_PROGRESS CREATE_IN_PROGRESS"',
        'echo "::group::Failed and in-progress stacks (both regions)"',
        'for region in ${{ env.AWS_REGION }} ${{ env.STANDBY_REGION }}; do',
        '  echo "=== $region ==="',
        '  aws cloudformation list-stacks --region "$region" --no-cli-pager \\',
        '    --query "StackSummaries[?StackStatus==\'CREATE_FAILED\' || StackStatus==\'ROLLBACK_IN_PROGRESS\' || StackStatus==\'ROLLBACK_COMPLETE\' || StackStatus==\'ROLLBACK_FAILED\' || StackStatus==\'UPDATE_ROLLBACK_COMPLETE\' || StackStatus==\'UPDATE_ROLLBACK_FAILED\' || StackStatus==\'REVIEW_IN_PROGRESS\' || StackStatus==\'CREATE_IN_PROGRESS\'].[StackName, StackStatus]" \\',
        '    --output text',
        'done',
        'echo "::endgroup::"',
        '',
        '# Enumerate all failed/rollback stacks for our ENV suffix and pull detailed diagnostics for each.',
        'echo "::group::Per-stack diagnostics for this run (ENV=${{ env.ENV }})"',
        'for region in ${{ env.AWS_REGION }} ${{ env.STANDBY_REGION }}; do',
        '  echo "=== $region ==="',
        '  STACKS=$(aws cloudformation list-stacks --region "$region" --no-cli-pager \\',
        '    --query "StackSummaries[?(StackStatus==\'CREATE_FAILED\' || StackStatus==\'ROLLBACK_IN_PROGRESS\' || StackStatus==\'ROLLBACK_COMPLETE\' || StackStatus==\'ROLLBACK_FAILED\' || StackStatus==\'UPDATE_ROLLBACK_COMPLETE\' || StackStatus==\'UPDATE_ROLLBACK_FAILED\' || StackStatus==\'REVIEW_IN_PROGRESS\') && contains(StackName, \'${{ env.ENV }}\')].StackName" \\',
        '    --output text)',
        '  for stack in $STACKS; do',
        '    echo "--- $stack ---"',
        '    # Failed stack events (show only failure/rollback lines).',
        '    aws cloudformation describe-stack-events --stack-name "$stack" --region "$region" --no-cli-pager \\',
        '      --max-items 40 --query "StackEvents[?contains(ResourceStatus, \'FAILED\') || contains(ResourceStatus, \'ROLLBACK\')].[Timestamp,LogicalResourceId,ResourceStatus,ResourceStatusReason]" --output text 2>&1 | head -40 || true',
        '    # Pre-provisioning validation failures surface via the latest failed change set.',
        '    CS=$(aws cloudformation list-change-sets --stack-name "$stack" --region "$region" --no-cli-pager --query "Summaries[?Status==\'FAILED\']|[0].ChangeSetId" --output text 2>/dev/null)',
        '    if [ -n "$CS" ] && [ "$CS" != "None" ]; then',
        '      echo "  ... describe-events (pre-provisioning validation) ..."',
        '      aws cloudformation describe-events --change-set-name "$CS" --region "$region" --no-cli-pager --filters FailedEvents=true --output json 2>&1 | head -80 || true',
        '    fi',
        '  done',
        'done',
        'echo "::endgroup::"',
      ].join('\n'),
    },
    {
      name: 'Smoke test',
      workingDirectory: 'deployment',
      run: [
        '# The application ALB is internal (only reachable from inside the',
        '# VPC), so the GitHub Actions runner cannot curl it. Instead, rely',
        '# on CloudWatch Synthetics canaries — they run inside the VPC and',
        '# are the actual user-facing health signal. Check that at least',
        '# one canary per region has run and reported PASSED within the',
        '# last 15 minutes.',
        'for region in ${{ env.AWS_REGION }} ${{ env.STANDBY_REGION }}; do',
        '  echo "===== $region ====="',
        '  CANARIES=$(aws synthetics describe-canaries --region "$region" --no-cli-pager \\',
        '    --query "Canaries[?contains(Name, \'${{ env.ENV }}\')].Name" --output text)',
        '  if [ -z "$CANARIES" ]; then',
        '    echo "$region: no canaries found for ENV=${{ env.ENV }}"',
        '    exit 1',
        '  fi',
        '  echo "Canaries: $CANARIES"',
        '  # Wait up to 20 min for at least one canary to report PASSED.',
        '  passed=false',
        '  for i in $(seq 1 40); do',
        '    for c in $CANARIES; do',
        '      STATE=$(aws synthetics get-canary-runs --name "$c" --region "$region" --no-cli-pager \\',
        '        --max-results 1 --query "CanaryRuns[0].Status.State" --output text 2>/dev/null)',
        '      if [ "$STATE" = "PASSED" ]; then',
        '        echo "  $c reported PASSED"',
        '        passed=true',
        '        break 2',
        '      fi',
        '    done',
        '    echo "  Attempt $i ($region): no canary PASSED yet, retrying in 30s..."',
        '    sleep 30',
        '  done',
        '  if [ "$passed" != "true" ]; then',
        '    echo "$region had no PASSED canary run after 20 minutes"',
        '    exit 1',
        '  fi',
        'done',
      ].join('\n'),
    },
    {
      name: 'Teardown',
      if: 'always()',
      workingDirectory: 'deployment',
      run: [
        'echo "Cleaning up e2e environment..."',
        'make destroy-all ENV=${{ env.ENV }} || true',
        '',
        '# Belt-and-suspenders: ensure VPC stacks are gone in both regions.',
        'for region in ${{ env.AWS_REGION }} ${{ env.STANDBY_REGION }}; do',
        '  aws cloudformation delete-stack --stack-name baseInfra${{ env.ENV }} --region "$region" || true',
        '  aws cloudformation wait stack-delete-complete --stack-name baseInfra${{ env.ENV }} --region "$region" || true',
        'done',
        '# baseVpc: standby first (peering connection lives on standby side).',
        'aws cloudformation delete-stack --stack-name baseVpc${{ env.ENV }} --region ${{ env.STANDBY_REGION }} || true',
        'aws cloudformation wait stack-delete-complete --stack-name baseVpc${{ env.ENV }} --region ${{ env.STANDBY_REGION }} || true',
        'aws cloudformation delete-stack --stack-name baseVpc${{ env.ENV }} --region ${{ env.AWS_REGION }} || true',
        'aws cloudformation wait stack-delete-complete --stack-name baseVpc${{ env.ENV }} --region ${{ env.AWS_REGION }} || true',
        '',
        '# Delete ECR repos in both regions.',
        'for region in ${{ env.AWS_REGION }} ${{ env.STANDBY_REGION }}; do',
        '  for repo in catalog checkout ui carts assets orders cloudwatch-agent adot-autoinstrumentation-java adot-autoinstrumentation-node; do',
        '    aws ecr delete-repository --force --repository-name ${repo}${{ env.ENV }} --region "$region" --no-cli-pager 2>/dev/null || true',
        '  done',
        'done',
        '',
        'echo "Teardown complete"',
      ].join('\n'),
    },
  ],
});

project.synth();
