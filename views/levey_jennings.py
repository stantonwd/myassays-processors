"""Levey-Jennings: each control lot across plates, from the records.
Click a plate's average point to accept or reject that control for the CVs."""
import altair as alt
import numpy as np
import pandas as pd
import streamlit as st
import records as rec
from common import REC_DIR, SERIES, MUTED, load_db, load_collection, sidebar_data_info

st.title("📈 Levey-Jennings")
db = load_db()
sidebar_data_info(*load_collection()[1:])
ctrl = db["controls"]
ctrl = ctrl[ctrl["control_id"] != ""]
if ctrl.empty:
    st.info("No controls with a lot ID have been saved yet.")
    st.stop()

ss = st.session_state
ss.setdefault("lj_ver", 0)

hormones = sorted(ctrl["hormone"].unique())
with st.sidebar:
    hormone = st.selectbox("Hormone", hormones)
c = ctrl[ctrl["hormone"] == hormone].copy()
c["_lvl"] = c["control_level"].map({"hi": 0, "mid": 1, "low": 2}).fillna(3)
lots = (c.sort_values(["_lvl", "control_id"])["control_id"].drop_duplicates().tolist())
latest = c.sort_values(["date_assayed", "plate_number"])["plate_id"].iloc[-1]
default = [l for l in lots if l in set(c.loc[c["plate_id"] == latest, "control_id"])]
show = st.multiselect("Control lots", lots, default=default or lots[:1])
st.caption("Each plate shows the control's two duplicate reads (small grey dots joined by "
           "a line) and their average (blue). Mean and SD lines use accepted averages only. "
           "Hollow = rejected, triangle = more than 2 SD from the mean. **Click an average "
           "to accept or reject it.**")


def lot_chart(d: pd.DataFrame, mu, sd):
    order = list(d["plate_id"])
    x = alt.X("plate_id:N", sort=order, title="Plate (by date assayed)")
    y_title = "Conc × dilution"
    layers = []
    if pd.notna(sd) and sd > 0:
        ref = pd.DataFrame({"y": [mu + i * sd for i in (-3, -2, -1, 0, 1, 2, 3)],
                            "lab": ["-3SD", "-2SD", "-1SD", "mean", "+1SD", "+2SD", "+3SD"]})
        ref["dash"] = np.where(ref["lab"] == "mean", "mean", "sd")
        layers += [
            alt.Chart(ref).mark_rule(color=MUTED, strokeWidth=1).encode(
                y="y:Q", strokeDash=alt.StrokeDash("dash:N", legend=None, scale=alt.Scale(
                    domain=["mean", "sd"], range=[[1, 0], [4, 4]]))),
            alt.Chart(ref).mark_text(align="left", baseline="bottom", dx=4, dy=-2,
                                     color=MUTED, fontSize=10).encode(
                y="y:Q", text="lab:N", x=alt.value(0))]
    reads = d.melt(id_vars=["plate_id", "control_plate_id"],
                   value_vars=["read_1", "read_2"], var_name="read", value_name="value")
    layers += [
        alt.Chart(d).mark_rule(color=MUTED, strokeWidth=2, opacity=0.6).encode(
            x=x, y=alt.Y("read_lo:Q", title=y_title), y2="read_hi:Q"),
        alt.Chart(reads).mark_circle(size=36, color=MUTED, opacity=0.9).encode(
            x=x, y="value:Q",
            tooltip=["plate_id", "read", alt.Tooltip("value:Q", format=",.1f")]),
    ]
    pick = alt.selection_point(name="pick", fields=["control_plate_id"], on="click",
                               empty=False)
    base = alt.Chart(d).encode(x=x, y=alt.Y("conc_x_dil:Q", title=y_title,
                                            scale=alt.Scale(zero=False),
                                            axis=alt.Axis(grid=False)))
    layers += [
        base.transform_filter("datum.keep_yn").mark_line(color=SERIES, strokeWidth=2),
        base.mark_point(strokeWidth=2, stroke=SERIES, opacity=1, cursor="pointer").encode(
            fill=alt.condition("datum.keep_yn", alt.value(SERIES), alt.value("transparent")),
            shape=alt.condition("datum.beyond_2sd", alt.value("triangle-up"),
                                alt.value("circle")),
            size=alt.condition(pick, alt.value(260), alt.value(110)),
            tooltip=["plate_id", "date_assayed",
                     alt.Tooltip("read_1:Q", format=",.1f", title="read 1"),
                     alt.Tooltip("read_2:Q", format=",.1f", title="read 2"),
                     alt.Tooltip("conc_x_dil:Q", format=",.1f", title="average"),
                     alt.Tooltip("cv_pct:Q", format=".1f", title="CV %"), "kept",
                     alt.Tooltip("z:Q", format=".2f", title="SD from mean"), "keep_note"],
        ).add_params(pick),
    ]
    return alt.layer(*layers).properties(height=280)


