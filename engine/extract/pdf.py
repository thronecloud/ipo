"""
Layered PDF text extraction — deterministic, OCR-capable, zero LLM.

PyMuPDF (fitz) pulls the native text layer from every page first: fast, exact, and the
right answer for the vast majority of Indian statutory filings, which are native-text
PDFs. A page that yields (almost) no extractable text is a scan; only those pages fall
back to Tesseract OCR, rasterized straight from fitz — no poppler, no intermediate files.

Per-page provenance is recorded so a consumer knows how each page's text was obtained and
how far to trust it:
  - method: "native" (fitz text layer) or "ocr" (tesseract)
  - char_count
  - confidence: native text is taken at face value (1.0); an OCR page carries tesseract's
    own mean word confidence (0..1).

`_ocr_page` and `_tesseract_version` are the only seams that touch Tesseract, so the
native path is fully exercised in CI without the binary and the OCR path is skipped where
it is absent.
"""

import os
from dataclasses import dataclass, field

import fitz  # PyMuPDF

# A page whose native text layer yields fewer than this many non-whitespace characters is
# treated as a scan and sent to OCR. Statutory cover pages are sparse but never this bare.
MIN_NATIVE_CHARS = int(os.environ.get("EXTRACT_MIN_NATIVE_CHARS", 24))
# Rasterization zoom for the OCR fallback — 2x (~144 dpi) is enough for Tesseract on
# printed statutory text without ballooning memory on a large scanned filing.
OCR_ZOOM = float(os.environ.get("EXTRACT_OCR_ZOOM", 2.0))


@dataclass
class PageExtraction:
    index: int          # 0-based page number
    method: str         # "native" | "ocr"
    char_count: int
    confidence: float   # 0..1
    text: str = ""


@dataclass
class DocumentExtraction:
    path: str
    pages: list[PageExtraction] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def pages_ocr(self) -> int:
        return sum(1 for p in self.pages if p.method == "ocr")

    @property
    def method_summary(self) -> dict:
        out: dict[str, int] = {}
        for p in self.pages:
            out[p.method] = out.get(p.method, 0) + 1
        return out

    @property
    def char_count(self) -> int:
        return sum(p.char_count for p in self.pages)

    @property
    def confidence(self) -> float:
        """Mean per-page extraction confidence (0 for an empty document)."""
        if not self.pages:
            return 0.0
        return sum(p.confidence for p in self.pages) / len(self.pages)

    @property
    def full_text(self) -> str:
        return "\n\f\n".join(p.text for p in self.pages)

    def page_confidence(self) -> dict[int, float]:
        return {p.index: p.confidence for p in self.pages}


def _tesseract_version():
    """The installed Tesseract version, or None if the binary/lib is unavailable."""
    try:
        import pytesseract

        return pytesseract.get_tesseract_version()
    except Exception:      # noqa: BLE001 — binary missing or unreadable: OCR simply off
        return None


def ocr_available() -> bool:
    return _tesseract_version() is not None


def _ocr_page(page) -> tuple[str, float]:
    """OCR a single fitz page rasterized in-memory. Returns (text, mean_confidence 0..1).

    The only seam that touches Tesseract, so the rest of the module runs in CI without it.
    """
    import pytesseract
    from PIL import Image
    from pytesseract import Output

    pix = page.get_pixmap(matrix=fitz.Matrix(OCR_ZOOM, OCR_ZOOM))
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    data = pytesseract.image_to_data(img, output_type=Output.DICT)

    words, confs = [], []
    for word, conf in zip(data.get("text", []), data.get("conf", [])):
        if not word or not word.strip():
            continue
        try:
            c = float(conf)
        except (TypeError, ValueError):
            continue
        if c < 0:            # tesseract marks non-text regions -1
            continue
        words.append(word)
        confs.append(c)
    text = " ".join(words)
    confidence = (sum(confs) / len(confs) / 100.0) if confs else 0.0
    return text, confidence


def extract_pdf(path: str, *, ocr: bool = True,
                min_native_chars: int = MIN_NATIVE_CHARS) -> DocumentExtraction:
    """Extract text from every page, native-first with a per-page OCR fallback.

    `ocr=False` disables the fallback entirely (a scan page then stays a sparse native
    page). When OCR is enabled but Tesseract is absent, the same graceful degradation
    applies — the page keeps whatever native text it had and is never lost.
    """
    do_ocr = ocr and ocr_available()
    doc = DocumentExtraction(path=path)
    with fitz.open(path) as pdf:
        for i, page in enumerate(pdf):
            native = page.get_text("text") or ""
            if len(native.strip()) >= min_native_chars or not do_ocr:
                doc.pages.append(PageExtraction(
                    index=i, method="native",
                    char_count=len(native.strip()), confidence=1.0, text=native))
                continue
            text, conf = _ocr_page(page)
            doc.pages.append(PageExtraction(
                index=i, method="ocr",
                char_count=len(text.strip()), confidence=conf, text=text))
    return doc
