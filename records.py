"""Records store, sample reruns, final pg/g and control-lot statistics.

Until the SQL database exists, records are four CSV files in DATA_DIR/records:
  plates.csv           one row per plate                        key: plate_id
  assays.csv           one row per sample per run (well pair)   key: run_id = plate_id_wells
  controls.csv         one row per control per plate            key: control_plate_id
  hormone_results.csv  one reported value per sample x hormone  key: sample_id + hormone

Links: assays/controls.plate_id -> plates; hormone_results.run_ids_used -> assays;
hormone_results.sample_id -> sample collection (dry weight, extraction volume).

hormone_results.decision is "auto" (worked out when a plate is saved) or "manual"
(chosen on the Issues page). Re-saving a plate never overwrites a manual decision
unless that plate brings a new run of the sample.

The data folder lives OUTSIDE the code repository (real data is never committed).
Set it in .streamlit/secrets.toml (data_dir = "C:/path/to/folder"), or with the
MYASSAYS_DATA_DIR environment variable; otherwise ~/myassays_data is used. The
fecal_hormone_collection_*.csv file goes in the same folder.
"""
from __future__ import annotations
import datetime as dt
import itertools
import os
from pathlib import Path
import numpy as np
import pandas as pd

PLATE_COLS = ["plate_id", "hormone", "plate_number", "plate_name", "date_assayed",
              "r2", "mse", "fit_a", "fit_b", "fit_c", "fit_d", "curve_ok",
              "rerun_reasons", "notes", "processed_date"]
ASSAY_COLS = ["run_id", "sample_id", "hormone", "plate_id", "plate_number",
              "date_assayed", "wells", "binding_pct", "conc_1", "conc_2", "conc_avg",
              "cv_pct", "dilution", "conc_x_dil", "n_valid", "out_of_range", "status",
              "binding_ok", "flag", "rerun_yn", "run_number", "used_in_result_yn"]
CONTROL_COLS = ["control_plate_id", "control_id", "control_level", "plate_id", "hormone",
                "plate_number", "date_assayed", "wells", "binding_pct", "conc_1",
                "conc_2", "conc_avg", "cv_pct", "dilution", "conc_x_dil", "status",
                "flag", "keep_yn", "keep_note"]
RESULT_COLS = ["sample_id", "hormone", "result_status", "decision", "basis",
               "run_ids_used", "reads_used", "plates_used", "n_runs", "conc_x_dil",
               "extraction_vol_ml", "dry_weight_g", "final_conc_pg_g", "avg_of_fails_yn",
               "initials", "justification", "decided_date", "notes"]
TABLES = {"plates": PLATE_COLS, "assays": ASSAY_COLS,
          "controls": CONTROL_COLS, "hormone_results": RESULT_COLS}
NUMERIC = {"plate_number", "r2", "mse", "fit_a", "fit_b", "fit_c", "fit_d",
           "binding_pct", "conc_1", "conc_2", "conc_avg", "cv_pct", "dilution",
           "conc_x_dil", "n_valid", "run_number", "n_runs", "extraction_vol_ml",
           "dry_weight_g", "final_conc_pg_g"}
BOOL = {"curve_ok", "binding_ok", "rerun_yn", "used_in_result_yn", "keep_yn",
        "avg_of_fails_yn"}

# Issue categories shown on the Issues page (auto results only)
ISSUE_OF = {"FAIL (all runs)": "Every run failed", "REVIEW": "Binding outside window",
            "PENDING RERUN": "Waiting for rerun"}
# statuses that mean "no value is reported, on purpose"
CLOSED_EMPTY = {"NO RESULT", "NOT A STUDY SAMPLE"}
PGG_NOTES = ("not in collection file", "no dry weight", "no extraction volume")


# ---------------------------------------------------------------- where data lives
def data_dir(configured: str | None = None) -> Path:
    p = configured or os.environ.get("MYASSAYS_DATA_DIR") or Path.home() / "myassays_data"
    return Path(p).expanduser()


def records_dir(base: Path) -> Path:
    return Path(base) / "records"


