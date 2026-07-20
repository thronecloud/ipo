"""Staleness-driven work selection tests — engine/analysis/engine.py find_work."""

from datetime import date
from types import SimpleNamespace

from pytest import approx

from engine import repo
from engine.analysis.engine import find_work
from engine.analysis.prompt import build_user_prompt, latest_close, reprice
from engine.repo import add_snapshot, extract_columns, save_analysis
from factories import full_info, make_stock, utc, yf_payload
from src.personas import get_persona_slugs

PERSONAS = get_persona_slugs()


def _full_snapshot(session, stock, price=100.0, minutes=0, revenue=1000.0):
    payload = yf_payload(price=price, revenue=revenue)
    snap, _ = add_snapshot(
        session, stock, payload, extract_columns(payload["info"]),
        data_quality="full", captured_at=utc(minutes),
    )
    session.commit()
    return snap


def _fake_result(score=7):
    # Contract-complete (save_analysis validates against ANALYSIS_JSON_SCHEMA).
    return {
        "score": score,
        "recommendation": "BUY",
        "investment_thesis": "Solid.",
        "key_strengths": ["a"],
        "key_risks": ["b"],
        "red_flags": [],
        "detailed_analysis": "Detailed.",
        "metrics_evaluated": {
            "moat_strength": "moderate",
            "management_quality": "good",
            "financial_health": "good",
            "valuation": "fair",
            "growth_potential": "good",
        },
    }


def test_persona_roster_is_ten():
    assert len(PERSONAS) == 10


def test_fresh_stock_yields_all_persona_pairs(db_session):
    stock = make_stock(db_session, "FRESH")
    snap = _full_snapshot(db_session, stock)

    work = find_work(db_session, PERSONAS)

    assert len(work) == 10
    assert {(s.symbol, sn.id, slug) for s, sn, slug in work} == {
        ("FRESH", snap.id, slug) for slug in PERSONAS
    }


def test_stock_without_full_snapshot_is_skipped(db_session):
    stock = make_stock(db_session, "PARTIAL")
    payload = yf_payload()
    add_snapshot(db_session, stock, payload, extract_columns(payload["info"]),
                 data_quality="partial")
    db_session.commit()

    assert find_work(db_session, PERSONAS) == []


def test_analyzed_pair_with_matching_hash_excluded(db_session):
    stock = make_stock(db_session, "DONE1")
    snap = _full_snapshot(db_session, stock)
    save_analysis(db_session, stock, snap, "warren_buffett", "m", "v3",
                  _fake_result(), {})
    db_session.commit()

    work = find_work(db_session, PERSONAS)

    assert len(work) == 9
    assert "warren_buffett" not in {slug for _, _, slug in work}


def test_changed_snapshot_reincludes_pair(db_session):
    stock = make_stock(db_session, "STALE")
    snap1 = _full_snapshot(db_session, stock, price=100.0, minutes=-10)
    save_analysis(db_session, stock, snap1, "warren_buffett", "m", "v3",
                  _fake_result(), {})
    db_session.commit()
    assert len(find_work(db_session, PERSONAS)) == 9

    # a fundamentals change (revenue) is what re-hashes and re-stales the pair
    snap2 = _full_snapshot(db_session, stock, revenue=5000.0, minutes=0)
    assert snap2.content_hash != snap1.content_hash

    work = find_work(db_session, PERSONAS)
    assert len(work) == 10  # buffett is stale again
    assert all(sn.id == snap2.id for _, sn, _ in work)


def test_force_includes_everything(db_session):
    stock = make_stock(db_session, "FORCED")
    snap = _full_snapshot(db_session, stock)
    for slug in PERSONAS:
        save_analysis(db_session, stock, snap, slug, "m", "v3", _fake_result(), {})
    db_session.commit()

    assert find_work(db_session, PERSONAS) == []
    assert len(find_work(db_session, PERSONAS, force=True)) == 10


