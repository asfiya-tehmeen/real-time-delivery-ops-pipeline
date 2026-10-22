"""Streaming pipeline: generator -> bounded queue -> consumer -> SQLite -> metrics.

A producer thread pulls events from the simulator and pushes them onto a bounded
`queue.Queue` (back-pressure). The consumer de-duplicates, assembles per-order state
(tolerating out-of-order arrival), writes closed orders to `fact_orders`, and
periodically refreshes `metrics_zone_hour` so a dashboard can follow along live.
"""
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from . import events as ev
from .metrics import compute_zone_hour
from .simulator import generate_events

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, event_type TEXT, ts TEXT, zone TEXT, lat REAL, lon REAL,
    order_id TEXT, driver_id TEXT, quoted_eta_min REAL, dropoff_lat REAL, dropoff_lon REAL);
CREATE TABLE IF NOT EXISTS fact_orders (
    order_id TEXT PRIMARY KEY, zone TEXT, driver_id TEXT, status TEXT,
    placed_ts TEXT, assigned_ts TEXT, picked_ts TEXT, delivered_ts TEXT, cancelled_ts TEXT,
    quoted_eta_min REAL, pickup_lat REAL, pickup_lon REAL, dropoff_lat REAL, dropoff_lon REAL);
CREATE TABLE IF NOT EXISTS driver_shifts (
    driver_id TEXT, zone TEXT, online_ts TEXT, offline_ts TEXT, PRIMARY KEY (driver_id, online_ts));
CREATE TABLE IF NOT EXISTS metrics_zone_hour (
    date TEXT, zone TEXT, hour INTEGER, orders REAL, delivered REAL, cancelled REAL, breaches REAL,
    eta_abs_err_sum REAL, eta_err_sum REAL, eta_within_tol REAL, dispatch_wait_sum REAL,
    busy_sec REAL, shift_sec REAL, PRIMARY KEY (date, zone, hour));
CREATE TABLE IF NOT EXISTS pipeline_stats (key TEXT PRIMARY KEY, value REAL);
"""

_SENTINEL = object()


@dataclass
class Stats:
    received: int = 0
    duplicates: int = 0
    orders_closed: int = 0
    refreshes: int = 0
    elapsed_s: float = 0.0


@dataclass
class _OrderState:
    zone: str = ""
    placed: dict | None = None
    assigned: dict | None = None
    picked: dict | None = None
    delivered: dict | None = None
    cancelled: dict | None = None


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def reset(db_path: str | Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)


def _close_order(oid: str, st: _OrderState) -> tuple | None:
    """Return a fact_orders row if the order's lifecycle is complete, else None."""
    p = st.placed
    if p is None:
        return None
    if st.cancelled is not None:
        return (oid, st.zone, None, "CANCELLED", p["ts"], None, None, None, st.cancelled["ts"],
                p["quoted_eta_min"], p["lat"], p["lon"], p["dropoff_lat"], p["dropoff_lon"])
    if st.assigned and st.picked and st.delivered:
        return (oid, st.zone, st.assigned["driver_id"], "DELIVERED", p["ts"], st.assigned["ts"],
                st.picked["ts"], st.delivered["ts"], None, p["quoted_eta_min"],
                p["lat"], p["lon"], p["dropoff_lat"], p["dropoff_lon"])
    return None