# ---------------------------------------------------------------- store I/O
def _to_bool(v) -> bool:
    """True/False from bools, 1/0, 1.0/0.0 (what pandas makes of a bool column
    concatenated with an empty table) or text like 'True'/'yes'."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return False
    if isinstance(v, (bool, np.bool_, int, float, np.integer, np.floating)):
        return bool(v)
    return str(v).strip().lower() in {"true", "1", "1.0", "yes", "y"}


def tidy(df: pd.DataFrame, cols) -> pd.DataFrame:
    df = df.reindex(columns=cols)
    for c in cols:
        if c in NUMERIC:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        elif c in BOOL:
            df[c] = df[c].map(_to_bool).astype(bool)
        else:
            df[c] = df[c].astype(object).where(df[c].notna(), "").astype(str)
    return df


def load(name: str, folder: Path) -> pd.DataFrame:
    path = Path(folder) / f"{name}.csv"
    if not path.exists():
        return tidy(pd.DataFrame(), TABLES[name])
    df = tidy(pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""]),
              TABLES[name])
    if name == "hormone_results":   # files saved before 'decision' existed
        df.loc[df["decision"] == "", "decision"] = "auto"
    return df


def load_all(folder: Path) -> dict:
    return {n: load(n, folder) for n in TABLES}


def _write(df: pd.DataFrame, name: str, folder: Path):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.csv"
    tmp = path.with_suffix(".tmp")
    tidy(df, TABLES[name]).to_csv(tmp, index=False)
    os.replace(tmp, path)          # never leaves a half-written file


def _key(d):
    return d["sample_id"] + "|" + d["hormone"]


def _refresh_used(a: pd.DataFrame, r: pd.DataFrame) -> pd.DataFrame:
    used = {(s, h): set(filter(None, ids.split("; ")))
            for s, h, ids in zip(r["sample_id"], r["hormone"], r["run_ids_used"])}
    a = a.copy()
    a["used_in_result_yn"] = [rid in used.get((s, h), set())
                              for rid, s, h in zip(a["run_id"], a["sample_id"], a["hormone"])]
    return a


def save_plate(plate_row, assays, controls, results, folder: Path) -> int:
    """Upsert one plate. Re-saving a plate_id replaces its rows. Results are
    replaced per sample x hormone, EXCEPT manual decisions whose sample has no
    new run (same number of runs as when the decision was made).
    Returns how many manual decisions were kept."""
    pid = plate_row["plate_id"].iloc[0]
    db = load_all(folder)
    plates = pd.concat([db["plates"][db["plates"]["plate_id"] != pid], plate_row])
    a = pd.concat([db["assays"][db["assays"]["plate_id"] != pid], assays])
    c = pd.concat([db["controls"][db["controls"]["plate_id"] != pid], controls])

    r_old = db["hormone_results"]
    manual = r_old[r_old["decision"] == "manual"].set_index(_key(r_old[r_old["decision"] == "manual"]))
    keep_manual = [k for k, n in zip(_key(results), results["n_runs"])
                   if k in manual.index and manual.at[k, "n_runs"] == n]
    new = results[~_key(results).isin(keep_manual)]
    r = pd.concat([r_old[~_key(r_old).isin(_key(new))], new])
    for name, df in [("plates", plates), ("assays", _refresh_used(a, r)),
                     ("controls", c), ("hormone_results", r)]:
        _write(df, name, folder)
    return len(keep_manual)


def save_results(rows: list[dict], folder: Path):
    """Upsert hormone_results rows (Issues page decisions)."""
    db = load_all(folder)
    new = tidy(pd.DataFrame(rows), RESULT_COLS)
    r_old = db["hormone_results"]
    r = pd.concat([r_old[~_key(r_old).isin(_key(new))], new])
    _write(r, "hormone_results", folder)
    _write(_refresh_used(db["assays"], r), "assays", folder)


def set_control_keep(folder: Path, control_plate_ids, keep: bool, note: str = ""):
    """Accept (keep=True) or reject controls for the lot CVs."""
    c = load("controls", folder)
    m = c["control_plate_id"].isin(list(control_plate_ids))
    c.loc[m, "keep_yn"] = keep
    if note:
        c.loc[m, "keep_note"] = note
    _write(c, "controls", folder)


def refresh_pg_g(folder: Path, collection: pd.DataFrame) -> int:
    """Recalculate final_conc_pg_g for every result from the (newer) collection
    file. Returns how many results gained a pg/g value."""
    r = load("hormone_results", folder)
    before = r["final_conc_pg_g"].notna().sum()
    for i, row in r.iterrows():
        vol, wt, pg_g, notes = _pg_g(row["sample_id"], row["conc_x_dil"], collection)
        keep = [n for n in row["notes"].split("; ") if n and n not in PGG_NOTES]
        r.loc[i, ["extraction_vol_ml", "dry_weight_g", "final_conc_pg_g"]] = [vol, wt, pg_g]
        r.loc[i, "notes"] = "; ".join(keep + (notes if pd.notna(row["conc_x_dil"]) else []))
    _write(r, "hormone_results", folder)
    return int(r["final_conc_pg_g"].notna().sum() - before)


# ---------------------------------------------------------------- sample collection
def find_collection_file(*folders) -> Path | None:
    """Newest fecal_hormone_collection_* file (or a folder of that name holding
    a .csv/.xlsx) in the given folders. The date suffix sorts, so the last
    name is the newest."""
    hits = sorted((h for f in folders if f and Path(f).exists()
                   for h in Path(f).glob("fecal_hormone_collection_*")),
                  key=lambda h: h.name)
    for h in reversed(hits):
        if h.is_dir():
            inner = sorted([*h.glob("*.csv"), *h.glob("*.xlsx")])
            if inner:
                return inner[-1]
        elif h.suffix.lower() in {".csv", ".xlsx"}:
            return h
    return None


def load_collection(path) -> pd.DataFrame:
    path = Path(path)
    df = (pd.read_excel(path, dtype=str) if path.suffix.lower() == ".xlsx"
          else pd.read_csv(path, dtype=str, encoding="utf-8-sig"))
    df["sample_id"] = df["sample_id"].astype(str).str.strip()
    out = pd.DataFrame({
        "sample_id": df["sample_id"],
        "dry_weight_g": pd.to_numeric(df.get("dry_wt_g"), errors="coerce"),
        "extraction_vol_ml": pd.to_numeric(df.get("extraction_volume_mL"), errors="coerce"),
    })
    return out.drop_duplicates("sample_id", keep="last").set_index("sample_id")


def _pg_g(sid, value, collection):
    """(extraction_vol_ml, dry_weight_g, final_conc_pg_g, notes)."""
    vol = wt = np.nan
    notes = []
    if collection is None or sid not in collection.index:
        notes.append("not in collection file")
    else:
        vol, wt = collection.loc[sid, ["extraction_vol_ml", "dry_weight_g"]]
        if pd.isna(wt) or wt <= 0:
            notes.append("no dry weight")
        if pd.isna(vol) or vol <= 0:
            notes.append("no extraction volume")
    ok = pd.notna(value) and pd.notna(vol) and pd.notna(wt) and wt > 0
    return vol, wt, (value * vol / wt if ok else np.nan), notes


# ---------------------------------------------------------------- tables from a plate
def assay_table(samp_out: pd.DataFrame, samples_qc: pd.DataFrame, rerun_ticks) -> pd.DataFrame:
    """samp_out is build_sample_output(); samples_qc supplies the QC columns."""
    s = samples_qc.reset_index(drop=True)
    d = samp_out.reset_index(drop=True).copy()
    d["sample_id"] = d["sample_id"].astype(str)
    d["run_id"] = d["plate_id"] + "_" + s["wells"].str.replace(", ", "-", regex=False)
    d["wells"] = s["wells"]
    d["cv_pct"] = s["cv_pct"]
    d["conc_x_dil"] = d["conc_avg"] * d["dilution"]
    for c in ["n_valid", "out_of_range", "status", "binding_ok"]:
        d[c] = s[c]
    d["rerun_yn"] = list(rerun_ticks)
    d["run_number"] = np.nan
    d["used_in_result_yn"] = False
    d["date_assayed"] = d["date_assayed"].astype(str).replace({"None": ""})
    return tidy(d, ASSAY_COLS)


def control_table(ctrl_out: pd.DataFrame, controls_qc: pd.DataFrame, keep) -> pd.DataFrame:
    """ctrl_out is build_control_output() for ALL controls; keep_yn records
    which ones count towards the lot CVs (rows are kept either way)."""
    q = controls_qc.reset_index(drop=True)
    d = ctrl_out.reset_index(drop=True).copy()
    d["wells"] = q["wells"]
    d["cv_pct"] = q["cv_pct"]
    d["conc_x_dil"] = d["conc_avg"] * d["dilution"]
    d["status"] = q["status"]
    d["flag"] = np.where(q["status"].eq("OK"), "", q["status"] + ": " + q["flags"])
    d["keep_yn"] = list(keep)
    d["control_plate_id"] = d["control_plate_id"].fillna("")
    d["date_assayed"] = d["date_assayed"].astype(str).replace({"None": ""})
    return tidy(d, CONTROL_COLS)


def plate_row(plate, plate_id, hormone, plate_number, date_assayed, curve_ok,
              rerun_reasons) -> pd.DataFrame:
    f = plate.fit
    return tidy(pd.DataFrame([{
        "plate_id": plate_id, "hormone": hormone, "plate_number": plate_number,
        "plate_name": plate.name, "date_assayed": str(date_assayed or ""),
        "r2": f.get("R²"), "mse": f.get("MSE"), "fit_a": f.get("a"), "fit_b": f.get("b"),
        "fit_c": f.get("c"), "fit_d": f.get("d"), "curve_ok": curve_ok,
        "rerun_reasons": "; ".join(rerun_reasons), "notes": plate.notes,
        "processed_date": dt.date.today().isoformat()}]), PLATE_COLS)


# ---------------------------------------------------------------- runs
def all_runs(current: pd.DataFrame, history: pd.DataFrame) -> pd.DataFrame:
    """Every recorded run (same hormone) of every sample on the current plate,
    in run order (date assayed, then plate number), with run_number filled in.
    The current plate's saved rows are replaced by the live ones."""
    pid, hormone = current["plate_id"].iloc[0], current["hormone"].iloc[0]
    h = history[(history["hormone"] == hormone) & (history["plate_id"] != pid)
                & history["sample_id"].isin(current["sample_id"])]
    runs = pd.concat([h, current], ignore_index=True)
    runs["_cur"] = runs["plate_id"] == pid
    return number_runs(runs)


