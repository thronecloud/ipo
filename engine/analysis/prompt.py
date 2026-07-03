"""Build the per-stock user prompt from a DB snapshot (reuses the proven formatters)."""

from src.analyze import build_financial_summary, format_currency
from src.personas import ANALYSIS_PROMPT_TEMPLATE


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


def build_user_prompt(stock, snap) -> str:
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

    return ANALYSIS_PROMPT_TEMPLATE.format(
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
