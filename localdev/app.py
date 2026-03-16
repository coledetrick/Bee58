import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from rules import B58DiagnosticEngine

st.set_page_config(page_title="B58 Specialized Diagnostic", layout="wide")

def main():
    st.title("🏁 B58 Specialized Diagnostic")
    st.caption("Professional-grade log analysis for Gen 1 B58 (Supports MHD & BM3)")

    uploaded_file = st.file_uploader("Upload CSV Log", type=['csv'])

    if uploaded_file:
        try:
            df = pd.read_csv(uploaded_file)
            
            # Initialize our diagnostic engine
            engine = B58DiagnosticEngine(df)
            results = engine.run_analysis()

            if not results:
                st.warning("Could not detect a valid Wide Open Throttle (WOT) pull in this log. Ensure the pedal hit 85% or higher.")
                return

            st.divider()
            st.header(f"Tuner Detected: {engine.tuner_type}")
            
            # --- TOP LEVEL METRICS ---
            c1, c2, c3 = st.columns(3)
            c1.metric("Health Score", f"{results['score']}/100")
            c2.metric("Status", results['status'])
            c3.metric("WOT Samples Analyzed", len(engine.wot))

            # --- FINDINGS SECTIONS ---
            col_left, col_right = st.columns(2)
            
            with col_left:
                st.subheader("🚨 Critical Findings")
                if results['alerts']:
                    for a in results['alerts']: st.error(a)
                else:
                    st.success("No hardware safety issues detected. Log looks clean!")

            with col_right:
                st.subheader("🛠️ Performance Insights")
                if results['performance_insights']:
                    for p in results['performance_insights']:
                        if "ℹ️" in p or "📈" in p: 
                            st.info(p)
                        else: 
                            st.warning(p)
                else:
                    st.info("No specific performance anomalies noted.")

            # --- ADVANCED VISUALIZATION SECTION ---
            st.divider()
            st.subheader("📈 Interactive Log Analysis")
            
            m = engine.map
            plot_df = engine.wot
            
            # 1. Setup UI Controls for the Chart
            c1, c2 = st.columns([1, 3])
            
            with c1:
                x_axis_choice = st.radio("X-Axis Alignment:", ["RPM", "Time"], horizontal=True)
                x_col = m['rpm'] if x_axis_choice == "RPM" else m['time']
                
                # Define available parameters to plot (ignoring missing columns gracefully)
                available_cols = {
                    "Boost Target": m['boost_target'],
                    "Boost Actual": m['boost_actual'],
                    "WGDC": m.get('wgdc'),
                    "Throttle": m['throttle'],
                    "Ignition Timing (Cyl 1)": engine.timing_cols[0] if engine.timing_cols else None,
                    "HPFP (Rail Pressure)": m['rail'],
                    "LPFP": m.get('lpfp'),
                    "IAT": m.get('iat')
                }
                # Filter out None values in case the log didn't include them
                available_cols = {k: v for k, v in available_cols.items() if v in plot_df.columns}
                
                selected_metrics = st.multiselect(
                    "Select Parameters to Plot:",
                    options=list(available_cols.keys()),
                    default=["Boost Target", "Boost Actual", "WGDC"] if "WGDC" in available_cols else ["Boost Target", "Boost Actual"]
                )

            # 2. Build the Interactive Plotly Chart
            with c2:
                if selected_metrics:
                    # Create a figure with a secondary y-axis for high-value metrics
                    fig = make_subplots(specs=[[{"secondary_y": True}]])
                    
                    for metric_name in selected_metrics:
                        col_name = available_cols[metric_name]
                        
                        # Route high-pressure fuel metrics to the secondary Y-axis
                        is_secondary = "HPFP" in metric_name or "LPFP" in metric_name
                        
                        fig.add_trace(
                            go.Scatter(
                                x=plot_df[x_col], 
                                y=plot_df[col_name], 
                                name=metric_name,
                                mode='lines'
                            ),
                            secondary_y=is_secondary
                        )

                    # Configure layout for a clean tuner aesthetic
                    fig.update_layout(
                        title="WOT Pull Data",
                        xaxis_title="Engine Speed (RPM)" if x_axis_choice == "RPM" else "Time (Seconds)",
                        hovermode="x unified",
                        height=500,
                        margin=dict(l=20, r=20, t=40, b=20)
                    )
                    
                    fig.update_yaxes(title_text="Standard Metrics (PSI, %, °)", secondary_y=False)
                    fig.update_yaxes(title_text="Fuel Pressure (PSI)", secondary_y=True)

                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("Select parameters on the left to generate the chart.")

        except Exception as e:
            st.error(f"Error processing file: {e}. Please ensure it is a valid CSV log from BM3 or MHD.")

if __name__ == "__main__":
    main()
