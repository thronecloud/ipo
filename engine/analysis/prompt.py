"""Build the per-stock user prompt from a DB snapshot (reuses the proven formatters)."""

import re
from datetime import datetime, timezone

from src.analyze import build_financial_summary, format_currency
from src.personas import ANALYSIS_PROMPT_TEMPLATE

# Personas were burning output tokens lamenting data gaps on recent listings and
# defaulting scores toward zero for what is simply recency. This block reframes
# the gaps once, up front, so the analysis spends itself on what IS visible.
RECENT_IPO_CONTEXT = """
IMPORTANT CONTEXT — RECENTLY LISTED COMPANY:
This company listed on the exchange only recently. Sparse data is EXPECTED:
few or no historical quarters, short price history, missing ratios, thin
coverage. Treat those gaps as artifacts of recency, not as red flags in
themselves, and do not spend your analysis enumerating what is unavailable.
Score the company on what IS visible — issue pricing vs current price, sector
economics, promoter/management quality, the quarters that do exist, screener
fundamentals — and let the limited history temper your CONVICTION language,
not drag the score toward zero by default.
"""

# A listing (or universe vintage) within this many years counts as recent.
RECENT_YEARS = 2
_EVERGREEN_RECENT_TAGS = {"ipo_recent", "ipo_upcoming"}


def _is_recent_ipo(stock) -> bool:
    cutoff = datetime.now(timezone.utc).year - RECENT_YEARS
    tags = set(stock.universe or [])
    if tags & _EVERGREEN_RECENT_TAGS:
        return True
    for tag in tags:
        m = re.fullmatch(r"ipo_(\d{4})", tag)
        if m and int(m.group(1)) >= cutoff:
            return True
    m = re.search(r"(19|20)\d{2}", stock.listing_date or "")
    return bool(m and int(m.group(0)) >= cutoff)


def snapshot_to_stock_data(stock, snap) -> dict:
    """Reconstruct the dict shape that build_financial_summary expects."""
    return {
        "info": snap.info or {},
        "financials": snap.financials or {},
        "balance_sheet": snap.balance_sheet or {},
        "cashflow": snap.cashflow or {},
        "history_summary": snap.history_summary or {},
        "ipo_data": snap.ipo_data or {},
        "yf_symbol": stock.yf_symbol or "",
        "data_quality": snap.data_quality,
        "fetch_status": snap.fetch_status,
    }


def _last_n(series: dict, n=5):
    return list(series.items())[-n:]


def format_screener(sc: dict) -> str:
    """Concise multi-year / quarterly / ROCE / shareholding block from a screener scrape."""
    if not sc:
        return ""
    lines = []
    ratios = sc.get("ratios", {})
    if ratios:
        keep = ["Stock P/E", "ROCE", "ROE", "Debt to equity", "Dividend Yield",
                "Book Value", "Market Cap", "Current Price", "High / Low"]
        r = [f"{k}: {ratios[k]}" for k in keep if k in ratios]
        if r:
            lines.append("Key ratios — " + " | ".join(r))
    pl = sc.get("profit_loss", {})
    for metric in ["Sales", "Revenue", "Net Profit", "Operating Profit", "OPM %", "EPS in Rs"]:
        if metric in pl and pl[metric]:
            pts = _last_n(pl[metric], 5)
            lines.append(f"  {metric} (yearly): " + " | ".join(f"{p}:{v}" for p, v in pts))
    q = sc.get("quarterly_results", {})
    for metric in ["Sales", "Net Profit"]:
        if metric in q and q[metric]:
            pts = _last_n(q[metric], 4)
            lines.append(f"  {metric} (quarterly): " + " | ".join(f"{p}:{v}" for p, v in pts))
    sh = sc.get("shareholding", {})
    if sh:
        latest = {}
        for grp in ["Promoters", "FIIs", "DIIs", "Public", "Government"]:
            if sh.get(grp):
                per, val = list(sh[grp].items())[-1]
                latest[grp] = val
        if latest:
            lines.append("  Shareholding (latest): " + " | ".join(f"{g} {v}%" for g, v in latest.items()))
    if not lines:
        return ""
    return "\n\n=== SCREENER FUNDAMENTALS (multi-year history) ===\n" + "\n".join(lines)


def build_user_prompt(stock, snap, screener_snap=None) -> str:
    sd = snapshot_to_stock_data(stock, snap)
    info = sd["info"]
    ipo = sd["ipo_data"]

    current_price = info.get("currentPrice") or info.get("regularMarketPrice") or "N/A"
    issue_price = ipo.get("issue_price") or stock.issue_price

    ipo_return = "N/A"
    if ipo.get("ipo_return_pct") is not None:
        ipo_return = f"{ipo.get('ipo_return_pct')}%"
    elif isinstance(current_price, (int, float)) and issue_price:
        ipo_return = f"{round((current_price - issue_price) / issue_price * 100, 1)}%"

    base = ANALYSIS_PROMPT_TEMPLATE.format(
        company_name=info.get("longName") or ipo.get("company_name") or stock.company_name or stock.symbol,
        symbol=stock.symbol,
        exchange=stock.exchange or ("NSE" if (stock.yf_symbol or "").endswith(".NS") else "BSE"),
        sector=info.get("sector") or stock.sector or "Unknown",
        industry=info.get("industry") or stock.industry or "Unknown",
        listing_date=ipo.get("listing_date") or stock.listing_date or "Unknown",
        issue_price=issue_price if issue_price is not None else "N/A",
        current_price=current_price,
        market_cap_display=format_currency(info.get("marketCap")),
        ipo_return_pct=ipo_return,
        financial_summary=build_financial_summary(sd),
    )
    if _is_recent_ipo(stock):
        base += RECENT_IPO_CONTEXT
    return base + format_screener(screener_snap.screener if screener_snap else None)
