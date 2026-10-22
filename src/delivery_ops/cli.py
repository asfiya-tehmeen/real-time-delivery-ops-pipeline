"""CLI: `python -m delivery_ops.cli run --days 7`"""
import argparse
from datetime import date

from .pipeline import run_pipeline


def main() -> None:
    p = argparse.ArgumentParser(prog="delivery_ops")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="simulate events and run the pipeline")
    r.add_argument("--db", default="data/delivery_ops.db")
    r.add_argument("--start", default="2026-09-01", help="first simulated day (YYYY-MM-DD)")
    r.add_argument("--days", type=int, default=7)
    r.add_argument("--orders-per-day", type=float, default=900)
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--throttle", type=float, default=0.0,
                   help="seconds to sleep per event (e.g. 0.002) so the dashboard can follow live")
    r.add_argument("--refresh-every", type=int, default=2000, help="events between metric refreshes")
    args = p.parse_args()

    from pathlib import Path
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    s = run_pipeline(args.db, date.fromisoformat(args.start), args.days, args.orders_per_day,
                     args.seed, refresh_every=args.refresh_every, throttle_s=args.throttle)
    print(f"events={s.received} duplicates_dropped={s.duplicates} orders_closed={s.orders_closed} "
          f"metric_refreshes={s.refreshes} elapsed={s.elapsed_s:.1f}s -> {args.db}")


if __name__ == "__main__":
    main()
