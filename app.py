"""MyAssays processor.  Run with:  streamlit run app.py"""
import io
import altair as alt
import numpy as np
import pandas as pd
import streamlit as st
from myassays_parser import (
    HORMONES, CV_LIMIT, BINDING_RANGE, STD_RECOVERY_RANGE, layout_for,
    parse_myassays, parse_plate_name, classify, apply_qc, four_pl, curve_checks,
    build_sample_output, build_control_output, make_control_plate_id)

st.set_page_config(page_title="MyAssays Processor", layout="wide")
ICON = {"OK": "🟢 OK", "REVIEW": "🟡 Review", "FAIL": "🔴 Fail"}

# =========================== sidebar: inputs ===========================
with st.sidebar:
    st.header("Plate")
    up = st.file_uploader("MyAssays export (.xlsx)", type=["xlsx"])

if not up:
    st.title("MyAssays processor")
    st.info("Upload a MyAssays export in the sidebar to begin.")
    st.stop()

try:
    plate = parse_myassays(up)
except Exception as e:
    st.error(f"Couldn't read that file: {e}")
    st.stop()

guess_h, guess_n, guess_date = parse_plate_name(plate.name)
fid = up.file_id           # widget keys change with each new upload, so inputs reset
with st.sidebar:
    hormone = st.selectbox("Hormone", HORMONES, key=f"h_{fid}",
                           index=HORMONES.index(guess_h) if guess_h in HORMONES else 0)
    plate_number = st.number_input("Plate number", 1, 9999, guess_n or 1, 1, key=f"n_{fid}")
    date_assayed = st.date_input("Date assayed", guess_date, key=f"d_{fid}")
    plate_id = f"{hormone}{plate_number}"
    st.caption(f"Plate ID: **{plate_id}**")
    if guess_h and guess_h != hormone:
        st.warning(f"File is named '{plate.name}' but hormone {hormone} is selected.")

cls = classify(plate, hormone)
samples_qc = apply_qc(cls.samples)
controls_qc = apply_qc(cls.controls, check_binding=False)   # controls: CV only

st.title(f"{plate_id}  ·  {date_assayed or 'date not set'}")
if plate.notes:
    st.info(f"Notes in the file: {plate.notes}")
if cls.missing_controls:
    lay = layout_for(hormone)["controls"]
    st.error("Control wells not found for: " + ", ".join(
        f"{l} ({'/'.join(lay[l])})" for l in cls.missing_controls) +
        f". Check that the hormone ({hormone}) is the right one for this file.")
if cls.stray_controls:
    st.warning("Rows named like controls but not in the expected control wells (treated "
               "as samples): " + ", ".join(cls.stray_controls))

tab_s, tab_c, tab_curve = st.tabs(["Samples", "Controls", "Curve"])

# =========================== samples ===========================
with tab_s:
    n_fail = int((samples_qc["status"] == "FAIL").sum())
    n_rev = int((samples_qc["status"] == "REVIEW").sum())
    n_ok = len(samples_qc) - n_fail - n_rev
    a, b, c = st.columns(3)
    a.metric("🟢 OK", n_ok)
    b.metric(f"🟡 Review (binding outside {BINDING_RANGE[0]:g}-{BINDING_RANGE[1]:g}%)", n_rev)
    c.metric(f"🔴 Fail (CV ≥ {CV_LIMIT:g}% or replicate problem)", n_fail)

    bulk = st.number_input("Dilution for all samples (1 = neat, 16 = 1:16)", min_value=1.0,
                           value=1.0, step=1.0, key=f"bulk_{fid}")
    s_in = pd.DataFrame({
        "Result": samples_qc["status"].map(ICON), "Sample": samples_qc["sample"],
        "Wells": samples_qc["wells"], "Binding %": samples_qc["pct_b_b0"],
        "Conc 1": samples_qc["conc_1"], "Conc 2": samples_qc["conc_2"],
        "Conc avg": samples_qc["conc_avg"], "CV %": samples_qc["cv_pct"],
        "Dilution": bulk, "Notes": samples_qc["flags"]})
    st.caption("The box above fills the whole Dilution column. To change one sample, "
               "type over its value in the table. Everything else is read-only.")
    s_ed = st.data_editor(
        s_in, hide_index=True, key=f"samples_{fid}", width="stretch",
        disabled=[c for c in s_in.columns if c != "Dilution"],
        height=min(36 * (len(s_in) + 1) + 3, 720),
        column_config={
            "Binding %": st.column_config.NumberColumn(format="%.1f"),
            "CV %": st.column_config.NumberColumn(format="%.1f"),
            "Conc 1": st.column_config.NumberColumn(format="%g"),
            "Conc 2": st.column_config.NumberColumn(format="%g"),
            "Conc avg": st.column_config.NumberColumn(format="%g"),
            "Dilution": st.column_config.NumberColumn(min_value=1.0, step=1.0, format="%g")})

