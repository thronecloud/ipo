"""Corporate announcements: NSE/BSE parsers, symbol resolution, dedup idempotency.

Fixture-driven — recorded-shape JSON in tests/fixtures/ feeds the parsers directly;
`fetch_announcements` runs with both `_fetch_*` network seams monkeypatched.
"""

import json
from datetime import date
from pathlib import Path

from sqlalchemy import func, select

from db.models import CorporateAnnouncement, JobRun
from engine.ingest import announcements as ann
from factories import make_stock

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


# ---------- parsers ----------

def test_parse_nse_maps_fields_and_drops_empty_rows():
    rows = ann.parse_nse_announcements(_load("nse_announcements.json"))
    assert len(rows) == 3                      # the all-null 4th row is dropped
    first = rows[0]
    assert first["exchange"] == "NSE"
    assert first["symbol"] == "ATHERENERG"
    assert first["category"] == "Financial Results"
    assert first["headline"].startswith("Ather Energy Ltd")
    assert first["attachment_url"].endswith("ATHERENERG_results_q4.pdf")
    assert first["announced_at"].date() == date(2025, 5, 2)
    assert first["announcement_id"] == "NSE-ANN-1001"


def test_parse_bse_builds_attachment_url_and_uses_scrip_code():
    rows = ann.parse_bse_announcements(_load("bse_announcements.json"))
    assert len(rows) == 2                       # all-null row dropped
    r0 = rows[0]
    assert r0["exchange"] == "BSE"
    assert r0["symbol"] == "543287"
    assert r0["attachment_url"] == ann.BSE_ATTACH_BASE + "abc123.pdf"
    assert r0["category"] == "Result"
    # second row has an empty ATTACHMENTNAME → no attachment url
    assert rows[1]["attachment_url"] is None


def test_parser_skips_non_dict_rows():
    assert ann.parse_nse_announcements([123, "x", None]) == []


# ---------- BSE pagination ----------

def test_bse_table_reads_empty_window_as_no_rows():
    # The empty-window response is the bare string, not a dict.
    assert ann._bse_table("No Record Found!") == []
    assert ann._bse_table({"Table1": [{"ROWCNT": 0}]}) == []
    assert ann._bse_table({"Table": [{"NEWSID": "x"}]}) == [{"NEWSID": "x"}]


def test_fetch_bse_walks_all_pages_until_empty(monkeypatch):
    # Page 1 (2 rows) + page 2 (1 row) + page 3 empty ("No Record Found!"): the
    # loop must gather all three rows, not truncate at page 1, and stop at the
    # first empty page.
    p1 = _load("bse_announcements_page1.json")
    p2 = _load("bse_announcements_page2.json")
    calls = []

    def fake_page(http, pageno, prev_date, to_date):
        calls.append(pageno)
        if pageno == 1:
            return p1
        if pageno == 2:
            return p2
        return "No Record Found!"

    monkeypatch.setattr(ann, "_fetch_bse_page", fake_page)
    payload = ann._fetch_bse_announcements()
    ids = [r["NEWSID"] for r in payload["Table"]]
    assert ids == ["BSE-P1-1", "BSE-P1-2", "BSE-P2-1"]   # page 1 was NOT truncated
    assert calls == [1, 2, 3]                            # stopped at first empty page
    # and the accumulated payload parses through the normal parser
    rows = ann.parse_bse_announcements(payload)
    assert {r["announcement_id"] for r in rows} == {"BSE-P1-1", "BSE-P1-2", "BSE-P2-1"}


# ---------- resolution + dedup ----------

def test_upsert_resolves_known_symbol_and_flags_unmatched(db_session):
    make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    rows = ann.parse_nse_announcements(_load("nse_announcements.json"))
    counts = {}
    added = ann.upsert_announcements(db_session, rows, counts)
    db_session.commit()
    assert added == 3
    # two ATHERENERG rows resolve; NONEXISTENTCO stays NULL and is counted unmatched.
    matched = db_session.scalars(
        select(CorporateAnnouncement).where(CorporateAnnouncement.symbol == "ATHERENERG")
    ).all()
    assert all(r.stock_id is not None for r in matched)
    unmatched = db_session.scalar(
        select(CorporateAnnouncement).where(CorporateAnnouncement.symbol == "NONEXISTENTCO")
    )
    assert unmatched.stock_id is None
    assert counts["unmatched"] == 1


def test_upsert_is_idempotent_on_second_run(db_session):
    make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    rows = ann.parse_nse_announcements(_load("nse_announcements.json"))
    assert ann.upsert_announcements(db_session, rows) == 3
    db_session.commit()
    # The payload is RE-PARSED, as a real re-fetch would: idempotency must survive
    # the full parse->hash->upsert path, not just a reused row list. A dedup hash
    # that isn't stable across parses would slip duplicates in nightly.
    rows_again = ann.parse_nse_announcements(_load("nse_announcements.json"))
    assert ann.upsert_announcements(db_session, rows_again) == 0
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(CorporateAnnouncement)) == 3


def test_same_hash_different_exchange_are_both_kept(db_session):
    # A hand-built collision: identical content, different exchange → distinct rows.
    row_nse = {"exchange": "NSE", "symbol": "X", "headline": "h", "category": "c",
               "attachment_url": None, "announced_at": None, "announcement_id": "SAME",
               "dedup_hash": "deadbeef", "raw": {}}
    row_bse = dict(row_nse, exchange="BSE")
    ann.upsert_announcements(db_session, [row_nse, row_bse])
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(CorporateAnnouncement)) == 2


# ---------- job entrypoint ----------

def test_fetch_announcements_stores_both_feeds(db_session, monkeypatch):
    make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    monkeypatch.setattr(ann, "_fetch_nse_announcements",
                        lambda: _load("nse_announcements.json"))
    monkeypatch.setattr(ann, "_fetch_bse_announcements",
                        lambda: _load("bse_announcements.json"))
    stats = ann.fetch_announcements(verbose=False)
    assert stats["added"] == 5                  # 3 NSE + 2 BSE
    assert stats["soft_fail"] == 0
    job = db_session.scalar(
        select(JobRun).where(JobRun.job_type == "corporate_announcements")
        .order_by(JobRun.id.desc())
    )
    assert job.stats["added"] == 5


def test_fetch_announcements_one_feed_down_does_not_sink_the_other(db_session, monkeypatch):
    def boom():
        raise RuntimeError("NSE blocked the datacenter IP")

    monkeypatch.setattr(ann, "_fetch_nse_announcements", boom)
    monkeypatch.setattr(ann, "_fetch_bse_announcements",
                        lambda: _load("bse_announcements.json"))
    stats = ann.fetch_announcements(verbose=False)
    assert stats["soft_fail"] == 1
    assert stats["added"] == 2                  # BSE still landed
