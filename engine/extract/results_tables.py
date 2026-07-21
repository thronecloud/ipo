"""
Quarterly-results table parser — deterministic, zero LLM.

Indian statutory results filings state a handful of magnitude line items (revenue from
operations, total income, expenses, profit before/after tax, total comprehensive income)
in a table whose header declares the unit — "Rs. in Lakhs", "₹ in Crores", "in Million".
This module locates those pages, reads the unit, and normalizes every value to a canonical
crore figure (`value_cr`), keeping the printed label, the detected unit and the source
period alongside it.

Two extraction strategies run per page and are merged (a metric found by the first wins):
  1. pdfplumber table extraction — the primary path for ruled statutory tables,
  2. a line-based fallback for tables pdfplumber can't rule off (label followed by numbers
     on one text line), which materially lifts the real-world hit rate.

Formats vary wildly across filers; this parser deliberately targets the common statutory
shape and reports what it could not read rather than guessing. Per-share figures (EPS) are
NOT emitted — they are not crore magnitudes and normalizing them would be a unit error.
"""

import re

import pdfplumber

# unit label -> multiplier that converts a value in that unit to crores.
# 1 crore = 100 lakh = 10 million = 10,000 thousand.
_UNIT_TABLE = (
    (re.compile(r"(?:rs\.?|inr|₹|rupees)?\s*(?:in\s*)?(?:lakh|lac)s?", re.I), "lakh", 0.01),
    (re.compile(r"(?:rs\.?|inr|₹|rupees)?\s*(?:in\s*)?(?:crore|cr)s?\b", re.I), "crore", 1.0),
    (re.compile(r"(?:rs\.?|inr|₹|rupees)?\s*(?:in\s*)?(?:million|mn)s?\b", re.I), "million", 0.1),
    (re.compile(r"(?:rs\.?|inr|₹|rupees)?\s*(?:in\s*)?thousands?\b", re.I), "thousand", 0.0001),
)

# canonical metric -> label matcher (matched against a row's / line's leading label).
_METRICS = (
    ("revenue_from_operations", re.compile(r"revenue\s+from\s+operations", re.I)),
    ("total_income", re.compile(r"\btotal\s+income\b", re.I)),
    ("total_expenses", re.compile(r"\btotal\s+expense", re.I)),
    ("profit_before_tax", re.compile(r"profit.{0,20}?before\s+tax", re.I)),
    ("profit_after_tax",
     re.compile(r"profit.{0,20}?(?:after\s+tax|for\s+the\s+period|for\s+the\s+year)", re.I)),
    ("total_comprehensive_income", re.compile(r"total\s+comprehensive\s+income", re.I)),
)

# Statutory-page detector: a page carrying at least this many of these header phrases is a
# financial-results page worth parsing (and non-financial pages are skipped for free).
_HEADER_PHRASES = (
    re.compile(r"revenue\s+from\s+operations", re.I),
    re.compile(r"profit.{0,20}?before\s+tax", re.I),
    re.compile(r"\btotal\s+income\b", re.I),
    re.compile(r"total\s+comprehensive\s+income", re.I),
    re.compile(r"earnings?\s+per\s+(?:equity\s+)?share", re.I),
    re.compile(r"\btotal\s+expense", re.I),
)
_MIN_HEADER_HITS = 2

# Period column headers, in the shapes Indian filings print them.
_PERIOD_RE = re.compile(
    r"(?:\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})"                      # 30.06.2025
    r"|(?:\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{4})"                     # 30 June 2025
    r"|(?:[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})"                     # June 30, 2025
    r"|(?:Q[1-4]\s*[-\s]?(?:FY)?\s*\d{2,4})",                     # Q1 FY2026
    re.I,
)

_NUM_RE = re.compile(r"^\(?-?[\d,]*\.?\d+\)?\*?$")


def detect_unit(text: str) -> tuple[str | None, float | None]:
    """Read the value unit a filing declares. Returns (label, to_crore_multiplier), or
    (None, None) when no unit phrase is present."""
    for pattern, label, mult in _UNIT_TABLE:
        if pattern.search(text or ""):
            return label, mult
    return None, None