for lot in show:
    d = c[c["control_id"] == lot].sort_values(["date_assayed", "plate_number"]).copy()
    d["read_1"] = d["conc_1"] * d["dilution"]
    d["read_2"] = d["conc_2"] * d["dilution"]
    d["read_lo"] = d[["read_1", "read_2"]].min(axis=1)
    d["read_hi"] = d[["read_1", "read_2"]].max(axis=1)
    k = d[d["keep_yn"] & d["conc_x_dil"].notna()]
    mu, sd = k["conc_x_dil"].mean(), k["conc_x_dil"].std()
    d["z"] = (d["conc_x_dil"] - mu) / sd if pd.notna(sd) and sd > 0 else np.nan
    d["beyond_2sd"] = d["z"].abs() > 2
    d["kept"] = np.where(d["keep_yn"], "accepted", "rejected")

    level = "/".join(sorted(set(d["control_level"])))
    st.markdown(f"#### {lot} ({level})")
    m = st.columns(4)
    m[0].metric("Accepted plates", f"{len(k)} of {len(d)}")
    m[1].metric("Mean", f"{mu:,.1f}" if pd.notna(mu) else "–")
    m[2].metric("Inter-assay CV", f"{sd / mu * 100:.1f}%" if len(k) > 1 else "–")
    m[3].metric("Mean intra-assay CV", f"{k['cv_pct'].mean():.1f}%" if len(k) else "–")

    ev = st.altair_chart(lot_chart(d, mu, sd), width="stretch", on_select="rerun",
                         selection_mode="pick", key=f"lj_{lot}_{ss['lj_ver']}")
    picked = []
    try:
        picked = [p["control_plate_id"] for p in (ev.selection.get("pick") or [])
                  if "control_plate_id" in p]
    except Exception:
        pass
    if picked:
        p = d[d["control_plate_id"] == picked[0]].iloc[0]
        with st.container(border=True):
            st.markdown(f"**{p['plate_id']}** · {p['date_assayed']} · read 1 "
                        f"{p['read_1']:,.1f} · read 2 {p['read_2']:,.1f} · average "
                        f"{p['conc_x_dil']:,.1f} · CV {p['cv_pct']:.1f}% · "
                        + (f"{p['z']:+.2f} SD · " if pd.notna(p["z"]) else "")
                        + f"currently **{p['kept']}**")
            if p["flag"]:
                st.caption(p["flag"])
            note = st.text_input("Note (optional)", p["keep_note"],
                                 key=f"note_{p['control_plate_id']}",
                                 placeholder="e.g. pipetting error on read 2")
            b = st.columns([1, 1, 4])
            if b[0].button("Accept", type="primary" if not p["keep_yn"] else "secondary",
                           key=f"acc_{p['control_plate_id']}"):
                rec.set_control_keep(REC_DIR, [p["control_plate_id"]], True, note)
                ss["lj_ver"] += 1
                st.rerun()
            if b[1].button("Reject", type="primary" if p["keep_yn"] else "secondary",
                           key=f"rej_{p['control_plate_id']}"):
                rec.set_control_keep(REC_DIR, [p["control_plate_id"]], False, note)
                ss["lj_ver"] += 1
                st.rerun()

    with st.expander(f"Table: {lot}"):
        st.dataframe(d[["plate_id", "date_assayed", "read_1", "read_2", "conc_x_dil",
                        "cv_pct", "kept", "z", "keep_note"]].rename(
            columns={"conc_x_dil": "average"}).style.format(precision=1),
            hide_index=True, width="stretch")

st.markdown("#### Lot summary (accepted controls)")
st.dataframe(rec.control_summary(c).style.format(precision=2), hide_index=True,
             width="stretch")
