#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { PipelineStack } from '../lib/pipeline-stack';

const app = new cdk.App();

new PipelineStack(app, 'AgentAutoFillPipelineStack', {
  env: { account: '590184086199', region: 'eu-west-1' },
});
