"""Order-event stream simulator.

`generate_events` is a Python generator. For each simulated day it builds the
full order/driver timeline (FIFO dispatch against a finite per-zone driver pool),
then yields events in *arrival* order: event time plus a small random delay, so
the stream is slightly out of order and contains a few duplicates, like a real feed.
"""
import heapq
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterator

import numpy as np

from . import events as ev
from .config import (BASE_SPEED_KMH, HOURLY_DEMAND, SHIFTS, TRAFFIC_FACTOR, ZONES, Zone)
from .geo import haversine_km, interpolate, offset_point

PING_EVERY_MIN = 4.0
DETOUR = 1.3                 # road distance / straight-line distance


@dataclass
class _Driver:
    driver_id: str
    zone: Zone
    shift_start: datetime
    shift_end: datetime
    free_at: datetime
    lat: float
    lon: float


def _scatter(rng: random.Random, zone: Zone) -> tuple[float, float]:
    r = zone.radius_km * math.sqrt(rng.random())
    theta = rng.random() * 2 * math.pi
    return offset_point(zone.lat, zone.lon, r * math.cos(theta), r * math.sin(theta))


def _speed_kmh(rng: random.Random, when: datetime) -> float:
    return BASE_SPEED_KMH * TRAFFIC_FACTOR[when.hour] * rng.lognormvariate(0, 0.12)


def quote_eta_min(pickup, dropoff) -> float:
    """What the platform tells the customer at order time.

    Deliberately naive: it ignores current driver load and traffic, which is why its
    accuracy degrades in peak hours.
    """
    dist = haversine_km(*pickup, *dropoff) * DETOUR
    return round(12.0 + 5.0 + dist / BASE_SPEED_KMH * 60 + 3.0, 1)


def _demand_rate(zone: Zone, hour: int, orders_per_day: float) -> float:
    boost = 1.0
    if 12 <= hour <= 14:
        boost = zone.lunch_boost
    elif 19 <= hour <= 21:
        boost = zone.dinner_boost
    return orders_per_day * zone.demand_weight * HOURLY_DEMAND[hour] * boost


def _make_drivers(day: datetime, rng: random.Random) -> dict[str, list[_Driver]]:
    pool: dict[str, list[_Driver]] = {}
    for zi, zone in enumerate(ZONES):
        drivers = []
        for i in range(zone.drivers):
            s, e = SHIFTS[i % len(SHIFTS)]
            start = day + timedelta(hours=s)
            drivers.append(_Driver(
                driver_id=f"D{zi + 1}{i + 1:02d}", zone=zone,
                shift_start=start, shift_end=day + timedelta(hours=e),
                free_at=start, lat=zone.lat, lon=zone.lon,
            ))
        pool[zone.name] = drivers
    return pool


