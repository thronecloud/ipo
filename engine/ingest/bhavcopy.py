"""
Bhavcopy ingestion — the official NSE & BSE end-of-day equity price files.

Each exchange publishes ONE downloadable file per trading day carrying every
listed equity's OHLCV. Two downloads replace the per-stock yfinance price
rotation (thousands of throttled `history()` calls) with two unthrottled fetches.

Both exchanges now serve the identical NSE/SEBI "UDiFF Common Bhavcopy" schema
(TradDt, ISIN, TckrSymb, OpnPric/HghPric/LwPric/ClsPric, TtlTradgVol, …); NSE
ships it zipped, BSE as a bare CSV. One parser handles both. Rows resolve to our
universe — NSE by ticker (nse_symbol/symbol), BSE by ISIN (falling back to
ticker, since our universe is NSE-keyed and BSE tickers rarely match) — and land
via the shared corrective `upsert_daily_prices` (per-date dedup, corporate-action
rebase, NULL-preserving coalesce, and the bars_rebased tally are all inherited).

A whole-exchange file names thousands of companies we do not track; an unmatched
row is a normal outcome, counted and skipped, never an error. A 404 before the
file is published (pre-~18:30 IST, weekends, exchange holidays) is "not
published": counted, not retried (404 is outside PoliteSession's retry set, so
one GET, no storm), and — when today's file is absent — the previous trading day
is fetched once if it wasn't already ingested.

`_fetch_nse_zip` / `_fetch_bse_csv` are the only network seams; tests feed
recorded bytes to the parsers and nothing here touches the network or the clock
in CI.
"""

import csv
import io
import zipfile
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from db.models import JobRun, Stock
from engine.ingest.archive import archive_safe
from engine.ingest.exchange_base import (
    BSE_BASE,
    bse_session,
    clean,
    nse_session,
    resolve_stock_id,
    to_float,
    to_int,
)
from engine.repo import job_run, upsert_daily_prices
from src.utils import log

# Current (post-2024 UDiFF) file locations. The date is the trading day, YYYYMMDD.
NSE_BHAVCOPY_URL = ("https://nsearchives.nseindia.com/content/cm/"
                    "BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip")
NSE_BHAVCOPY_REFERER = "https://www.nseindia.com/all-reports"
BSE_BHAVCOPY_URL = ("https://www.bseindia.com/download/BhavCopy/Equity/"
                    "BhavCopy_BSE_CM_0_0_0_{yyyymmdd}_F_0000.CSV")

# UDiFF financial-instrument type for a cash-market equity (the whole file is STK,
# but the filter future-proofs against the segment ever mixing in other types).
UDIFF_EQUITY_TYPE = "STK"

# IST is UTC+5:30; the exchanges date their files by the Indian trading day.
IST_OFFSET = timedelta(hours=5, minutes=30)


# ---------- parsing ----------

def _parse_trad_dt(v):
    s = clean(v)
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_udiff(csv_text: str) -> list[dict]:
    """A UDiFF Common Bhavcopy CSV (NSE or BSE) → bar dicts carrying the identity
    columns. Each item: {date, open, high, low, close, volume, symbol, isin, series}.
    Non-equity instrument rows are skipped; the OHLCV keys are exactly the shape
    `upsert_daily_prices` consumes."""
    out = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        if clean(row.get("FinInstrmTp")) != UDIFF_EQUITY_TYPE:
            continue
        d = _parse_trad_dt(row.get("TradDt"))
        if d is None:
            continue
        out.append({
            "date": d,
            "open": to_float(row.get("OpnPric")),
            "high": to_float(row.get("HghPric")),
            "low": to_float(row.get("LwPric")),
            "close": to_float(row.get("ClsPric")),
            "volume": to_int(row.get("TtlTradgVol")),
            "symbol": clean(row.get("TckrSymb")),
            "isin": clean(row.get("ISIN")),
            "series": clean(row.get("SctySrs")),
        })
    return out


def _unzip_single_csv(content: bytes) -> str:
    """Extract the single CSV member from an NSE bhavcopy ZIP."""
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        return z.read(z.namelist()[0]).decode("utf-8")


