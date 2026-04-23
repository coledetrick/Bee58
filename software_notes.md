# Notes — B58 Log Analyzer

This file is a living decision log. It explains *why* things were built the way they were, records open questions, and documents how to run everything locally. Update it as the project evolves.

---

## What I Found (Starting State)

The codebase had a working diagnostic engine (`localdev/rules.py`) paired with a broken Streamlit UI (`localdev/app.py`). Five structural problems were documented across `docs/engine.md` and `initial_research.md`:

| # | Problem | Impact |
|---|---|---|
| 1 | `app.py` referenced three attributes that don't exist on the engine | App crashed on every file upload |
| 2 | Synthesis engine matched against alert strings (`if "HPFP Crash" in alerts_str`) | Silent breakage when any alert message changed |
| 3 | All thresholds were magic numbers scattered through check methods | Impossible to test, impossible to tune per stage/hardware |
| 4 | Scoring was `100 - (len(alerts) * 15)` — a knock counted the same as a throttle closure | Misleading health scores |
| 5 | Multi-pull data was discarded — only the longest WOT segment was analyzed | Lost the most valuable diagnostic signal (heat soak progression) |

---

## Changes Made

### New files

```
app/
├── __init__.py
└── engine/
    ├── __init__.py       — exports B58DiagnosticEngine, ThresholdConfig
    ├── thresholds.py     — ThresholdConfig dataclass + stage presets
    ├── models.py         — AlertSeverity, Alert, PullSummary, DiagnosticReport (Pydantic)
    └── rules.py          — fully refactored B58DiagnosticEngine

tests/
└── engine/
    └── test_engine.py    — 23 pytest tests, all synthetic DataFrames

pyproject.toml            — package setup + pytest config (pythonpath = ["."])
software_notes.md         — this file
```

### Modified files

```
localdev/app.py           — fixed 3 broken attribute refs, updated alert rendering
localdev/requirements.txt — added pydantic>=2.0, pytest>=7.0
```

### Preserved file

`localdev/rules.py` — **left untouched** as a reference. The refactored engine is in `app/engine/rules.py`. You can delete `localdev/rules.py` once you've confirmed the tests pass.

---

## Key Decisions

### Why `_flags: set[str]` instead of inspecting alert objects in synthesis

The synthesis engine needs to ask "did the HPFP crash check fire?" not "what did its message say?" Flags are permanent snake_case identifiers; messages are UI copy that will change. Keeping them separate means you can rewrite every alert message — translate them, change the emoji, A/B test wording — without touching a single `if` branch in `_synthesize_diagnosis`.

The `_flags` set is internal (starts with underscore, not included in `DiagnosticReport`). Synthesis reads from it; nothing outside the engine should need to.

### Why Pydantic for models

The `DiagnosticReport` will eventually be serialized to JSON for DynamoDB storage and HTTP responses from Lambda. Pydantic gives you free serialization (`model.model_dump()`, `model.model_dump_json()`), validation, and a schema that documents the contract between the engine and its callers. A plain dict would require a custom serializer later and offers no validation.

`AlertSeverity` inherits from `str` so Pydantic serializes it as `"critical"` / `"major"` / etc. rather than `AlertSeverity.CRITICAL`. This matters for DynamoDB and API Gateway — they don't know about Python enums.

### Why `AlertSeverity.CRITICAL = instant zero` vs a large deduction

A car with knock or a dangerous lean condition does not have a score of 25 or 50 — it has a score of 0. The score is meant to reassure or alarm a non-expert user. "Your car scored 10/100" still sounds like there's hope. "0/100 — Do Not Drive" is unambiguous. The floor of 10 for non-critical alerts has the same reasoning: a car that made it to redline without exploding deserves credit for still running.

### Why `SEVERITY_DEDUCTIONS` is a module-level dict, not inline in `_calculate_score`

Tests should be able to verify that a specific alert type deducts the expected amount without knowing the engine's internal logic. The dict is importable and inspectable: `SEVERITY_DEDUCTIONS[AlertSeverity.MAJOR] == 25`. If the weights change, tests catch it immediately.

### Why `_compare_pulls` uses strictly-decreasing mean timing as the degradation signal

Mean timing correction across all cylinders is a single number per pull — easy to compare sequentially. "Strictly decreasing" means every pull is worse than the last, which is the classic heat soak signature (not just noise). It's a conservative test: two equally bad pulls, or a one-pull improvement in the middle, do not trigger the flag. Real heat soak is monotonic.

A future improvement would use a linear regression slope instead of strict inequality — this would handle noisy real-world data better. For now, strict decreasing is correct for synthetic data and appropriately conservative for real data.