def test_universe_filter(db_session):
    s_in = make_stock(db_session, "INUNI", universe=["ipo_2026"])
    s_out = make_stock(db_session, "OUTUNI", universe=["ipo_2025"])
    _full_snapshot(db_session, s_in)
    _full_snapshot(db_session, s_out)

    work = find_work(db_session, PERSONAS, universe="ipo_2026")

    assert len(work) == 10
    assert {s.symbol for s, _, _ in work} == {"INUNI"}


def test_limit_caps_work(db_session):
    stock = make_stock(db_session, "CAPPED")
    _full_snapshot(db_session, stock)

    assert len(find_work(db_session, PERSONAS, limit=3)) == 3


# ---------- prompt price freshness ----------

def _add_bars(session, stock, bars):
    repo.upsert_daily_prices(session, stock.id, [
        {"date": d, "open": c, "high": c, "low": c, "close": c, "volume": 1}
        for d, c in bars
    ])
    session.commit()


def test_prompt_uses_latest_daily_close_not_stale_snapshot_price(db_session):
    """A snapshot pinned at 100 must not be quoted to the persona when the
    latest bar says 250. Measured prod drift reached 700%."""
    stock = make_stock(db_session, "STALEPX", issue_price=50.0)
    snap = _full_snapshot(db_session, stock, price=100.0)
    _add_bars(db_session, stock, [(date(2026, 7, 17), 250.0)])

    prompt = build_user_prompt(db_session, stock, snap)

    assert "250" in prompt
    assert "Current Price: INR 100" not in prompt


def test_prompt_rescales_price_derived_ratios_to_the_quoted_price(db_session):
    """Quoting a fresh price beside ratios computed from the old one would make
    the prompt internally inconsistent. P/E 25.5 at 100 is P/E 63.75 at 250."""
    stock = make_stock(db_session, "RESCALE", issue_price=50.0)
    snap = _full_snapshot(db_session, stock, price=100.0)
    _add_bars(db_session, stock, [(date(2026, 7, 17), 250.0)])

    prompt = build_user_prompt(db_session, stock, snap)

    assert "P/E (TTM): 63.75" in prompt
    assert "25.50" not in prompt
    # IPO return is measured from the quoted price too: 50 -> 250 is +400%.
    assert "400.0%" in prompt


def test_prompt_carries_exactly_one_current_price(db_session):
    """The screener block used to inject a second, differently-timed price."""
    stock = make_stock(db_session, "ONEPX")
    snap = _full_snapshot(db_session, stock, price=100.0)
    _add_bars(db_session, stock, [(date(2026, 7, 17), 250.0)])
    screener = SimpleNamespace(screener={
        "ratios": {"Current Price": "180", "ROCE": "22%", "Stock P/E": "40"},
    })

    prompt = build_user_prompt(db_session, stock, snap, screener)

    assert prompt.count("Current Price") == 1
    assert "180" not in prompt
    assert "ROCE: 22%" in prompt


def test_prompt_falls_back_to_snapshot_price_without_bars(db_session):
    stock = make_stock(db_session, "NOBARS")
    snap = _full_snapshot(db_session, stock, price=100.0)

    prompt = build_user_prompt(db_session, stock, snap)

    assert "Current Price: INR 100" in prompt


def test_prompt_price_is_as_of_the_requested_date(db_session):
    """Point-in-time honesty: re-running an old analysis must not see the future."""
    stock = make_stock(db_session, "ASOF")
    snap = _full_snapshot(db_session, stock, price=100.0)
    _add_bars(db_session, stock, [(date(2026, 7, 10), 150.0), (date(2026, 7, 17), 250.0)])

    prompt = build_user_prompt(db_session, stock, snap, as_of=date(2026, 7, 12))

    assert "Current Price: INR 150" in prompt
    assert "250" not in prompt


def test_prompt_falls_back_to_regular_market_price_when_that_is_all_there_is(db_session):
    """Production carries snapshots whose only quote field is regularMarketPrice."""
    stock = make_stock(db_session, "RMPONLY")
    payload = yf_payload(price=100.0)
    payload["info"].pop("currentPrice")
    snap, _ = add_snapshot(db_session, stock, payload, extract_columns(payload["info"]),
                           data_quality="full", captured_at=utc())
    db_session.commit()

    assert "Current Price: INR 100" in build_user_prompt(db_session, stock, snap)


