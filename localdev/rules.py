import pandas as pd
import numpy as np

class B58DiagnosticEngine:
    def __init__(self, df):
        self.df = df.copy()
        self.cols = self.df.columns.tolist()
        self.tuner_type = self._detect_tuner()
        
        # Static Mapping for MHD vs BM3
        self.map = self._get_static_map()
        
        # Identify Timing Columns (platform-specific)
        if self.tuner_type == "MHD":
            self.timing_cols = [f"Cyl{i} Timing Cor (*)" for i in range(1, 7) if f"Cyl{i} Timing Cor (*)" in self.cols]
        else: # BM3
            self.timing_cols = [f"(RAM) Ignition Timing Corr. Cyl. {i}[°]" for i in range(1, 7) if f"(RAM) Ignition Timing Corr. Cyl. {i}[°]" in self.cols]

        # WOT Filter: BM3/MHD Pedal is 0-100. Lowered to 85% to capture all types of pulls.
        pedal_col = self.map['pedal']
        if pedal_col in self.cols:
            self.df[pedal_col] = pd.to_numeric(self.df[pedal_col], errors='coerce')
            self.wot = self.df[self.df[pedal_col] > 85].copy()
        else:
            self.wot = pd.DataFrame()

        self.report = {"score": 100, "status": "Healthy", "alerts": [], "performance_insights": []}

    def _detect_tuner(self):
        """Identifies the tuner based on signature columns or raises ValueError."""
        # MHD Indicators
        if "Accel Ped. Pos. (%)" in self.cols or "Cyl1 Timing Cor (*)" in self.cols:
            return "MHD"
        
        # BM3 Indicators (Common unique headers for Bootmod3)
        elif "Accel. Pedal[%]" in self.cols or "Engine speed[1/min]" in self.cols:
            return "BM3"
            
        # If neither is found, raise an exception to be caught by the UI/Main loop
        raise ValueError("Platform not currently supported.")
    
    def _get_static_map(self):
        """Returns the static column names for the detected tuner."""
        if self.tuner_type == "MHD":
            return {
                'pedal': 'Accel Ped. Pos. (%)',
                'rpm': 'RPM (rpm)',
                'boost_target': 'Boost target (PSI)',
                'boost_actual': 'Boost (PSI)',
                'throttle': 'Throttle Position (*)',
                'rail': 'Rail pressure mean 1 (PSI)',
                'iat': 'IAT (*F)',
                'time': 'Time'
            }
        else: # BM3
            return {
                'pedal': 'Accel. Pedal[%]',
                'rpm': 'Engine speed[1/min]',
                'boost_target': 'Boost pressure (Target)[psig]',
                'boost_actual': 'Boost (Pre-Throttle)[psig]',
                'throttle': 'Throttle Angle[%]',
                'rail': 'HPFP Act.[psig]',
                'iat': 'IAT[F]',
                'time': 'Time'
            }

    def run_analysis(self):
        if self.wot.empty: return None
        
        self._check_boost_with_spool_awareness()
        self._check_ignition_contextual()
        self._check_fuel_pressure()
        self._check_throttle_closures()
        self._calculate_performance_metrics()

        if self.report['alerts']:
            self.report['status'] = "Needs Attention"
            self.report['score'] = max(10, self.report['score'] - 40)
        
        return self.report

    def _check_boost_with_spool_awareness(self):
        m = self.map
        # Calculate deviation across the whole log
        diffs = pd.to_numeric(self.wot[m['boost_target']]) - pd.to_numeric(self.wot[m['boost_actual']])
        
        # 1. Leak Check (Post-Spool: > 3500 RPM)
        post_spool = self.wot[self.wot[m['rpm']] > 3500]
        if not post_spool.empty:
            leak_diffs = diffs.loc[post_spool.index]
            if leak_diffs.max() > 3.0:
                idx = leak_diffs.idxmax()
                rpm_at = int(self.wot.loc[idx, m['rpm']])
                self.report['alerts'].append(f"Boost Leak: {round(leak_diffs.max(), 1)} PSI deviation detected at {rpm_at} RPM.")

        # 2. Spool Insight (Pre-Spool: < 3200 RPM)
        pre_spool = self.wot[self.wot[m['rpm']] < 3200]
        if not pre_spool.empty:
            spool_lag = diffs.loc[pre_spool.index].max()
            if spool_lag > 5.0:
                self.report['performance_insights'].append(f"ℹ️ Turbo Spool: Physical lag of {round(spool_lag, 1)} PSI below 3200 RPM (Normal as the turbo takes time to spool up.).")

    def _check_ignition_contextual(self):
        if not self.timing_cols: return
        m = self.map
        pull_data = self.wot[self.timing_cols].apply(pd.to_numeric, errors='coerce')
        
        if pull_data.min().min() < -3.5:
            worst_idx = pull_data.min(axis=1).idxmin()
            worst_val = pull_data.loc[worst_idx].min()
            worst_cyl_col = pull_data.loc[worst_idx].idxmin()
            rpm_at = int(self.wot.loc[worst_idx, m['rpm']])
            
            # Clean cylinder name
            cyl_num = ''.join(filter(str.isdigit, worst_cyl_col))
            self.report['alerts'].append(f"Timing Pull: {worst_val}° on Cylinder {cyl_num} at {rpm_at} RPM.")

    def _check_fuel_pressure(self):
        m = self.map
        rail_data = pd.to_numeric(self.wot[m['rail']], errors='coerce')
        if rail_data.min() < 1900:
            rpm_at = int(self.wot.loc[rail_data.idxmin(), m['rpm']])
            self.report['alerts'].append(f"🔴 Fuel Pressure: HPFP dipped to {int(rail_data.min())} PSI at {rpm_at} RPM.")

    def _check_throttle_closures(self):
        m = self.map
        throttle_data = pd.to_numeric(self.wot[m['throttle']], errors='coerce')
        if throttle_data.min() < 93:
            rpm_at = int(self.wot.loc[throttle_data.idxmin(), m['rpm']])
            self.report['performance_insights'].append(f"🟡 Throttle Closure: ECU limited throttle to {int(throttle_data.min())}% at {rpm_at} RPM.")

    def _calculate_performance_metrics(self):
        m = self.map
        duration = self.wot[m['time']].iloc[-1] - self.wot[m['time']].iloc[0]
        rpm_gain = self.wot[m['rpm']].iloc[-1] - self.wot[m['rpm']].iloc[0]
        if duration > 0:
            accel = int(rpm_gain / duration)
            self.report['performance_insights'].append(f"📈 Performance: Acceleration rate is {accel} RPM/sec.")
