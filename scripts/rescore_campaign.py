"""
Targeted re-analysis driver for model homogenisation.

Under a one-company-one-model policy the analysis model is a per-stock variable,
which is the worst case for cross-sectional ranking: `opus`-dominant stocks score
~10 points below `sonnet`-dominant ones (composite 24.1 vs 33.9) purely because of
the model, and cluster at the bottom of every ranking regardless of the business.

This script re-analyses the non-comparable tail FULLY under one target model — all
ten personas per stock, never a patch — so every composite is backed by a single
model at the current prompt version.

Target model and prompt version are fixed for the whole campaign:
  - model  : ANALYSIS_MODEL env (default claude-fable-5), via default_model()
  - prompt : engine.analysis.engine.PROMPT_VERSION

The target list is built from the DB, highest-impact first:
  tier 1  dominant-model      stocks whose modal scored-analysis model != target
  tier 2  mixed-prompt        stocks carrying more than one prompt_version
  tier 3  price-drift         stocks with any analysis quoted >5% off the real close

Re-analysis is driven through the existing engine path (run_incremental with
force=True): per-pair isolation, the transient/permanent failure classifier
(a quota outage is recorded but never dead-lettered) and per-stock rescoring all
come for free — this script only selects, sequences, and reports.

Idempotent and resumable: a stock whose ten personas already carry the target
model at the current prompt version is skipped, so a re-invocation continues where
the last daily slice left off.

    python -m scripts.rescore_campaign                # dry run: list + counts only
    python -m scripts.rescore_campaign --limit 30     # dry run, capped preview
    python -m scripts.rescore_campaign --execute --limit 30 --workers 10
"""

import argparse
import time

from sqlalchemy import Date, cast, func, select, true

from db.models import Analysis, DailyPrice, Stock, StockSnapshot
from engine.analysis.backend import default_model
from engine.analysis.engine import PROMPT_VERSION, run_incremental
from engine.repo import job_run
from src.personas import get_persona_slugs

DRIFT_THRESHOLD = 0.05  # >5% gap between the quoted price and the real close


# ---------- target selection (pure DB queries) ----------

def dominant_model_targets(session, target_model: str) -> list[str]:
    """Symbols whose modal scored-analysis model is not the target model.

    The mode over a stock's scored analyses is its effective model; when it is
    anything other than the target, the stock's composite is not comparable to a
    target-model stock's and it belongs in the campaign. Once re-analysed, its
    mode flips to the target and it drops out on the next run."""
    dm = (
        select(
            Analysis.stock_id.label("stock_id"),
            func.mode().within_group(Analysis.model).label("m"),
        )
        .where(Analysis.score.isnot(None), Analysis.model.isnot(None))
        .group_by(Analysis.stock_id)
        .subquery()
    )
    q = (
        select(Stock.symbol)
        .join(dm, dm.c.stock_id == Stock.id)
        .where(dm.c.m != target_model)
        .order_by(Stock.symbol)
    )
    return list(session.scalars(q))


def mixed_prompt_targets(session) -> list[str]:
    """Symbols carrying more than one prompt_version across their analyses — a
    composite blended over incompatible prompts."""
    q = (
        select(Stock.symbol)
        .join(Analysis, Analysis.stock_id == Stock.id)
        .group_by(Stock.id, Stock.symbol)
        .having(func.count(func.distinct(Analysis.prompt_version)) > 1)
        .order_by(Stock.symbol)
    )
    return list(session.scalars(q))


def drift_targets(session, threshold: float = DRIFT_THRESHOLD) -> list[str]:
    """Symbols with any analysis whose quoted price drifts more than `threshold`
    from the real close as of the analysis date.

    The content hash excludes quote fields, so an unchanged fundamentals snapshot
    keeps its original price; personas then valued the company at a price it no
    longer traded at. The as-of close is the latest daily bar on or before the
    analysis date (LATERAL, matching Task 8's re-measure query)."""
    seen = func.coalesce(
        StockSnapshot.current_price,
        StockSnapshot.info["currentPrice"].as_float(),
    )
    as_of = cast(Analysis.analyzed_at, Date)
    dp = (
        select(DailyPrice.close.label("close"))
        .where(DailyPrice.stock_id == Analysis.stock_id, DailyPrice.date <= as_of)
        .order_by(DailyPrice.date.desc())
        .limit(1)
        .lateral("dp")
    )
    q = (
        select(Stock.symbol)
        .select_from(Analysis)
        .join(StockSnapshot, StockSnapshot.id == Analysis.snapshot_id)
        .join(Stock, Stock.id == Analysis.stock_id)
        .join(dp, true())
        .where(seen.isnot(None))
        .where(func.abs(seen - dp.c.close) / func.nullif(dp.c.close, 0) > threshold)
        .distinct()
        .order_by(Stock.symbol)
    )
    return list(session.scalars(q))


def fully_current_symbols(session, target_model: str, prompt_version: str) -> set[str]:
    """Symbols whose full persona roster already carries the target model at the
    current prompt version — nothing left to re-run, skip them."""
    roster = set(get_persona_slugs())
    rows = session.execute(
        select(Stock.symbol, Analysis.persona)
        .join(Analysis, Analysis.stock_id == Stock.id)
        .where(Analysis.model == target_model, Analysis.prompt_version == prompt_version)
        .distinct()
    ).all()
    done: dict[str, set] = {}
    for symbol, persona in rows:
        done.setdefault(symbol, set()).add(persona)
    return {symbol for symbol, personas in done.items() if roster <= personas}


