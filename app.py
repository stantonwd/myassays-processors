"""MyAssays processor.  Run with:  streamlit run app.py"""
import io
import altair as alt
import numpy as np
import pandas as pd
import streamlit as st
from myassays_parser import (
    HORMONES, CV_LIMIT, BINDING_RANGE, STD_RECOVERY_RANGE, layout_for,
    parse_myassays, parse_plate_name, classify, apply_qc, four_pl, curve_checks,
    build_sample_output, build_control_output, make_control_plate_id,
    apply_control_expectations, plate_verdict, duplicate_runs, CONTROL_TOLERANCE)
import records as rec

st.set_page_config(page_title="MyAssays Processor", layout="wide")
ICON = {"OK": "🟢 OK", "REVIEW": "🟡 Review", "FAIL": "🔴 Fail"}
SERIES, MUTED = "#2a78d6", "#898781"     # Levey-Jennings: one series + reference lines


def _secret(key):
    try:
        return st.secrets.get(key)
    except Exception:            # no secrets.toml on this machine
        return None


# Data folder (records + collection file), outside the repo. See records.py.
DATA_DIR = rec.data_dir(_secret("data_dir"))
REC_DIR = rec.records_dir(DATA_DIR)

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
controls_qc = apply_control_expectations(           # controls: CV + expected binding,
    apply_qc(cls.controls, check_binding=False))    # not the 20-80% window
checks, curve_ok = curve_checks(plate, cls.b0, cls.nsb)
rerun_reasons = plate_verdict(checks, controls_qc, cls.missing_controls)

db = rec.load_all(REC_DIR)
coll_file = rec.find_collection_file(DATA_DIR)
collection = None
if coll_file:
    try:
        collection = rec.load_collection(coll_file)
    except Exception as e:
        st.sidebar.warning(f"Couldn't read {coll_file.name}: {e}")
with st.sidebar:
    st.caption(f"Data folder: `{DATA_DIR}`")
    if coll_file:
        st.caption(f"Collection file: `{coll_file.name}`")
    else:
        st.warning("No fecal_hormone_collection_*.csv in the data folder, so pg/g "
                   "can't be calculated.")
    if (db["plates"]["plate_id"] == plate_id).any():
        st.warning(f"{plate_id} is already in the records. Saving again replaces it.")

st.title(f"{plate_id}  ·  {date_assayed or 'date not set'}")
if rerun_reasons:
    st.warning("**Consider rerunning this plate**\n\n" +
               "\n".join(f"- {r}" for r in rerun_reasons))
else:
    st.success("**Plate check:** controls are near their expected binding and the "
               "curve has a straight working range.")
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

tab_s, tab_c, tab_r, tab_lj, tab_curve = st.tabs(
    ["Samples", "Controls", "Reruns & results", "Levey-Jennings", "Curve"])

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
    ids = samples_qc["sample"].astype(str)
    h = db["assays"]
    h = h[(h["hormone"] == hormone) & (h["plate_id"] != plate_id)]
    earlier = ids.map(h["sample_id"].value_counts()).fillna(0).astype(int)
    s_in = pd.DataFrame({
        "Result": samples_qc["status"].map(ICON), "Sample": samples_qc["sample"],
        "Rerun": (earlier > 0) | ids.duplicated(keep=False), "Earlier runs": earlier,
        "Wells": samples_qc["wells"], "Binding %": samples_qc["pct_b_b0"],
        "Conc 1": samples_qc["conc_1"], "Conc 2": samples_qc["conc_2"],
        "Conc avg": samples_qc["conc_avg"], "CV %": samples_qc["cv_pct"],
        "Dilution": bulk, "Notes": samples_qc["flags"]})
    st.caption("The box above fills the whole Dilution column. To change one sample, "
               "type over its value in the table. **Rerun** is ticked when the sample "
               "already has a run of this hormone in the records (Earlier runs); tick it "
               "yourself for reruns of plates that were never saved. Everything else is "
               "read-only.")
    s_ed = st.data_editor(
        s_in, hide_index=True, key=f"samples_{fid}", width="stretch",
        disabled=[c for c in s_in.columns if c not in ("Dilution", "Rerun")],
        height=min(36 * (len(s_in) + 1) + 3, 720),
        column_config={
            "Binding %": st.column_config.NumberColumn(format="%.1f"),
            "CV %": st.column_config.NumberColumn(format="%.1f"),
            "Conc 1": st.column_config.NumberColumn(format="%g"),
            "Conc 2": st.column_config.NumberColumn(format="%g"),
            "Conc avg": st.column_config.NumberColumn(format="%g"),
            "Dilution": st.column_config.NumberColumn(min_value=1.0, step=1.0, format="%g")})
    at_one = s_ed.loc[s_ed["Rerun"].fillna(False) & (s_ed["Dilution"] == 1), "Sample"]
    if len(at_one):
        st.warning(f"{len(at_one)} rerun(s) still at dilution 1 (neat): "
                   f"{', '.join(map(str, at_one))}. Reruns are usually at a different "
                   "dilution, so check these before comparing or averaging runs.")

