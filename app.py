"""MyAssays -> QC / results web app.  Run with:  streamlit run app.py"""
import io
import pandas as pd
import streamlit as st
from myassays_parser import (parse_myassays, apply_qc,
                             apply_control_qc, apply_standard_qc)

st.set_page_config(page_title="MyAssays Processor", layout="wide")
st.title("MyAssays hormone assay processor")

# ---- sidebar: QC thresholds (edit once, everyone uses the same defaults) ----
with st.sidebar:
    st.header("QC settings")
    cv_limit = st.number_input("Duplicate %CV must be below", 1.0, 100.0, 15.5, 0.5)
    b_lo = st.number_input("Binding (%B/B0) minimum", 0.0, 100.0, 20.0)
    b_hi = st.number_input("Binding (%B/B0) maximum", 0.0, 100.0, 80.0)
    rec_lo, rec_hi = 80.0, 120.0  # standard recovery: informational only

up = st.file_uploader("Upload the MyAssays export (.xlsx)", type=["xlsx"])
if not up:
    st.info("Upload a MyAssays Excel export to begin.")
    st.stop()

try:
    plate = parse_myassays(up)
except Exception as e:
    st.error(f"Couldn't read that file: {e}")
    st.stop()

st.subheader(plate.name or "Unnamed plate")
if plate.notes:
    st.caption(f"Notes in file: {plate.notes}")

# ---- plate-level info ----
c = st.columns(4)
c[0].metric("R² (4PL)", f"{plate.fit.get('R²', float('nan')):.4f}")
c[1].metric("MSE", f"{plate.fit.get('MSE', float('nan')):.4f}")
c[2].metric("B0 raw", f"{plate.b0['raw_1'].iloc[0]:.3f} / {plate.b0['raw_2'].iloc[0]:.3f}"
            if len(plate.b0) else "n/a")
c[3].metric("NSB raw", f"{plate.nsb['raw_1'].iloc[0]:.3f} / {plate.nsb['raw_2'].iloc[0]:.3f}"
            if len(plate.nsb) else "n/a")

# ---- dilutions (typed by hand; 1 = neat). Final conc = MyAssays conc x factor ----
st.markdown("### Dilutions")
st.caption("Enter the dilution factor for each sample (e.g. 128 for 1:128). "
           "Leave at 1 if neat. See the notes line above for what was run.")
dil_df = pd.DataFrame({"sample": plate.samples["sample"], "dilution": 1.0})
dil_df = st.data_editor(dil_df, hide_index=True, disabled=["sample"], height=250,
                        column_config={"dilution": st.column_config.NumberColumn(
                            "Dilution factor (e.g. 128 for 1:128)", min_value=1)})
dilutions = dict(zip(dil_df["sample"], dil_df["dilution"]))

controls = apply_control_qc(plate.controls, cv_limit, b_lo, b_hi)
standards = apply_standard_qc(plate.standards, rec_lo, rec_hi)
samples = apply_qc(plate.samples, dilutions, cv_limit, b_lo, b_hi)

# ---- results ----
cols = ["sample", "wells", "pct_b_b0", "conc_1", "conc_2", "conc_avg", "cv_pct",
        "sd", "sem", "dilution", "final_conc", "status", "flags"]
def hl(row):
    s = row.get("status", "")
    color = {"FAIL": "#f8c8c8", "REVIEW": "#fff3a3"}.get(s, "")
    if "status" not in row and row.get("flags"):   # standards table
        color = "#eeeeee"
    return [f"background-color: {color}" if color else "" for _ in row]

tab1, tab2, tab3 = st.tabs(["Samples", "Controls", "Standards"])
with tab1:
    n_fail = int((samples["status"] == "FAIL").sum())
    n_rev = int((samples["status"] == "REVIEW").sum())
    st.write(f"{len(samples)} samples: **{n_fail} fail** (red: CV or replicate problem), "
             f"**{n_rev} to review** (yellow: CV ok, binding outside {b_lo:g}-{b_hi:g}%)")
    st.dataframe(samples[cols].style.apply(hl, axis=1).format(precision=2),
                 hide_index=True, use_container_width=True)
with tab2:
    st.dataframe(controls[["sample", "wells", "pct_b_b0", "conc_1", "conc_2",
                           "conc_avg", "cv_pct", "status", "flags"]].style.apply(hl, axis=1),
                 hide_index=True, use_container_width=True)
with tab3:
    st.dataframe(standards[["standard", "nominal", "well", "raw", "backfit",
                            "recovery_pct", "flags"]].style.apply(hl, axis=1),
                 hide_index=True, use_container_width=True)

# ---- downloads ----
st.markdown("### Export")
plate_cols = {"plate": plate.name, "R2": plate.fit.get("R²"), "notes": plate.notes}
ctrl_out = controls.assign(**plate_cols)
samp_out = samples.assign(**plate_cols)

buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as xw:
    samp_out.to_excel(xw, sheet_name="Samples", index=False)
    ctrl_out.to_excel(xw, sheet_name="Controls", index=False)
    standards.to_excel(xw, sheet_name="Standards", index=False)
d1, d2, d3 = st.columns(3)
d1.download_button("Download results (.xlsx)", buf.getvalue(),
                   file_name=f"{plate.name or 'plate'}_results.xlsx")
d2.download_button("Controls table (.csv)", ctrl_out.to_csv(index=False),
                   file_name=f"{plate.name or 'plate'}_controls.csv")
d3.download_button("Samples table (.csv)", samp_out.to_csv(index=False),
                   file_name=f"{plate.name or 'plate'}_samples.csv")

# ---- database hook (to fill in once we know your two databases) ----
# def save_controls(df): ...   # -> controls database
# def save_samples(df):  ...   # -> samples database
# st.button("Send to databases", on_click=lambda: (save_controls(ctrl_out), save_samples(samp_out)))
