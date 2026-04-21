"""
B58DiagnosticEngine test suite.

All tests use synthetic DataFrames — no real log files required.
make_mhd_df() produces a single clean WOT pull that passes every check.
Override specific columns to exercise failure conditions.
"""

import numpy as np
import pandas as pd
import pytest

from app.engine.rules import B58DiagnosticEngine
from app.engine.models import AlertSeverity
from app.engine.thresholds import ThresholdConfig


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
    assert any("Clean Bill of Health" in d for d in report.diagnosis)


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
# Synthesis — reads flags not strings (2 tests)
# ──────────────────────────────────────────────────────────────────────────────

def test_synthesis_cascading_fuel_failure():
    df = make_mhd_df()
    df["Rail pressure mean 1 (PSI)"] = 1500.0   # hpfp_crash
    df["Fuel low pressure sensor (PSI)"] = 40.0  # lpfp_starvation
    report = B58DiagnosticEngine(df).run_analysis()
    assert any("Cascading Fuel Failure" in d for d in report.diagnosis)


def test_synthesis_hpfp_only_no_cascade():
    df = make_mhd_df()
    df["Rail pressure mean 1 (PSI)"] = 1500.0  # hpfp_crash only, LPFP is healthy
    report = B58DiagnosticEngine(df).run_analysis()
    assert any("HPFP Limit Reached" in d for d in report.diagnosis)
    assert not any("Cascading" in d for d in report.diagnosis)


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
