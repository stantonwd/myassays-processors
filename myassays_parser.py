"""Parse a MyAssays Desktop Excel export into tidy pandas tables.

Layout it expects (matches the P15 export):
  A1            plate / run name
  A2            free-text notes (dilutions, reruns, ...)
  J:K rows 3-12 4PL fit parameters (a, b, c, d, MSE, R2, SS, SYX)
  "Calibrator"  header row -> standards table (2 wells per standard)
  "Sample"      header row -> controls, samples, B0, NSB (2 wells each)
Rows with a value in the Sample column start a new item; the next row
(blank Sample cell, merged in Excel) holds the second replicate well.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
import numpy as np
import pandas as pd
from openpyxl import load_workbook

SPECIAL = {"B0", "NSB"}


def _num(x):
    """Return float, or NaN if x is text like '> Curve' / '-' / None."""
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


def _find_header(ws, first_label: str, start: int = 1) -> int:
    for r in range(start, ws.max_row + 1):
        if _txt(ws.cell(r, 2).value) == first_label:
            return r
    raise ValueError(f"Could not find a '{first_label}' header row in column B. "
                     "Is this a MyAssays export?")


def _is_yellow(cell) -> bool:
    f = cell.fill
    return bool(f and f.fill_type and f.fgColor.type == "rgb"
                and f.fgColor.rgb in ("FFFFFF00", "00FFFF00"))


@dataclass
class Plate:
    name: str
    notes: str
    fit: dict
    standards: pd.DataFrame
    controls: pd.DataFrame
    samples: pd.DataFrame
    b0: pd.DataFrame
    nsb: pd.DataFrame


def parse_myassays(file) -> Plate:
    wb = load_workbook(file, data_only=True)
    ws = wb.active

    name = _txt(ws["A1"].value)
    notes = _txt(ws["A2"].value)

    # ---- 4PL fit parameters (label in col J, value in col K) -------------
    fit = {}
    for r in range(1, 20):
        lab = _txt(ws.cell(r, 10).value)
        if lab in {"a", "b", "c", "d", "MSE", "R²", "SS", "SYX"}:
            fit[lab] = _num(ws.cell(r, 11).value)

    # ---- standards ------------------------------------------------------
    cal_hdr = _find_header(ws, "Calibrator")
    sample_hdr = _find_header(ws, "Sample", cal_hdr)
    std_rows, cur = [], None
    for r in range(cal_hdr + 1, sample_hdr):
        label = _txt(ws.cell(r, 2).value)
        well = _txt(ws.cell(r, 3).value)
        if not well:
            continue
        if label:
            cur = label
            nominal = _num(ws.cell(r, 4).value)
            raw_avg, sem = _num(ws.cell(r, 6).value), _num(ws.cell(r, 7).value)
        std_rows.append(dict(
            standard=cur, nominal=nominal, well=well,
            raw=_num(ws.cell(r, 5).value), raw_avg=raw_avg, sem=sem,
            backfit=_num(ws.cell(r, 8).value),
            recovery_pct=_num(ws.cell(r, 9).value)))
    standards = pd.DataFrame(std_rows)

    # ---- samples / controls / B0 / NSB ----------------------------------
    items, cur = [], None
    for r in range(sample_hdr + 1, ws.max_row + 1):
        label = _txt(ws.cell(r, 2).value)
        well = _txt(ws.cell(r, 3).value)
        if not label and not well:
            continue
        if label.lower().startswith("the highlighted"):
            break
        if label:                                   # first replicate row
            cur = dict(
                sample=label, wells=[well], raw=[_num(ws.cell(r, 4).value)],
                pct_b_b0=_num(ws.cell(r, 5).value),
                conc=[ws.cell(r, 6).value],
                conc_avg_mya=_num(ws.cell(r, 7).value),
                cv_mya=_num(ws.cell(r, 8).value),
                sd_mya=_num(ws.cell(r, 9).value),
                sem_mya=_num(ws.cell(r, 10).value),
                highlighted=_is_yellow(ws.cell(r, 2)))
            items.append(cur)
        elif cur is not None:                       # second replicate row
            cur["wells"].append(well)
            cur["raw"].append(_num(ws.cell(r, 4).value))
            cur["conc"].append(ws.cell(r, 6).value)
            cur["highlighted"] |= _is_yellow(ws.cell(r, 6))

    rows = []
    for it in items:
        c_num = [_num(c) for c in it["conc"]]
        c_txt = [_txt(c) for c in it["conc"]]
        out_of_range = [t for t, n in zip(c_txt, c_num) if np.isnan(n) and t]
        rows.append(dict(
            sample=it["sample"],
            wells=", ".join(it["wells"]),
            raw_1=it["raw"][0], raw_2=it["raw"][1] if len(it["raw"]) > 1 else np.nan,
            pct_b_b0=it["pct_b_b0"],
            conc_1=c_num[0], conc_2=c_num[1] if len(c_num) > 1 else np.nan,
            conc_avg=it["conc_avg_mya"], cv_pct=it["cv_mya"],
            sd=it["sd_mya"], sem=it["sem_mya"],
            out_of_range=" / ".join(out_of_range),
            highlighted=it["highlighted"]))
    df = pd.DataFrame(rows)

    is_ctrl = df["sample"].str.match(r"(?i)^control", na=False)
    is_b0 = df["sample"].eq("B0")
    is_nsb = df["sample"].eq("NSB")
    return Plate(
        name=name, notes=notes, fit=fit, standards=standards,
        controls=df[is_ctrl].reset_index(drop=True),
        samples=df[~(is_ctrl | is_b0 | is_nsb)].reset_index(drop=True),
        b0=df[is_b0].reset_index(drop=True),
        nsb=df[is_nsb].reset_index(drop=True))


def parse_dilutions(notes: str, sample_ids) -> dict:
    """Best-effort guess of dilutions from free text like
    '1691-1956 ran @ 1:128, 1514, 1521, 1626 reran @ 1:32'.
    Always shown to the user as an editable table, so a miss is harmless."""
    out = {}
    ids = [str(s) for s in sample_ids]
    num_ids = [int(s) for s in ids if s.isdigit()]
    for m in re.finditer(r"([\d,\s\-–]+?)\s*(?:re)?ran\s*@\s*1:(\d+)", notes, flags=re.I):
        factor = int(m.group(2))
        for part in re.split(r"[,\s]+", m.group(1).strip()):
            if not part:
                continue
            rng = re.fullmatch(r"(\d+)[-–](\d+)", part)
            if rng:
                lo, hi = int(rng.group(1)), int(rng.group(2))
                for n in num_ids:
                    if lo <= n <= hi:
                        out[str(n)] = factor
            elif part.isdigit() and part in ids:
                out[part] = factor
    return out


def _qc_flags(r, cv_limit, b_lo, b_hi):
    """CV problems and replicate problems are failures; binding outside the
    window is only a 'review' note (the CV may be fine, a person decides)."""
    fail, review = [], []
    if r["out_of_range"]:
        fail.append(f"Replicate {r['out_of_range']}")
    if r["n_valid"] == 0:
        fail.append("No valid replicates")
    elif r["n_valid"] == 1:
        fail.append("Only 1 valid replicate")
    elif r["cv_pct"] >= cv_limit:
        fail.append(f"CV {r['cv_pct']:.1f}% (limit < {cv_limit:g}%)")
    if pd.notna(r["pct_b_b0"]) and not (b_lo <= r["pct_b_b0"] <= b_hi):
        review.append(f"Binding {r['pct_b_b0']:.1f}% outside {b_lo:g}-{b_hi:g}%")
    status = "FAIL" if fail else ("REVIEW" if review else "OK")
    return pd.Series({"status": status, "flags": "; ".join(fail + review)})


def apply_qc(samples: pd.DataFrame, dilutions: dict, cv_limit: float = 15.5,
             b_lo: float = 20.0, b_hi: float = 80.0,
             default_dilution: float = 1.0) -> pd.DataFrame:
    """Recompute mean/SD/CV from the two replicate concentrations, multiply by
    the (hand-entered) dilution factor and add QC status/flags."""
    d = samples.copy()
    d["dilution"] = d["sample"].map(dilutions).fillna(default_dilution)
    reps = d[["conc_1", "conc_2"]]
    d["n_valid"] = reps.notna().sum(axis=1)
    d["mean_conc"] = reps.mean(axis=1)
    d["sd_calc"] = reps.std(axis=1, ddof=1)
    d["cv_calc"] = d["sd_calc"] / d["mean_conc"] * 100
    d["final_conc"] = d["conc_avg"].fillna(d["mean_conc"]) * d["dilution"]
    return pd.concat([d, d.apply(_qc_flags, axis=1, args=(cv_limit, b_lo, b_hi))], axis=1)


def apply_control_qc(controls: pd.DataFrame, cv_limit: float = 15.5,
                     b_lo: float = 20.0, b_hi: float = 80.0) -> pd.DataFrame:
    """Controls: CV/replicate checks only. b_lo/b_hi are accepted but ignored."""
    d = controls.copy()
    d["n_valid"] = d[["conc_1", "conc_2"]].notna().sum(axis=1)
    # controls are exempt from the binding window: pass b_lo=0, b_hi=100 (never flags)
    return pd.concat([d, d.apply(_qc_flags, axis=1, args=(cv_limit, 0.0, 100.0))], axis=1)


def apply_standard_qc(standards: pd.DataFrame, lo=80.0, hi=120.0) -> pd.DataFrame:
    """Informational only: recovery is not used for pass/fail."""
    d = standards.copy()
    d["flags"] = np.where((d["recovery_pct"] < lo) | (d["recovery_pct"] > hi),
                          f"Recovery outside {lo:g}-{hi:g}% (info)", "")
    return d
