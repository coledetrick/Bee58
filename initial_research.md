# B58 Log Analyzer — Research & Architecture

## Project Goals

Dual purpose:
1. **Community tool** — Help BMW B58 owners who just got their first remote tune understand their car's health and build confidence reading ECU logs
2. **Resume project** — Showcase software, DevOps, and cloud engineering skills for job applications

Target roles: Software Engineer, DevOps Engineer, Cloud Engineer

---

## Target User

**The nervous first-tune owner.**

Someone who just got a remote tune flashed via MHD or BM3. They paid a tuner they've never met, were told to "pull some logs and check in," and are now staring at a CSV they don't fully understand. They're not trying to become a tuner — they want to know if their car is okay, understand what they're looking at, and have the language to communicate with their tuner if something is wrong.

This user needs:
- **Validation**: Is my car okay?
- **Education**: What does this finding mean?
- **Guidance**: What should I do next?
- **A bridge to their tuner**: Language to communicate findings back to the expert they paid

The tool should not try to replace their tuner. It should help them have a better conversation with their tuner.

---

## What the Project Does (Current State)

Analyzes BMW B58 ECU logs (CSV exports from MHD or BM3 tuning platforms) to:
- Detect hardware failures: fuel pump crashes, knock/detonation, boost leaks, heat soak
- Assess tuning quality: AFR, load matching, timing advance, boost control
- Synthesize root causes across multiple alerts (not just symptom listing)
- Display results with an interactive Plotly chart

Current stack: single-file Streamlit app (`app.py`) + diagnostic engine (`rules.py`) + Pandas/Plotly.

---

## Known Issues (Fix Before Cloud Work)

### app.py is out of sync with rules.py
`app.py` references attributes that do not exist on `B58DiagnosticEngine`:
- `engine.tuner_type` → should be `engine.tune_platform`
- `engine.wot` → should be `engine.prime_log`
- `engine.timing_cols` → should be `engine.engine_timing_cols`

The local app is currently broken. Fix before any cloud migration.

### Synthesis engine uses brittle string matching
`_synthesize_diagnosis()` concatenates alert strings and searches for substrings:
```python
alerts_str = " ".join(self.report['alerts'])
if "HPFP Crash" in alerts_str:
```
This breaks silently whenever alert text changes (wording, emoji, localization). Replace with a structured flag set: checks add tokens like `'hpfp_crash'` to `self.report['_flags']`, synthesis reads flags.

### Hardcoded thresholds throughout rules.py
Every check method contains magic numbers (3.0 PSI boost delta, 1900 PSI rail pressure, 3500 RPM post-spool, etc.) derived from research. These will be wrong for some tune stages and hardware configurations. Extract into a `ThresholdConfig` dataclass that the engine accepts at construction — enables testing, community threshold contribution, and tune-stage awareness without a rewrite.

### Scoring model is too simplistic
`score = 100 - (len(alerts) * 15)` treats knock and a throttle closure as equally severe. Knock should be an immediate 0. Implement a weighted severity enum per alert type.

### Multi-pull analysis is missing
The engine takes only the longest continuous WOT segment and discards the rest. Users typically do 3 consecutive pulls. Pull-to-pull degradation (heat soak progression, worsening timing pull) is the most valuable diagnostic signal and is currently thrown away. `prime_extracted_data` already collects all WOT segments — compare them rather than max-selecting.

---

## Product Design Principles (for Target User)

### Three severity lanes — clearly communicated
Every finding must land in one of:
1. **Stop driving / contact tuner immediately** — knock, dangerous lean condition
2. **Mention to your tuner at next check-in** — minor timing pull, throttle closure
3. **Normal and expected** — some WGDC ceiling on a healthy stock-turbo setup

The current output does not distinguish these clearly enough for a non-expert user.

### The Clean Bill of Health path is as important as the problem path
Most logs from a well-tuned car will be fine. The nervous first-tune user needs explicit, warm reassurance — not just an absence of alerts. "Your car is running exactly as your tuner intended. Boost is on target, fuel pressure is stable, no knock detected. You're good." is the output they came for.

