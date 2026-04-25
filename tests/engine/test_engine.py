"""
B58DiagnosticEngine test suite.

All tests use synthetic DataFrames — no real log files required.
make_mhd_df() produces a single clean WOT pull that passes every check.
Override specific columns to exercise failure conditions.
"""

import numpy as np
import pandas as pd
import pytest

from bee58.engine.rules import B58DiagnosticEngine
from bee58.engine.models import AlertSeverity
from bee58.engine.thresholds import ThresholdConfig


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ──────────────────────────────────────────────────────────────────────────────

def make_mhd_df(n: int = 20, **column_overrides) -> pd.DataFrame:
    """
    Build a synthetic MHD-format WOT log where every check passes by default.
    Pass column_overrides to inject a failure condition.
    """
    data: dict = {
        "Time":                             np.linspace(0, 5, n),
        "Accel Ped. Pos. (%)":              np.full(n, 95.0),
        "RPM (rpm)":                        np.linspace(3000, 7000, n),
        "Boost target (PSI)":               np.full(n, 20.0),
        "Boost (PSI)":                      np.full(n, 20.0),
        "Throttle Position (*)":            np.full(n, 100.0),
        "Rail pressure mean 1 (PSI)":       np.full(n, 2200.0),
        "IAT (*F)":                         np.linspace(80.0, 82.0, n),
        "STFT 1 (%)":                       np.zeros(n),
        "WGDC (%)":                         np.full(n, 70.0),
        "Knock Detect":                     np.zeros(n),
        "Fuel low pressure sensor (PSI)":   np.full(n, 70.0),
        "Torque Lim. active":               np.zeros(n),
        "AFR Target":                       np.full(n, 11.5),
        "AFR 1":                            np.full(n, 11.5),
        "Load req. (%)":                    np.full(n, 90.0),
        "Load act. (%)":                    np.full(n, 90.0),
        "Timing Cyl. 1 (*)":                np.full(n, 12.0),
        "Cyl1 Timing Cor (*)":              np.zeros(n),
        "Cyl2 Timing Cor (*)":              np.zeros(n),
        "Cyl3 Timing Cor (*)":              np.zeros(n),
        "Cyl4 Timing Cor (*)":              np.zeros(n),
        "Cyl5 Timing Cor (*)":              np.zeros(n),
        "Cyl6 Timing Cor (*)":              np.zeros(n),
    }
    data.update(column_overrides)
    return pd.DataFrame(data)


def make_multi_pull_df(pull_specs: list) -> pd.DataFrame:
    """
    Concatenate multiple WOT pulls separated by short non-WOT gaps.
    Each spec dict is forwarded as column_overrides to make_mhd_df.
    """
    segments = []
    offset = 0.0
    for i, spec in enumerate(pull_specs):
        if i > 0:
            gap = make_mhd_df(n=10)
            gap["Time"] = np.linspace(offset, offset + 2, 10)
            gap["Accel Ped. Pos. (%)"] = 0.0  # not WOT — creates segment boundary
            segments.append(gap)
            offset += 2

        pull = make_mhd_df(n=20, **spec)
        pull["Time"] = np.linspace(offset, offset + 5, 20)
        segments.append(pull)
        offset += 5

    return pd.concat(segments, ignore_index=True)


# ──────────────────────────────────────────────────────────────────────────────
# Platform detection (3 tests)
# ──────────────────────────────────────────────────────────────────────────────

def test_platform_detection_mhd():
    engine = B58DiagnosticEngine(make_mhd_df())
    assert engine.tune_platform == "MHD"