def detect_periods(text: str) -> list[str]:
    """Ordered, de-duplicated period column headers found on a page."""
    seen, out = set(), []
    for m in _PERIOD_RE.finditer(text or ""):
        p = re.sub(r"\s+", " ", m.group(0)).strip()
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def match_metric(label: str | None) -> str | None:
    if not label:
        return None
    for metric, pattern in _METRICS:
        if pattern.search(label):
            return metric
    return None


def is_results_page(text: str) -> bool:
    return sum(1 for p in _HEADER_PHRASES if p.search(text or "")) >= _MIN_HEADER_HITS


def parse_number(cell: str | None) -> float | None:
    """A statutory numeric cell to a float. Parentheses mean negative; dashes/blanks and
    non-numeric cells return None."""
    if cell is None:
        return None
    s = str(cell).strip().replace("₹", "").replace("Rs.", "").replace("Rs", "")
    s = s.strip()
    if not _NUM_RE.match(s):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").rstrip("*")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _emit_one(rows: dict, metric: str, raw_label: str, value: float,
              period: str | None, mult: float | None, unit_label: str | None,
              confidence: float | None) -> None:
    """Record one (metric, period) datum. First writer wins, so the pdfplumber table pass
    is never overwritten by the weaker text fallback."""
    key = (metric, period)
    if key in rows:
        return
    rows[key] = {
        "metric": metric,
        "raw_label": raw_label.strip()[:512],
        "period_label": period,
        "value_cr": (value * mult) if mult is not None else None,
        "unit_detected": unit_label,
        "confidence": confidence,
    }


def _table_column_periods(table) -> dict[int, str]:
    """Map a table's value-column index -> period label, read from the header rows only.

    Periods are taken from the table's own header cells (the rows above the first metric
    row), never from surrounding page prose — narrative dates ("as at 31 March", note
    references) would otherwise pollute the labels. Column index is preserved so a value
    in column j is paired with the period printed above column j."""
    periods: dict[int, str] = {}
    for cells in table:
        if cells and match_metric((cells[0] or "").replace("\n", " ")):
            break                          # reached the data rows; headers are done
        for j, cell in enumerate(cells):
            if j == 0 or not cell:
                continue
            m = _PERIOD_RE.search(str(cell).replace("\n", " "))
            if m:
                periods[j] = re.sub(r"\s+", " ", m.group(0)).strip()
    return periods


def _parse_page_tables(page, mult, unit_label, confidence, rows) -> None:
    for table in page.extract_tables() or []:
        col_periods = _table_column_periods(table)
        for cells in table:
            if not cells or not cells[0]:
                continue
            label = cells[0].replace("\n", " ")
            metric = match_metric(label)
            if not metric:
                continue
            for j, cell in enumerate(cells):
                if j == 0:
                    continue
                value = parse_number(cell)
                if value is not None:
                    _emit_one(rows, metric, label, value, col_periods.get(j),
                              mult, unit_label, confidence)


def _parse_page_text(text, periods, mult, unit_label, confidence, rows) -> None:
    for line in (text or "").splitlines():
        metric = match_metric(line)
        if not metric:
            continue
        # label is everything up to the first number; the numbers are the columns, paired
        # to detected periods by position (best-effort — no table structure to lean on).
        nums = re.findall(r"\(?-?[\d,]*\.?\d+\)?", line)
        values = [v for n in nums if (v := parse_number(n)) is not None]
        for j, value in enumerate(values):
            period = periods[j] if j < len(periods) else None
            _emit_one(rows, metric, line, value, period, mult, unit_label, confidence)


def parse_results(pdf_path: str, *, page_indices=None,
                  page_confidence: dict | None = None) -> list[dict]:
    """Parse statutory results line items from a filing.

    `page_indices` restricts parsing to the pages the extraction stage flagged as
    results pages (None = scan every page and self-select). `page_confidence` maps a
    page index to its extraction confidence, carried onto each emitted row.
    """
    page_confidence = page_confidence or {}
    rows: dict = {}
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            if page_indices is not None and i not in page_indices:
                continue
            text = page.extract_text() or ""
            if page_indices is None and not is_results_page(text):
                continue
            unit_label, mult = detect_unit(text)
            confidence = page_confidence.get(i)
            _parse_page_tables(page, mult, unit_label, confidence, rows)
            _parse_page_text(text, detect_periods(text), mult, unit_label,
                             confidence, rows)
    return list(rows.values())
