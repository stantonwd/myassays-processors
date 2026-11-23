"""Records store, sample reruns, final pg/g and control-lot statistics.

Until the SQL database exists, records are four CSV files in DATA_DIR/records:
  plates.csv           one row per plate                        key: plate_id
  assays.csv           one row per sample per run (well pair)   key: run_id = plate_id_wells
  controls.csv         one row per control per plate            key: control_plate_id
  hormone_results.csv  one reported value per sample x hormone  key: sample_id + hormone

Links: assays/controls.plate_id -> plates; hormone_results.run_ids_used -> assays;
hormone_results.sample_id -> sample collection (dry weight, extraction volume).

The data folder lives OUTSIDE the code repository (real data is never committed).
Set it in .streamlit/secrets.toml (data_dir = "C:/path/to/folder"), or with the
MYASSAYS_DATA_DIR environment variable; otherwise ~/myassays_data is used. The
fecal_hormone_collection_*.csv file goes in the same folder.
"""
from __future__ import annotations
import datetime as dt
import os
import re
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
                "flag", "keep_yn"]
RESULT_COLS = ["sample_id", "hormone", "result_status", "basis", "run_ids_used",
               "plates_used", "n_runs", "conc_x_dil", "extraction_vol_ml",
               "dry_weight_g", "final_conc_pg_g", "avg_of_fails_yn", "initials",
               "justification", "decided_date", "notes"]
TABLES = {"plates": PLATE_COLS, "assays": ASSAY_COLS,
          "controls": CONTROL_COLS, "hormone_results": RESULT_COLS}
NUMERIC = {"plate_number", "r2", "mse", "fit_a", "fit_b", "fit_c", "fit_d",
           "binding_pct", "conc_1", "conc_2", "conc_avg", "cv_pct", "dilution",
           "conc_x_dil", "n_valid", "run_number", "n_runs", "extraction_vol_ml",
           "dry_weight_g", "final_conc_pg_g"}
BOOL = {"curve_ok", "binding_ok", "rerun_yn", "used_in_result_yn", "keep_yn",
        "avg_of_fails_yn"}


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
    return tidy(pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""]),
                TABLES[name])


def load_all(folder: Path) -> dict:
    return {n: load(n, folder) for n in TABLES}