def test_platform_detection_bm3():
    df = pd.DataFrame({
        "Time":                             [0, 1],
        "Accel. Pedal[%]":                  [95.0, 95.0],
        "Engine speed[1/min]":              [4000, 5000],
        "Boost pressure (Target)[psig]":    [20.0, 20.0],
        "Boost (Pre-Throttle)[psig]":       [20.0, 20.0],
        "Throttle Angle[%]":                [100.0, 100.0],
        "HPFP Act.[psig]":                  [2200.0, 2200.0],
        "IAT[F]":                           [80.0, 80.0],
        "STFT 1[%]":                        [0.0, 0.0],
        "WGDC[%]":                          [70.0, 70.0],
        "Knock Detected":                   [0, 0],
        "LPFP Act.[psig]":                  [70.0, 70.0],
        "Torque Limiter Active":            [0, 0],
        "AFR Target":                       [11.5, 11.5],
        "AFR":                              [11.5, 11.5],
        "Load Target[%]":                   [90.0, 90.0],
        "Load Actual[%]":                   [90.0, 90.0],
        "(RAM) Ignition Timing Cyl. 1[°]":  [12.0, 12.0],
    })
    engine = B58DiagnosticEngine(df)
    assert engine.tune_platform == "BM3"


def test_unsupported_platform_raises():
    with pytest.raises(ValueError, match="Unsupported platform"):
        B58DiagnosticEngine(pd.DataFrame({"Unknown Col": [1, 2, 3]}))


# ──────────────────────────────────────────────────────────────────────────────
# WOT extraction (2 tests)
# ──────────────────────────────────────────────────────────────────────────────

def test_no_wot_segment_returns_none():
    df = make_mhd_df()
    df["Accel Ped. Pos. (%)"] = 20.0  # pedal never hits WOT threshold
    assert B58DiagnosticEngine(df).run_analysis() is None


def test_wot_segment_is_extracted():
    engine = B58DiagnosticEngine(make_mhd_df(n=20))
    assert not engine.prime_log.empty
    assert len(engine.prime_log) == 20


# ──────────────────────────────────────────────────────────────────────────────
# Clean log — all checks pass (3 tests)
# ──────────────────────────────────────────────────────────────────────────────

def test_clean_log_score_is_100():
    assert B58DiagnosticEngine(make_mhd_df()).run_analysis().score == 100


def test_clean_log_status_is_healthy():
    assert B58DiagnosticEngine(make_mhd_df()).run_analysis().status == "Healthy"


def test_clean_log_diagnosis_is_clean_bill_of_health():
    report = B58DiagnosticEngine(make_mhd_df()).run_analysis()
    assert any("Clean Bill of Health" in d.message for d in report.diagnosis)


# ──────────────────────────────────────────────────────────────────────────────
# Hardware / safety checks (6 tests)
# ──────────────────────────────────────────────────────────────────────────────

def test_boost_leak_detected():
    df = make_mhd_df()
    df["Boost (PSI)"] = 15.0  # 5 PSI under target — boost_delta > 3.0 post-spool
    flags = {a.flag for a in B58DiagnosticEngine(df).run_analysis().alerts}
    assert "boost_leak" in flags


def test_hpfp_crash_detected():
    df = make_mhd_df()
    df["Rail pressure mean 1 (PSI)"] = 1500.0
    flags = {a.flag for a in B58DiagnosticEngine(df).run_analysis().alerts}
    assert "hpfp_crash" in flags


def test_lpfp_starvation_detected():
    df = make_mhd_df()
    df["Fuel low pressure sensor (PSI)"] = 40.0
    flags = {a.flag for a in B58DiagnosticEngine(df).run_analysis().alerts}
    assert "lpfp_starvation" in flags


def test_knock_alert_has_critical_severity():
    df = make_mhd_df()
    df["Knock Detect"] = 1.0
    knock_alerts = [a for a in B58DiagnosticEngine(df).run_analysis().alerts if a.flag == "knock"]
    assert knock_alerts and knock_alerts[0].severity == AlertSeverity.CRITICAL


def test_knock_drives_score_to_zero():
    df = make_mhd_df()
    df["Knock Detect"] = 1.0
    assert B58DiagnosticEngine(df).run_analysis().score == 0


def test_dangerous_lean_drives_score_to_zero():
    df = make_mhd_df()
    df["AFR 1"] = 13.0  # 1.5 above 11.5 target — well over max_afr_delta of 0.8
    assert B58DiagnosticEngine(df).run_analysis().score == 0


