"""Data-versioned cache for the expensive backtest studies.

``run_event_study`` / ``run_persona_study`` / ``run_vintage_study`` each scan the
whole cohort and bootstrap thousands of resamples — tens of seconds a call. Behind
Next's ~30s ``/api`` proxy a cold call 500s the dashboard. Their output is a pure
function of the scored composites and the price/benchmark bars, so the cache keys
on a cheap "data version": the newest composite ``computed_at`` and the newest daily
and benchmark bar dates. A hit returns in microseconds; a miss recomputes once and
stores both in-process (an LRU, so a flood of distinct persona subsets can't grow it
without bound) and to a small JSON file so a process restart is still warm.

A scheduler job (``job_backtest_warm``) recomputes the default studies proactively so
the 25s cost is paid off the request path, not by whoever loads the dashboard first.
"""

from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from pathlib import Path

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select

from db.models import CompositeScore, DailyPrice, IndexPrice

# Bounded so an unbounded variety of persona subsets / benchmarks can't grow the
# cache without limit; the most-recently-served entries are the ones kept.
MAX_ENTRIES = 64

_lock = threading.RLock()
_store: "OrderedDict[str, dict]" = OrderedDict()  # key -> {"version": str, "payload": dict}
_loaded = False


def _cache_path() -> Path:
    return Path(
        os.environ.get(
            "BACKTEST_CACHE_PATH",
            str(Path(__file__).resolve().parents[2] / "data" / "backtest_cache.json"),
        )
    )


def data_version(session) -> str:
    """A token that changes exactly when a study's inputs change: the newest composite
    score, the newest daily bar, the newest benchmark bar. Cheap (three MAX scans)."""
    cs = session.scalar(select(func.max(CompositeScore.computed_at)))
    dp = session.scalar(select(func.max(DailyPrice.date)))
    ix = session.scalar(select(func.max(IndexPrice.date)))
    return "|".join("none" if v is None else v.isoformat() for v in (cs, dp, ix))


def _key(name: str, params: dict) -> str:
    return name + ":" + json.dumps(params, sort_keys=True, default=str)


def _load_disk() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        raw = json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        return
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, dict) and "version" in v and "payload" in v:
                _store[k] = v


def _persist() -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(_store))
        tmp.replace(path)
    except OSError:
        pass  # the cache is an optimisation — a read-only fs must never 500 the API


def cached_study(session, name: str, params: dict, compute) -> dict:
    """Return the study for (name, params), recomputing only when the data version
    changed. ``compute`` is a zero-arg callable returning the study dict."""
    version = data_version(session)
    key = _key(name, params)
    with _lock:
        _load_disk()
        entry = _store.get(key)
        if entry is not None and entry.get("version") == version:
            _store.move_to_end(key)
            return entry["payload"]

    # Compute outside the lock — a 25s study must not serialise unrelated callers.
    payload = jsonable_encoder(compute())

    with _lock:
        # Once the inputs move, every entry keyed to an older version is dead weight,
        # not just this key's — drop them so the file and the LRU stay small.
        for k in [k for k, v in _store.items() if v.get("version") != version]:
            del _store[k]
        _store[key] = {"version": version, "payload": payload}
        _store.move_to_end(key)
        while len(_store) > MAX_ENTRIES:
            _store.popitem(last=False)
        _persist()
    return payload


def clear() -> None:
    """Reset the in-process cache (and force a disk reload next call). For tests."""
    global _loaded
    with _lock:
        _store.clear()
        _loaded = False
