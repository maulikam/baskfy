"""GTT stops, as values and pure helpers the gateway can enforce (non-negotiable 4).

WHY THIS MODULE EXISTS
`CLAUDE.md` non-negotiable 4 is "every buy gets a GTT stop the same session, vol-scaled 8-12%
via `stop_from_vol()`", and non-negotiable 6 has carried its own exception ever since the desk
was merged:

    "GTT stops go through `kite_client.place_gtt_stop`, which carries its own guard -- the
     gateway has no GTT method yet. M16 owns closing that gap; until it does, 'everything goes
     through the gateway' has one documented exception."

A guard that lives on the broker wrapper is one import away from being bypassed, and the half of
the operation that *removes* a stop had even less: `kite_client.delete_gtt` checked the untouchable
list only when the caller happened to pass a symbol. A GTT that can be cancelled without passing
guards is the same hole as one that can be created without them, pointing the other way.

WHAT WAS PORTED, AND FROM WHERE
The authoritative copy of the desk for order-path *semantics* is the external repo at `1cb5cb5`
(`docs/DESK-SOURCE-RECONCILIATION.md` §4.1). `place_gtt_stop`, `delete_gtt`, `tick_size` and
`to_tick` are **byte-identical between the two desk copies** -- the only post-fork change to
`app/kite_client.py` is the market-protection band on `place_cnc_order` (external) and the
encrypted token store (subtree), neither of which touches the GTT path -- so the port below is
unambiguous.

    kite-momentum-rebalancer/app/kite_client.py:201-215   tick_size()   -> TickSizes
    kite-momentum-rebalancer/app/kite_client.py:217-223   to_tick()     -> to_tick()
    kite-momentum-rebalancer/app/kite_client.py:225-242   delete_gtt()  -> OrderGateway.delete_gtt
    kite-momentum-rebalancer/app/kite_client.py:244-273   place_gtt_stop()
                                                          -> OrderGateway.place_gtt_stop
    kite-momentum-rebalancer/app/analytics/protection.py:114-127  the 8-12% band  -> StopBand
    kite-momentum-rebalancer/app/main.py:665              STOP_OK       -> GTT_PLACED_STATUSES

The status vocabulary is preserved EXACTLY. The desk's `/stops/arm` decides success by matching
`{"GTT_PLACED", "DRY_RUN_GTT"}` and its cancel line by `{"GTT_DELETED", "DRY_RUN_GTT_DELETE"}`
(`app/main.py:665,874`). Those literals were themselves written after a live run reported
"0 armed, 17 failed" while sixteen triggers were sitting at the exchange, because a hand-written
success set had drifted from what the client returned -- and a false failure invites a re-arm,
which is how a position ends up with two stops. Renaming any of them here would recreate that
incident from the other side.

THE ARITHMETIC IS NOT REIMPLEMENTED. The stop level itself is `stop_from_vol()` -- it lives in
`baskfy_core.score` and is called at plan time, by `protection.build_stop_plan`, from prices and
volatilities the gateway has no business fetching. `packages/execution` does not import
`baskfy_core` (see `tenancy.py`), and it does not need to: the gateway's job is to *enforce* what
arrives, not to compute it. What this module adds is the check the desk never had -- that the
trigger it is asked to rest at is actually a stop.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from .risk import RiskManager

__all__ = [
    "DEFAULT_STOP_BAND",
    "DRY_RUN_GTT",
    "DRY_RUN_GTT_DELETE",
    "DRY_RUN_GTT_MODIFY",
    "GTT_DELETED",
    "GTT_DELETED_STATUSES",
    "GTT_DELETE_ERROR",
    "GTT_ERROR",
    "GTT_LIMIT_FRACTION",
    "GTT_MODIFIED",
    "GTT_MODIFIED_STATUSES",
    "GTT_MODIFY_ERROR",
    "GTT_PLACED",
    "GTT_PLACED_STATUSES",
    "ORDER_CANCELLED",
    "ORDER_CANCELLED_STATUSES",
    "ORDER_CANCEL_DRY_RUN",
    "ORDER_CANCEL_ERROR",
    "GttConstants",
    "StopBand",
    "TickSizes",
    "band_finding",
    "drop_pct",
    "gtt_order_leg",
    "gtt_params",
    "kill_switch_reason",
    "refuse_stop",
    "to_tick",
]


class GttConstants(Protocol):
    """The four broker constants a stop trigger is built from.

    The desk read these off the live client rather than hard-coding the wire strings
    (`kite_client.py:262-268`), so a change in the SDK's vocabulary arrives as a change in one
    place. Declared as a Protocol so this module can be read, and type-checked, without
    importing `kiteconnect` -- which `packages/execution` deliberately does not depend on.
    """

    GTT_TYPE_SINGLE: str
    TRANSACTION_TYPE_SELL: str
    ORDER_TYPE_LIMIT: str
    PRODUCT_CNC: str


# --- the status vocabulary ----------------------------------------------------------------
# Verbatim from kite_client.place_gtt_stop / delete_gtt. `app/main.py:665,874` matches on these
# strings; see the module docstring for why they are not "tidied".
GTT_PLACED: Final[str] = "GTT_PLACED"
DRY_RUN_GTT: Final[str] = "DRY_RUN_GTT"
GTT_ERROR: Final[str] = "GTT_ERROR"
GTT_DELETED: Final[str] = "GTT_DELETED"
DRY_RUN_GTT_DELETE: Final[str] = "DRY_RUN_GTT_DELETE"
GTT_DELETE_ERROR: Final[str] = "GTT_DELETE_ERROR"
#: `modify_gtt_quantity` (SW10.5, STANDING-ANSWERS A8): a resting trigger re-sized to the
#: quantity that has actually filled — never a second trigger.
GTT_MODIFIED: Final[str] = "GTT_MODIFIED"
DRY_RUN_GTT_MODIFY: Final[str] = "DRY_RUN_GTT_MODIFY"
GTT_MODIFY_ERROR: Final[str] = "GTT_MODIFY_ERROR"
#: `cancel_order` (SW10.5, A8): an open order's remainder pulled at 10:45. Named constants like
#: the GTT statuses — these are not statuses `place()` returns, so the weekly execution report
#: and the /execute circuit breaker (which read `place()`'s literals) are not their readers.
ORDER_CANCELLED: Final[str] = "ORDER_CANCELLED"
ORDER_CANCEL_DRY_RUN: Final[str] = "ORDER_CANCEL_DRY_RUN"
ORDER_CANCEL_ERROR: Final[str] = "ORDER_CANCEL_ERROR"

#: What counts as "the stop is now resting at the exchange" (or was faithfully simulated).
GTT_PLACED_STATUSES: Final[frozenset[str]] = frozenset({GTT_PLACED, DRY_RUN_GTT})
#: What counts as "the trigger is gone".
GTT_DELETED_STATUSES: Final[frozenset[str]] = frozenset({GTT_DELETED, DRY_RUN_GTT_DELETE})
#: "The GTT now covers the new quantity" — live or simulated.
GTT_MODIFIED_STATUSES: Final[frozenset[str]] = frozenset({GTT_MODIFIED, DRY_RUN_GTT_MODIFY})
#: "The order no longer rests" — live or simulated.
ORDER_CANCELLED_STATUSES: Final[frozenset[str]] = frozenset({ORDER_CANCELLED, ORDER_CANCEL_DRY_RUN})

#: `kite_client.py:260` -- the GTT's own limit sits just under the trigger so it fills on the
#: way down. A GTT fires a LIMIT order; at the trigger price exactly, a falling book walks
#: straight through it and the stop rests unfilled while the position keeps falling.
GTT_LIMIT_FRACTION: Final[float] = 0.995

#: `kite_client.py:212` -- what the desk assumes when the instrument dump does not say.
DEFAULT_TICK: Final[float] = 0.05

#: `protection.py:116,122` -- the desk compares a drop against the band with this slack, so a
#: stop that is exactly at the boundary is inside it rather than a finding.
_BAND_EPSILON: Final[float] = 1e-9


@dataclass(frozen=True, slots=True)
class StopBand:
    """How far below the last price a stop is allowed to rest.

    The desk's `STOP_MIN, STOP_MAX, STOP_VOL_MULT = 0.08, 0.12, 2.2` (`app/config.py:179`,
    identical in both desk copies) is what `stop_from_vol` clamps to, so every trigger the
    desk's own planner produces lands inside these bounds by construction. Injected rather
    than imported because `packages/execution` cannot reach the desk's config module, and
    defaulted to the desk's numbers so a caller that forgets gets the band the live book has
    actually been traded on.

    A deviation is JOURNALLED, NOT REFUSED, and that asymmetry is deliberate. Refusing to
    arm a stop because it is 12.4% below rather than 12.0% leaves a real position naked --
    the desk's own `protection.review()` reports TOO_FAR/TOO_CLOSE as findings for a human
    and never blocks on them (`protection.py:114-127`). The refusals in `refuse_stop` below
    are the ones where acting is worse than not acting.
    """

    min_pct: float = 0.08
    max_pct: float = 0.12

    def __post_init__(self) -> None:
        if not 0.0 < self.min_pct < self.max_pct < 1.0:
            raise ValueError(
                f"stop band is inverted or out of range: {self.min_pct} .. {self.max_pct}"
            )


DEFAULT_STOP_BAND: Final[StopBand] = StopBand()


def to_tick(price: float, tick: float) -> float:
    """Snap to the nearest valid tick. Sub-tick precision is not a price.

    Verbatim from `kite_client.py:217-223`. NSE ticks are not uniform: most names are 0.05 or
    0.10, but a high-priced scrip like OFSS is 1.00, and a price that is not a multiple of its
    tick is rejected outright -- "Trigger price should be a multiple of tick size 1.00" is what
    cost OFSS its stop on the first live arming run.
    """
    if tick <= 0:
        return round(price, 2)
    steps = round(price / tick)
    return round(steps * tick, 2)


class TickSizes:
    """Per-exchange tick sizes, cached for the life of the gateway.

    Ported from `kite_client.tick_size` (`kite_client.py:201-215`), which cached on the broker
    wrapper. The gateway holds the raw broker client, so the cache moves here with it.

    The fetch is a NETWORK CALL and is therefore never made until the instrument has already
    passed the untouchable guard -- see `OrderGateway.place_gtt_stop`, which is the only caller.
    """

    def __init__(self) -> None:
        self._by_exchange: dict[str, dict[str, float]] = {}

    def cached(self, exchange: str) -> bool:
        return exchange in self._by_exchange

    def remember(self, exchange: str, instruments: Iterable[Mapping[str, object]]) -> None:
        """Index one exchange's instrument dump.

        The desk's comprehension was `{i["tradingsymbol"]: float(i.get("tick_size") or 0.05)}`
        over every row. The one difference here is the `if` clause: a row with no tradingsymbol
        raised a KeyError on the desk and took the whole arming batch with it, and a malformed
        row in an instrument dump is not a reason to leave sixteen positions unstopped.
        """
        self._by_exchange[exchange] = {
            str(i["tradingsymbol"]): float(i.get("tick_size") or DEFAULT_TICK)
            for i in instruments
            if i.get("tradingsymbol")
        }

    def get(self, symbol: str, exchange: str) -> float:
        return self._by_exchange.get(exchange, {}).get(symbol, DEFAULT_TICK)


def kill_switch_reason(risk: RiskManager) -> str:
    """The kill switch's own words, or "" when it has not fired.

    `RiskManager.pre_order` refuses everything once killed and increments the day's order
    count as a side effect. Neither is right for a protective stop -- see
    `OrderGateway.place_gtt_stop` for why arming is advisory and cancelling is not -- so the
    state is read rather than consumed.

    The day roll happens inside `pre_order`/`on_pnl`, so a flag set yesterday can still read
    as set until the first of those runs today. That can only refuse a CANCEL, never withhold
    a stop, which is the direction an error here should point.
    """
    if not risk.state.killed:
        return ""
    return f"KILL SWITCH: {'; '.join(risk.state.reasons)}"


def refuse_stop(*, symbol: str, qty: int, trigger: float, last_price: float) -> str:
    """Why this GTT must not be placed at all, or "" when it is a stop.

    These are the cases where placing is worse than refusing, which is a much shorter list
    than "outside the band":

    * A non-positive quantity or trigger is not an instruction, it is a bug arriving at the
      broker. Kite would reject it; there is no reason to spend the round trip finding out.
    * Without a last price the trigger cannot be checked against anything, and an unchecked
      trigger is exactly what this function exists to prevent.
    * A sell trigger at or above the last price is not a stop. It fires on the next tick and
      sells the whole position at the GTT's limit -- a market exit dressed as protection, and
      the one mistake here that loses money immediately rather than eventually. The desk's
      planner cannot produce one (`stop_from_vol` subtracts at least `STOP_MIN`), which is
      precisely why nothing downstream ever checked.
    """
    if qty <= 0:
        return f"{symbol}: GTT quantity must be positive, got {qty}"
    if trigger <= 0:
        return f"{symbol}: GTT trigger must be positive, got {trigger}"
    if last_price <= 0:
        return (
            f"{symbol}: no last price, so the trigger {trigger} cannot be checked against "
            f"anything"
        )
    if trigger >= last_price:
        return (
            f"{symbol}: trigger {trigger} is at or above the last price {last_price} -- that "
            f"is not a stop, it would fire immediately and sell the position"
        )
    return ""


def band_finding(*, trigger: float, last_price: float, band: StopBand) -> str:
    """"too_far" / "too_close" / "" -- the desk's own two findings, same epsilon.

    `protection.py:114-127`. Reported, never refused: see `StopBand`.
    """
    if last_price <= 0:
        return ""
    drop = 1.0 - trigger / last_price
    if drop > band.max_pct + _BAND_EPSILON:
        return "too_far"
    if drop < band.min_pct - _BAND_EPSILON:
        return "too_close"
    return ""


def drop_pct(*, trigger: float, last_price: float) -> float:
    """How far below the last price the trigger sits, in percent, rounded as the desk rounds."""
    if last_price <= 0:
        return 0.0
    return round((1.0 - trigger / last_price) * 100.0, 2)


def gtt_order_leg(
    kc: GttConstants,
    *,
    symbol: str,
    exchange: str,
    qty: int,
    limit: float,
) -> dict[str, object]:
    """The single SELL leg a stop trigger fires.

    Verbatim from `kite_client.py:265-269`: CNC, LIMIT, the held quantity, at a price just
    under the trigger. Taking the broker's own constants off the client rather than hard-coding
    the strings, exactly as the desk did.
    """
    return {
        "exchange": exchange,
        "tradingsymbol": symbol,
        "transaction_type": kc.TRANSACTION_TYPE_SELL,
        "quantity": int(qty),
        "order_type": kc.ORDER_TYPE_LIMIT,
        "product": kc.PRODUCT_CNC,
        "price": limit,
    }


def gtt_params(
    kc: GttConstants,
    *,
    symbol: str,
    exchange: str,
    qty: int,
    trigger: float,
    limit: float,
    last_price: float,
) -> dict[str, object]:
    """Everything `kc.place_gtt` is called with. `kite_client.py:261-269`."""
    orders: Sequence[dict[str, object]] = [
        gtt_order_leg(kc, symbol=symbol, exchange=exchange, qty=qty, limit=limit)
    ]
    return {
        "trigger_type": kc.GTT_TYPE_SINGLE,
        "tradingsymbol": symbol,
        "exchange": exchange,
        "trigger_values": [trigger],
        "last_price": last_price,
        "orders": list(orders),
    }
