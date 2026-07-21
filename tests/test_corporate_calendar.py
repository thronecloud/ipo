"""Corporate events calendar + the event-driven chase.

Two concerns:
  1. calendar ingest — NSE event-calendar parse, results classification, symbol
     resolution, and (symbol, event_date, purpose_hash) dedup idempotency.
  2. the event trigger — chase selection (today/yesterday, unchased, universe-only,
     results-only), the chain firing each step behind monkeypatched seams and marking
     chased, the analysis_queue write, and the analyze job's queue-first selection.

Fixture-driven parsers; the chase reuses the announcement/report/refresh fetchers, all
monkeypatched, so nothing here touches the network.
"""

import json
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import func, select

from db.models import AnalysisQueue, CorporateEvent, JobRun
from engine.analysis.engine import consume_queue, find_work
from engine.ingest import corporate_calendar as cal
from engine.repo import add_snapshot, extract_columns
from factories import make_stock, utc, yf_payload

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def _full_snapshot(session, stock, minutes=0):
    payload = yf_payload()
    add_snapshot(session, stock, payload, extract_columns(payload["info"]),
                 data_quality="full", captured_at=utc(minutes))
    session.commit()


def _add_event(session, *, symbol, stock_id, event_date, event_type="results",
               purpose="Financial Results", chased_at=None):
    ev = CorporateEvent(
        symbol=symbol, stock_id=stock_id, event_type=event_type,
        event_date=event_date, purpose_text=purpose,
        purpose_hash=cal._purpose_hash(purpose), source="NSE", chased_at=chased_at,
    )
    session.add(ev)
    session.commit()
    return ev


# ---------- parse + classify ----------

def test_parse_maps_fields_and_classifies():
    rows = cal.parse_event_calendar(_load("nse_event_calendar.json"))
    # 6 raw rows: 1 dup kept (parser doesn't dedup), 1 no-symbol + 1 no-date dropped → 4.
    assert len(rows) == 4
    first = rows[0]
    assert first["symbol"] == "ATHERENERG"
    assert first["event_type"] == "results"
    assert first["event_date"] == date(2026, 7, 22)
    assert first["purpose_text"] == "Financial Results"
    # a dividend-only / fund-raising row is 'other', not a results trigger
    fund = next(r for r in rows if r["symbol"] == "SOMECO")
    assert fund["event_type"] == "other"


def test_parse_drops_rows_without_symbol_or_date():
    rows = cal.parse_event_calendar(_load("nse_event_calendar.json"))
    symbols = {r["symbol"] for r in rows}
    assert None not in symbols
    assert "NODATECO" not in symbols   # blank date dropped


def test_parse_skips_non_dict_rows():
    assert cal.parse_event_calendar([1, "x", None]) == []


def test_classify_event():
    assert cal.classify_event("Financial Results") == "results"
    assert cal.classify_event("Financial Results/Dividend") == "results"
    assert cal.classify_event("Fund Raising") == "other"
    assert cal.classify_event("Dividend") == "other"
    assert cal.classify_event(None) == "other"


# ---------- resolution + dedup ----------

def test_upsert_resolves_symbol_and_flags_unmatched(db_session):
    make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    rows = cal.parse_event_calendar(_load("nse_event_calendar.json"))
    counts = {}
    # 4 parsed rows, but the two identical ATHERENERG rows dedup → 3 stored.
    added = cal.upsert_events(db_session, rows, counts)
    db_session.commit()
    assert added == 3
    ather = db_session.scalars(
        select(CorporateEvent).where(CorporateEvent.symbol == "ATHERENERG")
    ).all()
    assert len(ather) == 1
    assert ather[0].stock_id is not None
    reliance = db_session.scalar(
        select(CorporateEvent).where(CorporateEvent.symbol == "RELIANCE")
    )
    assert reliance.stock_id is None          # not in our universe
    assert counts["unmatched"] == 2           # RELIANCE + SOMECO


def test_upsert_is_idempotent_on_second_run(db_session):
    rows = cal.parse_event_calendar(_load("nse_event_calendar.json"))
    assert cal.upsert_events(db_session, rows) == 3
    db_session.commit()
    rows_again = cal.parse_event_calendar(_load("nse_event_calendar.json"))
    assert cal.upsert_events(db_session, rows_again) == 0
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(CorporateEvent)) == 3


def test_fetch_calendar_stores_and_records_job(db_session, monkeypatch):
    make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    monkeypatch.setattr(cal, "_fetch_event_calendar",
                        lambda: _load("nse_event_calendar.json"))
    stats = cal.fetch_calendar(verbose=False)
    assert stats["added"] == 3
    assert stats["processed"] == 4
    job = db_session.scalar(
        select(JobRun).where(JobRun.job_type == "corporate_calendar")
        .order_by(JobRun.id.desc())
    )
    assert job.stats["added"] == 3


# ---------- chase selection ----------

def test_select_chase_events_today_yesterday_unchased_universe_only(db_session):
    stock = make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    today = date.today()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    _add_event(db_session, symbol="ATHERENERG", stock_id=stock.id, event_date=today)
    _add_event(db_session, symbol="ATHERENERG", stock_id=stock.id, event_date=yesterday,
               purpose="Financial Results/Dividend")
    # excluded: future date, already chased, non-results, non-universe (stock_id NULL)
    _add_event(db_session, symbol="ATHERENERG", stock_id=stock.id, event_date=tomorrow,
               purpose="Financial Results/Other")
    _add_event(db_session, symbol="ATHERENERG", stock_id=stock.id, event_date=today,
               purpose="Chased already", chased_at=utc())
    _add_event(db_session, symbol="ATHERENERG", stock_id=stock.id, event_date=today,
               event_type="other", purpose="Dividend")
    _add_event(db_session, symbol="RELIANCE", stock_id=None, event_date=today,
               purpose="Financial Results/RIL")

    events = cal.select_chase_events(db_session, today)
    got = {(e.symbol, e.event_date) for e in events}
    assert got == {("ATHERENERG", today), ("ATHERENERG", yesterday)}


