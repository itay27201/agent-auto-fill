import * as cdk from 'aws-cdk-lib/core';
import { Construct } from 'constructs';
import * as pipelines from 'aws-cdk-lib/pipelines';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as iam from 'aws-cdk-lib/aws-iam';
import { SiteStack } from './site-stack';

const GITHUB_REPO = 'itay27201/agent-auto-fill';
const GITHUB_BRANCH = 'main';
const GITHUB_CONNECTION_ARN =
  'arn:aws:codeconnections:eu-west-1:590184086199:connection/db28dd43-2c09-48e7-b806-bd05f1620971';
const BEDROCK_MODEL_ID = 'eu.anthropic.claude-sonnet-4-6';
const BACKEND_STACK_NAME = 'agent-auto-fill-backend';

export class PipelineStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const source = pipelines.CodePipelineSource.connection(GITHUB_REPO, GITHUB_BRANCH, {
      connectionArn: GITHUB_CONNECTION_ARN,
    });

    const pipeline = new pipelines.CodePipeline(this, 'Pipeline', {
      pipelineName: 'agent-auto-fill-pipeline',
      synth: new pipelines.ShellStep('Synth', {
        input: source,
        commands: ['cd infra', 'npm ci', 'npx cdk synth'],
        primaryOutputDirectory: 'infra/cdk.out',
      }),
    });

    // Built before deployBackend so its SiteStack's stackName (needed below
    // to look up the site bucket/distribution) is available to reference.
    const deployStage = new SiteStage(this, 'Deploy', { env: props?.env });

    // Deploys the SAM backend in lambdas/ alongside the CDK-managed site stack.
    //
    // `--use-container` builds inside SAM's Amazon Linux image so the layer's
    // binary wheels (pypdfium2, reportlab, pillow) match the Lambda runtime
    // rather than this Ubuntu host's glibc. It is NOT here to cross-compile:
    // lambdas/template.yaml pins Architectures to x86_64 precisely because
    // this host is x86_64 with no QEMU, so an arm64 container build fails
    // with "exec format error". Keep the two in step — if the template ever
    // moves to arm64, this step needs emulation set up first.
    const deployBackend = new pipelines.CodeBuildStep('DeployBackend', {
      input: source,
      env: {
        SITE_STACK_NAME: deployStage.siteStack.stackName,
      },
      buildEnvironment: {
        buildImage: codebuild.LinuxBuildImage.STANDARD_7_0,
        privileged: true,
      },
      commands: [
        'cd lambdas',
        'pip install --upgrade pip aws-sam-cli',
        'sam build --use-container',
        'sam deploy --no-confirm-changeset --no-fail-on-empty-changeset --resolve-s3' +
          ` --stack-name ${BACKEND_STACK_NAME} --region eu-west-1 --capabilities CAPABILITY_IAM` +
          ` --parameter-overrides BedrockModelId=${BEDROCK_MODEL_ID}`,
        // Publish this run's ApiUrl/WebSocketUrl into the already-deployed
        // site's config.json — SiteStack (CDK) and the backend (SAM) are
        // independent deployments, so nothing else copies these across.
        'cd ..',
        `API_URL=$(aws cloudformation describe-stacks --stack-name ${BACKEND_STACK_NAME} --region eu-west-1` +
          ' --query "Stacks[0].Outputs[?OutputKey==\'ApiUrl\'].OutputValue" --output text)',
        `WS_URL=$(aws cloudformation describe-stacks --stack-name ${BACKEND_STACK_NAME} --region eu-west-1` +
          ' --query "Stacks[0].Outputs[?OutputKey==\'WebSocketUrl\'].OutputValue" --output text)',
        'SITE_BUCKET=$(aws cloudformation describe-stacks --stack-name "$SITE_STACK_NAME" --region eu-west-1' +
          ' --query "Stacks[0].Outputs[?OutputKey==\'SiteBucketName\'].OutputValue" --output text)',
        'SITE_DIST_ID=$(aws cloudformation describe-stacks --stack-name "$SITE_STACK_NAME" --region eu-west-1' +
          ' --query "Stacks[0].Outputs[?OutputKey==\'SiteDistributionId\'].OutputValue" --output text)',
        'printf \'{"apiUrl":"%s","wsUrl":"%s"}\' "$API_URL" "$WS_URL" > config.json',
        'aws s3 cp config.json "s3://$SITE_BUCKET/config.json" --content-type application/json',
        'aws cloudfront create-invalidation --distribution-id "$SITE_DIST_ID" --paths "/config.json"',
      ],
      // Broad by request: the backend stack itself creates IAM roles, KMS
      // keys, API Gateway, and Step Functions, so scope this down to the
      // specific actions/resources in lambdas/template.yaml once it stabilizes.
      rolePolicyStatements: [new iam.PolicyStatement({ actions: ['*'], resources: ['*'] })],
    });

    pipeline.addStage(deployStage, {
      post: [deployBackend],
    });
  }
}

class SiteStage extends cdk.Stage {
  public readonly siteStack: SiteStack;

  constructor(scope: Construct, id: string, props?: cdk.StageProps) {
    super(scope, id, props);
    this.siteStack = new SiteStack(this, 'Site');
  }
}
