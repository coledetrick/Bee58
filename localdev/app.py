import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from rules import B58DiagnosticEngine

st.set_page_config(page_title="B58 Specialized Diagnostic", layout="wide")

def main():
    st.title("B58 Specialized Diagnostic")
    st.caption("Supports MHD and bootmod3 (BM3) Logs")

    uploaded_file = st.file_uploader("Upload CSV Log", type=['csv'])

    if uploaded_file:
        try:
            df = pd.read_csv(uploaded_file)
            
            # Initialization triggers our platform detection
            engine = B58DiagnosticEngine(df)
            results = engine.run_analysis()

            if results:
                st.divider()
                st.header(f"Tuner Detected: {engine.tuner_type}")
                
                # Top Level Metrics
                c1, c2, c3 = st.columns(3)
                c1.metric("Health Score", f"{results['score']}/100")
                c2.metric("Status", results['status'])
                c3.metric("WOT Samples", len(engine.wot))

                # Findings Sections
                col_left, col_right = st.columns(2)
                
                with col_left:
                    st.subheader("Critical Findings")
                    if results['alerts']:
                        for a in results['alerts']: st.error(a)
                    else:
                        st.success("No hardware safety issues detected.")

                with col_right:
                    st.subheader("Performance Insights")
                    if results['performance_insights']:
                        for p in results['performance_insights']:
                            if "📈" in p: st.info(p)
                            else: st.warning(p)

                # --- ADVANCED VISUALIZATION SECTION ---
                st.divider()
                st.subheader("Interactive Log Analysis")
                
                m = engine.map
                
                # 1. Setup UI Controls for the Chart
                c1, c2 = st.columns([1, 3])
                with c1:
                    x_axis_choice = st.radio("X-Axis Alignment:", ["RPM", "Time"], horizontal=True)
                    x_col = m['rpm'] if x_axis_choice == "RPM" else m['time']
                    
                    # Define which sensors usually need high-pressure scaling
