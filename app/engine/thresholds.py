from dataclasses import dataclass


@dataclass
class ThresholdConfig:
    """
    All numeric thresholds used by B58DiagnosticEngine.

    Override specific values at construction to support different tune stages
    and hardware configurations without changing engine logic. For example:
        engine = B58DiagnosticEngine(df, config=ThresholdConfig(min_rail_psi=2000))

    Defaults are research-derived baselines for a Stage 1/Stage 2 B58 on 91-93
    octane pump gas. E-blend or high-stage hardware will need adjusted values.
    """

    # Boost / spool
    post_spool_rpm: int = 3500          # RPM above which boost delta checks activate
    boost_delta_psi: float = 3.0        # max |target − actual| before flagging

    # Fuel pressure
    min_rail_psi: int = 1900            # below this = HPFP crash
    min_lpfp_psi: int = 55              # below this = LPFP starvation

    # Throttle
    min_throttle_pct: float = 93.0      # below this = ECU throttle closure

    # Fuel trims
    max_stft_pct: float = 25.0          # above this = fueling problem

    # Intake air temp — separate alert vs warning thresholds
    max_iat_delta_alert: float = 20.0   # rise triggers alert-level heat soak flag
    max_iat_delta_warn: float = 12.0    # rise triggers warning-level insight only

    # Wastegate duty cycle
    max_wgdc_pct: float = 95.0          # above this = turbo at mechanical limit

    # AFR
    max_afr_delta: float = 0.8          # actual − target above this = dangerous lean

    # Engine load
    max_load_miss_pct: float = 15.0     # target − actual above this = load miss

    # Ignition timing
    min_timing_correction: float = -3.5  # below this = significant timing pull
    min_peak_timing_adv: float = 8.0     # advance at pull end below this = octane limited

    # WOT detection
    wot_pedal_threshold: float = 85.0    # minimum pedal % to qualify a row as WOT

    # ── Dynamic detection (Pillar A — intra-log statistical normalization) ──────
    baseline_sigma: float = 2.0          # σ from non-WOT baseline before Pillar A flags
    baseline_min_rows: int = 5           # minimum non-WOT rows to attempt Pillar A

    # ── Rate-of-change thresholds (Pillar B — delta detection) ──────────────────
    # Rail pressure drop rate during WOT ramp: PSI per second
    rail_drop_rate_psi_per_s: float = 200.0
    # Sudden timing retard within a 3-sample rolling window
    timing_retard_event_deg: float = 3.0
    # AFR lean excursion above target mid-pull
    afr_lean_swing_delta: float = 1.2
    # Boost drop from peak mid-pull (post-spool)
    boost_mid_pull_drop_psi: float = 3.0
    # IAT jump at pull start between consecutive pulls
    iat_inter_pull_jump_f: float = 15.0

    # ── Cross-parameter correlation (Pillar C) ──────────────────────────────────
    # Pearson correlation threshold: boost vs rail_pressure — below this = diverging
    boost_rail_corr_threshold: float = -0.5
    # Minimum rail drop magnitude to flag Pillar C boost/rail divergence
    boost_rail_min_drop_psi: float = 100.0
    # Fraction of WOT samples that must be lean to flag throttle/AFR correlation
    throttle_afr_lean_fraction: float = 0.10
    # AFR above this at WOT is considered lean for Pillar C check
    wot_lean_afr: float = 13.2
    # Minimum IAT rise within a pull (°F) before checking timing correlation
    iat_timing_rise_min_f: float = 8.0
    # Minimum timing retard (°, negative = retard) to confirm IAT/timing correlation
    iat_timing_retard_min_deg: float = 2.0


# Community stage presets — use as starting points, not gospel.
# These will be validated against real log data as the dataset grows.
STAGE1 = ThresholdConfig()  # defaults are Stage 1 baselines
STAGE2 = ThresholdConfig(min_rail_psi=1800, boost_delta_psi=4.0)
E30 = ThresholdConfig(min_rail_psi=2000, max_afr_delta=0.5)
E50 = ThresholdConfig(min_rail_psi=2100, max_afr_delta=0.4)
