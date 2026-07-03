"""
Engine command line — run any job once.

    python -m engine.run status
    python -m engine.run discover --year 2026 --pages 5
    python -m engine.run refresh  --universe ipo_2025 --limit 50
    python -m engine.run analyze  --universe ipo_2025 --limit 20
    python -m engine.run score

The scheduler (engine/scheduler.py) calls the same functions on a cadence.
"""

import argparse

from sqlalchemy import func, select

from db.base import SessionLocal
from db.models import Analysis, CompositeScore, JobRun, Stock, StockSnapshot
from engine.analysis.engine import run_incremental
from engine.ingest.discover import discover_ipos
from engine.ingest.yf_refresh import refresh
from engine.repo import job_run, recompute_scores_for_stock


def cmd_discover(a):
    print(discover_ipos(year=a.year, pages=a.pages, resolve=not a.no_resolve))


def cmd_refresh(a):
    print(refresh(universe=a.universe, symbols=a.symbols, limit=a.limit, delay=a.delay))


def cmd_analyze(a):
    print(run_incremental(universe=a.universe, model=a.model, force=a.force,
                          limit=a.limit, delay=a.delay))


def cmd_score(a):
    with job_run("score", target="all") as (session, stats):
        stocks = session.scalars(select(Stock)).all()
        made = 0
        for stock in stocks:
            if recompute_scores_for_stock(session, stock) is not None:
                made += 1
        session.commit()
        stats["scored"] = made
        print(f"[score] recomputed {made} composite scores")


def cmd_status(a):
    s = SessionLocal()
    print("=== ENGINE STATUS ===")
    print(f"  stocks           : {s.query(Stock).count()}")
    print(f"  stock_snapshots  : {s.query(StockSnapshot).count()}")
    print(f"  analyses         : {s.query(Analysis).count()}")
    print(f"  composite_scores : {s.query(CompositeScore).count()}")

    # universe distribution
    from collections import Counter
    tags = Counter()
    for (u,) in s.execute(select(Stock.universe)).all():
        for t in (u or ["<none>"]):
            tags[t] += 1
    print(f"  universe         : {dict(tags)}")

    # status breakdown
    st = dict(s.execute(select(Stock.status, func.count()).group_by(Stock.status)).all())
    print(f"  stock status     : {st}")

    print("\n  recent jobs:")
    for j in s.query(JobRun).order_by(JobRun.id.desc()).limit(8).all():
        dur = ""
        if j.finished_at and j.started_at:
            dur = f" ({(j.finished_at - j.started_at).total_seconds():.0f}s)"
        print(f"    #{j.id} {j.job_type:16s} {j.status:8s}{dur}  {j.stats or j.error or ''}")
    s.close()


def main():
    p = argparse.ArgumentParser(description="IPO Analyzer living engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="Scrape screener for new IPO stocks")
    d.add_argument("--year", type=int, default=None)
    d.add_argument("--pages", type=int, default=5)
    d.add_argument("--no-resolve", action="store_true")
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("refresh", help="Refresh stock data from yfinance (hash-gated)")
    r.add_argument("--universe", default=None)
    r.add_argument("--symbols", nargs="*", default=None)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--delay", type=float, default=2.0)
    r.set_defaults(func=cmd_refresh)

    an = sub.add_parser("analyze", help="Run incremental persona analysis")
    an.add_argument("--universe", default=None)
    an.add_argument("--model", default=None)
    an.add_argument("--limit", type=int, default=0)
    an.add_argument("--delay", type=float, default=2.0)
    an.add_argument("--force", action="store_true")
    an.set_defaults(func=cmd_analyze)

    sc = sub.add_parser("score", help="Recompute composite scores")
    sc.set_defaults(func=cmd_score)

    stt = sub.add_parser("status", help="Show engine status")
    stt.set_defaults(func=cmd_status)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
