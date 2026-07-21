"""Transient vs permanent classification for analysis failures.

A permanent failure is a property of this (stock, persona, data_hash) —
malformed output, a contract violation, a bad flag. Retrying cannot help,
so the dead-letter counter should advance.

A transient failure is a property of the WORLD at that moment — quota
exhausted, network reset, timeout. Retrying will help. Counting these
toward the dead letter permanently removes stocks from the council after
a few bad hours, which is exactly what happened in production.
"""
import re

TRANSIENT_PATTERNS = (
    r"usage limit",
    r"rate.?limit",
    r"timeout",
    r"timed out",
    r"connection (reset|refused|closed)",
    r"overloaded",
    r"temporarily unavailable",
    r"5\d\d\b",
)


def is_transient(error: str | None) -> bool:
    # An empty/absent error tells us nothing about permanence. Treat the
    # unknown as transient: over-retrying costs quota, but wrongly
    # dead-lettering costs coverage silently and forever.
    if not error:
        return True
    low = error.lower()
    return any(re.search(p, low) for p in TRANSIENT_PATTERNS)
