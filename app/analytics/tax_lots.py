"""FIFO tax-lot review for regime sells. ADVISORY ONLY — never a risk override.

WHY LOT-LEVEL, NOT POSITION-LEVEL
A position's broker average price says nothing about which shares would actually be sold.
Selling 100 of 500 shares consumes the OLDEST 100 lots under FIFO, and those may be long-
term while the position as a whole looks recent (or the reverse). So the exact proposed
sell quantity is walked through its FIFO lots; a position-level age would be wrong roughly
whenever it matters.

WHAT A FLAG DOES AND DOES NOT DO
A flag means "this lot is profitable and days away from long-term treatment". It pushes
the lot to the back of the sell queue and raises a manual-review item. It never suppresses
an existing hard guard, never reorders the gateway, and never silently leaves the regime
exposure cap breached — if the cap is otherwise unreachable, the lot is still sold and the
review item records it.

MISSING DATA IS NOT "TAX-SAFE"
A missing acquisition DATE and a missing acquisition PRICE are distinguished and both
produce TAX_DATA_UNKNOWN with manual review. Absence of evidence is never read as
evidence that a sale is safe or that it is near the boundary.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

LONG_TERM_DAYS = 365
DEFAULT_REVIEW_DAYS = 45

# stable reason codes (mirrors core.regime.Reason style)
TAX_DATA_UNKNOWN = "TAX_DATA_UNKNOWN"
TAX_LOT_NEAR_LTCG = "TAX_LOT_NEAR_LTCG"
TAX_LOT_LONG_TERM = "TAX_LOT_LONG_TERM"
TAX_LOT_LOSS = "TAX_LOT_LOSS"
TAX_MISSING_ACQUISITION_DATE = "TAX_MISSING_ACQUISITION_DATE"
TAX_MISSING_ACQUISITION_PRICE = "TAX_MISSING_ACQUISITION_PRICE"
TAX_LOTS_INCOMPLETE = "TAX_LOTS_INCOMPLETE"


@dataclass(frozen=True)
class Lot:
    symbol: str
    quantity: int
    acquired_on: dt.date | None
    price: float | None
    corporate_action_adjusted: bool = False

    @property
    def complete(self) -> bool:
        return self.acquired_on is not None and self.price is not None


@dataclass(frozen=True)
class LotConsumption:
    lot: Lot
    quantity: int
    days_held: int | None
    days_to_ltcg: int | None
    gain: float | None
    long_term: bool | None
    flagged: bool
    codes: tuple[str, ...]


@dataclass(frozen=True)
class TaxReview:
    symbol: str
    requested_qty: int
    covered_qty: int
    consumptions: tuple[LotConsumption, ...]
    flagged: bool
    data_unknown: bool
    estimated_gain: float | None
    flagged_qty: int
    codes: tuple[str, ...]

    @property
    def manual_review(self) -> bool:
        return self.flagged or self.data_unknown

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "requested_qty": self.requested_qty,
                "covered_qty": self.covered_qty, "flagged": self.flagged,
                "flagged_qty": self.flagged_qty, "data_unknown": self.data_unknown,
                "estimated_gain": self.estimated_gain, "codes": list(self.codes)}


# =====================================================================================
# lot sourcing
# =====================================================================================
def open_lots(conn, symbol: str) -> list[Lot]:
    """Open acquisition lots for one symbol, oldest first, from the trades table.

    An open lot is a trade row with no exit. Rows missing entry_ts or entry_price are
    still returned — incomplete, and flagged as such downstream — rather than dropped,
    because dropping them would understate the quantity and look like clean coverage.
    """
    rows = conn.execute(
        "SELECT symbol, qty, entry_ts, entry_price FROM trades "
        "WHERE symbol=? AND exit_ts IS NULL ORDER BY COALESCE(entry_ts, 0)",
        (symbol,)).fetchall()
    out: list[Lot] = []
    for r in rows:
        acquired = None
        if r["entry_ts"] is not None:
            acquired = dt.datetime.fromtimestamp(float(r["entry_ts"])).date()
        out.append(Lot(symbol=symbol, quantity=int(r["qty"] or 0), acquired_on=acquired,
                       price=float(r["entry_price"]) if r["entry_price"] is not None
                       else None))
    return out


def lots_from_rows(symbol: str, rows: Iterable[Mapping]) -> list[Lot]:
    """Build lots from plain dicts — used by tests and by any non-DB caller."""
    out = []
    for r in rows:
        acq = r.get("acquired_on")
        if isinstance(acq, str):
            acq = dt.date.fromisoformat(acq)
        out.append(Lot(symbol=symbol, quantity=int(r.get("quantity", 0)),
                       acquired_on=acq, price=r.get("price"),
                       corporate_action_adjusted=bool(
                           r.get("corporate_action_adjusted", False))))
    return out


# =====================================================================================
# review
# =====================================================================================
def review_sale(symbol: str, qty: int, current_price: float, lots: Sequence[Lot], *,
                as_of: dt.date, review_days: int = DEFAULT_REVIEW_DAYS) -> TaxReview:
    """Walk the exact proposed sell quantity through its FIFO lots."""
    remaining = max(int(qty), 0)
    consumptions: list[LotConsumption] = []
    codes: set[str] = set()
    total_gain = 0.0
    gain_known = True
    flagged_qty = 0

    for lot in sorted(lots, key=lambda l: (l.acquired_on or dt.date.min)):
        if remaining <= 0:
            break
        take = min(remaining, max(lot.quantity, 0))
        if take <= 0:
            continue
        remaining -= take

        lot_codes: set[str] = set()
        days_held = days_to_ltcg = None
        gain = None
        long_term = None
        flagged = False

        if lot.acquired_on is None:
            lot_codes.add(TAX_MISSING_ACQUISITION_DATE)
            lot_codes.add(TAX_DATA_UNKNOWN)
        else:
            days_held = (as_of - lot.acquired_on).days
            days_to_ltcg = LONG_TERM_DAYS - days_held
            long_term = days_held >= LONG_TERM_DAYS

        if lot.price is None:
            lot_codes.add(TAX_MISSING_ACQUISITION_PRICE)
            lot_codes.add(TAX_DATA_UNKNOWN)
            gain_known = False
        else:
            gain = (current_price - lot.price) * take
            total_gain += gain

        if gain is not None and days_to_ltcg is not None:
            if gain <= 0:
                # A loser is never deferred for tax reasons: there is no gain to shelter.
                lot_codes.add(TAX_LOT_LOSS)
            elif long_term:
                lot_codes.add(TAX_LOT_LONG_TERM)
            elif 0 < days_to_ltcg <= review_days:
                lot_codes.add(TAX_LOT_NEAR_LTCG)
                flagged = True
                flagged_qty += take

        codes |= lot_codes
        consumptions.append(LotConsumption(
            lot=lot, quantity=take, days_held=days_held, days_to_ltcg=days_to_ltcg,
            gain=gain, long_term=long_term, flagged=flagged, codes=tuple(sorted(lot_codes))))

    covered = int(qty) - remaining
    if remaining > 0:
        # Not enough recorded history to account for the whole sale.
        codes.add(TAX_LOTS_INCOMPLETE)
        codes.add(TAX_DATA_UNKNOWN)

    return TaxReview(
        symbol=symbol, requested_qty=int(qty), covered_qty=covered,
        consumptions=tuple(consumptions),
        flagged=any(c.flagged for c in consumptions),
        data_unknown=TAX_DATA_UNKNOWN in codes,
        estimated_gain=round(total_gain, 2) if gain_known and consumptions else None,
        flagged_qty=flagged_qty, codes=tuple(sorted(codes)))


def review_sales(conn, proposed: Mapping[str, tuple[int, float]], *, as_of: dt.date,
                 review_days: int = DEFAULT_REVIEW_DAYS) -> dict[str, TaxReview]:
    """Review every proposed sale. `proposed` maps symbol -> (quantity, current_price)."""
    return {sym: review_sale(sym, qty, px, open_lots(conn, sym), as_of=as_of,
                             review_days=review_days)
            for sym, (qty, px) in proposed.items()}


def flagged_symbols(reviews: Mapping[str, TaxReview]) -> list[str]:
    """Symbols whose sale should be deferred when an alternative exists."""
    return sorted(s for s, r in reviews.items() if r.flagged)


def manual_review_items(reviews: Mapping[str, TaxReview]) -> list[dict]:
    out = []
    for sym, r in sorted(reviews.items()):
        if not r.manual_review:
            continue
        item = r.as_dict()
        item["detail"] = [
            {"quantity": c.quantity,
             "acquired_on": c.lot.acquired_on.isoformat() if c.lot.acquired_on else None,
             "days_to_ltcg": c.days_to_ltcg, "gain": c.gain,
             "codes": list(c.codes)}
            for c in r.consumptions]
        out.append(item)
    return out
