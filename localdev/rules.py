import pandas as pd
import numpy as np

class B58DiagnosticEngine:

    # Init df copy, cols, detect software platform of the log based on col names, and map the columns based on the platform.
    def __init__(self, df):
        self.df = df.copy() 
        self.cols = self.df.columns.tolist() 
        self.tune_platform = self._identify_tune_platform() 
        self.map = self._normalize_col_names()
        self.report = {"score": 100, "status": "Healthy", "alerts": [], "performance_insights": [], "diagnosis": []}
        
        # Group the timing cols based on the platform. 
        if self.tune_platform == "MHD":
            self.engine_timing_cols = [f"Cyl{i} Timing Cor (*)" for i in range(1, 7) if f"Cyl{i} Timing Cor (*)" in self.cols]
        else:
            self.engine_timing_cols = [f"(RAM) Ignition Timing Corr. Cyl. {i}[°]" for i in range(1, 7) if f"(RAM) Ignition Timing Corr. Cyl. {i}[°]" in self.cols]

        # Isolate the longest continuous pull, this gives us the best input possible.
        pedal_position_col = self.map['pedal']
        if pedal_position_col in self.cols:
            self.df[pedal_position_col] = pd.to_numeric(self.df[pedal_position_col], errors='coerce')
            throttle_position_filter = self.df[self.df[pedal_position_col] > 85]
            
            if not throttle_position_filter.empty:
                extract_gt_85_throttle_data = (throttle_position_filter.index.to_series().diff() > 1).cumsum()
                self.prime_extracted_data = [group for _, group in throttle_position_filter.groupby(extract_gt_85_throttle_data)]
                self.prime_log = max(self.prime_extracted_data, key=len).copy()
            else:
                self.prime_log = pd.DataFrame()
        else:
            self.prime_log = pd.DataFrame()

    
    def _identify_tune_platform(self):
        # Detect platform based on platform exclusive col names.
        if "Accel Ped. Pos. (%)" in self.cols or "Cyl1 Timing Cor (*)" in self.cols:
            return "MHD"
        elif "Accel. Pedal[%]" in self.cols or "Engine speed[1/min]" in self.cols:
            return "BM3"
        raise ValueError("Platform not currently supported. Please upload an MHD or BM3 CSV.")
    
    def _normalize_col_names(self):
        # Normalize unique col names from either platform. 
        if self.tune_platform == "MHD":
            return {
                'pedal': 'Accel Ped. Pos. (%)',
                'rpm': 'RPM (rpm)',
                'boost_target': 'Boost target (PSI)',
                'boost_actual': 'Boost (PSI)',
                'throttle': 'Throttle Position (*)',
                'rail': 'Rail pressure mean 1 (PSI)',
                'iat': 'IAT (*F)',
                'stft': 'STFT 1 (%)',
                'time': 'Time',
                'wgdc': 'WGDC (%)', 
                'knock': 'Knock Detect',
                'lpfp': 'Fuel low pressure sensor (PSI)',
                'tq_lim': 'Torque Lim. active',
                'afr_target': 'AFR Target',
                'afr_actual': 'AFR 1',
                'load_target': 'Load req. (%)',
                'load_actual': 'Load act. (%)',
                'timing_adv': 'Timing Cyl. 1 (*)'
            }
        else: 
            return {
                'pedal': 'Accel. Pedal[%]',
                'rpm': 'Engine speed[1/min]',
                'boost_target': 'Boost pressure (Target)[psig]',
                'boost_actual': 'Boost (Pre-Throttle)[psig]',
                'throttle': 'Throttle Angle[%]',
                'rail': 'HPFP Act.[psig]',
                'iat': 'IAT[F]',
                'stft': 'STFT 1[%]',
                'time': 'Time',
                'wgdc': 'WGDC[%]',
                'knock': 'Knock Detected',
                'lpfp': 'LPFP Act.[psig]',
                'tq_lim': 'Torque Limiter Active',
                'afr_target': 'AFR Target',
                'afr_actual': 'AFR', # BM3 sometimes logs Bank 1, but usually just AFR
                'load_target': 'Load Target[%]',
                'load_actual': 'Load Actual[%]',
                'timing_adv': '(RAM) Ignition Timing Cyl. 1[°]'
            }

    def run_analysis(self):
        if self.prime_log.empty: return None
        
        # Hardware & Safety Checks
        self._check_boost_with_spool_awareness()
        self._check_ignition_contextual()
        self._check_fuel_pressure()
        self._check_throttle_closures()
        self._check_fuel_trims()
        self._check_iat_delta()
        self._check_wgdc()
        self._check_knock()
        self._check_lpfp()
        self._check_torque_limiters()
        
        # Tuning Strategy Checks
        self._check_afr()
        self._check_load()
        self._check_timing_advance()
        self._calculate_performance_metrics()

        # The Synthesis Engine (Root Cause Analysis)
        self._synthesize_diagnosis()

        if self.report['alerts']:
            self.report['status'] = "Needs Attention"
            self.report['score'] = max(10, self.report['score'] - (len(self.report['alerts']) * 15))
        
        return self.report

