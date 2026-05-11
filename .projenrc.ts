import { github, javascript } from 'projen';
import { GitHubProject } from 'projen/lib/github';

const project = new GitHubProject({
  name: 'sample-multi-region-resilient-microservice-on-aws',
  defaultReleaseBranch: 'main',
  projenrcTs: true,
  projenrcJs: false,
  devDeps: ['projen', 'ts-node', 'typescript'],
  packageManager: javascript.NodePackageManager.NPM,
  pullRequestLintOptions: {
    semanticTitleOptions: {
      types: ['feat', 'fix', 'chore', 'docs', 'refactor', 'test', 'ci'],
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
        run: 'cfn-lint deployment/**/*.yaml deployment/**/*.yml --ignore-templates deployment/mirror-sidecar-buildspec.yml',
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
        name: 'Scan filesystem for vulnerabilities',
        uses: 'aquasecurity/trivy-action@v0.28.0',
        with: {
          'scan-type': 'fs',
          'scan-ref': 'source',
          'severity': 'HIGH,CRITICAL',
          'exit-code': '1',
          'ignore-unfixed': 'true',
        },
      },
    ],
  },
});

const e2e = project.github!.addWorkflow('e2e');
e2e.on({
  push: { branches: ['main'], paths: E2E_PATHS },
  pullRequest: { branches: ['main'], paths: E2E_PATHS },
  workflowDispatch: {},
});
e2e.concurrency = { group: 'e2e-${{ github.ref }}', cancelInProgress: true };
e2e.addJob('e2e', {
  name: 'Build, Deploy, Test, Teardown',
  runsOn: ['ubuntu-latest'],
  timeoutMinutes: 90,
  permissions: {
    idToken: github.workflows.JobPermission.WRITE,
    contents: github.workflows.JobPermission.READ,
  },
  env: {
    AWS_REGION: 'us-east-1',
    ENV: '-${{ github.sha }}',
  },
  steps: [
    { name: 'Checkout', uses: 'actions/checkout@v4' },
    {
      name: 'Configure AWS credentials',
      uses: 'aws-actions/configure-aws-credentials@v4',
      with: {
        'role-to-assume': '${{ secrets.E2E_ROLE_ARN }}',
        'aws-region': '${{ env.AWS_REGION }}',
        'role-duration-seconds': 5400,
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
      name: 'Build images',
      workingDirectory: 'deployment',
      run: 'make build-images ENV=${{ env.ENV }} PRIMARY_ONLY=true',
    },
    {
      name: 'Deploy (primary region only)',
      workingDirectory: 'deployment',
      run: 'make deploy-primary ENV=${{ env.ENV }} PRIMARY_ONLY=true',
    },
    {
      name: 'Smoke test',
      workingDirectory: 'deployment',
      run: [
        'echo "Waiting for ECS services to stabilize..."',
        'aws ecs wait services-stable \\',
        '  --cluster apps-EcsCluster${{ env.ENV }} \\',
        '  --services checkout${{ env.ENV }} ui${{ env.ENV }} catalog${{ env.ENV }} carts${{ env.ENV }} orders${{ env.ENV }} \\',
        '  --region ${{ env.AWS_REGION }} || true',
        '',
        'ALB_DNS=$(aws cloudformation describe-stacks \\',
        '  --stack-name apps${{ env.ENV }} --region ${{ env.AWS_REGION }} \\',
        '  --query "Stacks[0].Outputs[?OutputKey==\'LoadBalancerDNS\'].OutputValue" --output text)',
        '',
        'echo "Testing ALB endpoint: $ALB_DNS"',
        'for i in $(seq 1 30); do',
        '  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://$ALB_DNS" || echo "000")',
        '  if [ "$HTTP_CODE" = "200" ]; then',
        '    echo "Application responding (HTTP 200)"',
        '    exit 0',
        '  fi',
        '  echo "  Attempt $i: HTTP $HTTP_CODE - retrying in 10s..."',
        '  sleep 10',
        'done',
        'echo "Application did not respond after 5 minutes"',
        'exit 1',
      ].join('\n'),
    },
    {
      name: 'Teardown',
      if: 'always()',
      workingDirectory: 'deployment',
      run: [
        'echo "Cleaning up e2e environment..."',
        'make destroy-apps-primary ENV=${{ env.ENV }} PRIMARY_ONLY=true || true',
        'make destroy-databases-primary ENV=${{ env.ENV }} PRIMARY_ONLY=true || true',
        'make destroy-codebuild ENV=${{ env.ENV }} || true',
        '',
        '# Delete VPC stacks',
        'aws cloudformation delete-stack --stack-name baseInfra${{ env.ENV }} --region ${{ env.AWS_REGION }} || true',
        'aws cloudformation wait stack-delete-complete --stack-name baseInfra${{ env.ENV }} --region ${{ env.AWS_REGION }} || true',
        'aws cloudformation delete-stack --stack-name baseVpc${{ env.ENV }} --region ${{ env.AWS_REGION }} || true',
        'aws cloudformation wait stack-delete-complete --stack-name baseVpc${{ env.ENV }} --region ${{ env.AWS_REGION }} || true',
        '',
        '# Delete ECR repos',
        'for repo in catalog checkout ui carts assets orders cloudwatch-agent adot-autoinstrumentation-java adot-autoinstrumentation-node; do',
        '  aws ecr delete-repository --force --repository-name ${repo}${{ env.ENV }} --region ${{ env.AWS_REGION }} --no-cli-pager 2>/dev/null || true',
        'done',
        '',
        'echo "Teardown complete"',
      ].join('\n'),
    },
  ],
});

project.synth();