# =========================== controls ===========================
with tab_c:
    if controls_qc.empty:
        st.warning("No controls found for this hormone's layout.")
        c_ed = pd.DataFrame({"Control ID": [], "Dilution": []})
    else:
        st.caption(f"Controls are checked for CV < {CV_LIMIT:g}% and for binding within "
                   f"±{CONTROL_TOLERANCE:g} points of the expected level (no 20-80% rule). "
                   "Type the ID of each control, e.g. the lot name P_HI_6JUL2026; it is "
                   "combined with the plate ID to make control_plate_id. IDs are pre-filled "
                   "with the last lot saved for this hormone and level.")
        last = rec.last_lots(db["controls"], hormone)
        c_in = pd.DataFrame({
            "Keep": controls_qc["status"].ne("FAIL"), "Level": controls_qc["control_level"],
            "Control ID": controls_qc["control_level"].map(last).fillna(""),
            "Result": controls_qc["status"].map(ICON), "Wells": controls_qc["wells"],
            "Binding %": controls_qc["pct_b_b0"], "Expected %": controls_qc["expected_pct"],
            "Conc 1": controls_qc["conc_1"],
            "Conc 2": controls_qc["conc_2"], "Conc avg": controls_qc["conc_avg"],
            "CV %": controls_qc["cv_pct"], "Dilution": 1.0, "Notes": controls_qc["flags"]})
        c_ed = st.data_editor(
            c_in, hide_index=True, key=f"controls_{fid}", width="stretch",
            disabled=[c for c in c_in.columns if c not in ("Keep", "Control ID", "Dilution")],
            column_config={
                "Keep": st.column_config.CheckboxColumn(
                    "Use in CVs?", help="Untick to leave a bad control out of the inter- "
                    "and intra-assay CVs. It is still saved, with keep_yn = False."),
                "Control ID": st.column_config.TextColumn(
                    help="e.g. the pool/lot name. Same name across plates = same control."),
                "Binding %": st.column_config.NumberColumn(format="%.1f"),
                "Expected %": st.column_config.NumberColumn(format="~%g"),
                "CV %": st.column_config.NumberColumn(format="%.1f"),
                "Conc 1": st.column_config.NumberColumn(format="%g"),
                "Conc 2": st.column_config.NumberColumn(format="%g"),
                "Conc avg": st.column_config.NumberColumn(format="%g"),
                "Dilution": st.column_config.NumberColumn(min_value=1.0, step=1.0, format="%g")})
        kept = c_ed[c_ed["Keep"].fillna(False)]
        ids = kept["Control ID"].fillna("").tolist()
        if len(kept) < len(c_ed):
            st.caption(f"{len(c_ed) - len(kept)} control(s) will be left out of the CVs.")
        if any(not i.strip() for i in ids):
            st.warning("Enter an ID for every control you are keeping before exporting.")
        else:
            st.write("control_plate_id:  " + "   ·   ".join(
                f"**{l}** → `{make_control_plate_id(plate_id, i)}`"
                for l, i in zip(kept["Level"], ids)))