def number_runs(runs: pd.DataFrame) -> pd.DataFrame:
    runs = runs.sort_values(["hormone", "sample_id", "date_assayed", "plate_number",
                             "wells"], kind="stable").copy()
    runs["run_number"] = runs.groupby(["hormone", "sample_id"]).cumcount() + 1
    return runs.reset_index(drop=True)


def runs_of(assays: pd.DataFrame, sample_id: str, hormone: str) -> pd.DataFrame:
    a = assays[(assays["sample_id"] == sample_id) & (assays["hormone"] == hormone)]
    return number_runs(a)


def usable(run) -> bool:
    """A run can give a number if both replicates read on the curve."""
    return run["n_valid"] == 2 and not run["out_of_range"]


def suggest_result(runs_s: pd.DataFrame) -> dict:
    """Default for one sample, given all of its runs (in order). A single run
    outside binding is REVIEW; when every run is outside binding (or fails for
    any reason) the sample is FAIL (all runs)."""
    n = len(runs_s)
    ok = runs_s[runs_s["status"] == "OK"]
    if len(ok):
        r = ok.iloc[-1]
        return dict(result_status="OK", use=[int(r["run_number"])],
                    note="" if n == 1 else f"run {int(r['run_number'])} is the latest passing run")
    if n == 1:
        if runs_s["status"].iloc[0] == "REVIEW":
            return dict(result_status="REVIEW", use=[1],
                        note="binding outside window on a single run; rerun or accept")
        return dict(result_status="PENDING RERUN", use=[], note="run failed; rerun needed")
    all_binding = (runs_s["status"] == "REVIEW").all()
    return dict(result_status="FAIL (all runs)", use=[],
                note="binding outside window on every run" if all_binding
                else "no run passed QC")


