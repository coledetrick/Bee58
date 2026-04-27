# Research Topics

A running list of things that need investigation before the engine can be considered validated.

---

## 1. Expert Consultation — Tuner's Mental Checklist

When an experienced B58 tuner reviews a WOT log manually, what are they actually looking at and in what order? We need to know:

- What signals do they check first vs. last?
- Are there diagnostic signals or column combinations the engine currently doesn't check at all?
- What are the "obvious tells" a human spots instantly that we might be missing?
- What issues do they see so rarely they'd never flag them without strong evidence?

**Goal:** Verify that our detection coverage matches or exceeds an expert's manual review process.

---

## 2. Threshold Validation Against Real Logs

All numeric limits in `ThresholdConfig` are currently research-derived estimates. Each preset (Stage 1, Stage 2, E30, E50) needs to be validated against:

- Known-good logs (confirmed healthy car, confirmed tune stage and fuel)
- Known-bad logs (confirmed issue, ideally with tuner diagnosis to ground-truth)

**Specific thresholds to prioritize:**

- `min_rail_psi` — does 1900 PSI hold as a Stage 1 crash floor? What does a healthy Stage 2 / E30 / E50 actually hold?
- `rail_drop_rate_psi_per_s` — is 200 PSI/s the right rate to flag? Too sensitive or not sensitive enough?
- `wot_lean_afr` — is 13.2 the right lean threshold for each fuel type?
- `min_timing_correction` — is −3.5° the right per-cylinder correction onset for "injector imbalance" vs. "load-dependent"?
- `boost_delta_psi` — does 3.0 PSI deficit work across different target boost levels?
- `iat_timing_corr_threshold` — is −0.5 Pearson r the right correlation strength for heat soak causation?

**Note:** Forum logs need to be labeled with known stage, fuel type, and any known issues to be useful for validation. An unlabeled log from someone who "thinks their car is fine" is hard to learn from.

---

## 3. Top 5–10 Most Common B58 Issues in the Wild

We need a ranked list of the most frequently seen B58 problems on tuned cars to verify the engine's detection priorities are correct. Research sources:

- B58 tuning forums (MHD Discord, BMWM2 forums, F30post, etc.)
- Tuner feedback threads / "send your log" posts
- Common complaints after tune installs

**Questions to answer:**

- Which issues show up most often on Stage 1 / Stage 2 / E-fuel tunes?
- Are there platform-specific failure modes (MHD vs. BM3 tune behavior)?
- What issues are commonly misdiagnosed by the owner vs. by a tuner?

**Goal:** Confirm the engine is prioritizing its coverage correctly and not over-engineering detection for rare edge cases.

---

## 4. Educational Messaging Depth

The current `beginner_message` field on each alert is a one-liner. The goal is to bridge the gap for first-time tune owners — not just tell them there's a problem, but help them understand what it is and what to do. We need to decide:

- For each major alert type, what does a plain-English explanation look like?
- What vocabulary is acceptable for a first-time owner vs. what needs to be translated? (e.g., is "HPFP" okay to use? "rail pressure"?)
- What's the right action prompt per finding? (e.g., "stop driving and contact your tuner", "mention this at your next service", "normal for this tune stage")
- What's the minimum a user needs to know to have an informed conversation with their tuner?

**Format to investigate:** Inline expandable explanations per alert, a glossary, or a "what to tell your tuner" summary at the bottom of the report.

---

## 5. Gear Change Detection Robustness

The current gear change detection uses a 200 RPM drop in a single sample while the pedal stays floored. Open questions:

- What does a flat-shift (no-lift shift) look like in a log? Does the RPM drop qualify or get missed?
- What does a slow or bad gear change look like? Could the RPM drop be spread over multiple samples and fall below the 200 RPM threshold?
- Should we look at multiple samples for the RPM drop rather than a single frame?
- Do manual and DCT/auto gearboxes produce different RPM drop signatures?

**Goal:** Confirm the suppression window is wide enough to prevent false positives without masking real events that happen near a gear change.

---

## 6. BM3 Column Names and Unit Normalization

BM3 support is built but not validated against a real BM3 log. When we return to this:

- Obtain a real BM3 CSV export and verify the column names match what's in the platform map.
- Confirm whether BM3 metric exports need Bar→PSI and °C→°F normalization (MHD metric exports do).
- Check if any BM3 columns are missing that MHD provides (or vice versa) and decide how the engine should handle absent columns gracefully.