def _write(df: pd.DataFrame, name: str, folder: Path):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.csv"
    tmp = path.with_suffix(".tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)          # never leaves a half-written file


def save_plate(plate_row, assays, controls, results, folder: Path):
    """Upsert one plate. Re-saving a plate_id replaces its rows; results are
    replaced per sample x hormone; used_in_result_yn is refreshed for every run
    (on any plate) of the affected samples."""
    pid = plate_row["plate_id"].iloc[0]
    db = load_all(folder)
    plates = pd.concat([db["plates"][db["plates"]["plate_id"] != pid], plate_row])
    a = pd.concat([db["assays"][db["assays"]["plate_id"] != pid], assays])
    c = pd.concat([db["controls"][db["controls"]["plate_id"] != pid], controls])
    key = lambda d: d["sample_id"] + "|" + d["hormone"]
    r_old = db["hormone_results"]
    r = pd.concat([r_old[~key(r_old).isin(key(results))], results])
    used = {(s, h): set(filter(None, ids.split("; ")))
            for s, h, ids in zip(r["sample_id"], r["hormone"], r["run_ids_used"])}
    a["used_in_result_yn"] = [rid in used.get((s, h), set())
                              for rid, s, h in zip(a["run_id"], a["sample_id"], a["hormone"])]
    for name, df in [("plates", plates), ("assays", a), ("controls", c),
                     ("hormone_results", r)]:
        _write(tidy(df, TABLES[name]), name, folder)


# ---------------------------------------------------------------- sample collection
def find_collection_file(*folders) -> Path | None:
    """Newest fecal_hormone_collection_* file (or a folder of that name holding
    a .csv/.xlsx) in the given folders. The date suffix sorts, so the last
    name is the newest."""
    hits = sorted(h for f in folders if f and Path(f).exists()
                  for h in Path(f).glob("fecal_hormone_collection_*"))
    hits.sort(key=lambda h: h.name)
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


# ---------------------------------------------------------------- reruns
def prior_samples(history: pd.DataFrame, hormone: str, plate_id: str) -> set:
    h = history[(history["hormone"] == hormone) & (history["plate_id"] != plate_id)]
    return set(h["sample_id"])


def all_runs(current: pd.DataFrame, history: pd.DataFrame) -> pd.DataFrame:
    """Every recorded run (same hormone) of every sample on the current plate,
    in run order (date assayed, then plate number), with run_number filled in.
    The current plate's saved rows are replaced by the live ones."""
    pid, hormone = current["plate_id"].iloc[0], current["hormone"].iloc[0]
    h = history[(history["hormone"] == hormone) & (history["plate_id"] != pid)
                & history["sample_id"].isin(current["sample_id"])]
    runs = pd.concat([h, current], ignore_index=True)
    runs["_cur"] = runs["plate_id"] == pid
    runs = runs.sort_values(["sample_id", "date_assayed", "plate_number", "wells"],
                            kind="stable")
    runs["run_number"] = runs.groupby("sample_id").cumcount() + 1
    return runs.reset_index(drop=True)


def usable(run) -> bool:
    """A run can give a number if both replicates read on the curve."""
    return run["n_valid"] == 2 and not run["out_of_range"]


def suggest_result(runs_s: pd.DataFrame) -> dict:
    """Default decision for one sample, given all of its runs (in order).
    A single run outside binding is REVIEW; when every run is outside binding
    (or fails for any reason) the sample is FAIL (all runs)."""
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


def resolve(runs_s: pd.DataFrame, decision: str, runs_text: str, initials: str,
            justification: str, collection: pd.DataFrame | None) -> tuple[dict, list]:
    """Suggestion plus any override -> one hormone_results row. Returns
    (row, errors); errors block saving."""
    sug = suggest_result(runs_s)
    sid, hormone = runs_s["sample_id"].iloc[0], runs_s["hormone"].iloc[0]
    errors, n = [], len(runs_s)
    if decision == "Use run(s)":
        use = [int(x) for x in re.findall(r"\d+", str(runs_text or ""))]
        if not use:
            errors.append(f"{sid}: 'Use run(s)' chosen but no run numbers given")
    elif decision == "No result":
        use = []
    else:
        use = sug["use"]

    by_num = {int(r["run_number"]): r for _, r in runs_s.iterrows()}
    chosen = []
    for k in use:
        if k not in by_num:
            errors.append(f"{sid}: there is no run {k} (runs 1-{n})")
        elif not usable(by_num[k]):
            errors.append(f"{sid}: run {k} has an off-curve / missing replicate and can't be used")
        else:
            chosen.append(by_num[k])

    signoff = bool(chosen) and n >= 2 and all(r["status"] != "OK" for r in chosen)
    avg_fail = signoff and len(chosen) > 1
    if signoff and (not str(initials or "").strip() or not str(justification or "").strip()):
        errors.append(f"{sid}: reporting from failed runs needs initials and a justification")

    if decision == "No result":
        status = "NO RESULT"
    elif not chosen:
        status = sug["result_status"] if decision == "Suggested" else "NO RESULT"
    elif avg_fail:
        status = "AVG OF FAILS"
    elif signoff:
        status = "FAILED RUN ACCEPTED"
    else:
        status = "OK" if all(r["status"] == "OK" for r in chosen) else "REVIEW"

    nums = [int(r["run_number"]) for r in chosen]
    basis = ("" if not chosen else f"run {nums[0]} of {n}" if len(chosen) == 1
             else "average of runs " + "+".join(map(str, nums)) + f" of {n}")
    vals = np.array([r["conc_x_dil"] for r in chosen], dtype=float)
    value = float(vals.mean()) if chosen else np.nan

    notes = [sug["note"]] if sug["note"] else []
    if len(chosen) > 1:
        notes.append(f"between-run CV {vals.std(ddof=1) / vals.mean() * 100:.1f}%")
    vol = wt = np.nan
    if collection is None or sid not in collection.index:
        notes.append("not in collection file")
    else:
        vol, wt = collection.loc[sid, ["extraction_vol_ml", "dry_weight_g"]]
        if pd.isna(wt) or wt <= 0:
            notes.append("no dry weight")
        if pd.isna(vol) or vol <= 0:
            notes.append("no extraction volume")
    pg_g = value * vol / wt if chosen and pd.notna(vol) and pd.notna(wt) and wt > 0 else np.nan

    row = dict(sample_id=sid, hormone=hormone, result_status=status, basis=basis,
               run_ids_used="; ".join(r["run_id"] for r in chosen),
               plates_used="; ".join(r["plate_id"] for r in chosen), n_runs=n,
               conc_x_dil=value, extraction_vol_ml=vol, dry_weight_g=wt,
               final_conc_pg_g=pg_g, avg_of_fails_yn=avg_fail,
               initials=str(initials or "").strip() if signoff else "",
               justification=str(justification or "").strip() if signoff else "",
               decided_date=dt.date.today().isoformat(), notes="; ".join(notes))
    return row, errors


def runs_summary(runs_s: pd.DataFrame) -> str:
    parts = []
    for _, r in runs_s.iterrows():
        v = r["conc_x_dil"]
        parts.append(f"{int(r['run_number'])}: {r['plate_id']} x{r['dilution']:g} "
                     f"{r['status']} {'' if pd.isna(v) else f'{v:,.0f}'}".strip())
    return " | ".join(parts)


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
