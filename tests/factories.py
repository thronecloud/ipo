"""Shared builders for test data (imported by test modules, not a test file)."""

from datetime import datetime, timedelta, timezone

from db.models import Analysis, Stock


def utc(offset_minutes: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)


def full_info(price: float = 100.0, **overrides) -> dict:
    info = {
        "currentPrice": price,
        "regularMarketPrice": price,
        "previousClose": price - 1,
        "marketCap": 50_000_000_000.0,
        "trailingPE": 25.5,
        "returnOnEquity": 0.18,
        "debtToEquity": 30.0,
        "revenueGrowth": 0.22,
        "longBusinessSummary": "A test company.",
    }
    info.update(overrides)
    return info


def yf_payload(price: float = 100.0, revenue: float = 1_000.0) -> dict:
    """Payload in the shape fetch_payload/add_snapshot expect."""
    return {
        "info": full_info(price=price),
        "financials": {"2025-03-31": {"Total Revenue": revenue}},
        "quarterly_financials": {"2025-03-31": {"Total Revenue": revenue / 4}},
        "balance_sheet": {"2025-03-31": {"Total Assets": 2 * revenue}},
        "cashflow": {"2025-03-31": {"Free Cash Flow": revenue / 10}},
        "history_summary": {"last_close": price, "total_days": 300},
    }


def make_stock(session, symbol: str, *, status: str = "active",
               universe: list | None = None, **kwargs) -> Stock:
    stock = Stock(
        symbol=symbol,
        yf_symbol=kwargs.pop("yf_symbol", f"{symbol}.NS"),
        status=status,
        universe=universe or [],
        **kwargs,
    )
    session.add(stock)
    session.commit()
    return stock


def make_analysis(session, stock, persona: str, score: int | None,
                  recommendation: str | None = None, *,
                  data_hash: str | None = None, snapshot_id: int | None = None,
                  analyzed_at: datetime | None = None) -> Analysis:
    row = Analysis(
        stock_id=stock.id,
        persona=persona,
        score=score,
        recommendation=recommendation,
        data_hash=data_hash,
        snapshot_id=snapshot_id,
        analyzed_at=analyzed_at or utc(),
        model="test-model",
        prompt_version="test",
    )
    session.add(row)
    session.commit()
    return row
