# Infrastructure (Terraform)

**Location**: `infra/`  
**Phase**: 2 (Cloud Architecture)  
**IaC tool**: Terraform (chosen over CDK — already comfortable; execute well over learning CDK mid-project)

---

## Module Structure

```
infra/
├── modules/
│   ├── storage/        # S3 buckets + DynamoDB table
│   ├── functions/      # 3 Lambdas + IAM roles + Lambda Layer
│   ├── api/            # API Gateway + routes + CORS
│   └── cdn/            # CloudFront distribution + S3 origin
├── environments/
│   ├── dev/
│   │   ├── main.tf
│   │   └── terraform.tfvars
│   └── prod/
│       ├── main.tf
│       └── terraform.tfvars
└── main.tf
```

Environment directories are thin wrappers — they instantiate the shared modules with environment-specific variable values. All logic lives in modules.

---

## Storage Module

### S3 Buckets

Two buckets:

| Bucket | Purpose | Public? |
|---|---|---|
| `bee58-logs-{env}` | Raw CSV uploads (`raw/` prefix) | No — private, accessed only by Lambdas |
| `bee58-frontend-{env}` | Static frontend assets | Via CloudFront only |

**Log bucket configuration**:
- Block all public access
- Server-side encryption (SSE-S3 or SSE-KMS)
- Lifecycle rule: delete objects in `raw/` after 24 hours (anonymous jobs; no retention needed at MVP)
- Versioning: off (no need; each upload is a unique key)

**Frontend bucket configuration**:
- Block all public access (CloudFront is the only origin)
- Static website hosting: off — use CloudFront origin access control (OAC), not website endpoint

### DynamoDB Table

| Attribute | Type | Role |
|---|---|---|
| `job_id` | String | Partition key |

No sort key needed — single-item access by `job_id` only.

**Additional attributes stored per item** (not part of key schema):
```
status          String   "processing" | "complete" | "failed"
report          Map      Full Pydantic result model serialized to JSON
triggered_rules List     ["hpfp_crash", "timing_pull", ...]
platform        String   "MHD" | "BM3"
tune_stage      String   "stage1" | "stage2" | "e30" | "e50" (if provided by user)
created_at      Number   Unix timestamp
ttl             Number   Unix timestamp (created_at + 86400)
error_message   String   Set only on failed jobs
```

**TTL**: Enable DynamoDB TTL on the `ttl` attribute. Items auto-delete after 24 hours. No manual cleanup Lambda needed.

**Billing mode**: PAY_PER_REQUEST (on-demand) — traffic is unpredictable and bursty; provisioned capacity would either over-provision or throttle.

**Future aggregate analytics**: Storing `triggered_rules`, `platform`, and `tune_stage` as top-level attributes (not buried in `report`) means they're queryable via a GSI later without a schema migration.

---

## Functions Module

Three `aws_lambda_function` resources. Share a single Lambda Layer for the engine package.

### IAM Roles (least privilege per function)

Each Lambda gets its own role with the minimum permissions it needs:

**Upload Lambda role**:
```hcl
s3:PutObject on arn:aws:s3:::bee58-logs-{env}/raw/*
```

**Analysis Lambda role**:
```hcl
s3:GetObject  on arn:aws:s3:::bee58-logs-{env}/raw/*
dynamodb:PutItem on arn:aws:dynamodb:...:table/bee58-results-{env}
```

**Results Lambda role**:
```hcl
dynamodb:GetItem on arn:aws:dynamodb:...:table/bee58-results-{env}
```

All roles also get the standard `AWSLambdaBasicExecutionRole` managed policy for CloudWatch Logs.

### S3 Event Notification → Analysis Lambda

Wire the S3 `ObjectCreated` event to trigger the Analysis Lambda via Terraform:

```hcl
resource "aws_s3_bucket_notification" "log_upload" {
  bucket = aws_s3_bucket.logs.id
  lambda_function {
    lambda_function_arn = aws_lambda_function.analysis.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "raw/"
    filter_suffix       = ".csv"
  }
}

resource "aws_lambda_permission" "s3_invoke" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.analysis.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.logs.arn
}
```

### Cost Protection (required before public launch)

An unauthenticated public endpoint is an abuse target. These controls are not optional:

**API Gateway usage plan** (on Upload and Results Lambdas):
- Burst limit: 50 requests/second
- Rate limit: 20 requests/second
- Quota: 1000 requests/day per API key (or per IP if using a WAF rule)

**Analysis Lambda reserved concurrency cap**:
```hcl
resource "aws_lambda_function" "analysis" {
  ...
  reserved_concurrent_executions = 5
}
```
This bounds the max cost of a flood of uploads — 5 concurrent analyses max, everything else queues or drops.

**CloudFront WAF rule** (basic rate limiting):
- `AWSManagedRulesCommonRuleSet` is a starting point
- Add a rate-based rule limiting unique IPs to N requests per 5 minutes

---

## API Module

Two routes:

| Method | Path | Lambda | Auth |
|---|---|---|---|
| POST | `/upload` | Upload Lambda | None (MVP) |
| GET | `/result/{job_id}` | Results Lambda | None (MVP) |

**CORS**: Required — the frontend is on a different origin (CloudFront) than API Gateway.

```hcl
cors_configuration {
  allow_headers = ["Content-Type"]
  allow_methods = ["GET", "POST", "OPTIONS"]
  allow_origins = ["https://${var.frontend_domain}"]
}
```

Do not allow `*` origins on the API. Lock it to the CloudFront domain, even in dev.

**API Gateway type**: HTTP API (not REST API). HTTP API is cheaper, simpler, has native CORS support, and is sufficient for this use case.

---

## CDN Module

CloudFront distribution serves the static frontend from the `bee58-frontend-{env}` S3 bucket.

Key configuration:
- **Origin access control (OAC)**: CloudFront accesses S3 directly, bucket is not public.
- **Default root object**: `index.html`
- **Custom error response**: 404 → `index.html` with 200 status (required for SPA-style routing if added later)
- **HTTPS only**: redirect HTTP to HTTPS; no plain HTTP allowed
- **Cache policy**: `CachingOptimized` for static assets; `CachingDisabled` for `/api/*` if proxied (but in this architecture the API is a separate API Gateway endpoint, not proxied through CloudFront)

---

## Cost Tagging

Every resource gets cost allocation tags:

```hcl
default_tags {
  tags = {
    Project     = "bee58-analyzer"
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}
```

Set `default_tags` on the AWS provider block so they apply automatically to all resources in that environment. This enables per-environment cost breakdown in the AWS Cost Explorer without tagging each resource individually.

---

## Deployment Notes

- Run `terraform plan` as a required CI check on PRs (see [cicd.md](cicd.md))
- Never run `terraform apply` from a developer machine against prod — apply is gated by a GitHub Actions manual approval (see [cicd.md](cicd.md))
- State backend: S3 + DynamoDB state lock (standard Terraform remote state setup; set this up before writing any other Terraform)
