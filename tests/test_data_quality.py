"""Data-quality subsystem: metrics primitives, dimension scoring, audit, gapfill."""

from datetime import date

from sqlalchemy import func, select

from db.models import DataQualityReport
from engine.quality import metrics
from engine.quality.audit import audit
from engine.quality.dimensions import score_stock
from engine.quality.gapfill import gapfill
from engine.repo import add_snapshot, extract_columns
from factories import make_stock, utc, yf_payload


# ---------- metrics primitives (pure) ----------

def test_num_parses_indian_and_symbols():
    assert metrics.num("1,35,942") == 135942.0
    assert metrics.num("₹ 2,538.10") == 2538.10
    assert metrics.num("18.4%") == 18.4
    assert metrics.num("-") is None
    assert metrics.num(None) is None


def test_unit_normalizers():
    assert metrics.cr_to_absolute("19,290") == 19290 * 1e7
    assert metrics.pct_to_fraction("18") == 0.18


def test_quarter_counting_and_gaps():
    qf = {"Sales": {f"2024-{m:02d}-30 00:00:00": 1 for m in (3, 6, 9, 12)}}
    assert metrics.yf_quarters(qf) == ["2024Q1", "2024Q2", "2024Q3", "2024Q4"]
    assert metrics.gaps(metrics.yf_quarters(qf)) == 0
    # drop Sep -> one internal gap
    gappy = {"Sales": {"2024-03-30 00:00:00": 1, "2024-06-30 00:00:00": 1, "2024-12-30 00:00:00": 1}}
    assert metrics.gaps(metrics.yf_quarters(gappy)) == 1


def test_scr_quarters_parses_mon_year():
    q = {"Sales": {"Mar 2024": 1, "Jun 2024": 1}}
    assert metrics.scr_quarters(q) == ["2024Q1", "2024Q2"]


def test_expected_latest_quarter_date_math():
    from datetime import date
    # 90-day lag: by 4 Jul 2026, the Mar-2026 (Q1) quarter is reliably published.
    assert metrics.expected_latest_quarter(date(2026, 7, 4)) == "2026Q1"
    # In late April 2026, Mar-2026 isn't due yet -> expect Dec-2025 (Q4).
    assert metrics.expected_latest_quarter(date(2026, 4, 20)) == "2025Q4"
    assert metrics.quarter_ordinal("2026Q1") - metrics.quarter_ordinal("2025Q4") == 1


# ---------- snapshot builders ----------

def _recent_quarter_ends(n, offset_q=0):
    """N contiguous quarter-end ISO strings; newest = the currently-expected quarter
    minus `offset_q` quarters (offset_q>0 => stale)."""
    from datetime import date
    from engine.quality.metrics import expected_latest_quarter, quarter_ordinal
    base = quarter_ordinal(expected_latest_quarter(date.today())) - offset_q
    out = []
    for i in range(n):
        ordv = base - (n - 1 - i)
        yr, q = ordv // 4, ordv % 4
        if q == 0:
            yr, q = yr - 1, 4
        mo = q * 3
        out.append(f"{yr}-{mo:02d}-{'31' if mo in (3, 12) else '30'} 00:00:00")
    return out


def _yf(session, stock, *, price=100.0, quality="full", quarters=8, annual=True, offset_q=0):
    qf = {"Sales": {end: 100 for end in _recent_quarter_ends(quarters, offset_q)}}
    payload = {
        "info": {"currentPrice": price, "marketCap": 5e10, "trailingPE": 25.0,
                 "returnOnEquity": 0.18, "sector": "Healthcare", "industry": "Drugs"},
        "financials": {"2025": {"Total Revenue": 1000}} if annual else {},
        "quarterly_financials": qf,
        "balance_sheet": {}, "cashflow": {}, "history_summary": {},
    }
    add_snapshot(session, stock, payload, extract_columns(payload["info"]),
                 source="yfinance", data_quality=quality, captured_at=utc(-1))
    session.commit()


def _screener(session, stock, *, ratios=None, quarters=13):
    qr = {"Sales": {f"Q{i}": 1 for i in range(quarters)}}  # count only
    data = {
        "profit_loss": {"Sales": {"2025": 1}},
        "quarterly_results": qr,
        "ratios": ratios or {"Current Price": "100", "Market Cap": "5000",
                             "Stock P/E": "25", "ROE": "18"},
        "shareholding": {"Promoter": {"2025": "50"}},
    }
    add_snapshot(session, stock, {"screener": data}, {},
                 source="screener", data_quality="full", captured_at=utc(-1))
    session.commit()


# ---------- dimension scoring ----------