# =========================== controls ===========================
with tab_c:
    if controls_qc.empty:
        st.warning("No controls found for this hormone's layout.")
        c_ed = pd.DataFrame({"Control ID": [], "Dilution": []})
    else:
        st.caption(f"Controls are checked for CV < {CV_LIMIT:g}% only (no binding rule). "
                   "Type the ID of each control, e.g. the pool or lot name; it is combined "
                   "with the plate ID to make control_plate_id.")
        c_in = pd.DataFrame({
            "Keep": True, "Level": controls_qc["control_level"], "Control ID": "",
            "Result": controls_qc["status"].map(ICON), "Wells": controls_qc["wells"],
            "Binding %": controls_qc["pct_b_b0"], "Conc 1": controls_qc["conc_1"],
            "Conc 2": controls_qc["conc_2"], "Conc avg": controls_qc["conc_avg"],
            "CV %": controls_qc["cv_pct"], "Dilution": 1.0, "Notes": controls_qc["flags"]})
        c_ed = st.data_editor(
            c_in, hide_index=True, key=f"controls_{fid}", width="stretch",
            disabled=[c for c in c_in.columns if c not in ("Keep", "Control ID", "Dilution")],
            column_config={
                "Keep": st.column_config.CheckboxColumn(
                    "Export?", help="Untick to leave a bad control out of the export."),
                "Control ID": st.column_config.TextColumn(
                    help="e.g. the pool/lot name. Same name across plates = same control."),
                "Binding %": st.column_config.NumberColumn(format="%.1f"),
                "CV %": st.column_config.NumberColumn(format="%.1f"),
                "Conc 1": st.column_config.NumberColumn(format="%g"),
                "Conc 2": st.column_config.NumberColumn(format="%g"),
                "Conc avg": st.column_config.NumberColumn(format="%g"),
                "Dilution": st.column_config.NumberColumn(min_value=1.0, step=1.0, format="%g")})
        kept = c_ed[c_ed["Keep"].fillna(False)]
        ids = kept["Control ID"].fillna("").tolist()
        if len(kept) < len(c_ed):
            st.caption(f"{len(c_ed) - len(kept)} control(s) will be left out of the export.")
        if any(not i.strip() for i in ids):
            st.warning("Enter an ID for every control you are keeping before exporting.")
        else:
            st.write("control_plate_id:  " + "   ·   ".join(
                f"**{l}** → `{make_control_plate_id(plate_id, i)}`"
                for l, i in zip(kept["Level"], ids)))
        st.caption("Later: a Levey-Jennings view of each control across all plates "
                   "in the database, plus the running inter-assay CV.")

