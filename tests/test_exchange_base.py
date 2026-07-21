"""PoliteSession pacing/backoff/cookie-dance + shared parsing/resolution helpers.

No network and no real clock: `_raw_get` and `_sleep` are the seams, both replaced
here, so every test exercises the decision logic (when to wait, when to back off, when
to re-warm NSE's cookies) deterministically.
"""

from engine.ingest import exchange_base as eb
from engine.ingest.exchange_base import (
    NSE_BASE,
    PoliteSession,
    clean,
    nse_session,
    resolve_stock_id,
    to_float,
    to_int,
)
from factories import make_stock


class FakeRNG:
    """Deterministic: no jitter, first User-Agent."""

    @staticmethod
    def uniform(a, b):
        return 0.0

    @staticmethod
    def choice(seq):
        return seq[0]


class FakeResp:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code = status
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _instrument(sess, responder):
    """Wire a PoliteSession to a scripted responder; record fetched URLs and sleeps."""
    urls, sleeps = [], []

    def _raw_get(url, headers, params, timeout):
        urls.append(url)
        return responder(url, len(urls))

    sess._raw_get = _raw_get
    sess._sleep = lambda s: sleeps.append(s)
    return urls, sleeps


# ---------- pacing ----------

def test_first_request_does_not_wait_but_second_to_same_host_does():
    s = PoliteSession(min_interval=1.5, jitter=0.0, rng=FakeRNG)
    urls, sleeps = _instrument(s, lambda url, n: FakeResp(200, {"ok": True}))
    s.get("https://host.example/a")
    assert sleeps == []                       # first call to the host: no wait
    s.get("https://host.example/b")
    assert len(sleeps) == 1 and sleeps[0] >= 1.4   # second call is throttled ~min_interval


def test_different_hosts_are_not_throttled_against_each_other():
    s = PoliteSession(min_interval=5.0, jitter=0.0, rng=FakeRNG)
    _, sleeps = _instrument(s, lambda url, n: FakeResp(200, {}))
    s.get("https://a.example/x")
    s.get("https://b.example/y")
    assert sleeps == []                       # distinct hosts, independent pacing


# ---------- backoff ----------

def test_retries_with_exponential_backoff_then_succeeds():
    s = PoliteSession(min_interval=0.0, jitter=0.0, backoff_base=2.0,
                      max_retries=4, rng=FakeRNG)

    def responder(url, n):
        return FakeResp(429) if n <= 2 else FakeResp(200, {"done": True})

    _, sleeps = _instrument(s, responder)
    resp = s.get("https://host.example/api")
    assert resp.status_code == 200
    # two 429s → two backoff sleeps at base**0 and base**1 (jitter 0).
    assert sleeps == [1.0, 2.0]


def test_gives_up_after_max_retries_and_returns_last_response():
    s = PoliteSession(min_interval=0.0, jitter=0.0, max_retries=2, rng=FakeRNG)
    _instrument(s, lambda url, n: FakeResp(403))
    resp = s.get("https://host.example/api")
    assert resp.status_code == 403            # returned, not raised — caller decides


# ---------- NSE cookie dance ----------

def test_nse_session_warms_homepage_before_first_api_call():
    s = nse_session(min_interval=0.0, jitter=0.0, rng=FakeRNG)
    urls, _ = _instrument(s, lambda url, n: FakeResp(200, {"data": []}))
    s.get(f"{NSE_BASE}/api/corporate-announcements", referer=f"{NSE_BASE}/x")
    assert urls[0] == NSE_BASE                 # homepage first (cookie warmup)
    assert urls[1].endswith("/api/corporate-announcements")


def test_nse_403_triggers_cookie_rewarm_then_retry():
    s = nse_session(min_interval=0.0, jitter=0.0, max_retries=3, rng=FakeRNG)

    api = f"{NSE_BASE}/api/x"

    def responder(url, n):
        # homepage always 200; the api is 403 once, then 200.
        if url == NSE_BASE:
            return FakeResp(200, {})
        return FakeResp(403) if url == api and n < 4 else FakeResp(200, {"ok": 1})

    urls, _ = _instrument(s, responder)
    resp = s.get(api)
    assert resp.status_code == 200
    # homepage fetched twice: initial warmup + the re-warm after the 403.
    assert urls.count(NSE_BASE) == 2


# ---------- helpers ----------

def test_clean_normalizes_blanks():
    assert clean("  ABC ") == "ABC"
    assert clean("-") is None
    assert clean("") is None
    assert clean(None) is None


def test_to_float_strips_indian_grouping():
    assert to_float("1,23,456.78") == 123456.78
    assert to_float("₹ 99.5") == 99.5
    assert to_float("-") is None
    assert to_int("5,00,000") == 500000


def test_resolve_stock_id_matches_nse_symbol_and_canonical_symbol(db_session):
    stock = make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    other = make_stock(db_session, "TATAMOTORS")
    assert resolve_stock_id(db_session, "atherenerg") == stock.id   # case-insensitive
    assert resolve_stock_id(db_session, "TATAMOTORS") == other.id
    assert resolve_stock_id(db_session, "NOTINUNIVERSE") is None
    assert resolve_stock_id(db_session, None) is None
