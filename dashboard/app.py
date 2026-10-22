"""Streamlit dashboard: peak-hour bottlenecks by zone/hour.

Run:  streamlit run dashboard/app.py
"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from delivery_ops.config import ETA_TOLERANCE_MIN, SLA_MINUTES  # noqa: E402
from delivery_ops.metrics import ADDITIVE, bottleneck_scores, rollup  # noqa: E402

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "delivery_ops.db"
BLUE = "#2a6fb0"        # volume / supply
HEAT = "Blues"          # single-hue sequential: darker = more
BAD = "#c8442b"         # reserved for "problem" marks
BOTTLENECK_THRESHOLD = 0.7

st.set_page_config(page_title="Delivery Ops", layout="wide")


@st.cache_resource
def _conn(path: str) -> sqlite3.Connection:
    return sqlite3.connect(path, check_same_thread=False)


def load(path: str) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    conn = _conn(path)
    zh = pd.read_sql("SELECT * FROM metrics_zone_hour", conn)
    breached = pd.read_sql(
        "SELECT zone, pickup_lat, pickup_lon, status, placed_ts, delivered_ts, quoted_eta_min "
        "FROM fact_orders", conn)
    stats = dict(pd.read_sql("SELECT key, value FROM pipeline_stats", conn).values) if \
        conn.execute("SELECT COUNT(*) FROM pipeline_stats").fetchone()[0] else {}
    return zh, breached, stats


def heatmap(df: pd.DataFrame, value: str, title: str, fmt: str, zmax: float | None = None) -> go.Figure:
    pv = df.pivot(index="zone", columns="hour", values=value).reindex(columns=range(8, 24))
    fig = go.Figure(go.Heatmap(
        z=pv.values, x=[f"{h:02d}" for h in pv.columns], y=pv.index, colorscale=HEAT, zmin=0, zmax=zmax,
        xgap=2, ygap=2, hovertemplate="%{y} · %{x}:00<br>" + title + ": %{z:" + fmt + "}<extra></extra>",
        colorbar=dict(thickness=10, tickformat=fmt)))
    fig.update_layout(title=title, height=300, margin=dict(l=0, r=0, t=40, b=0),
                      xaxis_title="Hour of day", yaxis=dict(autorange="reversed"))
    return fig


def render(db_path: str) -> None:
    if not Path(db_path).exists():
        st.error(f"No database at {db_path}. Run: python -m delivery_ops.cli run --days 7")
        return
    zh, orders, stats = load(db_path)
    if zh.empty:
        st.info("Waiting for the pipeline to produce metrics...")
        return

    with st.sidebar:
        dates = sorted(zh["date"].unique())
        sel_dates = st.select_slider("Dates", options=dates, value=(dates[0], dates[-1]))
        zones = sorted(zh["zone"].unique())
        sel_zones = st.multiselect("Zones", zones, default=zones)
        st.caption(f"SLA = delivered ≤ {SLA_MINUTES} min after order · "
                   f"ETA 'accurate' = within ±{ETA_TOLERANCE_MIN} min")
        if stats:
            st.caption(f"Pipeline: {int(stats['received']):,} events, "
                       f"{int(stats['duplicates']):,} duplicates dropped, "
                       f"{int(stats['orders_closed']):,} orders closed")

    f = zh[(zh["date"] >= sel_dates[0]) & (zh["date"] <= sel_dates[1]) & zh["zone"].isin(sel_zones)]
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
    o = orders[orders["zone"].isin(sel_zones)].copy()
    o["placed_ts"] = pd.to_datetime(o["placed_ts"])
    o["delivered_ts"] = pd.to_datetime(o["delivered_ts"])
    o = o[(o["placed_ts"].dt.date.astype(str) >= sel_dates[0]) & (o["placed_ts"].dt.date.astype(str) <= sel_dates[1])]
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


st.sidebar.header("Controls")
db_path = st.sidebar.text_input("Database", str(DEFAULT_DB))
live = st.sidebar.toggle("Live refresh (follow a running pipeline)", value=False)
if live:
    st.fragment(run_every=3)(render)(db_path)
else:
    render(db_path)

