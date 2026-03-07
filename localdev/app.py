import streamlit as st
import pandas as pd
import plotly.express as px
from rules import B58DiagnosticEngine

st.set_page_config(page_title="B58 Master Diagnostic", layout="wide")

def main():
    st.title("Bee58")
    uploaded_file = st.file_uploader("Upload CSV Log", type=['csv'])

    if uploaded_file:
        try:
            uploaded_file.seek(0)
            content = uploaded_file.getvalue().decode("utf-8").splitlines()

            header_row_index = None
            for i, line in enumerate(content):
            # We split by comma and look for the specific first-column name
                first_col = line.split(',')[0].strip().lower()
        
        # JB4 starts with 'timestamp', MHD/BM3 starts with 'time'
                if first_col in ['timestamp', 'time']:
                    header_row_index = i
                    break
            uploaded_file.seek(0)
            df = pd.read_csv(uploaded_file, skiprows=header_row_index, on_bad_lines='skip')
            
            engine = B58DiagnosticEngine(df)
            results = engine.run_analysis()

            if results:
                st.divider()
                c1, c2, c3 = st.columns(3)
                c1.metric("Health Score", f"{results['score']}/100")
                c2.metric("Status", results['status'])
                c3.metric("WOT Samples", len(engine.wot))

                if results['alerts']:
                    for a in results['alerts']: st.error(a)
                else:
                    st.success("Critical Safety Checks Passed.")

                # SAFE PLOTTING
                st.subheader("Performance Graph")
                target = engine.mapping['boost_target']
                actual = engine.mapping['boost_actual']
                
                if target and actual and not engine.wot.empty:
                    # Plotly Express needs unique strings in the list
                    fig = px.line(engine.wot, x=engine.wot.index, y=[target, actual],
                                  title="Boost Profile (Target vs Actual)")
                    st.plotly_chart(fig, use_container_width=True)
            else:
                st.warning("No WOT pull detected.")

        except Exception as e:
            st.error(f"Analysis Error: {e}")

if __name__ == "__main__":
    main()
