"""Issues: samples that still need a decision, straight from the records."""
import numpy as np
import pandas as pd
import streamlit as st
import records as rec
from myassays_parser import BINDING_RANGE
from common import REC_DIR, load_db, load_collection, sidebar_data_info

st.title("🚩 Issues")
db = load_db()
collection, coll_file, coll_err = load_collection()
sidebar_data_info(coll_file, coll_err)
res, assays = db["hormone_results"], db["assays"]
if res.empty:
    st.info("Nothing has been saved yet. Process a plate and save it first.")
    st.stop()

ss = st.session_state
ss.setdefault("table_ver", 0)        # bump to clear the table selection after an action
ss.setdefault("initials", "")


def done(msg):
    ss["table_ver"] += 1
    st.toast(msg, icon="✅")
    st.rerun()


hormones = sorted(res["hormone"].unique())
with st.sidebar:
    hormone = st.selectbox("Hormone", ["All"] + hormones)
    ss["initials"] = st.text_input("Your initials", ss["initials"],
                                   help="Recorded with every decision you save here.")
pick = (lambda d: d) if hormone == "All" else (lambda d: d[d["hormone"] == hormone])

iss = pick(rec.issues(res))
miss = pick(rec.missing_pg_g(res))
manual = pick(res[res["decision"] == "manual"])

# ---------------------------------------------------------------- overview
order = ["Every run failed", "Binding outside window", "Waiting for rerun"]
counts = iss["issue"].value_counts()
cols = st.columns(5)
for col, name, helptext in zip(cols, order, [
        "Two or more runs and none passed. Pick the reads to average.",
        f"One run, CV fine, binding outside {BINDING_RANGE[0]:g}-{BINDING_RANGE[1]:g}%. "
        "Accept it or rerun.",
        "One run that failed (CV or replicate). Rerun, or pick reads."]):
    col.metric(name, int(counts.get(name, 0)), help=helptext)
cols[3].metric("Missing pg/g", len(miss), help="Value reported but the sample has no dry "
               "weight in the collection file yet.")
cols[4].metric("Decided here", len(manual))

views = [v for v in order if counts.get(v, 0)] + ["Missing pg/g", "Decided here"]
view = st.segmented_control("Show", views, default=views[0], key="view") or views[0]

# ---------------------------------------------------------------- helpers
plates_of = (assays.sort_values(["date_assayed", "plate_number"])
             .groupby(["hormone", "sample_id"])["plate_id"].agg(", ".join))
last_date = assays.groupby(["hormone", "sample_id"])["date_assayed"].max()


def with_runs(d: pd.DataFrame) -> pd.DataFrame:
    k = list(zip(d["hormone"], d["sample_id"]))
    return d.assign(plates=[plates_of.get(x, "") for x in k],
                    last_run=[last_date.get(x, "") for x in k])


def rerun_hint(r) -> str:
    b = r["binding_pct"]
    if pd.isna(b):
        return ""
    if b < BINDING_RANGE[0]:
        return f"binding {b:.0f}%: too concentrated, rerun more dilute than ×{r['dilution']:g}"
    if b > BINDING_RANGE[1]:
        return f"binding {b:.0f}%: too dilute, rerun less dilute than ×{r['dilution']:g}"
    return ""


def detail(sid: str, h: str):
    """Pick-the-reads panel for one sample."""
    runs_s = rec.runs_of(assays, sid, h)
    if runs_s.empty:
        st.warning("No runs of this sample are in the records.")
        return
    reads = rec.sample_reads(runs_s)
    current = res[(res["sample_id"] == sid) & (res["hormone"] == h)]
    status = current["result_status"].iloc[0] if len(current) else ""
    st.markdown(f"### Sample {sid} · {h} · {len(runs_s)} run(s) · {status}")

    st.dataframe(pd.DataFrame({
        "Run": runs_s["run_number"].astype(int), "Plate": runs_s["plate_id"],
        "Date": runs_s["date_assayed"], "Dilution": runs_s["dilution"],
        "Binding %": runs_s["binding_pct"], "CV %": runs_s["cv_pct"],
        "Result": runs_s["status"], "Why": runs_s["flag"],
        "Hint": runs_s.apply(rerun_hint, axis=1)}),
        hide_index=True, width="stretch",
        column_config={"Binding %": st.column_config.NumberColumn(format="%.1f"),
                       "CV %": st.column_config.NumberColumn(format="%.1f"),
                       "Dilution": st.column_config.NumberColumn(format="×%g")})

    suggested = rec.closest_pair(reads)
    st.markdown("**Pick the reads to average.** Every duplicate read from every run is "
                "listed with conc × dilution. The two closest reads are ticked to start.")
    ed = st.data_editor(
        pd.DataFrame({"Use": reads.index.isin(suggested), "Run": reads["run"],
                      "Plate": reads["plate"], "Read": reads["read"],
                      "Well": reads["well"], "Dilution": reads["dilution"],
                      "Conc": reads["conc"], "Conc × dil": reads["conc_x_dil"],
                      "Run result": reads["run_status"]}, index=reads.index),
        hide_index=True, width="stretch", key=f"reads_{h}_{sid}",
        disabled=["Run", "Plate", "Read", "Well", "Dilution", "Conc", "Conc × dil",
                  "Run result"],
        column_config={"Dilution": st.column_config.NumberColumn(format="×%g"),
                       "Conc": st.column_config.NumberColumn(format="%.4g"),
                       "Conc × dil": st.column_config.NumberColumn(format="%.1f")})
    chosen = list(ed.index[ed["Use"].fillna(False)])
    vals = reads.loc[chosen, "conc_x_dil"]

    m = st.columns(4)
    m[0].metric("Reads ticked", len(chosen))
    m[1].metric("Mean conc × dil", f"{vals.mean():,.1f}" if vals.notna().any() else "–")
    m[2].metric("CV of ticked reads", f"{rec.cv(vals.dropna()):.1f}%"
                if vals.notna().sum() > 1 else "–")
    _, _, pg_g, pnotes = rec._pg_g(sid, vals.mean() if len(vals) else np.nan, collection)
    m[3].metric("pg/g", f"{pg_g:,.1f}" if pd.notna(pg_g) else "–",
                help="; ".join(pnotes) or None)

    sel = reads.loc[chosen]
    needs_why = ("FAIL" in set(sel["run_status"])) or sel["run"].nunique() > 1
    why = st.text_input("Justification" + (" (required)" if needs_why else " (optional)"),
                        key=f"why_{h}_{sid}",
                        placeholder="e.g. closest duplicates across both runs agree within 5%")
    b = st.columns([2, 1, 1, 2])

    def save(outcome):
        row, errs = rec.manual_result(runs_s, reads, chosen, outcome, ss["initials"], why,
                                      collection)
        if errs:
            for e in errs:
                st.error(e)
            return
        rec.save_results([row], REC_DIR)
        done(f"{sid}: saved as {row['result_status']}")

    if b[0].button("Save result from ticked reads", type="primary", key=f"s_{h}_{sid}"):
        save("reads")
    if b[1].button("No result", key=f"n_{h}_{sid}"):
        save("no result")
    if b[2].button("Not a study sample", key=f"x_{h}_{sid}"):
        save("not a study sample")
    b[3].caption("To rerun it instead, leave it here: it stays on this list until the "
                 "rerun plate is saved.")


