"""The GST tax invoice, as a value and as a page — docs/11 §"Compliance & legal (India)".

    "GST-compliant invoices with GSTIN, HSN/SAC, place of supply."

Pure: an :class:`InvoiceDocument` in, PDF bytes out. Everything that touches a database or an
object store is in ``decile_api.invoices``; everything that decides an amount is in
``decile_core.gst``.

Rule 46 of the CGST Rules lists what a tax invoice must carry. The fields below are that list,
minus the ones that do not apply to a B2C supply of an online service: there is no transporter,
no delivery address and no unit of measure for a subscription.

Not tax advice. The supplier's own particulars — legal name, address, GSTIN, state — are
settings, because they belong to whoever is running the service, not to the code.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from decile_core.gst import GstSplit
from decile_core.pdf import A4_HEIGHT, A4_WIDTH, Page, render_pdf

__all__ = ["InvoiceDocument", "Party", "render_invoice_pdf"]

MARGIN: Final = 48.0
IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")

#: Every invoice says this, because docs/11 §Compliance requires the disclaimer in the footer as
#: well as on every analytics surface.
DISCLAIMER: Final = (
    "Decile is not a SEBI-registered investment adviser. Nothing sold here is investment advice."
)

REVERSE_CHARGE_NOTE: Final = "Whether tax is payable on reverse charge basis: No"


@dataclass(frozen=True, slots=True)
class Party:
    """A supplier or a recipient."""

    name: str
    address_lines: tuple[str, ...] = ()
    gstin: str | None = None
    state: str | None = None
    email: str | None = None


@dataclass(frozen=True, slots=True)
class InvoiceDocument:
    """One tax invoice, complete. Every number on the page comes from ``tax``."""

    invoice_number: str
    invoice_date: dt.date
    supplier: Party
    recipient: Party
    #: What was sold, in the customer's words — "Decile Monthly subscription".
    description: str
    #: docs/11: "HSN/SAC". A service, so SAC.
    sac_code: str
    #: docs/11: "place of supply", spelled the way the return expects: "Karnataka (29)".
    place_of_supply: str
    tax: GstSplit
    #: The period the subscription covers, if it is a subscription.
    period_start: dt.date | None = None
    period_end: dt.date | None = None
    #: Razorpay's payment id, so the invoice can be reconciled against the gateway.
    payment_reference: str | None = None
    currency: str = "INR"

    def line_total_inr(self) -> Decimal:
        return self.tax.taxable_inr


def _amount(value: Decimal) -> str:
    return f"{value:,.2f}"


def _period(document: InvoiceDocument) -> str | None:
    if document.period_start is None or document.period_end is None:
        return None
    return f"{document.period_start.isoformat()} to {document.period_end.isoformat()}"


def render_invoice_pdf(document: InvoiceDocument) -> bytes:
    """One A4 page. Deterministic: the creation date is the invoice date, not the clock."""
    page = Page()
    y = _draw_header(page, document)
    y = _draw_parties(page, document, y)
    y = _draw_line_item(page, document, y)
    y = _draw_totals(page, document, y)
    _draw_footer(page, document, y)

    created = dt.datetime.combine(document.invoice_date, dt.time(0, 0), tzinfo=IST)
    return render_pdf([page], title=f"Tax invoice {document.invoice_number}", created=created)


def _right() -> float:
    return A4_WIDTH - MARGIN


def _draw_header(page: Page, document: InvoiceDocument) -> float:
    right = _right()
    y = A4_HEIGHT - MARGIN
    page.text(MARGIN, y, "TAX INVOICE", size=16, bold=True)
    page.text_right(right, y, document.invoice_number, size=12, bold=True)
    y -= 14
    page.text_right(right, y, f"Date: {document.invoice_date.isoformat()}", size=9)
    y -= 12
    page.rule(MARGIN, y, right, width=1.0)
    return y - 20


def _draw_parties(page: Page, document: InvoiceDocument, top: float) -> float:
    """The supplier on the left, the recipient on the right, from the same baseline."""
    right = _right()
    y = top
    page.text(MARGIN, y, "Supplied by", size=9, bold=True)
    y -= 13
    for line in _party_lines(document.supplier):
        page.text(MARGIN, y, line, size=9)
        y -= 11
    supplier_bottom = y

    middle = MARGIN + (right - MARGIN) / 2
    y = top
    page.text(middle, y, "Supplied to", size=9, bold=True)
    y -= 13
    for line in _party_lines(document.recipient):
        page.text(middle, y, line, size=9)
        y -= 11

    y = min(supplier_bottom, y) - 8
    page.text(MARGIN, y, f"Place of supply: {document.place_of_supply}", size=9)
    y -= 11
    page.text(MARGIN, y, REVERSE_CHARGE_NOTE, size=9)
    return y - 18


def _draw_line_item(page: Page, document: InvoiceDocument, top: float) -> float:
    right = _right()
    y = top
    page.rule(MARGIN, y + 12, right)
    page.text(MARGIN, y, "Description", size=9, bold=True)
    page.text(MARGIN + 300, y, "SAC", size=9, bold=True)
    page.text_right(right, y, "Amount", size=9, bold=True)
    y -= 4
    page.rule(MARGIN, y, right)
    y -= 15

    page.text(MARGIN, y, document.description, size=9)
    page.text(MARGIN + 300, y, document.sac_code, size=9)
    page.text_right(right, y, _amount(document.line_total_inr()), size=9)
    y -= 12
    period = _period(document)
    if period is not None:
        page.text(MARGIN + 8, y, f"Period: {period}", size=8)
        y -= 12
    y -= 4
    page.rule(MARGIN, y, right)
    return y - 16


def _draw_totals(page: Page, document: InvoiceDocument, top: float) -> float:
    right = _right()
    y = top
    for label, value in _tax_rows(document):
        page.text_right(right - 90, y, label, size=9)
        page.text_right(right, y, _amount(value), size=9)
        y -= 13

    page.rule(right - 220, y + 6, right)
    y -= 8
    page.text_right(right - 90, y, f"Total ({document.currency})", size=10, bold=True)
    page.text_right(right, y, _amount(document.tax.gross_inr), size=10, bold=True)
    return y - 24


def _draw_footer(page: Page, document: InvoiceDocument, top: float) -> None:
    y = top
    if document.payment_reference is not None:
        page.text(MARGIN, y, f"Payment reference: {document.payment_reference}", size=8)
        y -= 12
    page.text(MARGIN, y, "Paid online. This is a computer-generated invoice.", size=8)
    page.rule(MARGIN, MARGIN + 22, _right())
    page.text(MARGIN, MARGIN + 10, DISCLAIMER, size=7)


def _party_lines(party: Party) -> list[str]:
    lines = [party.name, *party.address_lines]
    if party.state:
        lines.append(party.state)
    if party.email:
        lines.append(party.email)
    lines.append(f"GSTIN: {party.gstin}" if party.gstin else "GSTIN: Unregistered")
    return lines


def _tax_rows(document: InvoiceDocument) -> list[tuple[str, Decimal]]:
    tax = document.tax
    half = tax.rate_percent / 2
    rows: list[tuple[str, Decimal]] = [("Taxable value", tax.taxable_inr)]
    if tax.intra_state:
        rows.append((f"CGST @ {_rate(half)}%", tax.cgst_inr))
        rows.append((f"SGST @ {_rate(half)}%", tax.sgst_inr))
    else:
        rows.append((f"IGST @ {_rate(tax.rate_percent)}%", tax.igst_inr))
    return rows


def _rate(value: Decimal) -> str:
    normalised = value.normalize()
    return f"{normalised:f}"
