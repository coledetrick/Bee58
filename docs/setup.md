# Setup & Run Guide

This document covers how to get Bee58 running locally, run its tests, and use its diagnostic engine directly. Cloud deployment (Phase 2+) is covered at the end.

---

## What This Project Is

Bee58 analyzes CSV log exports from BMW B58 engine tuning platforms (MHD and BM3). It detects wide-open-throttle (WOT) pull segments, checks hardware health indicators (knock, fuel pressure, boost), scores tuning quality, and flags anything worth mentioning to a tuner. The result is a structured diagnostic report with severity-rated alerts.

Detection runs on three pillars:

- **Pillar A — Intra-log normalization**: Compares each parameter to the car's own non-WOT baseline instead of a fixed number. A deviation of N standard deviations from the car's idle/cruise data triggers a flag — tune-agnostic by design.
- **Pillar B — Rate-of-change detection**: Checks how fast values move rather than their absolute level. Sudden timing retard mid-pull, a rail pressure drop during boost ramp, or AFR swinging lean are all Pillar B signals.
- **Pillar C — Cross-parameter correlation**: Checks physical relationships that hold across all tune stages (IAT rise → timing retard, boost rise → fuel rail demand, 100% throttle → rich AFR). Divergence from those relationships is a diagnosis.

A single-pillar finding is a tentative flag. Two or three pillars pointing at the same event is a high-confidence diagnosis.

---

## Prerequisites

| Requirement | Minimum Version | Notes |
|-------------|----------------|-------|
| Python | 3.10 | 3.11 or 3.12 recommended |
| pip | any recent | comes with Python |
| git | any | to clone the repo |

No Docker, no cloud credentials, and no environment variables are needed for local development.

---

## 1. Get the Code

```bash
git clone <repo-url>
cd PET/Bee58
```

---

## 2. Create a Virtual Environment

Always use a virtual environment to keep dependencies isolated.

```bash
python -m venv .venv
```

Activate it:

- **Mac/Linux**: `source .venv/bin/activate`
- **Windows (PowerShell)**: `.venv\Scripts\Activate.ps1`
- **Windows (bash/Git Bash)**: `source .venv/Scripts/activate`

You should see `(.venv)` in your terminal prompt when the environment is active.

---

## 3. Install Dependencies

Install the package in editable mode with development extras:

```bash
pip install -e ".[dev]"
```

This installs:
- **Core**: `pandas`, `numpy`, `pydantic` (the analysis engine)
- **Dev**: `streamlit`, `plotly` (local UI), `pytest` (tests)

Verify the install:

```bash
python -c "from app.engine import B58DiagnosticEngine; print('OK')"
```

---

## 4. Run the Streamlit App (Local UI)

The Streamlit app is the primary way to interact with the analyzer while developing.

```bash
streamlit run localdev/app.py
```

Streamlit will open a browser tab at `http://localhost:8501`. From there:

1. Use the file uploader to load a CSV log export from MHD or BM3.
2. The app auto-detects the platform, extracts WOT segments, and runs the full diagnostic engine.
3. Results are displayed with severity-color-coded alerts and Plotly charts.

**No log file?** The engine tests in `tests/engine/test_engine.py` use synthetic DataFrames — you can read those to understand what a valid input looks like and generate a test CSV from them.

---

## 5. Run the Tests

From the `Bee58/` directory with the virtual environment active:

```bash
pytest tests/ -v
```

Expected output: **23 tests, all passing**. The `-v` flag shows each test name. If any test fails, the output will describe which assertion failed and what values were involved.

To run a single test file:

```bash
pytest tests/engine/test_engine.py -v
```

To run a specific test by name:

```bash
pytest tests/engine/test_engine.py -v -k "test_knock"
```

---

## 6. Use the Engine Directly (Python API)

If you want to call the diagnostic engine in your own script without the Streamlit UI:

```python
import pandas as pd
from app.engine import B58DiagnosticEngine, ThresholdConfig

# Load a CSV log
df = pd.read_csv("my_mhd_log.csv")

# Optional: customize thresholds for a specific tune stage
config = ThresholdConfig(min_rail_psi=1800)

# Run analysis
engine = B58DiagnosticEngine(df, config=config)
report = engine.run_analysis()

# Inspect results
print(f"Score: {report.score}/100")
print(f"Status: {report.status}")
print(f"Platform: {engine.tune_platform}")
print(f"WOT Pulls Found: {report.pull_count}")

for alert in report.alerts:
    print(f"[{alert.severity}] {alert.message}")

# Performance observations (no score deduction)
for insight in report.performance_insights:
    print(f"  note: {insight.message}")

# High-level diagnosis strings (cross-pillar synthesis)
for d in report.diagnosis:
    print(f"  dx: {d}")

# Per-pull breakdown (if multiple pulls detected)
if report.pull_comparison:
    for pull in report.pull_comparison:
        print(f"  Pull {pull.pull_number}: boost={pull.max_boost_psi} psi, IAT end={pull.iat_end_f}°F")
```

