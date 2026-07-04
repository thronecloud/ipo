#!/usr/bin/env bash
# Analyze all ipo_2026 IPOs with Fable — 10 parallel workers, one per persona.
#
# Each worker runs the incremental analysis engine scoped to a single persona over
# the ipo_2026 universe. Work is idempotent (find_work skips pairs already analyzed
# at the current data_hash), so re-running after rate-limit errors just fills gaps.
#
# After all 10 finish, a single-threaded pass recomputes composite scores for the
# universe — this collapses any duplicate CompositeScore rows left by the workers'
# concurrent end-of-run rescoring into one clean row per stock.
#
# Usage:  bash scripts/analyze_ipo2026_parallel.sh
set -u

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate 2>/dev/null || source venv/bin/activate 2>/dev/null

export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://ipo:ipo@localhost:5432/ipo}"
export ANALYSIS_BACKEND=cli
export ANALYSIS_MODEL="${ANALYSIS_MODEL:-claude-fable-5}"

LOGDIR=/tmp/ipo2026_analysis
mkdir -p "$LOGDIR"

PERSONAS=(warren_buffett charlie_munger benjamin_graham peter_lynch philip_fisher \
          joel_greenblatt howard_marks rakesh_jhunjhunwala radhakishan_damani vijay_kedia)

echo "[$(date -u +%H:%M:%SZ)] launching ${#PERSONAS[@]} parallel persona workers over ipo_2026"
pids=()
for slug in "${PERSONAS[@]}"; do
  python3 -c "
from engine.analysis.engine import run_incremental
stats = run_incremental(personas=['$slug'], universe='ipo_2026', delay=1.0, verbose=True)
print('DONE $slug', {k: stats.get(k) for k in ('planned','success','error')})
" > "$LOGDIR/$slug.log" 2>&1 &
  pids+=($!)
  echo "  worker $slug -> pid $! (log: $LOGDIR/$slug.log)"
done

echo "[$(date -u +%H:%M:%SZ)] waiting for ${#pids[@]} workers..."
fail=0
for pid in "${pids[@]}"; do
  wait "$pid" || fail=$((fail+1))
done
echo "[$(date -u +%H:%M:%SZ)] all workers finished ($fail non-zero exits)"

echo "[$(date -u +%H:%M:%SZ)] final composite recompute over ipo_2026..."
python3 -c "
from sqlalchemy import select, func
from db.base import SessionLocal
from db.models import Stock, CompositeScore
from engine.repo import universe_contains, recompute_scores_for_stock
s = SessionLocal()
stocks = s.scalars(select(Stock).where(universe_contains('ipo_2026'))).all()
n = 0
for st in stocks:
    if recompute_scores_for_stock(s, st) is not None:
        n += 1
s.commit()
scored = s.scalar(select(func.count(func.distinct(CompositeScore.stock_id)))
                  .select_from(CompositeScore).join(Stock, Stock.id==CompositeScore.stock_id)
                  .where(universe_contains('ipo_2026')))
print(f'recomputed composites for {n} ipo_2026 stocks; distinct scored = {scored}')
s.close()
"
echo "[$(date -u +%H:%M:%SZ)] ipo_2026 analysis complete. Per-persona logs in $LOGDIR/"
