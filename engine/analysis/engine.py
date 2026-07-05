"""
Incremental persona analysis on the DB.

A (stock, persona) needs (re)analysis when there is no analysis for that pair
whose data_hash matches the stock's latest full snapshot — i.e. the stock is new,
the persona is missing, or the underlying data changed. Prompt-version bumps do
NOT force re-analysis (avoids re-running the whole backfill on a prompt tweak);
use force=True for that.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from db.models import Stock
from engine.analysis.backend import default_model, get_backend
from engine.analysis.prompt import build_user_prompt
from engine.repo import (
    clear_analysis_failure,
    dead_letter_pairs,
    existing_persona_hashes,
    job_run,
    latest_snapshot,
    record_analysis_failure,
    recompute_scores_for_stock,
    save_analysis,
    universe_contains,
)
from src.personas import PERSONAS, get_persona_slugs

log = logging.getLogger(__name__)

PROMPT_VERSION = "v4"  # v1 = imported; v2 = corrected scales; v3 = research-grounded revamp (said-vs-did, verified thresholds, screener-aware); v4 = recent-IPO recency context (gaps ≠ red flags)

# A (stock, persona) that failed this many times on the SAME data_hash is
# dead-lettered: find_work stops planning it (it was silently re-burning the
# daily cap). New data (new hash) re-qualifies the pair; force=True overrides.
DEAD_LETTER_THRESHOLD = 3


def find_work(session, personas, universe=None, symbols=None, force=False, limit=0):
    """Yield (stock, snapshot, persona_slug) tuples that need analysis."""
    q = select(Stock).order_by(Stock.symbol)
    if symbols:
        q = q.where(Stock.symbol.in_([s.upper() for s in symbols]))
    if universe:
        q = q.where(universe_contains(universe))
    stocks = session.scalars(q).all()
    work = []
    for stock in stocks:
        snap = latest_snapshot(session, stock.id, quality="full")
        if snap is None:
            continue  # only analyze stocks with full financial data
        done = existing_persona_hashes(session, stock.id)
        dead = set() if force else dead_letter_pairs(session, stock.id, DEAD_LETTER_THRESHOLD)
        for slug in personas:
            has_current = (not force) and (snap.content_hash in done.get(slug, set()))
            if has_current or (slug, snap.content_hash) in dead:
                continue
            work.append((stock, snap, slug))
            if limit and len(work) >= limit:
                return work
    return work


def run_incremental(personas=None, universe=None, symbols=None, model=None, force=False,
                    limit=0, delay=2.0, verbose=True, workers=1):
    """Analyze all stock×persona pairs that need it. Returns a stats dict.

    workers > 1 fans each stock's personas across a thread pool: the backend
    calls (the slow part, ~60-100s of CLI each) run concurrently while ALL DB
    work stays on the main thread — per-pair isolation, dead-letter bookkeeping
    and per-pair commits are identical to the serial path. `delay` separates
    stock batches when parallel (separating pairs inside a burst is pointless).
    """
    personas = personas or get_persona_slugs()
    model = model or default_model()
    backend = get_backend()
    workers = max(1, workers)

    target = universe or (symbols and f"{len(symbols)} symbols") or "all"
    with job_run("analyze", target=target) as (session, stats):
        work = find_work(session, personas, universe=universe, symbols=symbols,
                         force=force, limit=limit)
        stats.update({"backend": backend.name, "model": model, "planned": len(work),
                      "workers": workers, "success": 0, "error": 0})
        if verbose:
            print(f"[analyze] backend={backend.name} model={model} workers={workers} "
                  f"planned={len(work)} pairs (universe={universe or 'all'})")

        # find_work emits stock-major order; group consecutive pairs per stock.
        groups: list[tuple] = []
        for stock, snap, slug in work:
            if groups and groups[-1][0].id == stock.id:
                groups[-1][2].append(slug)
            else:
                groups.append((stock, snap, [slug]))

        touched_stock_ids = set()
        i = -1
        for gi, (stock, snap, slugs) in enumerate(groups):
            screener_snap = latest_snapshot(session, stock.id, source="screener")
            user_prompt = build_user_prompt(stock, snap, screener_snap)

            # Fire this stock's personas concurrently; collect (kind, payload)
            # so backend exceptions surface per-pair, never abort the batch.
            def _invoke(slug):
                return backend.analyze(PERSONAS[slug]["system_prompt"], user_prompt, model)

            outcomes: dict[str, tuple] = {}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {slug: pool.submit(_invoke, slug) for slug in slugs}
            for slug in slugs:
                try:
                    outcomes[slug] = ("ok", futures[slug].result())
                except Exception as e:  # noqa: BLE001 — isolate to this pair
                    outcomes[slug] = ("exc", e)

            for slug in slugs:
                i += 1
                # Per-pair isolation: one bad result (backend error, off-contract
                # output, any exception) increments `error` and is skipped — it
                # never aborts the batch or leaves earlier analyses un-scored.
                try:
                    kind, payload = outcomes[slug]
                    if kind == "exc":
                        raise payload
                    result, meta = payload

                    if result is None:
                        stats["error"] += 1
                        # Dead-letter bookkeeping: repeated failures on this exact data
                        # eventually stop being planned (find_work skips at threshold).
                        record_analysis_failure(session, stock.id, slug, snap.content_hash,
                                                str((meta or {}).get("error") or meta))
                        session.commit()
                        if verbose:
                            print(f"  [{i+1}/{len(work)}] {stock.symbol} x {slug}: ERROR {meta}")
                    else:
                        model_used = (meta or {}).get("model_used", model)
                        save_analysis(session, stock, snap, slug, model_used, PROMPT_VERSION, result, meta)
                        clear_analysis_failure(session, stock.id, slug, snap.content_hash)
                        # Rescore this stock immediately so a committed analysis always has a
                        # matching composite (no orphan window on interruption).
                        recompute_scores_for_stock(session, stock)
                        touched_stock_ids.add(stock.id)
                        stats["success"] += 1
                        if verbose:
                            print(f"  [{i+1}/{len(work)}] {stock.symbol} x {slug}: "
                                  f"score={result.get('score')} rec={result.get('recommendation')} "
                                  f"model={model_used}")
                        session.commit()
                except Exception as e:
                    session.rollback()
                    stats["error"] += 1
                    # Off-contract output / any exception is a failure of THIS pair on
                    # THIS data — record it (fresh transaction after the rollback).
                    try:
                        record_analysis_failure(session, stock.id, slug, snap.content_hash,
                                                f"{type(e).__name__}: {e}")
                        session.commit()
                    except Exception:
                        session.rollback()
                        log.warning("could not record analysis failure for %s x %s",
                                    stock.symbol, slug, exc_info=True)
                    if verbose:
                        print(f"  [{i+1}/{len(work)}] {stock.symbol} x {slug}: SKIPPED ({type(e).__name__}: {e})")

            if gi < len(groups) - 1 and delay:
                time.sleep(delay)

        stats["stocks_rescored"] = len(touched_stock_ids)
        session.commit()

    return stats
