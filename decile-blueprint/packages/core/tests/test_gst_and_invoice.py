"""GST arithmetic and the tax invoice — docs/11 §"Compliance & legal (India)" (Prompt 13 §4).

    "GST-compliant invoices with GSTIN, HSN/SAC, place of supply."

These assert the *rule*, not the current output: that the split of a tax-inclusive amount adds
back up to the amount charged, that the financial year is the one Rule 46(b) means, and that the
rendered document carries every particular Rule 46 requires on its face.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from baskfy_core.gst import (
    DEFAULT_GST_RATE,
    GstSplit,
    financial_year,
    money,
    split_inclusive,
)
from baskfy_core.invoice import InvoiceDocument, Party, render_invoice_pdf
from baskfy_core.pdf import Page, render_pdf, text_width
from baskfy_core.seed_data import PLANS, PlanSeed


class TestSplittingATaxInclusivePrice:
    def test_the_parts_sum_to_the_amount_charged(self) -> None:
        """The invoice total must equal the payment, to the paisa. See docs/DECISIONS.md §13.1."""
        split = split_inclusive(Decimal("500.00"))
        assert split.taxable_inr + split.cgst_inr + split.sgst_inr + split.igst_inr == Decimal(
            "500.00"
        )

    def test_the_monthly_plan_at_eighteen_percent(self) -> None:
        """₹500 inclusive of 18% is ₹423.73 + ₹76.27."""
        split = split_inclusive(Decimal("500.00"))
        assert split.taxable_inr == Decimal("423.73")
        assert split.tax_inr == Decimal("76.27")

    def test_intra_state_splits_into_cgst_and_sgst(self) -> None:
        """Section 8 of the CGST Act: an intra-state supply carries both central and state GST."""
        split = split_inclusive(Decimal("500.00"), intra_state=True)
        assert split.igst_inr == Decimal("0.00")
        assert split.cgst_inr + split.sgst_inr == split.tax_inr
        # The odd paisa goes somewhere rather than disappearing.
        assert abs(split.cgst_inr - split.sgst_inr) <= Decimal("0.01")

    def test_inter_state_is_igst_only(self) -> None:
        """Section 5 of the IGST Act."""
        split = split_inclusive(Decimal("500.00"), intra_state=False)
        assert split.cgst_inr == split.sgst_inr == Decimal("0.00")
        assert split.igst_inr == split.tax_inr

    def test_a_zero_rate_is_all_taxable(self) -> None:
        split = split_inclusive(Decimal("500.00"), rate_percent=Decimal("0"))
        assert split.taxable_inr == Decimal("500.00")
        assert split.tax_inr == Decimal("0.00")

    def test_a_negative_amount_is_refused(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            split_inclusive(Decimal("-1.00"))

    @pytest.mark.parametrize("plan", PLANS, ids=lambda p: p.code)
    def test_every_seeded_price_reconciles(self, plan: PlanSeed) -> None:
        """docs/01 §1's three prices, each of which will be charged to a real card."""
        price = plan.price_inr
        split = split_inclusive(price, rate_percent=DEFAULT_GST_RATE)
        assert split.gross_inr == price
        assert split.taxable_inr + split.tax_inr == price

    @given(
        paise=st.integers(min_value=0, max_value=10_000_000),
        rate=st.sampled_from([Decimal("0"), Decimal("5"), Decimal("12"), Decimal("18")]),
        intra=st.booleans(),
    )
    def test_the_split_always_reconciles(self, paise: int, rate: Decimal, intra: bool) -> None:
        """Property: no amount and no rate can produce an invoice whose total is not the charge."""
        amount = money(Decimal(paise) / 100)
        split = split_inclusive(amount, rate_percent=rate, intra_state=intra)
        assert split.taxable_inr + split.cgst_inr + split.sgst_inr + split.igst_inr == amount


class TestTheFinancialYear:
    """Rule 46(b): the invoice number is unique within a financial year, which starts 1 April."""

    @pytest.mark.parametrize(
        ("year", "month", "expected"),
        [
            (2026, 4, "2026-27"),
            (2026, 12, "2026-27"),
            (2027, 3, "2026-27"),
            (2027, 4, "2027-28"),
            (2026, 1, "2025-26"),
            (2000, 3, "1999-00"),
        ],
    )
    def test_boundaries(self, year: int, month: int, expected: str) -> None:
        assert financial_year(year, month) == expected


