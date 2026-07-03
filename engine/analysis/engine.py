"""
Incremental persona analysis on the DB.

A (stock, persona) needs (re)analysis when there is no analysis for that pair
whose data_hash matches the stock's latest full snapshot — i.e. the stock is new,
the persona is missing, or the underlying data changed. Prompt-version bumps do
NOT force re-analysis (avoids re-running the whole backfill on a prompt tweak);
use force=True for that.
"""

import time

from sqlalchemy import select

from db.models import Stock
from engine.analysis.backend import default_model, get_backend
from engine.analysis.prompt import build_user_prompt
from engine.repo import (
    existing_persona_hashes,
    job_run,
    latest_snapshot,
    recompute_scores_for_stock,
    save_analysis,
    universe_contains,
)
from src.personas import PERSONAS, get_persona_slugs

PROMPT_VERSION = "v2"  # v1 = imported (pre-fix) prompts; v2 = corrected prompts


def find_work(session, personas, universe=None, force=False, limit=0):
    """Yield (stock, snapshot, persona_slug) tuples that need analysis."""
    q = select(Stock).order_by(Stock.symbol)
    if universe:
        q = q.where(universe_contains(universe))
    stocks = session.scalars(q).all()
    work = []
    for stock in stocks:
        snap = latest_snapshot(session, stock.id, quality="full")
        if snap is None:
            continue  # only analyze stocks with full financial data
        done = existing_persona_hashes(session, stock.id)
        for slug in personas:
            has_current = (not force) and (snap.content_hash in done.get(slug, set()))
            if not has_current:
                work.append((stock, snap, slug))
                if limit and len(work) >= limit:
                    return work
    return work


def run_incremental(personas=None, universe=None, model=None, force=False,
                    limit=0, delay=2.0, verbose=True):
    """Analyze all stock×persona pairs that need it. Returns a stats dict."""
    personas = personas or get_persona_slugs()
    model = model or default_model()
    backend = get_backend()

    with job_run("analyze", target=universe or "all") as (session, stats):
        work = find_work(session, personas, universe=universe, force=force, limit=limit)
        stats.update({"backend": backend.name, "model": model, "planned": len(work),
                      "success": 0, "error": 0})
        if verbose:
            print(f"[analyze] backend={backend.name} model={model} "
                  f"planned={len(work)} pairs (universe={universe or 'all'})")

        touched_stock_ids = set()
        for i, (stock, snap, slug) in enumerate(work):
            persona = PERSONAS[slug]
            screener_snap = latest_snapshot(session, stock.id, source="screener")
            user_prompt = build_user_prompt(stock, snap, screener_snap)
            result, meta = backend.analyze(persona["system_prompt"], user_prompt, model)

            if result is None:
                stats["error"] += 1
                if verbose:
                    print(f"  [{i+1}/{len(work)}] {stock.symbol} x {slug}: ERROR {meta}")
            else:
                model_used = (meta or {}).get("model_used", model)
                save_analysis(session, stock, snap, slug, model_used, PROMPT_VERSION, result, meta)
                touched_stock_ids.add(stock.id)
                stats["success"] += 1
                if verbose:
                    print(f"  [{i+1}/{len(work)}] {stock.symbol} x {slug}: "
                          f"score={result.get('score')} rec={result.get('recommendation')} "
                          f"model={model_used}")
                session.commit()

            if i < len(work) - 1:
                time.sleep(delay)

        # Recompute composite scores for every stock we touched.
        for sid in touched_stock_ids:
            recompute_scores_for_stock(session, session.get(Stock, sid))
        stats["stocks_rescored"] = len(touched_stock_ids)
        session.commit()

    return stats
