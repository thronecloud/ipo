"""
Document-extraction orchestrator — deterministic, zero LLM.

Walks the downloaded corporate_filings that have not yet been extracted, oldest-first,
under a per-run page budget (EXTRACT_PAGE_BUDGET, default 2000). For each filing it:
  1. extracts every page's text native-first with a per-page OCR fallback (engine.extract.pdf),
  2. writes the full text to data/extracted/{sha256}.txt (gitignored),
  3. parses statutory results line items from the results pages (engine.extract.results_tables),
  4. records one extracted_documents row (provenance) and the extracted_financials rows.

Re-extraction is idempotent: a filing's extracted_document and extracted_financials are
deleted and rewritten, never duplicated. The page budget is a soft ceiling — a filing
already begun is finished, but once the budget is spent the remaining filings are left
untouched (no row written) so the next run retries them.

`data/extracted` filenames key on the filing's content sha256, so the same PDF republished
under two URLs shares one text file.
"""

import os
from datetime import datetime, timezone

from sqlalchemy import delete, select

from db.models import CorporateFiling, ExtractedDocument, ExtractedFinancial
from engine.extract.pdf import extract_pdf
from engine.extract.results_tables import is_results_page, parse_results
from engine.repo import job_run

EXTRACTED_DIR = "data/extracted"
# Statutory results tables live in these filing types; annual reports carry the audited
# results too, so both are parsed. Other types get text extraction only.
_PARSE_TYPES = ("results", "annual_report")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


EXTRACT_PAGE_BUDGET = _env_int("EXTRACT_PAGE_BUDGET", 2000)


def _unextracted(session, force: bool, limit):
    """Downloaded filings with a local file, oldest-first. `force` includes filings that
    were already extracted (re-extraction); otherwise they are excluded."""
    q = select(CorporateFiling).where(
        CorporateFiling.status == "downloaded",
        CorporateFiling.local_path.isnot(None),
    )
    if not force:
        already = select(ExtractedDocument.filing_id)
        q = q.where(CorporateFiling.id.notin_(already))
    q = q.order_by(CorporateFiling.fetched_at.asc(), CorporateFiling.id.asc())
    if limit:
        q = q.limit(limit)
    return session.scalars(q).all()


def _text_path(filing: CorporateFiling) -> str:
    key = filing.sha256 or f"filing_{filing.id}"
    return os.path.join(EXTRACTED_DIR, f"{key}.txt")


def extract_filing(session, filing: CorporateFiling) -> dict:
    """Extract one filing and persist it, replacing any prior extraction (idempotent).

    Returns a small per-filing summary. Raises on an unreadable file — the caller counts
    it and moves on.
    """
    doc = extract_pdf(filing.local_path)

    text_path = _text_path(filing)
    os.makedirs(EXTRACTED_DIR, exist_ok=True)
    with open(text_path, "w", encoding="utf-8") as f:
        f.write(doc.full_text)

    financials: list[dict] = []
    if filing.filing_type in _PARSE_TYPES:
        results_pages = [p.index for p in doc.pages if is_results_page(p.text)]
        if results_pages:
            financials = parse_results(
                filing.local_path, page_indices=results_pages,
                page_confidence=doc.page_confidence())

    # Replace: a re-extraction must not duplicate rows.
    session.execute(
        delete(ExtractedFinancial).where(ExtractedFinancial.filing_id == filing.id))
    existing = session.scalar(
        select(ExtractedDocument).where(ExtractedDocument.filing_id == filing.id))
    if existing is not None:
        session.delete(existing)
    session.flush()

    session.add(ExtractedDocument(
        filing_id=filing.id, stock_id=filing.stock_id,
        extracted_at=datetime.now(timezone.utc),
        method_summary=doc.method_summary, pages=doc.page_count,
        pages_ocr=doc.pages_ocr, char_count=doc.char_count,
        confidence=doc.confidence, text_path=text_path))
    for row in financials:
        session.add(ExtractedFinancial(
            filing_id=filing.id, stock_id=filing.stock_id,
            metric=row["metric"], raw_label=row["raw_label"],
            period_label=row["period_label"], value_cr=row["value_cr"],
            unit_detected=row["unit_detected"], confidence=row["confidence"]))

    return {"pages": doc.page_count, "pages_ocr": doc.pages_ocr,
            "financial_rows": len(financials)}


def run_extraction(page_budget=None, limit=None, force=False, verbose=True) -> dict:
    """Extract the backlog of downloaded filings under a per-run page budget."""
    budget = EXTRACT_PAGE_BUDGET if page_budget is None else page_budget
    with job_run("extract_documents", target=f"budget/{budget}") as (session, stats):
        counts = {"planned": 0, "processed": 0, "results_parsed": 0, "financial_rows": 0,
                  "pages": 0, "pages_ocr": 0, "skipped_budget": 0, "error": 0}
        filings = _unextracted(session, force, limit)
        counts["planned"] = len(filings)

        spent = 0
        for filing in filings:
            if spent >= budget:
                counts["skipped_budget"] += 1     # no row — retried next run
                continue
            try:
                result = extract_filing(session, filing)
                session.commit()
            except Exception as e:      # noqa: BLE001 — one bad file never stops the run
                session.rollback()
                counts["error"] += 1
                if verbose:
                    print(f"  extract FAILED {filing.local_path}: {e}")
                continue
            counts["processed"] += 1
            counts["pages"] += result["pages"]
            counts["pages_ocr"] += result["pages_ocr"]
            counts["financial_rows"] += result["financial_rows"]
            if result["financial_rows"]:
                counts["results_parsed"] += 1
            spent += result["pages"]

        stats.update(counts)
        if verbose:
            print(f"  extract: {counts['processed']} filings, {counts['pages']} pages "
                  f"({counts['pages_ocr']} OCR), {counts['financial_rows']} financial rows "
                  f"from {counts['results_parsed']} filings, "
                  f"{counts['skipped_budget']} over budget")
    return stats


def main():
    import argparse

    p = argparse.ArgumentParser(description="Extract text + financials from filings")
    p.add_argument("--page-budget", type=int, default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--force", action="store_true",
                   help="re-extract filings already extracted (idempotent replace)")
    a = p.parse_args()
    print(run_extraction(page_budget=a.page_budget, limit=a.limit, force=a.force))


if __name__ == "__main__":
    main()
