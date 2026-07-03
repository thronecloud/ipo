"""Shared dependencies and small helpers for the API layer."""

from collections.abc import Iterator

from sqlalchemy.orm import Session

from db.base import SessionLocal
from src.personas import PERSONAS

# Fixed council order (spec): Buffett, Munger, Graham, Lynch, Fisher,
# Greenblatt, Marks, Jhunjhunwala, Damani, Kedia.
PERSONA_ORDER: list[str] = [
    "warren_buffett",
    "charlie_munger",
    "benjamin_graham",
    "peter_lynch",
    "philip_fisher",
    "joel_greenblatt",
    "howard_marks",
    "rakesh_jhunjhunwala",
    "radhakishan_damani",
    "vijay_kedia",
]

TOTAL_PERSONAS = len(PERSONA_ORDER)


def get_db() -> Iterator[Session]:
    """Request-scoped DB session wrapping db.base.SessionLocal (read-only usage)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def philosophy_line(system_prompt: str | None) -> str:
    """First sentence of a persona's system_prompt."""
    if not system_prompt:
        return ""
    first = system_prompt.strip().splitlines()[0].strip()
    # Trim to the first sentence if the line runs on.
    dot = first.find(". ")
    return first[: dot + 1] if dot != -1 else first


def persona_meta(slug: str) -> dict:
    p = PERSONAS.get(slug, {})
    return {
        "slug": slug,
        "display_name": p.get("display_name", slug),
        "nationality": p.get("nationality", ""),
        "philosophy": philosophy_line(p.get("system_prompt")),
    }


def personas_payload() -> list[dict]:
    return [persona_meta(slug) for slug in PERSONA_ORDER]


def to_cr(market_cap: float | None) -> float | None:
    """Raw INR market cap -> crore (1 cr = 1e7)."""
    if market_cap is None:
        return None
    return round(market_cap / 1e7, 2)


def ipo_return_pct(issue_price: float | None, current_price: float | None) -> float | None:
    if not issue_price or current_price is None:
        return None
    try:
        return round((current_price - issue_price) / issue_price * 100.0, 2)
    except ZeroDivisionError:
        return None
