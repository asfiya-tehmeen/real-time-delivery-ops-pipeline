# Real-Time Delivery Ops Pipeline

Simulated order-event stream → queue-based pipeline → zone/hour KPIs → bottleneck dashboard.

```
simulator (generator) ──▶ bounded queue.Queue ──▶ consumer ──▶ SQLite ──▶ Streamlit dashboard
 orders, drivers, geo       producer thread       dedupe, order-state     events, fact_orders,
 dup + out-of-order events                        assembly, metrics       driver_shifts, metrics_zone_hour
```

## Quick start (Windows)

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:PYTHONPATH = "src"
.\.venv\Scripts\python -m delivery_ops.cli run --days 7        # ~7 s
.\.venv\Scripts\streamlit run dashboard/app.py
.\.venv\Scripts\python -m pytest
```

Live mode: run the CLI with `--throttle 0.002` and turn on **Live refresh** in the dashboard sidebar.

## Event stream
`ORDER_PLACED → DRIVER_ASSIGNED → PICKED_UP → IN_TRANSIT (GPS ping every 4 min) → DELIVERED`, plus
`ORDER_CANCELLED` (customer gives up while waiting for a driver) and `DRIVER_ONLINE/OFFLINE`.
The stream has ~1% duplicate events and up to 45 s of arrival jitter; the consumer de-duplicates by
`event_id` and assembles orders regardless of arrival order.

## Simulation model (src/delivery_ops/config.py)
- 6 zones with geo-scattered restaurants/customers; Poisson demand with lunch (12–14) and dinner (19–21) peaks.
- Finite drivers per zone on staggered 8 h shifts (08–16, 12–20, 16–24), so supply is thin after 20:00.
- FIFO dispatch to the first-available driver; traffic slows drivers at peak.
- **Quoted ETA is deliberately naive** (prep + distance, no load/traffic) so its error grows at peaks.

## Metrics (src/delivery_ops/metrics.py), per date × zone × hour
| Metric | Definition |
|---|---|
| SLA breach rate | (delivered > 45 min after placement + cancelled) ÷ orders placed in that hour |
| ETA accuracy | MAE and bias of (actual − quoted) minutes; share within ±5 min |
| Driver utilization | driver-busy seconds (assigned → delivered) ÷ on-shift seconds in that hour |
| Bottleneck score | 0.4·utilization + 0.4·breach rate + 0.2·(dispatch wait ÷ 15 min) |

Metrics are stored as additive components, so any date/zone slice re-aggregates exactly.
