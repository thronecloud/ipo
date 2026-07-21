"""
Bitemporal raw-payload archive — every fetch's untouched source bytes, stored
compressed BEFORE parsing.

Why: a parser is the one lossy step between the wire and the database. When it has a
bug, the fact it mangled or dropped is gone forever — the parsed row is all that
survives. Archiving the raw payload first turns that permanent loss into a re-run:
`engine.run replay` re-parses the stored bytes through the normal (idempotent) upsert
paths, and the state of a source as-known-at any past fetch can be reconstructed.

Storage:
  data/archive/{source}/{YYYY-MM}/{fetched-at}_{entity-slug}_{sha12}.{ext}
gzip (stdlib, zero new deps) for text/JSON payloads; an already-compressed payload
(a bhavcopy ZIP) is stored verbatim with its own suffix — never double-compressed.

Identity is content: `sha256` of the RAW (uncompressed) bytes is the archive's unique
key. An unchanged daily poll hashes to a payload already on disk, so the existing index
row is returned and NOTHING new is written — the archive doesn't grow when the world
didn't change.

Retention (`prune_archive`, a weekly job): deletes the oldest files past a disk budget
AND past a max age, oldest-first, but KEEPS the index row with path=NULL + pruned_at —
the record that a payload once existed outlives its bytes.

`archive_safe` is the seam wrapper: archiving must NEVER fail a fetch, so it swallows
every error, logs it, and tallies `archive_failed` in the job's stats.
"""

import gzip
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from db.base import SessionLocal
from db.models import RawPayload, utcnow
from engine.repo import job_run
from src.utils import log

# Extension (and whether we gzip) per content type. A ZIP is already compressed, so it
# is stored verbatim; JSON/CSV are gzipped.
_CONTENT_EXT = {
    "application/json": (".json.gz", True),
    "text/csv": (".csv.gz", True),
    "application/zip": (".zip", False),
}


def archive_root() -> Path:
    """Where the compressed payloads live. Env-overridable so tests write to a tmp
    tree instead of the repo's data dir."""
    return Path(os.environ.get("ARCHIVE_ROOT", "data/archive"))


