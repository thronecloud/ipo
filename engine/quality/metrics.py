"""
Shared data-quality primitives — the single source of truth for quarter counting
and cross-source value normalization.

yfinance and screener store the same facts in different units and shapes:
  - yfinance: absolute numbers, ROE as a fraction, quarter columns as ISO dates.
  - screener: Indian-scale strings ("1,35,942"), Cr for market cap, ROE as a
    percent string, quarter columns as "Jun 2023".
These helpers normalize both onto a common footing so a comparator can check
agreement, and count/gap-detect quarters uniformly. Reused by the audit engine
and the benchmark scripts.
"""

import re

# Fiscal-quarter ordinal within a year (screener uses these month labels).
_MONTHS = {"mar": 1, "jun": 2, "sep": 3, "dec": 4}


# ---------- number parsing ----------

def num(x) -> float | None:
    """Robust float parse tolerant of Indian formatting, ₹, %, commas, blanks."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace(",", "").replace("₹", "").replace("%", "").strip()
    if s in ("", "-", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def cr_to_absolute(x) -> float | None:
    """Screener market cap is in ₹ Crore; yfinance marketCap is absolute (×1e7)."""
    v = num(x)
    return v * 1e7 if v is not None else None


def pct_to_fraction(x) -> float | None:
    """Screener ROE/yield are percent strings ('18.4'); yfinance stores fractions."""
    v = num(x)
    return v / 100.0 if v is not None else None


# ---------- quarter coverage ----------

def periods_of(section) -> list:
    """Period columns of a {metric: {period: value}} statement block."""
    if not section:
        return []
    return list(next(iter(section.values()), {}).keys())


def yf_quarters(quarterly_financials: dict | None) -> list:
    """yfinance quarter-end dates '2025-03-31 …' -> sorted fiscal 'YYYYQn' tokens."""
    out = set()
    for p in periods_of(quarterly_financials or {}):
        m = re.match(r"(\d{4})-(\d{2})", str(p))
        if m:
            yr, mo = int(m.group(1)), int(m.group(2))
            out.add(f"{yr}Q{(mo + 2) // 3}")  # 03->Q1 06->Q2 09->Q3 12->Q4
    return sorted(out)


def scr_quarters(quarterly_results: dict | None) -> list:
    """screener periods 'Jun 2023' -> sorted fiscal 'YYYYQn' tokens."""
    out = set()
    for p in periods_of(quarterly_results or {}):
        m = re.match(r"([A-Za-z]{3})\s+(\d{4})", str(p))
        if m:
            mon, yr = m.group(1).lower(), int(m.group(2))
            if mon in _MONTHS:
                out.add(f"{yr}Q{_MONTHS[mon]}")
    return sorted(out)


def gaps(quarters: list) -> int:
    """Missing quarters inside the observed span (non-contiguity). Accepts 'YYYYQn'."""
    ords = []
    for q in quarters:
        m = re.match(r"(\d{4})Q([1-4])", str(q))
        if m:
            ords.append(int(m.group(1)) * 4 + int(m.group(2)))
    if len(ords) < 2:
        return 0
    ords.sort()
    return (ords[-1] - ords[0] + 1) - len(ords)


def quarter_ordinal(token: str) -> int | None:
    """'2025Q3' -> absolute quarter index (year*4 + q) for comparison."""
    import re
    m = re.match(r"(\d{4})Q([1-4])", str(token))
    return int(m.group(1)) * 4 + int(m.group(2)) if m else None


# Indian quarterly-results reporting deadline (SEBI: 45 days interim / 60 annual)
# plus a buffer, after which a quarter is reliably published everywhere.
REPORTING_LAG_DAYS = 90


def expected_latest_quarter(today, lag_days: int = REPORTING_LAG_DAYS) -> str | None:
    """The most recent fiscal quarter (as 'YYYYQn') whose results should be public
    by `today` — i.e. the latest quarter-end at least `lag_days` in the past.
    Pure date math, no network."""
    from datetime import date, timedelta
    cutoff = today - timedelta(days=lag_days)
    ends = []
    for y in (cutoff.year, cutoff.year - 1):
        ends += [date(y, 3, 31), date(y, 6, 30), date(y, 9, 30), date(y, 12, 31)]
    for d in sorted(ends, reverse=True):
        if d <= cutoff:
            return f"{d.year}Q{(d.month + 2) // 3}"
    return None


def relative_diff(a: float | None, b: float | None) -> float | None:
    """Symmetric relative difference of two values, or None if not comparable."""
    if a is None or b is None:
        return None
    hi = max(abs(a), abs(b))
    if hi == 0:
        return 0.0
    return abs(a - b) / hi
