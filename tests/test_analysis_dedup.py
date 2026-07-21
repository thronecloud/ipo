"""P4-X1: analyses natural key (stock_id, persona, data_hash, model) is UNIQUE.

A force re-analysis of the same data with the same model must REFRESH the existing
row (new verdict, new analyzed_at), never mint a duplicate — duplicates silently
inflate history and make "latest per persona" depend on insertion order.
"""

from sqlalchemy import func, select

from db.models import Analysis
from engine.repo import add_snapshot, extract_columns, save_analysis
from factories import make_stock, utc, yf_payload


def _valid_result(score=6, rec="HOLD"):
    return {
        "score": score,
        "recommendation": rec,
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


def _snap(session, symbol):
    stock = make_stock(session, symbol)
    payload = yf_payload()
    snap, _ = add_snapshot(session, stock, payload, extract_columns(payload["info"]),
                           data_quality="full", captured_at=utc(-1))
    session.commit()
    return stock, snap


def test_same_natural_key_upserts_single_row(db_session):
    stock, snap = _snap(db_session, "DEDUP1")
    r1 = save_analysis(db_session, stock, snap, "warren_buffett", "m1", "v3",
                       _valid_result(3, "AVOID"), {})
    db_session.commit()
    r2 = save_analysis(db_session, stock, snap, "warren_buffett", "m1", "v3",
                       _valid_result(8, "BUY"), {})
    db_session.commit()

    rows = db_session.scalars(
        select(Analysis).where(Analysis.stock_id == stock.id)
    ).all()
    assert len(rows) == 1, "same (stock, persona, hash, model) must not duplicate"
    assert rows[0].score == 8 and rows[0].recommendation == "BUY"  # refreshed
    assert r1.id == r2.id
    assert rows[0].analyzed_at is not None


def test_refresh_bumps_analyzed_at(db_session):
    """The upsert must move analyzed_at forward — staleness/recompute keys off it."""
    stock, snap = _snap(db_session, "DEDUP2")
    save_analysis(db_session, stock, snap, "warren_buffett", "m1", "v3",
                  _valid_result(3, "AVOID"), {})
    db_session.commit()
    first = db_session.scalar(select(Analysis.analyzed_at).where(Analysis.stock_id == stock.id))
    save_analysis(db_session, stock, snap, "warren_buffett", "m1", "v3",
                  _valid_result(8, "BUY"), {})
    db_session.commit()
    second = db_session.scalar(select(Analysis.analyzed_at).where(Analysis.stock_id == stock.id))
    assert second > first


def test_different_key_components_do_not_collide(db_session):
    """Different persona, model, or data_hash → separate rows (the key is exact)."""
    stock, snap = _snap(db_session, "DEDUP3")
    save_analysis(db_session, stock, snap, "warren_buffett", "m1", "v3", _valid_result(), {})
    save_analysis(db_session, stock, snap, "charlie_munger", "m1", "v3", _valid_result(), {})
    save_analysis(db_session, stock, snap, "warren_buffett", "m2", "v3", _valid_result(), {})
    db_session.commit()
    n = db_session.scalar(select(func.count()).select_from(Analysis)
                          .where(Analysis.stock_id == stock.id))
    assert n == 3