### Why `_check_timing_advance` uses `adv.iloc[-1]` (end-of-pull value)

This is inherited from the original code. The assumption is that timing advance at the end of the pull (near redline) represents the "peak" — the point where octane is most stressed. This is debatable: the ECU may have already pulled timing before that point, making the last value look more conservative than the worst point.

**Open question**: Should this use `adv.max()` (maximum ever seen in the pull) rather than the last value? `max()` would be a more honest representation of what the tuner targeted. The current behavior could miss a mid-pull timing advance that collapses before redline.

### Why the `app.py` sys.path manipulation instead of requiring `pip install -e .`

Both approaches work. The `sys.path.insert` in `app.py` means anyone can run `streamlit run localdev/app.py` from the `Bee58/` directory without any install step. The `pyproject.toml` is there for the proper install path — `pip install -e .` from `Bee58/` — which is what the Lambda packaging will need later. Use either; they're not mutually exclusive.

### Why the old `localdev/rules.py` was not deleted

It's a useful reference during the transition and is kept until the first successful test run confirms parity. Delete it when you're satisfied.

---

## Open Questions / Things to Validate

1. **BM3 AFR column name**: The BM3 map has `"afr_actual": "AFR"` but the comment in the original code noted it's sometimes `"Bank 1 AFR"`. The AFR check silently skips if the column isn't found (`if m['afr_actual'] not in self.cols: return`), so this won't crash — but it will silently miss a dangerous lean condition on some BM3 exports. Need a real BM3 log to validate.

2. **Timing advance check uses end-of-pull value**: See decision above. Worth revisiting once real logs are available.

3. **Threshold values are research-derived**: The numbers in `ThresholdConfig` came from forum research and documented tuner guidance, not from a validated dataset of real logs. Some users will get false positives (stock HPFP on a built motor may legitimately run lower rail pressure). The "donate your log" flow planned for Phase 5 will build the dataset needed to validate these.

4. **The `conservative_timing` insight is INFO (no score deduction)**: This was a judgment call — a tune that's playing it safe isn't a problem, just a note. If the user wants to be more aggressive they can discuss with their tuner. Reconsider if users find it confusing.

5. **Multi-pull sort assumes `Time` column is present and numeric**: `_compare_pulls` sorts by `pd.to_numeric(p["Time"], ...).iloc[0]`. If the Time column is missing or non-numeric in a real log, the sort will produce garbage ordering. Added basic `errors="coerce"` but no fallback. Low risk for MHD/BM3 exports which always include Time.

---

## How to Run

### Prerequisites

```bash
# From the Bee58/ directory:
pip install pydantic>=2.0 pytest>=7.0

# Or install the whole package in dev mode (also makes `app.engine` importable):
pip install -e ".[dev]"
```

### Run tests

```bash
# From Bee58/
pytest
```

Expected: **23 tests, all passing**.

### Run Streamlit app

```bash
# From Bee58/
streamlit run localdev/app.py
```

Upload a CSV log exported from MHD or BM3. The app will detect the platform, find the longest WOT pull, run all checks, and render the report.

---

## File Map (Phase 1 complete)

```
Bee58/
├── app/
│   ├── __init__.py
│   └── engine/
│       ├── __init__.py       ← exports B58DiagnosticEngine, ThresholdConfig
│       ├── thresholds.py     ← ThresholdConfig dataclass
│       ├── models.py         ← Pydantic result schema
│       └── rules.py          ← diagnostic engine
├── docs/                     ← architecture blueprints (untouched)
├── localdev/
│   ├── app.py                ← Streamlit UI (fixed)
│   ├── rules.py              ← OLD — safe to delete after tests pass
│   └── requirements.txt      ← updated
├── scripts/
│   └── GetVideoTranscripts   ← YouTube transcript scraper for Phase 5 RAG
├── tests/
│   └── engine/
│       └── test_engine.py    ← 23 pytest tests
├── initial_research.md       ← project spec and roadmap
├── pyproject.toml            ← package config + pytest path setup
└── software_notes.md         ← this file
```

## What's Next (Phase 2)

With Phase 1 complete, the engine is packaged, tested, and ready for the cloud migration:

1. **Build 3 Lambda handlers** in `app/lambdas/` (upload, analysis, results)
2. **Write Terraform modules** in `infra/` (storage, functions, api, cdn)
3. **Wire S3 event trigger** to the analysis Lambda
4. **Deploy dev environment** end-to-end
5. **Build minimal HTML/JS frontend** with presigned upload and polling
