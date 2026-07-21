"""Synthetic-PDF builders for the extraction tests (imported, not collected).

Everything here is built in-process with PyMuPDF — no network, no fixture binaries.
`make_results_pdf` draws a real ruled statutory results table (pdfplumber extracts it as
a table); `make_scanned_pdf` renders text to an image and lays it on an otherwise
text-less page, so it can only be read by OCR.
"""

import fitz


def make_text_pdf(path, lines, *, title="Document"):
    """A plain native-text page, one string per line."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((40, 50), title, fontsize=12)
    y = 90
    for line in lines:
        page.insert_text((40, y), line, fontsize=10)
        y += 22
    doc.save(path)
    doc.close()
    return path


def make_results_pdf(path, *, unit="Rs. in Lakhs", periods=("30.06.2025",),
                     rows=None):
    """A ruled statutory results table. `rows` is a list of (label, *values) tuples with
    one value per period; defaults to a full statutory set in the declared unit."""
    if rows is None:
        rows = [
            ("Revenue from operations", "12,500.00"),
            ("Total income", "13,000.00"),
            ("Total expenses", "10,000.00"),
            ("Profit before tax", "3,000.00"),
            ("Profit for the period", "2,250.00"),
            ("Total comprehensive income", "(150.00)"),
            ("Earnings per share", "5.20"),
        ]
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((40, 50), f"Statement of Financial Results ({unit})", fontsize=11)
    page.insert_text((40, 72), "Quarter ended " + ", ".join(periods), fontsize=10)

    header = ("Particulars", *periods)
    body = [header, *rows]
    ncol = 1 + len(periods)
    left, right = 40, 555
    col_w = (right - left) / ncol
    xs = [left + i * col_w for i in range(ncol + 1)]
    top, rh = 100, 26
    bottom = top + len(body) * rh

    for i in range(len(body) + 1):
        page.draw_line((left, top + i * rh), (right, top + i * rh))
    for x in xs:
        page.draw_line((x, top), (x, bottom))
    for r, cells in enumerate(body):
        y = top + r * rh + 17
        for c, cell in enumerate(cells):
            page.insert_text((xs[c] + 4, y), str(cell), fontsize=9)

    doc.save(path)
    doc.close()
    return path


def make_scanned_pdf(path, lines, *, zoom=2.0):
    """An image-only page (no text layer) carrying `lines` — readable only via OCR."""
    src = fitz.open()
    sp = src.new_page(width=595, height=842)
    y = 120
    for line in lines:
        sp.insert_text((60, y), line, fontsize=26)
        y += 60
    pix = sp.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    src.close()

    out = fitz.open()
    op = out.new_page(width=595, height=842)
    op.insert_image(fitz.Rect(0, 0, 595, 842), pixmap=pix)
    out.save(path)
    out.close()
    return path