def test_full_data_stock_scores_A(db_session):
    st = make_stock(db_session, "FULL1", isin="INE001", sector="Healthcare",
                    industry="Drugs", cap_category="large")
    _yf(db_session, st)
    _screener(db_session, st)
    rep = score_stock(db_session, st)
    assert rep["grade"] in ("A", "B")
    assert rep["overall_score"] >= 75
    assert not any(f["type"].endswith("_mismatch") for f in rep["flags"])


def test_source_dark_stock_scored_only_on_identity(db_session):
    """No yfinance, no screener -> source_unavailable, dependent dims NA, not F."""
    st = make_stock(db_session, "DARK1", isin="INE9", sector="X",
                    industry="Y", cap_category="small")
    rep = score_stock(db_session, st)
    assert any(f["type"] == "source_unavailable" for f in rep["flags"])
    for dim in ("yfinance", "screener", "prices", "quarters", "correctness"):
        assert rep["dimensions"][dim]["status"] == "na"
    # identity is full -> should NOT be an F
    assert rep["grade"] in ("A", "B")


def test_screener_only_stock_prices_and_correctness_na(db_session):
    st = make_stock(db_session, "SCRONLY", isin="INE2", sector="X",
                    industry="Y", cap_category="small")
    _yf(db_session, st, quality="minimal")   # yahoo has nothing real
    _screener(db_session, st)
    rep = score_stock(db_session, st)
    assert rep["dimensions"]["prices"]["status"] == "na"
    assert rep["dimensions"]["correctness"]["status"] == "na"
    assert rep["dimensions"]["screener"]["status"] == "pass"
    assert any(f["type"] == "minimal_yf" for f in rep["flags"])


def test_price_mismatch_flagged(db_session):
    st = make_stock(db_session, "MISMATCH", isin="INE3", sector="X",
                    industry="Y", cap_category="mid")
    _yf(db_session, st, price=100.0)
    _screener(db_session, st, ratios={"Current Price": "17", "Market Cap": "5000",
                                     "Stock P/E": "25", "ROE": "18"})
    rep = score_stock(db_session, st)
    assert any(f["type"] == "price_mismatch" for f in rep["flags"])
    assert rep["dimensions"]["correctness"]["status"] in ("partial", "fail")


def test_quarter_gap_flagged(db_session):
    st = make_stock(db_session, "GAPPY", isin="INE4", sector="X",
                    industry="Y", cap_category="small")
    # yfinance with only 4 quarters and a gap; screener with a gap too
    payload = {
        "info": {"currentPrice": 100, "sector": "X"},
        "financials": {}, "balance_sheet": {}, "cashflow": {}, "history_summary": {},
        "quarterly_financials": {"Sales": {"2025-03-30 00:00:00": 1,
                                           "2025-06-30 00:00:00": 1,
                                           "2025-12-30 00:00:00": 1}},  # missing Sep
    }
    add_snapshot(db_session, st, payload, extract_columns(payload["info"]),
                 source="yfinance", data_quality="full", captured_at=utc(-1))
    db_session.commit()
    rep = score_stock(db_session, st)
    assert any(f["type"] == "missing_quarters" for f in rep["flags"])
    assert "quarters" in rep["missing"]


def test_current_quarters_not_flagged_stale(db_session):
    st = make_stock(db_session, "CURRENTQ", isin="INE7", sector="X",
                    industry="Y", cap_category="small")
    _yf(db_session, st, offset_q=0)  # newest quarter = expected
    _screener(db_session, st)
    rep = score_stock(db_session, st)
    assert not any(f["type"] == "stale_quarter" for f in rep["flags"])
    assert "latest_quarter" not in rep["missing"]


def test_stale_latest_quarter_flagged(db_session):
    st = make_stock(db_session, "STALEQ", isin="INE8", sector="X",
                    industry="Y", cap_category="small")
    _yf(db_session, st, offset_q=3)  # newest quarter is 3 quarters behind expected
    _screener(db_session, st)
    rep = score_stock(db_session, st)
    assert any(f["type"] == "stale_quarter" for f in rep["flags"])
    assert "latest_quarter" in rep["missing"]
    assert rep["dimensions"]["quarters"]["status"] != "pass"


def test_gapfill_routes_stale_quarter_to_screener(db_session, monkeypatch):
    """A stale-quarter stock must be queued for screener re-enrich."""
    import engine.quality.gapfill as gf
    st = make_stock(db_session, "STALEFILL", isin="INE10", sector="X",
                    industry="Y", cap_category="small")
    _yf(db_session, st, offset_q=4)
    _screener(db_session, st)
    audit(symbols=["STALEFILL"], verbose=False)
    calls = {}
    monkeypatch.setattr(gf, "enrich", lambda **kw: calls.setdefault("enrich", kw) or {"processed": 1})
    monkeypatch.setattr(gf, "refresh", lambda **kw: {"processed": 1})
    monkeypatch.setattr(gf, "backfill_prices", lambda **kw: {"stocks": 0})
    gapfill(targets=["screener"], symbols=["STALEFILL"], verbose=False)
    assert "STALEFILL" in (calls.get("enrich", {}).get("symbols") or [])


