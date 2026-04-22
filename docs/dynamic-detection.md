# Dynamic Detection Engine — Tune-Agnostic Diagnostic Approach

## Why Static Thresholds Fail

The current engine uses hardcoded thresholds (e.g., "rail pressure < 65 PSI = bad").
These break across configurations:

| Config | Timing @ 6500 RPM WOT | Boost |
|---|---|---|
| OEM Stock | 17.5° | 10.5 PSI |
| MHD Stage 2 91oct | 10.5° | 18.1 PSI |
| MHD Stage 2+ E30 | 18.0° | 19.0 PSI |
| BM3 Stage 1 91oct | 10.0° | 17.2 PSI |

A threshold of "timing < 10° = bad" would false-positive on a healthy MHD Stage 2 91oct car.
A threshold of "boost > 18 PSI = bad" would miss a Stage 2+ E30 running perfectly.

The absolute values mean nothing without knowing the tune. The **patterns and relationships** are what matter.

---

## The Three Pillars

### A — Intra-Log Statistical Normalization

**Principle:** Compare the car to itself, not to a fixed number.

Establish a baseline from non-WOT operating periods within the same log (idle, cruise, light throttle). During WOT segments, flag deviations that are statistically significant relative to that car's own baseline.

**How it works:**
1. Split log into baseline segments (throttle < 20%, load below threshold) and WOT segments
2. Compute per-parameter baseline stats (mean, std) from the non-WOT data
3. During WOT, flag parameters that deviate beyond N standard deviations from that car's baseline

