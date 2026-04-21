from typing import Optional, List, Tuple
import pandas as pd
import numpy as np

from .models import Alert, AlertSeverity, DiagnosticReport, PullSummary, SEVERITY_DEDUCTIONS
from .thresholds import ThresholdConfig


class B58DiagnosticEngine:
    """
    Analyzes BMW B58 ECU CSV logs from MHD or BM3 tuning platforms.

    Workflow:
      1. __init__  — detect platform, normalize column names, extract WOT segments
      2. run_analysis() — run all diagnostic checks, synthesize root causes,
                          return a DiagnosticReport

    Pass a ThresholdConfig to override any numeric threshold without touching
    engine logic — useful for Stage 2 / E-blend / custom hardware setups.
    """

    def __init__(self, df: pd.DataFrame, config: Optional[ThresholdConfig] = None):
        self.df = df.copy()
        self.cols = self.df.columns.tolist()
        self.config = config or ThresholdConfig()
        self.tune_platform = self._identify_tune_platform()
        self.map = self._normalize_col_names()
        self.engine_timing_cols = self._build_timing_cols()
        self.prime_log, self.prime_extracted_data = self._extract_wot_segments()

    # ──────────────────────────────────────────────────────────────────────────
    # Init helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _identify_tune_platform(self) -> str:
        if "Accel Ped. Pos. (%)" in self.cols or "Cyl1 Timing Cor (*)" in self.cols:
            return "MHD"
        if "Accel. Pedal[%]" in self.cols or "Engine speed[1/min]" in self.cols:
            return "BM3"
        raise ValueError("Unsupported platform. Please upload an MHD or BM3 CSV log.")

    def _normalize_col_names(self) -> dict:
        """Map semantic names to platform-specific column names."""
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
            "afr_actual":   "AFR",  # BM3 sometimes exports as "Bank 1 AFR" — see notes
            "load_target":  "Load Target[%]",
            "load_actual":  "Load Actual[%]",
            "timing_adv":   "(RAM) Ignition Timing Cyl. 1[°]",
        }

    def _build_timing_cols(self) -> List[str]:
        """Return per-cylinder timing correction columns that are present in this log."""
        if self.tune_platform == "MHD":
            candidates = [f"Cyl{i} Timing Cor (*)" for i in range(1, 7)]
        else:
            candidates = [f"(RAM) Ignition Timing Corr. Cyl. {i}[°]" for i in range(1, 7)]
        return [c for c in candidates if c in self.cols]

    def _extract_wot_segments(self) -> Tuple[pd.DataFrame, List[pd.DataFrame]]:
        """
        Split the log into continuous WOT segments (pedal > threshold).

        Returns (longest_segment, all_segments). Returns (empty DataFrame, [])
        when no WOT segment qualifies.
        """
        pedal_col = self.map["pedal"]
        if pedal_col not in self.cols:
            return pd.DataFrame(), []

        self.df[pedal_col] = pd.to_numeric(self.df[pedal_col], errors="coerce")
        wot_rows = self.df[self.df[pedal_col] > self.config.wot_pedal_threshold]

        if wot_rows.empty:
            return pd.DataFrame(), []

        # Group consecutive rows: a gap > 1 in index = new segment
        gap_labels = (wot_rows.index.to_series().diff() > 1).cumsum()
        all_segments = [group.copy() for _, group in wot_rows.groupby(gap_labels)]
        prime = max(all_segments, key=len)
        return prime, all_segments

    # ──────────────────────────────────────────────────────────────────────────
    # Main entry point
    # ──────────────────────────────────────────────────────────────────────────

    def run_analysis(self) -> Optional[DiagnosticReport]:
        """
        Run all diagnostic checks and return a DiagnosticReport.
        Returns None when no WOT segment was found in the log.

        Internal state (_flags, _alerts, _insights, _diagnosis) is reset on
        each call so the engine instance can be reused safely.
        """
        if self.prime_log.empty:
            return None

        self._flags: set = set()
        self._alerts: List[Alert] = []
        self._insights: List[Alert] = []
        self._diagnosis: List[str] = []

        # Hardware & safety checks
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

        # Tuning quality checks
        self._check_afr()
        self._check_load()
        self._check_timing_advance()
        self._calculate_performance_metrics()

        # Multi-pull comparison (may add more flags before synthesis)
        pull_comparison = self._compare_pulls()

        # Synthesis reads flags, never strings
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
    # Hardware & safety checks
    # ──────────────────────────────────────────────────────────────────────────

    def _check_boost_with_spool_awareness(self) -> None:
        m = self.map
        target = pd.to_numeric(self.prime_log[m["boost_target"]], errors="coerce")
        actual = pd.to_numeric(self.prime_log[m["boost_actual"]], errors="coerce")
        delta = target - actual  # positive = under target, negative = over target

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
            ))

        if post_delta.min() < -self.config.boost_delta_psi:
            self._flags.add("overboost")
            self._alerts.append(Alert(
                flag="overboost",
                severity=AlertSeverity.MAJOR,
                message=f"⚠️ Overboost: {abs(round(post_delta.min(), 1))} PSI over target detected.",
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
            ))

    def _check_fuel_pressure(self) -> None:
        rail = pd.to_numeric(self.prime_log[self.map["rail"]], errors="coerce")
        if rail.min() < self.config.min_rail_psi:
            self._flags.add("hpfp_crash")
            self._alerts.append(Alert(
                flag="hpfp_crash",
                severity=AlertSeverity.MAJOR,
                message=f"🔴 HPFP Crash: Fuel pressure dipped to {int(rail.min())} PSI.",
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
            ))

    def _check_throttle_closures(self) -> None:
        throttle = pd.to_numeric(self.prime_log[self.map["throttle"]], errors="coerce")
        if throttle.min() < self.config.min_throttle_pct:
            self._flags.add("throttle_closure")
            self._insights.append(Alert(
                flag="throttle_closure",
                severity=AlertSeverity.MINOR,
                message=f"🟡 Throttle Closure: ECU limited throttle to {int(throttle.min())}%.",
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
            ))
        elif delta > self.config.max_iat_delta_warn:
            self._flags.add("iat_rising")
            self._insights.append(Alert(
                flag="iat_rising",
                severity=AlertSeverity.INFO,
                message=f"🟡 IAT Rise: Intake temps rose {int(delta)}°F.",
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
            ))

    def _check_timing_advance(self) -> None:
        adv_col = self.map["timing_adv"]
        if adv_col not in self.cols:
            return
        adv = pd.to_numeric(self.prime_log[adv_col], errors="coerce")
        # Uses end-of-pull value as a proxy for peak advance at redline
        if adv.iloc[-1] < self.config.min_peak_timing_adv:
            self._flags.add("conservative_timing")
            self._insights.append(Alert(
                flag="conservative_timing",
                severity=AlertSeverity.INFO,
                message=f"🐢 Conservative Timing: Peak advance {round(adv.iloc[-1], 1)}°. Map may be octane limited.",
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
    # Multi-pull comparison
    # ──────────────────────────────────────────────────────────────────────────

    def _compare_pulls(self) -> Optional[List[PullSummary]]:
        """
        Compute per-pull summaries and flag progressive timing degradation.

        Pull-to-pull worsening timing corrections are the most actionable signal
        that a user can relay to their tuner — they indicate heat soak building
        across runs, which a single-pull analysis cannot detect.

        Returns None when fewer than two WOT segments were found.
        """
        if len(self.prime_extracted_data) < 2:
            return None

        time_col = self.map["time"]
        sorted_pulls = sorted(
            self.prime_extracted_data,
            key=lambda p: pd.to_numeric(p[time_col], errors="coerce").iloc[0],
        )

        summaries: List[PullSummary] = []
        for i, pull in enumerate(sorted_pulls):
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

        # Progressive degradation = each successive pull's mean timing is strictly worse
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
                        f"🌡️ Heat Soak Progression: Timing pulled further on each run "
                        f"({pull_values}). Classic heat soak signature."
                    ),
                ))

        return summaries

    # ──────────────────────────────────────────────────────────────────────────
    # Synthesis engine — reads flags, never strings
    # ──────────────────────────────────────────────────────────────────────────

    def _synthesize_diagnosis(self) -> None:
        """
        Cross-reference flags to identify root causes rather than just listing symptoms.

        Every branch reads structured flags set by the check methods. Human-readable
        alert wording is intentionally irrelevant here — changing an emoji or rephrasing
        a message will never break synthesis logic.
        """
        f = self._flags  # local alias for readability

        # Cascading fuel failure: LPFP starvation is the root cause of the HPFP crash
        if "hpfp_crash" in f and "lpfp_starvation" in f:
            self._diagnosis.append(
                "🛠️ **Cascading Fuel Failure:** Your HPFP is crashing because the in-tank LPFP is "
                "failing to supply it. Fix or upgrade the LPFP first — replacing the HPFP alone will not fix this."
            )
        elif "hpfp_crash" in f:
            self._diagnosis.append(
                "🛠️ **HPFP Limit Reached:** HPFP crashed but LPFP is healthy. You have exceeded the "
                "stock HPFP's physical capacity for this fuel blend. Consider a TU/Dorch upgrade or reduce E85 content."
            )

        # Dangerous lean — stop driving
        if "dangerous_lean" in f:
            self._diagnosis.append(
                "🚨 **CRITICAL SAFETY — Do Not Drive:** The engine ran dangerously lean at WOT. "
                "Do not do another pull. Inspect injectors, fuel pumps, and primary O2 sensor immediately."
            )

        # Boost leak overworking the turbo
        if "boost_leak" in f and "wgdc_saturation" in f:
            self._diagnosis.append(
                "🛠️ **Overworked Turbo:** A boost leak is forcing the wastegate fully open to compensate. "
                "This saturates the turbo and superheats intake air. Check charge pipes and inlet connections."
            )

        # Timing pull without heat soak = octane / fuel quality issue
        if ("knock" in f or "timing_pull" in f) and "iat_heat_soak" not in f:
            self._diagnosis.append(
                "🛠️ **Octane Limit:** Timing corrections are present without significant heat soak, "
                "pointing to fuel quality rather than temperature. Try adding 1-2 gallons of E85 or "
                "flashing a lower-octane map."
            )

        # Transmission torque limiting
        if "throttle_closure" in f and "torque_limiter" in f:
            self._diagnosis.append(
                "⚙️ **TCU Intervention:** Engine torque is exceeding the transmission's programmed limit, "
                "causing throttle closures. A transmission tune (xHP) may be needed to raise the torque cap."
            )

        # Progressive heat soak across multiple pulls
        if "timing_degradation_heat_soak" in f:
            self._diagnosis.append(
                "🌡️ **Inter-Run Heat Soak:** Timing is progressively pulled on each successive run. "
                "Allow 5-10 minutes of engine cooling between pulls. "
                "If persistent, consider an upgraded charge pipe or intercooler."
            )

        # Explicit clean bill of health — this is the most important output for most users
        if not self._diagnosis and not self._alerts:
            self._diagnosis.append(
                "✅ **Clean Bill of Health:** Hardware is happy, fuel pressure is stable, and timing is clean. "
                "The car is running exactly as your tuner intended."
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Scoring
    # ──────────────────────────────────────────────────────────────────────────

    def _calculate_score(self) -> int:
        """
        Compute a 0–100 health score from the current alert list.

        Any CRITICAL alert returns 0 immediately — knock or dangerous lean
        makes the score meaningless. Otherwise, deduct by severity tier.
        Floor is 10: a car with non-critical issues still ran and produced a log.
        """
        if any(a.severity == AlertSeverity.CRITICAL for a in self._alerts):
            return 0
        deductions = sum(
            SEVERITY_DEDUCTIONS.get(a.severity, 0) or 0
            for a in self._alerts
        )
        return max(10, 100 - deductions)
