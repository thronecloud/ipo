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
from engine.ingest.screener_enrich import enrich
from engine.backtest.study import DEFAULT_BENCHMARK, run_event_study
from engine.ingest.index_prices import refresh_index_prices
from engine.ingest.yf_refresh import backfill_prices, refresh
from engine.quality.audit import audit
from engine.quality.gapfill import ALL_TARGETS, gapfill
from engine.repo import job_run, reconcile_scores, recompute_scores_for_stock


def cmd_discover(a):
    print(discover_ipos(year=a.year, pages=a.pages, resolve=not a.no_resolve))


def cmd_refresh(a):
    kw = {}
    if a.status:
        kw["statuses"] = tuple(a.status)
    print(refresh(universe=a.universe, symbols=a.symbols, limit=a.limit, delay=a.delay, **kw))


def cmd_analyze(a):
    print(run_incremental(universe=a.universe, symbols=a.symbols, model=a.model,
                          force=a.force, limit=a.limit, delay=a.delay,
                          workers=a.workers))


def cmd_enrich(a):
    print(enrich(universe=a.universe, symbols=a.symbols, limit=a.limit, delay=a.delay))


def cmd_score(a):
    if a.reconcile:
        with job_run("score_reconcile", target="stale") as (session, stats):
            stats["rescored"] = reconcile_scores(session)
            print(f"[score] reconciled {stats['rescored']} stale/orphaned composites")
        return
    with job_run("score", target="all") as (session, stats):
        stocks = session.scalars(select(Stock)).all()
        made = 0
        for stock in stocks:
            if recompute_scores_for_stock(session, stock) is not None:
                made += 1
        session.commit()
        stats["scored"] = made
        print(f"[score] recomputed {made} composite scores")


def cmd_backfill(a):
    print(backfill_prices(universe=a.universe, symbols=a.symbols, limit=a.limit, delay=a.delay))


def cmd_dq_audit(a):
    print(audit(universe=a.universe, symbols=a.symbols, limit=a.limit))


def cmd_dq_fill(a):
    targets = tuple(a.targets) if a.targets else ALL_TARGETS
    print(gapfill(targets=targets, universe=a.universe, symbols=a.symbols,
                  limit=a.limit, delay=a.delay))


def cmd_reconcile(a):
    from engine.quality.reconcile import reconcile
    print(reconcile(limit=a.limit))


def cmd_extract(a):
    from engine.extract.run import run_extraction
    print(run_extraction(page_budget=a.page_budget, limit=a.limit, force=a.force))


def cmd_indexes(a):
    print(refresh_index_prices(symbols=a.symbols or None))