# ===================== output tables (used by the tabs below and the export) =====================
samp_out = build_sample_output(samples_qc, s_ed["Dilution"].fillna(1.0).values,
                               hormone, plate_number, date_assayed)
assays = rec.assay_table(samp_out, samples_qc, s_ed["Rerun"].fillna(False).tolist())
if controls_qc.empty:
    ctrl_rec = rec.tidy(pd.DataFrame(), rec.CONTROL_COLS)
else:
    ctrl_rec = rec.control_table(
        build_control_output(controls_qc, c_ed["Control ID"].fillna("").tolist(),
                             c_ed["Dilution"].fillna(1.0).values, hormone, plate_number,
                             date_assayed),
        controls_qc, c_ed["Keep"].fillna(False).tolist())


def hl(row):
    s = str(row.get("result_status", ""))
    color = ("#f8c8c8" if s.startswith(("FAIL", "PENDING", "NO RESULT")) else
             "#fff3a3" if s in {"REVIEW", "AVG OF FAILS", "FAILED RUN ACCEPTED"} else "")
    return [f"background-color: {color}" if color else "" for _ in row]


# =========================== reruns & results ===========================
errors = []
with tab_r:
    if assays.empty:
        st.info("No samples on this plate.")
        results = rec.tidy(pd.DataFrame(), rec.RESULT_COLS)
    else:
        runs = rec.all_runs(assays, db["assays"])
        assays["run_number"] = assays["run_id"].map(
            runs[runs["_cur"]].set_index("run_id")["run_number"])
        groups = dict(tuple(runs.groupby("sample_id", sort=False)))
        sugg = {sid: rec.suggest_result(g) for sid, g in groups.items()}
        attention = [sid for sid, g in groups.items()
                     if len(g) > 1 or sugg[sid]["result_status"] != "OK"]
        st.markdown(
            "Samples with more than one run, or without a passing run. A single run "
            f"outside {BINDING_RANGE[0]:g}-{BINDING_RANGE[1]:g}% binding is **REVIEW**; "
            "when *every* run of a sample is outside binding (or fails for any reason) it "
            "is **FAIL (all runs)**. To report one anyway, choose **Use run(s)** and type "
            "run numbers: one number reports that run, several report the average of "
            "conc × dilution across them. Reporting from failed runs needs initials and a "
            "justification.")
        no_hist = assays.loc[assays["rerun_yn"] & assays["run_number"].eq(1), "sample_id"]
        if len(no_hist):
            st.info("Ticked as reruns but no earlier run is in the records: "
                    + ", ".join(no_hist) + ". The earlier run can't be compared until "
                    "its plate is saved.")
        dec_df = pd.DataFrame([{
            "sample_id": sid, "runs": rec.runs_summary(groups[sid]),
            "suggested": sugg[sid]["result_status"] + (
                f" (run {', '.join(map(str, sugg[sid]['use']))})" if sugg[sid]["use"] else ""),
            "why": sugg[sid]["note"], "decision": "Suggested", "use runs": "",
            "initials": "", "justification": ""} for sid in attention],
            columns=["sample_id", "runs", "suggested", "why", "decision", "use runs",
                     "initials", "justification"])
        if len(dec_df):
            dec_df = st.data_editor(
                dec_df, hide_index=True, width="stretch", key=f"dec_{fid}",
                disabled=["sample_id", "runs", "suggested", "why"],
                column_config={
                    "runs": st.column_config.TextColumn(
                        "runs (# : plate ×dilution status conc×dil)", width="large"),
                    "decision": st.column_config.SelectboxColumn(
                        options=["Suggested", "Use run(s)", "No result"], required=True),
                    "use runs": st.column_config.TextColumn(help="e.g. 2, or 1,2 to average")})
        else:
            st.success("Every sample passed on its only run.")
        dec = dec_df.set_index("sample_id").to_dict("index")
        rows = []
        for sid, g in groups.items():
            d = dec.get(sid, {})
            row, err = rec.resolve(g, d.get("decision", "Suggested"), d.get("use runs", ""),
                                   d.get("initials", ""), d.get("justification", ""),
                                   collection)
            rows.append(row)
            errors += err
        results = rec.tidy(pd.DataFrame(rows), rec.RESULT_COLS)
        for e in errors:
            st.error(e)
        st.markdown("#### Hormone results")
        st.caption("final_conc_pg_g = conc × dilution (pg/mL) × extraction volume (mL) ÷ "
                   "dry weight (g), both from the collection file.")
        st.dataframe(results[["sample_id", "result_status", "basis", "plates_used",
                              "conc_x_dil", "extraction_vol_ml", "dry_weight_g",
                              "final_conc_pg_g", "initials", "justification", "notes"]]
                     .style.apply(hl, axis=1).format(precision=2),
                     hide_index=True, width="stretch")
        used = set("; ".join(results["run_ids_used"]).split("; "))
        assays["used_in_result_yn"] = assays["run_id"].isin(used)

