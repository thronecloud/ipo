"""Autonomous report fetcher: classification, download budget + per-file cap,
sha256 content dedup, (stock_id, source_url) idempotency, and the fresh `since` filter.

`_download_file` and `_fetch_annual_reports` are monkeypatched, so no file is written
and no network is touched — the download outcomes are scripted per URL.
"""

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from db.models import CorporateAnnouncement, CorporateFiling, JobRun
from engine.ingest import reports
from factories import make_stock

MB = 1024 * 1024


def _add_ann(session, stock_id, *, symbol, category, url, when, hid):
    session.add(CorporateAnnouncement(
        stock_id=stock_id, symbol=symbol, exchange="NSE", headline="h",
        category=category, attachment_url=url, announced_at=when,
        announcement_id=hid, dedup_hash=hid, raw={}))
    session.commit()


def _fake_download(*, size_by_url=None, sha_by_url=None, default_size=MB):
    calls = []

    def fake(http, url, dest, max_bytes):
        calls.append(url)
        size = (size_by_url or {}).get(url, default_size)
        if size > max_bytes:
            return None
        sha = (sha_by_url or {}).get(url) or hashlib.sha256(url.encode()).hexdigest()
        return sha, size

    return fake, calls


def _no_annual_reports(monkeypatch):
    monkeypatch.setattr(reports, "_fetch_annual_reports",
                        lambda http, symbol: {"data": []})


# ---------- classification ----------

def test_classify_maps_result_and_annual_report_categories():
    assert reports.classify("Financial Results") == "results"
    assert reports.classify("Annual Report") == "annual_report"
    assert reports.classify("Board Meeting Intimation") is None
    assert reports.classify(None) is None


# ---------- happy path ----------

def test_downloads_report_announcements_and_ignores_non_reports(db_session, monkeypatch):
    stock = make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    now = datetime.now(timezone.utc)
    _add_ann(db_session, stock.id, symbol="ATHERENERG", category="Financial Results",
             url="https://x/res.pdf", when=now, hid="a1")
    _add_ann(db_session, stock.id, symbol="ATHERENERG", category="Board Meeting",
             url="https://x/bm.pdf", when=now, hid="a2")
    fake, calls = _fake_download()
    monkeypatch.setattr(reports, "_download_file", fake)
    _no_annual_reports(monkeypatch)

    stats = reports.fetch_reports(verbose=False)

    assert stats["downloaded"] == 1
    assert calls == ["https://x/res.pdf"]        # the board-meeting doc is not a report
    row = db_session.scalar(select(CorporateFiling))
    assert row.status == "downloaded" and row.filing_type == "results"
    assert row.local_path and row.sha256


def test_idempotent_second_run_downloads_nothing(db_session, monkeypatch):
    stock = make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    _add_ann(db_session, stock.id, symbol="ATHERENERG", category="Financial Results",
             url="https://x/res.pdf", when=datetime.now(timezone.utc), hid="a1")
    _no_annual_reports(monkeypatch)

    fake, calls = _fake_download()
    monkeypatch.setattr(reports, "_download_file", fake)
    reports.fetch_reports(verbose=False)

    fake2, calls2 = _fake_download()
    monkeypatch.setattr(reports, "_download_file", fake2)
    stats = reports.fetch_reports(verbose=False)

    assert calls2 == []                          # (stock_id, source_url) already fetched
    assert stats["skipped_existing"] == 1
    assert db_session.scalar(select(func.count()).select_from(CorporateFiling)) == 1


# ---------- disk guards ----------

