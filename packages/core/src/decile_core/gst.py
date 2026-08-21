"""GST arithmetic for an Indian tax invoice — docs/11 §"Compliance & legal (India)".

    "GST-compliant invoices with GSTIN, HSN/SAC, place of supply."

Pure Decimal arithmetic, no I/O, so the numbers on the PDF, the numbers in ``payment`` and the
numbers a test asserts are produced by one function.

Two decisions that the bundle does not make, recorded in ``docs/DECISIONS.md``:

**The advertised price is GST-inclusive.** docs/01 §1 gives the prices as "Monthly ₹500 · Yearly
₹3,999 · Forever ₹14,999" with no mention of tax, and that is what the reference product charges
a card. Treating them as exclusive would mean charging ₹590 for a plan the page calls ₹500. So
:func:`split_inclusive` back-computes the taxable value from the gross, and the gross is what
Razorpay is asked for.

**Intra-state supply is CGST+SGST; inter-state is IGST.** Section 8 of the IGST Act: the split
depends on whether the place of supply matches the supplier's state. An unregistered consumer's
place of supply for an online information service is their location (IGST Act §12(2)), which for
a B2C sale we only know if they tell us — so the default is the supplier's own state, and a
customer who supplies a GSTIN or a state gets the correct treatment.

Nothing here is tax advice. A chartered accountant must confirm the rate and the SAC before the
first real invoice is issued; both are settings, not constants, for exactly that reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

__all__ = [
    "DEFAULT_GST_RATE",
    "DEFAULT_SAC_CODE",
    "GstSplit",
    "financial_year",
    "money",
    "split_inclusive",
]

#: docs/04's ``numeric(12,2)`` for every money column. Rounding happens once, here, so the API,
#: the PDF and the CSV cannot disagree (CLAUDE.md house rule 8).
PAISE: Final = Decimal("0.01")

#: 18% is the rate for online information and database access or retrieval services (OIDAR) and
#: for SaaS generally. A setting, not a constant, at the call sites.
DEFAULT_GST_RATE: Final = Decimal("18")

#: SAC 998439 — "Other on-line contents n.e.c.". NOT VERIFIED BY A CHARTERED ACCOUNTANT.
#: Overridable via ``DECILE_GST_SAC_CODE``.
DEFAULT_SAC_CODE: Final = "998439"

#: 1 April. The GST invoice series restarts each Indian financial year.
FINANCIAL_YEAR_START_MONTH: Final = 4


def money(value: Decimal) -> Decimal:
    """Quantise to paise, half-up. docs/04: "Money in numeric, never float"."""
    return value.quantize(PAISE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class GstSplit:
    """One invoice's tax lines. ``taxable + cgst + sgst + igst == gross``, exactly."""

    gross_inr: Decimal
    taxable_inr: Decimal
    cgst_inr: Decimal
    sgst_inr: Decimal
    igst_inr: Decimal
    rate_percent: Decimal
    intra_state: bool

    @property
    def tax_inr(self) -> Decimal:
        return self.cgst_inr + self.sgst_inr + self.igst_inr


def split_inclusive(
    gross_inr: Decimal, *, rate_percent: Decimal = DEFAULT_GST_RATE, intra_state: bool = True
) -> GstSplit:
    """Split a **tax-inclusive** amount into taxable value and tax.

    The taxable value is rounded to paise and the tax is then the *remainder*, rather than being
    rounded independently. Rounding both would let ``taxable + tax`` differ from the amount the
    customer was actually charged by a paisa, and an invoice whose total is not the payment is
    not a valid invoice.
    """
    if gross_inr < 0:
        raise ValueError("an invoice amount cannot be negative")
    if rate_percent < 0:
        raise ValueError("a GST rate cannot be negative")

    gross = money(gross_inr)
    taxable = money(gross * Decimal(100) / (Decimal(100) + rate_percent))
    tax = gross - taxable

    if intra_state:
        # Half to CGST, half to SGST; the odd paisa goes to CGST so the two still sum to `tax`.
        cgst = money(tax / 2)
        sgst = tax - cgst
        igst = Decimal("0.00")
    else:
        cgst = sgst = Decimal("0.00")
        igst = tax

    return GstSplit(
        gross_inr=gross,
        taxable_inr=taxable,
        cgst_inr=cgst,
        sgst_inr=sgst,
        igst_inr=igst,
        rate_percent=rate_percent,
        intra_state=intra_state,
    )


def financial_year(year: int, month: int) -> str:
    """``2026-27`` for any date from 1 Apr 2026 to 31 Mar 2027.

    Rule 46(b) of the CGST Rules requires an invoice number that is unique within a financial
    year, so the year is part of the series rather than a note on it.
    """
    start = year if month >= FINANCIAL_YEAR_START_MONTH else year - 1
    return f"{start}-{(start + 1) % 100:02d}"