# =========================== Levey-Jennings ===========================
with tab_lj:
    hc = db["controls"]
    allc = pd.concat([hc[hc["plate_id"] != plate_id], ctrl_rec], ignore_index=True)
    allc = allc[(allc["hormone"] == hormone) & (allc["control_id"] != "")]
    lot_opts = sorted(allc["control_id"].unique())
    if not lot_opts:
        st.info("Enter control IDs (lots) on the Controls tab to see Levey-Jennings plots.")
    else:
        default = [l for l in ctrl_rec["control_id"] if l in lot_opts]
        show = st.multiselect("Control lots", lot_opts, default=default or lot_opts[:1],
                              key=f"lots_{fid}")
        st.caption("This plate is shown as a preview until it is saved. Mean and SD lines "
                   "use kept controls only; hollow points are excluded controls, triangles "
                   "are more than 2 SD from the mean.")
        for lot in show:
            d = allc[allc["control_id"] == lot].sort_values(
                ["date_assayed", "plate_number"]).copy()
            k = d[d["keep_yn"] & d["conc_x_dil"].notna()]
            mu, sd = k["conc_x_dil"].mean(), k["conc_x_dil"].std()
            d["z"] = (d["conc_x_dil"] - mu) / sd if pd.notna(sd) and sd > 0 else np.nan
            d["beyond_2sd"] = d["z"].abs() > 2
            d["kept"] = np.where(d["keep_yn"], "kept", "excluded")
            d["this_plate"] = np.where(d["plate_id"] == plate_id, "this plate", "")
            st.markdown(f"**{lot}** · {len(k)} kept plates · mean {mu:,.1f} · "
                        + (f"inter-assay CV {sd / mu * 100:.1f}% · " if len(k) > 1 else "")
                        + f"mean intra-assay CV {k['cv_pct'].mean():.1f}%")
            x = alt.X("plate_id:N", sort=list(d["plate_id"]), title="Plate (by date assayed)")
            layers = []
            if pd.notna(sd) and len(k) > 1:
                ref = pd.DataFrame({"y": [mu + i * sd for i in (-3, -2, -1, 0, 1, 2, 3)],
                                    "lab": ["-3SD", "-2SD", "-1SD", "mean", "+1SD", "+2SD",
                                            "+3SD"]})
                ref["dash"] = np.where(ref["lab"] == "mean", "mean", "sd")
                layers.append(alt.Chart(ref).mark_rule(color=MUTED, strokeWidth=1).encode(
                    y="y:Q", strokeDash=alt.StrokeDash(
                        "dash:N", scale=alt.Scale(domain=["mean", "sd"],
                                                  range=[[1, 0], [4, 4]]), legend=None)))
                layers.append(alt.Chart(ref).mark_text(
                    align="left", baseline="bottom", dx=4, dy=-2, color=MUTED,
                    fontSize=10).encode(y="y:Q", text="lab:N", x=alt.value(0)))
            base = alt.Chart(d).encode(x=x, y=alt.Y(
                "conc_x_dil:Q", title="Conc × dilution", scale=alt.Scale(zero=False),
                axis=alt.Axis(grid=False)))
            layers.append(base.transform_filter("datum.keep_yn")
                          .mark_line(color=SERIES, strokeWidth=2))
            layers.append(base.mark_point(size=90, strokeWidth=2, stroke=SERIES,
                                          opacity=1).encode(
                fill=alt.condition("datum.keep_yn", alt.value(SERIES),
                                   alt.value("transparent")),
                shape=alt.condition("datum.beyond_2sd", alt.value("triangle-up"),
                                    alt.value("circle")),
                tooltip=["plate_id", "date_assayed",
                         alt.Tooltip("conc_x_dil", format=",.1f", title="conc × dil"),
                         alt.Tooltip("cv_pct", format=".1f"), "kept",
                         alt.Tooltip("z", format=".2f", title="SD from mean")]))
            layers.append(base.mark_text(dy=-12, fontSize=10).encode(text="this_plate:N"))
            st.altair_chart(alt.layer(*layers).properties(height=260), width="stretch")
            with st.expander(f"Table: {lot}"):
                st.dataframe(d[["plate_id", "date_assayed", "control_level", "conc_x_dil",
                                "cv_pct", "kept", "z"]], hide_index=True, width="stretch")
        st.markdown("#### Lot summary (kept controls)")
        st.dataframe(rec.control_summary(allc).style.format(precision=2),
                     hide_index=True, width="stretch")