# ---------- universe resolution ----------

_BAR_KEYS = ("date", "open", "high", "low", "close", "volume")


def _resolve_by_isin(session, isin: str | None) -> int | None:
    """Map an ISIN to our internal stock id, or None. ISIN is the universal
    cross-source identity key, so it is the reliable bridge for BSE rows whose
    ticker is not our (NSE-keyed) symbol."""
    s = clean(isin)
    if s is None:
        return None
    return session.scalar(
        select(Stock.id).where(func.upper(Stock.isin) == s.upper()).limit(1)
    )


def _resolve_bar(session, exchange: str, bar: dict) -> int | None:
    """NSE rows resolve by ticker (nse_symbol/symbol); BSE rows by ISIN first,
    falling back to ticker. An unmatched row (a non-universe company) is a valid
    outcome, never an error."""
    if exchange == "BSE":
        return (_resolve_by_isin(session, bar.get("isin"))
                or resolve_stock_id(session, bar.get("symbol")))
    return resolve_stock_id(session, bar.get("symbol"))


def ingest_bars(session, exchange: str, bars: list[dict], counts: dict) -> set[int]:
    """Resolve each bar to our universe, then land the matched bars per stock via
    the corrective upsert. Returns the set of stock ids priced. Unmatched rows are
    tallied and skipped."""
    per_stock: dict[int, list[dict]] = {}
    unmatched = 0
    for bar in bars:
        stock_id = _resolve_bar(session, exchange, bar)
        if stock_id is None:
            unmatched += 1
            continue
        per_stock.setdefault(stock_id, []).append({k: bar[k] for k in _BAR_KEYS})

    added = 0
    for stock_id, rows in per_stock.items():
        added += upsert_daily_prices(session, stock_id, rows, counts)

    counts["unmatched"] = counts.get("unmatched", 0) + unmatched
    counts["bars_added"] = counts.get("bars_added", 0) + added
    return set(per_stock)


# ---------- trading-day selection ----------

def _ist_today() -> date:
    return (datetime.now(timezone.utc) + IST_OFFSET).date()


def _prev_trading_day(d: date) -> date:
    """The weekday before `d` (holidays are handled by the download simply 404ing —
    a holiday's file is never published)."""
    d -= timedelta(days=1)
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d -= timedelta(days=1)
    return d


def _trading_candidates(target: date, last_ingested: date | None) -> list[date]:
    """The dates to try this run, most-recent first. Today's file is always
    attempted (a weekend target rolls back to the Friday); the previous trading day
    is added as a fallback only when it hasn't already been ingested — so an absent
    today's file heals to yesterday's without ever re-fetching a day we already have."""
    d = target
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    cands = [d]
    prev = _prev_trading_day(d)
    if last_ingested is None or prev > last_ingested:
        cands.append(prev)
    return cands


def _last_ingested_date(session) -> date | None:
    """The trading day of the last successful bhavcopy run, from its stored stats."""
    row = session.scalar(
        select(JobRun.stats)
        .where(JobRun.job_type == "bhavcopy",
               JobRun.status.in_(("success", "partial")))
        .order_by(JobRun.started_at.desc())
        .limit(1)
    )
    if isinstance(row, dict) and row.get("date"):
        try:
            return date.fromisoformat(row["date"])
        except ValueError:
            return None
    return None


# ---------- network seams ----------

def _fetch_nse_zip(on_date: date) -> bytes | None:
    """NSE bhavcopy ZIP bytes for a date, or None if not published yet (404).
    Other statuses raise. The archives host shares nseindia.com's cookie, so the
    homepage warm-up in `nse_session` suffices."""
    url = NSE_BHAVCOPY_URL.format(yyyymmdd=on_date.strftime("%Y%m%d"))
    resp = nse_session().get(url, referer=NSE_BHAVCOPY_REFERER)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content


