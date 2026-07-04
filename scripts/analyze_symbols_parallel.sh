#!/usr/bin/env bash
# Force-reanalyze an explicit list of symbols with Fable — 10 parallel workers,
# one per investor persona. Then recompute composite scores for just those stocks.
#
# Usage:  bash scripts/analyze_symbols_parallel.sh SYM1 SYM2 SYM3 ...
set -u

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate 2>/dev/null || source venv/bin/activate 2>/dev/null

export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://ipo:ipo@localhost:5432/ipo}"
export ANALYSIS_BACKEND=cli
export ANALYSIS_MODEL="${ANALYSIS_MODEL:-claude-fable-5}"

if [ "$#" -eq 0 ]; then
  echo "usage: $0 SYM1 SYM2 ..." >&2
  exit 2
fi
SYMS_PY="[$(printf "'%s'," "$@")]"   # python list literal: ['A','B',...]

LOGDIR=/tmp/symbols_analysis
mkdir -p "$LOGDIR"

PERSONAS=(warren_buffett charlie_munger benjamin_graham peter_lynch philip_fisher \
          joel_greenblatt howard_marks rakesh_jhunjhunwala radhakishan_damani vijay_kedia)

echo "[$(date -u +%H:%M:%SZ)] force-reanalyzing $# symbols: $* — ${#PERSONAS[@]} parallel persona workers"
pids=()
for slug in "${PERSONAS[@]}"; do
  python3 -c "
from engine.analysis.engine import run_incremental
stats = run_incremental(personas=['$slug'], symbols=$SYMS_PY, force=True, delay=1.0, verbose=True)
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

echo "[$(date -u +%H:%M:%SZ)] final composite recompute for these symbols..."
python3 -c "
from sqlalchemy import select
from db.base import SessionLocal
from db.models import Stock
from engine.repo import recompute_scores_for_stock
syms = [s.upper() for s in $SYMS_PY]
s = SessionLocal()
stocks = s.scalars(select(Stock).where(Stock.symbol.in_(syms))).all()
n = 0
for st in stocks:
    if recompute_scores_for_stock(s, st) is not None:
        n += 1
        print(f'  rescored {st.symbol}')
s.commit(); s.close()
print(f'recomputed composites for {n} stocks')
"
echo "[$(date -u +%H:%M:%SZ)] done. Per-persona logs in $LOGDIR/"
