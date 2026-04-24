from typing import Optional, List, Tuple
import pandas as pd
import numpy as np

from .models import Alert, AlertSeverity, DiagnosticReport, PullSummary, SEVERITY_DEDUCTIONS
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

        self._flags: set = set()
        self._alerts: List[Alert] = []
        self._insights: List[Alert] = []
        self._diagnosis: List[str] = []
        self._sorted_pulls: List[pd.DataFrame] = []

        # Pillar A: establish non-WOT baseline for this car
        self._compute_baseline_stats()

        # Absolute safety floor checks (always run regardless of baseline)
        self._check_boost_with_spool_awareness()
        self._check_ignition_contextual()
        self._check_fuel_pressure()
        self._check_lpfp()
        self._check_throttle_closures()
        self._check_fuel_trims()
        self._check_iat_delta()
        self._check_wgdc()
        self._check_knock()
        self._check_torque_limiters()

        # Pillar A: dynamic deviation from car's own baseline
        self._check_rail_deviation_a()

        # Pillar B: rate-of-change / single-pull delta detection
        self._check_rail_drop_rate_b()
        self._check_timing_retard_events_b()
        self._check_afr_lean_swing_b()
        self._check_boost_mid_pull_drop_b()

        # Tuning quality checks
        self._check_afr()
        self._check_load()
        self._check_timing_advance()
        self._calculate_performance_metrics()

        # Multi-pull comparison (also runs Pillar B inter-pull checks internally)
        pull_comparison = self._compare_pulls()

        # Pillar C: cross-parameter physical relationships
        self._correlate_iat_timing_c()
        self._correlate_boost_rail_c()
        self._correlate_throttle_afr_c()

        self._synthesize_diagnosis()

        return DiagnosticReport(
            score=self._calculate_score(),
            status="Needs Attention" if self._alerts else "Healthy",
            alerts=self._alerts,
            performance_insights=self._insights,
            diagnosis=self._diagnosis,
            pull_count=len(self.prime_extracted_data),
            pull_comparison=pull_comparison,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Pillar A — intra-log statistical normalization
    # ──────────────────────────────────────────────────────────────────────────

    def _compute_baseline_stats(self) -> None:
        """
        Build per-parameter baseline (mean, std) from non-WOT operating periods.
        Pillar A checks skip silently when baseline is absent (track-only logs).
        """
        pedal_col = self.map["pedal"]
        non_wot = self.df[pd.to_numeric(self.df[pedal_col], errors="coerce") < 20].copy()
        self._baseline: dict = {}

        if len(non_wot) < self.config.baseline_min_rows:
            return

        for key in ("rail", "iat", "afr_actual", "boost_actual"):
            col = self.map.get(key)
            if col and col in self.cols:
                series = pd.to_numeric(non_wot[col], errors="coerce").dropna()
                if len(series) >= self.config.baseline_min_rows:
                    self._baseline[key] = {"mean": float(series.mean()), "std": float(series.std())}

    def _check_rail_deviation_a(self) -> None:
        """Pillar A: rail pressure during WOT deviates significantly from this car's cruise baseline."""
        if "rail" not in self._baseline:
            return
        stats = self._baseline["rail"]
        # Floor std at 50 PSI so a constant baseline still produces a meaningful z-score;
        # real logs have natural variation that keeps this floor inactive.
        effective_std = max(stats["std"], 50.0)

        rail = pd.to_numeric(self.prime_log[self.map["rail"]], errors="coerce")
        wot_mean = float(rail.mean())
        z = (wot_mean - stats["mean"]) / effective_std

        if z < -self.config.baseline_sigma:
            self._flags.add("rail_deviation_a")
            self._alerts.append(Alert(
                flag="rail_deviation_a",
                severity=AlertSeverity.MINOR,
                message=(
                    f"⚡ Rail Pressure (Dynamic): WOT mean {round(wot_mean)} PSI is "
                    f"{abs(round(z, 1))}σ below this car's non-WOT baseline "
                    f"({round(stats['mean'])} PSI ± {round(stats['std'])} PSI)."
                ),
                beginner_message=(
                    "Your fuel pump delivers noticeably less pressure during hard acceleration "
                    "than it does while cruising — a sign it may be struggling under load."
                ),
            ))

    # ──────────────────────────────────────────────────────────────────────────
    # Absolute safety floor checks
    # ──────────────────────────────────────────────────────────────────────────

    def _check_boost_with_spool_awareness(self) -> None:
        m = self.map
        target = pd.to_numeric(self.prime_log[m["boost_target"]], errors="coerce")
        actual = pd.to_numeric(self.prime_log[m["boost_actual"]], errors="coerce")
        delta = target - actual

        post_spool = self.prime_log[
            pd.to_numeric(self.prime_log[m["rpm"]], errors="coerce") > self.config.post_spool_rpm
        ]
        if post_spool.empty:
            return

        post_delta = delta.loc[post_spool.index]

        if post_delta.max() > self.config.boost_delta_psi:
            self._flags.add("boost_leak")
            self._alerts.append(Alert(
                flag="boost_leak",
                severity=AlertSeverity.MAJOR,
                message=f"💨 Boost Leak: {round(post_delta.max(), 1)} PSI under target post-spool.",
                beginner_message=(
                    "Boost is escaping somewhere — the engine asked for more pressure than it got "
                    "once the turbo was fully spooled. Check charge pipes and couplers for leaks."
                ),
            ))

        if post_delta.min() < -self.config.boost_delta_psi:
            self._flags.add("overboost")
            self._alerts.append(Alert(
                flag="overboost",
                severity=AlertSeverity.MAJOR,
                message=f"⚠️ Overboost: {abs(round(post_delta.min(), 1))} PSI over target detected.",
                beginner_message=(
                    "The turbo pushed more boost than the tune asked for — "
                    "this can stress engine components. Investigate boost solenoid or wastegate."
                ),
            ))

    def _check_ignition_contextual(self) -> None:
        if not self.engine_timing_cols:
            return
        timing = self.prime_log[self.engine_timing_cols].apply(pd.to_numeric, errors="coerce")
        min_val = timing.min().min()
        if min_val < self.config.min_timing_correction:
            worst_row = timing.min(axis=1).idxmin()
            worst_cyl = "".join(filter(str.isdigit, timing.loc[worst_row].idxmin()))
            self._flags.add("timing_pull")
            self._alerts.append(Alert(
                flag="timing_pull",
                severity=AlertSeverity.MINOR,
                message=f"🔥 Timing Pull: {round(min_val, 1)}° on Cyl {worst_cyl}.",
                beginner_message=(
                    f"Cylinder {worst_cyl} had its timing pulled back significantly — "
                    "the ECU detected borderline knock and backed off to protect the engine."
                ),
            ))

    def _check_fuel_pressure(self) -> None:
        rail = pd.to_numeric(self.prime_log[self.map["rail"]], errors="coerce")
        if rail.min() < self.config.min_rail_psi:
            self._flags.add("hpfp_crash")
            self._alerts.append(Alert(
                flag="hpfp_crash",
                severity=AlertSeverity.MAJOR,
                message=f"🔴 HPFP Crash: Fuel pressure dipped to {int(rail.min())} PSI.",
                beginner_message=(
                    "Your high-pressure fuel pump dropped below a safe minimum — "
                    "the engine wasn't getting enough fuel under hard acceleration."
                ),
            ))

    def _check_lpfp(self) -> None:
        lpfp_col = self.map["lpfp"]
        if lpfp_col not in self.cols:
            return
        lpfp = pd.to_numeric(self.prime_log[lpfp_col], errors="coerce")
        if lpfp.min() < self.config.min_lpfp_psi:
            self._flags.add("lpfp_starvation")
            self._alerts.append(Alert(
                flag="lpfp_starvation",
                severity=AlertSeverity.MAJOR,
                message=f"📉 LPFP Starvation: Low-pressure pump dropped to {int(lpfp.min())} PSI.",
                beginner_message=(
                    "The in-tank fuel pump is starving — it can't supply enough fuel to the "
                    "high-pressure pump. This is the root cause of most HPFP problems."
                ),
            ))

    def _check_throttle_closures(self) -> None:
        throttle = pd.to_numeric(self.prime_log[self.map["throttle"]], errors="coerce")
        if throttle.min() < self.config.min_throttle_pct:
            self._flags.add("throttle_closure")
            self._insights.append(Alert(
                flag="throttle_closure",
                severity=AlertSeverity.MINOR,
                message=f"🟡 Throttle Closure: ECU limited throttle to {int(throttle.min())}%.",
                beginner_message=(
                    "The computer briefly closed the throttle during the pull — "
                    "usually the transmission telling the engine to back off."
                ),
            ))

    def _check_fuel_trims(self) -> None:
        stft_col = self.map["stft"]
        if stft_col not in self.cols:
            return
        stft = pd.to_numeric(self.prime_log[stft_col], errors="coerce")
        if stft.max() > self.config.max_stft_pct:
            self._flags.add("fuel_trims_high")
            self._alerts.append(Alert(
                flag="fuel_trims_high",
                severity=AlertSeverity.MINOR,
                message=f"⛽ Fuel Trims: STFT maxed at +{int(stft.max())}%.",
                beginner_message=(
                    "The ECU is adding a lot of extra fuel on the fly — "
                    "the base fueling map is running lean and the O2 sensor is compensating."
                ),
            ))

    def _check_iat_delta(self) -> None:
        iat_col = self.map["iat"]
        if iat_col not in self.cols:
            return
        iat = pd.to_numeric(self.prime_log[iat_col], errors="coerce").dropna()
        if iat.empty:
            return
        delta = iat.iloc[-1] - iat.iloc[0]
        if delta > self.config.max_iat_delta_alert:
            self._flags.add("iat_heat_soak")
            self._alerts.append(Alert(
                flag="iat_heat_soak",
                severity=AlertSeverity.MINOR,
                message=f"🌡️ IAT Heat Soak: Intake temps rose {int(delta)}°F during the pull.",
                beginner_message=(
                    f"The air feeding the engine got {int(delta)}°F hotter during the pull — "
                    "denser cool air makes more power, so this hurts performance and triggers timing pull."
                ),
            ))
        elif delta > self.config.max_iat_delta_warn:
            self._flags.add("iat_rising")
            self._insights.append(Alert(
                flag="iat_rising",
                severity=AlertSeverity.INFO,
                message=f"🟡 IAT Rise: Intake temps rose {int(delta)}°F.",
                beginner_message="Intake temps climbed during the pull — nothing critical, but worth monitoring.",
            ))

    def _check_wgdc(self) -> None:
        wgdc_col = self.map["wgdc"]
        if wgdc_col not in self.cols:
            return
        wgdc = pd.to_numeric(self.prime_log[wgdc_col], errors="coerce")
        if wgdc.max() > self.config.max_wgdc_pct:
            self._flags.add("wgdc_saturation")
            self._insights.append(Alert(
                flag="wgdc_saturation",
                severity=AlertSeverity.INFO,
                message="🐌 Turbo Headroom: WGDC at 100%. Turbo is at its physical limit.",
                beginner_message=(
                    "The wastegate is fully closed — the turbo is working as hard as it physically can. "
                    "If boost is still short of target, there's a leak. If boost is on target, the turbo is maxed."
                ),
            ))

    def _check_knock(self) -> None:
        knock_col = self.map["knock"]
        if knock_col not in self.cols:
            return
        knock = pd.to_numeric(self.prime_log[knock_col], errors="coerce")
        if knock.max() > 0:
            self._flags.add("knock")
            self._alerts.append(Alert(
                flag="knock",
                severity=AlertSeverity.CRITICAL,
                message="🚨 CRITICAL: Engine knock detected.",
                beginner_message=(
                    "The engine knocked — uncontrolled combustion that can destroy pistons. "
                    "Do not do another pull until you identify the cause."
                ),
            ))

    def _check_torque_limiters(self) -> None:
        tq_col = self.map["tq_lim"]
        if tq_col not in self.cols:
            return
        tq = pd.to_numeric(self.prime_log[tq_col], errors="coerce")
        if tq.max() > 0:
            self._flags.add("torque_limiter")
            self._insights.append(Alert(
                flag="torque_limiter",
                severity=AlertSeverity.INFO,
                message="⚙️ Torque Intervention: TCU/ECU torque limiter was active.",
                beginner_message=(
                    "The transmission's computer stepped in and capped engine torque — "
                    "it's protecting the gearbox from more torque than it's rated to handle."
                ),
            ))

    # ──────────────────────────────────────────────────────────────────────────
    # Tuning quality checks
    # ──────────────────────────────────────────────────────────────────────────

    def _check_afr(self) -> None:
        m = self.map
        if m["afr_target"] not in self.cols or m["afr_actual"] not in self.cols:
            return
        target = pd.to_numeric(self.prime_log[m["afr_target"]], errors="coerce")
        actual = pd.to_numeric(self.prime_log[m["afr_actual"]], errors="coerce")
        diff = actual - target
        if diff.max() > self.config.max_afr_delta:
            self._flags.add("dangerous_lean")
            self._alerts.append(Alert(
                flag="dangerous_lean",
                severity=AlertSeverity.CRITICAL,
                message=f"🚨 Dangerous Lean: AFR {round(diff.max(), 1)} points above target.",
                beginner_message=(
                    "The engine ran critically lean — way too little fuel for the air it consumed. "
                    "This can melt pistons. Do not pull again. Inspect injectors, pumps, and O2 sensor."
                ),
            ))

    def _check_load(self) -> None:
        m = self.map
        if m["load_target"] not in self.cols or m["load_actual"] not in self.cols:
            return
        target = pd.to_numeric(self.prime_log[m["load_target"]], errors="coerce")
        actual = pd.to_numeric(self.prime_log[m["load_actual"]], errors="coerce")
        if (target - actual).max() > self.config.max_load_miss_pct:
            self._flags.add("load_miss")
            self._insights.append(Alert(
                flag="load_miss",
                severity=AlertSeverity.MINOR,
                message="📉 Load Miss: Engine missed load target by >15%. Power is reduced.",
                beginner_message=(
                    "The engine didn't reach the power level it was aiming for — "
                    "something is preventing it from filling the cylinders fully."
                ),
            ))

    def _check_timing_advance(self) -> None:
        adv_col = self.map["timing_adv"]
        if adv_col not in self.cols:
            return
        adv = pd.to_numeric(self.prime_log[adv_col], errors="coerce")
        if adv.iloc[-1] < self.config.min_peak_timing_adv:
            self._flags.add("conservative_timing")
            self._insights.append(Alert(
                flag="conservative_timing",
                severity=AlertSeverity.INFO,
                message=f"🐢 Conservative Timing: Peak advance {round(adv.iloc[-1], 1)}°. Map may be octane limited.",
                beginner_message=(
                    "The tune is running less ignition advance than a healthy map would — "
                    "likely because the fuel quality or heat isn't allowing more timing."
                ),
            ))

    def _calculate_performance_metrics(self) -> None:
        m = self.map
        time_col = m["time"]
        if time_col not in self.prime_log.columns:
            return
        time_s = pd.to_numeric(self.prime_log[time_col], errors="coerce")
        rpm = pd.to_numeric(self.prime_log[m["rpm"]], errors="coerce")
        duration = time_s.iloc[-1] - time_s.iloc[0]
        if pd.notna(duration) and duration > 0:
            accel = int((rpm.iloc[-1] - rpm.iloc[0]) / duration)
            self._insights.append(Alert(
                flag="accel_rate",
                severity=AlertSeverity.INFO,
                message=f"📈 Acceleration Rate: {accel} RPM/sec.",
            ))

    # ──────────────────────────────────────────────────────────────────────────
    # Pillar B — rate-of-change / single-pull delta detection
    # ──────────────────────────────────────────────────────────────────────────

    def _check_rail_drop_rate_b(self) -> None:
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

        dt = time_ps.diff()
        d_rail = rail_ps.diff()
        with np.errstate(divide="ignore", invalid="ignore"):
            rate = d_rail / dt.replace(0, np.nan)

        min_rate = rate.min()
        if pd.notna(min_rate) and min_rate < -self.config.rail_drop_rate_psi_per_s:
            self._flags.add("rail_drop_rate_b")
            self._alerts.append(Alert(
                flag="rail_drop_rate_b",
                severity=AlertSeverity.MAJOR,
                message=(
                    f"📉 Rail Pressure Drop Rate (Pillar B): Fuel rail fell at "
                    f"{abs(round(min_rate))} PSI/s during WOT — "
                    f"exceeds safe rate ({self.config.rail_drop_rate_psi_per_s} PSI/s)."
                ),
                beginner_message=(
                    "Fuel pressure fell off sharply during the pull — "
                    "the pump couldn't keep up with what the engine needed right when boost hit."
                ),
            ))

    def _check_timing_retard_events_b(self) -> None:
        """Pillar B: sudden timing retard mid-pull — knock proxy even without a knock flag."""
        adv_col = self.map["timing_adv"]
        if adv_col not in self.cols:
            return
        adv = pd.to_numeric(self.prime_log[adv_col], errors="coerce").dropna()
        if len(adv) < 4:
            return

        # Max retard over any 3-sample rolling window
        rolling_drop = adv.rolling(3).apply(lambda w: w.iloc[0] - w.iloc[-1], raw=False)
        max_retard = rolling_drop.max()

        if pd.notna(max_retard) and max_retard > self.config.timing_retard_event_deg:
            self._flags.add("timing_retard_event_b")
            self._alerts.append(Alert(
                flag="timing_retard_event_b",
                severity=AlertSeverity.MINOR,
                message=(
                    f"⚡ Timing Retard Event (Pillar B): {round(max_retard, 1)}° sudden retard "
                    f"detected mid-pull. ECU responded to borderline knock."
                ),
                beginner_message=(
                    "The computer suddenly yanked timing back during the pull — "
                    "it sensed the engine was about to knock and backed off to protect it."
                ),
            ))

    def _check_afr_lean_swing_b(self) -> None:
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
            self._flags.add("afr_lean_swing_b")
            self._alerts.append(Alert(
                flag="afr_lean_swing_b",
                severity=AlertSeverity.MAJOR,
                message=(
                    f"⚡ AFR Lean Swing (Pillar B): AFR ran {round(max_lean, 2)} points lean "
                    f"of target in the middle of the pull — fueling fell behind demand."
                ),
                beginner_message=(
                    "The engine went lean in the middle of the pull — not at the start, not the end, "
                    "but right in the meat of it where the engine was working hardest."
                ),
            ))

    def _check_boost_mid_pull_drop_b(self) -> None:
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
            self._flags.add("boost_mid_pull_drop_b")
            self._alerts.append(Alert(
                flag="boost_mid_pull_drop_b",
                severity=AlertSeverity.MINOR,
                message=(
                    f"💨 Boost Mid-Pull Drop (Pillar B): Boost fell {round(drop, 1)} PSI from peak "
                    f"({round(peak_val, 1)} → {round(min_after, 1)} PSI) post-spool."
                ),
                beginner_message=(
                    "Boost built up fine then dropped off mid-pull — "
                    "possible boost leak that only opens under pressure, wastegate creep, or compressor surge."
                ),
            ))

    # ──────────────────────────────────────────────────────────────────────────
    # Multi-pull comparison (includes Pillar B inter-pull checks)
    # ──────────────────────────────────────────────────────────────────────────

    def _compare_pulls(self) -> Optional[List[PullSummary]]:
        if len(self.prime_extracted_data) < 2:
            return None

        time_col = self.map["time"]
        self._sorted_pulls = sorted(
            self.prime_extracted_data,
            key=lambda p: pd.to_numeric(p[time_col], errors="coerce").iloc[0],
        )

        summaries: List[PullSummary] = []
        for i, pull in enumerate(self._sorted_pulls):
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
                self._flags.add("timing_degradation_heat_soak")
                pull_values = ", ".join(f"{v:.1f}°" for v in timing_seq)
                self._alerts.append(Alert(
                    flag="timing_degradation_heat_soak",
                    severity=AlertSeverity.MAJOR,
                    message=(
                        f"🌡️ Heat Soak Progression: Timing pulled further each run "
                        f"({pull_values}). Classic heat soak signature."
                    ),
                    beginner_message=(
                        "Each successive pull got more timing pulled — the engine got progressively "
                        "hotter and the computer backed off more each time to protect it."
                    ),
                ))

        # Pillar B: inter-pull checks
        self._check_iat_inter_pull_jump_b()
        self._check_boost_progression_b()

        return summaries

    def _check_iat_inter_pull_jump_b(self) -> None:
        """Pillar B: large IAT jump between pull-start readings — intercooler not recovering."""
        if len(self._sorted_pulls) < 2:
            return
        iat_col = self.map["iat"]
        if iat_col not in self.cols:
            return

        jumps = []
        for i in range(1, len(self._sorted_pulls)):
            prev_end = pd.to_numeric(self._sorted_pulls[i - 1][iat_col], errors="coerce").iloc[-1]
            curr_start = pd.to_numeric(self._sorted_pulls[i][iat_col], errors="coerce").iloc[0]
            if pd.notna(prev_end) and pd.notna(curr_start):
                jumps.append(curr_start - prev_end)

        if not jumps:
            return

        max_jump = max(jumps)
        if max_jump > self.config.iat_inter_pull_jump_f:
            self._flags.add("iat_inter_pull_jump_b")
            self._insights.append(Alert(
                flag="iat_inter_pull_jump_b",
                severity=AlertSeverity.MINOR,
                message=(
                    f"🌡️ IAT Inter-Pull Jump (Pillar B): Intake temps jumped {round(max_jump)}°F "
                    f"between pulls — intercooler is not recovering between runs."
                ),
                beginner_message=(
                    "Between back-to-back pulls, intake temps jumped significantly — "
                    "the intercooler isn't cooling down fast enough between runs."
                ),
            ))

    def _check_boost_progression_b(self) -> None:
        """Pillar B: peak boost declining monotonically across pulls — turbo or boost control issue."""
        if len(self._sorted_pulls) < 2:
            return

        peak_boosts = [
            float(pd.to_numeric(p[self.map["boost_actual"]], errors="coerce").max())
            for p in self._sorted_pulls
        ]

        # Require each pull to drop by more than 1 PSI to avoid noise
        is_declining = all(
            peak_boosts[i] < peak_boosts[i - 1] - 1.0 for i in range(1, len(peak_boosts))
        )
        if is_declining:
            self._flags.add("boost_degradation_b")
            vals = ", ".join(f"{b:.1f}" for b in peak_boosts)
            self._insights.append(Alert(
                flag="boost_degradation_b",
                severity=AlertSeverity.MINOR,
                message=(
                    f"📉 Boost Degradation (Pillar B): Peak boost declining each pull "
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

    def _correlate_iat_timing_c(self) -> None:
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

        iat_rise = float(iat[common].iloc[-1] - iat[common].iloc[0])
        adv_change = float(adv[common].iloc[-1] - adv[common].iloc[0])  # negative = retard

        if iat_rise > self.config.iat_timing_rise_min_f and adv_change < -self.config.iat_timing_retard_min_deg:
            self._flags.add("iat_timing_correlation_c")
            self._insights.append(Alert(
                flag="iat_timing_correlation_c",
                severity=AlertSeverity.INFO,
                message=(
                    f"🔗 IAT→Timing Correlation (Pillar C): IAT rose {round(iat_rise, 1)}°F and "
                    f"timing retarded {abs(round(adv_change, 1))}° in the same pull — "
                    f"heat-soak feedback loop confirmed."
                ),
                beginner_message=(
                    "The data shows a direct cause-and-effect: air got hotter, computer pulled timing. "
                    "This is heat soak — not a fuel or knock problem."
                ),
            ))

    def _correlate_boost_rail_c(self) -> None:
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

        corr = boost_ps.corr(rail_ps)
        rail_drop = float(rail_ps.max() - rail_ps.min())
        boost_rise = float(boost_ps.max() - boost_ps.min())

        if (
            pd.notna(corr)
            and corr < self.config.boost_rail_corr_threshold
            and rail_drop > self.config.boost_rail_min_drop_psi
            and boost_rise > 2.0
        ):
            self._flags.add("boost_rail_divergence_c")
            self._alerts.append(Alert(
                flag="boost_rail_divergence_c",
                severity=AlertSeverity.MAJOR,
                message=(
                    f"🔗 Boost↔Rail Divergence (Pillar C): As boost climbed, rail pressure fell "
                    f"{round(rail_drop)} PSI post-spool (r={round(corr, 2)}). "
                    f"HPFP is struggling to meet fuel demand under boost."
                ),
                beginner_message=(
                    "As the turbo pushed harder, fuel pressure dropped — these two should track "
                    "together, but they went in opposite directions. The fuel pump is falling behind."
                ),
            ))

    def _correlate_throttle_afr_c(self) -> None:
        """
        Pillar C: at WOT throttle the AFR must be in the rich band.
        Flags when a significant fraction of full-throttle samples are lean.
        """
        m = self.map
        if m["afr_actual"] not in self.cols or m["throttle"] not in self.cols:
            return

        throttle = pd.to_numeric(self.prime_log[m["throttle"]], errors="coerce")
        actual = pd.to_numeric(self.prime_log[m["afr_actual"]], errors="coerce")

        high_throttle_mask = throttle > 95
        if not high_throttle_mask.any():
            return

        afr_wot = actual[high_throttle_mask]
        lean_wot = afr_wot[afr_wot > self.config.wot_lean_afr]

        if len(lean_wot) > len(afr_wot) * self.config.throttle_afr_lean_fraction:
            lean_pct = round(100 * len(lean_wot) / len(afr_wot))
            self._flags.add("throttle_afr_lean_c")
            self._alerts.append(Alert(
                flag="throttle_afr_lean_c",
                severity=AlertSeverity.MAJOR,
                message=(
                    f"🔗 WOT Lean (Pillar C): {lean_pct}% of full-throttle samples show "
                    f"AFR > {self.config.wot_lean_afr} (peak: {round(float(afr_wot.max()), 2)}). "
                    f"Fueling is not matching throttle demand."
                ),
                beginner_message=(
                    "Even with the pedal floored, the engine isn't getting enough fuel — "
                    "multiple sensor readings confirm it's lean at full throttle."
                ),
            ))

    # ──────────────────────────────────────────────────────────────────────────
    # Synthesis — cross-pillar correlation engine
    # ──────────────────────────────────────────────────────────────────────────

    def _synthesize_diagnosis(self) -> None:
        """
        Cross-reference flags from all three pillars to identify root causes.
        Single-pillar findings are reported with appropriate confidence.
        Multi-pillar corroboration raises confidence and sharpens the diagnosis.
        Diagnosis strings contain both technical detail and plain-English explanation.
        """
        f = self._flags

        # ── Fuel pressure / HPFP ─────────────────────────────────────────────
        if "hpfp_crash" in f and "lpfp_starvation" in f:
            n = sum(["rail_deviation_a" in f, "rail_drop_rate_b" in f, "boost_rail_divergence_c" in f])
            conf = _confidence_label(n)
            self._diagnosis.append(
                f"🛠️ **Cascading Fuel Failure{conf}:** HPFP is crashing because the in-tank LPFP "
                f"is failing to supply it. Fix or upgrade the LPFP first — replacing the HPFP alone "
                f"will not resolve this.\n\n"
                f"💬 **Plain English:** Think of it as two pumps in series — the in-tank pump feeds "
                f"the high-pressure pump. The in-tank pump is choking, so everything downstream starves. "
                f"Start with the LPFP."
            )

        elif "hpfp_crash" in f:
            n = sum(["rail_deviation_a" in f, "rail_drop_rate_b" in f, "boost_rail_divergence_c" in f])
            conf = _confidence_label(n)
            self._diagnosis.append(
                f"🛠️ **HPFP Limit Reached{conf}:** HPFP crashed but LPFP is healthy — "
                f"you have exceeded the stock HPFP's physical capacity for this fuel blend. "
                f"Consider a TU/Dorch upgrade or reduce E85 content.\n\n"
                f"💬 **Plain English:** Your high-pressure fuel pump hit its ceiling. "
                f"The in-tank pump is fine — the fix is either a pump upgrade or less ethanol in the tank."
            )

        elif "rail_drop_rate_b" in f and "boost_rail_divergence_c" in f:
            # Dynamic detection caught a struggling HPFP before it hit the absolute floor
            self._diagnosis.append(
                "⚠️ **HPFP Under Load — Early Warning (Pillars B+C):** Rail pressure drops at an "
                "excessive rate during WOT, and boost demand and rail pressure are moving in opposite "
                "directions. The pump hasn't hard-crashed yet, but the pattern indicates it is "
                "struggling. Monitor across logs; an upgrade may be warranted.\n\n"
                "💬 **Plain English:** Your fuel pump is working harder than it should and starting "
                "to fall behind when boost builds. It hasn't failed yet — but it's telling you it will."
            )

        elif "rail_deviation_a" in f and "rail_drop_rate_b" in f:
            self._diagnosis.append(
                "⚠️ **HPFP Trending Low (Pillars A+B):** Rail pressure is below this car's own "
                "non-WOT baseline AND drops at an elevated rate during the pull. "
                "Watch for progression across logs.\n\n"
                "💬 **Plain English:** Fuel pressure is lower than normal for this car and falls "
                "quickly when the engine works hard. Worth watching before it becomes a bigger problem."
            )

        # ── AFR / fueling ────────────────────────────────────────────────────
        if "dangerous_lean" in f:
            if "afr_lean_swing_b" in f or "throttle_afr_lean_c" in f:
                self._diagnosis.append(
                    "🚨 **CRITICAL SAFETY — Do Not Drive (High Confidence):** A dangerous lean "
                    "condition is confirmed by multiple detection methods. The engine ran critically "
                    "short on fuel at WOT. Do not do another pull. "
                    "Inspect injectors, fuel pumps, and primary O2 sensor immediately.\n\n"
                    "💬 **Plain English:** Multiple sensor readings agree — not enough fuel reached "
                    "the engine during hard acceleration. This can melt pistons. Stop pulling now."
                )
            else:
                self._diagnosis.append(
                    "🚨 **CRITICAL SAFETY — Do Not Drive:** The engine ran dangerously lean at WOT. "
                    "Do not do another pull. Inspect injectors, fuel pumps, and primary O2 sensor.\n\n"
                    "💬 **Plain English:** Way too little fuel for the air the engine consumed. "
                    "Find the cause before driving hard again."
                )

        elif "afr_lean_swing_b" in f and "throttle_afr_lean_c" in f:
            # Caught by dynamic detection before hitting the absolute dangerous threshold
            self._diagnosis.append(
                "⚠️ **Marginal Fueling — High Attention (Pillars B+C):** AFR swings lean mid-pull "
                "and lean samples cluster at full throttle — the fueling system is at its limit "
                "under peak demand. Not yet at the dangerous threshold, but this is the pattern "
                "that precedes a dangerous lean event.\n\n"
                "💬 **Plain English:** The car is running lean when you push it hardest, confirmed "
                "from two angles. It hasn't hit the danger zone yet — but it's close. "
                "Sort the fueling before it gets worse."
            )

        # ── Boost / turbo ────────────────────────────────────────────────────
        if "boost_leak" in f and "wgdc_saturation" in f:
            self._diagnosis.append(
                "🛠️ **Overworked Turbo:** A boost leak is forcing the wastegate fully closed to "
                "compensate. This saturates the turbo and superheats intake air. "
                "Check charge pipes and inlet couplers.\n\n"
                "💬 **Plain English:** Boost is escaping somewhere so the turbo is working at 100% "
                "just to compensate. Find and seal the leak — the turbo can't work any harder."
            )

        elif "boost_mid_pull_drop_b" in f and "boost_leak" not in f:
            self._diagnosis.append(
                "⚠️ **Mid-Pull Boost Anomaly (Pillar B):** Boost builds correctly then drops "
                "unexpectedly after hitting peak — possible intermittent boost leak (only opens "
                "under full pressure), wastegate creep, or compressor surge. "
                "Check if this repeats across logs.\n\n"
                "💬 **Plain English:** Boost was building fine then fell off mid-pull for no "
                "obvious reason. Could be a coupler that only leaks under full pressure, "
                "a wastegate that doesn't hold, or the turbo hitting its surge point."
            )

        # ── Knock / timing ───────────────────────────────────────────────────
        if ("knock" in f or "timing_pull" in f) and "iat_heat_soak" not in f:
            if "timing_retard_event_b" in f:
                self._diagnosis.append(
                    "🛠️ **Knock — Octane Limit (High Confidence, Pillars A+B):** "
                    "Cylinder timing corrections, an ECU knock flag, and a mid-pull retard "
                    "event all independently point to the same cause: the fuel is not adequate "
                    "for this tune's timing targets. "
                    "Add 1–2 gallons of E85 or flash to a lower-octane map.\n\n"
                    "💬 **Plain English:** Three separate checks all say the same thing — the fuel "
                    "and the tune aren't matched. Better fuel or a safer map is the fix."
                )
            else:
                self._diagnosis.append(
                    "🛠️ **Octane Limit:** Timing corrections present without significant heat soak — "
                    "points to fuel quality. Try adding 1–2 gallons of E85 or flash a lower-octane map.\n\n"
                    "💬 **Plain English:** The car pulled timing because of fuel quality, not heat. "
                    "Better fuel or a safer tune map will fix this."
                )

        # ── Heat soak ────────────────────────────────────────────────────────
        if "timing_degradation_heat_soak" in f:
            if "iat_timing_correlation_c" in f or "iat_inter_pull_jump_b" in f:
                self._diagnosis.append(
                    "🌡️ **Inter-Run Heat Soak (Confirmed, Multiple Pillars):** Progressive timing "
                    "degradation across pulls is corroborated by IAT→timing correlation and/or "
                    "inter-pull IAT jumps. Allow 5–10 minutes of cooling between runs. "
                    "If it persists with adequate cooling, investigate intercooler and charge pipe.\n\n"
                    "💬 **Plain English:** Each pull gets hotter and the computer pulls more timing "
                    "each time — confirmed by multiple data sources. Cool-down time between pulls "
                    "is the immediate fix. If that's not enough, consider an intercooler upgrade."
                )
            else:
                self._diagnosis.append(
                    "🌡️ **Inter-Run Heat Soak:** Timing pulled further on each successive run. "
                    "Allow 5–10 minutes of cooling between pulls. "
                    "If persistent, consider upgraded charge pipe or intercooler.\n\n"
                    "💬 **Plain English:** The car gets more heat-soaked with each run. "
                    "More cool-down time between pulls is the fix."
                )

        elif "iat_timing_correlation_c" in f and "iat_heat_soak" in f:
            self._diagnosis.append(
                "🌡️ **Single-Pull Heat Soak (Confirmed, Pillar C):** IAT rose and timing retarded "
                "in direct correlation within this pull — heat-soak feedback loop is active. "
                "The car arrived heat-soaked or the intercooler is undersized for this power level.\n\n"
                "💬 **Plain English:** The air got hotter during the pull and the computer backed "
                "off timing directly in response — both happened in lockstep in the same run."
            )

        # ── TCU ──────────────────────────────────────────────────────────────
        if "throttle_closure" in f and "torque_limiter" in f:
            self._diagnosis.append(
                "⚙️ **TCU Intervention:** Engine torque is exceeding the transmission's "
                "programmed limit, causing throttle closures. "
                "An xHP transmission tune may be needed to raise the torque cap.\n\n"
                "💬 **Plain English:** The gearbox is telling the engine to slow down — "
                "it's getting more torque than it's calibrated to handle. "
                "An xHP tune can raise that limit."
            )

        # ── Stand-alone insights ──────────────────────────────────────────────
        if "iat_inter_pull_jump_b" in f and "timing_degradation_heat_soak" not in f:
            self._diagnosis.append(
                "⚠️ **Intercooler Recovery (Pillar B):** IAT jumps significantly between pulls "
                "but timing hasn't walked back yet — the intercooler is under-recovering. "
                "This is the precursor to heat-soak timing degradation.\n\n"
                "💬 **Plain English:** Intake temps spike between runs even though timing is still "
                "holding. The intercooler isn't cooling down fast enough between pulls."
            )

        if "boost_degradation_b" in f and "boost_leak" not in f and "wgdc_saturation" not in f:
            self._diagnosis.append(
                "⚠️ **Boost Falling Across Session (Pillar B):** Peak boost declining across "
                "pulls without a confirmed boost leak. Possible causes: turbo heat soak, "
                "wastegate actuator drift, or boost solenoid degradation.\n\n"
                "💬 **Plain English:** Each run makes a little less boost for no obvious reason. "
                "Could be the turbo getting too hot, or something loosening in the boost control system."
            )

        # ── Clean bill of health ──────────────────────────────────────────────
        if not self._diagnosis and not self._alerts:
            self._diagnosis.append(
                "✅ **Clean Bill of Health:** Hardware is happy, fuel pressure is stable, "
                "and timing is clean. The car is running exactly as your tuner intended.\n\n"
                "💬 **Plain English:** Nothing wrong — no issues found across any check."
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Scoring
    # ──────────────────────────────────────────────────────────────────────────

    def _calculate_score(self) -> int:
        if any(a.severity == AlertSeverity.CRITICAL for a in self._alerts):
            return 0
        deductions = sum(
            SEVERITY_DEDUCTIONS.get(a.severity, 0) or 0
            for a in self._alerts
        )
        return max(10, 100 - deductions)
