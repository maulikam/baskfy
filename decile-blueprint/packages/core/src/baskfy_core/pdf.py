"""A minimal, deterministic PDF writer — enough for a GST tax invoice, and nothing more.

docs/02 locks the stack and names **no PDF library**; docs/11 §Compliance requires "GST-compliant
invoices", and PROMPTS.md Prompt 13 §4 requires "PDF generation stored in R2". Rather than add a
dependency the ADR does not list, this module writes the PDF directly. A one-page invoice needs
exactly what PDF 1.4 gives for free: a catalogue, one page, one uncompressed content stream, and
two of the fourteen standard fonts every conforming reader already has. That is ~150 lines and no
supply chain. Recorded in ``docs/DECISIONS.md``.

**Courier, not Helvetica.** Every glyph in Courier is 600/1000 em, so a column of figures can be
right-aligned exactly, with no font-metrics table. Helvetica would need its AFM widths
transcribed, and a transcription error is invented data in a document of record. A designed
invoice is a later, deliberate piece of work.

**Deterministic.** Nothing here reads the clock or a random source. The same invoice renders to
the same bytes forever, which is what makes ``test_invoice_pdf`` an assertion rather than a
smoke test.

The output is uncompressed on purpose: the content stream is legible in a hex dump, so a failure
is debuggable without a PDF toolchain.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Final

__all__ = ["A4_HEIGHT", "A4_WIDTH", "Page", "PdfText", "render_pdf", "text_width"]

#: A4 at 72 dpi, the ISO size every Indian invoice is printed on.
A4_WIDTH: Final = 595.28
A4_HEIGHT: Final = 841.89

#: Courier's single glyph width, in thousandths of an em (Adobe's Courier.afm).
COURIER_WIDTH_PER_1000: Final = 600

FONT_REGULAR: Final = "F1"
FONT_BOLD: Final = "F2"

#: Characters WinAnsiEncoding cannot represent are transliterated rather than dropped. The rupee
#: sign is the one that matters: U+20B9 postdates WinAnsi, so the amount is written "INR 500.00".
_TRANSLITERATIONS: Final[dict[str, str]] = {
    "\u20b9": "INR ",  # RUPEE SIGN — postdates WinAnsiEncoding
    "\u2014": "-",  # EM DASH
    "\u2013": "-",  # EN DASH
    "\u2018": "'",  # LEFT SINGLE QUOTATION MARK
    "\u2019": "'",  # RIGHT SINGLE QUOTATION MARK
    "\u201c": '"',  # LEFT DOUBLE QUOTATION MARK
    "\u201d": '"',  # RIGHT DOUBLE QUOTATION MARK
    "\u00a0": " ",  # NO-BREAK SPACE
}

#: WinAnsiEncoding is Latin-1 plus a few extras; anything above this becomes '?'.
LATIN1_CEILING: Final = 256


def text_width(value: str, size: float) -> float:
    """Exact, because Courier is monospaced."""
    return len(value) * size * COURIER_WIDTH_PER_1000 / 1000


def _sanitise(value: str) -> str:
    for source, replacement in _TRANSLITERATIONS.items():
        value = value.replace(source, replacement)
    # WinAnsiEncoding covers Latin-1 plus a handful of extras; anything outside becomes '?' so a
    # stray character can never produce a PDF a reader refuses to open.
    return "".join(c if ord(c) < LATIN1_CEILING else "?" for c in value)


def _escape(value: str) -> str:
    return _sanitise(value).replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


@dataclass(frozen=True, slots=True)
class PdfText:
    """One run of text, positioned from the bottom-left of the page like PDF's own origin."""

    x: float
    y: float
    value: str
    size: float = 10.0
    bold: bool = False


@dataclass(frozen=True, slots=True)
class PdfLine:
    x1: float
    y1: float
    x2: float
    y2: float
    width: float = 0.5


@dataclass
class Page:
    """One page's drawing operations, in the order they are painted."""

    texts: list[PdfText] = field(default_factory=list)
    lines: list[PdfLine] = field(default_factory=list)

    def text(
        self, x: float, y: float, value: str, *, size: float = 10.0, bold: bool = False
    ) -> None:
        self.texts.append(PdfText(x=x, y=y, value=value, size=size, bold=bold))

    def text_right(
        self, right: float, y: float, value: str, *, size: float = 10.0, bold: bool = False
    ) -> None:
        self.text(right - text_width(value, size), y, value, size=size, bold=bold)

    def rule(self, x1: float, y: float, x2: float, *, width: float = 0.5) -> None:
        self.lines.append(PdfLine(x1=x1, y1=y, x2=x2, y2=y, width=width))

    def _content(self) -> bytes:
        parts: list[str] = []
        for line in self.lines:
            parts.append(
                f"{line.width:.2f} w {line.x1:.2f} {line.y1:.2f} m {line.x2:.2f} {line.y2:.2f} l S"
            )
        for run in self.texts:
            font = FONT_BOLD if run.bold else FONT_REGULAR
            parts.append(
                f"BT /{font} {run.size:.2f} Tf {run.x:.2f} {run.y:.2f} Td "
                f"({_escape(run.value)}) Tj ET"
            )
        return "\n".join(parts).encode("latin-1", errors="replace")


def _pdf_date(moment: dt.datetime) -> str:
    """PDF's own date syntax (PDF 1.7 §7.9.4), e.g. ``D:20260821000000+05'30'``."""
    offset = moment.utcoffset() or dt.timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"D:{moment.strftime('%Y%m%d%H%M%S')}{sign}{hours:02d}'{minutes:02d}'"


def render_pdf(pages: list[Page], *, title: str, created: dt.datetime) -> bytes:
    """Assemble a PDF 1.4 file. ``created`` must be derived from the document, never the clock."""
    if not pages:
        raise ValueError("a PDF needs at least one page")

    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    # Object numbers are allocated in a fixed order so the file is byte-stable.
    catalog_number = add(b"")  # 1, patched below once the pages object number is known
    pages_number = add(b"")  # 2
    font_regular = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"
    )
    font_bold = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier-Bold /Encoding /WinAnsiEncoding >>"
    )
    info_number = add(
        b"<< /Title (" + _escape(title).encode("latin-1") + b") /Producer (Decile) "
        b"/Creator (Decile) /CreationDate (" + _pdf_date(created).encode("latin-1") + b") >>"
    )

    page_numbers: list[int] = []
    for page in pages:
        content = page._content()
        content_number = add(
            b"<< /Length "
            + str(len(content)).encode("ascii")
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        )
        page_numbers.append(
            add(
                f"<< /Type /Page /Parent {pages_number} 0 R "
                f"/MediaBox [0 0 {A4_WIDTH:.2f} {A4_HEIGHT:.2f}] "
                f"/Resources << /Font << /{FONT_REGULAR} {font_regular} 0 R "
                f"/{FONT_BOLD} {font_bold} 0 R >> >> "
                f"/Contents {content_number} 0 R >>".encode("latin-1")
            )
        )

    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    objects[pages_number - 1] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_numbers)} >>".encode("latin-1")
    )
    objects[catalog_number - 1] = f"<< /Type /Catalog /Pages {pages_number} 0 R >>".encode(
        "latin-1"
    )

    out = bytearray(b"%PDF-1.4\n")
    # A binary comment marks the file as binary for tools that sniff the first bytes.
    out += b"%\xe2\xe3\xcf\xd3\n"
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_number} 0 R "
        f"/Info {info_number} 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)
