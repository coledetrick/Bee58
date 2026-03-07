import streamlit as st
import pandas as pd
import plotly.express as px
from rules import B58DiagnosticEngine

st.set_page_config(page_title="B58 Specialized Diagnostic", layout="wide")

def main():
    st.title("B58 specialized Diagnostic")
    st.caption("Supports MHD and bootmod3 (BM3) Logs")

    uploaded_file = st.file_uploader("Upload CSV Log", type=['csv'])

    if uploaded_file:
        try:
            # BM3/MHD logs usually have headers on the first line
            df = pd.read_csv(uploaded_file)
            
            engine = B58DiagnosticEngine(df)
            results = engine.run_analysis()

            if results:
                st.divider()
                st.header(f"Tuner Detected: {engine.tuner_type}")
                
                c1, c2, c3 = st.columns(3)
                c1.metric("Health Score", f"{results['score']}/100")
                c2.metric("Status", results['status'])
                c3.metric("WOT Samples", len(engine.wot))

                if results['alerts']:
                    st.subheader("Critical Findings")
                    for a in results['alerts']: st.error(a)
                else:
                    st.success("No hardware safety issues detected.")

                if results['performance_insights']:
                    st.subheader("Performance & Tuning Insights")
                    for p in results['performance_insights']:
                        if "📈" in p: st.info(p)
                        else: st.warning(p)

                # Charting using mapped columns
                m = engine.map
                fig = px.line(engine.wot, x=m['rpm'], y=[m['boost_target'], m['boost_actual']],
                              title="Boost Analysis (WOT Only)", labels={m['rpm']: "RPM", "value": "PSI"})
                st.plotly_chart(fig, use_container_width=True)
                
            else:
                st.warning("No Wide Open Throttle pull (>85%) detected in this log.")

        except Exception as e:
            st.error(f"App Error: {e}")

if __name__ == "__main__":
    main()
