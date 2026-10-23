"""Streamlit dashboard: peak-hour bottlenecks by zone/hour.

Run:  streamlit run dashboard/app.py
"""
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from delivery_ops.config import ETA_TOLERANCE_MIN, SLA_MINUTES, ZONES  # noqa: E402
from delivery_ops.metrics import ADDITIVE, bottleneck_scores, rollup  # noqa: E402

DEFAULT_DB = Path(os.environ.get("DELIVERY_OPS_DB",
                                  Path(__file__).resolve().parents[1] / "data" / "delivery_ops.db"))
BLUE = "#2a6fb0"        # volume / supply
HEAT = "Blues"          # single-hue sequential: darker = more
BAD = "#c8442b"         # reserved for "problem" marks
BOTTLENECK_THRESHOLD = 0.7

st.set_page_config(page_title="Delivery Ops", layout="wide")


def load(path: str) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    # Short-lived connection: a long-lived one would lock the file on Windows and stop the
    # pipeline from resetting the database while the dashboard is open.
    conn = sqlite3.connect(path, timeout=30)
    try:
        zh = pd.read_sql("SELECT * FROM metrics_zone_hour", conn)
        orders = pd.read_sql(
            "SELECT zone, pickup_lat, pickup_lon, status, placed_ts, delivered_ts, quoted_eta_min "
            "FROM fact_orders", conn)
        stats = dict(pd.read_sql("SELECT key, value FROM pipeline_stats", conn).values)
    finally:
        conn.close()
    return zh, orders, stats


def heatmap(df: pd.DataFrame, value: str, title: str, fmt: str, zmax: float | None = None) -> go.Figure:
    pv = df.pivot(index="zone", columns="hour", values=value).reindex(columns=range(8, 24))
    fig = go.Figure(go.Heatmap(
        z=pv.values, x=[f"{h:02d}" for h in pv.columns], y=pv.index, colorscale=HEAT, zmin=0, zmax=zmax,
        xgap=2, ygap=2, hovertemplate="%{y} · %{x}:00<br>" + title + ": %{z:" + fmt + "}<extra></extra>",
        colorbar=dict(thickness=10, tickformat=fmt)))
    fig.update_layout(title=title, height=300, margin=dict(l=0, r=0, t=40, b=0),
                      xaxis_title="Hour of day", yaxis=dict(autorange="reversed"))
    return fig