# ──────────────────────────────────────────────────────────────────────────────
# Tuning quality checks (2 tests)
# ──────────────────────────────────────────────────────────────────────────────

def test_timing_pull_detected():
    df = make_mhd_df()
    df["Cyl3 Timing Cor (*)"] = -4.0  # below min_timing_correction of -3.5
    flags = {a.flag for a in B58DiagnosticEngine(df).run_analysis().alerts}
    assert "timing_pull" in flags


def test_iat_heat_soak_alert_triggered():
    df = make_mhd_df()
    df["IAT (*F)"] = np.linspace(80.0, 105.0, 20)  # 25°F rise > 20°F threshold
    flags = {a.flag for a in B58DiagnosticEngine(df).run_analysis().alerts}
    assert "iat_heat_soak" in flags


# ──────────────────────────────────────────────────────────────────────────────
# ThresholdConfig overrides (1 test)
# ──────────────────────────────────────────────────────────────────────────────

def test_threshold_config_override_changes_rail_threshold():
    df = make_mhd_df()
    df["Rail pressure mean 1 (PSI)"] = 1950.0  # above default 1900, below custom 2000

    default_flags = {a.flag for a in B58DiagnosticEngine(df).run_analysis().alerts}
    custom_flags = {
        a.flag for a in B58DiagnosticEngine(
            df, config=ThresholdConfig(min_rail_psi=2000)
        ).run_analysis().alerts
    }

    assert "hpfp_crash" not in default_flags  # 1950 > 1900 → no alert
    assert "hpfp_crash" in custom_flags         # 1950 < 2000 → alert fires


# ──────────────────────────────────────────────────────────────────────────────
# Weighted scoring (2 tests)
# ──────────────────────────────────────────────────────────────────────────────

def test_major_alert_deducts_25_points():
    df = make_mhd_df()
    df["Rail pressure mean 1 (PSI)"] = 1500.0  # single MAJOR alert
    assert B58DiagnosticEngine(df).run_analysis().score == 75  # 100 - 25


def test_minor_alert_deducts_10_points():
    df = make_mhd_df()
    df["Cyl3 Timing Cor (*)"] = -4.0  # single MINOR alert (timing_pull)
    assert B58DiagnosticEngine(df).run_analysis().score == 90  # 100 - 10


# ──────────────────────────────────────────────────────────────────────────────
# Synthesis — reads flags not strings (9 tests)
# ──────────────────────────────────────────────────────────────────────────────

def test_synthesis_cascading_fuel_failure():
    df = make_mhd_df()
    df["Rail pressure mean 1 (PSI)"] = 1500.0   # hpfp_crash
    df["Fuel low pressure sensor (PSI)"] = 40.0  # lpfp_starvation
    report = B58DiagnosticEngine(df).run_analysis()
    assert any("Cascading Fuel Failure" in d.message for d in report.diagnosis)


def test_synthesis_hpfp_only_no_cascade():
    df = make_mhd_df()
    df["Rail pressure mean 1 (PSI)"] = 1500.0  # hpfp_crash only, LPFP is healthy
    report = B58DiagnosticEngine(df).run_analysis()
    assert any("HPFP Limit Reached" in d.message for d in report.diagnosis)
    assert not any("Cascading" in d.message for d in report.diagnosis)


def test_synthesis_marginal_fueling():
    # AFR 1.3 lean of target — triggers Pillar B lean swing and Pillar C WOT lean,
    # but NOT dangerous_lean (max_afr_delta raised to 3.0 to decouple the thresholds).
    df = make_mhd_df()
    df["AFR 1"] = np.full(20, 12.8)
    config = ThresholdConfig(max_afr_delta=3.0, wot_lean_afr=12.0)
    report = B58DiagnosticEngine(df, config=config).run_analysis()
    assert any(d.flag == "dx_marginal_fueling" for d in report.diagnosis)


