"""Holdings drift vs broker snapshot - docs/smallcase/04 section 7 (SC4 / leaf 1.3.2).

Compares the intended ledger (``cb_investment_holding``) to a desk broker holdings
snapshot. Shortfalls become a ``DRIFT`` pending-action payload; the fix path emits a
synthetic EXIT lot so realized-PnL stays honest, then re-bases the ledger to broker
qty. Excess (bought more outside Baskfy) is flagged as CUSTOMIZE history - never an
order. Pure values in / values out; no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from baskfy_core.gst import money

__all__ = [
    "CUSTOMIZE_KIND",
    "DRIFT_ACTION_TYPE",
    "BrokerHolding",
    "CustomizeMarker",
    "DriftDelta",
    "DriftDetection",
    "DriftFix",
    "DriftShortfall",
    "LedgerHolding",
    "RebaseResult",
    "SyntheticExit",
    "detect_drift",
    "detect_drift_maps",
    "fix_drift",
    "rebase_holdings_after_drift",
]

DRIFT_ACTION_TYPE: Final = "DRIFT"
CUSTOMIZE_KIND: Final = "CUSTOMIZE"

InstrumentId = int


@dataclass(frozen=True, slots=True)
class LedgerHolding:
    """One row of the intended investment ledger."""

    instrument_id: int
    qty: Decimal
    avg_price: Decimal


@dataclass(frozen=True, slots=True)
class BrokerHolding:
    """One instrument qty from the desk broker holdings snapshot."""

    instrument_id: int
    qty: Decimal


@dataclass(frozen=True, slots=True)
class DriftDelta:
    """Per-instrument gap: positive ``shortfall`` means ledger > broker."""

    instrument_id: int
    ledger_qty: Decimal
    broker_qty: Decimal
    shortfall: Decimal
    excess: Decimal


@dataclass(frozen=True, slots=True)
class DriftDetection:
    """Result of comparing ledger vs broker. ``action_type`` is ``DRIFT`` when any shortfall."""

    deltas: tuple[DriftDelta, ...]
    action_type: Literal["DRIFT"] | None
    customize_instrument_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SyntheticExit:
    """Archived EXIT lot for the shortfall, so realized-PnL math stays honest (04 section 7)."""

    instrument_id: int
    qty: Decimal
    avg_cost: Decimal
    kind: Literal["EXIT"] = "EXIT"

    @property
    def cost_basis(self) -> Decimal:
        return money(self.qty * self.avg_cost)


@dataclass(frozen=True, slots=True)
class DriftFix:
    """Re-based ledger + synthetic exits produced by the fix flow."""

    holdings: tuple[LedgerHolding, ...]
    synthetic_exits: tuple[SyntheticExit, ...]
    cleared_action: bool


@dataclass(frozen=True, slots=True)
class DriftShortfall:
    """Broker is short of intended - mapping-API shortfall row (leaf contract)."""

    instrument_id: InstrumentId
    intended_qty: Decimal
    broker_qty: Decimal
    delta: Decimal  # intended - broker (> 0)


@dataclass(frozen=True, slots=True)
class CustomizeMarker:
    """Excess bought at the broker - CUSTOMIZE-kind history (04 section 7)."""

    instrument_id: InstrumentId
    qty: Decimal
    avg_price: Decimal
    kind: Literal["CUSTOMIZE"] = "CUSTOMIZE"


@dataclass(frozen=True, slots=True)
class RebaseResult:
    """Mapping-API rebase artefacts (leaf contract name)."""

    new_intended: dict[InstrumentId, Decimal]
    synthetic_exits: tuple[SyntheticExit, ...]
    customize_markers: tuple[CustomizeMarker, ...]


def detect_drift(
    ledger: Sequence[LedgerHolding],
    broker: Sequence[BrokerHolding],
) -> DriftDetection:
    """Compare intended holdings to the broker snapshot - 04 section 7.

    Any shortfall (ledger qty > broker qty) yields ``action_type='DRIFT'`` with the
    per-instrument deltas. Excess (broker > ledger) is listed under
    ``customize_instrument_ids`` for CUSTOMIZE-kind history - not a DRIFT action.
    """
    ledger_map: dict[int, LedgerHolding] = {}
    for row in ledger:
        if row.qty < 0 or row.avg_price < 0:
            raise ValueError("ledger qty/avg_price cannot be negative")
        if row.instrument_id in ledger_map:
            raise ValueError(f"duplicate ledger instrument_id {row.instrument_id}")
        ledger_map[row.instrument_id] = row

    broker_map: dict[int, Decimal] = {}
    for row in broker:
        if row.qty < 0:
            raise ValueError("broker qty cannot be negative")
        if row.instrument_id in broker_map:
            raise ValueError(f"duplicate broker instrument_id {row.instrument_id}")
        broker_map[row.instrument_id] = row.qty

    instrument_ids = sorted(set(ledger_map) | set(broker_map))
    deltas: list[DriftDelta] = []
    customize: list[int] = []
    has_shortfall = False

    for instrument_id in instrument_ids:
        led = ledger_map.get(instrument_id)
        ledger_qty = led.qty if led is not None else Decimal("0")
        broker_qty = broker_map.get(instrument_id, Decimal("0"))
        shortfall = ledger_qty - broker_qty
        excess = broker_qty - ledger_qty
        if shortfall < 0:
            shortfall = Decimal("0")
        if excess < 0:
            excess = Decimal("0")
        if shortfall > 0:
            has_shortfall = True
        if excess > 0:
            customize.append(instrument_id)
        if shortfall > 0 or excess > 0:
            deltas.append(
                DriftDelta(
                    instrument_id=instrument_id,
                    ledger_qty=ledger_qty,
                    broker_qty=broker_qty,
                    shortfall=shortfall,
                    excess=excess,
                )
            )

    return DriftDetection(
        deltas=tuple(deltas),
        action_type=DRIFT_ACTION_TYPE if has_shortfall else None,
        customize_instrument_ids=tuple(customize),
    )


def detect_drift_maps(
    intended: Mapping[InstrumentId, Decimal],
    broker: Mapping[InstrumentId, Decimal],
) -> list[DriftShortfall]:
    """Leaf-contract mapping form: shortfalls where broker qty is below intended."""
    shortfalls: list[DriftShortfall] = []
    for instrument_id, intended_qty in sorted(intended.items()):
        if intended_qty < 0:
            raise ValueError("intended qty cannot be negative")
        broker_qty = broker.get(instrument_id, Decimal("0"))
        if broker_qty < 0:
            raise ValueError("broker qty cannot be negative")
        if broker_qty < intended_qty:
            shortfalls.append(
                DriftShortfall(
                    instrument_id=instrument_id,
                    intended_qty=intended_qty,
                    broker_qty=broker_qty,
                    delta=intended_qty - broker_qty,
                )
            )
    return shortfalls


def fix_drift(
    ledger: Sequence[LedgerHolding],
    broker: Sequence[BrokerHolding],
    *,
    mark_prices: Mapping[int, Decimal] | None = None,
) -> DriftFix:
    """Re-base ledger to broker reality; archive shortfalls as synthetic EXIT lots.

    ``mark_prices`` is unused for the EXIT cost basis - 04 section 7 archives the delta
    at the ledger's average cost so realized-PnL stays honest relative to the intended
    book. Excess instruments keep the ledger avg_price and adopt the broker qty
    (CUSTOMIZE).
    """
    del mark_prices  # reserved for a future mark-to-market audit trail
    detection = detect_drift(ledger, broker)
    ledger_map = {row.instrument_id: row for row in ledger}
    broker_map = {row.instrument_id: row.qty for row in broker}

    synthetic: list[SyntheticExit] = []
    rebased: list[LedgerHolding] = []

    instrument_ids = sorted(set(ledger_map) | set(broker_map))
    for instrument_id in instrument_ids:
        led = ledger_map.get(instrument_id)
        broker_qty = broker_map.get(instrument_id, Decimal("0"))
        ledger_qty = led.qty if led is not None else Decimal("0")
        avg_price = led.avg_price if led is not None else Decimal("0")

        shortfall = ledger_qty - broker_qty
        if shortfall > 0:
            synthetic.append(
                SyntheticExit(
                    instrument_id=instrument_id,
                    qty=shortfall,
                    avg_cost=avg_price,
                )
            )

        if broker_qty == 0:
            continue
        rebased.append(
            LedgerHolding(
                instrument_id=instrument_id,
                qty=broker_qty,
                avg_price=money(avg_price) if led is not None else money(Decimal("0")),
            )
        )

    return DriftFix(
        holdings=tuple(rebased),
        synthetic_exits=tuple(synthetic),
        cleared_action=detection.action_type is not None or not detection.deltas,
    )


def rebase_holdings_after_drift(
    intended: Mapping[InstrumentId, Decimal],
    broker: Mapping[InstrumentId, Decimal],
    avg_prices: Mapping[InstrumentId, Decimal],
) -> RebaseResult:
    """Leaf-contract mapping form of the fix flow - 04 section 7.

    Shortfall -> new intended equals broker qty + synthetic EXIT for the delta.
    Excess -> intended rises to broker qty + CUSTOMIZE history marker.
    """
    ledger = [
        LedgerHolding(
            instrument_id=i,
            qty=q,
            avg_price=avg_prices.get(i, Decimal("0")),
        )
        for i, q in sorted(intended.items())
    ]
    # Broker-only names need a zero ledger seed so fix_drift sees the excess.
    for instrument_id in sorted(set(broker) - set(intended)):
        ledger.append(
            LedgerHolding(
                instrument_id=instrument_id,
                qty=Decimal("0"),
                avg_price=avg_prices.get(instrument_id, Decimal("0")),
            )
        )
    broker_rows = [BrokerHolding(instrument_id=i, qty=q) for i, q in sorted(broker.items())]

    # Instruments with intended but missing avg_price on a shortfall must fail loudly.
    for instrument_id, intended_qty in intended.items():
        broker_qty = broker.get(instrument_id, Decimal("0"))
        if broker_qty < intended_qty and instrument_id not in avg_prices:
            raise KeyError(f"avg_price required for shortfall on instrument {instrument_id}")

    fixed = fix_drift(ledger, broker_rows)
    new_intended = {h.instrument_id: h.qty for h in fixed.holdings}

    markers: list[CustomizeMarker] = []
    for instrument_id in sorted(set(intended) | set(broker)):
        intended_qty = intended.get(instrument_id, Decimal("0"))
        broker_qty = broker.get(instrument_id, Decimal("0"))
        if broker_qty > intended_qty:
            markers.append(
                CustomizeMarker(
                    instrument_id=instrument_id,
                    qty=broker_qty - intended_qty,
                    avg_price=avg_prices.get(instrument_id, Decimal("0")),
                )
            )

    return RebaseResult(
        new_intended=new_intended,
        synthetic_exits=fixed.synthetic_exits,
        customize_markers=tuple(markers),
    )
