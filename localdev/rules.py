import pandas as pd
import numpy as np

class B58DiagnosticEngine:
    def __init__(self, df):
        self.df = df.copy()
        self.cols = self.df.columns.tolist()
        self.tuner_type = self._detect_tuner()
        
        self.map = self._get_static_map()
        
        # Timing Columns
        if self.tuner_type == "MHD":
            self.timing_cols = [f"Cyl{i} Timing Cor (*)" for i in range(1, 7) if f"Cyl{i} Timing Cor (*)" in self.cols]
        else:
            self.timing_cols = [f"(RAM) Ignition Timing Corr. Cyl. {i}[°]" for i in range(1, 7) if f"(RAM) Ignition Timing Corr. Cyl. {i}[°]" in self.cols]

        # WOT Filter: Isolate the longest continuous pull
        pedal_col = self.map['pedal']
        if pedal_col in self.cols:
            self.df[pedal_col] = pd.to_numeric(self.df[pedal_col], errors='coerce')
            wot_filter = self.df[self.df[pedal_col] > 85]
            
            if not wot_filter.empty:
                # Group rows where the index difference is > 1 to find continuous pulls
                pull_groups = (wot_filter.index.to_series().diff() > 1).cumsum()
                self.all_pulls = [group for _, group in wot_filter.groupby(pull_groups)]
                
                # Automatically select the longest continuous pull for analysis
                self.wot = max(self.all_pulls, key=len).copy()
            else:
                self.wot = pd.DataFrame()
                self.all_pulls = []
        else:
            self.wot = pd.DataFrame()
            self.all_pulls = []

        self.report = {"score": 100, "status": "Healthy", "alerts": [], "performance_insights": []}

    def _detect_tuner(self):
        if "Accel Ped. Pos. (%)" in self.cols or "Cyl1 Timing Cor (*)" in self.cols:
            return "MHD"
        elif "Accel. Pedal[%]" in self.cols or "Engine speed[1/min]" in self.cols:
            return "BM3"
        raise ValueError("Platform not currently supported. Please upload an MHD or BM3 CSV.")
    
    def _get_static_map(self):
        if self.tuner_type == "MHD":
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
                'tq_lim': 'Torque Lim. active'
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
                'tq_lim': 'Torque Limiter Active'
            }

    def run_analysis(self):
        if self.wot.empty: return None
        
        self._check_boost_with_spool_awareness()
        self._check_ignition_contextual()
        self._check_fuel_pressure()
        self._check_throttle_closures()
        self._check_fuel_trims()
        self._check_iat_delta()
        
        # New "Next-Level" Checks
        self._check_wgdc()
        self._check_knock()
        self._check_lpfp()
        self._check_torque_limiters()
        
        self._calculate_performance_metrics()

        if self.report['alerts']:
            self.report['status'] = "Needs Attention"
            self.report['score'] = max(10, self.report['score'] - 40)
        
        return self.report

    def _check_boost_with_spool_awareness(self):
        m = self.map
        diffs = pd.to_numeric(self.wot[m['boost_target']]) - pd.to_numeric(self.wot[m['boost_actual']])
        
        post_spool = self.wot[self.wot[m['rpm']] > 3500]
        if not post_spool.empty:
            leak_diffs = diffs.loc[post_spool.index]
            
            if leak_diffs.max() > 3.0:
                idx = leak_diffs.idxmax()
                rpm_at = int(self.wot.loc[idx, m['rpm']])
                self.report['alerts'].append(f"💨 Boost Leak: {round(leak_diffs.max(), 1)} PSI under target detected at {rpm_at} RPM.")
                
            if leak_diffs.min() < -3.0:
                idx = leak_diffs.idxmin()
                rpm_at = int(self.wot.loc[idx, m['rpm']])
                self.report['alerts'].append(f"⚠️ Overboost: {abs(round(leak_diffs.min(), 1))} PSI over target detected at {rpm_at} RPM.")

        pre_spool = self.wot[self.wot[m['rpm']] < 3200]
        if not pre_spool.empty:
            spool_lag = diffs.loc[pre_spool.index].max()
            if spool_lag > 5.0:
                self.report['performance_insights'].append(f"ℹ️ Turbo Spool: Physical lag of {round(spool_lag, 1)} PSI below 3200 RPM.")

    def _check_ignition_contextual(self):
        if not self.timing_cols: return
        m = self.map
        pull_data = self.wot[self.timing_cols].apply(pd.to_numeric, errors='coerce')
        
        if pull_data.min().min() < -3.5:
            worst_idx = pull_data.min(axis=1).idxmin()
            worst_val = pull_data.loc[worst_idx].min()
            worst_cyl_col = pull_data.loc[worst_idx].idxmin()
            rpm_at = int(self.wot.loc[worst_idx, m['rpm']])
            
            cyl_num = ''.join(filter(str.isdigit, worst_cyl_col))
            self.report['alerts'].append(f"🔥 Timing Pull: {worst_val}° on Cylinder {cyl_num} at {rpm_at} RPM.")

    def _check_fuel_pressure(self):
        m = self.map
        rail_data = pd.to_numeric(self.wot[m['rail']], errors='coerce')
        if rail_data.min() < 1900:
            rpm_at = int(self.wot.loc[rail_data.idxmin(), m['rpm']])
            self.report['alerts'].append(f"🔴 HPFP Crash: Fuel pressure dipped to {int(rail_data.min())} PSI at {rpm_at} RPM.")

    def _check_throttle_closures(self):
        m = self.map
        throttle_data = pd.to_numeric(self.wot[m['throttle']], errors='coerce')
        if throttle_data.min() < 93:
            rpm_at = int(self.wot.loc[throttle_data.idxmin(), m['rpm']])
            self.report['performance_insights'].append(f"🟡 Throttle Closure: ECU limited throttle to {int(throttle_data.min())}% at {rpm_at} RPM.")

    def _check_fuel_trims(self):
        m = self.map
        if m['stft'] not in self.cols: return
        stft_data = pd.to_numeric(self.wot[m['stft']], errors='coerce')
        if stft_data.max() > 25:
            rpm_at = int(self.wot.loc[stft_data.idxmax(), m['rpm']])
            self.report['alerts'].append(f"⛽ Fuel Trims: STFT maxed out at +{int(stft_data.max())}% at {rpm_at} RPM (Running Lean).")

    def _check_iat_delta(self):
        m = self.map
        if m['iat'] not in self.cols: return
        iat_data = pd.to_numeric(self.wot[m['iat']], errors='coerce')
        if iat_data.empty: return
        
        delta = iat_data.iloc[-1] - iat_data.iloc[0]
        if delta > 20:
            self.report['alerts'].append(f"🌡️ IAT Heat Soak: Intake temps rose by {int(delta)}°F during the pull.")
        elif delta > 12:
            self.report['performance_insights'].append(f"🟡 IAT Rise: Intake temps rose by {int(delta)}°F. Intercooler is working hard.")

    def _check_wgdc(self):
        m = self.map
        if m['wgdc'] not in self.cols: return
        wgdc_data = pd.to_numeric(self.wot[m['wgdc']], errors='coerce')
        if wgdc_data.max() > 95:
            rpm_at = int(self.wot.loc[wgdc_data.idxmax(), m['rpm']])
            self.report['performance_insights'].append(f"🐌 Turbo Headroom: WGDC maxed out at >95% near {rpm_at} RPM. The turbo has no remaining headroom.")

    def _check_knock(self):
        m = self.map
        if m['knock'] not in self.cols: return
        knock_data = pd.to_numeric(self.wot[m['knock']], errors='coerce')
        if knock_data.max() > 0:
            rpm_at = int(self.wot.loc[knock_data.idxmax(), m['rpm']])
            self.report['alerts'].append(f"🚨 CRITICAL: Engine knock detected at {rpm_at} RPM. Check fuel quality and log safety immediately.")
            self.report['score'] = 0  # Knock immediately tanks the health score

    def _check_lpfp(self):
        m = self.map
        if m['lpfp'] not in self.cols: return
        lpfp_data = pd.to_numeric(self.wot[m['lpfp']], errors='coerce')
        if lpfp_data.min() < 55:
            rpm_at = int(self.wot.loc[lpfp_data.idxmin(), m['rpm']])
            self.report['alerts'].append(f"📉 LPFP Starvation: Low pressure fuel pump dropped to {int(lpfp_data.min())} PSI at {rpm_at} RPM.")

    def _check_torque_limiters(self):
        m = self.map
        if m['tq_lim'] not in self.cols: return
        tq_data = pd.to_numeric(self.wot[m['tq_lim']], errors='coerce')
        if tq_data.max() > 0:
            rpm_at = int(self.wot.loc[tq_data.idxmax(), m['rpm']])
            self.report['performance_insights'].append(f"⚙️ Torque Intervention: Transmission/ECU torque limiter became active at {rpm_at} RPM.")

    def _calculate_performance_metrics(self):
        m = self.map
        duration = self.wot[m['time']].iloc[-1] - self.wot[m['time']].iloc[0]
        rpm_gain = self.wot[m['rpm']].iloc[-1] - self.wot[m['rpm']].iloc[0]
        if duration > 0:
            accel = int(rpm_gain / duration)
            self.report['performance_insights'].append(f"📈 Performance: Acceleration rate is {accel} RPM/sec.")