def auto_result(runs_s: pd.DataFrame, collection) -> dict:
    """The result written when a plate is saved."""
    sug = suggest_result(runs_s)
    sid, hormone = runs_s["sample_id"].iloc[0], runs_s["hormone"].iloc[0]
    by_num = {int(r["run_number"]): r for _, r in runs_s.iterrows()}
    chosen = [by_num[k] for k in sug["use"] if usable(by_num[k])]
    value = float(np.mean([r["conc_x_dil"] for r in chosen])) if chosen else np.nan
    vol, wt, pg_g, pnotes = _pg_g(sid, value, collection)
    n = len(runs_s)
    basis = f"run {int(chosen[0]['run_number'])} of {n}" if chosen else ""
    return dict(sample_id=sid, hormone=hormone, result_status=sug["result_status"],
                decision="auto", basis=basis,
                run_ids_used="; ".join(r["run_id"] for r in chosen),
                reads_used="", plates_used="; ".join(r["plate_id"] for r in chosen),
                n_runs=n, conc_x_dil=value, extraction_vol_ml=vol, dry_weight_g=wt,
                final_conc_pg_g=pg_g, avg_of_fails_yn=False, initials="",
                justification="", decided_date=dt.date.today().isoformat(),
                notes="; ".join(([sug["note"]] if sug["note"] else []) + pnotes))


