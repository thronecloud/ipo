"""
One-time backfill: register 2024 IPOs, then fetch data for every `new` stock
(2026 + 2024) from yfinance. Data-only — no Claude analysis is run here.

Idempotent: discovery skips known symbols; fetch is content-hash-gated.
Safe to re-run.
"""

from engine.ingest.discover import discover_ipos
from engine.ingest.yf_refresh import refresh


def main():
    print(">>> STEP 1: discover 2024 IPOs (deep scrape to reach 2024 listings)")
    d = discover_ipos(year=2024, pages=30, resolve=True, verbose=True)
    print("discover 2024 result:", {k: v for k, v in d.items() if k != "new_symbols"})

    print("\n>>> STEP 2: fetch ALL new stocks via yfinance (promotes new -> active)")
    r = refresh(statuses=("new",), delay=1.5, verbose=True)
    print("fetch result:", r)

    print("\n>>> BACKFILL COMPLETE")


if __name__ == "__main__":
    main()
