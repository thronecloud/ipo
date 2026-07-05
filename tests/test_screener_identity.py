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