def cmd_backtest(a):
    s = SessionLocal()
    try:
        r = run_event_study(s, benchmark=a.benchmark)
    finally:
        s.close()

    def fmt(v):
        return "    —  " if v is None else f"{v:+6.2f}%"

    print(f"=== EVENT STUDY vs {r['benchmark']} ===")
    print(f"cohort {r['cohort_size']}  priced {r['priced']}  "
          f"unpriced {len(r['unpriced_symbols'])}")
    for title, key in (("confidence tier", "by_tier"),
                       ("LCB quintile (5=best)", "by_lcb_quintile"),
                       ("composite quintile (5=best)", "by_composite_quintile"),
                       ("recommendation", "by_recommendation")):
        print(f"\n-- by {title} --")
        for name, st in r[key].items():
            cells = "  ".join(
                f"{h}d {fmt(st['mean_excess'][h])} hit "
                f"{'—' if st['hit_rate'][h] is None else round(st['hit_rate'][h])}%"
                for h in r["horizons"]
            )
            print(f"  {str(name):<12} n={st['n']:>3}  {cells}")
    print("\n-- Spearman IC (score vs forward return) --")
    for k in ("composite", "lcb"):
        print(f"  {k:<10}",
              {h: (None if v is None else round(v, 3)) for h, v in r["ic"][k].items()})
    ranked = sorted((x for x in r["stocks"] if x["excess_to_date"] is not None),
                    key=lambda x: x["excess_to_date"], reverse=True)
    for label, rows in (("winners", ranked[:10]), ("losers", ranked[-10:])):
        print(f"\n-- top {label} (excess to date) --")
        for x in rows:
            print(f"  {x['symbol']:<14} comp {x['composite']:>3.0f}  lcb {x['lcb']:>3.0f}  "
                  f"{x['tier']:<11} {x['recommendation']:<5} "
                  f"excess {fmt(x['excess_to_date'])}")


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
    r.add_argument("--status", nargs="*", default=None,
                   help="Stock statuses to fetch (default: active new). Backfill uses: --status new")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--delay", type=float, default=2.0)
    r.set_defaults(func=cmd_refresh)

    an = sub.add_parser("analyze", help="Run incremental persona analysis")
    an.add_argument("--universe", default=None)
    an.add_argument("--symbols", nargs="*", default=None)
    an.add_argument("--model", default=None)
    an.add_argument("--limit", type=int, default=0)
    an.add_argument("--delay", type=float, default=2.0)
    an.add_argument("--force", action="store_true")
    an.add_argument("--workers", type=int, default=1,
                    help="concurrent persona calls per stock (10 = full council at once)")
    an.set_defaults(func=cmd_analyze)

    en = sub.add_parser("enrich", help="Scrape screener.in fundamentals into screener snapshots")
    en.add_argument("--universe", default=None)
    en.add_argument("--symbols", nargs="*", default=None)
    en.add_argument("--limit", type=int, default=0)
    en.add_argument("--delay", type=float, default=1.5)
    en.set_defaults(func=cmd_enrich)

    sc = sub.add_parser("score", help="Recompute composite scores")
    sc.add_argument("--reconcile", action="store_true",
                    help="only rescore stocks whose composite is stale/missing (heals orphans)")
    sc.set_defaults(func=cmd_score)

    bf = sub.add_parser("backfill", help="Backfill daily OHLCV prices (no snapshots)")
    bf.add_argument("--universe", default=None)
    bf.add_argument("--symbols", nargs="*", default=None)
    bf.add_argument("--limit", type=int, default=0)
    bf.add_argument("--delay", type=float, default=0.5)
    bf.set_defaults(func=cmd_backfill)

    da = sub.add_parser("dq_audit", help="Score every stock's data quality into data_quality_reports")
    da.add_argument("--universe", default=None)
    da.add_argument("--symbols", nargs="*", default=None)
    da.add_argument("--limit", type=int, default=0)
    da.set_defaults(func=cmd_dq_audit)

    df = sub.add_parser("dq_fill", help="Fill the gaps the latest audit flagged")
    df.add_argument("--targets", nargs="*", default=None,
                    help=f"subset of {list(ALL_TARGETS)} (default: all)")
    df.add_argument("--universe", default=None)
    df.add_argument("--symbols", nargs="*", default=None)
    df.add_argument("--limit", type=int, default=0)
    df.add_argument("--delay", type=float, default=None,
                    help="seconds between network requests (set 6-8 to avoid screener rate limits)")
    df.set_defaults(func=cmd_dq_fill)

    rc = sub.add_parser("reconcile", help="Cross-source reconciliation: flag divergences + score source trust")
    rc.add_argument("--limit", type=int, default=0)
    rc.set_defaults(func=cmd_reconcile)

    ex = sub.add_parser("extract", help="Extract text + statutory financials from downloaded filings")
    ex.add_argument("--page-budget", type=int, default=None)
    ex.add_argument("--limit", type=int, default=None)
    ex.add_argument("--force", action="store_true",
                    help="re-extract filings already extracted (idempotent replace)")
    ex.set_defaults(func=cmd_extract)

    ix = sub.add_parser("indexes", help="Refresh benchmark index price series")
    ix.add_argument("--symbols", nargs="*", default=None)
    ix.set_defaults(func=cmd_indexes)

    bt = sub.add_parser("backtest", help="Point-in-time event study of scored cohorts")
    bt.add_argument("--benchmark", default=DEFAULT_BENCHMARK)
    bt.set_defaults(func=cmd_backtest)

    stt = sub.add_parser("status", help="Show engine status")
    stt.set_defaults(func=cmd_status)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
