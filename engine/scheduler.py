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


def job_discover():
    print("[scheduler] discover")
    discover_ipos(year=None, pages=5)


def job_refresh():
    print("[scheduler] refresh")
    refresh(limit=REFRESH_BATCH, verbose=False)


def job_analyze_and_score():
    print("[scheduler] analyze batch")
    run_incremental(limit=ANALYZE_BATCH, verbose=False)


def build_scheduler() -> BlockingScheduler:
    sched = BlockingScheduler(timezone=os.environ.get("SCHED_TZ", "Asia/Kolkata"))
    # Weekly discovery — Monday 06:00
    sched.add_job(job_discover, CronTrigger(day_of_week="mon", hour=6, minute=0), id="discover")
    # Daily refresh — 07:00
    sched.add_job(job_refresh, CronTrigger(hour=7, minute=0), id="refresh")
    # Hourly analysis batch (drains the backlog over time)
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
