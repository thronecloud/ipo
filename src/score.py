"""
Stage 3b: Compute composite scores from individual persona analyses.

Reads all data/analyses/{SYMBOL}/{persona}.json files, computes
weighted average scores, consensus recommendations, and writes
data/scores.json for the dashboard.
"""

import argparse
import os
from collections import Counter
from datetime import datetime

from src.utils import log, save_json, load_json, file_exists
from src.personas import get_persona_slugs

ANALYSES_DIR = "data/analyses"
STOCKS_DIR = "data/stocks"
OUTPUT_PATH = "data/scores.json"
ALL_PERSONAS = get_persona_slugs()


def compute_stock_score(symbol):
    """
    Compute composite score for a single stock from its persona analyses.
    Returns a dict with scores, or None if no analyses exist.
    """
    analysis_dir = os.path.join(ANALYSES_DIR, symbol)
    if not os.path.isdir(analysis_dir):
        return None

    persona_scores = {}
    recommendations = []
    analyses_found = 0

    for slug in ALL_PERSONAS:
        analysis_path = os.path.join(analysis_dir, f"{slug}.json")
        data = load_json(analysis_path)
        if not data or "analysis" not in data:
            continue

        analysis = data["analysis"]
        score = analysis.get("score")
        rec = analysis.get("recommendation")

        if score is not None:
            persona_scores[slug] = score
            analyses_found += 1

        if rec:
            recommendations.append(rec)

    if not persona_scores:
        return None

    # Composite score = SUM of persona scores (each 0-10, total 0-100)
    composite = sum(persona_scores.values())

    # Consensus recommendation = majority vote
    rec_counts = Counter(recommendations)
    consensus = rec_counts.most_common(1)[0][0] if rec_counts else "N/A"

    # Load stock data for additional info
    stock_data = load_json(os.path.join(STOCKS_DIR, f"{symbol}.json"))
    info = stock_data.get("info", {}) if stock_data else {}
    ipo_data = stock_data.get("ipo_data", {}) if stock_data else {}

    # Load screener data as fallback for price/mcap
    ipo_list = load_json("data/ipo_list.json")
    screener_entry = {}
    if ipo_list:
        for s in ipo_list.get("stocks", []):
            if s["symbol"] == symbol:
                screener_entry = s
                break

    # Current price: yfinance → screener.in fallback
    current_price = (
        info.get("currentPrice")
        or info.get("regularMarketPrice")
        or info.get("previousClose")
        or screener_entry.get("current_price")
    )

    # P/E: yfinance → compute from EPS if available
    pe_ratio = info.get("trailingPE")
    if pe_ratio is None and info.get("epsTrailingTwelveMonths") and current_price:
        eps = info["epsTrailingTwelveMonths"]
        if eps and eps > 0:
            pe_ratio = round(current_price / eps, 2)

    # Market cap
    market_cap_cr = None
    if info.get("marketCap"):
        market_cap_cr = round(info["marketCap"] / 1e7, 0)
    elif screener_entry.get("ipo_mcap_cr"):
        market_cap_cr = screener_entry["ipo_mcap_cr"]

    # Issue price
    issue_price = ipo_data.get("issue_price") or screener_entry.get("issue_price")

    # IPO return
    ipo_return_pct = None
    if current_price and issue_price:
        ipo_return_pct = round((current_price - issue_price) / issue_price * 100, 1)
    elif screener_entry.get("ipo_return_pct") is not None:
        ipo_return_pct = screener_entry["ipo_return_pct"]

    return {
        "symbol": symbol,
        "company_name": info.get("longName") or ipo_data.get("company_name") or screener_entry.get("company_name", symbol),
        "sector": info.get("sector", "Unknown"),
        "industry": info.get("industry", "Unknown"),
        "listing_date": ipo_data.get("listing_date") or screener_entry.get("listing_date"),
        "current_price": current_price,
        "market_cap": info.get("marketCap"),
        "market_cap_cr": market_cap_cr,
        "pe_ratio": pe_ratio,
        "roe": info.get("returnOnEquity"),
        "debt_to_equity": info.get("debtToEquity"),
        "revenue_growth": info.get("revenueGrowth"),
        "issue_price": issue_price,
        "ipo_return_pct": ipo_return_pct,
        "composite_score": round(composite, 1),
        "persona_scores": persona_scores,
        "consensus_recommendation": consensus,
        "recommendation_counts": dict(rec_counts),
        "analysis_coverage": analyses_found,
        "total_personas": len(ALL_PERSONAS),
        "data_quality": stock_data.get("data_quality", "unknown") if stock_data else "unknown",
    }


def main():
    parser = argparse.ArgumentParser(description="Compute composite scores from analyses")
    parser.add_argument("--output", default=OUTPUT_PATH, help="Output path")
    args = parser.parse_args()

    log("Computing composite scores from analyses...")

    # Find all symbols that have analysis directories
    if not os.path.isdir(ANALYSES_DIR):
        log("ERROR: No analyses directory found. Run analyze.py first.")
        return

    symbols = sorted([
        d for d in os.listdir(ANALYSES_DIR)
        if os.path.isdir(os.path.join(ANALYSES_DIR, d))
    ])

    log(f"Found {len(symbols)} symbols with analyses")

    scores = []
    for symbol in symbols:
        result = compute_stock_score(symbol)
        if result:
            # Compute IPO return
            if result["current_price"] and result["issue_price"]:
                result["ipo_return_pct"] = round(
                    (result["current_price"] - result["issue_price"]) / result["issue_price"] * 100, 1
                )
            scores.append(result)

    # Sort by composite score descending
    scores.sort(key=lambda s: s["composite_score"], reverse=True)

    output = {
        "computed_at": datetime.utcnow().isoformat() + "Z",
        "total_stocks": len(scores),
        "scoring_method": "sum_of_persona_scores_each_0_to_10",
        "personas": ALL_PERSONAS,
        "stocks": scores,
    }

    save_json(args.output, output)
    log(f"Saved scores for {len(scores)} stocks to {args.output}")

    # Print top 10
    if scores:
        log("\nTop 10 by composite score:")
        for i, s in enumerate(scores[:10]):
            log(f"  {i+1:2d}. {s['symbol']:20s} Score: {s['composite_score']:5.1f}  Rec: {s['consensus_recommendation']:5s}  Sector: {s['sector']}")

        log(f"\nBottom 5:")
        for s in scores[-5:]:
            log(f"      {s['symbol']:20s} Score: {s['composite_score']:5.1f}  Rec: {s['consensus_recommendation']:5s}")

        # Score distribution
        brackets = {"70-100": 0, "50-69": 0, "30-49": 0, "10-29": 0, "0-9": 0}
        for s in scores:
            sc = s["composite_score"]
            if sc >= 70: brackets["70-100"] += 1
            elif sc >= 50: brackets["50-69"] += 1
            elif sc >= 30: brackets["30-49"] += 1
            elif sc >= 10: brackets["10-29"] += 1
            else: brackets["0-9"] += 1

        log(f"\nScore distribution:")
        for bracket, count in brackets.items():
            bar = "#" * count
            log(f"  {bracket}: {count:3d} {bar}")


if __name__ == "__main__":
    main()
