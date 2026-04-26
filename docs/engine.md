# Diagnostic Engine

**Location**: `bee58/engine/`  
**Phase**: 1 (Foundation — complete)

---

## Current State

`B58DiagnosticEngine` in `rules.py`. Accepts a CSV log (MHD export), extracts WOT segments, runs check methods, builds a report, and returns a `DiagnosticReport`. 53 tests pass. Ready for Phase 2 cloud integration.

---

## Phase 1 Status

All five original issues are resolved:

1. **`localdev/app.py` attribute mismatch** — fixed; uses `tune_platform`, `prime_log`, `engine_timing_cols`
2. **Brittle string synthesis** — fixed; `_synthesize_diagnosis()` reads structured flag tokens (`self._flags`), not alert text
3. **Hardcoded thresholds** — fixed; all numeric limits in `ThresholdConfig` with STAGE1/STAGE2/E30/E50 presets
4. **Simplistic scoring** — fixed; `AlertSeverity` enum with CRITICAL/MAJOR/MINOR/INFO; any CRITICAL → score = 0
5. **Multi-pull analysis** — fixed; all WOT segments in `prime_extracted_data`; `_compare_pulls()` produces `pull_comparison` in report

---

## Target Package Structure

```
bee58/engine/
├── __init__.py        # exports B58DiagnosticEngine, ThresholdConfig
├── rules.py           # engine class + check methods
├── models.py          # Pydantic result schema (required for Lambda JSON)
└── thresholds.py      # ThresholdConfig dataclass + stage presets
```

`models.py` must define the result schema as Pydantic so Lambda can serialize it to JSON cleanly. Define it here rather than in the Lambda handler.

---

## Testing Requirements

53 tests in `tests/engine/test_engine.py` using synthetic DataFrames. Cover all check methods (pass and fail cases), `ThresholdConfig` overrides, flag-based synthesis, severity scoring, multi-pull degradation, and edge cases. No real log files required.

---

## Improvement Backlog (post-Phase 1)

- **Tune-stage awareness**: `ThresholdConfig` presets for Stage 1, Stage 2, E30, E50. User selects at upload; config is passed to engine.
- **Threshold validation**: Replace research-derived values with data-validated ones from real community logs. "Donate your log" opt-in grows this dataset.
- **Column aliasing**: MHD and BM3 export different column names for the same signals. Centralize the mapping rather than scattering conditionals through check methods.
