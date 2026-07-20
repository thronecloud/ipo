"""Screener as identity/mcap fallback for fresh listings.

yfinance carries ~nothing for recently listed names (no sector, no marketCap),
and AMFI only classifies established stocks — so 2026 IPOs rendered blank in
Discovery. Screener.in has all of it: the peers section carries the full
sector→industry classification and top ratios carry Market Cap (₹ Cr) and
Current Price. Three closures under test:

1. scrape_company_page captures `classification` (peers-section link texts).
2. _fill_identity falls back to screener classification for sector/industry.
3. The consumer API falls back to screener ratios for market cap / price.
"""

from bs4 import BeautifulSoup

from engine.quality.gapfill import _fill_identity
from engine.repo import add_snapshot, extract_columns
from src.fetch_screener_data import extract_classification
from tests.factories import make_stock, utc, yf_payload

PEERS_HTML = """
<section id="peers">
  <p class="sub">
    <a href="/market/IN02/">Consumer Discretionary</a>
    <a href="/market/IN02/IN0203/">Textiles</a>
    <a href="/market/IN02/IN0203/IN020301/">Textiles &amp; Apparels</a>
    <a href="/market/IN02/IN0203/IN020301/IN020301002/">Other Textile Products</a>
  </p>
</section>
"""


def _screener_snap(session, stock, *, classification=None, ratios=None):
    payload = {
        "screener_url": "https://www.screener.in/company/X/",
        "ratios": ratios or {},
        "about": "A company.",
    }
    if classification is not None:
        payload["classification"] = classification
    snap, _ = add_snapshot(session, stock, {"screener": payload}, {},
                           source="screener", data_quality="full",
                           captured_at=utc())
    session.commit()
    return snap


# ---------- 1. scraper ----------

def test_extract_classification_from_peers_section():
    soup = BeautifulSoup(PEERS_HTML, "html.parser")
    assert extract_classification(soup) == [
        "Consumer Discretionary", "Textiles",
        "Textiles & Apparels", "Other Textile Products",
    ]


def test_extract_classification_missing_section_is_empty():
    assert extract_classification(BeautifulSoup("<div></div>", "html.parser")) == []


# ---------- 2. identity gapfill fallback ----------

def test_fill_identity_uses_screener_classification(db_session):
    stock = make_stock(db_session, "SCRID1")   # no sector/industry
    _screener_snap(db_session, stock, classification=[
        "Consumer Discretionary", "Textiles",
        "Textiles & Apparels", "Other Textile Products",
    ])
    filled = _fill_identity(db_session, [stock])
    assert filled == 1
    assert stock.sector == "Consumer Discretionary"
    assert stock.industry == "Other Textile Products"   # most specific level


def test_fill_identity_prefers_yf_never_overwrites(db_session):
    stock = make_stock(db_session, "SCRID2", sector="Healthcare")
    _screener_snap(db_session, stock, classification=["Energy", "Oil"])
    _fill_identity(db_session, [stock])
    assert stock.sector == "Healthcare"        # existing value untouched
    assert stock.industry == "Oil"             # only the NULL field filled


# ---------- 3. API market-cap/price fallback ----------

def test_api_list_falls_back_to_screener_mcap_and_price(db_session):
    from fastapi.testclient import TestClient
    from api.main import app

    stock = make_stock(db_session, "SCRID3", universe=["ipo_2026"])
    _screener_snap(db_session, stock,
                   ratios={"Market Cap": "135", "Current Price": "62.9"})

    with TestClient(app) as client:
        r = client.get("/api/stocks", params={"q": "SCRID3"})
    assert r.status_code == 200
    (item,) = r.json()["items"]
    assert item["market_cap_cr"] == 135.0
    assert item["current_price"] == 62.9


def test_api_list_screener_mcap_parses_indian_commas(db_session):
    from fastapi.testclient import TestClient
    from api.main import app

    stock = make_stock(db_session, "SCRID4", universe=["ipo_2026"])
    _screener_snap(db_session, stock, ratios={"Market Cap": "1,20,464"})

    with TestClient(app) as client:
        r = client.get("/api/stocks", params={"q": "SCRID4"})
    (item,) = r.json()["items"]
    assert item["market_cap_cr"] == 120464.0


