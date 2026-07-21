"""Statutory results-table parsing: unit detection + normalization to crores, statutory
metric matching via pdfplumber table extraction, parenthesis-negatives, period pairing,
and the text-line fallback. Pure parsing — no database, no network."""

import pytest

from engine.extract import results_tables as rt
from pdf_factories import make_results_pdf, make_text_pdf


# ---------- unit detection + normalization ----------

def test_detect_unit_recognises_the_indian_scales():
    assert rt.detect_unit("Rs. in Lakhs") == ("lakh", 0.01)
    assert rt.detect_unit("(₹ in Crores)") == ("crore", 1.0)
    assert rt.detect_unit("Amounts in Million") == ("million", 0.1)
    assert rt.detect_unit("figures in thousands") == ("thousand", 0.0001)
    assert rt.detect_unit("no unit stated here") == (None, None)


def test_lakh_is_normalized_to_crore_by_dividing_100(tmp_path):
    path = make_results_pdf(str(tmp_path / "r.pdf"), unit="Rs. in Lakhs",
                            rows=[("Revenue from operations", "12,500.00")])
    rows = rt.parse_results(str(path), page_indices=[0])
    rev = next(r for r in rows if r["metric"] == "revenue_from_operations")
    assert rev["value_cr"] == pytest.approx(125.0)      # 12,500 lakh / 100
    assert rev["unit_detected"] == "lakh"


def test_crore_unit_passes_value_through(tmp_path):
    path = make_results_pdf(str(tmp_path / "r.pdf"), unit="₹ in Crores",
                            rows=[("Total income", "845.60")])
    rows = rt.parse_results(str(path), page_indices=[0])
    assert next(r for r in rows if r["metric"] == "total_income")["value_cr"] == pytest.approx(845.60)


def test_unknown_unit_leaves_value_cr_none(tmp_path):
    path = make_results_pdf(str(tmp_path / "r.pdf"), unit="(some unlabelled figures)",
                            rows=[("Profit before tax", "3,000.00")])
    rows = rt.parse_results(str(path), page_indices=[0])
    pbt = next(r for r in rows if r["metric"] == "profit_before_tax")
    assert pbt["value_cr"] is None
    assert pbt["unit_detected"] is None


# ---------- number parsing ----------

def test_parenthesised_value_is_negative():
    assert rt.parse_number("(150.00)") == -150.0
    assert rt.parse_number("12,500.00") == 12500.0
    assert rt.parse_number("—") is None
    assert rt.parse_number("N.A.") is None
    assert rt.parse_number(None) is None


# ---------- full table ----------

def test_parse_extracts_statutory_metrics_and_skips_eps(tmp_path):
    path = make_results_pdf(str(tmp_path / "r.pdf"), unit="Rs. in Lakhs")
    rows = rt.parse_results(str(path))
    got = {r["metric"]: r["value_cr"] for r in rows}
    assert got["revenue_from_operations"] == pytest.approx(125.0)
    assert got["total_income"] == pytest.approx(130.0)
    assert got["total_expenses"] == pytest.approx(100.0)
    assert got["profit_before_tax"] == pytest.approx(30.0)
    assert got["profit_after_tax"] == pytest.approx(22.5)
    assert got["total_comprehensive_income"] == pytest.approx(-1.5)   # (150.00) lakh
    # EPS is a per-share figure, never a crore magnitude — it must not be emitted.
    assert "earnings_per_share" not in got


def test_period_is_paired_to_its_column(tmp_path):
    path = make_results_pdf(
        str(tmp_path / "r.pdf"), unit="Rs. in Lakhs",
        periods=("30.06.2025", "31.03.2025"),
        rows=[("Revenue from operations", "12,500.00", "11,000.00")])
    rows = [r for r in rt.parse_results(str(path), page_indices=[0])
            if r["metric"] == "revenue_from_operations"]
    by_period = {r["period_label"]: r["value_cr"] for r in rows}
    assert by_period["30.06.2025"] == pytest.approx(125.0)
    assert by_period["31.03.2025"] == pytest.approx(110.0)


def test_confidence_is_carried_onto_rows(tmp_path):
    path = make_results_pdf(str(tmp_path / "r.pdf"))
    rows = rt.parse_results(str(path), page_confidence={0: 0.87})
    assert all(r["confidence"] == 0.87 for r in rows)


# ---------- text fallback ----------

def test_text_fallback_parses_a_table_without_ruling_lines(tmp_path):
    # A results statement rendered as plain text lines (no ruled grid) — pdfplumber finds
    # no table, and the line-based fallback must still recover the figures.
    path = make_text_pdf(
        str(tmp_path / "t.pdf"),
        ["Statement of Financial Results (Rs. in Lakhs)",
         "Quarter ended 30.06.2025",
         "Revenue from operations 12,500.00",
         "Total income 13,000.00",
         "Profit before tax 3,000.00",
         "Total comprehensive income 2,200.00"])
    rows = {r["metric"]: r["value_cr"] for r in rt.parse_results(str(path))}
    assert rows["revenue_from_operations"] == pytest.approx(125.0)
    assert rows["profit_before_tax"] == pytest.approx(30.0)