def test_synthesis_boost_deficit():
    # Boost 5 PSI below target, WGDC not saturated → boost_leak only, no wgdc_saturation
    df = make_mhd_df()
    df["Boost (PSI)"] = np.full(20, 15.0)
    df["Boost target (PSI)"] = np.full(20, 20.0)
    df["WGDC (%)"] = np.full(20, 70.0)
    report = B58DiagnosticEngine(df).run_analysis()
    assert any(d.flag == "dx_boost_deficit" for d in report.diagnosis)


def test_synthesis_boost_leak_saturated():
    # Boost spike then drop → mid-pull drop flag; deficit throughout → boost_leak;
    # WGDC saturated → wgdc_saturation. All three together → dx_boost_leak_saturated.
    df = make_mhd_df()
    boost = np.full(20, 15.0)
    boost[8] = 20.0  # brief peak then falls back — triggers mid-pull drop
    df["Boost (PSI)"] = boost
    df["Boost target (PSI)"] = np.full(20, 20.0)
    df["WGDC (%)"] = np.full(20, 98.0)
    report = B58DiagnosticEngine(df).run_analysis()
    assert any(d.flag == "dx_boost_leak_saturated" for d in report.diagnosis)


def test_synthesis_knock_octane_limit():
    # Steady timing corrections at -4° (below -3.5 threshold) with no retard event
    # and no thermal flags → dx_knock_octane_limit (lower-confidence branch)
    df = make_mhd_df()
    for col in ["Cyl1 Timing Cor (*)", "Cyl2 Timing Cor (*)", "Cyl3 Timing Cor (*)",
                "Cyl4 Timing Cor (*)", "Cyl5 Timing Cor (*)", "Cyl6 Timing Cor (*)"]:
        df[col] = np.full(20, -4.0)
    report = B58DiagnosticEngine(df).run_analysis()
    assert any(d.flag == "dx_knock_octane_limit" for d in report.diagnosis)


def test_synthesis_tcu_intervention():
    # Throttle dips below 93% AND torque limiter is active → dx_tcu_intervention
    df = make_mhd_df()
    df.loc[10, "Throttle Position (*)"] = 85.0
    df["Torque Lim. active"] = np.where(np.arange(20) > 5, 1.0, 0.0)
    report = B58DiagnosticEngine(df).run_analysis()
    assert any(d.flag == "dx_tcu_intervention" for d in report.diagnosis)


def test_synthesis_intercooler_recovery():
    # Two pulls: IAT jumps 20°F between them, timing corrections stable (no degradation)
    # → dx_intercooler_recovery (IAT jump flag without timing_degradation_heat_soak)
    df = make_multi_pull_df([
        {"IAT (*F)": np.full(20, 80.0)},
        {"IAT (*F)": np.full(20, 100.0)},
    ])
    report = B58DiagnosticEngine(df).run_analysis()
    assert any(d.flag == "dx_intercooler_recovery" for d in report.diagnosis)


def test_synthesis_boost_degradation():
    # Three pulls with peak boost declining by 2 PSI each time, no boost_leak or WGDC saturation
    df = make_multi_pull_df([
        {"Boost (PSI)": np.full(20, 22.0), "Boost target (PSI)": np.full(20, 20.0)},
        {"Boost (PSI)": np.full(20, 20.0), "Boost target (PSI)": np.full(20, 20.0)},
        {"Boost (PSI)": np.full(20, 18.0), "Boost target (PSI)": np.full(20, 20.0)},
    ])
    report = B58DiagnosticEngine(df).run_analysis()
    assert any(d.flag == "dx_boost_degradation" for d in report.diagnosis)


# ──────────────────────────────────────────────────────────────────────────────
# Multi-pull comparison (3 tests)
# ──────────────────────────────────────────────────────────────────────────────