def test_api_list_yf_values_win_over_screener(db_session):
    from fastapi.testclient import TestClient
    from api.main import app

    stock = make_stock(db_session, "SCRID5", universe=["ipo_2026"])
    payload = yf_payload(price=200.0)
    add_snapshot(db_session, stock, payload, extract_columns(payload["info"]),
                 data_quality="full", captured_at=utc())
    db_session.commit()
    _screener_snap(db_session, stock,
                   ratios={"Market Cap": "1", "Current Price": "1.0"})

    with TestClient(app) as client:
        r = client.get("/api/stocks", params={"q": "SCRID5"})
    (item,) = r.json()["items"]
    assert item["current_price"] == 200.0                  # yf wins
    assert item["market_cap_cr"] == 5000.0                 # 50B abs -> 5,000 Cr


# ---------- 4. ticker-belongs-to-company guard (yf_refresh) ----------
#
# The screener slug is assumed to be the NSE ticker with no verification. A
# slug/ticker mismatch fetches a *different* listed company; every downstream
# number is internally consistent and about the wrong business. refresh() must
# refuse to promote (or store) a payload whose longName disagrees with the
# stock's company_name.

from sqlalchemy import func, select

import engine.ingest.yf_refresh as yfr
from db.models import Stock, StockSnapshot


def _reload_stock(db_session, symbol: str) -> Stock:
    db_session.expire_all()
    return db_session.scalar(select(Stock).where(Stock.symbol == symbol))


def _snapshot_count(db_session, stock_id: int) -> int:
    return db_session.scalar(
        select(func.count(StockSnapshot.id)).where(StockSnapshot.stock_id == stock_id)
    )


def _payload_named(long_name):
    payload = yf_payload()
    payload["info"]["longName"] = long_name
    return payload


def test_refresh_refuses_to_promote_on_company_name_mismatch(db_session, monkeypatch):
    make_stock(db_session, "IDENTMIS", status="new",
               company_name="Acme Industries Ltd")
    payload = _payload_named("Zenith Textiles Limited")
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (payload, "full"))

    stats = yfr.refresh(symbols=["IDENTMIS"], delay=0, verbose=False)

    assert stats["identity_mismatch"] == 1
    assert stats["promoted"] == 0
    stock = _reload_stock(db_session, "IDENTMIS")
    assert stock.status != "active"
    assert stock.status == "new"                       # not promoted, not parked
    assert _snapshot_count(db_session, stock.id) == 0  # mismatched payload never stored


def test_refresh_promotes_when_company_names_agree(db_session, monkeypatch):
    """The guard must not block a legitimate refresh: a first-token match promotes."""
    make_stock(db_session, "IDENTOK", status="new",
               company_name="Acme Industries Ltd")
    payload = _payload_named("Acme Textiles Ltd")   # first significant token agrees
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (payload, "full"))

    stats = yfr.refresh(symbols=["IDENTOK"], delay=0, verbose=False)

    assert stats["promoted"] == 1
    assert stats.get("identity_mismatch", 0) == 0
    stock = _reload_stock(db_session, "IDENTOK")
    assert stock.status == "active"
    assert _snapshot_count(db_session, stock.id) == 1


def test_refresh_allows_promotion_when_longname_missing(db_session, monkeypatch):
    """Missing longName cannot disprove identity — fail-safe: do not block."""
    make_stock(db_session, "IDENTNONE", status="new",
               company_name="Acme Industries Ltd")
    payload = yf_payload()                           # full_info carries no longName
    assert "longName" not in payload["info"]
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (payload, "full"))

    stats = yfr.refresh(symbols=["IDENTNONE"], delay=0, verbose=False)

    assert stats["promoted"] == 1
    assert stats.get("identity_mismatch", 0) == 0
    assert _reload_stock(db_session, "IDENTNONE").status == "active"
