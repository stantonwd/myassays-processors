"""Parse a MyAssays Desktop Excel export and apply QC.

Expected export layout (the P15 export):
  A1  plate / run name, e.g. "P15 06NOV2026"      A2  free-text notes
  J:K 4PL fit parameters (a, b, c, d, MSE, R2, SS, SYX)
  "Calibrator" header row -> standards (2 wells each)
  "Sample" header row     -> every other well pair (controls, B0, NSB, samples)
Rows with a value in the Sample column start a new item; the next row (blank
Sample cell, merged in Excel) is the second replicate well.
"""
from __future__ import annotations
import datetime as _dt
import re
from dataclasses import dataclass
import numpy as np
import pandas as pd
from openpyxl import load_workbook

# ---------------------------------------------------------------------------
# Settings (change here, not in the app)
# ---------------------------------------------------------------------------
HORMONES = ["P", "E", "T", "GC"]       # code used in plate names, P15 = 15th P plate
CV_LIMIT = 15.5                         # duplicate CV must be BELOW this
BINDING_RANGE = (20.0, 80.0)            # %B/B0 window for samples (controls exempt)

# Well positions. Each control/B0/NSB is a pair of duplicate wells.
LAYOUTS = {
    "default": {   # P, T, GC
        "controls": {"hi": ("B3", "B4"), "mid": ("C3", "C4"), "low": ("D3", "D4")},
        "b0": ("A3", "A4"), "nsb": ("A1", "A2"),
    },
    "E": {
        "controls": {"hi": ("A3", "A4"), "mid": ("H1", "H2"), "low": ("B3", "B4")},
        "b0": ("G1", "G2"), "nsb": ("A1", "A2"),
    },
}
LEVEL_ORDER = ["hi", "mid", "low"]

# Curve diagnostic thresholds (suggested starting points; edit freely)
CURVE_R2_MIN = 0.99
STD_RECOVERY_RANGE = (80.0, 120.0)      # informational only


def layout_for(hormone: str) -> dict:
    return LAYOUTS["E"] if hormone == "E" else LAYOUTS["default"]


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------
def _num(x):
    if x is None:
        return np.nan
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return float(str(x).strip())
    except ValueError:
        return np.nan


def _txt(x) -> str:
    return "" if x is None else str(x).strip()


def _find_header(ws, label: str, start: int = 1) -> int:
    for r in range(start, ws.max_row + 1):
        if _txt(ws.cell(r, 2).value) == label:
            return r
    raise ValueError(f"Could not find a '{label}' header row in column B. "
                     "Is this a MyAssays export?")


@dataclass
class Plate:
    name: str
    notes: str
    fit: dict
    standards: pd.DataFrame
    rows: pd.DataFrame        # every item in the "Sample" table, unclassified


def parse_myassays(file) -> Plate:
    wb = load_workbook(file, data_only=True)
    ws = wb.active
    name, notes = _txt(ws["A1"].value), _txt(ws["A2"].value)

    fit = {}
    for r in range(1, 20):
        lab = _txt(ws.cell(r, 10).value)
        if lab in {"a", "b", "c", "d", "MSE", "R²", "SS", "SYX"}:
            fit[lab] = _num(ws.cell(r, 11).value)

    cal_hdr = _find_header(ws, "Calibrator")
    sample_hdr = _find_header(ws, "Sample", cal_hdr)

    std_rows, cur = [], None
    for r in range(cal_hdr + 1, sample_hdr):
        label, well = _txt(ws.cell(r, 2).value), _txt(ws.cell(r, 3).value)
        if not well:
            continue
        if label:
            cur = label
            nominal = _num(ws.cell(r, 4).value)
            raw_avg, sem = _num(ws.cell(r, 6).value), _num(ws.cell(r, 7).value)
        std_rows.append(dict(
            standard=cur, nominal=nominal, well=well, raw=_num(ws.cell(r, 5).value),
            raw_avg=raw_avg, sem=sem, backfit=_num(ws.cell(r, 8).value),
            recovery_pct=_num(ws.cell(r, 9).value)))
    standards = pd.DataFrame(std_rows)

    items, cur = [], None
    for r in range(sample_hdr + 1, ws.max_row + 1):
        label, well = _txt(ws.cell(r, 2).value), _txt(ws.cell(r, 3).value)
        if not label and not well:
            continue
        if label.lower().startswith("the highlighted"):
            break
        if label:
            cur = dict(sample=label, well_list=[well],
                       raw=[_num(ws.cell(r, 4).value)],
                       pct_b_b0=_num(ws.cell(r, 5).value),
                       conc=[ws.cell(r, 6).value],
                       conc_avg=_num(ws.cell(r, 7).value),
                       cv_pct=_num(ws.cell(r, 8).value),
                       sd=_num(ws.cell(r, 9).value), sem=_num(ws.cell(r, 10).value))
            items.append(cur)
        elif cur is not None:
            cur["well_list"].append(well)
            cur["raw"].append(_num(ws.cell(r, 4).value))
            cur["conc"].append(ws.cell(r, 6).value)

    rows = []
    for it in items:
        c_num = [_num(c) for c in it["conc"]]
        c_txt = [_txt(c) for c in it["conc"]]
        rows.append(dict(
            sample=it["sample"], well_list=it["well_list"],
            wells=", ".join(it["well_list"]),
            raw_1=it["raw"][0], raw_2=it["raw"][1] if len(it["raw"]) > 1 else np.nan,
            pct_b_b0=it["pct_b_b0"],
            conc_1=c_num[0], conc_2=c_num[1] if len(c_num) > 1 else np.nan,
            conc_avg=it["conc_avg"], cv_pct=it["cv_pct"], sd=it["sd"], sem=it["sem"],
            out_of_range=" / ".join(t for t, n in zip(c_txt, c_num) if np.isnan(n) and t)))
    return Plate(name, notes, fit, standards, pd.DataFrame(rows))


