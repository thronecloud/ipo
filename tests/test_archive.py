"""Bitemporal raw-payload archive: roundtrip, content dedup, the never-fail-a-fetch
contract at the seam, age/budget retention, and end-to-end replay.

The archive writes real compressed files, so every test points ARCHIVE_ROOT at its own
tmp_path. Nothing here touches the network — fetch seams are monkeypatched.
"""

import gzip
import json
import os
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select

from db.models import CorporateAnnouncement, JobRun, RawPayload, utcnow
from engine.ingest import archive
from engine.ingest import corporate_calendar as cal
from factories import make_stock

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def _use_tmp_archive(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIVE_ROOT", str(tmp_path))


# ---------- roundtrip ----------

def test_archive_roundtrip_writes_compressed_file_and_index_row(db_session, monkeypatch, tmp_path):
    _use_tmp_archive(monkeypatch, tmp_path)
    payload = {"hello": "world", "n": [1, 2, 3]}
    sha = archive.archive_payload("announcements", "NSE", payload)

    row = db_session.scalar(select(RawPayload).where(RawPayload.sha256 == sha))
    assert row is not None
    assert row.source == "announcements" and row.entity == "NSE"
    assert row.content_type == "application/json"
    assert row.pruned_at is None

    # the file exists, is gzip, and round-trips back to the raw payload bytes
    p = Path(row.path)
    assert p.exists() and p.suffix == ".gz"
    assert row.path.startswith(str(tmp_path))
    restored = json.loads(gzip.decompress(p.read_bytes()))
    assert restored == payload
    assert row.bytes == p.stat().st_size


def test_archive_zip_passthrough_is_stored_verbatim(db_session, monkeypatch, tmp_path):
    _use_tmp_archive(monkeypatch, tmp_path)
    blob = os.urandom(2048)   # already-compressed bytes must NOT be gzipped again
    sha = archive.archive_payload("bhavcopy", "NSE-2026-07-20", blob,
                                  content_type="application/zip")
    row = db_session.scalar(select(RawPayload).where(RawPayload.sha256 == sha))
    assert row.path.endswith(".zip")
    assert Path(row.path).read_bytes() == blob     # verbatim, not double-compressed


# ---------- content dedup ----------

def test_archive_dedup_same_payload_one_file_one_row(db_session, monkeypatch, tmp_path):
    _use_tmp_archive(monkeypatch, tmp_path)
    payload = {"a": 1}
    sha1 = archive.archive_payload("bulk_deals", "NSE", payload, fetched_at=utcnow())
    sha2 = archive.archive_payload("bulk_deals", "NSE", payload,
                                   fetched_at=utcnow() + timedelta(hours=1))
    assert sha1 == sha2
    # exactly one index row and one file on disk despite two calls
    assert db_session.scalar(select(func.count()).select_from(RawPayload)) == 1
    files = list(tmp_path.rglob("*.json.gz"))
    assert len(files) == 1


# ---------- never fail a fetch ----------

def test_archive_safe_swallows_failure_and_counts_it(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(archive, "archive_payload", boom)
    counts = {}
    assert archive.archive_safe("announcements", "NSE", {"x": 1}, counts) is None
    assert counts["archive_failed"] == 1


def test_fetch_seam_survives_archive_failure(db_session, monkeypatch, tmp_path):
    """A raising archive must never fail the fetch: rows still land, the job still
    succeeds, and the failure is tallied in stats."""
    _use_tmp_archive(monkeypatch, tmp_path)
    make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    monkeypatch.setattr(cal, "_fetch_event_calendar",
                        lambda: _load("nse_event_calendar.json"))
    monkeypatch.setattr(archive, "archive_payload",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))

    stats = cal.fetch_calendar(verbose=False)
    assert stats["added"] == 3               # the fetch fully succeeded
    assert stats["archive_failed"] == 1
    job = db_session.scalar(
        select(JobRun).where(JobRun.job_type == "corporate_calendar").order_by(JobRun.id.desc())
    )
    assert job.status == "success"


def test_fetch_seam_archives_before_parse(db_session, monkeypatch, tmp_path):
    _use_tmp_archive(monkeypatch, tmp_path)
    make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    monkeypatch.setattr(cal, "_fetch_event_calendar",
                        lambda: _load("nse_event_calendar.json"))
    cal.fetch_calendar(verbose=False)
    # the raw calendar payload is now archived, keyed (corporate_calendar, NSE)
    row = db_session.scalar(
        select(RawPayload).where(RawPayload.source == "corporate_calendar")
    )
    assert row is not None and row.entity == "NSE"


# ---------- retention ----------

def _archive_verbatim(entity, blob, fetched_at):
    """Archive incompressible bytes verbatim so file sizes are controllable."""
    return archive.archive_payload("bulk_deals", entity, blob,
                                   content_type="application/zip", fetched_at=fetched_at)


def test_retention_budget_prunes_oldest_first_keeps_index(db_session, monkeypatch, tmp_path):
    _use_tmp_archive(monkeypatch, tmp_path)
    now = utcnow()
    size = 600 * 1024                       # ~0.6 MB each, three files ~1.8 MB total
    shas = []
    for i in range(3):
        shas.append(_archive_verbatim(f"e{i}", os.urandom(size),
                                      now - timedelta(hours=3 - i)))   # e0 oldest

    # Budget 1 MB, age effectively off → prune oldest-first until under budget.
    stats = archive.prune_archive(now=now, budget_mb=1, retention_days=99999, verbose=False)
    assert stats["pruned"] == 2 and stats["pruned_budget"] == 2
    assert stats["live_after"] == 1

    rows = {r.sha256: r for r in db_session.scalars(select(RawPayload)).all()}
    assert len(rows) == 3                    # every index row survives pruning
    assert rows[shas[0]].path is None and rows[shas[0]].pruned_at is not None   # oldest gone
    assert rows[shas[1]].path is None
    assert rows[shas[2]].path is not None    # newest kept
    assert not list(tmp_path.rglob("*.zip")) or len(list(tmp_path.rglob("*.zip"))) == 1


def test_retention_age_prunes_old_keeps_recent(db_session, monkeypatch, tmp_path):
    _use_tmp_archive(monkeypatch, tmp_path)
    now = utcnow()
    old = archive.archive_payload("bulk_deals", "old", {"k": "old"},
                                  fetched_at=now - timedelta(days=200))
    recent = archive.archive_payload("bulk_deals", "recent", {"k": "recent"},
                                     fetched_at=now - timedelta(days=1))

    stats = archive.prune_archive(now=now, budget_mb=10_000, retention_days=180, verbose=False)
    assert stats["pruned"] == 1 and stats["pruned_aged"] == 1

    old_row = db_session.scalar(select(RawPayload).where(RawPayload.sha256 == old))
    recent_row = db_session.scalar(select(RawPayload).where(RawPayload.sha256 == recent))
    assert old_row.path is None and old_row.pruned_at is not None    # record survives
    assert recent_row.path is not None                              # young payload kept


def test_retention_is_idempotent(db_session, monkeypatch, tmp_path):
    _use_tmp_archive(monkeypatch, tmp_path)
    now = utcnow()
    archive.archive_payload("bulk_deals", "x", {"k": 1}, fetched_at=now - timedelta(days=300))
    archive.prune_archive(now=now, retention_days=180, verbose=False)
    stats = archive.prune_archive(now=now, retention_days=180, verbose=False)
    assert stats["pruned"] == 0             # already-pruned rows are never revisited


# ---------- replay ----------

def test_replay_announcements_end_to_end_and_idempotent(db_session, monkeypatch, tmp_path):
    _use_tmp_archive(monkeypatch, tmp_path)
    make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    # archive a recorded NSE announcements payload as the fetch seam would
    archive.archive_payload("announcements", "NSE", _load("nse_announcements.json"))

    stats = archive.replay("announcements", verbose=False)
    assert stats["payloads"] == 1 and stats["replayed"] == 1
    # 3 rows parse from the fixture (the all-null row is dropped)
    assert db_session.scalar(select(func.count()).select_from(CorporateAnnouncement)) == 3

    # replay goes through the idempotent upsert path → a second run lands nothing new
    stats2 = archive.replay("announcements", verbose=False)
    assert stats2["replayed"] == 1
    assert db_session.scalar(select(func.count()).select_from(CorporateAnnouncement)) == 3


def test_replay_since_filters_by_fetched_at(db_session, monkeypatch, tmp_path):
    _use_tmp_archive(monkeypatch, tmp_path)
    now = utcnow()
    archive.archive_payload("corporate_calendar", "NSE", _load("nse_event_calendar.json"),
                            fetched_at=now - timedelta(days=10))
    stats = archive.replay("corporate_calendar",
                           since=(now - timedelta(days=1)).date().isoformat(), verbose=False)
    assert stats["payloads"] == 0           # the only payload predates the cutoff


def test_replay_skips_pruned_payloads(db_session, monkeypatch, tmp_path):
    _use_tmp_archive(monkeypatch, tmp_path)
    now = utcnow()
    archive.archive_payload("corporate_calendar", "NSE", _load("nse_event_calendar.json"),
                            fetched_at=now - timedelta(days=300))
    archive.prune_archive(now=now, retention_days=180, verbose=False)   # bytes gone, row kept
    stats = archive.replay("corporate_calendar", verbose=False)
    assert stats["payloads"] == 1 and stats["pruned_skipped"] == 1 and stats["replayed"] == 0


def test_replay_unsupported_source_reports_clearly():
    stats = archive.replay("shareholding", verbose=False)
    assert stats.get("unsupported") is True
    assert "shareholding" in stats["message"]