class TestThePdfWriter:
    def test_it_produces_a_parseable_header_and_trailer(self) -> None:
        page = Page()
        page.text(72, 700, "hello")
        raw = render_pdf([page], title="t", created=dt.datetime(2026, 8, 21, tzinfo=dt.UTC))
        assert raw.startswith(b"%PDF-1.4\n")
        assert raw.rstrip().endswith(b"%%EOF")
        assert b"/Type /Catalog" in raw
        assert b"startxref" in raw

    def test_the_cross_reference_offsets_point_at_their_objects(self) -> None:
        """A wrong offset is the difference between a PDF and a file with a .pdf extension."""
        page = Page()
        page.text(72, 700, "hello")
        raw = render_pdf([page], title="t", created=dt.datetime(2026, 8, 21, tzinfo=dt.UTC))
        start = raw.index(b"xref\n")
        lines = raw[start:].split(b"\n")
        # lines[0] = "xref", lines[1] = "0 N", lines[2] = the free entry, then one per object.
        for index, entry in enumerate(lines[3:], start=1):
            if not entry.endswith(b" n "):
                break
            offset = int(entry.split(b" ")[0])
            assert raw[offset:].startswith(f"{index} 0 obj".encode())

    def test_it_is_deterministic(self) -> None:
        """docs/06's determinism instinct applied to a document of record."""
        page = Page()
        page.text(72, 700, "hello")
        moment = dt.datetime(2026, 8, 21, tzinfo=dt.UTC)
        assert render_pdf([page], title="t", created=moment) == render_pdf(
            [page], title="t", created=moment
        )

    def test_the_rupee_sign_is_transliterated(self) -> None:
        """U+20B9 postdates WinAnsiEncoding, so it cannot be written as itself."""
        page = Page()
        page.text(72, 700, "₹500.00")
        raw = render_pdf([page], title="t", created=dt.datetime(2026, 8, 21, tzinfo=dt.UTC))
        assert b"INR 500.00" in raw

    def test_parentheses_are_escaped(self) -> None:
        """An unescaped '(' terminates the string operand and corrupts the stream."""
        page = Page()
        page.text(72, 700, "Karnataka (29)")
        raw = render_pdf([page], title="t", created=dt.datetime(2026, 8, 21, tzinfo=dt.UTC))
        assert rb"Karnataka \(29\)" in raw

    def test_courier_is_monospaced(self) -> None:
        assert text_width("abcd", 10) == text_width("....", 10)


SUPPLIER = Party(
    "Decile Analytics Private Limited",
    ("12 MG Road", "Bengaluru 560001"),
    "29AAAAA0000A1Z5",
    "Karnataka",
)


RECIPIENT = Party("Asha Rao", email="asha@example.com")


def _document(
    *,
    recipient: Party = RECIPIENT,
    tax: GstSplit | None = None,
    place_of_supply: str = "Karnataka (29)",
    period_start: dt.date | None = None,
    period_end: dt.date | None = None,
) -> InvoiceDocument:
    return InvoiceDocument(
        invoice_number="DCL/2026-27/000001",
        invoice_date=dt.date(2026, 8, 21),
        supplier=SUPPLIER,
        recipient=recipient,
        description="Decile Monthly subscription",
        sac_code="998439",
        place_of_supply=place_of_supply,
        tax=tax if tax is not None else split_inclusive(Decimal("500.00")),
        period_start=period_start,
        period_end=period_end,
    )


class TestTheRenderedInvoice:
    """docs/11: "GST-compliant invoices with GSTIN, HSN/SAC, place of supply"."""

    @pytest.mark.parametrize(
        "expected",
        [
            b"TAX INVOICE",
            b"DCL/2026-27/000001",
            b"2026-08-21",
            b"29AAAAA0000A1Z5",
            b"998439",
            rb"Karnataka \(29\)",
            b"423.73",
            b"500.00",
            b"reverse charge",
            b"SEBI-registered",
        ],
    )
    def test_the_page_carries_every_required_particular(self, expected: bytes) -> None:
        assert expected in render_invoice_pdf(_document())

    def test_an_unregistered_recipient_is_named_as_such(self) -> None:
        """Rule 46(f): a B2C invoice still states the recipient, and "no GSTIN" is a fact."""
        assert b"GSTIN: Unregistered" in render_invoice_pdf(_document())

    def test_a_registered_recipient_gets_their_gstin_printed(self) -> None:
        raw = render_invoice_pdf(
            _document(recipient=Party("Acme Ltd", gstin="27BBBBB1111B1Z2", state="Maharashtra"))
        )
        assert b"27BBBBB1111B1Z2" in raw

    def test_an_inter_state_invoice_shows_igst_and_not_cgst(self) -> None:
        raw = render_invoice_pdf(
            _document(
                tax=split_inclusive(Decimal("500.00"), intra_state=False),
                place_of_supply="Maharashtra (27)",
            )
        )
        assert b"IGST @ 18%" in raw
        assert b"CGST" not in raw

    def test_the_subscription_period_is_printed_when_there_is_one(self) -> None:
        raw = render_invoice_pdf(
            _document(period_start=dt.date(2026, 8, 21), period_end=dt.date(2026, 9, 21))
        )
        assert b"2026-09-21" in raw

    def test_rendering_is_deterministic(self) -> None:
        assert render_invoice_pdf(_document()) == render_invoice_pdf(_document())
