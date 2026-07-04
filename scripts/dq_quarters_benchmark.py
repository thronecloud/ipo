"""
Quarterly data-quality benchmark — are we missing quarters?

For N random stocks (that have a yfinance snapshot), compares quarterly coverage
across three sources, IN PARALLEL:

  - yf_stored   : quarters in our latest yfinance snapshot (quarterly_financials)
  - scr_stored  : quarters in our latest screener snapshot (quarterly_results)
  - scr_live    : quarters screener.in serves right now (live scrape)

Findings surfaced:
  - source coverage (avg quarters, zero-coverage counts)
  - MISSING vs source: stocks where scr_live has more quarters than scr_stored
    (we captured stale/partial screener data) — the user's "missing quarters" hunch
  - yfinance sparsity + internal GAPS (non-contiguous quarters, e.g. missing Sep)

Screener is fetched live (rate-limited via a small thread pool); yfinance is read
from the DB snapshot (no extra Yahoo calls). Writes a markdown report.

Usage:  python scripts/dq_quarters_benchmark.py -n 70 [--seed 7]
"""

import argparse
import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select

from db.base import SessionLocal
from db.models import Stock
from engine.repo import latest_snapshot
from src.fetch_screener_data import scrape_company_page

MONTHS = {"mar": 1, "jun": 2, "sep": 3, "dec": 4}  # fiscal-quarter ordinal within a year


def periods_of(section: dict) -> list:
    """Period columns of a {metric: {period: value}} statement block."""
    if not section:
        return []
    return list(next(iter(section.values()), {}).keys())


def _yf_quarters(snap) -> list:
    """yfinance quarter-end dates '2025-03-31 ...' -> fiscal 'YYYYQn' tokens."""
    qf = (snap.quarterly_financials or {}) if snap else {}
    out = set()
    for p in periods_of(qf):
        m = re.match(r"(\d{4})-(\d{2})", str(p))
        if m:
            yr, mo = int(m.group(1)), int(m.group(2))
            out.add(f"{yr}Q{(mo + 2) // 3}")  # 03->Q1 06->Q2 09->Q3 12->Q4
    return sorted(out)


def _scr_quarters(section) -> list:
    """Screener periods look like 'Jun 2023'. Return sorted 'YYYYQn' tokens."""
    out = []
    for p in periods_of(section):
        m = re.match(r"([A-Za-z]{3})\s+(\d{4})", str(p))
        if m:
            mon, yr = m.group(1).lower(), int(m.group(2))
            if mon in MONTHS:
                out.append(f"{yr}Q{MONTHS[mon]}")
    return sorted(set(out))


def _gaps(quarters: list) -> int:
    """Count missing quarters inside the observed span (non-contiguity). Accepts
    'YYYYQn' tokens; ignores anything that doesn't match."""
    ords = []
    for q in quarters:
        m = re.match(r"(\d{4})Q([1-4])", str(q))
        if m:
            ords.append(int(m.group(1)) * 4 + int(m.group(2)))
    if len(ords) < 2:
        return 0
    ords.sort()
    return (ords[-1] - ords[0] + 1) - len(ords)


def pull_screener_live(symbol: str, url: str | None):
    """Defensive live scrape — tolerates any return shape / exception (screener
    rate-limits aggressively, especially under concurrency)."""
    for path in ([url] if url else []) + [
        f"https://www.screener.in/company/{symbol}/consolidated/",
        f"https://www.screener.in/company/{symbol}/",
    ]:
        if not path:
            continue
        try:
            res = scrape_company_page(path)
            data = res[0] if isinstance(res, tuple) else res
        except Exception:
            continue
        if data and (data.get("quarterly_results") or data.get("profit_loss")):
            return data
    return None


