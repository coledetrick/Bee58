# B58 Log Analyzer — Initial Research & Planning

## Project Goals

Dual purpose:
1. **Community tool** — BMW B58 tuning community; upload ECU logs, get automated hardware and tuning diagnosis
2. **Resume project** — showcase software, DevOps, and cloud engineering skills for job applications

Target roles: Software Engineer, DevOps Engineer, Cloud Engineer

---

## What the Project Does (Current State)

Analyzes BMW B58 ECU logs (CSV exports from MHD or BM3 tuning platforms) to:
- Detect hardware failures: fuel pump crashes, knock/detonation, boost leaks, heat soak
- Assess tuning quality: AFR, load matching, timing advance, boost control
- Synthesize root causes across multiple alerts (not just symptom listing)
- Display results with an interactive Plotly chart

Current stack: single-file Streamlit app (`app.py`) + diagnostic engine (`rules.py`) + Pandas/Plotly.

---

## Architecture Decision: Async Serverless on AWS

### Why Async Split

Separating upload, analysis, and results retrieval into distinct Lambdas teaches event-driven architecture — a core cloud engineering pattern that appears frequently in interviews and production systems.

### File Upload: Presigned S3 URL (Option B)

The frontend uploads CSV files directly to S3 via a presigned URL, bypassing API Gateway payload limits and Lambda file handling. This is the production-standard pattern for file uploads on AWS.

### Full Architecture

```
                    ┌─────────────────────────────────────────┐
                    │           AWS                           │
                    │                                         │
Frontend (S3+CF) ──→│ API Gateway                            │
                    │    │                                    │
                    │    ├─ POST /upload ──→ Upload Lambda    │
                    │    │                      │             │
                    │    │            returns presigned URL   │
                    │    │                      │             │
Frontend ───────────────────────────────────→ S3 bucket      │
                    │    │                   (raw logs)       │
                    │    │                      │             │
                    │    │              S3 event trigger      │
                    │    │                      ↓             │
                    │    │               Analysis Lambda      │
                    │    │               (rules.py engine)    │
                    │    │                      │             │
                    │    │                   DynamoDB         │
                    │    │                (job results + TTL) │
                    │    │                      │             │
                    │    └─ GET /result/{id} ──→ Results Lambda│
                    └─────────────────────────────────────────┘
```

### Request Flow

1. User selects CSV in browser → `POST /upload` → Upload Lambda returns `job_id` + presigned S3 URL
2. Frontend PUTs file directly to S3 using presigned URL
3. S3 event triggers Analysis Lambda → runs diagnostic engine → writes result to DynamoDB
4. Frontend polls `GET /result/{job_id}` every 2-3 seconds until status = `complete`
5. Frontend renders diagnosis report

---

## Technology Decisions

| Concern | Decision | Reason |
|---|---|---|
| IaC | Terraform | Already comfortable; execute well over half-learning CDK |
| Frontend | Minimal HTML/JS static site | Focus stays on backend/infra; hosted on S3 + CloudFront |
| Backend | Python Lambda (3 functions) | Matches existing rules.py; lightweight |
| File upload | Presigned S3 URL | Production pattern; no API GW size limits |
| Job state | DynamoDB | Lightweight, fast, cheap; TTL for automatic cleanup |
| Auth | None (stateless, for now) | MVP scope; each upload is anonymous and ephemeral |
| CI/CD | GitHub Actions | Tests + Docker build on push; deploy gated by environment |

---

## Terraform Module Structure

```
infra/
├── modules/
│   ├── storage/        # S3 buckets (logs + frontend assets), DynamoDB table + TTL
│   ├── functions/      # 3 Lambdas + IAM roles (least-privilege per function)
│   ├── api/            # API Gateway, routes, CORS configuration
│   └── cdn/            # CloudFront distribution + S3 origin
├── environments/
│   ├── dev/
│   └── prod/
└── main.tf
```

---

## Application Code Structure

```
app/
├── engine/             # rules.py extracted as an importable package
│   ├── __init__.py
│   ├── rules.py
│   └── models.py       # result schema (dataclass or Pydantic)
├── lambdas/
│   ├── upload/         # presigned URL generation + job_id creation
│   ├── analysis/       # S3-triggered; runs engine; writes to DynamoDB
│   └── results/        # DynamoDB read; returns job status + report
└── frontend/
    ├── index.html
    ├── app.js
    └── styles.css
```

---

## Phased Roadmap

### Phase 1 — Foundation (make it a real software project)
- [ ] Add `pytest` tests for `rules.py` with synthetic log data (15-20 tests minimum)
- [ ] Extract `rules.py` into a proper Python package (`engine/`)
- [ ] Add `Dockerfile`
- [ ] GitHub Actions: lint + test on every push

### Phase 2 — Cloud Architecture (the resume centrepiece)
- [ ] Build 3 Lambda functions (`upload`, `analysis`, `results`)
- [ ] Write Terraform modules (`storage`, `functions`, `api`, `cdn`)
- [ ] Wire S3 event notification → Analysis Lambda in Terraform
- [ ] Deploy dev environment end-to-end
- [ ] Build minimal HTML/JS frontend with polling

### Phase 3 — Observability (DevOps differentiator)
- [ ] Structured JSON logging to CloudWatch from all Lambdas
- [ ] Custom CloudWatch metrics (analyses run, alert types triggered, errors)
- [ ] CloudWatch dashboard
- [ ] Cost tags on every resource

### Phase 4 — CI/CD to Cloud
- [ ] GitHub Actions builds and pushes Lambda zip/Docker image on merge to `main`
- [ ] Terraform plan as PR check
- [ ] Manual approval gate for prod deploy

---

## Key Learning Outcomes by Phase

| Phase | Skills Demonstrated |
|---|---|
| 1 | Testing, packaging, Docker, CI basics |
| 2 | Serverless architecture, event-driven design, IAM least-privilege, presigned URLs, DynamoDB, API Gateway CORS, CloudFront |
| 3 | Structured logging, custom metrics, observability mindset |
| 4 | Full CI/CD pipeline, IaC in automation, environment promotion |

---

## Future Considerations (post-MVP)

- **Auth**: Cognito for user accounts + log history
- **AI diagnosis**: Claude API for narrative diagnosis using YouTube tuning transcripts as context (RAG pattern) — significant resume differentiator for ML/AI-adjacent roles
- **Threshold config**: Expose rule thresholds as configurable parameters; version rule sets so community can track changes
- **Aggregate analytics**: Anonymous cross-user data to validate and improve thresholds
- **Expert validation**: Supplement research-based thresholds with data from professional tuners

---

## Resume Bullet Points (target)

- *Designed and deployed event-driven serverless pipeline on AWS (Lambda, S3, API Gateway, DynamoDB, CloudFront) using Terraform as IaC across dev and prod environments*
- *Implemented presigned S3 URL upload pattern, decoupling file ingestion from API Gateway payload constraints*
- *Built CI/CD pipeline with GitHub Actions: automated testing, Docker builds, Terraform plan checks, and environment-gated deployments*
- *Instrumented Lambda functions with structured CloudWatch logging and custom metrics dashboard for operational visibility*