def parse_plate_name(name: str):
    """'P15 06NOV2026' -> ('P', 15, date(2026, 11, 6)). Any part may be None."""
    hormone = number = date = None
    name = name or ""
    m = re.search(r"(\d{1,2}[A-Za-z]{3}\d{4})", name)
    if m:
        try:
            date = _dt.datetime.strptime(m.group(1).upper(), "%d%b%Y").date()
        except ValueError:
            pass
    m = re.match(r"\s*([A-Za-z]+)\s*(\d+)", name)
    if m:
        hormone, number = m.group(1).upper(), int(m.group(2))
    return hormone, number, date


# ---------------------------------------------------------------------------
# Sorting rows into controls / samples / B0 / NSB by well position
# ---------------------------------------------------------------------------
@dataclass
class Classified:
    controls: pd.DataFrame
    samples: pd.DataFrame
    b0: pd.DataFrame
    nsb: pd.DataFrame
    missing_controls: list     # control levels expected for this hormone but not found
    stray_controls: list       # rows NAMED like controls that are not in control wells


def classify(plate: Plate, hormone: str) -> Classified:
    lay = layout_for(hormone)
    df = plate.rows.copy()
    wsets = df["well_list"].map(frozenset)

    is_b0 = df["sample"].str.upper().eq("B0") | wsets.eq(frozenset(lay["b0"]))
    is_nsb = df["sample"].str.upper().eq("NSB") | wsets.eq(frozenset(lay["nsb"]))
    level = pd.Series(pd.NA, index=df.index, dtype="object")
    for lvl, wells in lay["controls"].items():
        level[wsets.eq(frozenset(wells))] = lvl
    is_ctrl = level.notna() & ~is_b0 & ~is_nsb

    controls = df[is_ctrl].assign(control_level=level[is_ctrl])
    controls["_o"] = controls["control_level"].map(LEVEL_ORDER.index)
    controls = controls.sort_values("_o").drop(columns="_o").reset_index(drop=True)
    samples = df[~(is_ctrl | is_b0 | is_nsb)].reset_index(drop=True)
    missing = [l for l in LEVEL_ORDER if l not in set(controls["control_level"])]
    stray = samples.loc[samples["sample"].str.match(r"(?i)^control"), "sample"].tolist()
    return Classified(controls, samples, df[is_b0].reset_index(drop=True),
                      df[is_nsb].reset_index(drop=True), missing, stray)


# ---------------------------------------------------------------------------
# QC
# ---------------------------------------------------------------------------
def _qc_row(r, check_binding: bool):
    """CV / replicate problems are FAIL. Binding outside the window is REVIEW
    (a person decides). Controls are exempt from the binding rule."""
    fail, review = [], []
    n_valid = int(pd.notna(r["conc_1"])) + int(pd.notna(r["conc_2"]))
    if r["out_of_range"]:
        fail.append(f"Replicate {r['out_of_range']}")
    if n_valid == 0:
        fail.append("No valid replicates")
    elif n_valid == 1:
        fail.append("Only 1 valid replicate")
    elif r["cv_pct"] >= CV_LIMIT:
        fail.append(f"CV {r['cv_pct']:.1f}% (limit < {CV_LIMIT:g}%)")
    if check_binding and pd.notna(r["pct_b_b0"]) and \
            not (BINDING_RANGE[0] <= r["pct_b_b0"] <= BINDING_RANGE[1]):
        review.append(f"Binding {r['pct_b_b0']:.1f}% outside "
                      f"{BINDING_RANGE[0]:g}-{BINDING_RANGE[1]:g}%")
    status = "FAIL" if fail else ("REVIEW" if review else "OK")
    return pd.Series({"status": status, "flags": "; ".join(fail + review)})


