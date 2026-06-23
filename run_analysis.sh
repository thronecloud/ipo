#!/bin/bash
# Auto-restarting analysis runner for Claude Max plan
# Monitors 10 persona processes, restarts any that die,
# sleeps during rate limit periods, runs until all analyses complete.
#
# Usage: ./run_analysis.sh [model] [delay]
#   model: sonnet (default) or opus
#   delay: seconds between calls (default: 5)

MODEL="${1:-sonnet}"
DELAY="${2:-5}"
TARGET=3740  # 374 stocks x 10 personas
CHECK_INTERVAL=120  # seconds between health checks

PERSONAS=(
    warren_buffett
    charlie_munger
    benjamin_graham
    peter_lynch
    philip_fisher
    joel_greenblatt
    howard_marks
    rakesh_jhunjhunwala
    radhakishan_damani
    vijay_kedia
)

count_analyses() {
    find data/analyses -name "*.json" 2>/dev/null | wc -l
}

log() {
    echo "[$(date '+%H:%M:%S')] $1"
}

# Initial launch
log "Starting analysis runner: model=$MODEL, delay=$DELAY"
log "Target: $TARGET analyses"

cd "$(dirname "$0")"

for p in "${PERSONAS[@]}"; do
    python3 -m src.analyze --persona "$p" --model "$MODEL" --delay "$DELAY" > /dev/null 2>&1 &
done

log "Launched ${#PERSONAS[@]} processes"

# Monitor loop
while true; do
    sleep "$CHECK_INTERVAL"

    CURRENT=$(count_analyses)
    log "Progress: $CURRENT/$TARGET ($(( CURRENT * 100 / TARGET ))%)"

    # Check if done
    if [ "$CURRENT" -ge "$TARGET" ]; then
        log "ALL ANALYSES COMPLETE!"
        python3 -m src.score
        log "Scores computed. Dashboard ready."
        exit 0
    fi

    # Check each persona and restart if dead
    for p in "${PERSONAS[@]}"; do
        ALIVE=$(ps aux | grep "src.analyze" | grep "$p" | grep -v grep | wc -l)
        if [ "$ALIVE" -eq 0 ]; then
            log "Restarting $p (process died)"
            python3 -m src.analyze --persona "$p" --model "$MODEL" --delay "$DELAY" > /dev/null 2>&1 &
        fi
    done

    # Kill duplicates (keep only newest per persona)
    for p in "${PERSONAS[@]}"; do
        COUNT=$(ps aux | grep "src.analyze" | grep "$p" | grep -v grep | wc -l)
        if [ "$COUNT" -gt 1 ]; then
            # Kill all but the newest
            ps aux | grep "src.analyze" | grep "$p" | grep -v grep | awk '{print $2}' | sort -n | head -n -1 | xargs kill -9 2>/dev/null
            log "Killed $(( COUNT - 1 )) duplicate(s) for $p"
        fi
    done

    PROCS=$(ps aux | grep "src.analyze" | grep -v grep | wc -l)
    log "Active processes: $PROCS"
done
