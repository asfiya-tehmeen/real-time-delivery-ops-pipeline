"""Zone/hour KPIs: ETA accuracy, driver utilization, SLA breach rate, bottleneck score.

`compute_zone_hour` returns *additive* components per (date, zone, hour) so any
slice (date range, zone subset) can be re-aggregated exactly with `add_ratios`.
"""
import numpy as np
import pandas as pd

from .config import ETA_TOLERANCE_MIN, SLA_MINUTES

ADDITIVE = ["orders", "delivered", "cancelled", "breaches", "eta_abs_err_sum", "eta_err_sum",
            "eta_within_tol", "dispatch_wait_sum", "busy_sec", "shift_sec"]


def _hourly_overlap(df: pd.DataFrame, start_col: str, end_col: str, out_col: str) -> pd.DataFrame:
    """Split [start, end) intervals at hour boundaries -> rows of (date, zone, hour, seconds)."""
    rows = []
    for zone, s, e in zip(df["zone"], df[start_col], df[end_col]):
        t = s
        while t < e:
            nxt = min(e, t.floor("h") + pd.Timedelta(hours=1))
            rows.append((t.date().isoformat(), zone, t.hour, (nxt - t).total_seconds()))
            t = nxt
    cols = ["date", "zone", "hour", out_col]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows, columns=cols).groupby(["date", "zone", "hour"], as_index=False)[out_col].sum()


def compute_zone_hour(orders: pd.DataFrame, shifts: pd.DataFrame) -> pd.DataFrame:
    """orders: closed orders (DELIVERED/CANCELLED) from fact_orders; shifts: driver_shifts."""
    keys = ["date", "zone", "hour"]
    parts = []

    if len(orders):
        o = orders.copy()
        for c in ["placed_ts", "assigned_ts", "delivered_ts"]:
            o[c] = pd.to_datetime(o[c])
        o["date"] = o["placed_ts"].dt.date.astype(str)
        o["hour"] = o["placed_ts"].dt.hour
        deliv = o["status"] == "DELIVERED"
        o["delivered"] = deliv.astype(int)
        o["cancelled"] = (~deliv).astype(int)
        o["orders"] = 1
        total_min = (o["delivered_ts"] - o["placed_ts"]).dt.total_seconds() / 60
        o["breaches"] = ((~deliv) | (total_min > SLA_MINUTES)).astype(int)
        err = (total_min - o["quoted_eta_min"]).where(deliv, 0.0)
        o["eta_err_sum"] = err
        o["eta_abs_err_sum"] = err.abs()
        o["eta_within_tol"] = (deliv & (err.abs() <= ETA_TOLERANCE_MIN)).astype(int)
        o["dispatch_wait_sum"] = ((o["assigned_ts"] - o["placed_ts"]).dt.total_seconds() / 60).where(deliv, 0.0)
        parts.append(o.groupby(keys, as_index=False)[
            ["orders", "delivered", "cancelled", "breaches", "eta_err_sum", "eta_abs_err_sum",
             "eta_within_tol", "dispatch_wait_sum"]].sum())

        busy_df = o[deliv & o["assigned_ts"].notna()]
        parts.append(_hourly_overlap(busy_df, "assigned_ts", "delivered_ts", "busy_sec"))

    if len(shifts):
        s = shifts.dropna(subset=["online_ts", "offline_ts"]).copy()
        s["online_ts"] = pd.to_datetime(s["online_ts"])
        s["offline_ts"] = pd.to_datetime(s["offline_ts"])
        parts.append(_hourly_overlap(s, "online_ts", "offline_ts", "shift_sec"))

    if not parts:
        return pd.DataFrame(columns=keys + ADDITIVE)
    out = parts[0]
    for p in parts[1:]:
        out = out.merge(p, on=keys, how="outer")
    for c in ADDITIVE:
        if c not in out:
            out[c] = 0.0
    out[ADDITIVE] = out[ADDITIVE].fillna(0.0)
    return out[keys + ADDITIVE].sort_values(keys).reset_index(drop=True)


def add_ratios(df: pd.DataFrame) -> pd.DataFrame:
    """Derive rates from additive components. Call after summing whatever slice you need."""
    d = df.copy()
    n = d["orders"].replace(0, np.nan)
    dn = d["delivered"].replace(0, np.nan)
    d["sla_breach_rate"] = d["breaches"] / n
    d["cancel_rate"] = d["cancelled"] / n
    d["eta_mae_min"] = d["eta_abs_err_sum"] / dn
    d["eta_bias_min"] = d["eta_err_sum"] / dn            # positive = ETAs too optimistic
    d["eta_within_tol_rate"] = d["eta_within_tol"] / dn
    d["avg_dispatch_wait_min"] = d["dispatch_wait_sum"] / dn
    d["driver_utilization"] = (d["busy_sec"] / d["shift_sec"].replace(0, np.nan)).clip(upper=1.0)
    d["driver_hours"] = d["shift_sec"] / 3600
    return d


def rollup(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """Sum additive components over `by` and compute ratios."""
    return add_ratios(df.groupby(by, as_index=False)[ADDITIVE].sum())


def bottleneck_scores(zone_hour: pd.DataFrame, min_orders: int = 10) -> pd.DataFrame:
    """Rank zone-hours by how badly they're strained.

    score = 0.4*utilization + 0.4*breach rate + 0.2*(dispatch wait / 15 min), each capped at 1.
    Hours with fewer than `min_orders` orders are dropped (rates are too noisy).
    """
    d = rollup(zone_hour, ["zone", "hour"])
    d = d[d["orders"] >= min_orders].copy()
    d["bottleneck_score"] = (
        0.4 * d["driver_utilization"].fillna(0).clip(0, 1)
        + 0.4 * d["sla_breach_rate"].fillna(0).clip(0, 1)
        + 0.2 * (d["avg_dispatch_wait_min"].fillna(0) / 15).clip(0, 1)
    )
    return d.sort_values("bottleneck_score", ascending=False).reset_index(drop=True)