# ---------------------------------------------------------------- main list
if view in order:
    d = with_runs(iss[iss["issue"] == view]).sort_values("last_run", ascending=False)
    st.caption("Click a row to work on that sample. Tick several rows for bulk actions "
               "(for example clearing test wells that aren't study samples).")
    table = pd.DataFrame({"Sample": d["sample_id"], "Hormone": d["hormone"],
                          "Runs": d["n_runs"].astype("Int64"), "Plates": d["plates"],
                          "Last run": d["last_run"], "Why": d["notes"]})
    ev = st.dataframe(table, hide_index=True, width="stretch", on_select="rerun",
                      selection_mode="multi-row", key=f"tbl_{view}_{ss['table_ver']}",
                      height=min(36 * (len(table) + 1) + 3, 420))
    rows = ev.selection.rows if ev else []
    if len(rows) == 1:
        r = d.iloc[rows[0]]
        detail(r["sample_id"], r["hormone"])
    elif len(rows) > 1:
        picked = d.iloc[rows]
        st.markdown(f"### {len(picked)} samples selected")
        st.write(", ".join(picked["sample_id"]))
        all_review = (picked["result_status"] == "REVIEW").all()
        b = st.columns(3)
        if not ss["initials"].strip():
            st.warning("Enter your initials in the sidebar first.")
        else:
            out = None
            if b[0].button("Not study samples", key="bulk_x"):
                out = "not a study sample"
            if b[1].button("No result", key="bulk_n"):
                out = "no result"
            if b[2].button("Accept single-run reviews as they are", key="bulk_a",
                           disabled=not all_review,
                           help="Only when every selected sample is a single run with "
                           "binding outside the window."):
                out = "reads"
            if out:
                rows_out = []
                for _, p in picked.iterrows():
                    runs_s = rec.runs_of(assays, p["sample_id"], p["hormone"])
                    reads = rec.sample_reads(runs_s)
                    chosen = list(reads.index[reads["conc_x_dil"].notna()]) if out == "reads" else []
                    row, errs = rec.manual_result(runs_s, reads, chosen, out, ss["initials"],
                                                  "", collection)
                    if errs:
                        st.error(f"{p['sample_id']}: " + " ".join(errs))
                        break
                    rows_out.append(row)
                else:
                    rec.save_results(rows_out, REC_DIR)
                    done(f"Saved {len(rows_out)} decisions")

elif view == "Missing pg/g":
    st.caption("These samples have a value but no pg/g, because they aren't in the "
               "collection file yet or have no dry weight. When a newer "
               "fecal_hormone_collection file is in the data folder, recalculate.")
    st.dataframe(with_runs(miss)[["sample_id", "hormone", "result_status", "conc_x_dil",
                                  "plates", "notes"]],
                 hide_index=True, width="stretch")
    if collection is not None and st.button(f"Recalculate pg/g from {coll_file.name}",
                                            type="primary"):
        n = rec.refresh_pg_g(REC_DIR, collection)
        done(f"Recalculated; {n} result(s) gained a pg/g value")

else:  # Decided here
    st.caption("Decisions made on this page. Select one to reopen it (it goes back to "
               "the automatic result and onto the issue list).")
    d = manual.sort_values("decided_date", ascending=False)
    ev = st.dataframe(d[["sample_id", "hormone", "result_status", "basis", "conc_x_dil",
                         "final_conc_pg_g", "initials", "justification", "decided_date",
                         "notes"]], hide_index=True, width="stretch", on_select="rerun",
                      selection_mode="single-row", key=f"tbl_manual_{ss['table_ver']}")
    rows = ev.selection.rows if ev else []
    if rows:
        r = d.iloc[rows[0]]
        if st.button(f"Reopen {r['sample_id']} ({r['hormone']})"):
            runs_s = rec.runs_of(assays, r["sample_id"], r["hormone"])
            rec.save_results([rec.auto_result(runs_s, collection)], REC_DIR)
            done(f"{r['sample_id']} reopened")
