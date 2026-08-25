import * as path from 'path';
import * as cdk from 'aws-cdk-lib/core';
import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';

export class SiteStack extends cdk.Stack {
  public readonly siteBucket: s3.Bucket;
  public readonly distribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const siteBucket = new s3.Bucket(this, 'SiteBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });
    this.siteBucket = siteBucket;

    const distribution = new cloudfront.Distribution(this, 'SiteDistribution', {
      defaultRootObject: 'index.html',
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(siteBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      },
      errorResponses: [
        { httpStatus: 403, responseHttpStatus: 200, responsePagePath: '/index.html' },
        { httpStatus: 404, responseHttpStatus: 200, responsePagePath: '/index.html' },
      ],
    });

    new s3deploy.BucketDeployment(this, 'DeploySite', {
      sources: [s3deploy.Source.asset(path.join(__dirname, '..', '..', 'site'))],
      destinationBucket: siteBucket,
      distribution,
      distributionPaths: ['/*'],
    });

    this.distribution = distribution;

    new cdk.CfnOutput(this, 'SiteURL', {
      value: `https://${distribution.distributionDomainName}`,
    });
    // Consumed by the DeployBackend CodeBuild step (see pipeline-stack.ts) to
    // publish the backend's ApiUrl/WebSocketUrl into config.json after the
    // SAM stack deploys — this stack has no CDK reference to that stack's
    // outputs, so it's looked up by name at pipeline-run time instead.
    new cdk.CfnOutput(this, 'SiteBucketName', {
      value: siteBucket.bucketName,
    });
    new cdk.CfnOutput(this, 'SiteDistributionId', {
      value: distribution.distributionId,
    });
  }
}