class Consumer:
    def __init__(self, conn: sqlite3.Connection, refresh_every: int = 2000):
        self.conn = conn
        self.refresh_every = refresh_every
        self.stats = Stats()
        self._orders: dict[str, _OrderState] = {}
        self._online: dict[str, dict] = {}
        self._since_refresh = 0

    def handle(self, e: dict) -> None:
        self.stats.received += 1
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO events VALUES (:event_id,:event_type,:ts,:zone,:lat,:lon,"
            ":order_id,:driver_id,:quoted_eta_min,:dropoff_lat,:dropoff_lon)", e)
        if cur.rowcount == 0:
            self.stats.duplicates += 1
            return
        t = e["event_type"]
        if t in ev.DRIVER_EVENTS:
            self._handle_driver(e)
        elif t in ev.ORDER_EVENTS:
            self._handle_order(e)
        self._since_refresh += 1
        if self._since_refresh >= self.refresh_every:
            self.refresh_metrics()

    def _handle_driver(self, e: dict) -> None:
        did = e["driver_id"]
        if e["event_type"] == ev.DRIVER_ONLINE:
            self._online[did] = e
        else:
            on = self._online.pop(did, None)
            if on:
                self.conn.execute("INSERT OR REPLACE INTO driver_shifts VALUES (?,?,?,?)",
                                  (did, e["zone"], on["ts"], e["ts"]))

    def _handle_order(self, e: dict) -> None:
        oid = e["order_id"]
        st = self._orders.setdefault(oid, _OrderState(zone=e["zone"]))
        slot = {ev.ORDER_PLACED: "placed", ev.DRIVER_ASSIGNED: "assigned", ev.PICKED_UP: "picked",
                ev.DELIVERED: "delivered", ev.ORDER_CANCELLED: "cancelled"}.get(e["event_type"])
        if slot is None:          # IN_TRANSIT pings are stored raw but don't affect order state
            return
        setattr(st, slot, e)
        row = _close_order(oid, st)
        if row:
            self.conn.execute("INSERT OR REPLACE INTO fact_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            del self._orders[oid]
            self.stats.orders_closed += 1

    def refresh_metrics(self) -> None:
        orders = pd.read_sql("SELECT * FROM fact_orders", self.conn)
        shifts = pd.read_sql("SELECT * FROM driver_shifts", self.conn)
        # Shifts still open (driver not yet offline) count up to the latest event seen.
        if self._online:
            now = pd.read_sql("SELECT MAX(ts) AS t FROM events", self.conn)["t"][0]
            open_rows = [{"driver_id": d, "zone": e["zone"], "online_ts": e["ts"], "offline_ts": now}
                         for d, e in self._online.items() if e["ts"] < now]
            shifts = pd.concat([shifts, pd.DataFrame(open_rows)], ignore_index=True)
        zh = compute_zone_hour(orders, shifts)
        self.conn.execute("DELETE FROM metrics_zone_hour")
        zh.to_sql("metrics_zone_hour", self.conn, if_exists="append", index=False)
        self._since_refresh = 0
        self.stats.refreshes += 1
        self.conn.commit()

    def finish(self) -> None:
        self.refresh_metrics()
        for k, v in vars(self.stats).items():
            self.conn.execute("INSERT OR REPLACE INTO pipeline_stats VALUES (?,?)", (k, float(v)))
        self.conn.commit()


def run_pipeline(db_path: str | Path, start: date, days: int = 1, orders_per_day: float = 900,
                 seed: int = 42, queue_size: int = 5000, refresh_every: int = 2000,
                 throttle_s: float = 0.0, fresh: bool = True) -> Stats:
    """Run producer + consumer to completion. `throttle_s` sleeps per event to mimic real time."""
    if fresh:
        reset(db_path)
    conn = connect(db_path)
    q: queue.Queue = queue.Queue(maxsize=queue_size)
    errors: list[BaseException] = []

    def produce():
        try:
            for e in generate_events(start, days, orders_per_day, seed):
                q.put(e.to_dict())
                if throttle_s:
                    time.sleep(throttle_s)
        except BaseException as exc:      # surface producer failures in the main thread
            errors.append(exc)
        finally:
            q.put(_SENTINEL)

    t0 = time.perf_counter()
    thread = threading.Thread(target=produce, daemon=True)
    thread.start()
    consumer = Consumer(conn, refresh_every)
    while True:
        item = q.get()
        if item is _SENTINEL:
            break
        consumer.handle(item)
    thread.join()
    if errors:
        raise errors[0]
    consumer.stats.elapsed_s = time.perf_counter() - t0
    consumer.finish()
    conn.close()
    return consumer.stats