def render(db_path: str, date_range: tuple[str, str] | None, zone_filter: list[str]) -> None:
    """Draw the dashboard body. Sidebar widgets must live outside this function: Streamlit
    forbids them inside a fragment, and live mode re-runs this as a fragment."""
    if not Path(db_path).exists():
        st.error(f"No database at {db_path}. Run: delivery-ops run --days 7")
        return
    zh, orders, stats = load(db_path)
    if zh.empty:
        st.info("Waiting for the pipeline to produce metrics...")
        return

    f = zh
    if date_range:
        f = f[(f["date"] >= date_range[0]) & (f["date"] <= date_range[1])]
    if zone_filter:
        f = f[f["zone"].isin(zone_filter)]
    if f.empty:
        st.warning("No data for this selection.")
        return
    total = rollup(f.assign(k=1), ["k"]).iloc[0]
    by_zh = rollup(f, ["zone", "hour"])
    by_hour = rollup(f, ["hour"])
    scores = bottleneck_scores(f)

    st.title("Delivery operations — peak-hour bottlenecks")
    c = st.columns(5)
    c[0].metric("Orders", f"{int(total.orders):,}")
    c[1].metric("SLA breach rate", f"{total.sla_breach_rate:.1%}")
    c[2].metric("ETA error (MAE)", f"{total.eta_mae_min:.1f} min", f"bias {total.eta_bias_min:+.1f} min",
                delta_color="off")
    c[3].metric("ETA within ±5 min", f"{total.eta_within_tol_rate:.0%}")
    c[4].metric("Driver utilization", f"{total.driver_utilization:.0%}")

    hot = scores[scores["bottleneck_score"] >= BOTTLENECK_THRESHOLD]
    if len(hot):
        worst = hot.iloc[0]
        st.error(f"**{len(hot)} bottleneck zone-hours** (score ≥ {BOTTLENECK_THRESHOLD}). "
                 f"Worst: **{worst.zone} at {int(worst.hour):02d}:00** — "
                 f"{worst.sla_breach_rate:.0%} SLA breaches, {worst.driver_utilization:.0%} utilization, "
                 f"{worst.avg_dispatch_wait_min:.0f} min avg dispatch wait.")
    else:
        st.success("No bottleneck zone-hours in this selection.")

    st.subheader("Demand and SLA breaches, by hour")
    days = max(f["date"].nunique(), 1)
    left, right = st.columns(2)
    for col, y, title, color, fmt in [
        (left, by_hour["orders"] / days, "Orders per day", BLUE, ".0f"),
        (right, by_hour["sla_breach_rate"], "SLA breach rate", BAD, ".0%"),
    ]:
        fig = go.Figure(go.Bar(x=by_hour["hour"], y=y, marker_color=color,
                               hovertemplate="%{x}:00 — %{y:" + fmt + "}<extra></extra>"))
        fig.update_layout(title=title, height=300, margin=dict(l=0, r=0, t=40, b=0),
                          xaxis=dict(title="Hour of day", dtick=1), yaxis=dict(tickformat=fmt))
        col.plotly_chart(fig, width="stretch")

    st.subheader("Zone × hour")
    a, b = st.columns(2)
    a.plotly_chart(heatmap(by_zh, "sla_breach_rate", "SLA breach rate", ".0%", 1.0), width="stretch")
    b.plotly_chart(heatmap(by_zh, "driver_utilization", "Driver utilization", ".0%", 1.0), width="stretch")
    a, b = st.columns(2)
    a.plotly_chart(heatmap(by_zh, "eta_mae_min", "ETA error (MAE, min)", ".1f"), width="stretch")
    b.plotly_chart(heatmap(by_zh, "avg_dispatch_wait_min", "Avg dispatch wait (min)", ".1f"),
                   width="stretch")

    st.subheader("Top bottlenecks")
    top = scores.head(10)[["zone", "hour", "orders", "sla_breach_rate", "driver_utilization",
                           "avg_dispatch_wait_min", "eta_mae_min", "bottleneck_score"]]
    st.dataframe(
        top.rename(columns={"sla_breach_rate": "SLA breach", "driver_utilization": "utilization",
                            "avg_dispatch_wait_min": "dispatch wait (min)", "eta_mae_min": "ETA MAE (min)",
                            "bottleneck_score": "score"}),
        hide_index=True, width="stretch",
        column_config={"SLA breach": st.column_config.NumberColumn(format="percent"),
                       "utilization": st.column_config.NumberColumn(format="percent"),
                       "dispatch wait (min)": st.column_config.NumberColumn(format="%.1f"),
                       "ETA MAE (min)": st.column_config.NumberColumn(format="%.1f"),
                       "score": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
    st.caption("score = 0.4·utilization + 0.4·breach rate + 0.2·(dispatch wait ÷ 15 min), each capped at 1.")

    st.subheader("Where late orders originate")
    o = orders[orders["zone"].isin(zone_filter)].copy() if zone_filter else orders.copy()
    o["placed_ts"] = pd.to_datetime(o["placed_ts"])
    o["delivered_ts"] = pd.to_datetime(o["delivered_ts"])
    if date_range:
        day = o["placed_ts"].dt.date.astype(str)
        o = o[(day >= date_range[0]) & (day <= date_range[1])]
    o["late"] = (o["status"] != "DELIVERED") | ((o["delivered_ts"] - o["placed_ts"]).dt.total_seconds() / 60 > SLA_MINUTES)
    late = o[o["late"]]
    if len(late):
        sample = late.sample(min(len(late), 3000), random_state=0)
        m = px.scatter_map(sample, lat="pickup_lat", lon="pickup_lon", color="zone", zoom=11.3, height=420,
                           center={"lat": sample["pickup_lat"].mean(), "lon": sample["pickup_lon"].mean()},
                           opacity=0.6, hover_data={"pickup_lat": False, "pickup_lon": False})
        m.update_layout(map_style="open-street-map", margin=dict(l=0, r=0, t=0, b=0),
                        legend=dict(orientation="h", y=1.02))
        st.plotly_chart(m, width="stretch")
        st.caption(f"{len(late):,} late or cancelled orders (map shows up to 3,000), by restaurant location.")
    else:
        st.caption("No late orders in this selection.")
    if stats:
        st.caption(f"Pipeline: {int(stats['received']):,} events, {int(stats['duplicates']):,} duplicates "
                   f"dropped, {int(stats['orders_closed']):,} orders closed")


def available_dates(path: str) -> list[str]:
    if not Path(path).exists():
        return []
    conn = sqlite3.connect(path, timeout=30)
    try:
        return sorted(pd.read_sql("SELECT DISTINCT date FROM metrics_zone_hour", conn)["date"])
    except Exception:
        return []     # database exists but the pipeline hasn't written metrics yet
    finally:
        conn.close()


st.sidebar.header("Controls")
db_path = st.sidebar.text_input("Database", str(DEFAULT_DB))
live = st.sidebar.toggle("Live refresh (follow a running pipeline)", value=False)
dates = available_dates(db_path)
date_range = None
if len(dates) > 1:
    picked = st.sidebar.select_slider("Dates", options=dates, value=(dates[0], dates[-1]))
    date_range = None if picked == (dates[0], dates[-1]) else picked   # full range = "follow latest"
zone_filter = st.sidebar.multiselect("Zones (empty = all)", [z.name for z in ZONES])
st.sidebar.caption(f"SLA = delivered ≤ {SLA_MINUTES} min after order · "
                   f"ETA 'accurate' = within ±{ETA_TOLERANCE_MIN} min")
if live:
    st.fragment(run_every=3)(render)(db_path, date_range, zone_filter)
else:
    render(db_path, date_range, zone_filter)