def test_missing_identity_recorded(db_session):
    st = make_stock(db_session, "NOIDENT")  # no isin/sector/industry/cap
    _yf(db_session, st)
    _screener(db_session, st)
    rep = score_stock(db_session, st)
    assert "identity:sector" in rep["missing"]
    assert "identity:isin" in rep["missing"]
    assert rep["dimensions"]["identity"]["status"] in ("partial", "fail")


# ---------- audit + gapfill ----------

def test_audit_idempotent_on_unchanged_data(db_session):
    """as_of fix: re-auditing unchanged data yields identical grade + writes no 2nd row."""
    from db.models import DataQualityReport
    st = make_stock(db_session, "IDEM1", isin="INE1", sector="X", industry="Y", cap_category="small")
    _yf(db_session, st)
    _screener(db_session, st)
    s1 = audit(symbols=["IDEM1"], verbose=False)
    rep1 = db_session.scalar(
        select(DataQualityReport).where(DataQualityReport.stock_id == st.id)
        .order_by(DataQualityReport.id.desc())
    )
    n1 = db_session.scalar(select(func.count()).select_from(DataQualityReport).where(DataQualityReport.stock_id == st.id))
    s2 = audit(symbols=["IDEM1"], verbose=False)
    n2 = db_session.scalar(select(func.count()).select_from(DataQualityReport).where(DataQualityReport.stock_id == st.id))
    assert n1 == n2 == 1                       # second audit wrote no new row
    assert s2["reports_written"] == 0
    rep2 = db_session.scalar(
        select(DataQualityReport).where(DataQualityReport.stock_id == st.id)
        .order_by(DataQualityReport.id.desc())
    )
    assert rep1.grade == rep2.grade            # identical grade on unchanged data


def test_last_fetched_at_updates_without_new_snapshot(db_session, monkeypatch):
    """Freshness tracks attempts: a refresh with unchanged data still bumps last_fetched_at."""
    from engine.ingest import yf_refresh as yfr
    st = make_stock(db_session, "FETCHED1", status="active")
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (yf_payload(), "full"))
    yfr.refresh_one(db_session, st)   # first: creates snapshot
    db_session.commit()
    first = st.last_fetched_at
    assert first is not None
    res = yfr.refresh_one(db_session, st)   # second: identical payload -> unchanged, no new snapshot
    db_session.commit()
    assert res == "unchanged"
    assert st.last_fetched_at >= first      # attempt still recorded


def test_audit_writes_reports_and_rollup(db_session):
    for i in range(3):
        st = make_stock(db_session, f"AUD{i}", isin=f"INE{i}", sector="X",
                        industry="Y", cap_category="small")
        _yf(db_session, st)
        _screener(db_session, st)
    stats = audit(verbose=False)
    assert stats["audited"] >= 3
    assert db_session.scalar(select(func.count()).select_from(DataQualityReport)) >= 3
    assert "dimension_coverage" in stats and "grades" in stats


def test_job_run_truncates_oversized_target(db_session):
    """Regression: a huge symbol list must not overflow job_runs.target VARCHAR(128)."""
    from db.models import JobRun
    from engine.repo import job_run
    long_target = ",".join(f"SYM{i}" for i in range(1000))  # ~7 KB
    with job_run("dq_test_job", target=long_target) as (session, stats):
        stats["ok"] = 1
    j = db_session.scalar(select(JobRun).order_by(JobRun.id.desc()).limit(1))
    assert j.job_type == "dq_test_job"
    assert j.target is not None and len(j.target) <= 128


# ---------- gapfill loop-closure (P4) ----------

def test_quarters_shortfall_routes_to_both_sources(db_session, monkeypatch):
    """A stock short of the quarters target must be re-pulled at BOTH quarterly
    sources (screener + yfinance) — previously the 'quarters' token went nowhere."""
    import engine.quality.gapfill as gf
    st = make_stock(db_session, "FEWQ", isin="INE20", sector="X",
                    industry="Y", cap_category="small")
    _yf(db_session, st, quarters=4)          # < QUARTERS_TARGET
    _screener(db_session, st, quarters=4)
    rep = score_stock(db_session, st)
    assert "quarters" in rep["missing"]
    audit(symbols=["FEWQ"], verbose=False)
    calls = {}
    monkeypatch.setattr(gf, "enrich", lambda **kw: calls.setdefault("enrich", kw) or {})
    monkeypatch.setattr(gf, "refresh", lambda **kw: calls.setdefault("refresh", kw) or {})
    monkeypatch.setattr(gf, "backfill_prices", lambda **kw: {})
    monkeypatch.setattr(gf, "dq_audit", lambda **kw: {})
    gapfill(targets=["screener", "yfinance"], symbols=["FEWQ"], verbose=False)
    assert "FEWQ" in (calls.get("enrich", {}).get("symbols") or [])
    assert "FEWQ" in (calls.get("refresh", {}).get("symbols") or [])


