# CI/CD Pipeline

**Location**: `.github/workflows/`  
**Phase**: 1 (testing/lint) → Phase 4 (full deploy pipeline)  
**Platform**: GitHub Actions

---

## Overview

Four workflows, introduced in phases:

| Workflow | Phase | Trigger |
|---|---|---|
| `ci.yml` | 1 | Push to any branch, PRs |
| `docker.yml` | 1 | Push to `main` |
| `terraform-plan.yml` | 4 | PRs touching `infra/` |
| `deploy.yml` | 4 | Push to `main` (with manual approval for prod) |

---

## Phase 1: CI Workflow (`ci.yml`)

Runs on every push and every PR. Must pass before merge.

```yaml
name: CI
on:
  push:
  pull_request:

jobs:
  lint-and-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install -e ".[dev]"
      - run: ruff check app/
      - run: pytest tests/ -v --tb=short
```

**What this validates**:
- `ruff check` catches common Python issues and enforces style (replaces flake8 + isort)
- `pytest` runs the engine tests (see [engine.md](engine.md) for test requirements)

Keep this workflow fast — under 90 seconds. If it grows beyond 2 minutes, split into parallel jobs.

---

## Phase 1: Docker Build (`docker.yml`)

Runs on push to `main`. Validates that the Lambda container image builds successfully.

```yaml
name: Docker Build
on:
  push:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker build -t bee58-analysis:latest -f app/lambdas/analysis/Dockerfile .
      - run: docker build -t bee58-upload:latest -f app/lambdas/upload/Dockerfile .
      - run: docker build -t bee58-results:latest -f app/lambdas/results/Dockerfile .
```

This is a build-only check — no push to ECR until the deploy workflow in Phase 4. Catches `Dockerfile` issues early without needing AWS credentials.

---

## Phase 4: Terraform Plan (`terraform-plan.yml`)

Runs on PRs that touch `infra/`. Posts the plan output as a PR comment.

```yaml
name: Terraform Plan
on:
  pull_request:
    paths:
      - 'infra/**'

jobs:
  plan:
    runs-on: ubuntu-latest
    environment: dev
    permissions:
      id-token: write   # OIDC
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN }}
          aws-region: us-east-1
      - uses: hashicorp/setup-terraform@v3
      - run: terraform -chdir=infra/environments/dev init
      - id: plan
        run: terraform -chdir=infra/environments/dev plan -no-color
      - uses: actions/github-script@v7
        with:
          script: |
            github.rest.issues.createComment({
              issue_number: context.issue.number,
              owner: context.repo.owner,
              repo: context.repo.repo,
              body: '```\n${{ steps.plan.outputs.stdout }}\n```'
            })
```

**AWS auth**: Use OIDC (not long-lived access keys). Configure an IAM OIDC identity provider for GitHub Actions and an IAM role that GitHub can assume on PRs.

---

## Phase 4: Deploy (`deploy.yml`)

Two-stage job: dev deploys automatically on merge to `main`; prod requires a manual approval.

```yaml
name: Deploy
on:
  push:
    branches: [main]

jobs:
  deploy-dev:
    runs-on: ubuntu-latest
    environment: dev
    permissions:
      id-token: write
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEV_ROLE_ARN }}
          aws-region: us-east-1
      - name: Build and push Lambda images to ECR
        run: |
          aws ecr get-login-password | docker login --username AWS --password-stdin $ECR_REGISTRY
          docker build -t $ECR_REGISTRY/bee58-analysis:$GITHUB_SHA -f app/lambdas/analysis/Dockerfile .
          docker push $ECR_REGISTRY/bee58-analysis:$GITHUB_SHA
          # repeat for upload and results
      - name: Terraform apply (dev)
        run: |
          terraform -chdir=infra/environments/dev init
          terraform -chdir=infra/environments/dev apply -auto-approve \
            -var="image_tag=$GITHUB_SHA"
      - name: Sync frontend to S3 + invalidate CloudFront
        run: |
          echo "window.BEE58_CONFIG = { apiBase: '${API_BASE}' };" > app/frontend/config.js
          aws s3 sync app/frontend/ s3://bee58-frontend-dev/
          aws cloudfront create-invalidation --distribution-id $CF_DIST_ID --paths "/*"

  deploy-prod:
    needs: deploy-dev
    runs-on: ubuntu-latest
    environment: prod   # GitHub environment with required reviewers = manual approval gate
    permissions:
      id-token: write
      contents: read
    steps:
      # Same as dev but using prod role, prod bucket, prod Terraform environment
```

### Manual approval gate

The `prod` GitHub environment is configured with **required reviewers**. When the `deploy-prod` job starts, GitHub pauses and sends a review request. No prod deploy happens until a reviewer approves it in the GitHub Actions UI.

Set this up under: Repository → Settings → Environments → prod → Required reviewers.

---

## Secrets and Credentials

All AWS credentials use OIDC — no long-lived access keys stored as secrets.

GitHub secrets needed:

| Secret | Used by |
|---|---|
| `AWS_DEV_ROLE_ARN` | Terraform plan, dev deploy |
| `AWS_PROD_ROLE_ARN` | Prod deploy |
| `ECR_REGISTRY` | Docker push |
| `API_BASE_DEV` | Frontend config.js generation |
| `API_BASE_PROD` | Frontend config.js generation |
| `CF_DIST_ID_DEV` | CloudFront invalidation |
| `CF_DIST_ID_PROD` | CloudFront invalidation |

---

## Branch Strategy

| Branch | Policy |
|---|---|
| `main` | Protected; requires passing CI + PR review; triggers dev deploy |
| Feature branches | CI runs; no deploy |
| `main` → prod | Manual approval gate in GitHub environment |

Keep it simple — no `develop` branch, no release branches at MVP scale.
