from typing import Optional, List, Tuple
import pandas as pd
import numpy as np

from .models import Alert, AlertSeverity, DiagnosticReport, PullSummary, SEVERITY_DEDUCTIONS, _AnalysisState
from .thresholds import ThresholdConfig


def _confidence_label(n: int) -> str:
    if n >= 2:
        return " (high confidence)"
    if n == 1:
        return " (moderate confidence)"
    return ""


class B58DiagnosticEngine:
    """
    Analyzes BMW B58 ECU CSV logs from MHD or BM3 tuning platforms.

    Detection is built on three pillars:
      A — Intra-log statistical normalization (compare car to itself)
      B — Rate-of-change / delta detection (patterns, not absolutes)
      C — Cross-parameter correlation (physical relationships)

    A single pillar finding is a flag. Cross-pillar corroboration is a diagnosis.
    Absolute thresholds (from ThresholdConfig) remain as a safety floor for
    conditions severe enough to warrant an alert regardless of baseline.

    The engine instance is read-only after __init__. run_analysis() creates a
    fresh _AnalysisState each call — safe to call multiple times or from concurrent
    Lambda invocations on a warm instance.
    """

    def __init__(self, df: pd.DataFrame, config: Optional[ThresholdConfig] = None):
        self.df = df.copy()
        self.cols = self.df.columns.tolist()
        self.config = config or ThresholdConfig()
        self.tune_platform = self._identify_tune_platform()
        self._normalize_mhd_units()
        self.cols = self.df.columns.tolist()
        self.map = self._normalize_col_names()
        self.engine_timing_cols = self._build_timing_cols()
        self.prime_log, self.prime_extracted_data = self._extract_wot_segments()
        strict_prime = self._extract_prime_wot_pull()
        if not strict_prime.empty:
            self.prime_log = strict_prime

    # ──────────────────────────────────────────────────────────────────────────
    # Init helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _identify_tune_platform(self) -> str:
        if "Accel Ped. Pos. (%)" in self.cols or "Cyl1 Timing Cor (*)" in self.cols:
            return "MHD"
        if "Accel. Pedal[%]" in self.cols or "Engine speed[1/min]" in self.cols:
            return "BM3"
        raise ValueError("Unsupported platform. Please upload an MHD or BM3 CSV log.")

    def _normalize_mhd_units(self) -> None:
        """
        MHD metric exports use Bar for pressure and °C for temperature.
        Create PSI/°F aliases so the column map and thresholds work unchanged.
        """
        if self.tune_platform != "MHD":
            return
        BAR_TO_PSI = 14.5038
        for src, dst in [
            ("Boost (Bar)",                "Boost (PSI)"),
            ("Boost target (Bar)",         "Boost target (PSI)"),
            ("Rail pressure mean 1 (Bar)", "Rail pressure mean 1 (PSI)"),
        ]:
            if src in self.cols and dst not in self.cols:
                self.df[dst] = pd.to_numeric(self.df[src], errors="coerce") * BAR_TO_PSI
        if "IAT (*C)" in self.cols and "IAT (*F)" not in self.cols:
            self.df["IAT (*F)"] = pd.to_numeric(self.df["IAT (*C)"], errors="coerce") * 9 / 5 + 32
        if "Charge air temp. (*C)" in self.cols and "Charge air temp. (*F)" not in self.cols:
            self.df["Charge air temp. (*F)"] = (
                pd.to_numeric(self.df["Charge air temp. (*C)"], errors="coerce") * 9 / 5 + 32
            )
        for src, dst in [
            ("STFT 1 (-)",    "STFT 1 (%)"),
            ("WGDC 1 (%)",    "WGDC (%)"),
            ("Lambda 1 (AFR)", "AFR 1"),
            ("Timing Cyl. 1",  "Timing Cyl. 1 (*)"),
        ]:
            if src in self.cols and dst not in self.cols:
                self.df[dst] = self.df[src]

    def _normalize_col_names(self) -> dict:
        if self.tune_platform == "MHD":
            return {
                "pedal":        "Accel Ped. Pos. (%)",
                "rpm":          "RPM (rpm)",
                "boost_target": "Boost target (PSI)",
                "boost_actual": "Boost (PSI)",
                "throttle":     "Throttle Position (*)",
                "rail":         "Rail pressure mean 1 (PSI)",
                "iat":          "IAT (*F)",
                "stft":         "STFT 1 (%)",
                "time":         "Time",
                "wgdc":         "WGDC (%)",
                "knock":        "Knock Detect",
                "lpfp":         "Fuel low pressure sensor (PSI)",
                "tq_lim":       "Torque Lim. active",
                "afr_target":   "AFR Target",
                "afr_actual":   "AFR 1",
                "load_target":  "Load req. (%)",
                "load_actual":  "Load act. (%)",
                "timing_adv":   "Timing Cyl. 1 (*)",
                "charge_air":   "Charge air temp. (*F)",
            }
        return {
            "pedal":        "Accel. Pedal[%]",
            "rpm":          "Engine speed[1/min]",
            "boost_target": "Boost pressure (Target)[psig]",
            "boost_actual": "Boost (Pre-Throttle)[psig]",
            "throttle":     "Throttle Angle[%]",
            "rail":         "HPFP Act.[psig]",
            "iat":          "IAT[F]",
            "stft":         "STFT 1[%]",
            "time":         "Time",
            "wgdc":         "WGDC[%]",
            "knock":        "Knock Detected",
            "lpfp":         "LPFP Act.[psig]",
            "tq_lim":       "Torque Limiter Active",
            "afr_target":   "AFR Target",
            "afr_actual":   "AFR",
            "load_target":  "Load Target[%]",
            "load_actual":  "Load Actual[%]",
            "timing_adv":   "(RAM) Ignition Timing Cyl. 1[°]",
            "charge_air":   "Charge Air Temp[F]",
        }

    def _build_timing_cols(self) -> List[str]:
        if self.tune_platform == "MHD":
            candidates = [f"Cyl{i} Timing Cor (*)" for i in range(1, 7)]
        else:
            candidates = [f"(RAM) Ignition Timing Corr. Cyl. {i}[°]" for i in range(1, 7)]
        return [c for c in candidates if c in self.cols]

    def _extract_prime_wot_pull(self) -> pd.DataFrame:
        """
        Find the single best WOT pull using a strict pedal threshold (default 99%).

        Bridges short gaps (ECU cut, fuel cut, TC blip) up to max_cutout_gap_seconds
        by including the gap rows in the returned slice so the time series stays
        continuous. Filters out candidates that never reach min_pull_rpm or are
        shorter than min_pull_duration_seconds. Returns the longest valid candidate,
        or an empty DataFrame if none qualify (caller keeps the 85% fallback).
        """
        pedal_col = self.map["pedal"]
        time_col = self.map["time"]
        rpm_col = self.map["rpm"]

        wot_mask = pd.to_numeric(self.df[pedal_col], errors="coerce") > self.config.wot_pedal_strict
        wot_rows = self.df[wot_mask]

        if wot_rows.empty:
            return pd.DataFrame()

        gap_labels = (wot_rows.index.to_series().diff() > 1).cumsum()
        raw_segs: List[pd.DataFrame] = [grp for _, grp in wot_rows.groupby(gap_labels)]

        # Merge adjacent segments separated by a short cut-out.
        # Include the intervening rows from df so the time series is unbroken.
        merged: List[pd.DataFrame] = [raw_segs[0]]
        for seg in raw_segs[1:]:
            prev = merged[-1]
            t_gap = (
                pd.to_numeric(seg[time_col].iloc[0], errors="coerce")
                - pd.to_numeric(prev[time_col].iloc[-1], errors="coerce")
            )
            if t_gap <= self.config.max_cutout_gap_seconds:
                merged[-1] = self.df.loc[prev.index[0]:seg.index[-1]].copy()
            else:
                merged.append(seg)

        valid: List[Tuple[float, pd.DataFrame]] = []
        for seg in merged:
            times = pd.to_numeric(seg[time_col], errors="coerce")
            duration = times.max() - times.min()
            max_rpm = pd.to_numeric(seg[rpm_col], errors="coerce").max()
            if duration >= self.config.min_pull_duration_seconds and max_rpm >= self.config.min_pull_rpm:
                valid.append((duration, seg))

        if not valid:
            return pd.DataFrame()

        return max(valid, key=lambda x: x[0])[1]

    def _extract_wot_segments(self) -> Tuple[pd.DataFrame, List[pd.DataFrame]]:
        pedal_col = self.map["pedal"]
        if pedal_col not in self.cols:
            return pd.DataFrame(), []

        self.df[pedal_col] = pd.to_numeric(self.df[pedal_col], errors="coerce")
        wot_rows = self.df[self.df[pedal_col] > self.config.wot_pedal_threshold]

        if wot_rows.empty:
            return pd.DataFrame(), []

        gap_labels = (wot_rows.index.to_series().diff() > 1).cumsum()
        all_segments = [group.copy() for _, group in wot_rows.groupby(gap_labels)]
        prime = max(all_segments, key=len)
        return prime, all_segments

    # ──────────────────────────────────────────────────────────────────────────
    # Main entry point
    # ──────────────────────────────────────────────────────────────────────────

    def run_analysis(self) -> Optional[DiagnosticReport]:
        if self.prime_log.empty:
            return None

        state = _AnalysisState()

        # Pillar A: establish non-WOT baseline for this car
        self._compute_baseline_stats(state)

        # Absolute safety floor checks (always run regardless of baseline)
        self._check_boost_with_spool_awareness(state)
        self._check_ignition_contextual(state)
        self._check_fuel_pressure(state)
        self._check_lpfp(state)
        self._check_throttle_closures(state)
        self._check_fuel_trims(state)
        self._check_iat_delta(state)
        self._check_charge_air_temp(state)
        self._check_wgdc(state)
        self._check_knock(state)
        self._check_torque_limiters(state)

        # Pillar A: dynamic deviation from car's own baseline
        self._check_rail_deviation_a(state)

        # Pillar B: rate-of-change / single-pull delta detection
        self._check_rail_drop_rate_b(state)
        self._check_timing_retard_events_b(state)
        self._check_afr_lean_swing_b(state)
        self._check_boost_mid_pull_drop_b(state)

        # Tuning quality checks
        self._check_afr(state)
        self._check_load(state)
        self._check_timing_advance(state)
        self._calculate_performance_metrics(state)

        # Multi-pull comparison (also runs Pillar B inter-pull checks internally)
        pull_comparison = self._compare_pulls(state)

        # Pillar C: cross-parameter physical relationships
        self._correlate_iat_timing_c(state)
        self._correlate_charge_air_timing_c(state)
        self._correlate_boost_rail_c(state)
        self._correlate_throttle_afr_c(state)

        self._synthesize_diagnosis(state)

        return DiagnosticReport(
            score=self._calculate_score(state),
            status="Needs Attention" if state.alerts else "Healthy",
            alerts=state.alerts,
            performance_insights=state.insights,
            diagnosis=state.diagnosis,
            pull_count=len(self.prime_extracted_data),
            pull_comparison=pull_comparison,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Pillar A — intra-log statistical normalization
    # ──────────────────────────────────────────────────────────────────────────

    def _compute_baseline_stats(self, state: _AnalysisState) -> None:
        """
        Build per-parameter baseline (mean, std) from non-WOT operating periods.
        Pillar A checks skip silently when baseline is absent (track-only logs).
        """
        pedal_col = self.map["pedal"]
        non_wot = self.df[pd.to_numeric(self.df[pedal_col], errors="coerce") < 20].copy()

        if len(non_wot) < self.config.baseline_min_rows:
            return

        for key in ("rail", "iat", "afr_actual", "boost_actual"):
            col = self.map.get(key)
            if col and col in self.cols:
                series = pd.to_numeric(non_wot[col], errors="coerce").dropna()
                if len(series) >= self.config.baseline_min_rows:
                    state.baseline[key] = {"mean": float(series.mean()), "std": float(series.std())}

    def _check_rail_deviation_a(self, state: _AnalysisState) -> None:
        """Pillar A: rail pressure during WOT deviates significantly from this car's cruise baseline."""
        if "rail" not in state.baseline:
            return
        stats = state.baseline["rail"]
        # Floor std at 50 PSI so a constant baseline still produces a meaningful z-score;
        # real logs have natural variation that keeps this floor inactive.
        effective_std = max(stats["std"], 50.0)

        rail = pd.to_numeric(self.prime_log[self.map["rail"]], errors="coerce")
        wot_mean = float(rail.mean())
        z = (wot_mean - stats["mean"]) / effective_std

        if z < -self.config.baseline_sigma:
            _rail_rpm = pd.to_numeric(self.prime_log[self.map["rpm"]], errors="coerce").dropna()
            _rail_range = (int(_rail_rpm.min()), int(_rail_rpm.max())) if not _rail_rpm.empty else None
            state.flags.add("rail_deviation_a")
            state.alerts.append(Alert(
                flag="rail_deviation_a",
                severity=AlertSeverity.MINOR,
                message=(
                    f"Rail Pressure (Dynamic): WOT mean {round(wot_mean)} PSI is "
                    f"{abs(round(z, 1))}σ below this car's non-WOT baseline "
                    f"({round(stats['mean'])} PSI ± {round(stats['std'])} PSI)."
                ),
                beginner_message=(
                    "Your fuel pump delivers noticeably less pressure during hard acceleration "
                    "than it does while cruising — a sign it may be struggling under load."
                ),
                rpm_range=_rail_range,
            ))

    # ──────────────────────────────────────────────────────────────────────────
    # Absolute safety floor checks
    # ──────────────────────────────────────────────────────────────────────────

    def _check_boost_with_spool_awareness(self, state: _AnalysisState) -> None:
        m = self.map
        target = pd.to_numeric(self.prime_log[m["boost_target"]], errors="coerce")
        actual = pd.to_numeric(self.prime_log[m["boost_actual"]], errors="coerce")
        delta = target - actual

        rpm_series = pd.to_numeric(self.prime_log[m["rpm"]], errors="coerce")
        post_spool = self.prime_log[rpm_series > self.config.post_spool_rpm]
        if post_spool.empty:
            return

        post_delta = delta.loc[post_spool.index]
        post_rpm = rpm_series.loc[post_spool.index]

        # Split deficit detection: mid-RPM deficit = real leak; high-RPM-only = normal taper
        mid_rpm_mask = post_rpm < self.config.boost_taper_rpm_threshold
        mid_max_deficit = post_delta[mid_rpm_mask].max() if mid_rpm_mask.any() else 0.0

        if mid_max_deficit > self.config.boost_delta_psi:
            state.flags.add("boost_leak")
            _mid_rpms = post_rpm[mid_rpm_mask].dropna()
            _leak_range = (int(_mid_rpms.min()), int(_mid_rpms.max())) if not _mid_rpms.empty else None
            state.alerts.append(Alert(
                flag="boost_leak",
                severity=AlertSeverity.MAJOR,
                message=f"Boost Leak: {round(mid_max_deficit, 1)} PSI under target in the power band.",
                beginner_message=(
                    "Boost is escaping somewhere — the engine asked for more pressure than it got "
                    "once the turbo was fully spooled. Check charge pipes and couplers for leaks."
                ),
                rpm_range=_leak_range,
            ))
        elif post_delta.max() > self.config.boost_delta_psi:
            # Deficit only at high RPM — normal power-band taper, not a leak
            state.flags.add("boost_taper_high_rpm")
            _high_rpms = post_rpm[~mid_rpm_mask].dropna()
            _taper_range = (int(_high_rpms.min()), int(_high_rpms.max())) if not _high_rpms.empty else None
            state.insights.append(Alert(
                flag="boost_taper_high_rpm",
                severity=AlertSeverity.INFO,
                message=(
                    f"High-RPM Boost Taper: Boost fell"
                    f"{round(post_delta.max(), 1)} PSI under target above "
                    f"{int(self.config.boost_taper_rpm_threshold)} RPM — "
                    f"normal power-band rolloff for this turbo."
                ),
                beginner_message=(
                    "Boost dropped slightly short of target only at very high RPM — "
                    "this is typical turbo behavior at the top of the power band, not a leak."
                ),
                rpm_range=_taper_range,
            ))

        if post_delta.min() < -self.config.boost_delta_psi:
            state.flags.add("overboost")
            _ob_idx = post_delta.idxmin()
            _ob_rpm = post_rpm.loc[_ob_idx]
            _ob_range = (int(_ob_rpm), int(_ob_rpm)) if pd.notna(_ob_rpm) else None
            state.alerts.append(Alert(
                flag="overboost",
                severity=AlertSeverity.MAJOR,
                message=f"Overboost: {abs(round(post_delta.min(), 1))} PSI over target detected.",
                beginner_message=(
                    "The turbo pushed more boost than the tune asked for — "
                    "this can stress engine components. Investigate boost solenoid or wastegate."
                ),
                rpm_range=_ob_range,
            ))

    def _check_ignition_contextual(self, state: _AnalysisState) -> None:
        if not self.engine_timing_cols:
            return
        timing = self.prime_log[self.engine_timing_cols].apply(pd.to_numeric, errors="coerce")
        min_val = timing.min().min()
        if min_val < self.config.min_timing_correction:
            worst_row = timing.min(axis=1).idxmin()
            worst_cyl = "".join(filter(str.isdigit, timing.loc[worst_row].idxmin()))
            _wr_rpm = pd.to_numeric(self.prime_log.loc[worst_row, self.map["rpm"]], errors="coerce")
            _wr_range = (int(_wr_rpm), int(_wr_rpm)) if pd.notna(_wr_rpm) else None
            state.flags.add("timing_pull")
            state.alerts.append(Alert(
                flag="timing_pull",
                severity=AlertSeverity.MINOR,
                message=f"Timing Pull: {round(min_val, 1)}° on Cyl {worst_cyl}.",
                beginner_message=(
                    f"Cylinder {worst_cyl} had its timing pulled back significantly — "
                    "the ECU detected borderline knock and backed off to protect the engine."
                ),
                rpm_range=_wr_range,
            ))

    def _check_fuel_pressure(self, state: _AnalysisState) -> None:
        rail = pd.to_numeric(self.prime_log[self.map["rail"]], errors="coerce")
        if rail.min() < self.config.min_rail_psi:
            _crash_idx = rail.idxmin()
            _crash_rpm = pd.to_numeric(self.prime_log.loc[_crash_idx, self.map["rpm"]], errors="coerce")
            _crash_range = (int(_crash_rpm), int(_crash_rpm)) if pd.notna(_crash_rpm) else None
            state.flags.add("hpfp_crash")
            state.alerts.append(Alert(
                flag="hpfp_crash",
                severity=AlertSeverity.MAJOR,
                message=f"🔴 HPFP Crash: Fuel pressure dipped to {int(rail.min())} PSI.",
                beginner_message=(
                    "Your high-pressure fuel pump dropped below a safe minimum — "
                    "the engine wasn't getting enough fuel under hard acceleration."
                ),
                rpm_range=_crash_range,
            ))

    def _check_lpfp(self, state: _AnalysisState) -> None:
        lpfp_col = self.map["lpfp"]
        if lpfp_col not in self.cols:
            return
        lpfp = pd.to_numeric(self.prime_log[lpfp_col], errors="coerce")
        if lpfp.min() < self.config.min_lpfp_psi:
            _starve_idx = lpfp.idxmin()
            _starve_rpm = pd.to_numeric(self.prime_log.loc[_starve_idx, self.map["rpm"]], errors="coerce")
            _starve_range = (int(_starve_rpm), int(_starve_rpm)) if pd.notna(_starve_rpm) else None
            state.flags.add("lpfp_starvation")
            state.alerts.append(Alert(
                flag="lpfp_starvation",
                severity=AlertSeverity.MAJOR,
                message=f"LPFP Starvation: Low-pressure pump dropped to {int(lpfp.min())} PSI.",
                beginner_message=(
                    "The in-tank fuel pump is starving — it can't supply enough fuel to the "
                    "high-pressure pump. This is the root cause of most HPFP problems."
                ),
                rpm_range=_starve_range,
            ))

    def _check_throttle_closures(self, state: _AnalysisState) -> None:
        throttle = pd.to_numeric(self.prime_log[self.map["throttle"]], errors="coerce")
        if throttle.min() < self.config.min_throttle_pct:
            _tc_idx = throttle.idxmin()
            _tc_rpm = pd.to_numeric(self.prime_log.loc[_tc_idx, self.map["rpm"]], errors="coerce")
            _tc_range = (int(_tc_rpm), int(_tc_rpm)) if pd.notna(_tc_rpm) else None
            state.flags.add("throttle_closure")
            state.insights.append(Alert(
                flag="throttle_closure",
                severity=AlertSeverity.INFO,
                message=f"Throttle Closure: ECU limited throttle to {int(throttle.min())}%.",
                beginner_message=(
                    "The computer briefly closed the throttle during the pull — "
                    "usually the transmission telling the engine to back off."
                ),
                rpm_range=_tc_range,
            ))

    def _check_fuel_trims(self, state: _AnalysisState) -> None:
        stft_col = self.map["stft"]
        if stft_col not in self.cols:
            return
        stft = pd.to_numeric(self.prime_log[stft_col], errors="coerce")
        if stft.max() > self.config.max_stft_pct:
            _stft_idx = stft.idxmax()
            _stft_rpm = pd.to_numeric(self.prime_log.loc[_stft_idx, self.map["rpm"]], errors="coerce")
            _stft_range = (int(_stft_rpm), int(_stft_rpm)) if pd.notna(_stft_rpm) else None
            state.flags.add("fuel_trims_high")
            state.alerts.append(Alert(
                flag="fuel_trims_high",
                severity=AlertSeverity.MINOR,
                message=f"⛽ Fuel Trims: STFT maxed at +{int(stft.max())}%.",
                beginner_message=(
                    "The ECU is adding a lot of extra fuel on the fly — "
                    "the base fueling map is running lean and the O2 sensor is compensating."
                ),
                rpm_range=_stft_range,
            ))

    def _check_iat_delta(self, state: _AnalysisState) -> None:
        iat_col = self.map["iat"]
        if iat_col not in self.cols:
            return
        iat = pd.to_numeric(self.prime_log[iat_col], errors="coerce").dropna()
        if iat.empty:
            return
        delta = iat.iloc[-1] - iat.iloc[0]
        _pull_rpm = pd.to_numeric(self.prime_log.loc[iat.index, self.map["rpm"]], errors="coerce").dropna()
        _pull_range = (int(_pull_rpm.min()), int(_pull_rpm.max())) if not _pull_rpm.empty else None
        if delta > self.config.max_iat_delta_alert:
            state.flags.add("iat_heat_soak")
            state.alerts.append(Alert(
                flag="iat_heat_soak",
                severity=AlertSeverity.MINOR,
                message=f"IAT Heat Soak: Intake temps rose {int(delta)}°F during the pull.",
                beginner_message=(
                    f"The air feeding the engine got {int(delta)}°F hotter during the pull — "
                    "denser cool air makes more power, so this hurts performance and triggers timing pull."
                ),
                rpm_range=_pull_range,
            ))
        elif delta > self.config.max_iat_delta_warn:
            state.flags.add("iat_rising")
            state.insights.append(Alert(
                flag="iat_rising",
                severity=AlertSeverity.INFO,
                message=f"IAT Rise: Intake temps rose {int(delta)}°F.",
                beginner_message="Intake temps climbed during the pull — nothing critical, but worth monitoring.",
                rpm_range=_pull_range,
            ))

    def _check_charge_air_temp(self, state: _AnalysisState) -> None:
        charge_air_col = self.map.get("charge_air")
        if not charge_air_col or charge_air_col not in self.cols:
            return
        ca = pd.to_numeric(self.prime_log[charge_air_col], errors="coerce").dropna()
        if ca.empty:
            return
        peak = float(ca.max())
        if peak > self.config.charge_air_critical_f:
            _ca_idx = ca.idxmax()
            _ca_rpm = pd.to_numeric(self.prime_log.loc[_ca_idx, self.map["rpm"]], errors="coerce")
            _ca_range = (int(_ca_rpm), int(_ca_rpm)) if pd.notna(_ca_rpm) else None
            state.charge_air_peak_f = peak
            state.flags.add("charge_air_high")
            state.alerts.append(Alert(
                flag="charge_air_high",
                severity=AlertSeverity.MAJOR,
                message=(
                    f"Charge Air Temp: Peak {round(peak)}°F during WOT — "
                    f"intercooler or charge pipe is overwhelmed."
                ),
                beginner_message=(
                    f"The air entering the engine reached {round(peak)}°F after the intercooler — "
                    "far hotter than it should be. Hot air is less dense, makes less power, "
                    "and forces the ECU to pull timing to protect the engine."
                ),
                rpm_range=_ca_range,
            ))

    def _check_wgdc(self, state: _AnalysisState) -> None:
        wgdc_col = self.map["wgdc"]
        if wgdc_col not in self.cols:
            return
        wgdc = pd.to_numeric(self.prime_log[wgdc_col], errors="coerce")
        if wgdc.max() > self.config.max_wgdc_pct:
            _sat_rpms = pd.to_numeric(
                self.prime_log.loc[wgdc[wgdc >= self.config.max_wgdc_pct].index, self.map["rpm"]],
                errors="coerce",
            ).dropna()
            _sat_range = (int(_sat_rpms.min()), int(_sat_rpms.max())) if not _sat_rpms.empty else None
            state.flags.add("wgdc_saturation")
            state.insights.append(Alert(
                flag="wgdc_saturation",
                severity=AlertSeverity.INFO,
                message="🐌 Turbo Headroom: WGDC at 100%. Turbo is at its physical limit.",
                beginner_message=(
                    "The wastegate is fully closed — the turbo is working as hard as it physically can. "
                    "If boost is still short of target, there's a leak. If boost is on target, the turbo is maxed."
                ),
                rpm_range=_sat_range,
            ))

    def _check_knock(self, state: _AnalysisState) -> None:
        knock_col = self.map["knock"]
        if knock_col not in self.cols:
            return
        knock = pd.to_numeric(self.prime_log[knock_col], errors="coerce")
        if knock.max() > 0:
            _knock_rpms = pd.to_numeric(
                self.prime_log.loc[knock[knock > 0].index, self.map["rpm"]], errors="coerce"
            ).dropna()
            _knock_range = (int(_knock_rpms.min()), int(_knock_rpms.max())) if not _knock_rpms.empty else None
            state.flags.add("knock")
            state.alerts.append(Alert(
                flag="knock",
                severity=AlertSeverity.CRITICAL,
                message="CRITICAL: Engine knock detected.",
                beginner_message=(
                    "The engine knocked — uncontrolled combustion that can destroy pistons. "
                    "Do not do another pull until you identify the cause."
                ),
                rpm_range=_knock_range,
            ))

    def _check_torque_limiters(self, state: _AnalysisState) -> None:
        tq_col = self.map["tq_lim"]
        if tq_col not in self.cols:
            return
        tq = pd.to_numeric(self.prime_log[tq_col], errors="coerce")
        if tq.max() > 0:
            _tq_rpms = pd.to_numeric(
                self.prime_log.loc[tq[tq > 0].index, self.map["rpm"]], errors="coerce"
            ).dropna()
            _tq_range = (int(_tq_rpms.min()), int(_tq_rpms.max())) if not _tq_rpms.empty else None
            state.flags.add("torque_limiter")
            state.insights.append(Alert(
                flag="torque_limiter",
                severity=AlertSeverity.INFO,
                message="Torque Intervention: TCU/ECU torque limiter was active.",
                beginner_message=(
                    "The transmission's computer stepped in and capped engine torque — "
                    "it's protecting the gearbox from more torque than it's rated to handle."
                ),
                rpm_range=_tq_range,
            ))

    # ──────────────────────────────────────────────────────────────────────────
    # Tuning quality checks
    # ──────────────────────────────────────────────────────────────────────────

    def _check_afr(self, state: _AnalysisState) -> None:
        m = self.map
        if m["afr_target"] not in self.cols or m["afr_actual"] not in self.cols:
            return
        target = pd.to_numeric(self.prime_log[m["afr_target"]], errors="coerce")
        actual = pd.to_numeric(self.prime_log[m["afr_actual"]], errors="coerce")
        diff = actual - target
        if diff.max() > self.config.max_afr_delta:
            _lean_idx = diff.idxmax()
            _lean_rpm = pd.to_numeric(self.prime_log.loc[_lean_idx, m["rpm"]], errors="coerce")
            _lean_range = (int(_lean_rpm), int(_lean_rpm)) if pd.notna(_lean_rpm) else None
            state.flags.add("dangerous_lean")
            state.alerts.append(Alert(
                flag="dangerous_lean",
                severity=AlertSeverity.CRITICAL,
                message=f"Dangerous Lean: AFR {round(diff.max(), 1)} points above target.",
                beginner_message=(
                    "The engine ran critically lean — way too little fuel for the air it consumed. "
                    "This can melt pistons. Do not pull again. Inspect injectors, pumps, and O2 sensor."
                ),
                rpm_range=_lean_range,
            ))

    def _check_load(self, state: _AnalysisState) -> None:
        m = self.map
        if m["load_target"] not in self.cols or m["load_actual"] not in self.cols:
            return
        target = pd.to_numeric(self.prime_log[m["load_target"]], errors="coerce")
        actual = pd.to_numeric(self.prime_log[m["load_actual"]], errors="coerce")
        miss = target - actual
        if miss.max() > self.config.max_load_miss_pct:
            _miss_idx = miss.idxmax()
            _miss_rpm = pd.to_numeric(self.prime_log.loc[_miss_idx, m["rpm"]], errors="coerce")
            _miss_range = (int(_miss_rpm), int(_miss_rpm)) if pd.notna(_miss_rpm) else None
            state.flags.add("load_miss")
            state.insights.append(Alert(
                flag="load_miss",
                severity=AlertSeverity.INFO,
                message="Load Miss: Engine missed load target by >15%. Power is reduced.",
                beginner_message=(
                    "The engine didn't reach the power level it was aiming for — "
                    "something is preventing it from filling the cylinders fully."
                ),
                rpm_range=_miss_range,
            ))

    def _check_timing_advance(self, state: _AnalysisState) -> None:
        adv_col = self.map["timing_adv"]
        if adv_col not in self.cols:
            return
        adv = pd.to_numeric(self.prime_log[adv_col], errors="coerce")
        if adv.max() < self.config.min_peak_timing_adv:
            _adv_rpm = pd.to_numeric(self.prime_log[self.map["rpm"]], errors="coerce").dropna()
            _adv_range = (int(_adv_rpm.min()), int(_adv_rpm.max())) if not _adv_rpm.empty else None
            state.flags.add("conservative_timing")
            state.insights.append(Alert(
                flag="conservative_timing",
                severity=AlertSeverity.INFO,
                message=f"🐢 Conservative Timing: Peak advance {round(adv.max(), 1)}°. Map may be octane limited.",
                beginner_message=(
                    "The tune is running less ignition advance than a healthy map would — "
                    "likely because the fuel quality or heat isn't allowing more timing."
                ),
                rpm_range=_adv_range,
            ))

    def _calculate_performance_metrics(self, state: _AnalysisState) -> None:
        m = self.map
        time_col = m["time"]
        if time_col not in self.prime_log.columns:
            return
        time_s = pd.to_numeric(self.prime_log[time_col], errors="coerce")
        rpm = pd.to_numeric(self.prime_log[m["rpm"]], errors="coerce")
        duration = time_s.iloc[-1] - time_s.iloc[0]
        if pd.notna(duration) and duration > 0:
            accel = int((rpm.iloc[-1] - rpm.iloc[0]) / duration)
            state.insights.append(Alert(
                flag="accel_rate",
                severity=AlertSeverity.INFO,
                message=f"Acceleration Rate: {accel} RPM/sec.",
            ))

    # ──────────────────────────────────────────────────────────────────────────
    # Pillar B — rate-of-change / single-pull delta detection
    # ──────────────────────────────────────────────────────────────────────────

    def _check_rail_drop_rate_b(self, state: _AnalysisState) -> None:
        """Pillar B: rail pressure drops at excessive rate during WOT ramp (pump struggling)."""
        m = self.map
        time_col = m["time"]
        if time_col not in self.prime_log.columns:
            return

        rpm = pd.to_numeric(self.prime_log[m["rpm"]], errors="coerce")
        rail = pd.to_numeric(self.prime_log[m["rail"]], errors="coerce")
        time_s = pd.to_numeric(self.prime_log[time_col], errors="coerce")

        post_spool_mask = rpm > self.config.post_spool_rpm
        if not post_spool_mask.any():
            return

        rail_ps = rail[post_spool_mask]
        time_ps = time_s[post_spool_mask]
        if len(rail_ps) < 3:
            return

        # Trim trailing 10% to exclude pull-end pressure normalization (decel artifact)
        trim = max(1, len(rail_ps) // 10)
        rail_ps = rail_ps.iloc[:-trim]
        time_ps = time_ps.iloc[:-trim]
        if len(rail_ps) < 3:
            return

        dt = time_ps.diff()
        d_rail = rail_ps.diff()
        with np.errstate(divide="ignore", invalid="ignore"):
            rate = d_rail / dt.replace(0, np.nan)

        min_rate = rate.min()
        if pd.notna(min_rate) and min_rate < -self.config.rail_drop_rate_psi_per_s:
            _rate_idx = rate.idxmin()
            _rate_rpm = pd.to_numeric(self.prime_log.loc[_rate_idx, self.map["rpm"]], errors="coerce")
            _rate_range = (int(_rate_rpm), int(_rate_rpm)) if pd.notna(_rate_rpm) else None
            state.flags.add("rail_drop_rate_b")
            state.alerts.append(Alert(
                flag="rail_drop_rate_b",
                severity=AlertSeverity.MAJOR,
                message=(
                    f"Rail Pressure Drop Rate (Pillar B): Fuel rail fell at "
                    f"{abs(round(min_rate))} PSI/s during WOT — "
                    f"exceeds safe rate ({self.config.rail_drop_rate_psi_per_s} PSI/s)."
                ),
                beginner_message=(
                    "Fuel pressure fell off sharply during the pull — "
                    "the pump couldn't keep up with what the engine needed right when boost hit."
                ),
                rpm_range=_rate_range,
            ))

    def _check_timing_retard_events_b(self, state: _AnalysisState) -> None:
        """Pillar B: sudden timing retard mid-pull — knock proxy even without a knock flag."""
        adv_col = self.map["timing_adv"]
        if adv_col not in self.cols:
            return
        adv = pd.to_numeric(self.prime_log[adv_col], errors="coerce").dropna()
        if len(adv) < 4:
            return

        # Trim trailing 10% to exclude pull-end timing map collapse (gear change, decel)
        trim = max(1, len(adv) // 10)
        adv = adv.iloc[:-trim]
        if len(adv) < 4:
            return

        # Max retard over any 3-sample rolling window
        rolling_drop = adv.rolling(3).apply(lambda w: w.iloc[0] - w.iloc[-1], raw=False)
        max_retard = rolling_drop.max()

        if pd.notna(max_retard) and max_retard > self.config.timing_retard_event_deg:
            _retard_idx = rolling_drop.idxmax()
            _retard_rpm = pd.to_numeric(self.prime_log.loc[_retard_idx, self.map["rpm"]], errors="coerce")
            _retard_range = (int(_retard_rpm), int(_retard_rpm)) if pd.notna(_retard_rpm) else None
            state.flags.add("timing_retard_event_b")
            state.alerts.append(Alert(
                flag="timing_retard_event_b",
                severity=AlertSeverity.MINOR,
                message=(
                    f"Timing Retard Event (Pillar B): {round(max_retard, 1)}° sudden retard "
                    f"detected mid-pull. ECU responded to borderline knock."
                ),
                beginner_message=(
                    "The computer suddenly yanked timing back during the pull — "
                    "it sensed the engine was about to knock and backed off to protect it."
                ),
                rpm_range=_retard_range,
            ))

    def _check_afr_lean_swing_b(self, state: _AnalysisState) -> None:
        """Pillar B: AFR swings lean mid-pull — fueling system can't sustain demand."""
        m = self.map
        if m["afr_actual"] not in self.cols or m["afr_target"] not in self.cols:
            return

        target = pd.to_numeric(self.prime_log[m["afr_target"]], errors="coerce")
        actual = pd.to_numeric(self.prime_log[m["afr_actual"]], errors="coerce")
        delta = actual - target  # positive = lean of target

        # Mid-pull only: exclude first and last 10% of samples
        n = len(delta)
        mid_start = max(1, n // 10)
        mid_end = min(n - 1, n - n // 10)
        mid_delta = delta.iloc[mid_start:mid_end]

        if mid_delta.empty:
            return

        max_lean = mid_delta.max()
        if max_lean > self.config.afr_lean_swing_delta:
            _afr_idx = mid_delta.idxmax()
            _afr_rpm = pd.to_numeric(self.prime_log.loc[_afr_idx, self.map["rpm"]], errors="coerce")
            _afr_range = (int(_afr_rpm), int(_afr_rpm)) if pd.notna(_afr_rpm) else None
            state.flags.add("afr_lean_swing_b")
            state.alerts.append(Alert(
                flag="afr_lean_swing_b",
                severity=AlertSeverity.MAJOR,
                message=(
                    f"AFR Lean Swing (Pillar B): AFR ran {round(max_lean, 2)} points lean "
                    f"of target in the middle of the pull — fueling fell behind demand."
                ),
                beginner_message=(
                    "The engine went lean in the middle of the pull — not at the start, not the end, "
                    "but right in the meat of it where the engine was working hardest."
                ),
                rpm_range=_afr_range,
            ))

    def _check_boost_mid_pull_drop_b(self, state: _AnalysisState) -> None:
        """Pillar B: boost drops from peak mid-pull after hitting target (leak, wastegate, surge)."""
        m = self.map
        rpm = pd.to_numeric(self.prime_log[m["rpm"]], errors="coerce")
        boost = pd.to_numeric(self.prime_log[m["boost_actual"]], errors="coerce")

        post_spool_mask = rpm > self.config.post_spool_rpm
        if not post_spool_mask.any():
            return

        boost_ps = boost[post_spool_mask]
        if len(boost_ps) < 4:
            return

        peak_idx = boost_ps.idxmax()
        peak_val = float(boost_ps[peak_idx])
        after_peak = boost_ps.loc[peak_idx:]

        if len(after_peak) < 3:
            return

        min_after = float(after_peak.min())
        drop = peak_val - min_after

        if drop > self.config.boost_mid_pull_drop_psi:
            _drop_end_idx = after_peak.idxmin()
            rpm_series = pd.to_numeric(self.prime_log[m["rpm"]], errors="coerce")
            _peak_rpm = rpm_series.loc[peak_idx]
            _end_rpm = rpm_series.loc[_drop_end_idx]
            if pd.notna(_peak_rpm) and pd.notna(_end_rpm):
                _drop_range: Optional[Tuple[int, int]] = (
                    int(min(_peak_rpm, _end_rpm)), int(max(_peak_rpm, _end_rpm))
                )
            else:
                _drop_range = None
            state.flags.add("boost_mid_pull_drop_b")
            state.insights.append(Alert(
                flag="boost_mid_pull_drop_b",
                severity=AlertSeverity.INFO,
                message=(
                    f"Boost Mid-Pull Drop (Pillar B): Boost fell {round(drop, 1)} PSI from peak "
                    f"({round(peak_val, 1)} → {round(min_after, 1)} PSI) post-spool."
                ),
                beginner_message=(
                    "Boost built up fine then dropped off mid-pull — "
                    "possible boost leak that only opens under pressure, wastegate creep, or compressor surge."
                ),
                rpm_range=_drop_range,
            ))

    # ──────────────────────────────────────────────────────────────────────────
    # Multi-pull comparison (includes Pillar B inter-pull checks)
    # ──────────────────────────────────────────────────────────────────────────

    def _compare_pulls(self, state: _AnalysisState) -> Optional[List[PullSummary]]:
        if len(self.prime_extracted_data) < 2:
            return None

        time_col = self.map["time"]
        state.sorted_pulls = sorted(
            self.prime_extracted_data,
            key=lambda p: pd.to_numeric(p[time_col], errors="coerce").iloc[0],
        )

        summaries: List[PullSummary] = []
        for i, pull in enumerate(state.sorted_pulls):
            mean_timing = None
            if self.engine_timing_cols:
                timing = pull[self.engine_timing_cols].apply(pd.to_numeric, errors="coerce")
                mean_timing = round(float(timing.mean().mean()), 2)

            max_boost = round(
                float(pd.to_numeric(pull[self.map["boost_actual"]], errors="coerce").max()), 1
            )

            iat_end = None
            iat_col = self.map["iat"]
            if iat_col in pull.columns:
                iat_end = round(
                    float(pd.to_numeric(pull[iat_col], errors="coerce").iloc[-1]), 1
                )

            summaries.append(PullSummary(
                pull_number=i + 1,
                mean_timing_correction=mean_timing,
                max_boost_psi=max_boost,
                iat_end_f=iat_end,
            ))

        # Progressive timing degradation across pulls
        timing_seq = [s.mean_timing_correction for s in summaries if s.mean_timing_correction is not None]
        if len(timing_seq) >= 2:
            is_degrading = all(timing_seq[i] < timing_seq[i - 1] for i in range(1, len(timing_seq)))
            if is_degrading:
                state.flags.add("timing_degradation_heat_soak")
                pull_values = ", ".join(f"{v:.1f}°" for v in timing_seq)
                state.alerts.append(Alert(
                    flag="timing_degradation_heat_soak",
                    severity=AlertSeverity.MAJOR,
                    message=(
                        f"Heat Soak Progression: Timing pulled further each run "
                        f"({pull_values}). Classic heat soak signature."
                    ),
                    beginner_message=(
                        "Each successive pull got more timing pulled — the engine got progressively "
                        "hotter and the computer backed off more each time to protect it."
                    ),
                ))

        # Pillar B: inter-pull checks
        self._check_iat_inter_pull_jump_b(state)
        self._check_boost_progression_b(state)

        return summaries

    def _check_iat_inter_pull_jump_b(self, state: _AnalysisState) -> None:
        """Pillar B: large IAT jump between pull-start readings — intercooler not recovering."""
        if len(state.sorted_pulls) < 2:
            return
        iat_col = self.map["iat"]
        if iat_col not in self.cols:
            return

        jumps = []
        for i in range(1, len(state.sorted_pulls)):
            prev_end = pd.to_numeric(state.sorted_pulls[i - 1][iat_col], errors="coerce").iloc[-1]
            curr_start = pd.to_numeric(state.sorted_pulls[i][iat_col], errors="coerce").iloc[0]
            if pd.notna(prev_end) and pd.notna(curr_start):
                jumps.append(curr_start - prev_end)

        if not jumps:
            return

        max_jump = max(jumps)
        if max_jump > self.config.iat_inter_pull_jump_f:
            state.flags.add("iat_inter_pull_jump_b")
            state.insights.append(Alert(
                flag="iat_inter_pull_jump_b",
                severity=AlertSeverity.INFO,
                message=(
                    f"IAT Inter-Pull Jump (Pillar B): Intake temps jumped {round(max_jump)}°F "
                    f"between pulls — intercooler is not recovering between runs."
                ),
                beginner_message=(
                    "Between back-to-back pulls, intake temps jumped significantly — "
                    "the intercooler isn't cooling down fast enough between runs."
                ),
            ))

    def _check_boost_progression_b(self, state: _AnalysisState) -> None:
        """Pillar B: peak boost declining monotonically across pulls — turbo or boost control issue."""
        if len(state.sorted_pulls) < 2:
            return

        peak_boosts = [
            float(pd.to_numeric(p[self.map["boost_actual"]], errors="coerce").max())
            for p in state.sorted_pulls
        ]

        # Require each pull to drop by more than 1 PSI to avoid noise
        is_declining = all(
            peak_boosts[i] < peak_boosts[i - 1] - 1.0 for i in range(1, len(peak_boosts))
        )
        if is_declining:
            state.flags.add("boost_degradation_b")
            vals = ", ".join(f"{b:.1f}" for b in peak_boosts)
            state.insights.append(Alert(
                flag="boost_degradation_b",
                severity=AlertSeverity.INFO,
                message=(
                    f"Boost Degradation (Pillar B): Peak boost declining each pull "
                    f"({vals} PSI)."
                ),
                beginner_message=(
                    "Each run makes a little less boost — the turbo may be heat-soaking, "
                    "or something in the boost control system is drifting."
                ),
            ))

    # ──────────────────────────────────────────────────────────────────────────
    # Pillar C — cross-parameter physical relationships
    # ──────────────────────────────────────────────────────────────────────────

    def _correlate_iat_timing_c(self, state: _AnalysisState) -> None:
        """
        Pillar C: IAT rises within the pull AND timing retards over the same window.
        Confirms heat-soak feedback loop — not octane or knock.
        """
        iat_col = self.map["iat"]
        adv_col = self.map["timing_adv"]
        if iat_col not in self.cols or adv_col not in self.cols:
            return

        iat = pd.to_numeric(self.prime_log[iat_col], errors="coerce").dropna()
        adv = pd.to_numeric(self.prime_log[adv_col], errors="coerce").dropna()
        common = iat.index.intersection(adv.index)
        if len(common) < 5:
            return

        iat_range = float(iat[common].max() - iat[common].min())
        if iat_range < self.config.iat_timing_rise_min_f:
            return

        with np.errstate(invalid="ignore"):
            corr = iat[common].corr(adv[common])

        if pd.notna(corr) and corr < self.config.iat_timing_corr_threshold:
            _iat_c_rpm = pd.to_numeric(self.prime_log.loc[common, self.map["rpm"]], errors="coerce").dropna()
            _iat_c_range = (int(_iat_c_rpm.min()), int(_iat_c_rpm.max())) if not _iat_c_rpm.empty else None
            state.flags.add("iat_timing_correlation_c")
            state.insights.append(Alert(
                flag="iat_timing_correlation_c",
                severity=AlertSeverity.INFO,
                message=(
                    f"IAT→Timing Correlation (Pillar C): IAT ranged {round(iat_range, 1)}°F "
                    f"over the pull (r={round(corr, 2)} with timing advance) — "
                    f"heat-soak feedback loop confirmed."
                ),
                beginner_message=(
                    "The data shows a direct cause-and-effect: air got hotter, computer pulled timing. "
                    "This is heat soak — not a fuel or knock problem."
                ),
                rpm_range=_iat_c_range,
            ))

    def _correlate_charge_air_timing_c(self, state: _AnalysisState) -> None:
        """
        Pillar C: charge air temp (post-IC) rises within the pull AND timing retards.
        More precise than the ambient-IAT correlation — confirms intercooler heat soak.
        """
        charge_air_col = self.map.get("charge_air")
        adv_col = self.map["timing_adv"]
        if not charge_air_col or charge_air_col not in self.cols or adv_col not in self.cols:
            return

        ca = pd.to_numeric(self.prime_log[charge_air_col], errors="coerce").dropna()
        adv = pd.to_numeric(self.prime_log[adv_col], errors="coerce").dropna()
        common = ca.index.intersection(adv.index)
        if len(common) < 5:
            return

        ca_range = float(ca[common].max() - ca[common].min())
        if ca_range < self.config.charge_air_rise_min_f:
            return

        with np.errstate(invalid="ignore"):
            corr = ca[common].corr(adv[common])

        if pd.notna(corr) and corr < self.config.iat_timing_corr_threshold:
            _ca_c_rpm = pd.to_numeric(self.prime_log.loc[common, self.map["rpm"]], errors="coerce").dropna()
            _ca_c_range = (int(_ca_c_rpm.min()), int(_ca_c_rpm.max())) if not _ca_c_rpm.empty else None
            state.flags.add("charge_air_timing_c")
            state.insights.append(Alert(
                flag="charge_air_timing_c",
                severity=AlertSeverity.INFO,
                message=(
                    f"Charge Air→Timing Correlation (Pillar C): Charge air ranged "
                    f"{round(ca_range, 1)}°F over the pull (r={round(corr, 2)} with timing advance) — "
                    f"intercooler heat soak confirmed."
                ),
                beginner_message=(
                    "The data shows a direct link: as the charge air got hotter, the ECU "
                    "pulled timing back in response. This is intercooler heat soak, not a fuel or knock issue."
                ),
                rpm_range=_ca_c_range,
            ))

    def _correlate_boost_rail_c(self, state: _AnalysisState) -> None:
        """
        Pillar C: boost climbs post-spool while rail pressure drops — HPFP can't meet fuel demand.
        Uses Pearson correlation: healthy = near-zero or positive, struggling = strongly negative.
        """
        m = self.map
        rpm = pd.to_numeric(self.prime_log[m["rpm"]], errors="coerce")
        boost = pd.to_numeric(self.prime_log[m["boost_actual"]], errors="coerce")
        rail = pd.to_numeric(self.prime_log[m["rail"]], errors="coerce")

        post_spool_mask = rpm > self.config.post_spool_rpm
        if not post_spool_mask.any():
            return

        boost_ps = boost[post_spool_mask]
        rail_ps = rail[post_spool_mask]
        if len(boost_ps) < 5:
            return

        with np.errstate(invalid="ignore"):
            corr = boost_ps.corr(rail_ps)
        rail_drop = float(rail_ps.max() - rail_ps.min())
        boost_rise = float(boost_ps.max() - boost_ps.min())

        if (
            pd.notna(corr)
            and corr < self.config.boost_rail_corr_threshold
            and rail_drop > self.config.boost_rail_min_drop_psi
            and boost_rise > 2.0
        ):
            _br_rpms = rpm[post_spool_mask].dropna()
            _br_range = (int(_br_rpms.min()), int(_br_rpms.max())) if not _br_rpms.empty else None
            state.flags.add("boost_rail_divergence_c")
            state.alerts.append(Alert(
                flag="boost_rail_divergence_c",
                severity=AlertSeverity.MAJOR,
                message=(
                    f"Boost↔Rail Divergence (Pillar C): As boost climbed, rail pressure fell "
                    f"{round(rail_drop)} PSI post-spool (r={round(corr, 2)}). "
                    f"HPFP is struggling to meet fuel demand under boost."
                ),
                beginner_message=(
                    "As the turbo pushed harder, fuel pressure dropped — these two should track "
                    "together, but they went in opposite directions. The fuel pump is falling behind."
                ),
                rpm_range=_br_range,
            ))

    def _correlate_throttle_afr_c(self, state: _AnalysisState) -> None:
        """
        Pillar C: at WOT throttle the AFR must be in the rich band.
        Flags when a significant fraction of full-throttle samples are lean.
        """
        m = self.map
        if m["afr_actual"] not in self.cols or m["throttle"] not in self.cols:
            return

        throttle = pd.to_numeric(self.prime_log[m["throttle"]], errors="coerce")
        actual = pd.to_numeric(self.prime_log[m["afr_actual"]], errors="coerce")
        rpm = pd.to_numeric(self.prime_log[m["rpm"]], errors="coerce")

        # Require post-spool RPM to exclude spool-up lean samples
        high_throttle_mask = (throttle > 95) & (rpm > self.config.post_spool_rpm)
        if not high_throttle_mask.any():
            return

        afr_wot = actual[high_throttle_mask]
        lean_wot = afr_wot[afr_wot > self.config.wot_lean_afr]

        if len(lean_wot) > len(afr_wot) * self.config.throttle_afr_lean_fraction:
            lean_pct = round(100 * len(lean_wot) / len(afr_wot))
            _wot_rpms = rpm[high_throttle_mask].dropna()
            _wot_range = (int(_wot_rpms.min()), int(_wot_rpms.max())) if not _wot_rpms.empty else None
            state.flags.add("throttle_afr_lean_c")
            state.alerts.append(Alert(
                flag="throttle_afr_lean_c",
                severity=AlertSeverity.MAJOR,
                message=(
                    f"WOT Lean (Pillar C): {lean_pct}% of full-throttle samples show "
                    f"AFR > {self.config.wot_lean_afr} (peak: {round(float(afr_wot.max()), 2)}). "
                    f"Fueling is not matching throttle demand."
                ),
                beginner_message=(
                    "Even with the pedal floored, the engine isn't getting enough fuel — "
                    "multiple sensor readings confirm it's lean at full throttle."
                ),
                rpm_range=_wot_range,
            ))

    # ──────────────────────────────────────────────────────────────────────────
    # Synthesis — cross-pillar correlation engine
    # ──────────────────────────────────────────────────────────────────────────

    def _synthesize_diagnosis(self, state: _AnalysisState) -> None:
        """
        Cross-reference flags from all three pillars to identify root causes.
        Single-pillar findings are reported with appropriate confidence.
        Multi-pillar corroboration raises confidence and sharpens the diagnosis.
        """
        f = state.flags

        # ── Fuel pressure / HPFP ─────────────────────────────────────────────
        if "hpfp_crash" in f and "lpfp_starvation" in f:
            n = sum(["rail_deviation_a" in f, "rail_drop_rate_b" in f, "boost_rail_divergence_c" in f])
            conf = _confidence_label(n)
            state.diagnosis.append(Alert(
                flag="dx_cascading_fuel_failure",
                severity=AlertSeverity.MAJOR,
                message=(
                    f"**Cascading Fuel Failure{conf}:** HPFP is crashing because the in-tank LPFP "
                    f"is failing to supply it. Fix or upgrade the LPFP first — replacing the HPFP alone "
                    f"will not resolve this.\n\n"
                    f"**Plain English:** Think of it as two pumps in series — the in-tank pump feeds "
                    f"the high-pressure pump. The in-tank pump is choking, so everything downstream starves. "
                    f"Start with the LPFP."
                ),
            ))

        elif "hpfp_crash" in f:
            n = sum(["rail_deviation_a" in f, "rail_drop_rate_b" in f, "boost_rail_divergence_c" in f])
            conf = _confidence_label(n)
            state.diagnosis.append(Alert(
                flag="dx_hpfp_limit",
                severity=AlertSeverity.MAJOR,
                message=(
                    f"**HPFP Limit Reached{conf}:** HPFP crashed but LPFP is healthy — "
                    f"you have exceeded the stock HPFP's physical capacity for this fuel blend. "
                    f"Consider a TU/Dorch upgrade or reduce E85 content.\n\n"
                    f"**Plain English:** Your high-pressure fuel pump hit its ceiling. "
                    f"The in-tank pump is fine — the fix is either a pump upgrade or less ethanol in the tank."
                ),
            ))

        elif "rail_drop_rate_b" in f and "boost_rail_divergence_c" in f:
            state.diagnosis.append(Alert(
                flag="dx_hpfp_early_warning",
                severity=AlertSeverity.MINOR,
                message=(
                    "**HPFP Under Load — Early Warning (Pillars B+C):** Rail pressure drops at an "
                    "excessive rate during WOT, and boost demand and rail pressure are moving in opposite "
                    "directions. The pump hasn't hard-crashed yet, but the pattern indicates it is "
                    "struggling. Monitor across logs; an upgrade may be warranted.\n\n"
                    "**Plain English:** Your fuel pump is working harder than it should and starting "
                    "to fall behind when boost builds. It hasn't failed yet — but it's telling you it will."
                ),
            ))

        elif "rail_deviation_a" in f and "rail_drop_rate_b" in f:
            state.diagnosis.append(Alert(
                flag="dx_hpfp_trending_low",
                severity=AlertSeverity.MINOR,
                message=(
                    "**HPFP Trending Low (Pillars A+B):** Rail pressure is below this car's own "
                    "non-WOT baseline AND drops at an elevated rate during the pull. "
                    "Watch for progression across logs.\n\n"
                    "**Plain English:** Fuel pressure is lower than normal for this car and falls "
                    "quickly when the engine works hard. Worth watching before it becomes a bigger problem."
                ),
            ))

        # ── AFR / fueling ────────────────────────────────────────────────────
        if "dangerous_lean" in f:
            if "afr_lean_swing_b" in f or "throttle_afr_lean_c" in f:
                state.diagnosis.append(Alert(
                    flag="dx_dangerous_lean_critical",
                    severity=AlertSeverity.CRITICAL,
                    message=(
                        "**CRITICAL SAFETY — Do Not Drive (High Confidence):** A dangerous lean "
                        "condition is confirmed by multiple detection methods. The engine ran critically "
                        "short on fuel at WOT. Do not do another pull. "
                        "Inspect injectors, fuel pumps, and primary O2 sensor immediately.\n\n"
                        "**Plain English:** Multiple sensor readings agree — not enough fuel reached "
                        "the engine during hard acceleration. This can melt pistons. Stop pulling now."
                    ),
                ))
            else:
                state.diagnosis.append(Alert(
                    flag="dx_dangerous_lean",
                    severity=AlertSeverity.CRITICAL,
                    message=(
                        "**CRITICAL SAFETY — Do Not Drive:** The engine ran dangerously lean at WOT. "
                        "Do not do another pull. Inspect injectors, fuel pumps, and primary O2 sensor.\n\n"
                        "**Plain English:** Way too little fuel for the air the engine consumed. "
                        "Find the cause before driving hard again."
                    ),
                ))

        elif "afr_lean_swing_b" in f and "throttle_afr_lean_c" in f:
            state.diagnosis.append(Alert(
                flag="dx_marginal_fueling",
                severity=AlertSeverity.MAJOR,
                message=(
                    "**Marginal Fueling — High Attention (Pillars B+C):** AFR swings lean mid-pull "
                    "and lean samples cluster at full throttle — the fueling system is at its limit "
                    "under peak demand. Not yet at the dangerous threshold, but this is the pattern "
                    "that precedes a dangerous lean event.\n\n"
                    "**Plain English:** The car is running lean when you push it hardest, confirmed "
                    "from two angles. It hasn't hit the danger zone yet — but it's close. "
                    "Sort the fueling before it gets worse."
                ),
            ))

        # ── Boost / turbo ────────────────────────────────────────────────────
        if "boost_leak" in f and "wgdc_saturation" in f and "boost_mid_pull_drop_b" in f:
            state.diagnosis.append(Alert(
                flag="dx_boost_leak_saturated",
                severity=AlertSeverity.MAJOR,
                message=(
                    "**Boost Leak — Turbo Saturated:** A boost leak is confirmed by a power-band "
                    "deficit, mid-pull boost drop, and the wastegate fully closed compensating. "
                    "Check charge pipes and inlet couplers.\n\n"
                    "**Plain English:** Boost is escaping under pressure. The turbo is working at "
                    "100% just to compensate and still can't hit target. Find and seal the leak."
                ),
            ))
        elif "boost_leak" in f and "wgdc_saturation" in f:
            state.diagnosis.append(Alert(
                flag="dx_boost_leak",
                severity=AlertSeverity.MAJOR,
                message=(
                    "**Boost Leak:** Power-band boost deficit with wastegate fully closed — "
                    "boost is escaping faster than the turbo can compensate. "
                    "Check charge pipes and inlet couplers.\n\n"
                    "**Plain English:** The turbo is working at 100% and still can't reach target. "
                    "Something is leaking — check all charge pipes and couplers."
                ),
            ))
        elif "boost_leak" in f:
            state.diagnosis.append(Alert(
                flag="dx_boost_deficit",
                severity=AlertSeverity.MINOR,
                message=(
                    "**Boost Deficit:** Boost is consistently under target in the power band. "
                    "Possible boost leak, wastegate issue, or turbo limitation.\n\n"
                    "**Plain English:** The engine isn't getting the boost it's asking for. "
                    "Check charge pipes for leaks and confirm the wastegate is sealing properly."
                ),
            ))

        if "boost_mid_pull_drop_b" in f and "boost_leak" not in f:
            state.diagnosis.append(Alert(
                flag="dx_boost_mid_pull_anomaly",
                severity=AlertSeverity.MINOR,
                message=(
                    "**Mid-Pull Boost Anomaly (Pillar B):** Boost builds correctly then drops "
                    "unexpectedly after hitting peak — possible intermittent boost leak (only opens "
                    "under full pressure), wastegate creep, or compressor surge. "
                    "Check if this repeats across logs.\n\n"
                    "**Plain English:** Boost was building fine then fell off mid-pull for no "
                    "obvious reason. Could be a coupler that only leaks under full pressure, "
                    "a wastegate that doesn't hold, or the turbo hitting its surge point."
                ),
            ))

        # ── Knock / timing ───────────────────────────────────────────────────
        thermal_flags = {"iat_heat_soak", "charge_air_high", "charge_air_timing_c"}
        if ("knock" in f or "timing_pull" in f) and not (f & thermal_flags):
            if "timing_retard_event_b" in f:
                state.diagnosis.append(Alert(
                    flag="dx_knock_octane_limit_high",
                    severity=AlertSeverity.MAJOR,
                    message=(
                        "**Knock — Octane Limit (High Confidence, Pillars A+B):** "
                        "Cylinder timing corrections, an ECU knock flag, and a mid-pull retard "
                        "event all independently point to the same cause: the fuel is not adequate "
                        "for this tune's timing targets. "
                        "Add 1–2 gallons of E85 or flash to a lower-octane map.\n\n"
                        "**Plain English:** Three separate checks all say the same thing — the fuel "
                        "and the tune aren't matched. Better fuel or a safer map is the fix."
                    ),
                ))
            else:
                state.diagnosis.append(Alert(
                    flag="dx_knock_octane_limit",
                    severity=AlertSeverity.MINOR,
                    message=(
                        "**Octane Limit:** Timing corrections present without significant heat soak — "
                        "points to fuel quality. Try adding 1–2 gallons of E85 or flash a lower-octane map.\n\n"
                        "**Plain English:** The car pulled timing because of fuel quality, not heat. "
                        "Better fuel or a safer tune map will fix this."
                    ),
                ))

        # ── Charge air / intercooler heat soak ───────────────────────────────
        if "charge_air_high" in f:
            n = sum(["charge_air_timing_c" in f, "iat_timing_correlation_c" in f])
            conf = _confidence_label(n)
            peak_str = f" ({round(state.charge_air_peak_f)}°F)" if state.charge_air_peak_f is not None else ""
            state.diagnosis.append(Alert(
                flag="dx_intercooler_heat_soak",
                severity=AlertSeverity.MAJOR,
                message=(
                    f"**Intercooler Heat Soak{conf}:** Charge air temps{peak_str} are far beyond "
                    f"what the intercooler can handle at this power level. This directly causes timing "
                    f"retard and power loss — the ECU protects the engine by pulling advance as temps climb. "
                    f"Allow more cool-down time between pulls; consider an intercooler or charge pipe upgrade.\n\n"
                    f"**Plain English:** The air entering the engine is too hot after the intercooler. "
                    f"Hot air = less power and the computer pulls timing to prevent detonation. "
                    f"Cool down between runs. If it persists, the intercooler isn't big enough for this tune."
                ),
            ))

        # ── Heat soak ────────────────────────────────────────────────────────
        if "timing_degradation_heat_soak" in f:
            if "iat_timing_correlation_c" in f or "iat_inter_pull_jump_b" in f:
                state.diagnosis.append(Alert(
                    flag="dx_heat_soak_multi_pull",
                    severity=AlertSeverity.MINOR,
                    message=(
                        "**Inter-Run Heat Soak (Confirmed, Multiple Pillars):** Progressive timing "
                        "degradation across pulls is corroborated by IAT→timing correlation and/or "
                        "inter-pull IAT jumps. Allow 5–10 minutes of cooling between runs. "
                        "If it persists with adequate cooling, investigate intercooler and charge pipe.\n\n"
                        "**Plain English:** Each pull gets hotter and the computer pulls more timing "
                        "each time — confirmed by multiple data sources. Cool-down time between pulls "
                        "is the immediate fix. If that's not enough, consider an intercooler upgrade."
                    ),
                ))
            else:
                state.diagnosis.append(Alert(
                    flag="dx_heat_soak_single",
                    severity=AlertSeverity.MINOR,
                    message=(
                        "**Inter-Run Heat Soak:** Timing pulled further on each successive run. "
                        "Allow 5–10 minutes of cooling between pulls. "
                        "If persistent, consider upgraded charge pipe or intercooler.\n\n"
                        "**Plain English:** The car gets more heat-soaked with each run. "
                        "More cool-down time between pulls is the fix."
                    ),
                ))

        elif ("iat_timing_correlation_c" in f or "charge_air_timing_c" in f) and "iat_heat_soak" in f:
            state.diagnosis.append(Alert(
                flag="dx_heat_soak_single_pull_timing",
                severity=AlertSeverity.MINOR,
                message=(
                    "**Single-Pull Heat Soak (Confirmed, Pillar C):** IAT rose and timing retarded "
                    "in direct correlation within this pull — heat-soak feedback loop is active. "
                    "The car arrived heat-soaked or the intercooler is undersized for this power level.\n\n"
                    "**Plain English:** The air got hotter during the pull and the computer backed "
                    "off timing directly in response — both happened in lockstep in the same run."
                ),
            ))

        # ── TCU ──────────────────────────────────────────────────────────────
        if "throttle_closure" in f and "torque_limiter" in f:
            state.diagnosis.append(Alert(
                flag="dx_tcu_intervention",
                severity=AlertSeverity.INFO,
                message=(
                    "**TCU Intervention:** Engine torque is exceeding the transmission's "
                    "programmed limit, causing throttle closures. "
                    "An xHP transmission tune may be needed to raise the torque cap.\n\n"
                    "**Plain English:** The gearbox is telling the engine to slow down — "
                    "it's getting more torque than it's calibrated to handle. "
                    "An xHP tune can raise that limit."
                ),
            ))

        # ── Stand-alone insights ──────────────────────────────────────────────
        if "iat_inter_pull_jump_b" in f and "timing_degradation_heat_soak" not in f:
            state.diagnosis.append(Alert(
                flag="dx_intercooler_recovery",
                severity=AlertSeverity.INFO,
                message=(
                    "**Intercooler Recovery (Pillar B):** IAT jumps significantly between pulls "
                    "but timing hasn't walked back yet — the intercooler is under-recovering. "
                    "This is the precursor to heat-soak timing degradation.\n\n"
                    "**Plain English:** Intake temps spike between runs even though timing is still "
                    "holding. The intercooler isn't cooling down fast enough between pulls."
                ),
            ))

        if "boost_degradation_b" in f and "boost_leak" not in f and "wgdc_saturation" not in f:
            state.diagnosis.append(Alert(
                flag="dx_boost_degradation",
                severity=AlertSeverity.MINOR,
                message=(
                    "**Boost Falling Across Session (Pillar B):** Peak boost declining across "
                    "pulls without a confirmed boost leak. Possible causes: turbo heat soak, "
                    "wastegate actuator drift, or boost solenoid degradation.\n\n"
                    "**Plain English:** Each run makes a little less boost for no obvious reason. "
                    "Could be the turbo getting too hot, or something loosening in the boost control system."
                ),
            ))

        # ── Clean bill of health ──────────────────────────────────────────────
        if not state.diagnosis and not state.alerts:
            state.diagnosis.append(Alert(
                flag="dx_clean",
                severity=AlertSeverity.INFO,
                message=(
                    "**Clean Bill of Health:** Hardware is happy, fuel pressure is stable, "
                    "and timing is clean. The car is running exactly as your tuner intended.\n\n"
                    "**Plain English:** Nothing wrong — no issues found across any check."
                ),
            ))

    # ──────────────────────────────────────────────────────────────────────────
    # Scoring
    # ──────────────────────────────────────────────────────────────────────────

    def _calculate_score(self, state: _AnalysisState) -> int:
        if any(a.severity == AlertSeverity.CRITICAL for a in state.alerts):
            return 0
        deductions = sum(
            SEVERITY_DEDUCTIONS.get(a.severity, 0) or 0
            for a in state.alerts
        )
        return max(10, 100 - deductions)