def test_multi_pull_degradation_detected():
    df = make_multi_pull_df([
        {"Cyl1 Timing Cor (*)": np.full(20, -0.5)},   # pull 1: slight
        {"Cyl1 Timing Cor (*)": np.full(20, -1.5)},   # pull 2: worse
        {"Cyl1 Timing Cor (*)": np.full(20, -3.0)},   # pull 3: worst
    ])
    flags = {a.flag for a in B58DiagnosticEngine(df).run_analysis().alerts}
    assert "timing_degradation_heat_soak" in flags


def test_multi_pull_stable_no_degradation():
    df = make_multi_pull_df([
        {"Cyl1 Timing Cor (*)": np.full(20, -0.5)},
        {"Cyl1 Timing Cor (*)": np.full(20, -0.5)},
        {"Cyl1 Timing Cor (*)": np.full(20, -0.5)},
    ])
    flags = {a.flag for a in B58DiagnosticEngine(df).run_analysis().alerts}
    assert "timing_degradation_heat_soak" not in flags


def test_pull_count_in_report():
    df = make_multi_pull_df([{}, {}, {}])
    report = B58DiagnosticEngine(df).run_analysis()
    assert report.pull_count == 3


# ──────────────────────────────────────────────────────────────────────────────
# Pillar A — intra-log statistical normalization (2 tests)
# ──────────────────────────────────────────────────────────────────────────────

def make_mhd_df_with_baseline(n_baseline: int = 30, n_wot: int = 20, **wot_overrides) -> pd.DataFrame:
    """
    Build a log with a non-WOT baseline section followed by a WOT section.
    The baseline is cruise data (pedal=10%) that Pillar A uses to compute per-car stats.
    """
    baseline = make_mhd_df(n=n_baseline)
    baseline["Accel Ped. Pos. (%)"] = 10.0
    baseline["Time"] = np.linspace(0, n_baseline - 1, n_baseline)

    wot = make_mhd_df(n=n_wot, **wot_overrides)
    wot["Time"] = np.linspace(n_baseline, n_baseline + n_wot - 1, n_wot)

    return pd.concat([baseline, wot], ignore_index=True)


def test_pillar_a_rail_deviation_fires_when_wot_rail_low():
    # Cruise rail = 2200 PSI; WOT rail = 1750 PSI — well below baseline
    df = make_mhd_df_with_baseline(
        **{"Rail pressure mean 1 (PSI)": 1750.0}
    )
    flags = {a.flag for a in B58DiagnosticEngine(df).run_analysis().alerts}
    assert "rail_deviation_a" in flags


def test_pillar_a_no_baseline_no_deviation_flag():
    # All-WOT log — Pillar A should skip gracefully (no baseline data)
    df = make_mhd_df()
    flags = {a.flag for a in B58DiagnosticEngine(df).run_analysis().alerts}
    assert "rail_deviation_a" not in flags


# ──────────────────────────────────────────────────────────────────────────────
# Pillar B — rate-of-change / delta detection (5 tests)
# ──────────────────────────────────────────────────────────────────────────────

def test_pillar_b_rail_drop_rate_fires():
    # Rail crashes from 2200 to 1600 in 0.5 seconds post-spool = 1200 PSI/s
    df = make_mhd_df()
    rail = np.full(20, 2200.0)
    rail[10:] = 1600.0  # sharp drop in second half
    df["Rail pressure mean 1 (PSI)"] = rail
    flags = {a.flag for a in B58DiagnosticEngine(df).run_analysis().alerts}
    assert "rail_drop_rate_b" in flags


def test_pillar_b_timing_retard_event_fires():
    # Timing drops 4° over 3 samples mid-pull
    df = make_mhd_df()
    timing = np.full(20, 12.0)
    timing[8] = 12.0
    timing[9] = 10.0
    timing[10] = 8.0  # 4° retard over 3 samples
    df["Timing Cyl. 1 (*)"] = timing
    flags = {a.flag for a in B58DiagnosticEngine(df).run_analysis().alerts}
    assert "timing_retard_event_b" in flags


