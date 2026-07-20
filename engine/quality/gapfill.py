"""
The gap-fill closer: read the latest audit's `missing` lists and dispatch to the
existing ingest entrypoints to fill each hole. Writes no source logic of its own.

- identity : copy sector/industry from the stock's already-stored yfinance info into
             Stock columns (DB-only, no network) — the biggest, cheapest win.
- prices   : backfill_prices(symbols=...)   (OHLCV backfill, creates no snapshots)
- screener : enrich(symbols=...)            (sequential — screener rate-limits)
- yfinance : refresh(symbols=...)           (re-fetch minimal/missing snapshots)

Run `dq_audit` first so `data_quality_reports` reflects reality; gapfill targets
exactly what that audit flagged.
"""

import glob
import os
import re

from sqlalchemy import select

from db.models import DataQualityReport, Stock
from engine.ingest.screener_enrich import enrich
from engine.ingest.yf_refresh import backfill_prices, refresh
from engine.quality.audit import audit as dq_audit
from engine.repo import job_run, latest_snapshot, universe_contains

ALL_TARGETS = ("identity", "prices", "screener", "yfinance")

AMFI_SOURCES_DIR = "data/sources"


def _amfi_identity_map() -> dict:
    """symbol -> {isin, cap_category} from the newest downloaded AMFI xlsx.

    Offline (no network): AMFI is the authoritative SEBI classification and its
    file is already on disk from the amfi ingest job. Both NSE and BSE symbols
    are keyed. Empty dict when no file has been downloaded yet.
    """
    from engine.ingest.amfi import parse_amfi
    files = sorted(glob.glob(os.path.join(AMFI_SOURCES_DIR, "*.xlsx")),
                   key=os.path.getmtime)
    if not files:
        return {}
    out: dict = {}
    for row in parse_amfi(files[-1]):
        for sym in (row["nse_symbol"], row["bse_symbol"]):
            if sym:
                out[str(sym).upper()] = {
                    "isin": row["isin"], "cap_category": row["category"],
                    "company": row["company"],
                }
    return out


# Exchange and provider company names differ in suffixes and punctuation. Compare
# the first two significant tokens so a genuine match survives, while a
# cross-namespace symbol collision (a different company entirely) is caught.
_NAME_STOPWORDS = {"ltd", "limited", "the", "india", "industries", "company",
                   "co", "pvt", "private"}


def _name_tokens(s: str) -> list:
    return [t for t in re.sub(r"[^a-z0-9 ]", " ", s.lower()).split()
            if t not in _NAME_STOPWORDS][:2]


def _names_agree(a, b) -> bool:
    if not a or not b:
        return True  # cannot disprove on missing data; do not block the fill
    ta, tb = _name_tokens(a), _name_tokens(b)
    return bool(ta) and bool(tb) and ta == tb


def _latest_reports(session, symbols, universe):
    sq = (
        select(DataQualityReport.stock_id, DataQualityReport.missing)
        .order_by(DataQualityReport.stock_id, DataQualityReport.checked_at.desc(), DataQualityReport.id.desc())
        .distinct(DataQualityReport.stock_id)
        .subquery()
    )
    q = select(Stock, sq.c.missing).join(sq, sq.c.stock_id == Stock.id)
    if symbols:
        q = q.where(Stock.symbol.in_([s.upper() for s in symbols]))
    if universe:
        q = q.where(universe_contains(universe))
    return session.execute(q).all()