def test_per_run_budget_stops_downloads_and_leaves_them_retryable(db_session, monkeypatch):
    stock = make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    now = datetime.now(timezone.utc)
    _add_ann(db_session, stock.id, symbol="ATHERENERG", category="Financial Results",
             url="https://x/one.pdf", when=now, hid="a1")
    _add_ann(db_session, stock.id, symbol="ATHERENERG", category="Financial Results",
             url="https://x/two.pdf", when=now - timedelta(hours=1), hid="a2")
    fake, calls = _fake_download(default_size=MB)   # 1 MB each
    monkeypatch.setattr(reports, "_download_file", fake)
    _no_annual_reports(monkeypatch)

    stats = reports.fetch_reports(budget_mb=1, verbose=False)   # room for exactly one

    assert stats["downloaded"] == 1
    assert stats["skipped_budget"] == 1
    assert len(calls) == 1
    # the budget-skipped candidate wrote NO row, so a later run will retry it.
    assert db_session.scalar(select(func.count()).select_from(CorporateFiling)) == 1


def test_over_cap_file_is_recorded_skipped_cap(db_session, monkeypatch):
    stock = make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    _add_ann(db_session, stock.id, symbol="ATHERENERG", category="Financial Results",
             url="https://x/huge.pdf", when=datetime.now(timezone.utc), hid="a1")
    fake, _ = _fake_download(default_size=999 * MB)   # over the per-file cap
    monkeypatch.setattr(reports, "_download_file", fake)
    _no_annual_reports(monkeypatch)

    stats = reports.fetch_reports(verbose=False)

    assert stats["skipped_cap"] == 1
    assert stats["downloaded"] == 0
    row = db_session.scalar(select(CorporateFiling))
    assert row.status == "skipped_cap" and row.local_path is None


def test_sha256_content_dedup_within_stock(db_session, monkeypatch):
    stock = make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    now = datetime.now(timezone.utc)
    _add_ann(db_session, stock.id, symbol="ATHERENERG", category="Financial Results",
             url="https://x/a.pdf", when=now, hid="a1")
    _add_ann(db_session, stock.id, symbol="ATHERENERG", category="Financial Results",
             url="https://x/b.pdf", when=now - timedelta(hours=1), hid="a2")
    # both URLs resolve to the SAME content hash (same PDF re-published)
    fake, _ = _fake_download(sha_by_url={"https://x/a.pdf": "SAME", "https://x/b.pdf": "SAME"})
    monkeypatch.setattr(reports, "_download_file", fake)
    _no_annual_reports(monkeypatch)

    stats = reports.fetch_reports(verbose=False)

    assert stats["downloaded"] == 1
    assert stats["duplicate"] == 1
    dup = db_session.scalar(select(CorporateFiling).where(CorporateFiling.status == "duplicate"))
    assert dup.local_path is None and dup.sha256 == "SAME"


# ---------- fresh trigger ----------

def test_since_filters_out_stale_announcements(db_session, monkeypatch):
    stock = make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    now = datetime.now(timezone.utc)
    _add_ann(db_session, stock.id, symbol="ATHERENERG", category="Financial Results",
             url="https://x/fresh.pdf", when=now, hid="fresh")
    _add_ann(db_session, stock.id, symbol="ATHERENERG", category="Financial Results",
             url="https://x/old.pdf", when=now - timedelta(days=10), hid="old")
    fake, calls = _fake_download()
    monkeypatch.setattr(reports, "_download_file", fake)
    _no_annual_reports(monkeypatch)

    reports.fetch_reports(since=now - timedelta(days=2), verbose=False)

    assert calls == ["https://x/fresh.pdf"]      # the 10-day-old one is out of window


def test_annual_reports_endpoint_feeds_candidates(db_session, monkeypatch):
    stock = make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    monkeypatch.setattr(reports, "_fetch_annual_reports", lambda http, symbol: {
        "data": [{"fromYr": "2024", "toYr": "2025",
                  "fileName": "https://x/ar.pdf"}]})
    fake, calls = _fake_download()
    monkeypatch.setattr(reports, "_download_file", fake)

    stats = reports.fetch_reports(verbose=False)

    assert calls == ["https://x/ar.pdf"]
    assert stats["downloaded"] == 1
    row = db_session.scalar(select(CorporateFiling))
    assert row.filing_type == "annual_report" and row.period == "2024-2025"
