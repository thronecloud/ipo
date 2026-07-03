"""
The living-engine scheduler — an always-on process that keeps data fresh and
analysis current, without anyone running a script by hand.

Cadence (all configurable via env):
  - discover  weekly   : find new IPOs / stocks on screener
  - refresh   daily    : re-pull yfinance for the active universe (hash-gated)
  - analyze   hourly   : work through the analysis backlog in bounded batches
  - score     hourly   : recompute composites after each analyze batch

Run locally or on the cloud host:
    python -m engine.scheduler

Every job is wrapped in a JobRun row, so the admin dashboard sees the full history.
"""

import os

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from engine.analysis.engine import run_incremental
from engine.ingest.discover import discover_ipos
from engine.ingest.screener_enrich import enrich
from engine.ingest.yf_refresh import refresh
from engine.run import cmd_score


def _int(env, default):
    try:
        return int(os.environ.get(env, default))
    except ValueError:
        return default


# Bounded batch sizes so a single tick can't blow the Max-plan usage budget.
ANALYZE_BATCH = _int("SCHED_ANALYZE_BATCH", 20)
REFRESH_BATCH = _int("SCHED_REFRESH_BATCH", 100)
ENRICH_BATCH = _int("SCHED_ENRICH_BATCH", 50)


def job_discover():
    # APPEND-ONLY: scrapes screener's recent-IPO front pages and registers any
    # newly-listed company. Never mutates or removes existing stocks.
    print("[scheduler] discover (append-only)")
    discover_ipos(year=None, pages=3)


def job_refresh():
    print("[scheduler] refresh")
    refresh(limit=REFRESH_BATCH, verbose=False)


def job_enrich():
    print("[scheduler] screener enrich batch")
    enrich(limit=ENRICH_BATCH, verbose=False)


def job_analyze_and_score():
    print("[scheduler] analyze batch")
    run_incremental(limit=ANALYZE_BATCH, verbose=False)


def build_scheduler() -> BlockingScheduler:
    # Default to UTC so schedules are unambiguous across hosts.
    sched = BlockingScheduler(timezone=os.environ.get("SCHED_TZ", "UTC"))
    # Daily discovery at 14:00 UTC — APPEND-ONLY (adds new companies).
    sched.add_job(job_discover, CronTrigger(hour=14, minute=0), id="discover")
    # Daily data refresh at 02:00 UTC (hash-gated; a snapshot is added only when data changed).
    sched.add_job(job_refresh, CronTrigger(hour=2, minute=0), id="refresh")
    # Weekly screener enrichment (fundamentals move quarterly) — Sunday 03:00 UTC.
    sched.add_job(job_enrich, CronTrigger(day_of_week="sun", hour=3, minute=0), id="enrich")
    # Hourly analysis batch drains the backlog over time.
    sched.add_job(job_analyze_and_score, CronTrigger(minute=30), id="analyze")
    return sched


def main():
    sched = build_scheduler()
    print("Living engine scheduler starting. Jobs:")
    for job in sched.get_jobs():
        print(f"  - {job.id}: {job.trigger}")
    print("Ctrl-C to stop.")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("\nScheduler stopped.")


if __name__ == "__main__":
    main()