def _fetch_bse_csv(on_date: date) -> str | None:
    """BSE bhavcopy CSV text for a date, or None if not published yet (404)."""
    url = BSE_BHAVCOPY_URL.format(yyyymmdd=on_date.strftime("%Y%m%d"))
    resp = bse_session().get(url, referer=f"{BSE_BASE}/", headers={"Origin": BSE_BASE})
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content.decode("utf-8")


def _load_csv(exchange: str, on_date: date) -> str | None:
    """Download and decompress one exchange's bhavcopy for a date to CSV text, or
    None when the file is not published for that date."""
    if exchange == "NSE":
        content = _fetch_nse_zip(on_date)
        return _unzip_single_csv(content) if content is not None else None
    if exchange == "BSE":
        return _fetch_bse_csv(on_date)
    raise ValueError(f"unknown exchange {exchange!r}")


def _fetch_one_exchange(session, exchange: str, candidates: list[date],
                        counts: dict) -> tuple[set[int], date | None]:
    """Try each candidate trading day in order; ingest the first that is published
    and stop. Returns (priced stock ids, the ingested date) or (empty, None) when
    no candidate's file exists yet."""
    for d in candidates:
        text = _load_csv(exchange, d)
        if text is None:
            continue
        # Archive the raw EOD file BEFORE parse_udiff, so a parser bug over a
        # whole-exchange file is replayable rather than a lost trading day.
        archive_safe("bhavcopy", f"{exchange}-{d.isoformat()}", text, counts,
                     content_type="text/csv")
        bars = parse_udiff(text)
        counts["processed"] = counts.get("processed", 0) + len(bars)
        return ingest_bars(session, exchange, bars, counts), d
    return set(), None


# ---------- completeness ----------

def _active_without_bar(session, priced: set[int]) -> int:
    """Count active/new universe stocks that received no bar in today's files —
    the completeness signal (delisting / suspension / symbol change / BSE-only SME
    absent from the NSE feed)."""
    active = set(session.scalars(
        select(Stock.id).where(Stock.status.in_(("active", "new")))
    ).all())
    return len(active - priced)


# ---------- entrypoint ----------

def fetch_bhavcopy(on_date: date | None = None, exchanges=("NSE", "BSE"),
                   verbose=True) -> dict:
    """Ingest the NSE + BSE end-of-day bhavcopy for the latest trading day.

    Idempotent (re-running re-sends the same bars; the corrective upsert inserts 0
    new). One GET per candidate day per exchange — a not-yet-published file (404)
    is counted and, if it is today's, the previous trading day is tried once.
    """
    with job_run("bhavcopy", target=",".join(exchanges)) as (session, stats):
        counts = {"processed": 0, "bars_added": 0, "bars_rebased": 0, "unmatched": 0,
                  "matched_stocks": 0, "not_published": 0, "soft_fail": 0,
                  "universe_missing": 0, "archive_failed": 0}
        target = on_date or _ist_today()
        candidates = _trading_candidates(target, _last_ingested_date(session))
        priced: set[int] = set()
        ingested_date: date | None = None

        for exchange in exchanges:
            try:
                matched, used = _fetch_one_exchange(session, exchange, candidates, counts)
                session.commit()
                if used is None:
                    counts["not_published"] += 1
                    if verbose:
                        print(f"  {exchange}: bhavcopy not published for "
                              f"{', '.join(str(c) for c in candidates)}")
                else:
                    priced |= matched
                    ingested_date = used
                    if verbose:
                        print(f"  {exchange}: {used} — {len(matched)} universe "
                              f"stock(s) priced")
            except Exception as e:      # noqa: BLE001 — one exchange down must not sink the other
                session.rollback()
                counts["soft_fail"] += 1
                if verbose:
                    print(f"  {exchange} bhavcopy FAILED: {e}")

        counts["matched_stocks"] = len(priced)
        if ingested_date is not None:
            counts["date"] = ingested_date.isoformat()
            missing = _active_without_bar(session, priced)
            counts["universe_missing"] = missing
            if missing:
                log(f"bhavcopy: {missing} active universe stock(s) had no bar in "
                    f"{ingested_date}'s files (delisted / suspended / symbol change?)")
        stats.update(counts)
    return stats
