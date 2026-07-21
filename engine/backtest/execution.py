"""Execution-reality modeling for the Indian smallcap backtest.

The event study measures entries at the information-date close with zero
friction. For an SME-heavy cohort that is a fiction, so this module supplies the
pure execution primitives the studies wire in: next-open entry with a
circuit-delay window, one-way friction applied on both legs, and a
trailing-liquidity (ADV) thinness flag. Every constant is chosen conservatively
and documented so a reader can re-derive or override it.

── Entry basis ───────────────────────────────────────────────────
Entry is the FIRST session strictly after the information date (no same-day
fill, no lookahead — unchanged). Within that session we transact at the OPEN
where the provider carries one, falling back to that session's close when the
open is missing (older / SME bars sometimes lack a distinct open). This replaces
the previous close-at-entry basis, which was a price you could never have
transacted at: the signal is formed on that close.

── Circuit detection ─────────────────────────────────────────────
A session is treated as circuit-locked (untradeable) when its range is zero
(high == low) AND the close sits at least CIRCUIT_PCT away from the prior
session's close. BOTH conditions are required: a zero-range bar on its own can
be a flat no-trade day on an illiquid name, whereas a zero-range bar a full band
from yesterday's close is the EOD signature of a locked upper/lower circuit —
every print at the band, no depth to fill against. When the intended entry
session is locked, entry is delayed to the first tradeable session within
ENTRY_WINDOW trading days. If every session in that window is locked, the pick
is UNENTERABLE and excluded with reason (folded into cohort accounting, never
silently dropped).

CIRCUIT_PCT is 4.9%, just inside the tightest common band (±5%). SME and many
mainboard scrips also carry ±10%/±20% bands; keying on the tightest catches all
of them while sitting below it so a genuine 5% locked move is not missed to
rounding.

── Friction ──────────────────────────────────────────────────────
FRICTION_BPS is a one-way, all-in cost applied on BOTH the entry and the exit
leg. The default 85 bps for smallcaps decomposes as:
  · STT, delivery equity                  ~10 bps  (0.1% per side)
  · Exchange txn + SEBI + stamp + GST       ~2 bps  (small, bundled)
  · Brokerage + half-spread / impact       ~73 bps  (dominant on illiquid SME
                                                     names — the spread you cross
                                                     plus market impact)
                                          ─────────
                                           ~85 bps one-way (~170 bps round trip)
The impact/spread allowance dominates and is deliberately generous: for a thin
name the true cost is size-dependent and often worse, which is why thin picks
(below) are additionally quarantined rather than trusted at this rate.

Friction is multiplicative, not additive: you buy at entry*(1+f) and sell at
exit*(1-f), so a gross ratio g = exit/entry realizes g*(1-f)/(1+f). This is
exact and symmetric, and collapses to roughly gross - 2f for small f.

── Thinness (ADV) ────────────────────────────────────────────────
A pick is flagged ``thin`` when its trailing median daily traded value
(close*volume over the ADV_WINDOW sessions up to and including entry) is below
ADV_FLOOR (₹25 lakh). At that liquidity the printed return may be unrealizable
in any meaningful size, so thin picks are reported and counted separately and
every headline stat is published all-picks AND ex-thin. ₹25 lakh/day is a
conservative floor: below it even a modest position is a large share of a day's
turnover.
"""

from __future__ import annotations

from collections import namedtuple
from datetime import date

# A daily OHLCV bar as the studies consume it. Kept as a lightweight namedtuple
# so existing positional habits (bar[0] == date) still read while .close / .open
# make the execution logic legible.
Bar = namedtuple("Bar", ("date", "open", "high", "low", "close", "volume"))

# The resolution of an entry attempt. status ∈ {"ok", "predate", "unenterable"}:
# "predate" — no bar strictly after the information date; "unenterable" — bars
# exist but every one in the window is circuit-locked. delay is trading days
# between the first post-view session and the session actually entered.
EntryResolution = namedtuple("EntryResolution", ("index", "price", "basis", "delay", "status"))

# ── constants (see module docstring for the reasoning behind each) ──
ENTRY_WINDOW = 5          # trading days to look for a tradeable session
CIRCUIT_PCT = 4.9         # |Δclose| at a zero-range bar to call it band-locked
FRICTION_BPS = 85.0       # one-way, all-in smallcap cost, charged on both legs
ADV_WINDOW = 21           # trailing sessions for the median-traded-value test
ADV_FLOOR = 2_500_000.0   # ₹25 lakh/day median traded value below which = thin


def is_circuit_locked(bar: Bar, prev_close: float | None, circuit_pct: float = CIRCUIT_PCT) -> bool:
    """True when ``bar`` is a zero-range session pinned a full band from the
    prior close — the EOD signature of a locked upper/lower circuit."""
    if bar.high is None or bar.low is None or bar.high != bar.low:
        return False
    if prev_close is None or not prev_close or bar.close is None:
        return False
    return abs((bar.close / prev_close - 1.0) * 100.0) >= circuit_pct


def resolve_entry(series: list[Bar], info_date: date,
                  entry_window: int = ENTRY_WINDOW,
                  circuit_pct: float = CIRCUIT_PCT) -> EntryResolution:
    """Where a pick actually gets filled: the first tradeable session strictly
    after ``info_date``, entering at its open (fallback close), skipping
    circuit-locked sessions up to ``entry_window`` trading days."""
    start = next((i for i, b in enumerate(series) if b.date > info_date), None)
    if start is None:
        return EntryResolution(None, None, None, 0, "predate")
    end = min(len(series), start + entry_window)
    for i in range(start, end):
        bar = series[i]
        prev_close = series[i - 1].close if i > 0 else None
        if is_circuit_locked(bar, prev_close, circuit_pct):
            continue
        if bar.open is not None:
            return EntryResolution(i, bar.open, "open", i - start, "ok")
        return EntryResolution(i, bar.close, "close", i - start, "ok")
    return EntryResolution(None, None, None, 0, "unenterable")


def friction_multiplier(friction_bps: float = FRICTION_BPS) -> float:
    """The factor a gross price ratio is scaled by to net out round-trip
    friction: (1 - f) / (1 + f) for one-way fraction f."""
    f = friction_bps / 10_000.0
    return (1.0 - f) / (1.0 + f)


def apply_friction(gross_pct: float | None, friction_bps: float = FRICTION_BPS) -> float | None:
    """Net-of-friction percentage return from a gross percentage return, with
    ``friction_bps`` charged on both the entry and the exit leg."""
    if gross_pct is None:
        return None
    g = 1.0 + gross_pct / 100.0
    return (g * friction_multiplier(friction_bps) - 1.0) * 100.0


def trailing_adv(series: list[Bar], entry_idx: int, window: int = ADV_WINDOW) -> float | None:
    """Median daily traded value (close*volume) over the ``window`` sessions up
    to and including entry — the liquidity known at the decision point. None when
    no session in the window carries volume."""
    lo = max(0, entry_idx - window + 1)
    vals = [b.close * b.volume for b in series[lo:entry_idx + 1]
            if b.volume is not None and b.close is not None]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def is_thin(adv: float | None, floor: float = ADV_FLOOR) -> bool:
    """True when trailing liquidity is below the floor (unknown ADV is NOT thin —
    absence of volume data is not evidence of illiquidity)."""
    return adv is not None and adv < floor
