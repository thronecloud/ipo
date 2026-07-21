"""Target selection and skip-if-current for the model-homogenisation campaign.

The real analysis call burns quota, so it is never exercised here: selection is
pure DB queries (no mocking), and the one execution test mocks the backend at the
same boundary the engine's own tests use (engine.analysis.engine.get_backend).
"""

from datetime import date, timedelta

from sqlalchemy import func, select

from db.models import Analysis, CompositeScore, DailyPrice, JobRun
from engine.repo import add_snapshot, extract_columns
from factories import make_analysis, make_stock, utc, yf_payload
import scripts.rescore_campaign as rc
from src.personas import get_persona_slugs

TARGET = "claude-fable-5"
PROMPT = rc.PROMPT_VERSION
ROSTER = get_persona_slugs()


def _snap(session, stock, price=100.0, minutes=-1):
    payload = yf_payload(price=price)
    snap, _ = add_snapshot(session, stock, payload, extract_columns(payload["info"]),
                           data_quality="full", captured_at=utc(minutes))
    session.commit()
    return snap


def _roster_at(session, stock, model, prompt, *, score=7):
    for slug in ROSTER:
        make_analysis(session, stock, slug, score, model=model, prompt_version=prompt)


# ---------- tier 1: dominant model ----------

def test_dominant_model_selects_stock_whose_modal_model_is_not_target(db_session):
    opus = make_stock(db_session, "OPUSDOM")
    for slug in ROSTER[:6]:
        make_analysis(db_session, opus, slug, 5, model="opus", prompt_version=PROMPT)
    for slug in ROSTER[6:]:
        make_analysis(db_session, opus, slug, 5, model="sonnet", prompt_version=PROMPT)

    fable = make_stock(db_session, "FABLEDOM")
    _roster_at(db_session, fable, TARGET, PROMPT)

    assert rc.dominant_model_targets(db_session, TARGET) == ["OPUSDOM"]


def test_dominant_model_ignores_unscored_analyses(db_session):
    # Scored analyses are all target-model; the only non-target rows are unscored
    # and must not tip the mode.
    stock = make_stock(db_session, "SCOREDOK")
    for slug in ROSTER:
        make_analysis(db_session, stock, slug, 6, model=TARGET, prompt_version=PROMPT)
    make_analysis(db_session, stock, "warren_buffett", None, model="opus",
                  prompt_version=PROMPT)

    assert rc.dominant_model_targets(db_session, TARGET) == []


# ---------- tier 2: mixed prompt version ----------

def test_mixed_prompt_flags_stock_with_multiple_prompt_versions(db_session):
    mixed = make_stock(db_session, "MIXEDPV")
    make_analysis(db_session, mixed, "warren_buffett", 5, model=TARGET, prompt_version="v4")
    make_analysis(db_session, mixed, "charlie_munger", 5, model=TARGET, prompt_version="v5")

    clean = make_stock(db_session, "CLEANPV")
    make_analysis(db_session, clean, "warren_buffett", 5, model=TARGET, prompt_version="v5")

    assert rc.mixed_prompt_targets(db_session) == ["MIXEDPV"]


# ---------- tier 3: price drift ----------

def _seed_drift(session, symbol, seen_price, close_price):
    stock = make_stock(session, symbol)
    snap = _snap(session, stock, price=seen_price)
    session.add(DailyPrice(stock_id=stock.id, date=date.today() - timedelta(days=1),
                           close=close_price))
    session.commit()
    make_analysis(session, stock, "warren_buffett", 5, model=TARGET,
                  prompt_version=PROMPT, snapshot_id=snap.id, analyzed_at=utc())
    return stock


def test_drift_flags_analysis_quoted_off_the_real_close(db_session):
    _seed_drift(db_session, "DRIFTED", seen_price=200.0, close_price=100.0)   # 100%
    _seed_drift(db_session, "ONPRICE", seen_price=100.5, close_price=100.0)   # 0.5%

    assert rc.drift_targets(db_session) == ["DRIFTED"]


# ---------- dedup, ordering, skip-if-current ----------

