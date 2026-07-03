"""
One-time migration: load the existing JSON dataset into Postgres.

Nothing is re-fetched or re-analyzed — this reads what's already on disk:
  - data/ipo_list.json            -> Stock metadata (universe tag ipo_2025)
  - data/stocks/{SYMBOL}.json     -> Stock + one StockSnapshot (content-hashed)
  - data/analyses/{SYM}/{p}.json  -> Analysis rows
  - composite scores are recomputed from the imported analyses (DB = source of truth)

Idempotent: re-running skips snapshots/analyses that already exist.
"""

import glob
import hashlib
import json
import os
from datetime import datetime, timezone

from sqlalchemy import select

from db.base import Base, engine, SessionLocal
from db.models import Stock, StockSnapshot, Analysis, CompositeScore, JobRun, utcnow

IPO_LIST = "data/ipo_list.json"
STOCKS_GLOB = "data/stocks/*.json"
ANALYSES_GLOB = "data/analyses/*/*.json"


# ---------- helpers ----------

def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def content_hash(stock_json: dict) -> str:
    """Stable hash of the meaningful scraped payload."""
    payload = {
        k: stock_json.get(k)
        for k in ("info", "financials", "balance_sheet", "cashflow", "history_summary")
    }
    if stock_json.get("fetch_status") == "error":
        payload = {"error": stock_json.get("error"), "status": "error"}
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _num(v):
    """Coerce to float or None (yfinance fields are sometimes strings/None)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def extract_columns(info: dict) -> dict:
    info = info or {}
    price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
    pe = info.get("trailingPE")
    if pe is None:
        eps = _num(info.get("epsTrailingTwelveMonths"))
        p = _num(price)
        if eps and eps > 0 and p:
            pe = round(p / eps, 2)
    return {
        "current_price": _num(price),
        "market_cap": _num(info.get("marketCap")),
        "pe_ratio": _num(pe),
        "roe": _num(info.get("returnOnEquity")),
        "debt_to_equity": _num(info.get("debtToEquity")),
        "revenue_growth": _num(info.get("revenueGrowth")),
    }


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# ---------- migration steps ----------

def build_ipo_metadata():
    data = load_json(IPO_LIST) or {}
    meta = {}
    for s in data.get("stocks", []):
        meta[s["symbol"]] = s
    return meta


def get_or_create_stock(session, symbol, cache):
    if symbol in cache:
        return cache[symbol]
    stock = session.scalar(select(Stock).where(Stock.symbol == symbol))
    if stock is None:
        stock = Stock(symbol=symbol, universe=[], status="active")
        session.add(stock)
        session.flush()
    cache[symbol] = stock
    return stock


def enrich_stock(stock, ipo_meta, stock_json):
    """Fill identity/metadata fields from ipo_list + the stock file's info block."""
    info = (stock_json or {}).get("info", {}) if stock_json else {}
    ipo = (stock_json or {}).get("ipo_data", {}) if stock_json else {}

    if ipo_meta:
        stock.company_name = stock.company_name or ipo_meta.get("company_name")
        stock.nse_symbol = stock.nse_symbol or ipo_meta.get("nse_symbol")
        stock.yf_symbol = stock.yf_symbol or ipo_meta.get("yf_symbol")
        stock.listing_date = stock.listing_date or ipo_meta.get("listing_date")
        stock.issue_price = stock.issue_price or ipo_meta.get("issue_price")
        stock.ipo_mcap_cr = stock.ipo_mcap_cr or ipo_meta.get("ipo_mcap_cr")
        stock.screener_url = stock.screener_url or ipo_meta.get("screener_url")
        if "ipo_2025" not in stock.universe:
            stock.universe = stock.universe + ["ipo_2025"]

    stock.company_name = stock.company_name or info.get("longName") or ipo.get("company_name")
    stock.yf_symbol = stock.yf_symbol or (stock_json or {}).get("yf_symbol")
    stock.sector = stock.sector or info.get("sector")
    stock.industry = stock.industry or info.get("industry")
    if stock.yf_symbol:
        stock.exchange = stock.exchange or ("NSE" if stock.yf_symbol.endswith(".NS") else "BSE")

    # Merge any `categories` tags the fetcher recorded.
    cats = (stock_json or {}).get("categories")
    if isinstance(cats, list):
        for c in cats:
            tag = str(c)
            if tag not in stock.universe:
                stock.universe = stock.universe + [tag]


def migrate_stocks(session, ipo_meta):
    files = sorted(glob.glob(STOCKS_GLOB))
    cache = {}
    created_stocks = snap_new = snap_skip = 0

    for i, path in enumerate(files):
        sj = load_json(path)
        if not sj:
            continue
        symbol = sj.get("symbol") or os.path.splitext(os.path.basename(path))[0]

        before = len(cache)
        stock = get_or_create_stock(session, symbol, cache)
        if len(cache) > before:
            created_stocks += 1
        enrich_stock(stock, ipo_meta.get(symbol), sj)

        chash = content_hash(sj)
        exists = session.scalar(
            select(StockSnapshot.id).where(
                StockSnapshot.stock_id == stock.id,
                StockSnapshot.content_hash == chash,
            )
        )
        if exists:
            snap_skip += 1
        else:
            cols = extract_columns(sj.get("info", {}))
            snap = StockSnapshot(
                stock_id=stock.id,
                captured_at=parse_dt(sj.get("fetched_at")) or utcnow(),
                source="yfinance",
                fetch_status=sj.get("fetch_status"),
                data_quality=sj.get("data_quality"),
                content_hash=chash,
                info=sj.get("info"),
                financials=sj.get("financials"),
                balance_sheet=sj.get("balance_sheet"),
                cashflow=sj.get("cashflow"),
                history_summary=sj.get("history_summary"),
                ipo_data=sj.get("ipo_data"),
                **cols,
            )
            session.add(snap)
            snap_new += 1

        if (i + 1) % 200 == 0:
            session.commit()
            print(f"  stocks: {i + 1}/{len(files)}")

    session.commit()
    print(f"Stocks: {created_stocks} created, snapshots: {snap_new} new / {snap_skip} skipped")
    return cache