# =========================== curve ===========================
with tab_curve:
    checks, curve_ok = curve_checks(plate, cls.b0, cls.nsb)
    (st.success if curve_ok else st.error)(
        "Curve looks good" if curve_ok else "Check this curve before trusting the results")
    for name, passed, detail in checks:
        icon = "ℹ️" if passed is None else ("✅" if passed else "❌")
        st.write(f"{icon} **{name}**: {detail}")

    f = plate.fit
    std = plate.standards.drop_duplicates("standard")
    if len(std) and all(k in f for k in "abcd"):
        xs = np.geomspace(std["nominal"].min() * 0.5, std["nominal"].max() * 1.5, 150)
        line = pd.DataFrame({"conc": xs, "binding": four_pl(xs, f["a"], f["b"], f["c"], f["d"])})
        pts = pd.concat([
            pd.DataFrame({"conc": std["nominal"], "binding": std["raw_avg"], "Series": "Standards"}),
            samples_qc.dropna(subset=["conc_avg", "pct_b_b0"]).query("conc_avg > 0")
            .rename(columns={"conc_avg": "conc", "pct_b_b0": "binding"})[["conc", "binding"]]
            .assign(Series="Samples")])
        x = alt.X("conc:Q", scale=alt.Scale(type="log"), title="Concentration (MyAssays units)")
        y = alt.Y("binding:Q", title="% binding (B/B0)")
        chart = (
            alt.Chart(line).mark_line(color="#888").encode(x=x, y=y)
            + alt.Chart(pts).mark_circle(opacity=0.8).encode(
                x=x, y=y, size=alt.Size("Series:N", scale=alt.Scale(
                    domain=["Standards", "Samples"], range=[130, 40]), legend=None),
                color=alt.Color("Series:N", scale=alt.Scale(
                    domain=["Standards", "Samples"], range=["#1f77b4", "#ff7f0e"])),
                tooltip=["Series", "conc", "binding"])
            + alt.Chart(pd.DataFrame({"y": list(BINDING_RANGE)})).mark_rule(
                strokeDash=[4, 4], color="#bbb").encode(y="y:Q")
        ).properties(height=380)
        st.altair_chart(chart, width="stretch")
        st.caption("Grey line: fitted 4PL curve. Dashed lines: the "
                   f"{BINDING_RANGE[0]:g}-{BINDING_RANGE[1]:g}% working window.")
    with st.expander("Fit parameters and standards"):
        st.write({k: v for k, v in f.items()})
        st.dataframe(plate.standards[["standard", "nominal", "well", "raw", "backfit",
                                      "recovery_pct"]], hide_index=True)

# =========================== sidebar: export ===========================
dil_s = s_ed["Dilution"].fillna(1.0).values
samp_out = build_sample_output(samples_qc, dil_s, hormone, plate_number, date_assayed)
if controls_qc.empty:
    ctrl_out = pd.DataFrame()
else:
    ctrl_out = build_control_output(
        controls_qc, c_ed["Control ID"].fillna("").tolist(),
        c_ed["Dilution"].fillna(1.0).values, hormone, plate_number, date_assayed)
    ctrl_out = ctrl_out[c_ed["Keep"].fillna(False).values].reset_index(drop=True)

with st.sidebar:
    st.divider()
    st.header("Export")
    problems = []
    if not date_assayed:
        problems.append("date assayed")
    if not ctrl_out.empty and (ctrl_out["control_id"] == "").any():
        problems.append("control IDs")
    if problems:
        st.warning("Still needed: " + ", ".join(problems))
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        samp_out.to_excel(xw, sheet_name="Samples", index=False)
        ctrl_out.to_excel(xw, sheet_name="Controls", index=False)
    st.download_button("Samples (.csv)", samp_out.to_csv(index=False),
                       f"{plate_id}_samples.csv", width="stretch")
    st.download_button("Controls (.csv)", ctrl_out.to_csv(index=False),
                       f"{plate_id}_controls.csv", width="stretch")
    st.download_button("Both (.xlsx)", buf.getvalue(), f"{plate_id}_results.xlsx",
                       width="stretch")
    st.caption("conc values are raw MyAssays values, not multiplied by dilution.")

# Database hook, one destination per hormone (fill in when SQL Server is ready):
# DESTINATIONS = {"P": {"samples": "...", "controls": "..."}, "E": {...}, "T": {...}, "GC": {...}}
# dest = DESTINATIONS[hormone]; save(dest["samples"], samp_out); save(dest["controls"], ctrl_out)
