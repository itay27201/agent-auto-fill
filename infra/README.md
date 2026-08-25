# Infrastructure

Two stacks that deploy independently and have to be introduced to each other.

```
PipelineStack            CodePipeline, from GitHub main
  └─ Deploy (stage)
       └─ SiteStack      S3 + CloudFront, serves site/
  └─ DeployBackend       CodeBuild: sam build && sam deploy  ->  lambdas/
```

The backend is **SAM**, not CDK. `lambdas/template.yaml` owns every Lambda,
the REST API, the WebSocket API, DynamoDB, the buckets and the KMS key. CDK
owns only the static site and the pipeline that drives both.

## The seam worth understanding

`SiteStack` and the SAM backend are separate deployments with no CloudFormation
reference between them, so nothing carries the backend's URLs to the frontend
automatically. `site/config.json` closes that gap:

1. `SiteStack` deploys `site/` verbatim — including a `config.json` with empty
   URLs, because the site has **no build step** and nothing can be injected at
   bundle time.
2. `DeployBackend` deploys the SAM stack, reads its `ApiUrl`/`WebSocketUrl`
   outputs, writes a real `config.json` straight to the site bucket, and
   invalidates it in CloudFront.
3. `site/js/config.js` fetches it at runtime and retries a few times, which
   covers the window between those two steps on a fresh deploy.

Nobody should ever type an API URL by hand.

## Adding things

Most changes need nothing here:

| Change | What to do |
|---|---|
| A new Lambda, route, or bucket | Add it to `lambdas/template.yaml`. `sam build` finds it. |
| A new page, script, or stylesheet | Drop it in `site/`. `Source.asset` ships the whole directory. |
| A new backend stack output the site needs | Add it to `template.yaml` **and** to the `DeployBackend` commands that write `config.json`. |

## Architecture pinning

`lambdas/template.yaml` pins `Architectures: [x86_64]`, and the pipeline's
`--use-container` build depends on that matching the CodeBuild host. Moving to
arm64 saves ~20% on Lambda cost but needs QEMU emulation configured in
`DeployBackend` first — otherwise the container build dies with
"exec format error". The two files carry matching comments; change both or
neither.

## Commands

```bash
npm ci
npm run build        # type-check
npx cdk diff         # compare against what is deployed
npx cdk deploy       # deploy the pipeline itself
```

Deploying the pipeline is a one-time act. After that, pushing to `main`
deploys everything — **a push is a production deploy.**
