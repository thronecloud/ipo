"""Document-extraction orchestrator: end-to-end storage of a filing's text + statutory
financials, idempotent re-extraction (replace, never duplicate), the per-run page budget,
and the downloaded-only / oldest-first selection.

Filings point at real synthetic PDFs on disk; the extracted-text directory is redirected
to tmp so the suite writes nothing into the repo. No network, no LLM.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from db.models import CorporateFiling, ExtractedDocument, ExtractedFinancial
from engine.extract import run as extract_run
from factories import make_stock
from pdf_factories import make_results_pdf, make_text_pdf


def _redirect_extracted_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(extract_run, "EXTRACTED_DIR", str(tmp_path / "extracted"))


def _make_filing(session, stock, path, *, sha, filing_type="results",
                 status="downloaded", fetched_at=None):
    filing = CorporateFiling(
        stock_id=stock.id, symbol=stock.symbol, filing_type=filing_type,
        period="2025-06-30", source_url=f"https://x/{sha}.pdf",
        local_path=str(path), sha256=sha, size_bytes=1000, status=status,
        fetched_at=fetched_at or datetime.now(timezone.utc))
    session.add(filing)
    session.commit()
    return filing


def test_extraction_stores_document_and_financials(db_session, monkeypatch, tmp_path):
    _redirect_extracted_dir(monkeypatch, tmp_path)
    stock = make_stock(db_session, "ATHERENERG")
    path = make_results_pdf(str(tmp_path / "res.pdf"), unit="Rs. in Lakhs")
    filing = _make_filing(db_session, stock, path, sha="deadbeef")

    stats = extract_run.run_extraction(verbose=False)

    assert stats["processed"] == 1
    assert stats["results_parsed"] == 1
    doc = db_session.scalar(select(ExtractedDocument).where(
        ExtractedDocument.filing_id == filing.id))
    assert doc is not None
    assert doc.stock_id == stock.id
    assert doc.pages == 1 and doc.method_summary == {"native": 1}
    # full text was written to disk under the redirected dir, keyed by sha256.
    with open(doc.text_path, encoding="utf-8") as f:
        assert "Revenue from operations" in f.read()

    fins = db_session.scalars(select(ExtractedFinancial).where(
        ExtractedFinancial.filing_id == filing.id)).all()
    by_metric = {f.metric: f.value_cr for f in fins}
    assert by_metric["revenue_from_operations"] == 125.0
    assert all(f.stock_id == stock.id and f.unit_detected == "lakh" for f in fins)


def test_re_extraction_replaces_rows_idempotently(db_session, monkeypatch, tmp_path):
    _redirect_extracted_dir(monkeypatch, tmp_path)
    stock = make_stock(db_session, "ATHERENERG")
    path = make_results_pdf(str(tmp_path / "res.pdf"))
    filing = _make_filing(db_session, stock, path, sha="cafef00d")

    extract_run.run_extraction(verbose=False)
    first_docs = db_session.scalar(select(func.count()).select_from(ExtractedDocument))
    first_fins = db_session.scalar(select(func.count()).select_from(ExtractedFinancial))

    # force re-extraction of the already-extracted filing.
    stats = extract_run.run_extraction(force=True, verbose=False)

    assert stats["processed"] == 1
    assert db_session.scalar(select(func.count()).select_from(ExtractedDocument)) == first_docs
    assert db_session.scalar(select(func.count()).select_from(ExtractedFinancial)) == first_fins
    # still exactly one document row for that filing (replaced, not appended).
    assert db_session.scalar(select(func.count()).select_from(ExtractedDocument).where(
        ExtractedDocument.filing_id == filing.id)) == 1


def test_already_extracted_filing_is_skipped_without_force(db_session, monkeypatch, tmp_path):
    _redirect_extracted_dir(monkeypatch, tmp_path)
    stock = make_stock(db_session, "ATHERENERG")
    path = make_results_pdf(str(tmp_path / "res.pdf"))
    _make_filing(db_session, stock, path, sha="0011")

    extract_run.run_extraction(verbose=False)
    stats = extract_run.run_extraction(verbose=False)      # nothing left to do

    assert stats["planned"] == 0 and stats["processed"] == 0


def test_page_budget_stops_and_leaves_filings_retryable(db_session, monkeypatch, tmp_path):
    _redirect_extracted_dir(monkeypatch, tmp_path)
    stock = make_stock(db_session, "ATHERENERG")
    now = datetime.now(timezone.utc)
    older = make_text_pdf(str(tmp_path / "a.pdf"), ["Annual note"], title="A")
    newer = make_text_pdf(str(tmp_path / "b.pdf"), ["Annual note"], title="B")
    a = _make_filing(db_session, stock, older, sha="aaa", filing_type="other",
                     fetched_at=now - timedelta(hours=2))
    b = _make_filing(db_session, stock, newer, sha="bbb", filing_type="other",
                     fetched_at=now)

    stats = extract_run.run_extraction(page_budget=1, verbose=False)   # room for one page

    assert stats["processed"] == 1
    assert stats["skipped_budget"] == 1
    # oldest-first: A was extracted, B was left with NO row so a later run retries it.
    assert db_session.scalar(select(func.count()).select_from(ExtractedDocument).where(
        ExtractedDocument.filing_id == a.id)) == 1
    assert db_session.scalar(select(func.count()).select_from(ExtractedDocument).where(
        ExtractedDocument.filing_id == b.id)) == 0


def test_non_downloaded_filings_are_ignored(db_session, monkeypatch, tmp_path):
    _redirect_extracted_dir(monkeypatch, tmp_path)
    stock = make_stock(db_session, "ATHERENERG")
    path = make_results_pdf(str(tmp_path / "res.pdf"))
    # a duplicate filing carries no local file — it must never be selected for extraction.
    dup = CorporateFiling(stock_id=stock.id, symbol="ATHERENERG", filing_type="results",
                          source_url="https://x/dup.pdf", local_path=None,
                          sha256="dup", status="duplicate",
                          fetched_at=datetime.now(timezone.utc))
    db_session.add(dup)
    db_session.commit()

    stats = extract_run.run_extraction(verbose=False)

    assert stats["planned"] == 0 and stats["processed"] == 0
