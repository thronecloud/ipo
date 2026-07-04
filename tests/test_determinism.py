"""P1-4: every 'latest' query is totally ordered — an id tiebreak resolves timestamp ties."""

from sqlalchemy import select

from db.models import StockSnapshot
from engine.repo import add_snapshot, extract_columns, latest_snapshot
from factories import make_stock, utc, yf_payload


def test_latest_snapshot_tiebreak_is_deterministic(db_session):
    """Two snapshots with the SAME captured_at -> latest_snapshot returns the higher id,
    stably across many runs (no arbitrary DB row order)."""
    stock = make_stock(db_session, "TIE1")
    ts = utc(-1)
    # two distinct snapshots (different payloads -> different content_hash) same timestamp
    p1 = yf_payload(price=100.0)
    p2 = yf_payload(price=101.0)
    s1, _ = add_snapshot(db_session, stock, p1, extract_columns(p1["info"]),
                         data_quality="full", captured_at=ts)
    s2, _ = add_snapshot(db_session, stock, p2, extract_columns(p2["info"]),
                         data_quality="full", captured_at=ts)
    db_session.commit()
    winner_id = max(s1.id, s2.id)
    for _ in range(50):
        got = latest_snapshot(db_session, stock.id, source="yfinance")
        assert got.id == winner_id


def test_content_hash_ignores_volatile_quote_fields():
    """M3: two payloads differing ONLY in intraday quote/volume hash equal — a stable
    business doesn't mint a new snapshot every day; a fundamentals change still does."""
    from engine.repo import compute_content_hash
    base = {
        "info": {"sector": "Tech", "currentPrice": 100.0, "volume": 5000, "marketCap": 1e9},
        "financials": {"2025": {"rev": 100}},
        "history_summary": {"last_close": 100.0, "avg_volume_30d": 4000},
    }
    quote_moved = {
        "info": {"sector": "Tech", "currentPrice": 999.0, "volume": 12, "marketCap": 2e9},
        "financials": {"2025": {"rev": 100}},
        "history_summary": {"last_close": 999.0, "avg_volume_30d": 9},
    }
    assert compute_content_hash(base) == compute_content_hash(quote_moved)
    # a real fundamentals change must still change the hash
    fundamentals_changed = {**base, "financials": {"2025": {"rev": 200}}}
    assert compute_content_hash(base) != compute_content_hash(fundamentals_changed)


def test_add_snapshot_unchanged_when_only_price_moves(db_session):
    from engine.repo import add_snapshot, extract_columns
    from factories import yf_payload
    stock = make_stock(db_session, "HASHSTAB")
    p1 = yf_payload(price=100.0)
    _, created1 = add_snapshot(db_session, stock, p1, extract_columns(p1["info"]), data_quality="full")
    db_session.commit()
    p2 = yf_payload(price=250.0)  # only the quote moved; fundamentals identical
    _, created2 = add_snapshot(db_session, stock, p2, extract_columns(p2["info"]), data_quality="full")
    db_session.commit()
    assert created1 is True and created2 is False  # no churn on a price-only change


def test_latest_snapshot_prefers_newest_timestamp(db_session):
    stock = make_stock(db_session, "TIE2")
    older = yf_payload(price=50.0)
    newer = yf_payload(price=60.0)
    add_snapshot(db_session, stock, older, extract_columns(older["info"]),
                 data_quality="full", captured_at=utc(-10))
    s_new, _ = add_snapshot(db_session, stock, newer, extract_columns(newer["info"]),
                            data_quality="full", captured_at=utc(-1))
    db_session.commit()
    assert latest_snapshot(db_session, stock.id, source="yfinance").id == s_new.id
