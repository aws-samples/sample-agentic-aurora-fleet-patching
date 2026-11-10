# Development

[Back to README](../README.md)

## Development

```bash
# Run API locally
cd api && uvicorn server:app --port 8000 --reload

# Run UI locally (proxies /api to localhost:8000)
cd ui && npm run dev
```

> The UI uses Vite 5. Open advisories for `vite` and its bundled `esbuild`
> affect the **development server** (`npm run dev`) only; fixing them requires a
> major Vite upgrade. The deployed UI is a static build (`npm run build`) served
> by the Lambda function and doesn't run the dev server. Run `npm run dev` only
> on `localhost`, never on a shared network.

## Updating Deployed Lambda (without CDK)

When CDK doesn't detect code changes:

```bash
# Rebuild and push directly
bash scripts/build-lambda.sh
cd .lambda-build && zip -r /tmp/lambda-build.zip . && cd ..
aws lambda update-function-code \
  --function-name aurora-patch-agent-api \
  --zip-file fileb:///tmp/lambda-build.zip \
  --region us-east-2
```