def migrate_analyses(session, stock_cache):
    files = glob.glob(ANALYSES_GLOB)
    new = skip = 0

    # Map symbol -> latest snapshot (id, hash) for staleness linkage.
    snap_by_stock = {}

    def latest_snap(stock_id):
        if stock_id not in snap_by_stock:
            row = session.execute(
                select(StockSnapshot.id, StockSnapshot.content_hash)
                .where(StockSnapshot.stock_id == stock_id)
                .order_by(StockSnapshot.captured_at.desc())
                .limit(1)
            ).first()
            snap_by_stock[stock_id] = (row[0], row[1]) if row else (None, None)
        return snap_by_stock[stock_id]

    for i, path in enumerate(files):
        aj = load_json(path)
        if not aj or "analysis" not in aj:
            continue
        symbol = aj.get("symbol") or os.path.basename(os.path.dirname(path))
        persona = aj.get("persona") or os.path.splitext(os.path.basename(path))[0]
        stock = get_or_create_stock(session, symbol, stock_cache)

        meta = aj.get("metadata") or {}
        model = meta.get("model_used")
        analyzed_at = parse_dt(aj.get("analyzed_at")) or utcnow()

        # Dedup: same stock+persona+model+timestamp already imported.
        dup = session.scalar(
            select(Analysis.id).where(
                Analysis.stock_id == stock.id,
                Analysis.persona == persona,
                Analysis.model == model,
                Analysis.analyzed_at == analyzed_at,
            )
        )
        if dup:
            skip += 1
            continue

        a = aj["analysis"]
        snap_id, snap_hash = latest_snap(stock.id)
        row = Analysis(
            stock_id=stock.id,
            persona=persona,
            model=model,
            prompt_version="v1",   # imported = pre-fix prompts
            snapshot_id=snap_id,
            data_hash=snap_hash,
            score=a.get("score"),
            recommendation=a.get("recommendation"),
            investment_thesis=a.get("investment_thesis"),
            key_strengths=a.get("key_strengths"),
            key_risks=a.get("key_risks"),
            red_flags=a.get("red_flags"),
            detailed_analysis=a.get("detailed_analysis"),
            metrics_evaluated=a.get("metrics_evaluated"),
            analyzed_at=analyzed_at,
            duration_ms=meta.get("duration_ms"),
            total_cost_usd=meta.get("total_cost_usd"),
            usage=meta.get("usage"),
        )
        session.add(row)
        new += 1

        if (i + 1) % 500 == 0:
            session.commit()
            print(f"  analyses: {i + 1}/{len(files)}")

    session.commit()
    print(f"Analyses: {new} new / {skip} skipped")


def recompute_scores(session):
    from collections import Counter

    # Clear and recompute (idempotent).
    session.query(CompositeScore).delete()
    session.commit()

    stocks = session.scalars(select(Stock)).all()
    total_personas = 10
    made = 0
    for stock in stocks:
        analyses = session.scalars(
            select(Analysis).where(Analysis.stock_id == stock.id)
        ).all()
        # latest analysis per persona
        latest = {}
        for a in analyses:
            if a.score is None:
                continue
            cur = latest.get(a.persona)
            if cur is None or a.analyzed_at > cur.analyzed_at:
                latest[a.persona] = a
        if not latest:
            continue

        persona_scores = {p: a.score for p, a in latest.items()}
        composite = sum(persona_scores.values())
        recs = [a.recommendation for a in latest.values() if a.recommendation]
        if composite >= 60:
            consensus = "BUY"
        elif composite >= 40:
            consensus = "HOLD"
        else:
            consensus = "AVOID"

        session.add(CompositeScore(
            stock_id=stock.id,
            composite_score=round(composite, 1),
            persona_scores=persona_scores,
            consensus_recommendation=consensus,
            recommendation_counts=dict(Counter(recs)),
            analysis_coverage=len(persona_scores),
            total_personas=total_personas,
            scoring_method="sum_of_persona_scores_each_0_to_10",
        ))
        made += 1
    session.commit()
    print(f"Composite scores: {made} stocks")


def main():
    Base.metadata.create_all(engine)
    session = SessionLocal()
    job = JobRun(job_type="migrate_json_to_db", target="all", status="running")
    session.add(job)
    session.commit()

    try:
        ipo_meta = build_ipo_metadata()
        print(f"IPO metadata: {len(ipo_meta)} symbols")
        cache = migrate_stocks(session, ipo_meta)
        migrate_analyses(session, cache)
        recompute_scores(session)

        job.status = "success"
        job.finished_at = utcnow()
        job.stats = {
            "stocks": session.query(Stock).count(),
            "snapshots": session.query(StockSnapshot).count(),
            "analyses": session.query(Analysis).count(),
            "composite_scores": session.query(CompositeScore).count(),
        }
        session.commit()
        print("\n=== MIGRATION SUMMARY ===")
        for k, v in job.stats.items():
            print(f"  {k}: {v}")
    except Exception as e:
        session.rollback()
        job.status = "error"
        job.error = str(e)
        job.finished_at = utcnow()
        session.commit()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
