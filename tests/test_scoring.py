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


def test_composite_is_coverage_weighted_mean(db_session):
    stock = make_stock(db_session, "MEAN")
    _seed_scores(db_session, stock, [7, 8, 3])  # mean 6.0 -> 60.0 on the 0-100 scale

    row = recompute_scores_for_stock(db_session, stock)
    db_session.commit()

    assert row.composite_score == 60.0
    assert row.persona_scores == {
        "warren_buffett": 7, "charlie_munger": 8, "benjamin_graham": 3,
    }
    assert row.analysis_coverage == 3
    assert row.total_personas == TOTAL_PERSONAS == 10
    assert row.scoring_method == SCORING_METHOD


def test_coverage_does_not_bias_consensus(db_session):
    """P0-3 invariant: 4 personas @10 and 10 personas @10 are both 100/BUY —
    partial coverage no longer structurally caps the score."""
    s4 = make_stock(db_session, "COV4")
    _seed_scores(db_session, s4, [10, 10, 10, 10])
    r4 = recompute_scores_for_stock(db_session, s4)
    s10 = make_stock(db_session, "COV10")
    _seed_scores(db_session, s10, [10] * 10)
    r10 = recompute_scores_for_stock(db_session, s10)
    db_session.commit()
    assert r4.composite_score == 100.0 and r4.consensus_recommendation == "BUY"
    assert r10.composite_score == 100.0 and r10.consensus_recommendation == "BUY"


def test_scores_clamped_defensively(db_session):
    """A rogue out-of-range score (inserted bypassing save_analysis) is clamped."""
    stock = make_stock(db_session, "CLAMP")
    make_analysis(db_session, stock, "warren_buffett", 99, "BUY")
    make_analysis(db_session, stock, "charlie_munger", 5, "HOLD")
    row = recompute_scores_for_stock(db_session, stock)
    db_session.commit()
    assert row.persona_scores["warren_buffett"] == 10       # clamped
    assert row.composite_score == 75.0                       # mean(10,5)=7.5 -> 75


def _consensus(db_session, symbol, scores):
    stock = make_stock(db_session, symbol)
    _seed_scores(db_session, stock, scores)
    row = recompute_scores_for_stock(db_session, stock)
    db_session.commit()
    return row.consensus_recommendation


def test_consensus_boundaries_on_0_100_scale(db_session):
    assert _consensus(db_session, "BUY60", [6]) == "BUY"      # 60 -> BUY
    assert _consensus(db_session, "HOLD50", [5]) == "HOLD"    # 50 -> HOLD
    assert _consensus(db_session, "HOLD40", [4]) == "HOLD"    # 40 -> HOLD boundary
    assert _consensus(db_session, "AVOID30", [3]) == "AVOID"  # 30 -> AVOID


def test_latest_analysis_wins_per_persona(db_session):
    stock = make_stock(db_session, "LATEST")
    make_analysis(db_session, stock, "warren_buffett", 3, "AVOID", analyzed_at=utc(-60))
    make_analysis(db_session, stock, "warren_buffett", 9, "BUY", analyzed_at=utc(0))
    make_analysis(db_session, stock, "peter_lynch", 5, "HOLD", analyzed_at=utc(-30))

    row = recompute_scores_for_stock(db_session, stock)
    db_session.commit()

    assert row.persona_scores == {"warren_buffett": 9, "peter_lynch": 5}
    assert row.composite_score == 70.0  # mean(9,5)=7.0 -> 70
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
    assert row.composite_score == 60.0  # mean(5,5,8)=6.0 -> 60
    assert row.analysis_coverage == 3


def test_recompute_persists_confidence_layer(db_session):
    """LEAK#1: recompute stores the axis-based tier + LCB on the composite and history."""
    stock = make_stock(db_session, "CONF1")
    # only correlated-core personas present -> provisional (missing value/growth/independent)
    for p in ["warren_buffett", "charlie_munger", "radhakishan_damani"]:
        make_analysis(db_session, stock, p, 8, "BUY")
    row = recompute_scores_for_stock(db_session, stock)
    db_session.commit()
    assert row.confidence_tier == "provisional"
    assert row.factor_version == "axis-v1"
    assert row.lcb is not None and row.lcb < row.composite_score  # coverage penalty applied
    assert row.axis_scores == {"core": 80.0}


def test_recompute_appends_pointintime_history(db_session):
    """Tier 2.5: each recompute appends one point-in-time history row, idempotent per day."""
    from db.models import CompositeScoreHistory
    stock = make_stock(db_session, "HIST1")
    _seed_scores(db_session, stock, [7, 8, 3])  # -> composite 60.0
    recompute_scores_for_stock(db_session, stock)
    db_session.commit()

    rows = db_session.scalars(
        select(CompositeScoreHistory).where(CompositeScoreHistory.stock_id == stock.id)
    ).all()
    assert len(rows) == 1
    assert rows[0].composite_score == 60.0
    assert rows[0].information_date is not None      # the date the view formed
    assert rows[0].analysis_coverage == 3

    # Same-day re-recompute refreshes the point, never duplicates it.
    recompute_scores_for_stock(db_session, stock)
    db_session.commit()
    n = db_session.scalar(
        select(func.count()).select_from(CompositeScoreHistory).where(
            CompositeScoreHistory.stock_id == stock.id
        )
    )
    assert n == 1


def test_reconcile_rescores_old_scoring_method(db_session):
    """F1: a composite scored under an OLD formula must be reselected & rescored,
    so identical analyses never yield different scores based on WHEN they were scored."""
    from engine.repo import SCORING_METHOD, reconcile_scores
    stock = make_stock(db_session, "OLDMETHOD")
    _seed_scores(db_session, stock, [7, 8, 3])  # new formula -> 60.0
    recompute_scores_for_stock(db_session, stock)
    db_session.commit()
    # simulate a legacy row: old method + old sum-based value, current timestamp
    row = db_session.scalar(select(CompositeScore).where(CompositeScore.stock_id == stock.id))
    row.scoring_method = "sum_of_persona_scores_each_0_to_10"
    row.composite_score = 18.0
    db_session.commit()

    n = reconcile_scores(db_session)
    db_session.commit()
    db_session.expire_all()  # upsert is Core SQL — drop stale ORM identity-map cache
    updated = db_session.scalar(select(CompositeScore).where(CompositeScore.stock_id == stock.id))
    assert n >= 1
    assert updated.scoring_method == SCORING_METHOD
    assert updated.composite_score == 60.0


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
    assert row.composite_score == 60.0  # mean(6)=6.0 -> 60
    assert row.analysis_coverage == 1
