"""
Shared foundation for the NSE/BSE direct scrapers.

`PoliteSession` is a thin, deterministic wrapper over a `requests.Session` that a
datacenter IP can use without getting itself banned:

  - a realistic rotating User-Agent (one per session, since NSE ties its cookies to
    the UA that obtained them),
  - a per-host minimum interval + jitter so we never burst a single host,
  - exponential backoff (capped, jittered) on the throttle/anti-bot statuses
    (429 / 403 / 401 / 503),
  - a hard connect+read timeout on every request,
  - and NSE's homepage-first cookie dance: NSE's JSON APIs 401/403 unless the
    session already carries the cookies its homepage sets, so `nse_session()` warms
    them before the first API call and re-warms them if a call is later rejected.

The actual socket call is isolated in `PoliteSession._raw_get`, and the sleeping in
`PoliteSession._sleep`, so tests exercise the throttle/backoff/warmup logic with both
monkeypatched — nothing here ever touches the network or the clock in CI. The
per-source fetchers keep their live-endpoint calls behind their own `_fetch_*`
seams for the same reason.
"""

import os
import random
import time
from urllib.parse import urlparse

import requests

from sqlalchemy import func, or_, select

from db.models import Stock

# Realistic desktop User-Agents. One is chosen per session and kept for the session's
# life so the cookies NSE issues stay bound to the UA that fetched them.
USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
)

# Statuses that mean "slow down / prove you are a browser", not "this resource is gone".
RETRY_STATUSES = frozenset({429, 403, 401, 503})
# Subset that specifically indicates a stale/absent anti-bot cookie on NSE.
COOKIE_STATUSES = frozenset({401, 403})

NSE_BASE = "https://www.nseindia.com"
BSE_BASE = "https://www.bseindia.com"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class PoliteSession:
    """Rate-limited, backoff-guarded HTTP GET wrapper. Not thread-safe by design —
    one session per fetcher, used serially."""

    def __init__(self, *, min_interval: float | None = None, jitter: float = 0.5,
                 timeout: float = 30.0, max_retries: int = 4,
                 backoff_base: float = 2.0, backoff_cap: float | None = None,
                 warmup_url: str | None = None, rng=random):
        self._session = requests.Session()
        self.min_interval = (min_interval if min_interval is not None
                             else _env_float("SCRAPE_MIN_INTERVAL", 1.5))
        self.jitter = jitter
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = (backoff_cap if backoff_cap is not None
                            else _env_float("SCRAPE_BACKOFF_CAP", 60.0))
        self.warmup_url = warmup_url
        self._rng = rng
        self._ua = rng.choice(USER_AGENTS)
        self._last: dict[str, float] = {}   # host -> monotonic time of last request
        self._warmed = False

    # --- seams the tests override (no network, no real clock) ---
    def _raw_get(self, url, headers, params, timeout):
        return self._session.get(url, headers=headers, params=params, timeout=timeout)

    def _sleep(self, seconds: float):
        time.sleep(seconds)

    # --- pacing ---
    def _throttle(self, host: str):
        last = self._last.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            wait = self.min_interval - elapsed
            if wait > 0:
                self._sleep(wait + self._rng.uniform(0, self.jitter))
        self._last[host] = time.monotonic()

    def _backoff_delay(self, attempt: int) -> float:
        raw = self.backoff_base ** attempt
        return min(self.backoff_cap, raw) + self._rng.uniform(0, self.jitter)

    def _headers(self, referer: str | None, extra: dict | None) -> dict:
        h = {
            "User-Agent": self._ua,
            "Accept": "application/json, text/html, */*",
            "Accept-Language": "en-US,en;q=0.9",
            # Only advertise what requests can always decode. Brotli ('br') needs the
            # optional brotli package; without it NSE's br-compressed JSON comes back as
            # undecodable bytes and .json() dies on "line 1 column 1".
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        if referer:
            h["Referer"] = referer
        if extra:
            h.update(extra)
        return h

    def warmup(self, url: str | None = None, referer: str | None = None):
        """Prime the session's cookies with a SINGLE homepage GET.

        NSE serves its homepage a 403 to datacenter IPs but still sets the anti-bot
        cookies on that response, and the JSON API then answers 200 with those cookies.
        So the warmup must NOT go through the retry loop — retrying the 403 both wastes
        requests and trips a heavier block; we take whatever the one hit gives us and
        let the real API call be the arbiter."""
        target = url or self.warmup_url
        if not target:
            return
        host = urlparse(target).netloc
        self._throttle(host)
        try:
            self._raw_get(target, self._headers(referer, None), None, self.timeout)
        except requests.RequestException:
            pass  # a failed warmup may still have set cookies; the API call decides
        self._warmed = True

    def _raw_throttled_get(self, url, headers, params, warmup):
        """One paced request with backoff. On a cookie-status rejection, re-warm the
        NSE homepage once per attempt before retrying, so an expired anti-bot cookie
        heals mid-run instead of failing the whole poll."""
        host = urlparse(url).netloc
        attempt = 0
        while True:
            self._throttle(host)
            resp = self._raw_get(url, headers, params, self.timeout)
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                if (warmup and self.warmup_url
                        and resp.status_code in COOKIE_STATUSES):
                    # re-prime cookies (without recursing into the retry loop)
                    self._throttle(urlparse(self.warmup_url).netloc)
                    self._raw_get(self.warmup_url,
                                  self._headers(None, None), None, self.timeout)
                self._sleep(self._backoff_delay(attempt))
                attempt += 1
                continue
            return resp

    def get(self, url: str, *, params: dict | None = None,
            referer: str | None = None, headers: dict | None = None):
        if self.warmup_url and not self._warmed:
            self.warmup()
        return self._raw_throttled_get(url, self._headers(referer, headers), params,
                                       warmup=True)

    def get_json(self, url: str, **kw):
        resp = self.get(url, **kw)
        resp.raise_for_status()
        return resp.json()


def nse_session(**kw) -> PoliteSession:
    """A PoliteSession pre-configured for NSE's homepage-first cookie requirement."""
    return PoliteSession(warmup_url=NSE_BASE, **kw)


def bse_session(**kw) -> PoliteSession:
    """A PoliteSession for BSE's JSON API (needs a bseindia.com Referer/Origin, no
    cookie dance)."""
    return PoliteSession(**kw)


# ---------- shared parsing helpers ----------

def clean(v):
    """Trim to a non-empty string, or None. Treats the exchange feeds' '-'/'' blanks
    as missing."""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "-", "NA", "N/A", "--"):
        return None
    return s


def to_float(v):
    """Coerce an exchange-formatted number ('1,23,456.78') to float, or None."""
    s = clean(v)
    if s is None:
        return None
    s = s.replace(",", "").replace("₹", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def to_int(v):
    f = to_float(v)
    return int(f) if f is not None else None


# ---------- symbol → universe resolution ----------

def resolve_stock_id(session, symbol: str | None) -> int | None:
    """Map a raw exchange ticker to our internal stock id, or None if it is not in
    our universe. Matches case-insensitively against both `nse_symbol` and the
    canonical `symbol` — an unmatched ticker (a non-universe company) is a valid
    outcome, never an error."""
    s = clean(symbol)
    if s is None:
        return None
    key = s.upper()
    return session.scalar(
        select(Stock.id).where(
            or_(func.upper(Stock.nse_symbol) == key,
                func.upper(Stock.symbol) == key)
        ).limit(1)
    )