def _fmt(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def _day_events(day: date, rng: random.Random, nprng: np.random.Generator,
                orders_per_day: float, seq_start: int) -> list[tuple[datetime, ev.Event]]:
    midnight = datetime.combine(day, datetime.min.time())
    pool = _make_drivers(midnight, rng)
    out: list[tuple[datetime, ev.Event]] = []
    counter = [0]

    def emit(etype, ts, zone, lat, lon, **kw):
        counter[0] += 1
        out.append((ts, ev.Event(
            event_id=f"{day:%Y%m%d}-{counter[0]:06d}", event_type=etype, ts=_fmt(ts),
            zone=zone, lat=round(lat, 6), lon=round(lon, 6), **kw)))

    for drivers in pool.values():
        for d in drivers:
            emit(ev.DRIVER_ONLINE, d.shift_start, d.zone.name, d.zone.lat, d.zone.lon, driver_id=d.driver_id)
            emit(ev.DRIVER_OFFLINE, d.shift_end, d.zone.name, d.zone.lat, d.zone.lon, driver_id=d.driver_id)

    # --- demand: Poisson arrivals per zone-hour, uniform within the hour
    placed: list[tuple[datetime, Zone]] = []
    for zone in ZONES:
        for hour in range(24):
            n = nprng.poisson(_demand_rate(zone, hour, orders_per_day))
            for _ in range(n):
                placed.append((midnight + timedelta(hours=hour, seconds=rng.uniform(0, 3599)), zone))
    placed.sort(key=lambda x: x[0])

    # --- dispatch in placement order
    seq = seq_start
    for placed_at, zone in placed:
        seq += 1
        order_id = f"O{day:%y%m%d}{seq:05d}"
        pickup = _scatter(rng, zone)
        dropoff = _scatter(rng, zone)
        eta = quote_eta_min(pickup, dropoff)
        emit(ev.ORDER_PLACED, placed_at, zone.name, *pickup, order_id=order_id,
             quoted_eta_min=eta, dropoff_lat=round(dropoff[0], 6), dropoff_lon=round(dropoff[1], 6))

        patience = timedelta(minutes=rng.uniform(25, 40))
        best, best_start = None, None
        for d in pool[zone.name]:
            start = max(placed_at, d.free_at, d.shift_start)
            if start + timedelta(minutes=30) > d.shift_end:     # not enough shift left
                continue
            if best_start is None or start < best_start:
                best, best_start = d, start
        if best is None or best_start - placed_at > patience:
            emit(ev.ORDER_CANCELLED, placed_at + patience, zone.name, *pickup, order_id=order_id)
            continue

        d = best
        start = best_start
        emit(ev.DRIVER_ASSIGNED, start, zone.name, d.lat, d.lon, order_id=order_id, driver_id=d.driver_id)

        to_pickup_km = haversine_km(d.lat, d.lon, *pickup) * DETOUR
        arrive = start + timedelta(hours=to_pickup_km / _speed_kmh(rng, start))
        ready = placed_at + timedelta(minutes=min(35, rng.lognormvariate(math.log(11), 0.35)))
        picked = max(arrive, ready) + timedelta(minutes=rng.uniform(1, 2))
        emit(ev.PICKED_UP, picked, zone.name, *pickup, order_id=order_id, driver_id=d.driver_id)

        drive_km = haversine_km(*pickup, *dropoff) * DETOUR
        drive = timedelta(hours=drive_km / _speed_kmh(rng, picked))
        delivered = picked + drive + timedelta(minutes=rng.uniform(1.5, 3))
        ping = picked + timedelta(minutes=PING_EVERY_MIN)
        while ping < delivered - timedelta(minutes=1):
            frac = min(1.0, (ping - picked) / drive) if drive.total_seconds() > 0 else 1.0
            lat, lon = interpolate(pickup, dropoff, frac)
            emit(ev.IN_TRANSIT, ping, zone.name, lat, lon, order_id=order_id, driver_id=d.driver_id)
            ping += timedelta(minutes=PING_EVERY_MIN)
        emit(ev.DELIVERED, delivered, zone.name, *dropoff, order_id=order_id, driver_id=d.driver_id)

        d.free_at, d.lat, d.lon = delivered, dropoff[0], dropoff[1]

    return out


def generate_events(start: date, days: int = 1, orders_per_day: float = 900, seed: int = 42,
                    max_delay_s: float = 45.0, dup_rate: float = 0.01) -> Iterator[ev.Event]:
    """Yield the event stream in (approximate) arrival order."""
    rng = random.Random(seed)
    nprng = np.random.default_rng(seed)
    seq = 0
    for i in range(days):
        day_events = _day_events(start + timedelta(days=i), rng, nprng, orders_per_day, seq)
        seq += len(day_events)
        heap = []
        for n, (ts, e) in enumerate(day_events):
            arrival = ts + timedelta(seconds=rng.uniform(0, max_delay_s))
            heapq.heappush(heap, (arrival, n, e))
            if rng.random() < dup_rate:      # at-least-once delivery -> duplicates
                heapq.heappush(heap, (arrival + timedelta(seconds=rng.uniform(1, 30)), n, e))
        while heap:
            yield heapq.heappop(heap)[2]
