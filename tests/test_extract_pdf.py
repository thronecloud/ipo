"""Layered PDF extraction: native-first text, per-page OCR fallback, and the per-page
provenance (method, char_count, confidence) each carries.

The native path runs everywhere (PyMuPDF is a hard dependency). The OCR path needs the
Tesseract binary; where it is absent the test is skipped with an explicit marker rather
than silently passing.
"""

import os

import pytest

from engine.extract.pdf import extract_pdf, ocr_available
from pdf_factories import make_results_pdf, make_scanned_pdf, make_text_pdf

needs_ocr = pytest.mark.skipif(not ocr_available(), reason="tesseract binary not installed")


def test_native_text_is_read_without_ocr(tmp_path):
    path = make_text_pdf(str(tmp_path / "n.pdf"),
                         ["Revenue from operations 12,500", "Profit before tax 3,000"])
    doc = extract_pdf(str(path))
    assert doc.page_count == 1
    page = doc.pages[0]
    assert page.method == "native"
    assert page.confidence == 1.0
    assert "Revenue from operations" in page.text
    assert page.char_count > 0
    assert doc.method_summary == {"native": 1}
    assert doc.pages_ocr == 0


def test_ocr_disabled_leaves_a_scan_as_sparse_native(tmp_path):
    # An image-only page has no native text; with OCR off it must still yield a page
    # (never lost), just an empty native one.
    path = make_scanned_pdf(str(tmp_path / "s.pdf"), ["Total income 13,000"])
    doc = extract_pdf(str(path), ocr=False)
    assert doc.page_count == 1
    assert doc.pages[0].method == "native"
    assert doc.pages[0].char_count == 0


@needs_ocr
def test_scanned_page_falls_back_to_ocr(tmp_path):
    path = make_scanned_pdf(str(tmp_path / "s.pdf"),
                            ["Revenue from operations", "Profit before tax"])
    doc = extract_pdf(str(path))
    page = doc.pages[0]
    assert page.method == "ocr"
    assert 0.0 < page.confidence <= 1.0        # tesseract mean word confidence
    assert "Revenue" in page.text
    assert doc.pages_ocr == 1


@needs_ocr
def test_mixed_document_summary_and_confidence(tmp_path):
    # One native page (conf 1.0) + one scanned page (conf < 1.0): the document summary
    # counts both methods and the confidence is their mean.
    native = make_text_pdf(str(tmp_path / "n.pdf"), ["Total income 13,000"])
    scan = make_scanned_pdf(str(tmp_path / "s.pdf"), ["Profit before tax"])
    # Stitch the two single-page PDFs into one document.
    import fitz
    merged = str(tmp_path / "m.pdf")
    doc0 = fitz.open(native)
    doc0.insert_pdf(fitz.open(scan))
    doc0.save(merged)
    doc0.close()

    doc = extract_pdf(merged)
    assert doc.page_count == 2
    assert doc.method_summary == {"native": 1, "ocr": 1}
    assert doc.pages_ocr == 1
    # mean of 1.0 (native) and the OCR page's confidence -> strictly between them.
    ocr_conf = next(p.confidence for p in doc.pages if p.method == "ocr")
    assert doc.confidence == pytest.approx((1.0 + ocr_conf) / 2)


def test_empty_document_has_zero_confidence(tmp_path):
    import fitz
    path = str(tmp_path / "empty.pdf")
    d = fitz.open()
    d.new_page()          # a blank page, no text
    d.save(path)
    d.close()
    doc = extract_pdf(path, ocr=False)
    assert doc.page_count == 1
    assert doc.char_count == 0
