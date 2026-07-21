"""Data-versioned cache for the expensive backtest studies.

The studies (event / persona / vintage) are ~25s cohort scans behind Next's ~30s
proxy — a cold call 500s the dashboard. The cache keys on a cheap data version (the
newest composite computed_at and the newest daily/benchmark bar date) so a repeat
call returns instantly, and any change to the underlying scores or bars forces a
recompute. These tests exercise that layer directly with a compute counter, so hits
and invalidations are asserted exactly rather than timed.
"""

from datetime import date, datetime, timedelta, timezone

from db.models import CompositeScore, DailyPrice, IndexPrice
from engine.backtest import cache
from tests.factories import make_stock

UTC = timezone.utc


def _seed_composite(session, stock, computed_at):
    session.add(CompositeScore(stock_id=stock.id, composite_score=80.0,
                               computed_at=computed_at))
    session.commit()


def _seed_bar(session, stock, d: date):
    session.add(DailyPrice(stock_id=stock.id, date=d, open=1, high=1, low=1,
                           close=1, volume=1))
    session.commit()


class _Counter:
    """A compute callable that records how many times it actually ran."""

    def __init__(self, payload):
        self.calls = 0
        self.payload = payload

    def __call__(self):
        self.calls += 1
        return self.payload


def test_second_call_is_served_from_cache(db_session):
    stock = make_stock(db_session, "CACHE1")
    _seed_composite(db_session, stock, datetime(2026, 1, 1, tzinfo=UTC))
    compute = _Counter({"ok": True})

    first = cache.cached_study(db_session, "event", {"benchmark": "B"}, compute)
    second = cache.cached_study(db_session, "event", {"benchmark": "B"}, compute)

    assert first == second == {"ok": True}
    assert compute.calls == 1  # the second call never recomputed


def test_new_composite_invalidates_the_cache(db_session):
    stock = make_stock(db_session, "CACHE2")
    _seed_composite(db_session, stock, datetime(2026, 1, 1, tzinfo=UTC))
    compute = _Counter({"n": 1})
    cache.cached_study(db_session, "event", {"benchmark": "B"}, compute)

    # A freshly-scored composite moves the data version → recompute.
    other = make_stock(db_session, "CACHE2B")
    _seed_composite(db_session, other, datetime(2026, 2, 1, tzinfo=UTC))
    cache.cached_study(db_session, "event", {"benchmark": "B"}, compute)

    assert compute.calls == 2


def test_new_price_bar_invalidates_the_cache(db_session):
    stock = make_stock(db_session, "CACHE3")
    _seed_composite(db_session, stock, datetime(2026, 1, 1, tzinfo=UTC))
    _seed_bar(db_session, stock, date(2026, 1, 5))
    compute = _Counter({"n": 1})
    cache.cached_study(db_session, "event", {"benchmark": "B"}, compute)

    _seed_bar(db_session, stock, date(2026, 1, 6))  # newer bar → new version
    cache.cached_study(db_session, "event", {"benchmark": "B"}, compute)

    assert compute.calls == 2


def test_distinct_params_are_cached_separately(db_session):
    stock = make_stock(db_session, "CACHE4")
    _seed_composite(db_session, stock, datetime(2026, 1, 1, tzinfo=UTC))
    a = _Counter({"which": "A"})
    b = _Counter({"which": "B"})

    # Different persona subsets are different keys → each computes once, but a repeat
    # of either is a hit (the LRU keeps recent subsets).
    cache.cached_study(db_session, "personas", {"personas": ["x"]}, a)
    cache.cached_study(db_session, "personas", {"personas": ["y"]}, b)
    assert cache.cached_study(db_session, "personas", {"personas": ["x"]}, a)["which"] == "A"
    assert cache.cached_study(db_session, "personas", {"personas": ["y"]}, b)["which"] == "B"

    assert a.calls == 1 and b.calls == 1


def test_stale_version_entries_are_evicted_on_recompute(db_session):
    stock = make_stock(db_session, "CACHE5")
    _seed_composite(db_session, stock, datetime(2026, 1, 1, tzinfo=UTC))
    cache.cached_study(db_session, "event", {"benchmark": "B"}, _Counter({"v": 1}))
    cache.cached_study(db_session, "personas", {"personas": None}, _Counter({"v": 1}))

    # Advance the version, then recompute one key: the other key's stale entry must be
    # purged so the cache never serves a study computed against superseded data.
    _seed_bar(db_session, stock, date(2026, 3, 1))
    fresh = _Counter({"v": 2})
    cache.cached_study(db_session, "event", {"benchmark": "B"}, fresh)

    # The stale "personas" entry was dropped, so it recomputes against the new version.
    personas_recompute = _Counter({"v": 2})
    cache.cached_study(db_session, "personas", {"personas": None}, personas_recompute)
    assert personas_recompute.calls == 1


def test_endpoint_serves_repeat_calls_from_cache(db_session, monkeypatch):
    """The HTTP path itself caches: the second GET /api/backtest must not recompute
    the study — the whole point of moving the ~25s scan off the request path."""
    import api.routers.consumer as consumer
    from fastapi.testclient import TestClient

    from api.main import app

    stock = make_stock(db_session, "EP1")
    _seed_composite(db_session, stock, datetime(2026, 1, 1, tzinfo=UTC))

    calls = {"n": 0}

    def _fake_study(db, **kwargs):
        calls["n"] += 1
        return {"computed": calls["n"]}

    monkeypatch.setattr(consumer, "run_event_study", _fake_study)

    with TestClient(app) as c:
        first = c.get("/api/backtest", params={"benchmark": "B"})
        second = c.get("/api/backtest", params={"benchmark": "B"})

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert calls["n"] == 1  # only the first request computed


def test_disk_persistence_survives_a_process_restart(db_session):
    stock = make_stock(db_session, "CACHE6")
    _seed_composite(db_session, stock, datetime(2026, 1, 1, tzinfo=UTC))
    cache.cached_study(db_session, "event", {"benchmark": "B"}, _Counter({"persisted": True}))

    # Simulate a restart: the in-process store is gone, but the JSON file remains.
    cache.clear()
    after_restart = _Counter({"persisted": False})
    got = cache.cached_study(db_session, "event", {"benchmark": "B"}, after_restart)

    assert got == {"persisted": True}      # warmed from disk
    assert after_restart.calls == 0        # never recomputed