### Tune-Stage Presets

`thresholds.py` ships four community-derived starting-point configs:

```python
from app.engine.thresholds import STAGE1, STAGE2, E30, E50

engine = B58DiagnosticEngine(df, config=STAGE2)
```

| Preset | `min_rail_psi` | `boost_delta_psi` | `max_afr_delta` | Notes |
|--------|---------------|-------------------|-----------------|-------|
| `STAGE1` | 1900 | 3.0 | 0.8 | Default baselines (91–93 oct pump gas) |
| `STAGE2` | 1800 | 4.0 | 0.8 | Slightly looser rail floor, wider boost tolerance |
| `E30` | 2000 | 3.0 | 0.5 | Higher rail demand, tighter AFR tolerance |
| `E50` | 2100 | 3.0 | 0.4 | Highest rail demand, tightest AFR tolerance |

These are starting points, not gospel — validate against real logs for your specific hardware.

### ThresholdConfig Reference

`ThresholdConfig` (in `app/engine/thresholds.py`) exposes every numeric limit the engine uses, organized by pillar:

| Group | Key fields |
|-------|-----------|
| Absolute thresholds | `min_rail_psi`, `min_lpfp_psi`, `max_afr_delta`, `min_timing_correction` |
| Pillar A | `baseline_sigma`, `baseline_min_rows` |
| Pillar B | `rail_drop_rate_psi_per_s`, `timing_retard_event_deg`, `afr_lean_swing_delta`, `boost_mid_pull_drop_psi`, `iat_inter_pull_jump_f` |
| Pillar C | `boost_rail_corr_threshold`, `throttle_afr_lean_fraction`, `wot_lean_afr`, `iat_timing_rise_min_f` |

Pass a customized instance at construction to override any subset of these.

---

## 7. Project Structure Quick Reference

```
Bee58/
├── app/engine/              # The diagnostic engine (Phase 1 complete)
│   ├── __init__.py          # Exports B58DiagnosticEngine, ThresholdConfig
│   ├── rules.py             # B58DiagnosticEngine — ABC pillar detection, WOT extraction, scoring
│   ├── models.py            # Pydantic schemas: Alert, DiagnosticReport, AlertSeverity, PullSummary
│   └── thresholds.py        # ThresholdConfig dataclass + STAGE1/STAGE2/E30/E50 presets
├── localdev/
│   └── app.py               # Streamlit UI (reference implementation)
├── tests/engine/
│   └── test_engine.py       # 23 pytest tests with synthetic DataFrames
├── docs/                    # Architecture docs for Phases 2–5
└── pyproject.toml           # Package metadata and dependency declarations
```

---

## 8. Cloud Deployment (Phase 2 — Not Yet Built)

The cloud architecture is fully designed in `docs/infrastructure.md`, `docs/lambdas.md`, `docs/cicd.md`, and `docs/observability.md`. The deployment flow when Phase 2 is implemented will be:

### One-Time Infrastructure Setup

Requirements: AWS CLI configured, Terraform installed (>= 1.5).

```bash
cd infra/environments/dev
terraform init
terraform plan
terraform apply
```

This provisions: S3 bucket (log storage + frontend), DynamoDB table (job results with 24h TTL), three Lambda functions (upload, analysis, results), API Gateway, and CloudFront CDN.

### Deploy Lambda Code

Each Lambda handler will be packaged as a Docker image and pushed to ECR. GitHub Actions handles this automatically on push to `main` (once CI/CD is configured per `docs/cicd.md`).

### Environment Variables (Phase 2)

These are set via Terraform, not manually:

| Variable | Where Used | Example Value |
|----------|-----------|---------------|
| `LOG_BUCKET` | Analysis Lambda | `bee58-logs-dev` |
| `RESULTS_TABLE` | Analysis + Results Lambda | `bee58-results-dev` |
| `API_BASE` | Frontend `config.js` | `https://<id>.execute-api.us-east-1.amazonaws.com` |

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'app'`**
Run `pip install -e .` from the `Bee58/` directory. The `pyproject.toml` configures `pythonpath = ["."]` for pytest, but scripts run outside pytest need the package installed.

**Streamlit shows a blank page after uploading**
Check the terminal where you ran `streamlit run` for a Python traceback. The most common cause is a CSV with unexpected column names — MHD and BM3 exports vary by firmware version.

**Tests fail with pandas dtype errors**
Ensure you are on Python 3.10+ and that `pandas >= 2.0` is installed: `pip show pandas`.

**`streamlit: command not found`**
The virtual environment is not activated, or the `.[dev]` extras were not installed. Re-run `pip install -e ".[dev]"` with the venv active.