### "What to send your tuner" section
Generate a short, pre-written message the user can copy and send to their tuner:
> "Hey, ran a log today. The analyzer flagged a timing pull of -4.2° on Cyl 3. Everything else looks clean. Is this within normal range for this map?"

This bridges the tool back to the expert relationship rather than replacing it.

### Shareable results URL
The async architecture produces a `job_id` naturally. The results page at `/result/{job_id}` is a shareable link. Surface this explicitly: "Copy this link to share your results with your tuner."

### Onboarding gap: how to pull a log
The nervous first-tune user may not know how to get a log in the first place. "Upload CSV Log" as the entry point assumes knowledge they may not have. Include a collapsible "How to pull your first log" guide for both MHD and BM3 covering: which parameters to enable, how to do a safe WOT pull, and how to export the CSV.

---

## Architecture Decision: Async Serverless on AWS

### Why Async Split

Separating upload, analysis, and results retrieval into distinct Lambdas teaches event-driven architecture — a core cloud engineering pattern that appears frequently in interviews and production systems.

### File Upload: Presigned S3 URL

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
5. Frontend renders diagnosis report with shareable URL

### Infrastructure Advisories

**S3 trigger idempotency**: S3 event notifications are at-least-once delivery. The Analysis Lambda must write to DynamoDB with a conditional expression (`attribute_not_exists(job_id)`) to prevent a retried event from overwriting a completed result.

**Cost protection (required before public launch)**: An unauthenticated public endpoint that triggers Lambda + S3 + DynamoDB is an abuse target. No auth does not mean no protection. Required before launch:
- API Gateway usage plan with burst/rate throttling per IP
- CloudFront rate limiting or basic WAF rule
- Lambda reserved concurrency cap on the Analysis function

**Results Lambda status model**: Return explicit `status` values — `processing`, `complete`, `failed` — so the polling frontend doesn't need to infer state from missing records. Store a `error_message` field on failure so the frontend can surface a meaningful message.

**DynamoDB schema**: Store structured alert identifiers (e.g., `triggered_rules: ["hpfp_crash", "timing_pull"]`), `platform` (MHD/BM3), and `tune_stage` as top-level attributes alongside the rendered report. This makes future aggregate analytics possible without a schema migration. Set TTL at 24h for anonymous jobs.

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

**Frontend note**: The current Streamlit app has interactive Plotly charts with dual Y-axes, multi-select controls, and dynamic column awareness. Reproducing this in vanilla HTML/JS is more work than it appears. Consider a lightweight Vite + minimal JS build if the chart complexity becomes a blocker — still fully hostable on S3/CloudFront.

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
│   └── models.py       # result schema (Pydantic) — required for Lambda JSON serialization
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

### Phase 1 — Foundation (fix and package)
- [ ] Fix `app.py` attribute mismatch with `rules.py` (broken references to `tuner_type`, `wot`, `timing_cols`)
- [ ] Replace synthesis string-matching with structured flag set
- [ ] Extract thresholds into `ThresholdConfig` dataclass
- [ ] Add `pytest` tests for `rules.py` with synthetic log data (15-20 tests minimum)
- [ ] Extract `rules.py` into a proper Python package (`engine/`)
- [ ] Add `models.py` with Pydantic result schema
- [ ] Add `Dockerfile`
- [ ] GitHub Actions: lint + test on every push

### Phase 2 — Cloud Architecture (resume centrepiece)
- [ ] Build 3 Lambda functions (`upload`, `analysis`, `results`)
- [ ] Write Terraform modules (`storage`, `functions`, `api`, `cdn`)
- [ ] Wire S3 event notification → Analysis Lambda in Terraform
- [ ] Implement idempotent DynamoDB writes (conditional expression on job_id)
- [ ] Add API Gateway usage plan + Lambda reserved concurrency cap (cost protection)
- [ ] Deploy dev environment end-to-end
- [ ] Build minimal HTML/JS frontend with polling and shareable result URL

