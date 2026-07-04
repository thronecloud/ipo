"""Staleness-driven work selection tests — engine/analysis/engine.py find_work."""

from engine.analysis.engine import find_work
from engine.repo import add_snapshot, extract_columns, save_analysis
from factories import make_stock, utc, yf_payload
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