def test_pillar_b_afr_lean_swing_fires():
    # AFR spikes 1.5 above target mid-pull
    df = make_mhd_df()
    afr = np.full(20, 11.5)
    afr[8:12] = 13.0  # 1.5 above 11.5 target in the middle
    df["AFR 1"] = afr
    flags = {a.flag for a in B58DiagnosticEngine(df).run_analysis().alerts}
    assert "afr_lean_swing_b" in flags


def test_pillar_b_boost_mid_pull_drop_fires():
    # Boost ramps to 22 PSI then collapses to 12 PSI — 10 PSI drop, well above 3 PSI threshold.
    # boost_mid_pull_drop_b is an insight (not an alert) — check performance_insights.
    df = make_mhd_df()
    n = 20
    boost = np.concatenate([np.linspace(10, 22, 12), np.full(8, 12.0)])
    df["Boost (PSI)"] = boost
    df["Boost target (PSI)"] = np.full(n, 22.0)  # match target so boost_leak doesn't fire
    flags = {p.flag for p in B58DiagnosticEngine(df).run_analysis().performance_insights}
    assert "boost_mid_pull_drop_b" in flags


def test_pillar_b_iat_inter_pull_jump_fires():
    # Pull 1 ends at IAT 90°F; Pull 2 starts at IAT 110°F — 20°F jump
    pull1 = make_mhd_df(n=20)
    pull1["IAT (*F)"] = np.linspace(85.0, 90.0, 20)
    pull1["Time"] = np.linspace(0, 5, 20)

    gap = make_mhd_df(n=5)
    gap["Accel Ped. Pos. (%)"] = 0.0
    gap["IAT (*F)"] = np.linspace(90.0, 110.0, 5)
    gap["Time"] = np.linspace(5, 7, 5)

    pull2 = make_mhd_df(n=20)
    pull2["IAT (*F)"] = np.linspace(110.0, 112.0, 20)
    pull2["Time"] = np.linspace(7, 12, 20)

    df = pd.concat([pull1, gap, pull2], ignore_index=True)
    flags = {a.flag for a in B58DiagnosticEngine(df).run_analysis().performance_insights}
    assert "iat_inter_pull_jump_b" in flags


# ──────────────────────────────────────────────────────────────────────────────
# Pillar C — cross-parameter correlation (3 tests)
# ──────────────────────────────────────────────────────────────────────────────

def test_pillar_c_iat_timing_correlation_fires():
    # IAT rises 15°F and timing retards 4° in the same pull
    df = make_mhd_df()
    df["IAT (*F)"] = np.linspace(80.0, 95.0, 20)    # 15°F rise
    df["Timing Cyl. 1 (*)"] = np.linspace(12.0, 8.0, 20)  # 4° retard
    flags = {a.flag for a in B58DiagnosticEngine(df).run_analysis().performance_insights}
    assert "iat_timing_correlation_c" in flags


def test_pillar_c_boost_rail_divergence_fires():
    # Boost climbs from 12 to 22 PSI while rail drops from 2200 to 1800 PSI post-spool
    df = make_mhd_df()
    n = 20
    df["Boost (PSI)"] = np.linspace(12.0, 22.0, n)
    df["Rail pressure mean 1 (PSI)"] = np.linspace(2200.0, 1800.0, n)
    flags = {a.flag for a in B58DiagnosticEngine(df).run_analysis().alerts}
    assert "boost_rail_divergence_c" in flags


def test_pillar_c_throttle_afr_lean_fires():
    # All WOT rows have AFR = 14.0 — lean at full throttle
    df = make_mhd_df()
    df["AFR 1"] = 14.0
    flags = {a.flag for a in B58DiagnosticEngine(df).run_analysis().alerts}
    assert "throttle_afr_lean_c" in flags


# ──────────────────────────────────────────────────────────────────────────────
# Multi-pillar synthesis confidence (2 tests)
# ──────────────────────────────────────────────────────────────────────────────

