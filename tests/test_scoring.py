"""Composite scoring tests — engine/repo.py recompute_scores_for_stock."""

from sqlalchemy import func, select

from db.models import CompositeScore
from engine.repo import SCORING_METHOD, TOTAL_PERSONAS, recompute_scores_for_stock
from factories import make_analysis, make_stock, utc

PERSONA_POOL = [
    "warren_buffett", "charlie_munger", "benjamin_graham", "peter_lynch",
    "philip_fisher", "joel_greenblatt", "howard_marks",
    "rakesh_jhunjhunwala", "radhakishan_damani", "vijay_kedia",
]


def _seed_scores(session, stock, scores, rec="HOLD"):
    for persona, score in zip(PERSONA_POOL, scores):
        make_analysis(session, stock, persona, score, rec)


def _row_count(session, stock_id):
    return session.scalar(
        select(func.count(CompositeScore.id)).where(CompositeScore.stock_id == stock_id)
    )


def test_composite_is_sum_of_latest_persona_scores(db_session):
    stock = make_stock(db_session, "SUMS")
    _seed_scores(db_session, stock, [7, 8, 3])

    row = recompute_scores_for_stock(db_session, stock)
    db_session.commit()

    assert row.composite_score == 18
    assert row.persona_scores == {
        "warren_buffett": 7, "charlie_munger": 8, "benjamin_graham": 3,
    }
    assert row.analysis_coverage == 3
    assert row.total_personas == TOTAL_PERSONAS == 10
    assert row.scoring_method == SCORING_METHOD


def _consensus_for_total(db_session, symbol, scores):
    stock = make_stock(db_session, symbol)
    _seed_scores(db_session, stock, scores)
    row = recompute_scores_for_stock(db_session, stock)
    db_session.commit()
    assert row.composite_score == sum(scores)
    return row.consensus_recommendation


def test_buy_boundary_at_60(db_session):
    assert _consensus_for_total(db_session, "BUY60", [10] * 6) == "BUY"


def test_hold_just_below_buy_at_59(db_session):
    assert _consensus_for_total(db_session, "HOLD59", [10, 10, 10, 10, 10, 9]) == "HOLD"


def test_hold_boundary_at_40(db_session):
    assert _consensus_for_total(db_session, "HOLD40", [10] * 4) == "HOLD"


def test_avoid_just_below_hold_at_39(db_session):
    assert _consensus_for_total(db_session, "AVOID39", [10, 10, 10, 9]) == "AVOID"


def test_latest_analysis_wins_per_persona(db_session):
    stock = make_stock(db_session, "LATEST")
    make_analysis(db_session, stock, "warren_buffett", 3, "AVOID", analyzed_at=utc(-60))
    make_analysis(db_session, stock, "warren_buffett", 9, "BUY", analyzed_at=utc(0))
    make_analysis(db_session, stock, "peter_lynch", 5, "HOLD", analyzed_at=utc(-30))

    row = recompute_scores_for_stock(db_session, stock)
    db_session.commit()

    assert row.persona_scores == {"warren_buffett": 9, "peter_lynch": 5}
    assert row.composite_score == 14
    assert row.recommendation_counts == {"BUY": 1, "HOLD": 1}


def test_recompute_replaces_prior_row(db_session):
    stock = make_stock(db_session, "REPLACE")
    _seed_scores(db_session, stock, [5, 5])
    recompute_scores_for_stock(db_session, stock)
    db_session.commit()
    assert _row_count(db_session, stock.id) == 1

    make_analysis(db_session, stock, "benjamin_graham", 8, "BUY")
    row = recompute_scores_for_stock(db_session, stock)
    db_session.commit()

    assert _row_count(db_session, stock.id) == 1
    assert row.composite_score == 18
    assert row.analysis_coverage == 3


def test_null_scores_ignored_and_no_row_when_nothing_scored(db_session):
    stock = make_stock(db_session, "NOSCORE")
    make_analysis(db_session, stock, "warren_buffett", None, "HOLD")

    assert recompute_scores_for_stock(db_session, stock) is None
    db_session.commit()
    assert _row_count(db_session, stock.id) == 0

    # A mix of null and real scores counts only the real ones.
    make_analysis(db_session, stock, "peter_lynch", 6, "HOLD")
    row = recompute_scores_for_stock(db_session, stock)
    db_session.commit()
    assert row.composite_score == 6
    assert row.analysis_coverage == 1
