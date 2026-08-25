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

    // Deploys the SAM backend in lambdas/ alongside the CDK-managed site stack.
    // Runs `sam build --use-container` because the layer/functions target
    // arm64 (Globals.Function.Architectures in lambdas/template.yaml) and the
    // CodeBuild host is x86_64 — container build cross-compiles correctly.
    const deployBackend = new pipelines.CodeBuildStep('DeployBackend', {
      input: source,
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
      ],
      // Broad by request: the backend stack itself creates IAM roles, KMS
      // keys, API Gateway, and Step Functions, so scope this down to the
      // specific actions/resources in lambdas/template.yaml once it stabilizes.
      rolePolicyStatements: [new iam.PolicyStatement({ actions: ['*'], resources: ['*'] })],
    });

    pipeline.addStage(new SiteStage(this, 'Deploy', { env: props?.env }), {
      post: [deployBackend],
    });
  }
}

class SiteStage extends cdk.Stage {
  constructor(scope: Construct, id: string, props?: cdk.StageProps) {
    super(scope, id, props);
    new SiteStack(this, 'Site');
  }
}
