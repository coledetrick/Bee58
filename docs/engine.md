# Diagnostic Engine

**Location**: `app/engine/` (target) — currently `rules.py` (single file)  
**Phase**: 1 (Foundation)

---

## Current State

Single-file `rules.py` containing `B58DiagnosticEngine`. Accepts a CSV log (MHD or BM3 export), extracts WOT segments, runs check methods, builds a report dict, and returns a diagnosis. Works locally but has four structural problems that must be fixed before the cloud migration.

---

## Known Issues (Fix First)

### 1. `app.py` attribute mismatch

`app.py` references attributes that don't exist on `B58DiagnosticEngine`:

| `app.py` references | Actual attribute |
|---|---|
| `engine.tuner_type` | `engine.tune_platform` |
| `engine.wot` | `engine.prime_log` |
| `engine.timing_cols` | `engine.engine_timing_cols` |

**Fix**: Update `app.py` to use the correct attribute names. Do not rename the engine attributes — they are semantically correct.

---

### 2. Brittle string matching in synthesis

`_synthesize_diagnosis()` concatenates all alert strings and uses `in` checks:

```python
alerts_str = " ".join(self.report['alerts'])
if "HPFP Crash" in alerts_str:
```

This breaks silently when alert wording changes (emoji, localization, minor rephrasing).

**Fix**: Introduce a structured flag set. Each check method appends a token to `self.report['_flags']` when a condition triggers. Synthesis reads flags, not strings.

```python
# In check method:
self.report['_flags'].add('hpfp_crash')

# In synthesis:
if 'hpfp_crash' in self.report['_flags']:
```

Flag tokens are snake_case identifiers — never human-readable strings. The human-readable alert text is separate and can change freely.

---

### 3. Hardcoded thresholds

Every check method contains magic numbers derived from research (3.0 PSI boost delta, 1900 PSI rail pressure, 3500 RPM post-spool, etc.). These are wrong for some tune stages and hardware configurations.

**Fix**: Extract into a `ThresholdConfig` dataclass that the engine accepts at construction:

```python
@dataclass
class ThresholdConfig:
    boost_delta_psi: float = 3.0
    min_rail_pressure_psi: float = 1900
    post_spool_rpm: int = 3500
    # ... all other threshold values
```

```python
engine = B58DiagnosticEngine(df, config=ThresholdConfig(boost_delta_psi=2.5))
```

Benefits:
- Tests can pass explicit configs without patching globals
- Tune-stage awareness (Stage 1 vs Stage 2 vs E30) is a config swap, not a code change
- Community-contributed threshold sets are a PR to a config file, not to engine logic

---

### 4. Simplistic scoring model

`score = 100 - (len(alerts) * 15)` treats knock and a minor throttle closure as equally severe. Knock should be a zero by itself.

**Fix**: Weighted severity enum per alert type.

```python
class AlertSeverity(Enum):
    CRITICAL = 0   # immediate zero (knock, dangerous lean)
    MAJOR = 25     # large deduction (HPFP crash)
    MINOR = 10     # small deduction (throttle closure, minor timing pull)
    INFO = 0       # informational; no deduction
```

Each check method emits an `Alert(flag, severity, message)` object. Scoring iterates alerts: if any `CRITICAL` alert exists, score = 0. Otherwise subtract weights.

---

### 5. Multi-pull analysis discarded

The engine takes only the longest continuous WOT segment and discards all others. Users typically do 3 consecutive pulls. Pull-to-pull degradation (heat soak progression, worsening timing pull across pulls) is the most valuable diagnostic signal and is currently thrown away.

`prime_extracted_data` already collects all WOT segments — the data is there.

**Fix**: Compare all WOT segments rather than max-selecting. Add a `_compare_pulls()` method that:
- Sequences pulls by timestamp
- Computes per-pull means for timing, AFR, boost
- Flags degradation trends (e.g., timing pulling progressively across pulls indicates heat soak)
- Includes pull-to-pull delta in the report

---

## Target Package Structure

```
app/engine/
├── __init__.py        # exports B58DiagnosticEngine, ThresholdConfig
├── rules.py           # engine class + check methods
├── models.py          # Pydantic result schema (required for Lambda JSON)
└── thresholds.py      # ThresholdConfig dataclass + stage presets
```

`models.py` must define the result schema as Pydantic so Lambda can serialize it to JSON cleanly. Define it here rather than in the Lambda handler.

---

## Testing Requirements

Minimum 15-20 pytest tests using synthetic log DataFrames. Cover:
- Each check method with a passing case and a failing case
- `ThresholdConfig` overrides actually change behavior
- Synthesis reads flags correctly (not strings)
- Scoring: single CRITICAL alert → score = 0
- Multi-pull: degrading timing across pulls triggers heat soak flag
- Edge cases: single-row log, no WOT segment detected, missing columns

Tests live in `tests/engine/`. Use `pytest` fixtures for synthetic DataFrame construction — do not require real log files for unit tests.

---

## Improvement Backlog (post-Phase 1)

- **Tune-stage awareness**: `ThresholdConfig` presets for Stage 1, Stage 2, E30, E50. User selects at upload; config is passed to engine.
- **Threshold validation**: Replace research-derived values with data-validated ones from real community logs. "Donate your log" opt-in grows this dataset.
- **Column aliasing**: MHD and BM3 export different column names for the same signals. Centralize the mapping rather than scattering conditionals through check methods.