**What it detects:**
- Fuel pressure dropping anomalously during WOT relative to the car's normal cruise pressure
- IAT running abnormally high at pull start (car arrived heat-soaked)
- AFR target shifting under load (fueling issue relative to the tune's own baseline)

**Why it's tune-agnostic:**
A Stage 1 car and a Stage 2+ car will have completely different absolute values, but each car's WOT behavior should be consistent relative to its own baseline. A deviation from self is the signal.

**Data requirements:**
- Enough non-WOT data in the log to establish a meaningful baseline (most logs have this)
- At minimum: throttle position, RPM, IAT, fuel rail pressure, AFR

---

### B — Rate-of-Change / Delta Detection

**Principle:** Stop checking absolute values. Check how fast things move.

Physical problems manifest as unexpected rates of change — a fuel pump struggling shows as a pressure drop *during* the WOT ramp, not as a single low number. Heat soak shows as IAT rising *between* pulls, not as a single temperature reading.

**Single-pull signals:**
| Parameter | Normal Pattern | Concerning Pattern |
|---|---|---|
| HPFP Rail Pressure | Holds steady or rises slightly during WOT ramp | Sharp drop during boost buildup |
| Timing Advance | Stable or slight taper at peak RPM | Sudden large retard mid-pull (knock event) |
| AFR | Settles to target early in pull and holds | Swings lean mid-pull (fueling can't keep up) |
| Boost | Builds to target, holds or tapers smoothly | Sudden mid-pull drop (boost leak, wastegate) |

**Multi-pull (session) signals:**
| Parameter | Normal Pattern | Concerning Pattern |
|---|---|---|
| IAT at pull start | Climbs gradually each pull | Large jump between pulls (cooling problem) |
| Average timing per pull | Consistent across pulls | Walks back progressively (heat soak degrading octane effectiveness) |
| Peak boost per pull | Consistent | Drops across pulls (boost management issue) |

**Why it's tune-agnostic:**
A timing retard *event* during WOT is a knock signal regardless of whether the baseline timing is 10° or 18°. The delta is the problem, not the absolute value.

---

### C — Cross-Parameter Correlation

**Principle:** The engine has stable physical relationships. Flag when they break.

These relationships hold across all tune configurations and fuel types:

**Relationship 1: IAT → Timing**
Rising intake temps trigger ECU timing retard (knock protection). If IAT rises significantly across a session but average timing does *not* retard, investigate: either the IAT rise is too small to matter, or the IAT sensor may be inaccurate.

**Relationship 2: Boost → Fuel Rail Pressure**
Higher boost demand = higher fuel demand = HPFP should maintain pressure under load. If boost is high but rail pressure drops proportionally more than the boost rise justifies, HPFP is struggling.

**Relationship 3: Throttle/Load → AFR**
At 100% throttle and high load, AFR must be in the rich WOT band. If throttle is fully open but AFR is lean relative to the car's own WOT target, there is a fueling problem.

**Relationship 4: Timing Retard → Knock Proxy**
The ECU retards timing in response to detected knock. A sudden timing retard event mid-pull is not necessarily bad (ECU did its job), but it is evidence of knock occurring. Multiple retard events = knock is real, not a sensor artifact.

**Relationship 5: RPM → Boost Taper**
Boost naturally tapers at high RPM due to turbo physics. A smooth taper is normal. A sudden drop mid-pull that does not match the expected taper curve is a signal (wastegate, boost leak, compressor surge).

**Why it's tune-agnostic:**
These are physical constraints of the engine and ECU, not tune parameters. A car running E30 and a car running 91 oct have different timing targets, but both will retard timing when knock is detected. The *relationship* is invariant.

---

## How the Three Pillars Compose

A single finding from one pillar is a flag. Corroboration across pillars is a diagnosis.

| Scenario | Pillar A | Pillar B | Pillar C | Diagnosis |
|---|---|---|---|---|
| Fuel pump struggling | Rail pressure deviates from car's own baseline | Rail pressure drops during WOT ramp | Rail pressure drop doesn't match boost rise | HPFP issue — high confidence |
| Heat soak | IAT at pull start elevated vs baseline | IAT rising between pulls, timing walking back | IAT rise correlates with timing retard | Normal heat soak — expected behavior, note severity |
| Knock event | Timing deviation during pull | Sudden timing retard mid-pull | Retard coincides with 100% throttle WOT | Knock detected — flag severity by frequency |
| Lean condition | AFR deviates from car's WOT baseline | AFR swings during pull | Lean AFR at 100% throttle | Fueling problem — high confidence |
| False positive (single pillar) | Sensor noise spike | Single sample outlier | No corroborating relationship | No alert — insufficient evidence |

---

## Where Reference Data Fits

The kern417 tune baseline data (boost, AFR, timing at 6500 RPM WOT by config) is **not used as thresholds**.
It is used as a **sanity layer** after the three pillars have already flagged something:

- If Pillar B flags a timing pattern and the absolute timing value is also well outside the expected range for the identified tune class, confidence increases
- If Pillar A flags an AFR deviation but absolute AFR is within the 12.2–12.8 WOT band, it may be a baseline computation artifact rather than a real problem

Reference data validates and weights findings. It does not gate them.

The reference table lives in `data/tune_reference.json` (to be created) and is consumed by the engine as an optional confidence layer, not as a required dependency.

---

## Tune Classification

To use reference data as a confidence layer, the engine needs to estimate what configuration is running. This is done by fingerprinting the log, not by asking the user:

| Signal | What it tells us |
|---|---|
| Peak boost at 6500 RPM | Stage 1 vs Stage 2 vs Stage 2+ |
| Average WOT timing across pulls | Fuel type (91 < 93 < E30) |
| AFR target during WOT | Platform and fueling strategy |
| Platform header columns | MHD vs BM3 |

The classification is probabilistic — the engine picks the closest matching reference config and notes confidence. If confidence is low (ambiguous fingerprint), reference data is not used and the engine reports on pillars A/B/C alone.

---

## What Weekend Logs Validate

The first real logs (Stage 1 93oct MHD, cold + heat-soaked series) will:

1. **Validate baseline segmentation** — does the idle/cruise data produce a stable, meaningful baseline for normalization?
2. **Validate delta detection** — does the cold-to-heat-soaked progression show the expected IAT/timing pattern?
3. **Validate correlation logic** — do the physical relationships (IAT→timing, boost→rail pressure) hold in real data?
4. **Calibrate sigma thresholds** — how many standard deviations from baseline is actually noise vs signal?
5. **Validate tune fingerprinting** — does Stage 1 93oct fingerprint correctly against the reference table?

A healthy log on a known-good car is the ground truth. If the engine flags issues on a healthy log, the detection logic is wrong.

---

## Open Questions

- **Sigma calibration**: How many standard deviations from intra-log baseline is noise vs signal? Needs real data to calibrate. Start at 2.0, adjust based on false positive rate on healthy logs.
- **Baseline segment minimum**: How much non-WOT data is required for a stable baseline? Logs with only WOT data (track logs) will need a different normalization strategy.
- **Taper curve modeling**: The boost taper relationship (Pillar C, Relationship 5) needs an expected taper curve per tune config. This may require more reference data points across the RPM range, not just 6500 RPM.
- **MHD column validation**: Several columns (WGDC, LPFP, lambda vs AFR) vary by MHD log config. The engine must degrade gracefully when channels are missing rather than skipping checks silently.


extra notes from Kern417

how to read logs

- set logging parameters
- look for wot section
- look at boost
	- look for target vs actual
	- throttle angle relative to this relationship
- hpfp target vs actual
	- gen 1: 2800-2900psi
	- gen 2: ~5000psi
	- look at this throughout the rev range
	- +- 100 psi is okay
- timing
	- timing corrections
	- deviation between cylinders (happens with bad fuel)
	- boost and timing are inverse relationship
- check IAT's
	- if youre not on throttle it's normal for afrs to go sky high
- wgdc hard the turbo is working 
	- high wgdc = boost leak