def _fill_identity(session, stocks, stats=None) -> int:
    """Populate Stock identity fields from already-stored/offline sources:
    sector/industry from the stock's yfinance info, falling back to the
    screener peers classification (the only source covering fresh listings);
    isin/cap_category from the newest AMFI xlsx. Fills only NULL fields —
    never overwrites. Each write stamps its `_source` so an imputed value stays
    distinguishable from a measured one."""
    amfi = (_amfi_identity_map()
            if any(not st.isin or not st.cap_category for st in stocks) else {})
    filled = 0
    for st in stocks:
        yf = latest_snapshot(session, st.id, source="yfinance")
        info = (yf.info if yf else None) or {}
        cls = []
        if not st.sector or not st.industry:
            scr = latest_snapshot(session, st.id, source="screener")
            cls = ((scr.screener if scr else None) or {}).get("classification") or []
        # yf naming wins when present; screener chain is broadest -> most specific.
        yf_sector, yf_industry = info.get("sector"), info.get("industry")
        sector = yf_sector or (cls[0] if cls else None)
        industry = yf_industry or (cls[-1] if len(cls) > 1 else None)
        changed = False
        if not st.sector and sector:
            st.sector = sector
            st.sector_source = "yfinance" if yf_sector else "screener"
            changed = True
        if not st.industry and industry:
            st.industry = industry
            st.industry_source = "yfinance" if yf_industry else "screener"
            changed = True
        ref = (amfi.get((st.symbol or "").upper())
               or amfi.get((st.nse_symbol or "").upper()))
        if ref:
            if not st.isin and ref.get("isin"):
                # The AMFI map keys NSE and BSE symbols into one flat dict with
                # last-wins overwrite, so a cross-namespace collision would assign
                # another company's ISIN — on the universal cross-source identity
                # key. Trust it only when the AMFI company name agrees.
                if _names_agree(ref.get("company"), st.company_name):
                    st.isin = ref["isin"]
                    st.isin_source = "amfi"
                    changed = True
                elif stats is not None:
                    stats["amfi_name_mismatch"] = stats.get("amfi_name_mismatch", 0) + 1
            if not st.cap_category and ref.get("cap_category"):
                st.cap_category = ref["cap_category"]
                changed = True
        if changed:
            filled += 1
    session.commit()
    return filled


def gapfill(targets=ALL_TARGETS, symbols=None, universe=None, limit=0, delay=None, verbose=True) -> dict:
    """delay: seconds between network requests. None uses each ingest job's default;
    set high (e.g. 6-8s) to stay under screener.in rate limits."""
    targets = tuple(targets)
    net_kw = {"delay": delay} if delay is not None else {}
    with job_run("dq_fill", target=",".join(targets)) as (session, stats):
        reports = _latest_reports(session, symbols, universe)
        need_identity, need_prices, need_screener, need_yf = [], [], [], []
        for st, missing in reports:
            missing = missing or []
            if any(m.startswith("identity") for m in missing):
                need_identity.append(st)
            # "stale_prices" -> the series exists but is behind; same fill path.
            if "prices" in missing or "stale_prices" in missing:
                need_prices.append(st.symbol)
            # "latest_quarter"/"quarters" -> re-pull both quarterly sources
            # (newest quarter missing, or history short of the 8q target).
            if "screener" in missing or "latest_quarter" in missing or "quarters" in missing:
                need_screener.append(st.symbol)
            if ("yfinance" in missing or "yfinance_refresh" in missing
                    or "latest_quarter" in missing or "quarters" in missing):
                need_yf.append(st.symbol)

        def cap(xs):
            return xs[:limit] if limit else xs

        result = {}
        touched: set = set()
        # 1) identity first — cheap, no network, in this session.
        if "identity" in targets:
            idents = cap(need_identity)
            result["identity_filled"] = _fill_identity(session, idents, stats)
            touched.update(st.symbol for st in idents)

        # 2) network jobs — each opens its own job_run/session (nested observability).
        if "yfinance" in targets and need_yf:
            syms = cap(need_yf)
            result["yfinance"] = refresh(symbols=syms, verbose=verbose, **net_kw)
            touched.update(syms)
        if "screener" in targets and need_screener:
            syms = cap(need_screener)
            result["screener"] = enrich(symbols=syms, verbose=verbose, **net_kw)
            touched.update(syms)
        if "prices" in targets and need_prices:
            syms = cap(need_prices)
            result["prices"] = backfill_prices(symbols=syms, verbose=verbose, **net_kw)
            touched.update(syms)

        # 3) read-back: re-audit exactly the stocks we touched so the reports
        #    reflect the post-fill state — the loop closes on evidence, not hope.
        #    (Insert-only-on-change: unchanged scorecards write no new rows.)
        if touched:
            pa = dq_audit(symbols=sorted(touched), verbose=False)
            result["post_audit"] = {
                "audited": pa.get("audited"),
                "reports_written": pa.get("reports_written"),
                "avg_overall": pa.get("avg_overall"),
            }

        stats.update({
            "candidates": {
                "identity": len(need_identity), "prices": len(need_prices),
                "screener": len(need_screener), "yfinance": len(need_yf),
            },
            "targets": list(targets),
            "result": result,
        })
        if verbose:
            print(f"[dq_fill] targets={targets} candidates={stats['candidates']}")
    return stats