def bench_stored(symbol: str) -> dict:
    """DB-only quarterly coverage (parallel-safe — own session, no network)."""
    s = SessionLocal()
    try:
        st = s.scalar(select(Stock).where(Stock.symbol == symbol))
        yf = latest_snapshot(s, st.id, source="yfinance")
        scr = latest_snapshot(s, st.id, source="screener")
        yf_q = _yf_quarters(yf)
        scr_stored_q = _scr_quarters((scr.screener or {}).get("quarterly_results")) if scr and scr.screener else []
        return {
            "symbol": symbol,
            "yf_n": len(yf_q),
            "yf_gaps": _gaps(yf_q),
            "scr_stored_n": len(scr_stored_q),
            "scr_stored_q": scr_stored_q,
            "has_screener_snap": bool(scr),
            "screener_url": st.screener_url,
        }
    except Exception as e:
        return {"symbol": symbol, "error": str(e)[:120]}
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=70)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--live", type=int, default=15, help="stocks to live-check on screener")
    ap.add_argument("--out", default=".gstack/qa-reports/dq-quarters-benchmark.md")
    args = ap.parse_args()

    random.seed(args.seed)
    s = SessionLocal()
    # Only stocks that actually have a yfinance snapshot are meaningful to benchmark.
    candidates = []
    for st in s.scalars(select(Stock)).all():
        if latest_snapshot(s, st.id, source="yfinance"):
            candidates.append(st.symbol)
    s.close()
    picks = random.sample(candidates, min(args.n, len(candidates)))
    print(f"[1/2] STORED audit — {len(picks)} random stocks, DB-only, {args.workers} parallel workers...")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(bench_stored, sym): sym for sym in picks}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            tag = f"err {r['error']}" if "error" in r else (
                f"yf={r['yf_n']}q(gap{r['yf_gaps']}) scr_stored={r['scr_stored_n']}q"
                + ("" if r["has_screener_snap"] else "  [no screener snap]"))
            print(f"  [{i}/{len(picks)}] {r['symbol']:12} {tag}")

    ok = [r for r in results if "error" not in r]

    # [2/2] Live screener staleness spot-check — SEQUENTIAL + delayed (screener
    # blocks parallel access). Sample stocks that DO have a screener snapshot.
    import time
    live_subset = [r for r in ok if r["has_screener_snap"]][: args.live]
    print(f"\n[2/2] LIVE screener staleness check — {len(live_subset)} stocks, sequential (1.5s delay)...")
    for r in live_subset:
        live = pull_screener_live(r["symbol"], r.get("screener_url"))
        live_q = _scr_quarters((live or {}).get("quarterly_results")) if live else []
        r["scr_live_n"] = len(live_q)
        r["scr_missing"] = max(0, len(live_q) - r["scr_stored_n"])
        print(f"  {r['symbol']:12} stored={r['scr_stored_n']}q live={r['scr_live_n']}q"
              + (f"  MISSING {r['scr_missing']}q" if r["scr_missing"] else ""))
        time.sleep(1.5)

    n = len(ok) or 1
    yf_zero = sum(1 for r in ok if r["yf_n"] == 0)
    scr_stored_zero = sum(1 for r in ok if r["scr_stored_n"] == 0)
    no_scr_snap = sum(1 for r in ok if not r["has_screener_snap"])
    yf_gappy = [r for r in ok if r["yf_gaps"] > 0]
    yf_lt_scr = [r for r in ok if r["scr_stored_n"] > r["yf_n"]]
    missing = [r for r in live_subset if r.get("scr_missing", 0) > 0]
    live_checked = len(live_subset) or 1
    avg_yf = sum(r["yf_n"] for r in ok) / n
    avg_scr_stored = sum(r["scr_stored_n"] for r in ok) / n
    avg_scr_live = sum(r.get("scr_live_n", 0) for r in live_subset) / live_checked

    lines = []
    P = lines.append
    P(f"# Quarterly Data-Quality Benchmark ({len(ok)} stocks, seed {args.seed})\n")
    P("Are we missing quarters? Compares stored yfinance vs stored screener quarterly "
      "coverage (all sampled stocks), plus a live screener staleness spot-check.\n")
    P("## Headline\n")
    P(f"- **avg quarters stored — yfinance: {avg_yf:.1f}q · screener: {avg_scr_stored:.1f}q** "
      f"(live screener on the {live_checked}-stock spot-check: {avg_scr_live:.1f}q)")
    P(f"- yfinance is the sparse source: **{yf_zero}/{len(ok)}** have ZERO quarterly rows; "
      f"**{len(yf_gappy)}/{len(ok)}** have internal gaps (missing quarters inside their span)")
    P(f"- **screener beats yfinance on {len(yf_lt_scr)}/{len(ok)} stocks** for quarterly depth "
      f"(this is why screener is the quarterly source of truth)")
    P(f"- screener coverage: **{scr_stored_zero}/{len(ok)}** stored-empty, "
      f"**{no_scr_snap}/{len(ok)}** have NO screener snapshot at all")
    P(f"- staleness spot-check: **{len(missing)}/{live_checked} stocks** where screener.in now "
      f"serves more quarters than we stored (total {sum(r['scr_missing'] for r in missing)} uncaptured)\n")

    P("## Missing screener snapshots (stocks with no enrichment yet)\n")
    no_snap = [r for r in ok if not r["has_screener_snap"]]
    P(f"{len(no_snap)} of {len(ok)} sampled: " + ", ".join(r["symbol"] for r in no_snap[:40]) or "none")
    P("")
    P("## Live staleness — uncaptured screener quarters\n")
    P("| symbol | yf stored | scr stored | scr live | missing |")
    P("|---|--:|--:|--:|--:|")
    for r in sorted(missing, key=lambda r: -r["scr_missing"])[:20]:
        P(f"| {r['symbol']} | {r['yf_n']} | {r['scr_stored_n']} | {r['scr_live_n']} | **{r['scr_missing']}** |")

    P("\n## yfinance internal gaps (non-contiguous quarters)\n")
    P("| symbol | yf quarters | gaps |")
    P("|---|--:|--:|")
    for r in sorted(yf_gappy, key=lambda r: -r["yf_gaps"])[:20]:
        P(f"| {r['symbol']} | {r['yf_n']} | {r['yf_gaps']} |")

    errs = [r for r in results if "error" in r]
    if errs:
        P(f"\n## Errors ({len(errs)})\n")
        for r in errs[:15]:
            P(f"- {r['symbol']}: {r['error']}")

    report = "\n".join(lines)
    import os
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(report + "\n")
    print("\n" + report)
    print(f"\n[report written to {args.out}]")


if __name__ == "__main__":
    main()
