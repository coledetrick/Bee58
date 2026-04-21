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


# Community stage presets — use as starting points, not gospel.
# These will be validated against real log data as the dataset grows.
STAGE1 = ThresholdConfig()  # defaults are Stage 1 baselines
STAGE2 = ThresholdConfig(min_rail_psi=1800, boost_delta_psi=4.0)
E30 = ThresholdConfig(min_rail_psi=2000, max_afr_delta=0.5)
E50 = ThresholdConfig(min_rail_psi=2100, max_afr_delta=0.4)