def test_synthesis_hpfp_high_confidence_when_multi_pillar():
    # Rail drops at 240 PSI/s (fires Pillar B) AND inversely correlates with rising boost (fires Pillar C)
    # Together they push the synthesis to "high confidence"
    df = make_mhd_df()
    n = 20
    df["Rail pressure mean 1 (PSI)"] = np.linspace(2200.0, 1000.0, n)   # 240 PSI/s over 5s > threshold
    df["Boost (PSI)"] = np.linspace(14.0, 22.0, n)
    df["Boost target (PSI)"] = np.linspace(14.0, 22.0, n)  # match actual to avoid boost_leak flag
    report = B58DiagnosticEngine(df).run_analysis()
    assert any("high confidence" in d.message for d in report.diagnosis)


def test_synthesis_dangerous_lean_high_confidence_when_corroborated():
    # dangerous_lean + afr_lean_swing_b → "High Confidence" in diagnosis
    df = make_mhd_df()
    afr = np.full(20, 13.5)  # 2.0 above 11.5 target — triggers dangerous_lean and lean_swing
    df["AFR 1"] = afr
    report = B58DiagnosticEngine(df).run_analysis()
    assert any("High Confidence" in d.message for d in report.diagnosis)


# ──────────────────────────────────────────────────────────────────────────────
# Alert beginner_message field (1 test)
# ──────────────────────────────────────────────────────────────────────────────

def test_alerts_have_beginner_messages():
    df = make_mhd_df()
    df["Knock Detect"] = 1.0
    report = B58DiagnosticEngine(df).run_analysis()
    knock_alerts = [a for a in report.alerts if a.flag == "knock"]
    assert knock_alerts[0].beginner_message is not None


# ──────────────────────────────────────────────────────────────────────────────
# Prime pull extraction (4 tests)
# ──────────────────────────────────────────────────────────────────────────────

def test_prime_pull_contiguous():
    # 100% pedal throughout — strict extraction captures the full segment
    df = make_mhd_df(n=20)
    df["Accel Ped. Pos. (%)"] = 100.0
    engine = B58DiagnosticEngine(df)
    assert len(engine.prime_log) == 20


def test_prime_pull_bridges_short_cutout():
    # Two WOT segments with a 0.4s gap — should be merged into one
    seg1 = make_mhd_df(n=10)
    seg1["Time"] = np.linspace(0.0, 2.0, 10)
    seg1["Accel Ped. Pos. (%)"] = 100.0

    gap = make_mhd_df(n=3)
    gap["Time"] = np.linspace(2.1, 2.3, 3)
    gap["Accel Ped. Pos. (%)"] = 50.0  # brief cut-out

    seg2 = make_mhd_df(n=10)
    seg2["Time"] = np.linspace(2.4, 5.0, 10)
    seg2["Accel Ped. Pos. (%)"] = 100.0

    df = pd.concat([seg1, gap, seg2], ignore_index=True)
    engine = B58DiagnosticEngine(df)
    assert len(engine.prime_log) == 23  # all rows merged (10 + 3 gap + 10)


def test_prime_pull_long_gap_selects_longest():
    # Two WOT segments with a 1.2s gap — treated as separate pulls; longer one wins
    seg1 = make_mhd_df(n=20)
    seg1["Time"] = np.linspace(0.0, 5.0, 20)
    seg1["Accel Ped. Pos. (%)"] = 100.0

    gap = make_mhd_df(n=5)
    gap["Time"] = np.linspace(5.1, 6.1, 5)
    gap["Accel Ped. Pos. (%)"] = 0.0

    seg2 = make_mhd_df(n=5)
    seg2["Time"] = np.linspace(6.2, 7.5, 5)
    seg2["Accel Ped. Pos. (%)"] = 100.0

    df = pd.concat([seg1, gap, seg2], ignore_index=True)
    engine = B58DiagnosticEngine(df)
    assert len(engine.prime_log) == 20  # seg1 (5.0s) beats seg2 (1.3s)


