import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from delivery_ops import events as ev  # noqa: E402
from delivery_ops.config import SLA_MINUTES  # noqa: E402
from delivery_ops.metrics import bottleneck_scores, compute_zone_hour, rollup  # noqa: E402
from delivery_ops.pipeline import run_pipeline  # noqa: E402
from delivery_ops.simulator import generate_events  # noqa: E402


def test_simulator_is_deterministic():
    a = [e.event_id for e in generate_events(date(2026, 9, 1), 1, 200, seed=1)]
    b = [e.event_id for e in generate_events(date(2026, 9, 1), 1, 200, seed=1)]
    assert a == b and len(a) > 100


def test_order_lifecycle_is_ordered():
    by_order: dict[str, dict[str, str]] = {}
    for e in generate_events(date(2026, 9, 1), 1, 300, seed=3):
        if e.order_id and e.event_type in (ev.ORDER_PLACED, ev.DRIVER_ASSIGNED, ev.PICKED_UP, ev.DELIVERED):
            by_order.setdefault(e.order_id, {})[e.event_type] = e.ts
    done = [o for o in by_order.values() if ev.DELIVERED in o]
    assert done
    for o in done:
        assert o[ev.ORDER_PLACED] <= o[ev.DRIVER_ASSIGNED] <= o[ev.PICKED_UP] < o[ev.DELIVERED]


def test_pipeline_dedupes_and_closes_every_order(tmp_path):
    db = tmp_path / "t.db"
    stats = run_pipeline(db, date(2026, 9, 1), days=1, orders_per_day=300, seed=5, refresh_every=500)
    assert stats.duplicates > 0
    conn = sqlite3.connect(db)
    placed = conn.execute("SELECT COUNT(*) FROM events WHERE event_type='ORDER_PLACED'").fetchone()[0]
    closed = conn.execute("SELECT COUNT(*) FROM fact_orders").fetchone()[0]
    assert placed == closed == stats.orders_closed
    assert conn.execute("SELECT COUNT(*) FROM metrics_zone_hour").fetchone()[0] > 0
    raw = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert raw == stats.received - stats.duplicates


def test_metrics_on_handcrafted_orders():
    orders = pd.DataFrame([
        # on time, ETA exact
        dict(order_id="a", zone="Z", status="DELIVERED", placed_ts="2026-09-01T12:00:00",
             assigned_ts="2026-09-01T12:00:00", delivered_ts="2026-09-01T12:30:00", quoted_eta_min=30.0),
        # late (60 min) with ETA 30 -> error 30
        dict(order_id="b", zone="Z", status="DELIVERED", placed_ts="2026-09-01T12:10:00",
             assigned_ts="2026-09-01T12:30:00", delivered_ts="2026-09-01T13:10:00", quoted_eta_min=30.0),
        # cancelled counts as breach
        dict(order_id="c", zone="Z", status="CANCELLED", placed_ts="2026-09-01T12:15:00",
             assigned_ts=None, delivered_ts=None, quoted_eta_min=30.0),
    ])
    shifts = pd.DataFrame([dict(driver_id="d1", zone="Z", online_ts="2026-09-01T12:00:00",
                                offline_ts="2026-09-01T14:00:00")])
    zh = compute_zone_hour(orders, shifts)
    r = rollup(zh[zh["hour"] == 12], ["zone"]).iloc[0]
    assert r.orders == 3 and r.breaches == 2
    assert abs(r.sla_breach_rate - 2 / 3) < 1e-9
    assert abs(r.eta_mae_min - 15.0) < 1e-9           # (0 + 30) / 2 delivered
    # hour 12 busy: a 12:00-12:30 (1800s) + b 12:30-13:00 (1800s) of one driver's 3600s
    assert abs(r.driver_utilization - 1.0) < 1e-9
    assert SLA_MINUTES == 45


def test_bottleneck_scores_rank_strained_hours_first():
    zh = pd.DataFrame([
        dict(date="d", zone="A", hour=12, orders=50, delivered=50, cancelled=0, breaches=40,
             eta_abs_err_sum=500, eta_err_sum=500, eta_within_tol=5, dispatch_wait_sum=700,
             busy_sec=3500, shift_sec=3600),
        dict(date="d", zone="A", hour=9, orders=50, delivered=50, cancelled=0, breaches=0,
             eta_abs_err_sum=100, eta_err_sum=0, eta_within_tol=45, dispatch_wait_sum=50,
             busy_sec=1000, shift_sec=3600),
    ])
    s = bottleneck_scores(zh)
    assert list(s["hour"]) == [12, 9]
