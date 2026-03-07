import pandas as pd
import numpy as np

class B58DiagnosticEngine:
    def __init__(self, df):
        self.df = df.copy()
        # We DO NOT rename self.df.columns. We keep them unique.
        self.raw_cols = self.df.columns.tolist()
        
        # 1. Create a "Searchable" version of headers for mapping
        searchable = [c.lower() for c in self.raw_cols]
        
        # 2. Advanced Mapping Logic
        self.mapping = {
            'pedal': self._match(searchable, ['accel', 'pedal'], exclude=['vvt', 'angle']),
            'rpm': self._match(searchable, ['rpm', 'engine speed']),
            'boost_target': self._match(searchable, ['boost pressure (target)', 'boost target'], exclude=['ram']),
            'boost_actual': self._match(searchable, ['boost (pre-throttle)', 'boost pressure (actual)', 'boost (psi)'], exclude=['target', 'deviation']),
            'iat': self._match(searchable, ['iat', 'intake temp']),
            'rail_actual': self._match(searchable, ['hpfp act', 'rail pressure', 'fp_h'])
        }

        # 3. Find Timing Corrections (handling BM3 RAM vs MHD standard)
        self.timing_cols = [c for c in self.raw_cols if 'timing corr' in c.lower() or 'timing cor' in c.lower() or 'ign_' in c.lower()]

        # 4. Filter for WOT
        pedal_col = self.mapping['pedal']
        if pedal_col:
            self.df[pedal_col] = pd.to_numeric(self.df[pedal_col], errors='coerce')
            self.wot = self.df[self.df[pedal_col] > 95].copy()
        else:
            self.wot = pd.DataFrame()

        self.report = {"score": 100, "status": "Healthy", "alerts": [], "insights": []}

    def _match(self, searchable_list, keywords, exclude=None):
        """Finds the best unique column match while avoiding false positives."""
        for col_name in self.raw_cols:
            low_col = col_name.lower()
            if any(k in low_col for k in keywords):
                if exclude and any(e in low_col for e in exclude):
                    continue
                return col_name
        return None

    def run_analysis(self):
        if self.wot.empty: return None

        # --- Analysis: Boost ---
        target = self.mapping['boost_target']
        actual = self.mapping['boost_actual']
        if target and actual:
            # Ensure we are comparing numbers
            t_val = pd.to_numeric(self.wot[target], errors='coerce')
            a_val = pd.to_numeric(self.wot[actual], errors='coerce')
            dev = (t_val - a_val).mean()
            
            # Filter out "impossible" math (like comparing Load % to PSI)
            if 0 < dev < 40: 
                if dev > 3.0:
                    self.report['alerts'].append(f"Boost Deviation: {round(dev,1)} PSI below target.")
                    self.report['score'] -= 20

        # --- Analysis: Timing ---
        if self.timing_cols:
            # BM3/MHD pull timing. We look for negative values.
            pull_data = self.wot[self.timing_cols].apply(pd.to_numeric, errors='coerce')
            worst_pull = pull_data.min().min()
            if worst_pull < -3.5:
                self.report['alerts'].append(f"Ignition Timing Pull: {worst_pull}° detected.")
                self.report['score'] -= 30

        if self.report['score'] < 75: self.report['status'] = "Attention Required"
        return self.report
