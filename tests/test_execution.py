"""Execution-reality primitives: next-open entry, circuit delay, friction, ADV.

These are the pure functions the studies wire in (engine.backtest.execution).
Kept separate from the study integration tests so the arithmetic is pinned in
isolation, with exact synthetic inputs -> exact expected outputs.
"""

from datetime import date, timedelta

from engine.backtest.execution import (
    ADV_FLOOR,
    Bar,
    CIRCUIT_PCT,
    FRICTION_BPS,
    apply_friction,
    friction_multiplier,
    is_circuit_locked,
    is_thin,
    resolve_entry,
    trailing_adv,
)

MON = date(2026, 1, 5)  # a Monday


def _bar(d: date, o, hi, lo, c, v=1000) -> Bar:
    return Bar(d, o, hi, lo, c, v)


def _days(n: int) -> list[date]:
    out, d = [], MON
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# ── next-open entry ───────────────────────────────────────────────

def test_entry_takes_next_open_not_close():
    d = _days(3)
    # info_date is MON (d[0]); the first session strictly after is d[1], entered
    # at its OPEN (105), which differs from that session's close (110).
    series = [_bar(d[0], 100, 101, 99, 100), _bar(d[1], 105, 112, 104, 110),
              _bar(d[2], 111, 113, 110, 112)]
    res = resolve_entry(series, MON)
    assert res.status == "ok"
    assert res.index == 1 and res.price == 105 and res.basis == "open"
    assert res.delay == 0


def test_entry_falls_back_to_close_when_open_missing():
    d = _days(2)
    series = [_bar(d[0], 100, 101, 99, 100), _bar(d[1], None, 112, 104, 110)]
    res = resolve_entry(series, MON)
    assert res.price == 110 and res.basis == "close"


def test_predate_when_no_bar_after_info_date():
    # Every bar is on/before the info date -> nothing to enter.
    d = _days(2)
    series = [_bar(d[0], 100, 101, 99, 100)]
    res = resolve_entry(series, d[0])
    assert res.status == "predate" and res.index is None


# ── circuit detection + delay ─────────────────────────────────────

def test_is_circuit_locked_requires_zero_range_and_big_move():
    # Zero range + a full band from prev close = locked.
    assert is_circuit_locked(_bar(MON, 105, 105, 105, 105), prev_close=100) is True
    # Zero range but a tiny move (flat/no-trade day) is NOT a circuit.
    assert is_circuit_locked(_bar(MON, 100.4, 100.4, 100.4, 100.4), prev_close=100) is False
    # Big move but a real intraday range is an ordinary volatile day, not locked.
    assert is_circuit_locked(_bar(MON, 105, 108, 104, 106), prev_close=100) is False
    # 4.9% threshold boundary: exactly the band counts.
    assert is_circuit_locked(_bar(MON, 104.9, 104.9, 104.9, 104.9), prev_close=100) is True


def test_circuit_locked_entry_delayed_to_first_tradeable():
    d = _days(4)
    # d[1] gaps up to a locked upper circuit (+10%, zero range); d[2] trades.
    series = [
        _bar(d[0], 100, 101, 99, 100),
        _bar(d[1], 110, 110, 110, 110),          # locked band, untradeable
        _bar(d[2], 111, 114, 110, 112),          # first tradeable session
        _bar(d[3], 112, 113, 111, 113),
    ]
    res = resolve_entry(series, MON)
    assert res.status == "ok"
    assert res.index == 2 and res.delay == 1 and res.price == 111


def test_unenterable_when_locked_through_entry_window():
    d = _days(8)
    series = [_bar(d[0], 100, 101, 99, 100)]
    # Five consecutive locked-limit-up sessions == the whole ENTRY_WINDOW.
    px = 100.0
    for i in range(1, 6):
        px = round(px * 1.10, 4)
        series.append(_bar(d[i], px, px, px, px))
    res = resolve_entry(series, MON)
    assert res.status == "unenterable" and res.index is None


# ── friction arithmetic (exact) ───────────────────────────────────

def test_friction_multiplier_and_apply_exact():
    # A clean 10% one-way friction makes the math checkable by hand:
    # mult = 0.9 / 1.1; a flat gross (0%) realizes mult - 1 = -18.1818%.
    mult = friction_multiplier(1000.0)
    assert abs(mult - (0.90 / 1.10)) < 1e-12
    assert abs(apply_friction(0.0, 1000.0) - (mult - 1.0) * 100.0) < 1e-12
    # A +10% gross under 10% friction: 1.10 * (0.9/1.1) - 1 = -10%.
    assert abs(apply_friction(10.0, 1000.0) - (-10.0)) < 1e-9
    assert apply_friction(None, 1000.0) is None


def test_friction_default_is_symmetric_both_legs():
    # Default 85 bps: a flat trade loses ~2f of value (round trip).
    f = FRICTION_BPS / 10_000.0
    assert abs(apply_friction(0.0) - ((1 - f) / (1 + f) - 1) * 100.0) < 1e-12


# ── ADV / thinness ────────────────────────────────────────────────

def test_trailing_adv_is_median_close_times_volume():
    d = _days(3)
    # Values: 100*100=10k, 100*300=30k, 100*200=20k -> median 20k.
    series = [_bar(d[0], 100, 101, 99, 100, 100),
              _bar(d[1], 100, 101, 99, 100, 300),
              _bar(d[2], 100, 101, 99, 100, 200)]
    assert trailing_adv(series, 2) == 20_000.0


def test_thinness_floor_and_unknown_adv():
    assert is_thin(ADV_FLOOR - 1) is True
    assert is_thin(ADV_FLOOR) is False        # at the floor is not below it
    assert is_thin(None) is False             # unknown liquidity is not thin