# ---------------------------------------------------------------- reads (Issues page)
def sample_reads(runs_s: pd.DataFrame) -> pd.DataFrame:
    """One row per individual read (duplicate well) across all runs, with
    conc x dilution. Off-curve reads ('> Curve') have no value."""
    rows = []
    for _, r in runs_s.iterrows():
        wells = r["wells"].split(", ")
        for k in (1, 2):
            conc = r[f"conc_{k}"]
            rows.append(dict(
                run=int(r["run_number"]), plate=r["plate_id"], date=r["date_assayed"],
                read=k, well=wells[k - 1] if len(wells) >= k else "",
                dilution=r["dilution"], conc=conc,
                conc_x_dil=conc * r["dilution"] if pd.notna(conc) else np.nan,
                run_status=r["status"], binding_pct=r["binding_pct"],
                run_flag=r["flag"], run_id=r["run_id"]))
    return pd.DataFrame(rows)


def closest_pair(reads: pd.DataFrame) -> list:
    """Index labels of the two reads (any runs) whose conc x dilution are
    closest in relative terms. Empty if fewer than two reads have values."""
    v = reads["conc_x_dil"].dropna()
    best, pair = np.inf, []
    for i, j in itertools.combinations(v.index, 2):
        m = (v[i] + v[j]) / 2
        if m > 0 and abs(v[i] - v[j]) / m < best:
            best, pair = abs(v[i] - v[j]) / m, [i, j]
    return pair


def cv(values) -> float:
    v = np.asarray(values, dtype=float)
    return float(v.std(ddof=1) / v.mean() * 100) if len(v) > 1 and v.mean() else np.nan