# stopped here.
    def _check_boost_with_spool_awareness(self):
        m = self.map
        diffs = pd.to_numeric(self.prime_log[m['boost_target']]) - pd.to_numeric(self.prime_log[m['boost_actual']])
        post_spool = self.prime_log[self.prime_log[m['rpm']] > 3500]
        if not post_spool.empty:
            leak_diffs = diffs.loc[post_spool.index]
            if leak_diffs.max() > 3.0:
                self.report['alerts'].append(f"💨 Boost Leak: {round(leak_diffs.max(), 1)} PSI under target detected.")
            if leak_diffs.min() < -3.0:
                self.report['alerts'].append(f"⚠️ Overboost: {abs(round(leak_diffs.min(), 1))} PSI over target detected.")

    def _check_ignition_contextual(self):
        if not self.engine_timing_cols: return
        m = self.map
        pull_data = self.prime_log[self.engine_timing_cols].apply(pd.to_numeric, errors='coerce')
        if pull_data.min().min() < -3.5:
            worst_idx = pull_data.min(axis=1).idxmin()
            worst_val = pull_data.loc[worst_idx].min()
            worst_cyl = ''.join(filter(str.isdigit, pull_data.loc[worst_idx].idxmin()))
            self.report['alerts'].append(f"🔥 Timing Pull: {worst_val}° on Cyl {worst_cyl}.")

    def _check_fuel_pressure(self):
        m = self.map
        rail_data = pd.to_numeric(self.prime_log[m['rail']], errors='coerce')
        if rail_data.min() < 1900:
            self.report['alerts'].append(f"🔴 HPFP Crash: Fuel pressure dipped to {int(rail_data.min())} PSI.")

    def _check_lpfp(self):
        m = self.map
        if m['lpfp'] not in self.cols: return
        lpfp_data = pd.to_numeric(self.prime_log[m['lpfp']], errors='coerce')
        if lpfp_data.min() < 55:
            self.report['alerts'].append(f"📉 LPFP Starvation: Low pressure pump dropped to {int(lpfp_data.min())} PSI.")

    def _check_throttle_closures(self):
        m = self.map
        throttle = pd.to_numeric(self.prime_log[m['throttle']], errors='coerce')
        if throttle.min() < 93:
            self.report['performance_insights'].append(f"🟡 Throttle Closure: ECU limited throttle to {int(throttle.min())}%.")

    def _check_fuel_trims(self):
        m = self.map
        if m['stft'] not in self.cols: return
        stft = pd.to_numeric(self.prime_log[m['stft']], errors='coerce')
        if stft.max() > 25:
            self.report['alerts'].append(f"⛽ Fuel Trims: STFT maxed out at +{int(stft.max())}%.")

    def _check_iat_delta(self):
        m = self.map
        if m['iat'] not in self.cols: return
        iat = pd.to_numeric(self.prime_log[m['iat']], errors='coerce')
        if iat.empty: return
        delta = iat.iloc[-1] - iat.iloc[0]
        if delta > 20:
            self.report['alerts'].append(f"🌡️ IAT Heat Soak: Intake temps rose by {int(delta)}°F.")
        elif delta > 12:
            self.report['performance_insights'].append(f"🟡 IAT Rise: Intake temps rose by {int(delta)}°F.")

    def _check_wgdc(self):
        m = self.map
        if m['wgdc'] not in self.cols: return
        wgdc = pd.to_numeric(self.prime_log[m['wgdc']], errors='coerce')
        if wgdc.max() > 95:
            self.report['performance_insights'].append(f"🐌 Turbo Headroom: WGDC maxed out (>95%).")

    def _check_knock(self):
        m = self.map
        if m['knock'] not in self.cols: return
        knock = pd.to_numeric(self.prime_log[m['knock']], errors='coerce')
        if knock.max() > 0:
            self.report['alerts'].append(f"🚨 CRITICAL: Engine knock detected.")
            self.report['score'] = 0 

    def _check_torque_limiters(self):
        m = self.map
        if m['tq_lim'] not in self.cols: return
        tq = pd.to_numeric(self.prime_log[m['tq_lim']], errors='coerce')
        if tq.max() > 0:
            self.report['performance_insights'].append(f"⚙️ Torque Intervention: TCU/ECU limiter active.")

    def _check_afr(self):
        m = self.map
        if m['afr_target'] not in self.cols or m['afr_actual'] not in self.cols: return
        target = pd.to_numeric(self.prime_log[m['afr_target']], errors='coerce')
        actual = pd.to_numeric(self.prime_log[m['afr_actual']], errors='coerce')
        diff = actual - target
        if diff.max() > 0.8:
            self.report['alerts'].append(f"🚨 Dangerous Lean Condition: AFR spiked {round(diff.max(), 1)} points above target.")

    def _check_load(self):
        m = self.map
        if m['load_target'] not in self.cols or m['load_actual'] not in self.cols: return
        target = pd.to_numeric(self.prime_log[m['load_target']], errors='coerce')
        actual = pd.to_numeric(self.prime_log[m['load_actual']], errors='coerce')
        if (target - actual).max() > 15:
            self.report['performance_insights'].append(f"📉 Load Miss: Engine missed load target by >15%. Power is reduced.")

    def _check_timing_advance(self):
        m = self.map
        if m['timing_adv'] not in self.cols: return
        adv = pd.to_numeric(self.prime_log[m['timing_adv']], errors='coerce')
        if adv.iloc[-1] < 8:
            self.report['performance_insights'].append(f"🐢 Conservative Timing: Peak advance was only {round(adv.iloc[-1], 1)}°. Tune may be octane limited.")

    def _calculate_performance_metrics(self):
        m = self.map
        duration = self.prime_log[m['time']].iloc[-1] - self.prime_log[m['time']].iloc[0]
        rpm_gain = self.prime_log[m['rpm']].iloc[-1] - self.prime_log[m['rpm']].iloc[0]
        if duration > 0:
            accel = int(rpm_gain / duration)
            self.report['performance_insights'].append(f"📈 Acceleration Rate: {accel} RPM/sec.")

    # --- THE SYNTHESIS ENGINE ---
    def _synthesize_diagnosis(self):
        """Cross-references alerts to determine the root cause."""
        alerts_str = " ".join(self.report['alerts'])
        insights_str = " ".join(self.report['performance_insights'])
        diagnosis = []

        # 1. Cascading Fuel Failure vs HPFP Limit
        if "HPFP Crash" in alerts_str and "LPFP Starvation" in alerts_str:
            diagnosis.append("🛠️ **Cascading Fuel Failure:** Your High-Pressure Fuel Pump is crashing because the in-tank Low-Pressure Fuel Pump is failing to feed it. Fix/upgrade the LPFP first.")
        elif "HPFP Crash" in alerts_str:
            diagnosis.append("🛠️ **HPFP Limit Reached:** Your HPFP is crashing, but the LPFP is healthy. You have exceeded the physical limits of the stock HPFP for this fuel blend. Consider a TU/Dorch upgrade or run less E85.")

        # 2. Dangerous Lean Condition
        if "Dangerous Lean Condition" in alerts_str:
            diagnosis.append("🚨 **CRITICAL SAFETY:** The car is running dangerously lean at WOT. DO NOT do another pull. Inspect injectors, fuel pumps, and primary O2 sensor immediately.")

        # 3. Overworked Turbo
        if "Boost Leak" in alerts_str and "WGDC maxed" in insights_str:
            diagnosis.append("🛠️ **Overworked Turbo:** A physical boost leak is forcing your wastegate to 100% to compensate. This is choking the turbo and superheating the intake air. Check charge pipe and inlet connections.")

        # 4. Octane/Knock Limit
        if ("CRITICAL: Engine knock" in alerts_str or "Timing Pull" in alerts_str) and "IAT Heat Soak" not in alerts_str:
            diagnosis.append("🛠️ **Octane Limit Reached:** You are experiencing significant timing corrections without severe intake heat. The fuel quality is too poor for this map's timing targets. Add 1-2 gallons of E85 or flash to a lower octane map.")

        # 5. Transmission Limit
        if "Throttle Closure" in insights_str and "Torque Intervention" in insights_str:
            diagnosis.append("⚙️ **TCU Intervention:** The engine is producing more torque than the transmission allows, causing a throttle closure. You may need a transmission tune (xHP) to raise the torque limits.")

        # Clean Bill of Health
        if not diagnosis and not self.report['alerts']:
            diagnosis.append("✅ **Clean Bill of Health:** Hardware is happy, fuel pressure is stable, and timing is clean. The car is running exactly as the tuner intended.")

        self.report['diagnosis'] = diagnosis
