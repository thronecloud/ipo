"""
The data-quality auditor: score every stock and persist a scorecard.

`audit(...)` iterates the selected stocks, scores each with `score_stock`, writes a
`DataQualityReport` row, and rolls a summary (grade counts, per-dimension coverage,
flag/missing tallies) into the JobRun stats so the admin dashboard can read it.
"""

from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import select

from db.models import DataQualityReport, Stock
from engine.quality.dimensions import WEIGHTS, score_stock
from engine.repo import job_run, universe_contains


def audit(universe=None, symbols=None, statuses=("active",), limit=0, verbose=True) -> dict:
    _t = universe or (symbols and f"{len(symbols)} symbols") or "all"
    with job_run("dq_audit", target=_t) as (session, stats):
        q = select(Stock)
        if symbols:
            q = q.where(Stock.symbol.in_([s.upper() for s in symbols]))
        elif statuses:
            q = q.where(Stock.status.in_(list(statuses)))
        if universe:
            q = q.where(universe_contains(universe))
        stocks = session.scalars(q.order_by(Stock.symbol)).all()
        if limit:
            stocks = stocks[:limit]

        grades = Counter()
        flag_counts = Counter()
        missing_counts = Counter()
        dim_score_sum = Counter()
        dim_score_n = Counter()
        dim_pass = Counter()
        overall_sum = 0.0
        overall_n = 0
        written = 0

        # One time reference for the whole run → the scorecard is a pure function of
        # stored data + this fixed `as_of`, so re-auditing unchanged data is idempotent.
        as_of = datetime.now(timezone.utc)

        for i, stock in enumerate(stocks):
            rep = score_stock(session, stock, as_of=as_of)
            # Insert-only-on-change: skip a new row when the scorecard is identical to
            # the latest one (stops unbounded, drifting history on unchanged data).
            latest = session.scalar(
                select(DataQualityReport)
                .where(DataQualityReport.stock_id == stock.id)
                .order_by(DataQualityReport.checked_at.desc(), DataQualityReport.id.desc())
                .limit(1)
            )
            changed = latest is None or (
                latest.grade != rep["grade"]
                or latest.dimensions != rep["dimensions"]
                or latest.flags != rep["flags"]
                or latest.missing != rep["missing"]
            )
            if changed:
                session.add(DataQualityReport(
                    stock_id=stock.id,
                    checked_at=as_of,
                    overall_score=rep["overall_score"],
                    grade=rep["grade"],
                    dimensions=rep["dimensions"],
                    flags=rep["flags"],
                    missing=rep["missing"],
                ))
                written += 1
            grades[rep["grade"] or "?"] += 1
            for f in rep["flags"]:
                flag_counts[f["type"]] += 1
            for m in rep["missing"]:
                # collapse identity:sector -> identity for the rollup
                missing_counts[m.split(":")[0]] += 1
            for dim, d in rep["dimensions"].items():
                if d["status"] != "na" and d["score"] is not None:
                    dim_score_sum[dim] += d["score"]
                    dim_score_n[dim] += 1
                    if d["status"] == "pass":
                        dim_pass[dim] += 1
            if rep["overall_score"] is not None:
                overall_sum += rep["overall_score"]
                overall_n += 1
            if verbose and (i + 1) % 200 == 0:
                print(f"  [{i+1}/{len(stocks)}] audited")
            if (i + 1) % 300 == 0:
                session.commit()
        session.commit()

        dimension_coverage = {
            dim: {
                "avg_score": round(dim_score_sum[dim] / dim_score_n[dim], 3) if dim_score_n[dim] else None,
                "pct_pass": round(100 * dim_pass[dim] / dim_score_n[dim], 1) if dim_score_n[dim] else None,
                "scored": dim_score_n[dim],
            }
            for dim in WEIGHTS
        }
        stats.update({
            "audited": len(stocks),
            "reports_written": written,
            "avg_overall": round(overall_sum / overall_n, 1) if overall_n else None,
            "grades": dict(grades),
            "dimension_coverage": dimension_coverage,
            "flags": dict(flag_counts),
            "missing": dict(missing_counts),
        })
        if verbose:
            print(f"[dq_audit] {len(stocks)} stocks | avg {stats['avg_overall']} | grades {dict(grades)}")
            print(f"  flags: {dict(flag_counts)}")
            print(f"  missing: {dict(missing_counts)}")
    return stats
