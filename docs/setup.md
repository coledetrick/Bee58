# Setup & Run Guide

This document covers how to get Bee58 running locally, run its tests, and use its diagnostic engine directly. Cloud deployment (Phase 2+) is covered at the end.

---

## What This Project Is

Bee58 analyzes CSV log exports from BMW B58 engine tuning platforms (MHD and BM3). It detects wide-open-throttle (WOT) pull segments, checks hardware health indicators (knock, fuel pressure, boost), scores tuning quality, and flags anything worth mentioning to a tuner. The result is a structured diagnostic report with severity-rated alerts.

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
print(f"Platform: {report.tune_platform}")
print(f"WOT Pulls Found: {report.pull_count}")

for alert in report.alerts:
    print(f"[{alert.severity}] {alert.message}")
```

The `ThresholdConfig` dataclass (defined in `app/engine/thresholds.py`) exposes all numeric limits used by the rules engine. Pass a customized instance when analyzing logs from a non-stock tune stage.

---

## 7. Project Structure Quick Reference

```
Bee58/
├── app/engine/          # The diagnostic engine (Phase 1 complete)
│   ├── rules.py         # B58DiagnosticEngine — main analysis class
│   ├── models.py        # Pydantic schemas: Alert, DiagnosticReport, AlertSeverity
│   └── thresholds.py    # ThresholdConfig dataclass + stage presets
├── localdev/
│   └── app.py           # Streamlit UI (reference implementation)
├── tests/engine/
│   └── test_engine.py   # 23 pytest tests with synthetic DataFrames
├── docs/                # Architecture docs for Phases 2–5
└── pyproject.toml       # Package metadata and dependency declarations
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
