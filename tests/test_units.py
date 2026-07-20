"""Unit-scale contract for quote fields.

yfinance returns these three fields on three different scales. Every one of
them was being rendered on the wrong scale in the UI. These assertions pin
the canonical scale so a regression fails here rather than on screen.
"""
import pytest

from api.units import normalize_quote


def test_roe_fraction_becomes_percent():
    # yfinance returnOnEquity for IEX is 0.39419997 -> 39.4%
    assert normalize_quote({"roe": 0.39419997})["roe"] == pytest.approx(39.42, abs=0.01)


def test_revenue_growth_fraction_becomes_percent():
    assert normalize_quote({"revenue_growth": 0.185})["revenue_growth"] == pytest.approx(18.5, abs=0.01)


def test_negative_revenue_growth_keeps_sign():
    assert normalize_quote({"revenue_growth": -0.048})["revenue_growth"] == pytest.approx(-4.8, abs=0.01)


def test_debt_to_equity_percent_becomes_ratio():
    # yfinance debtToEquity 637.09 means 6.3709x, not 637x
    assert normalize_quote({"debt_to_equity": 637.09})["debt_to_equity"] == pytest.approx(6.3709, abs=0.0001)


def test_debt_to_equity_near_zero_is_not_mistaken_for_a_ratio():
    # EMMVEE's raw yfinance debtToEquity is 9.745, i.e. 0.09745x — essentially
    # debt-free, not 9.7x leveraged. Single-digit inputs like this are common:
    # 967 of the 3,622 non-null snapshots sit below 10 (the median is 29.786).
    assert normalize_quote({"debt_to_equity": 9.745})["debt_to_equity"] == pytest.approx(0.09745, abs=0.00001)


def test_price_and_pe_pass_through_untouched():
    out = normalize_quote({"current_price": 1234.5, "pe_ratio": 42.0})
    assert out["current_price"] == 1234.5
    assert out["pe_ratio"] == 42.0


def test_none_stays_none_and_is_never_coerced_to_zero():
    out = normalize_quote({"roe": None, "debt_to_equity": None, "revenue_growth": None})
    assert out["roe"] is None
    assert out["debt_to_equity"] is None
    assert out["revenue_growth"] is None


def test_zero_is_preserved_and_distinguishable_from_missing():
    assert normalize_quote({"roe": 0.0})["roe"] == 0.0


def test_zero_is_converted_not_skipped():
    # Guards `if v is not None` against being relaxed to `if v:`. Zero is falsy
    # but present, and 0 * 100 == 0 / 100 == 0, so the arithmetic alone cannot
    # tell the two apart — only the int -> float promotion proves the
    # conversion ran on a falsy value rather than being skipped.
    out = normalize_quote({"roe": 0, "revenue_growth": 0, "debt_to_equity": 0})
    for key in ("roe", "revenue_growth", "debt_to_equity"):
        assert out[key] == 0.0
        assert isinstance(out[key], float), f"{key} was skipped, not converted"


def test_input_dict_is_not_mutated():
    # Callers pass dicts built from ORM rows and keep using them afterwards.
    # If the defensive copy is ever dropped, normalising the same row twice
    # compounds: ROE 0.394 -> 39.4 -> 3942.0.
    raw = {"roe": 0.394, "revenue_growth": 0.185, "debt_to_equity": 637.09}
    normalize_quote(raw)
    assert raw["roe"] == 0.394
    assert raw["revenue_growth"] == 0.185
    assert raw["debt_to_equity"] == 637.09


def test_unknown_keys_are_passed_through():
    assert normalize_quote({"data_quality": "full"})["data_quality"] == "full"