### Phase 3 — Observability (DevOps differentiator)
- [ ] Structured JSON logging to CloudWatch from all Lambdas
- [ ] Custom CloudWatch metrics (analyses run, alert types triggered, error rate)
- [ ] CloudWatch dashboard
- [ ] Cost tags on every resource

### Phase 4 — CI/CD to Cloud
- [ ] GitHub Actions builds and pushes Lambda zip/Docker image on merge to `main`
- [ ] Terraform plan as PR check
- [ ] Manual approval gate for prod deploy

### Phase 5 — AI Narrative Diagnosis (product differentiator)
- [ ] Clean transcript corpus (normalize acronyms, strip filler, fix auto-caption errors)
- [ ] Embed transcripts → vector store (Bedrock Knowledge Base or OpenSearch Serverless)
- [ ] Analysis Lambda calls Claude API with structured rule findings + retrieved context
- [ ] Claude generates plain-English narrative diagnosis targeted at the nervous first-tune user
- [ ] Output includes: plain-English explanation per finding, severity lane (stop/mention/normal), "what to send your tuner" pre-written message
- [ ] Rule engine findings ground the Claude output — Claude interprets, rules decide

---

## Key Learning Outcomes by Phase

| Phase | Skills Demonstrated |
|---|---|
| 1 | Testing, packaging, Docker, CI basics |
| 2 | Serverless architecture, event-driven design, IAM least-privilege, presigned URLs, DynamoDB, API Gateway CORS, CloudFront |
| 3 | Structured logging, custom metrics, observability mindset |
| 4 | Full CI/CD pipeline, IaC in automation, environment promotion |
| 5 | RAG architecture, Claude API integration, prompt engineering, vector stores |

---

## Future Considerations (post-MVP)

- **Auth**: Cognito for user accounts + log history; required before any personalization or log retention beyond 24h TTL
- **Multi-pull comparison**: The engine already collects all WOT segments. Comparing pull-to-pull degradation (heat soak, timing pull worsening) is the most valuable diagnostic signal currently discarded — high value for the target user
- **Threshold validation**: Replace research-based thresholds with data-validated ones from real community logs. Build "donate your log" opt-in to grow the validation dataset over time
- **Aggregate analytics**: Anonymous cross-user data to validate thresholds and surface patterns. DynamoDB schema supports this from day one if structured alert identifiers are stored
- **Tune-stage awareness**: Thresholds that differ by Stage 1 / Stage 2 / E30 / E50 configuration

---

## Data Strategy

**Primary source**: MHD and BM3 Discord communities. Post what you're building, ask for log donations. Community responds well to working prototypes. Prioritize logs with known outcomes (forum threads where an expert diagnosed the issue) — these validate the diagnostic accuracy of the rule engine.

**Build "donate your log" into the tool**: Opt-in checkbox at upload. Anonymized logs with consent are the long-term validation flywheel.

**For AI training corpus**: YouTube transcript scraper is already built. Transcripts need a cleaning pass before embedding — auto-captions garble technical terms ("HPFP" → "H P F P", units inconsistently transcribed). Clean before chunking.

---

## Resume Bullet Points (target)

- *Designed and deployed event-driven serverless pipeline on AWS (Lambda, S3, API Gateway, DynamoDB, CloudFront) using Terraform as IaC across dev and prod environments*
- *Implemented presigned S3 URL upload pattern, decoupling file ingestion from API Gateway payload constraints*
- *Built CI/CD pipeline with GitHub Actions: automated testing, Docker builds, Terraform plan checks, and environment-gated deployments*
- *Instrumented Lambda functions with structured CloudWatch logging and custom metrics dashboard for operational visibility*
- *Integrated Claude API with RAG over community tuning knowledge base to generate plain-English diagnostic narratives grounded in rule-engine findings*
