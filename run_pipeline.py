"""
Pipeline orchestrator: runs stages 1-3 sequentially.

Usage:
    python run_pipeline.py                    # Run all stages
    python run_pipeline.py --stages 1,2       # Run specific stages
    python run_pipeline.py --stages 3 --limit 5  # Analyze first 5 stocks
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def run_stage(name, cmd):
    """Run a pipeline stage as a subprocess."""
    log(f"{'=' * 60}")
    log(f"STAGE: {name}")
    log(f"CMD:   {' '.join(cmd)}")
    log(f"{'=' * 60}")

    start = time.time()
    result = subprocess.run(cmd, text=True)
    elapsed = time.time() - start

    if result.returncode != 0:
        log(f"STAGE FAILED: {name} (exit code {result.returncode}) in {elapsed:.1f}s")
        return False

    log(f"STAGE COMPLETE: {name} in {elapsed:.1f}s")
    return True


def main():
    parser = argparse.ArgumentParser(description="Run IPO analysis pipeline")
    parser.add_argument("--stages", default="1,2,3,4", help="Comma-separated stage numbers (default: 1,2,3,4)")
    parser.add_argument("--force", action="store_true", help="Force re-fetch/re-analyze")
    parser.add_argument("--limit", type=int, default=0, help="Limit stocks for stages 2-3")
    parser.add_argument("--model", default="opus", help="Claude model for analysis (default: opus)")
    parser.add_argument("--year", type=int, default=2025, help="IPO year (default: 2025)")
    parser.add_argument("--pages", type=int, default=25, help="Screener.in pages to scrape (default: 25)")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between Claude calls (default: 2.0)")
    args = parser.parse_args()

    stages = [int(s.strip()) for s in args.stages.split(",")]
    force_flag = ["--force"] if args.force else []
    limit_flag = ["--limit", str(args.limit)] if args.limit else []

    log(f"Pipeline starting: stages={stages}, force={args.force}, limit={args.limit}, model={args.model}")
    start_time = time.time()
    failed = []

    # Stage 1: Fetch IPO list
    if 1 in stages:
        cmd = [sys.executable, "-m", "src.fetch_ipo_list",
               "--year", str(args.year), "--pages", str(args.pages)] + force_flag
        if not run_stage("Fetch IPO List (screener.in)", cmd):
            failed.append(1)

    # Stage 2: Fetch stock data
    if 2 in stages:
        cmd = [sys.executable, "-m", "src.fetch_stock_data"] + force_flag + limit_flag
        if not run_stage("Fetch Stock Data (yfinance)", cmd):
            failed.append(2)

    # Stage 3: AI persona analysis
    if 3 in stages:
        cmd = [sys.executable, "-m", "src.analyze",
               "--model", args.model, "--delay", str(args.delay)] + force_flag + limit_flag
        if not run_stage("AI Persona Analysis (Claude)", cmd):
            failed.append(3)

    # Stage 4: Compute scores — the DB engine is the ONLY scorer (mean×10).
    # The legacy JSON SUM-scorer is retired: it used a different formula and
    # produced contradictory composites.
    if 4 in stages:
        cmd = [sys.executable, "-m", "engine.run", "score"]
        if not run_stage("Compute Composite Scores (DB engine)", cmd):
            failed.append(4)

    elapsed = time.time() - start_time
    log(f"\n{'=' * 60}")
    log(f"PIPELINE COMPLETE in {elapsed:.1f}s ({elapsed/60:.1f} min)")
    if failed:
        log(f"FAILED STAGES: {failed}")
    else:
        log(f"ALL STAGES PASSED")
    log(f"{'=' * 60}")

    if not failed:
        log(f"\nDashboard: docker compose up -d  →  http://localhost:3000")


if __name__ == "__main__":
    main()