def test_prime_pull_rpm_filter_falls_back_to_85pct_result():
    # 100% pedal but RPM never reaches min_pull_rpm — strict prime filtered out,
    # prime_log falls back to the 85% extraction result (which has no RPM floor)
    df = make_mhd_df(n=20)
    df["Accel Ped. Pos. (%)"] = 100.0
    df["RPM (rpm)"] = 1500.0  # below min_pull_rpm=2000
    engine = B58DiagnosticEngine(df)
    assert len(engine.prime_log) == 20  # fallback: 85% result still covers all rows


# ──────────────────────────────────────────────────────────────────────────────
# Charge air temperature detection
# ──────────────────────────────────────────────────────────────────────────────

def test_charge_air_high_flagged():
    # 130°C = 266°F > 250°F default threshold — charge_air_high should fire
    df = make_mhd_df(n=20, **{"Charge air temp. (*C)": np.full(20, 130.0)})
    results = B58DiagnosticEngine(df).run_analysis()
    alert_flags = {a.flag for a in results.alerts}
    assert "charge_air_high" in alert_flags


def test_charge_air_timing_correlation_flagged():
    # Charge air rises 40°C (72°F) > 30°F threshold; timing retards from 18° to 10° (-8° < -2°)
    df = make_mhd_df(
        n=20,
        **{
            "Charge air temp. (*C)": np.linspace(60.0, 100.0, 20),
            "Timing Cyl. 1 (*)": np.linspace(18.0, 10.0, 20),
        },
    )
    results = B58DiagnosticEngine(df).run_analysis()
    insight_flags = {p.flag for p in results.performance_insights}
    assert "charge_air_timing_c" in insight_flags


# ──────────────────────────────────────────────────────────────────────────────
# False-positive fixes
# ──────────────────────────────────────────────────────────────────────────────

def test_timing_retard_b_no_false_positive_from_pull_end():
    # Timing drops only in the last 2 rows (post-pull decel/gear change artifact).
    # With 10% trim (2 rows for n=20), the rolling window never sees the drop.
    n = 20
    timing = np.full(n, 15.0)
    timing[-2:] = [5.0, 1.0]
    df = make_mhd_df(n=n, **{"Timing Cyl. 1 (*)": timing})
    results = B58DiagnosticEngine(df).run_analysis()
    all_flags = {a.flag for a in results.alerts} | {p.flag for p in results.performance_insights}
    assert "timing_retard_event_b" not in all_flags


def test_boost_taper_high_rpm_not_flagged_as_leak():
    # Boost deficit of 4 PSI only above 5500 RPM — should be boost_taper_high_rpm INFO, not boost_leak MAJOR
    n = 30
    rpm = np.linspace(3000.0, 7500.0, n)
    boost_actual = np.where(rpm > 5500.0, 16.0, 20.0)
    df = make_mhd_df(
        n=n,
        **{
            "RPM (rpm)": rpm,
            "Boost target (PSI)": np.full(n, 20.0),
            "Boost (PSI)": boost_actual,
        },
    )
    results = B58DiagnosticEngine(df).run_analysis()
    alert_flags = {a.flag for a in results.alerts}
    insight_flags = {p.flag for p in results.performance_insights}
    assert "boost_leak" not in alert_flags
    assert "boost_taper_high_rpm" in insight_flags


def test_wot_lean_excludes_spool_up_samples():
    # AFR lean only during spool-up (RPM < 3500). Post-spool AFR is fine.
    # throttle_afr_lean_c should NOT fire because the RPM filter excludes these samples.
    n = 20
    rpm = np.linspace(2000.0, 7000.0, n)
    afr = np.where(rpm < 3500.0, 15.0, 11.5)
    df = make_mhd_df(
        n=n,
        **{
            "RPM (rpm)": rpm,
            "AFR 1": afr,
            "AFR Target": np.full(n, 11.5),
        },
    )
    results = B58DiagnosticEngine(df).run_analysis()
    alert_flags = {a.flag for a in results.alerts}
    assert "throttle_afr_lean_c" not in alert_flags
