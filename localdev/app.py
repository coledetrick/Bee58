import sys
import os

# Make the project root importable so `app.engine` resolves correctly
# whether you run `streamlit run localdev/app.py` from the Bee58/ directory
# or install the package with `pip install -e .` from Bee58/.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from app.engine.rules import B58DiagnosticEngine

st.set_page_config(page_title="B58 Specialized Diagnostic", layout="wide")


def _load_csv(uploaded_file) -> pd.DataFrame:
    import io
    raw = uploaded_file.read().decode("utf-8", errors="replace")
    uploaded_file.seek(0)
    lines = [l for l in raw.splitlines() if l.strip()]
    field_counts = [l.count(",") for l in lines]
    max_fields = max(field_counts)
    header_idx = next(i for i, c in enumerate(field_counts) if c == max_fields)
    return pd.read_csv(io.StringIO(raw), skiprows=header_idx)


def main():
    st.title("B58 Diagnostic")
    st.caption("Professional-grade log analysis for Gen 1 B58 (Supports MHD & BM3)")

    uploaded_file = st.file_uploader("Upload CSV Log", type=["csv"])

    if uploaded_file:
        try:
            df = _load_csv(uploaded_file)
            engine = B58DiagnosticEngine(df)
            results = engine.run_analysis()

            if not results:
                st.warning(
                    "Could not detect a valid Wide Open Throttle (WOT) pull in this log. "
                    "Ensure the pedal hit 85% or higher."
                )
                return

            st.divider()
            st.header(f"Platform Detected: {engine.tune_platform}")  # fixed: was engine.tuner_type

            # --- TOP LEVEL METRICS ---
            c1, c2, c3 = st.columns(3)
            c1.metric("Health Score", f"{results.score}/100")
            c2.metric("Status", results.status)
            c3.metric("WOT Samples Analyzed", len(engine.prime_log))  # fixed: was engine.wot

            # Show pull count if multiple pulls were found
            if results.pull_count > 1:
                st.info(f"📊 {results.pull_count} WOT pulls detected. Analysis used the longest pull; "
                        "multi-pull comparison shown below.")

            # --- FINDINGS SECTIONS ---
            col_left, col_right = st.columns(2)

            with col_left:
                st.subheader("🚨 Critical Findings")
                if results.alerts:
                    for a in results.alerts:
                        st.error(a.message)
                else:
                    st.success("No hardware safety issues detected. Log looks clean!")

            with col_right:
                st.subheader("🛠️ Performance Insights")
                if results.performance_insights:
                    for p in results.performance_insights:
                        if "ℹ️" in p.message or "📈" in p.message:
                            st.info(p.message)
                        else:
                            st.warning(p.message)
                else:
                    st.info("No specific performance anomalies noted.")

            st.divider()

            # --- ROOT CAUSE ANALYSIS (synthesis — derived from findings above) ---
            st.subheader("🔍 Root Cause Analysis")
            st.caption("Interpretation based on the findings above.")
            if results.diagnosis:
                for diag in results.diagnosis:
                    if "✅" in diag:
                        st.success(diag)
                    elif "🚨" in diag:
                        st.error(diag)
                    else:
                        st.warning(diag)
            elif not results.alerts:
                st.success(
                    "✅ **Clean Bill of Health:** Hardware is happy, fuel pressure is stable, "
                    "and timing is clean. The car is running exactly as your tuner intended.\n\n"
                    "💬 **Plain English:** Nothing wrong — no issues found across any check."
                )

            # --- MULTI-PULL COMPARISON TABLE ---
            if results.pull_comparison:
                st.divider()
                st.subheader("🔁 Pull-to-Pull Comparison")
                pull_data = [
                    {
                        "Pull #": s.pull_number,
                        "Mean Timing Correction (°)": s.mean_timing_correction,
                        "Max Boost (PSI)": s.max_boost_psi,
                        "IAT at End (°F)": s.iat_end_f,
                    }
                    for s in results.pull_comparison
                ]
                st.dataframe(pd.DataFrame(pull_data), use_container_width=True)

            # --- INTERACTIVE LOG CHART ---
            st.divider()
            st.subheader("📈 Interactive Log Analysis")

            m = engine.map
            plot_df = engine.prime_log  # fixed: was engine.wot

            c1, c2 = st.columns([1, 3])

            with c1:
                x_axis_choice = st.radio("X-Axis Alignment:", ["RPM", "Time"], horizontal=True)
                x_col = m["rpm"] if x_axis_choice == "RPM" else m["time"]

                available_cols = {
                    "Boost Target":           m["boost_target"],
                    "Boost Actual":           m["boost_actual"],
                    "WGDC":                   m.get("wgdc"),
                    "Throttle":               m["throttle"],
                    "Ignition Timing (Cyl 1)": engine.engine_timing_cols[0] if engine.engine_timing_cols else None,  # fixed: was engine.timing_cols
                    "HPFP (Rail Pressure)":   m["rail"],
                    "LPFP":                   m.get("lpfp"),
                    "IAT":                    m.get("iat"),
                    "Charge Air Temp":        m.get("charge_air"),
                }
                # Filter out None values and columns not in the log
                available_cols = {k: v for k, v in available_cols.items() if v and v in plot_df.columns}

                selected_metrics = st.multiselect(
                    "Select Parameters to Plot:",
                    options=list(available_cols.keys()),
                    default=["Boost Target", "Boost Actual", "WGDC"]
                    if "WGDC" in available_cols
                    else ["Boost Target", "Boost Actual"],
                )

            with c2:
                if selected_metrics:
                    fig = make_subplots(specs=[[{"secondary_y": True}]])

                    for metric_name in selected_metrics:
                        col_name = available_cols[metric_name]
                        is_secondary = "HPFP" in metric_name or "LPFP" in metric_name
                        fig.add_trace(
                            go.Scatter(
                                x=plot_df[x_col],
                                y=plot_df[col_name],
                                name=metric_name,
                                mode="lines",
                            ),
                            secondary_y=is_secondary,
                        )

                    fig.update_layout(
                        title="WOT Pull Data",
                        xaxis_title="Engine Speed (RPM)" if x_axis_choice == "RPM" else "Time (Seconds)",
                        hovermode="x unified",
                        height=500,
                        margin=dict(l=20, r=20, t=40, b=20),
                    )
                    fig.update_yaxes(title_text="Standard Metrics (PSI, %, °)", secondary_y=False)
                    fig.update_yaxes(title_text="Fuel Pressure (PSI)", secondary_y=True)

                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("Select parameters on the left to generate the chart.")

        except ValueError as e:
            st.error(str(e))
        except Exception as e:
            st.error(f"Error processing file: {e}. Please ensure it is a valid CSV log from BM3 or MHD.")


if __name__ == "__main__":
    main()