# =========================== curve ===========================
with tab_curve:
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
plate_out = rec.plate_row(plate, plate_id, hormone, plate_number, date_assayed,
                          curve_ok, rerun_reasons)
with st.sidebar:
    st.divider()
    st.header("Export")
    problems = []
    if not date_assayed:
        problems.append("date assayed")
    if (ctrl_rec["keep_yn"] & (ctrl_rec["control_id"] == "")).any():
        problems.append("control IDs")
    if problems:
        st.warning("Still needed: " + ", ".join(problems))
    dups = duplicate_runs(samp_out)
    if dups:
        st.warning("Same sample ID more than once at the same dilution: " +
                   ", ".join(dups) + ". Likely a typo or a missing dilution; check "
                   "before exporting.")
    if errors:
        st.error(f"{len(errors)} problem(s) on the Reruns & results tab.")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        plate_out.to_excel(xw, sheet_name="Plate", index=False)
        assays.to_excel(xw, sheet_name="Samples", index=False)
        ctrl_rec.to_excel(xw, sheet_name="Controls", index=False)
        results.to_excel(xw, sheet_name="Hormone results", index=False)
    st.download_button("Download this plate (.xlsx)", buf.getvalue(),
                       f"{plate_id}_results.xlsx", width="stretch")
    blocked = bool(problems or errors)
    if st.button("Save plate to records", type="primary", width="stretch",
                 disabled=blocked, key=f"save_{fid}",
                 help="Fix the warnings above first." if blocked else
                 f"Writes to {REC_DIR}"):
        rec.save_plate(plate_out, assays, ctrl_rec, results, REC_DIR)
        st.success(f"Saved {plate_id}: {len(assays)} sample runs, {len(ctrl_rec)} "
                   f"controls, {len(results)} hormone results.")
    st.caption("conc_1 / conc_2 / conc_avg are raw MyAssays values; conc_x_dil is "
               "multiplied by dilution.")

# Database hook (fill in when SQL Server is ready): replace rec.load_all() and
# rec.save_plate() with queries; nothing else needs to change.