def _chase_recorders(monkeypatch, refresh=lambda session, stock: "new_snapshot"):
    calls = {"announcements": 0, "reports": [], "refresh": []}

    def rec_ann(verbose=False):
        calls["announcements"] += 1
        return {}

    def rec_reports(symbols=None, **kw):
        calls["reports"].append(list(symbols or []))
        return {}

    def rec_refresh(session, stock):
        calls["refresh"].append(stock.symbol)
        return refresh(session, stock)

    monkeypatch.setattr(cal, "fetch_announcements", rec_ann)
    monkeypatch.setattr(cal, "fetch_reports", rec_reports)
    monkeypatch.setattr(cal, "refresh_one", rec_refresh)
    return calls


def test_chase_runs_full_chain_and_marks_chased(db_session, monkeypatch):
    stock = make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    today = date.today()
    ev = _add_event(db_session, symbol="ATHERENERG", stock_id=stock.id, event_date=today)

    calls = _chase_recorders(monkeypatch)
    stats = cal.chase_events(verbose=False)

    # (a) announcements once feed-wide; (b) reports symbol-filtered; (c) refresh
    assert calls["announcements"] == 1
    assert calls["reports"] == [["ATHERENERG"]]
    assert calls["refresh"] == ["ATHERENERG"]
    assert stats["stocks"] == 1 and stats["queued"] == 1 and stats["refreshed"] == 1

    # (d) analysis_queue row written, (e) event marked chased
    q = db_session.scalars(select(AnalysisQueue)).all()
    assert len(q) == 1
    assert q[0].stock_id == stock.id and q[0].reason == "results"
    assert q[0].consumed_at is None
    db_session.refresh(ev)
    assert ev.chased_at is not None

    job = db_session.scalar(
        select(JobRun).where(JobRun.job_type == "event_chase").order_by(JobRun.id.desc())
    )
    assert job.stats["queued"] == 1


def test_chase_with_no_due_events_is_a_noop(db_session, monkeypatch):
    make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    # an event, but for tomorrow → nothing due
    _add_event(db_session, symbol="ATHERENERG", stock_id=None, event_date=date.today())
    calls = _chase_recorders(monkeypatch)
    stats = cal.chase_events(verbose=False)
    assert calls["announcements"] == 0        # not even the context fetch fires
    assert stats["events"] == 0 and stats["queued"] == 0
    assert db_session.scalar(select(func.count()).select_from(AnalysisQueue)) == 0


def test_chase_is_idempotent_second_run_finds_nothing(db_session, monkeypatch):
    stock = make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    _add_event(db_session, symbol="ATHERENERG", stock_id=stock.id, event_date=date.today())
    _chase_recorders(monkeypatch)
    cal.chase_events(verbose=False)
    calls2 = _chase_recorders(monkeypatch)
    stats = cal.chase_events(verbose=False)
    assert stats["events"] == 0               # the first run marked it chased
    assert calls2["announcements"] == 0
    assert db_session.scalar(select(func.count()).select_from(AnalysisQueue)) == 1


# ---------- queue write + analyze selection preference ----------

def test_queue_analysis_writes_pending_row(db_session):
    stock = make_stock(db_session, "AAA")
    cal.queue_analysis(db_session, stock.id, "results")
    db_session.commit()
    row = db_session.scalar(select(AnalysisQueue))
    assert row.stock_id == stock.id and row.reason == "results"
    assert row.consumed_at is None


def test_consume_queue_marks_pending_consumed_and_is_idempotent(db_session):
    stock = make_stock(db_session, "AAA")
    cal.queue_analysis(db_session, stock.id, "results")
    db_session.commit()
    assert consume_queue(db_session, [stock.id]) == 1
    db_session.commit()
    assert db_session.scalar(select(AnalysisQueue)).consumed_at is not None
    assert consume_queue(db_session, [stock.id]) == 0   # nothing left pending


def test_analyze_prefers_queued_stock_first(db_session):
    # Two stocks both needing analysis; queue the alphabetically-later one.
    aaa = make_stock(db_session, "AAA")
    zzz = make_stock(db_session, "ZZZ")
    _full_snapshot(db_session, aaa)
    _full_snapshot(db_session, zzz)

    # Empty queue → plain symbol order (AAA before ZZZ).
    order = [s.symbol for s, _snap, _slug in find_work(db_session, ["warren_buffett"])]
    assert order == ["AAA", "ZZZ"]

    cal.queue_analysis(db_session, zzz.id, "results")
    db_session.commit()

    # Queued stock floats to the front.
    order = [s.symbol for s, _snap, _slug in find_work(db_session, ["warren_buffett"])]
    assert order == ["ZZZ", "AAA"]


def test_queue_ordering_is_oldest_first(db_session):
    aaa = make_stock(db_session, "AAA")
    bbb = make_stock(db_session, "BBB")
    ccc = make_stock(db_session, "CCC")
    for s in (aaa, bbb, ccc):
        _full_snapshot(db_session, s)

    # Queue CCC first (oldest), then AAA — both ahead of un-queued BBB, CCC before AAA.
    db_session.add(AnalysisQueue(stock_id=ccc.id, reason="results", queued_at=utc(-10)))
    db_session.add(AnalysisQueue(stock_id=aaa.id, reason="results", queued_at=utc(-5)))
    db_session.commit()

    order = [s.symbol for s, _snap, _slug in find_work(db_session, ["warren_buffett"])]
    assert order == ["CCC", "AAA", "BBB"]