# ---------- restatement arithmetic ----------

# 100 -> 250 is k = 2.5 against the full_info fixture: marketCap 50bn,
# enterpriseValue 60bn (so 10bn of net debt), bookValue 20 with P/B 5.
REPRICED = reprice(full_info(price=100.0), 250.0)


def test_reprice_moves_only_the_equity_leg_of_enterprise_value():
    """EV is equity + net debt. Repricing the shares does not reprice the debt,
    so EV' = EV + mcap*(k-1) = 135bn — emphatically NOT EV*k = 150bn."""
    assert REPRICED["enterpriseValue"] == approx(135_000_000_000.0)
    assert REPRICED["enterpriseValue"] != approx(150_000_000_000.0)


def test_reprice_moves_dividend_yield_inversely_to_price():
    """Yield is dividend/price: the one ratio that falls when the price rises."""
    assert REPRICED["dividendYield"] == approx(0.016 / 2.5)
    assert REPRICED["dividendYield"] < full_info()["dividendYield"]


def test_reprice_scales_price_derived_ratios_linearly():
    assert REPRICED["marketCap"] == approx(125_000_000_000.0)
    assert REPRICED["priceToBook"] == approx(12.5)
    assert REPRICED["trailingPE"] == approx(63.75)
    assert REPRICED["forwardPE"] == approx(50.0)
    assert REPRICED["priceToSalesTrailing12Months"] == approx(10.0)


def test_reprice_restates_ev_multiples_off_the_restated_ev():
    """EV multiples ride the restated EV, not the price factor — 135/60, not 2.5."""
    ratio = 135.0 / 60.0
    assert REPRICED["enterpriseToEbitda"] == approx(16.67 * ratio)
    assert REPRICED["enterpriseToRevenue"] == approx(5.0 * ratio)


def test_reprice_leaves_accounting_figures_untouched():
    """A share price move restates valuation, never the accounts behind it."""
    before = full_info(price=100.0)
    for field in ("bookValue", "trailingEps", "ebitda", "totalRevenue",
                  "returnOnEquity", "operatingMargins", "profitMargins",
                  "debtToEquity", "revenueGrowth"):
        assert REPRICED[field] == approx(before[field]), field


def test_reprice_keeps_price_to_book_consistent_with_book_value():
    """Mutual consistency: the persona can divide the stated numbers itself."""
    assert REPRICED["priceToBook"] == approx(
        REPRICED["currentPrice"] / REPRICED["bookValue"])


def test_prompt_states_one_dividend_yield(db_session):
    """The screener's as-scraped yield contradicted the repriced yfinance one."""
    stock = make_stock(db_session, "ONEDIV")
    snap = _full_snapshot(db_session, stock, price=100.0)
    _add_bars(db_session, stock, [(date(2026, 7, 17), 250.0)])
    screener = SimpleNamespace(screener={
        "ratios": {"Dividend Yield": "3.40", "ROCE": "22%"},
    })

    prompt = build_user_prompt(db_session, stock, snap, screener)

    assert prompt.count("Dividend Yield") == 1
    assert "3.40" not in prompt
    assert "Dividend Yield: 0.6%" in prompt
    assert "ROCE: 22%" in prompt


def test_latest_close_skips_a_null_newest_bar(db_session):
    """Production carries bars whose close never arrived. Taking the newest row
    regardless returns None, and the snapshot-price fallback then quotes the
    stale price the whole fix exists to replace."""
    stock = make_stock(db_session, "NULLBAR")
    snap = _full_snapshot(db_session, stock, price=100.0)
    _add_bars(db_session, stock, [(date(2026, 7, 16), 250.0)])
    repo.upsert_daily_prices(db_session, stock.id, [
        {"date": date(2026, 7, 17), "open": None, "high": None,
         "low": None, "close": None, "volume": 0},
    ])
    db_session.commit()

    assert latest_close(db_session, stock.id) == 250.0
    assert "Current Price: INR 250" in build_user_prompt(db_session, stock, snap)