def manual_result(runs_s: pd.DataFrame, reads: pd.DataFrame, chosen_idx, outcome: str,
                  initials: str, justification: str, collection) -> tuple[dict, list]:
    """A decision from the Issues page. outcome: 'reads' (average the chosen
    reads), 'no result' or 'not a study sample'. Returns (row, errors)."""
    sid, hormone = runs_s["sample_id"].iloc[0], runs_s["hormone"].iloc[0]
    n, errors = len(runs_s), []
    initials, justification = str(initials or "").strip(), str(justification or "").strip()
    if not initials:
        errors.append("Enter your initials.")
    sel = reads.loc[list(chosen_idx)] if outcome == "reads" else reads.iloc[0:0]
    if outcome == "reads":
        if sel.empty:
            errors.append("Tick at least one read.")
        elif sel["conc_x_dil"].isna().any():
            errors.append("An off-curve read has no value and can't be used.")
        runs_used = sorted(set(sel["run"]))
        statuses = set(sel["run_status"])
        from_failed = "FAIL" in statuses or len(runs_used) > 1
        if from_failed and not justification:
            errors.append("Reads from a failed run, or from more than one run, need a "
                          "justification.")
        value = float(sel["conc_x_dil"].mean()) if len(sel) else np.nan
        if statuses == {"OK"}:
            status = "OK"
        elif len(runs_used) > 1 and "OK" not in statuses:
            status = "AVG OF FAILS"
        elif "FAIL" in statuses:
            status = "FAILED RUN ACCEPTED"
        else:
            status = "REVIEW ACCEPTED"
        labels = [f"{p} r{k}" for p, k in zip(sel["plate"], sel["read"])]
        basis = (f"mean of {len(sel)} reads: " + ", ".join(labels)) if len(sel) else ""
        avg_fail = status == "AVG OF FAILS"
    else:
        value, status, basis, avg_fail, runs_used = np.nan, outcome.upper(), "", False, []

    vol, wt, pg_g, pnotes = _pg_g(sid, value, collection)
    notes = [f"between-read CV {cv(sel['conc_x_dil']):.1f}%"] if len(sel) > 1 else []
    if outcome == "reads":
        notes += pnotes
    by_run = {int(r["run_number"]): r for _, r in runs_s.iterrows()}
    row = dict(sample_id=sid, hormone=hormone, result_status=status, decision="manual",
               basis=basis,
               run_ids_used="; ".join(by_run[k]["run_id"] for k in runs_used),
               reads_used="; ".join(f"{a}#{k}" for a, k in zip(sel["run_id"], sel["read"])),
               plates_used="; ".join(by_run[k]["plate_id"] for k in runs_used),
               n_runs=n, conc_x_dil=value, extraction_vol_ml=vol, dry_weight_g=wt,
               final_conc_pg_g=pg_g, avg_of_fails_yn=avg_fail, initials=initials,
               justification=justification, decided_date=dt.date.today().isoformat(),
               notes="; ".join(notes))
    return row, errors


def issues(results: pd.DataFrame) -> pd.DataFrame:
    """Open issues: auto results that are not OK."""
    r = results[(results["decision"] == "auto") & results["result_status"].isin(ISSUE_OF)]
    return r.assign(issue=r["result_status"].map(ISSUE_OF))


def missing_pg_g(results: pd.DataFrame) -> pd.DataFrame:
    """Results with a value but no pg/g (sample not in the collection file yet,
    or no dry weight)."""
    return results[results["conc_x_dil"].notna() & results["final_conc_pg_g"].isna()
                   & ~results["result_status"].isin(CLOSED_EMPTY)]


# ---------------------------------------------------------------- controls
def control_summary(ctrl: pd.DataFrame) -> pd.DataFrame:
    """Per hormone x control lot (control_id): inter-assay CV = SD/mean of the
    plate averages; intra-assay CV = mean of within-plate CVs. Kept only."""
    k = ctrl[ctrl["keep_yn"] & ctrl["conc_x_dil"].notna() & (ctrl["control_id"] != "")]
    cols = ["hormone", "control_id", "control_level", "n_plates", "mean", "sd",
            "inter_assay_cv_pct", "intra_assay_cv_pct"]
    if k.empty:
        return pd.DataFrame(columns=cols)
    out = k.groupby(["hormone", "control_id"]).agg(
        control_level=("control_level", lambda s: "/".join(sorted(set(s)))),
        n_plates=("plate_id", "nunique"), mean=("conc_x_dil", "mean"),
        sd=("conc_x_dil", "std"), intra_assay_cv_pct=("cv_pct", "mean")).reset_index()
    out["inter_assay_cv_pct"] = out["sd"] / out["mean"] * 100
    return out[cols]


def last_lots(ctrl_hist: pd.DataFrame, hormone: str) -> dict:
    """Most recently used control_id per level for this hormone (to pre-fill)."""
    h = ctrl_hist[(ctrl_hist["hormone"] == hormone) & (ctrl_hist["control_id"] != "")]
    h = h.sort_values(["date_assayed", "plate_number"])
    return h.groupby("control_level")["control_id"].last().to_dict()