def _slug(entity: str) -> str:
    """A filesystem-safe token for an entity key (exchange / symbol / date)."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", str(entity or "unknown")).strip("-").lower()
    return (s or "unknown")[:64]


def _as_bytes(payload) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    # A parsed object (list/dict) — serialize deterministically so the same payload
    # hashes the same across runs.
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def archive_payload(source: str, entity: str, payload, *, fetched_at: datetime | None = None,
                    content_type: str = "application/json") -> str:
    """Archive a fetch's raw bytes and index them. Returns the payload's sha256.

    Runs in its OWN database session so an archive write can never poison the caller's
    transaction. Deduplicates on sha256: identical content returns the existing row's
    digest without writing a second file or index row.
    """
    fetched_at = fetched_at or utcnow()
    raw = _as_bytes(payload)
    sha256 = hashlib.sha256(raw).hexdigest()

    session = SessionLocal()
    try:
        existing = session.scalar(select(RawPayload).where(RawPayload.sha256 == sha256))
        if existing is not None:
            # Same content already archived — do not grow the archive.
            return sha256

        ext, do_gzip = _CONTENT_EXT.get(content_type, (".bin.gz", True))
        stored = gzip.compress(raw) if do_gzip else raw

        month = fetched_at.strftime("%Y-%m")
        stamp = fetched_at.strftime("%Y%m%dT%H%M%S%fZ")
        fname = f"{stamp}_{_slug(entity)}_{sha256[:12]}{ext}"
        directory = archive_root() / source / month
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / fname
        path.write_bytes(stored)

        row = RawPayload(
            source=source, entity=str(entity)[:128], fetched_at=fetched_at,
            sha256=sha256, path=str(path), bytes=len(stored),
            content_type=content_type,
        )
        session.add(row)
        session.commit()
        return sha256
    finally:
        session.close()


def archive_safe(source: str, entity: str, payload, counts: dict | None = None, *,
                 fetched_at: datetime | None = None,
                 content_type: str = "application/json") -> str | None:
    """Archive at a fetch seam without ever failing the fetch.

    Returns the sha256 on success, None on failure. A failure is logged and tallied in
    `counts['archive_failed']` so the job's stats surface it — the fetch itself carries
    on regardless. This is the ONLY entry point the ingest fetchers should call.
    """
    try:
        return archive_payload(source, entity, payload, fetched_at=fetched_at,
                               content_type=content_type)
    except Exception as e:      # noqa: BLE001 — archiving must never sink a fetch
        if counts is not None:
            counts["archive_failed"] = counts.get("archive_failed", 0) + 1
        log(f"archive FAILED [{source}/{entity}]: {e}")
        return None


# ---------- retention ----------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


ARCHIVE_BUDGET_MB = _env_int("ARCHIVE_BUDGET_MB", 500)
ARCHIVE_RETENTION_DAYS = _env_int("ARCHIVE_RETENTION_DAYS", 180)


def _delete_file(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass  # already gone — the goal (bytes not on disk) is met either way


def prune_archive(now: datetime | None = None, *, budget_mb: int | None = None,
                  retention_days: int | None = None, verbose: bool = True) -> dict:
    """Delete archived files past the disk budget AND past the max age, oldest-first,
    keeping their index rows (path=NULL, pruned_at stamped). The record survives; only
    the bytes are reclaimed. Idempotent — an already-pruned row is never revisited.
    """
    now = now or utcnow()
    budget_bytes = (ARCHIVE_BUDGET_MB if budget_mb is None else budget_mb) * 1024 * 1024
    days = ARCHIVE_RETENTION_DAYS if retention_days is None else retention_days
    cutoff = now - timedelta(days=days)

    with job_run("archive_prune", target=f"budget={budget_mb or ARCHIVE_BUDGET_MB}MB") as (session, stats):
        counts = {"pruned": 0, "pruned_aged": 0, "pruned_budget": 0,
                  "freed_bytes": 0, "live_after": 0}
        live = session.scalars(
            select(RawPayload)
            .where(RawPayload.path.isnot(None))
            .order_by(RawPayload.fetched_at.asc(), RawPayload.id.asc())
        ).all()
        total = sum((r.bytes or 0) for r in live)

        for row in live:
            too_old = row.fetched_at is not None and row.fetched_at < cutoff
            over_budget = total > budget_bytes
            if not (too_old or over_budget):
                counts["live_after"] += 1
                continue
            _delete_file(row.path)
            counts["freed_bytes"] += row.bytes or 0
            counts["pruned"] += 1
            if too_old:
                counts["pruned_aged"] += 1
            else:
                counts["pruned_budget"] += 1
            total -= row.bytes or 0
            row.path = None
            row.pruned_at = now

        session.commit()
        stats.update(counts)
        if verbose:
            print(f"  archive prune: {counts['pruned']} file(s) freed "
                  f"({counts['freed_bytes'] // (1024 * 1024)} MB), "
                  f"{counts['live_after']} live")
    return stats


# ---------- replay ----------

def _read_archived(row: RawPayload) -> bytes:
    """The raw (uncompressed) bytes of an archived payload, decompressing by suffix."""
    data = Path(row.path).read_bytes()
    if row.path.endswith(".gz"):
        return gzip.decompress(data)
    return data


def _replay_announcements(session, entity: str, payload, counts: dict) -> None:
    from engine.ingest.announcements import (
        parse_bse_announcements,
        parse_nse_announcements,
        upsert_announcements,
    )
    parser = parse_bse_announcements if entity.upper() == "BSE" else parse_nse_announcements
    upsert_announcements(session, parser(payload), counts)


def _replay_calendar(session, entity: str, payload, counts: dict) -> None:
    from engine.ingest.corporate_calendar import parse_event_calendar, upsert_events
    upsert_events(session, parse_event_calendar(payload), counts)


# Per-source replay support. A source absent here can be archived but not yet
# replayed — the CLI says so rather than silently doing nothing.
REPLAY_HANDLERS = {
    "announcements": _replay_announcements,
    "corporate_calendar": _replay_calendar,
}


def replay(source: str, *, since: str | None = None, entity: str | None = None,
           verbose: bool = True) -> dict:
    """Re-parse archived payloads for a source through the normal (idempotent) upsert
    paths. `since` is an ISO date/datetime lower bound on fetched_at; `entity` narrows
    to one exchange/symbol. Pruned rows (bytes gone) are counted and skipped."""
    handler = REPLAY_HANDLERS.get(source)
    if handler is None:
        supported = ", ".join(sorted(REPLAY_HANDLERS)) or "(none)"
        msg = f"replay unsupported for source {source!r}; supported: {supported}"
        if verbose:
            print(f"  {msg}")
        return {"unsupported": True, "source": source, "message": msg}

    since_dt = _parse_since(since)

    with job_run("replay", target=f"{source}/{entity or 'all'}") as (session, stats):
        counts = {"payloads": 0, "replayed": 0, "pruned_skipped": 0, "added": 0, "failed": 0}
        q = select(RawPayload).where(RawPayload.source == source)
        if entity is not None:
            q = q.where(RawPayload.entity == entity)
        if since_dt is not None:
            q = q.where(RawPayload.fetched_at >= since_dt)
        rows = session.scalars(q.order_by(RawPayload.fetched_at.asc(), RawPayload.id.asc())).all()

        for row in rows:
            counts["payloads"] += 1
            if row.path is None:
                counts["pruned_skipped"] += 1
                continue
            try:
                payload = json.loads(_read_archived(row))
                handler(session, row.entity, payload, counts)
                session.commit()
                counts["replayed"] += 1
            except Exception as e:      # noqa: BLE001 — one bad payload doesn't sink the replay
                session.rollback()
                counts["failed"] += 1
                if verbose:
                    print(f"  replay FAILED [{source}/{row.entity} #{row.id}]: {e}")
        stats.update(counts)
        if verbose:
            print(f"  replay {source}: {counts['replayed']}/{counts['payloads']} payloads, "
                  f"{counts['added']} rows landed, {counts['pruned_skipped']} pruned-skipped")
    return stats


def _parse_since(since: str | None) -> datetime | None:
    if not since:
        return None
    s = since.strip()
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # A bare date (YYYY-MM-DD).
        dt = datetime.strptime(s, "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