def test_select_dedupes_to_highest_tier_and_orders(db_session):
    # ZED is both dominant-model (tier 1) and mixed-prompt (tier 2); it must
    # appear once, at tier 1. AAA is only mixed-prompt (tier 2).
    zed = make_stock(db_session, "ZED")
    for slug in ROSTER:
        make_analysis(db_session, zed, slug, 5, model="opus", prompt_version="v4")
    make_analysis(db_session, zed, "warren_buffett", 5, model="opus", prompt_version="v5")

    aaa = make_stock(db_session, "AAA")
    make_analysis(db_session, aaa, "warren_buffett", 5, model=TARGET, prompt_version="v4")
    make_analysis(db_session, aaa, "charlie_munger", 5, model=TARGET, prompt_version="v5")

    plan = rc.select_targets(db_session, TARGET, PROMPT)

    assert plan["tier_of"] == {"ZED": 1, "AAA": 2}
    # ordered by (tier, symbol): tier-1 ZED before tier-2 AAA
    assert plan["actionable"] == ["ZED", "AAA"]


def test_fully_current_stock_is_skipped(db_session):
    # A real tier-2 candidate (carries v4 AND v5) that is nonetheless already
    # complete under the target model at the current prompt: nothing to re-run.
    stock = make_stock(db_session, "DONECUR")
    _roster_at(db_session, stock, TARGET, PROMPT)          # 10 personas @ target/current
    _roster_at(db_session, stock, TARGET, "v4")            # + stale prompt -> mixed

    plan = rc.select_targets(db_session, TARGET, PROMPT)

    assert "DONECUR" in plan["skipped_current"]
    assert "DONECUR" not in plan["actionable"]


# ---------- dry run touches nothing ----------

def test_dry_run_writes_nothing(db_session):
    stock = make_stock(db_session, "DRYRUN")
    for slug in ROSTER:
        make_analysis(db_session, stock, slug, 5, model="opus", prompt_version=PROMPT)
    jobs_before = db_session.scalar(select(func.count()).select_from(JobRun))
    analyses_before = db_session.scalar(select(func.count()).select_from(Analysis))

    result = rc.run_campaign(target_model=TARGET, execute=False, verbose=False)

    assert result == {"planned": 1, "executed": 0, "dry_run": True}
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(JobRun)) == jobs_before
    assert db_session.scalar(select(func.count()).select_from(Analysis)) == analyses_before


# ---------- execute drives the real engine path (backend mocked) ----------

class _FakeBackend:
    name = "fake"

    def analyze(self, system_prompt, user_prompt, model):
        result = {
            "score": 7, "recommendation": "BUY", "investment_thesis": "Solid.",
            "key_strengths": ["a"], "key_risks": ["b"], "red_flags": [],
            "detailed_analysis": "Detailed.",
            "metrics_evaluated": {"moat_strength": "moderate", "management_quality": "good",
                                  "financial_health": "good", "valuation": "fair",
                                  "growth_potential": "good"},
        }
        return result, {"model_used": model}


def test_execute_reanalyses_actionable_under_target_and_records_jobrun(db_session, monkeypatch):
    import engine.analysis.engine as eng
    monkeypatch.setattr(eng, "get_backend", lambda: _FakeBackend())

    # Actionable: opus-dominant with a full snapshot so find_work plans it.
    target = make_stock(db_session, "RUNME")
    snap = _snap(db_session, target)
    for slug in ROSTER:
        make_analysis(db_session, target, slug, 4, model="opus", prompt_version=PROMPT,
                      snapshot_id=snap.id, data_hash=snap.content_hash)

    # Already current: must not be re-run.
    current = make_stock(db_session, "SKIPME")
    _roster_at(db_session, current, TARGET, PROMPT)

    result = rc.run_campaign(target_model=TARGET, execute=True, workers=4, delay=0,
                             verbose=False)

    assert result["dry_run"] is False
    assert result["planned"] == 1 and result["success"] == 1

    db_session.expire_all()
    # All ten personas now carry the target model at the current prompt version.
    rows = db_session.execute(
        select(Analysis.persona).where(Analysis.stock_id == target.id,
                                       Analysis.model == TARGET,
                                       Analysis.prompt_version == PROMPT)
    ).all()
    assert {p for (p,) in rows} == set(ROSTER)
    # The skipped stock was never touched by the target model beyond its seed.
    assert db_session.scalar(
        select(func.count()).select_from(CompositeScore).where(
            CompositeScore.stock_id == target.id)
    ) == 1
    # Campaign JobRun landed in ops history.
    campaign_jobs = db_session.scalars(
        select(JobRun).where(JobRun.job_type == "rescore_campaign")
    ).all()
    assert len(campaign_jobs) == 1 and campaign_jobs[0].status in ("success", "partial")