def select_targets(session, target_model: str, prompt_version: str) -> dict:
    """Build the ordered, deduplicated campaign target list.

    Each symbol appears once, at its highest-priority tier. Symbols already fully
    current under the target model are removed and reported separately so a
    re-invocation is transparent about what it skipped."""
    tiers = {
        1: dominant_model_targets(session, target_model),
        2: mixed_prompt_targets(session),
        3: drift_targets(session),
    }
    tier_of: dict[str, int] = {}
    for tier in (1, 2, 3):
        for symbol in tiers[tier]:
            tier_of.setdefault(symbol, tier)

    current = fully_current_symbols(session, target_model, prompt_version)

    candidates = sorted(tier_of, key=lambda s: (tier_of[s], s))
    actionable = [s for s in candidates if s not in current]
    skipped = [s for s in candidates if s in current]
    return {
        "tier_of": tier_of,
        "tier_candidates": {t: tiers[t] for t in (1, 2, 3)},
        "actionable": actionable,
        "skipped_current": skipped,
    }


# ---------- reporting + execution ----------

def _print_plan(plan: dict, target_model: str, prompt_version: str, limit: int) -> None:
    tier_names = {1: "dominant-model", 2: "mixed-prompt", 3: "price-drift"}
    actionable = plan["actionable"]
    batch = actionable[:limit] if limit else actionable

    print(f"[rescore_campaign] target model={target_model} prompt={prompt_version}")
    for tier in (1, 2, 3):
        cand = plan["tier_candidates"][tier]
        owned = [s for s in plan["actionable"] if plan["tier_of"][s] == tier]
        print(f"  tier {tier} {tier_names[tier]:<15} candidates={len(cand):>3} "
              f"actionable(unique)={len(owned):>3}")
    print(f"  already current (skipped) : {len(plan['skipped_current'])}")
    print(f"  actionable total          : {len(actionable)}")
    print(f"  this invocation (limit={limit or '—'}) : {len(batch)} stocks "
          f"≈ {len(batch) * len(get_persona_slugs())} CLI calls")
    if batch:
        print("  targets:")
        for symbol in batch:
            print(f"    {symbol:<16} tier {plan['tier_of'][symbol]}")


def run_campaign(target_model: str | None = None, limit: int = 0, execute: bool = False,
                 workers: int = 10, delay: float = 2.0, verbose: bool = True) -> dict:
    """Select targets and, when `execute`, re-analyse them one stock at a time.

    Dry run (execute=False) touches nothing — no JobRun, no analyses — it only
    prints the plan. Execution wraps the whole slice in a `rescore_campaign`
    JobRun so it lands in ops history, and pauses (never dead-letters) the moment
    a stock comes back all-error, the signature of a quota outage."""
    target_model = target_model or default_model()
    prompt_version = PROMPT_VERSION

    from db.base import SessionLocal
    read = SessionLocal()
    try:
        plan = select_targets(read, target_model, prompt_version)
    finally:
        read.close()

    if verbose:
        _print_plan(plan, target_model, prompt_version, limit)

    batch = plan["actionable"][:limit] if limit else plan["actionable"]
    if not execute:
        if verbose:
            print("[rescore_campaign] dry run — nothing executed. Pass --execute to run.")
        return {"planned": len(batch), "executed": 0, "dry_run": True}

    with job_run("rescore_campaign", target=f"{target_model}:{len(batch)}") as (_session, stats):
        stats.update({"model": target_model, "prompt_version": prompt_version,
                      "planned": len(batch), "success": 0, "error": 0,
                      "skipped": 0, "calls": 0, "paused": False})
        for idx, symbol in enumerate(batch, 1):
            if verbose:
                print(f"[rescore_campaign] ({idx}/{len(batch)}) {symbol} "
                      f"tier {plan['tier_of'][symbol]} …")
            ri = run_incremental(symbols=[symbol], model=target_model, force=True,
                                 workers=workers, delay=0, verbose=verbose)
            stats["calls"] += ri.get("planned", 0)
            ok, bad = ri.get("success", 0), ri.get("error", 0)
            if ok == 0 and bad == 0:
                stats["skipped"] += 1  # no full snapshot — nothing to analyse
            elif ok == 0 and bad > 0:
                # All pairs failed: a per-stock defect can't do that to ten
                # personas at once — this is a global outage. Stop the campaign
                # rather than march the rest of the slice into the same wall.
                stats["error"] += 1
                stats["paused"] = True
                if verbose:
                    print(f"[rescore_campaign] PAUSED at {symbol}: {bad} failures, "
                          "0 successes — likely a quota outage. Resume later.")
                break
            elif bad > 0:
                stats["error"] += 1  # partial — still non-homogeneous, retry next pass
            else:
                stats["success"] += 1
            if delay and idx < len(batch):
                time.sleep(delay)
        return {"planned": len(batch), "dry_run": False, **stats}


def main():
    p = argparse.ArgumentParser(description="Re-analyse the non-comparable tail")
    p.add_argument("--execute", action="store_true",
                   help="run the re-analysis (default: dry run, prints the plan only)")
    p.add_argument("--limit", type=int, default=0,
                   help="cap stocks this invocation, for daily quota-sized slices")
    p.add_argument("--workers", type=int, default=10,
                   help="concurrent persona calls per stock (10 = full council)")
    p.add_argument("--delay", type=float, default=2.0,
                   help="seconds between stocks")
    p.add_argument("--model", default=None,
                   help="target model (default: ANALYSIS_MODEL env / claude-fable-5)")
    a = p.parse_args()
    result = run_campaign(target_model=a.model, limit=a.limit, execute=a.execute,
                          workers=a.workers, delay=a.delay)
    print(result)


if __name__ == "__main__":
    main()