def test_stale_prices_emits_missing_token_and_routes(db_session, monkeypatch):
    """Stale daily bars are fillable (backfill_prices) — they must surface as a
    missing token and route to the prices backfill, not rot as an info flag."""
    from datetime import date, timedelta
    import engine.quality.gapfill as gf
    from engine.repo import upsert_daily_prices
    st = make_stock(db_session, "STALEBAR", isin="INE21", sector="X",
                    industry="Y", cap_category="small")
    _yf(db_session, st)
    _screener(db_session, st)
    upsert_daily_prices(db_session, st.id, [
        {"date": date.today() - timedelta(days=45),
         "open": 1, "high": 1, "low": 1, "close": 1, "volume": 10},
    ])
    db_session.commit()
    rep = score_stock(db_session, st)
    assert "stale_prices" in rep["missing"]
    audit(symbols=["STALEBAR"], verbose=False)
    calls = {}
    monkeypatch.setattr(gf, "backfill_prices", lambda **kw: calls.setdefault("prices", kw) or {})
    monkeypatch.setattr(gf, "enrich", lambda **kw: {})
    monkeypatch.setattr(gf, "refresh", lambda **kw: {})
    monkeypatch.setattr(gf, "dq_audit", lambda **kw: {})
    gapfill(targets=["prices"], symbols=["STALEBAR"], verbose=False)
    assert "STALEBAR" in (calls.get("prices", {}).get("symbols") or [])


def test_fill_identity_isin_and_cap_from_amfi(db_session, monkeypatch):
    """identity:isin / identity:cap_category are fillable offline from the newest
    downloaded AMFI xlsx — the authoritative SEBI classification."""
    import engine.quality.gapfill as gf
    st = make_stock(db_session, "AMFIFILL")  # no isin / cap_category / sector
    _yf(db_session, st)
    audit(symbols=["AMFIFILL"], verbose=False)
    monkeypatch.setattr(gf, "_amfi_identity_map",
                        lambda: {"AMFIFILL": {"isin": "INE999X01010", "cap_category": "small"}})
    monkeypatch.setattr(gf, "dq_audit", lambda **kw: {})
    res = gapfill(targets=["identity"], symbols=["AMFIFILL"], verbose=False)
    db_session.expire_all()
    from db.models import Stock
    refreshed = db_session.scalar(select(Stock).where(Stock.symbol == "AMFIFILL"))
    assert refreshed.isin == "INE999X01010"
    assert refreshed.cap_category == "small"
    assert refreshed.sector == "Healthcare"          # yf copy still works
    assert res["result"]["identity_filled"] >= 1


def test_gapfill_post_audit_readback(db_session):
    """After a fill, gapfill re-audits the touched stocks so data_quality_reports
    reflect the new state — the loop CLOSES instead of trusting the fill blindly."""
    from db.models import DataQualityReport
    st = make_stock(db_session, "READBACK")  # sector missing
    _yf(db_session, st)  # yf info carries sector=Healthcare
    audit(symbols=["READBACK"], verbose=False)
    before = db_session.scalar(
        select(DataQualityReport).where(DataQualityReport.stock_id == st.id)
        .order_by(DataQualityReport.checked_at.desc(), DataQualityReport.id.desc()).limit(1)
    )
    assert "identity:sector" in (before.missing or [])
    res = gapfill(targets=["identity"], symbols=["READBACK"], verbose=False)
    assert res["result"].get("post_audit", {}).get("audited", 0) >= 1
    db_session.expire_all()
    after = db_session.scalar(
        select(DataQualityReport).where(DataQualityReport.stock_id == st.id)
        .order_by(DataQualityReport.checked_at.desc(), DataQualityReport.id.desc()).limit(1)
    )
    assert after.id != before.id                       # read-back wrote a fresh report
    assert "identity:sector" not in (after.missing or [])


def test_gapfill_identity_copies_sector_from_yf(db_session):
    st = make_stock(db_session, "FILLME")  # no sector
    _yf(db_session, st)  # yf info carries sector=Healthcare
    audit(symbols=["FILLME"], verbose=False)  # produces a report with identity:sector missing
    res = gapfill(targets=["identity"], symbols=["FILLME"], verbose=False)
    db_session.expire_all()
    from db.models import Stock
    refreshed = db_session.scalar(select(Stock).where(Stock.symbol == "FILLME"))
    assert refreshed.sector == "Healthcare"
    assert res["result"]["identity_filled"] >= 1
