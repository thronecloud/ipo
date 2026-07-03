"""
Data-quality benchmark: screener.in (live) vs yfinance (from today's stored snapshot).

The yfinance side is read from the StockSnapshot we already pulled today, so we do
NOT hammer yfinance again. Only screener is fetched live. Compares three axes:
  - VOLUME    : how many fields / statement periods each provides
  - ENRICHMENT: valuable fields unique to each source
  - FRESHNESS : most recent data point (quarter / price date)

Usage: python -m scripts.dq_benchmark [--n 5] [--symbols SYM1 SYM2 ...]
"""

import argparse
import random

from sqlalchemy import select

from db.base import SessionLocal
from db.models import Stock, StockSnapshot
from engine.repo import latest_snapshot, universe_contains
from src.fetch_screener_data import scrape_company_page

YF_KEYS = [
    "currentPrice", "marketCap", "trailingPE", "priceToBook", "returnOnEquity",
    "debtToEquity", "revenueGrowth", "earningsGrowth", "operatingMargins",
    "profitMargins", "dividendYield", "beta", "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
    "targetMeanPrice", "heldPercentInstitutions",
]


def yf_from_snapshot(snap):
    info = snap.info or {}
    fin = snap.financials or {}
    periods = []
    if fin:
        periods = sorted({str(p)[:10] for vals in fin.values() for p in vals.keys()})
    return {
        "info_fields": sum(1 for v in info.values() if v not in (None, "", [], {})),
        "key_coverage": [k for k in YF_KEYS if info.get(k) not in (None, "")],
        "annual_periods": periods,
        "price_last_date": (snap.history_summary or {}).get("last_date"),
        "captured_at": snap.captured_at,
        "data_quality": snap.data_quality,
    }


def pull_screener(symbol):
    for path in (f"https://www.screener.in/company/{symbol}/consolidated/",
                 f"https://www.screener.in/company/{symbol}/"):
        data, err = scrape_company_page(path)
        if data and (data.get("ratios") or data.get("profit_loss")):
            return data, path
    return None, None


def periods_of(section):
    if not section:
        return []
    return list(next(iter(section.values()), {}).keys())


def bench_one(stock, snap):
    symbol = stock.symbol
    yfd = yf_from_snapshot(snap)
    scr, url = pull_screener(symbol)

    print(f"\n{'='*74}\n{symbol}  —  {stock.company_name}   (yfinance snapshot: {yfd['data_quality']}, pulled {str(yfd['captured_at'])[:16]})\n{'='*74}")

    scr_ratios = len(scr.get("ratios", {})) if scr else 0
    pl = periods_of(scr.get("profit_loss")) if scr else []
    q = periods_of(scr.get("quarterly_results")) if scr else []
    bs = periods_of(scr.get("balance_sheet")) if scr else []
    cf = periods_of(scr.get("cash_flow")) if scr else []
    sh = bool(scr and scr.get("shareholding"))

    print("VOLUME")
    print(f"  yfinance : {yfd['info_fields']:3d} info fields | key metrics {len(yfd['key_coverage'])}/{len(YF_KEYS)} "
          f"| annual periods {len(yfd['annual_periods'])} | quarterly 0 (not captured)")
    print(f"  screener : {scr_ratios:3d} ratios       | P&L {len(pl)}y | quarterly {len(q)}q "
          f"| BS {len(bs)}y | CF {len(cf)}y | shareholding {'yes' if sh else 'no'}")

    scr_has = []
    if scr:
        r = scr.get("ratios", {})
        if r.get("ROCE"): scr_has.append("ROCE")
        if sh: scr_has.append("promoter/FII/DII holding")
        if q: scr_has.append(f"{len(q)}q quarterly results")
        if len(pl) >= 8: scr_has.append(f"{len(pl)}y history")
    yf_has = (["real-time price"] if yfd["price_last_date"] else []) + \
             [k for k in ("beta", "targetMeanPrice", "heldPercentInstitutions", "fiftyTwoWeekHigh")
              if k in yfd["key_coverage"]]
    print("ENRICHMENT")
    print(f"  yfinance strengths : {', '.join(yf_has) or '—'}")
    print(f"  screener strengths : {', '.join(scr_has) or '—'}")

    yf_fresh = yfd["annual_periods"][-1] if yfd["annual_periods"] else "—"
    scr_fresh = q[-1] if q else (pl[-1] if pl else "—")
    print("FRESHNESS (most recent fundamentals period)")
    print(f"  yfinance : price {yfd['price_last_date'] or '—'} | annual statements {yf_fresh}")
    print(f"  screener : {scr_fresh}")

    vol_yf = yfd["info_fields"] + len(yfd["annual_periods"])
    vol_scr = scr_ratios + len(pl) + len(q) + len(bs) + len(cf) + (5 if sh else 0)
    return {
        "symbol": symbol,
        "scr_reachable": scr is not None,
        "yf_key": len(yfd["key_coverage"]),
        "scr_quarters": len(q),
        "scr_promoter": sh,
        "vol_winner": "screener" if vol_scr > vol_yf else "yfinance",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--symbols", nargs="*", default=None)
    args = ap.parse_args()

    s = SessionLocal()
    if args.symbols:
        picks = args.symbols
    else:
        # small-caps that already have a FULL yfinance snapshot from today's pull
        full_ids = select(StockSnapshot.stock_id).where(StockSnapshot.data_quality == "full").distinct()
        syms = s.scalars(
            select(Stock.symbol).where(universe_contains("nse_smallcap"), Stock.id.in_(full_ids))
        ).all()
        picks = random.sample(syms, min(args.n, len(syms)))

    print(f"Benchmarking {len(picks)} small-caps (yfinance = today's snapshot, screener = live): {picks}")
    results = []
    for sym in picks:
        stock = s.scalar(select(Stock).where(Stock.symbol == sym))
        snap = latest_snapshot(s, stock.id, quality="full") or latest_snapshot(s, stock.id)
        results.append(bench_one(stock, snap))

    print(f"\n{'='*74}\nSUMMARY\n{'='*74}")
    n = len(results)
    print(f"  screener reachable        : {sum(r['scr_reachable'] for r in results)}/{n}")
    print(f"  more raw volume           : screener won {sum(r['vol_winner']=='screener' for r in results)}/{n}")
    print(f"  avg yfinance key coverage : {sum(r['yf_key'] for r in results)/n:.1f}/{len(YF_KEYS)}")
    print(f"  avg screener quarters     : {sum(r['scr_quarters'] for r in results)/n:.1f}")
    print(f"  screener promoter holding : {sum(r['scr_promoter'] for r in results)}/{n} stocks")
    s.close()


if __name__ == "__main__":
    main()