def apply_qc(df: pd.DataFrame, check_binding: bool = True) -> pd.DataFrame:
    if df.empty:
        return df.assign(status=[], flags=[])
    return pd.concat([df, df.apply(_qc_row, axis=1, args=(check_binding,))], axis=1)


# ---------------------------------------------------------------------------
# Curve diagnostics
# ---------------------------------------------------------------------------
def four_pl(x, a, b, c, d):
    """MyAssays 4PL: y = d + (a - d) / (1 + (x / c) ** b)."""
    return d + (a - d) / (1 + (np.asarray(x, dtype=float) / c) ** b)


def curve_checks(plate: Plate, b0: pd.DataFrame, nsb: pd.DataFrame):
    """Returns (list of (name, passed|None, detail), overall_ok)."""
    checks = []
    r2 = plate.fit.get("R²", np.nan)
    checks.append(("Curve fit R²", bool(r2 >= CURVE_R2_MIN),
                   f"{r2:.4f} (want ≥ {CURVE_R2_MIN})"))

    std = plate.standards.drop_duplicates("standard").sort_values("nominal")
    if len(std) >= 2:
        mono = bool(np.all(np.diff(std["raw_avg"].values) <= 0))
        checks.append(("Binding falls as standard concentration rises", mono,
                       "yes" if mono else "no: curve is not monotonic"))
        lo, hi = BINDING_RANGE
        top, bottom = std["raw_avg"].max(), std["raw_avg"].min()
        span = bool(top >= hi and bottom <= lo)
        checks.append((f"Standards bracket the {lo:g}-{hi:g}% working range", span,
                       f"standards run from {bottom:.1f}% to {top:.1f}% binding"))

    rec = plate.standards["recovery_pct"].dropna()
    if len(rec):
        ok = rec.between(*STD_RECOVERY_RANGE)
        checks.append((f"Standard recovery {STD_RECOVERY_RANGE[0]:g}-{STD_RECOVERY_RANGE[1]:g}% (info only)",
                       None, f"{int(ok.sum())} of {len(rec)} wells within range"))
    if len(b0) and len(nsb):
        b0m = np.nanmean([b0["raw_1"].iloc[0], b0["raw_2"].iloc[0]])
        nsbm = np.nanmean([nsb["raw_1"].iloc[0], nsb["raw_2"].iloc[0]])
        if b0m:
            checks.append(("NSB as % of B0 (info only)", None,
                           f"{nsbm / b0m * 100:.1f}%  (B0 {b0m:.3f}, NSB {nsbm:.3f})"))
    overall = all(c[1] for c in checks if c[1] is not None)
    return checks, overall


# ---------------------------------------------------------------------------
# Output tables (the columns that go to the databases)
# conc_1 / conc_2 / conc_avg are the raw MyAssays values (pg/mL in the assay),
# NOT multiplied by dilution. Final pg/g is calculated later with dry weight.
# ---------------------------------------------------------------------------
def _flag_text(df: pd.DataFrame):
    return np.where(df["status"].eq("OK"), "", df["status"] + ": " + df["flags"])


def build_sample_output(samples_qc, dilution, hormone, plate_number, date_assayed):
    d = samples_qc.reset_index(drop=True)
    return pd.DataFrame({
        "sample_id": d["sample"], "hormone": hormone,
        "plate_id": f"{hormone}{plate_number}", "plate_number": plate_number,
        "date_assayed": date_assayed, "binding_pct": d["pct_b_b0"],
        "conc_1": d["conc_1"], "conc_2": d["conc_2"], "conc_avg": d["conc_avg"],
        "dilution": np.asarray(dilution, dtype=float), "flag": _flag_text(d)})


def make_control_plate_id(plate_id: str, control_id: str):
    cid = re.sub(r"\s+", "-", (control_id or "").strip())
    return f"{plate_id}_{cid}" if cid else None


def build_control_output(controls_qc, control_ids, dilution, hormone, plate_number, date_assayed):
    d = controls_qc.reset_index(drop=True)
    plate_id = f"{hormone}{plate_number}"
    ids = [(c or "").strip() for c in control_ids]
    return pd.DataFrame({
        "control_id": ids, "control_level": d["control_level"],
        "plate_id": plate_id,
        "control_plate_id": [make_control_plate_id(plate_id, c) for c in ids],
        "hormone": hormone, "plate_number": plate_number, "date_assayed": date_assayed,
        "binding_pct": d["pct_b_b0"], "conc_1": d["conc_1"], "conc_2": d["conc_2"],
        "conc_avg": d["conc_avg"], "dilution": np.asarray(dilution, dtype=float)})
