"""Kite Publisher basket payloads — the hand-off, and the line it must not cross.

WHAT THIS IS
------------
Zerodha's **Publisher** basket is a plain HTML form POST to ``kite.zerodha.com/connect/basket``
carrying an ``api_key`` and a JSON ``data`` array. The user's browser makes that request; Kite
renders the basket **in the user's own Kite session** and they review and execute it there.

WHAT THIS IS NOT
----------------
An order path. Nothing here talks to a broker, holds a credential, or produces a fill. The desk's
non-negotiable #1 ("orders fire only from ``POST /execute`` with ``confirm=true``") and the two
laws are untouched: ``packages/execution`` is still the only way an order leaves this system, and
this module cannot reach it except to ask a question that can only answer *no* (below).

It is exactly D3 posture B as `baskfy_core.broker_connections` states it: "publish baskets; user
executes in their own broker account after confirm."

WHY THE DESK'S UNTOUCHABLE-INSTRUMENT GUARD IS **NOT** APPLIED HERE
-------------------------------------------------------------------
An earlier version of this module called `baskfy_execution.guards.assert_tradeable`, which drops
SGB*, the GB/GS series and `EXCLUDED_SYMBOLS`. That was wrong, and the reason is worth writing
down because the mistake is an easy one to repeat.

Non-negotiable #7 protects **one account: the desk owner's.** `EXCLUDED_SYMBOLS` is literally a
set containing one sovereign gold bond that the desk's owner holds long-term and does not want a
momentum strategy selling out from under him. It is a personal position guard, expressed as
system policy because the desk *is* one person's system.

Baskfy's users are other people, with their own holdings, their own tax positions and their own
reasons. A basket handed to somebody else's Kite session is theirs to accept, edit or reject —
in their own terminal, over their own account. Filtering it against the desk owner's protected
holdings would silently remove rows for a reason that has nothing to do with the person reading
them (Maulik, 27 Aug 2026; `docs/DECISIONS-MERGE.md` M47).

**The desk's own guard is untouched and still absolute.** `packages/execution` still refuses
those instruments for every order it places, which is the account that rule was written for.
Nothing here can place an order at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

#: Where the browser posts. Not configurable: a settable endpoint for a form that carries an
#: account-scoped basket is a phishing target with a config file.
KITE_BASKET_URL: Final = "https://kite.zerodha.com/connect/basket"

#: docs — the desk's non-negotiable #5: CNC-only. MIS needs ``INTRADAY_ENABLED`` and NFO/BFO needs
#: ``OPTIONS_ENABLED``, neither of which has any meaning in a hand-off the user confirms in Kite.
#: Hard-coded rather than a parameter for the same reason the untouchable list is hard-coded.
PRODUCT: Final = "CNC"
VARIETY: Final = "regular"
EXCHANGE: Final = "NSE"

#: ``MARKET``. A basket the user reviews seconds before submitting does not want our stale
#: reference price pinned into it as a limit — `planned_ref_price` is from the desk's evaluation,
#: which may be hours old, and a limit that no longer crosses is an order that silently does not
#: fill. Kite lets them change the type in front of them, which is the right place for that choice.
ORDER_TYPE: Final = "MARKET"

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True, slots=True)
class BasketItem:
    """One row of Kite's ``data`` array, in Kite's own field names."""

    variety: str
    tradingsymbol: str
    exchange: str
    transaction_type: Side
    order_type: str
    quantity: int
    product: str
    #: Kite renders a `readonly` row but will not let the user change its quantity. False: the
    #: basket is a suggestion, and a user who wants to buy half of it must be able to say so.
    readonly: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "variety": self.variety,
            "tradingsymbol": self.tradingsymbol,
            "exchange": self.exchange,
            "transaction_type": self.transaction_type,
            "order_type": self.order_type,
            "quantity": self.quantity,
            "product": self.product,
            "readonly": self.readonly,
        }


@dataclass(frozen=True, slots=True)
class BasketPayload:
    """What the browser needs to render the form, and what was left out of it."""

    url: str
    api_key: str
    items: tuple[BasketItem, ...]
    #: Symbols dropped as malformed, so the UI can say so rather than quietly shipping a short
    #: basket. A user who sees nine rows for a ten-name plan deserves to know which name is
    #: missing, or they will assume the app lost it.
    excluded: tuple[str, ...]

    @property
    def configured(self) -> bool:
        """False when no publisher key is set: the hand-off is off, not permissive."""
        return bool(self.api_key)


def _normalise_side(side: str) -> Side | None:
    """The desk writes ``BUY``/``SELL``; anything else is not an instruction we understand.

    Spelled out as two comparisons rather than a membership test returning the input: a checker
    cannot narrow ``str`` to the ``Side`` literal through ``in``, so the one-liner only compiled
    with a suppression comment — which house rule 3 forbids, and rightly. The suppression would
    have hidden a real widening if ``Side`` ever gained a member this function did not return.
    """
    upper = side.strip().upper()
    if upper == "BUY":
        return "BUY"
    if upper == "SELL":
        return "SELL"
    return None


def build_basket(
    orders: list[tuple[str, str, int]],
    *,
    api_key: str,
) -> BasketPayload:
    """Turn ``(symbol, side, quantity)`` triples into a Kite Publisher basket.

    **No instrument is filtered on policy grounds.** Whatever the plan contains is offered, and
    the user decides in Kite — see the module docstring for why the desk's untouchable list does
    not travel to other people's accounts.

    Rows are dropped only when they are *malformed*, never when they are merely unwelcome:

    * the side is not BUY or SELL — an unrecognised instruction is not a reason to guess;
    * the quantity is zero or negative — Kite rejects it, and a zero-quantity row in a basket the
      user is about to confirm is noise at the worst possible moment.

    Every drop is reported in :attr:`BasketPayload.excluded` so the interface can account for it.
    """
    items: list[BasketItem] = []
    excluded: list[str] = []

    for symbol, side, quantity in orders:
        transaction_type = _normalise_side(side)
        if transaction_type is None or quantity <= 0:
            excluded.append(symbol)
            continue
        items.append(
            BasketItem(
                variety=VARIETY,
                tradingsymbol=symbol,
                exchange=EXCHANGE,
                transaction_type=transaction_type,
                order_type=ORDER_TYPE,
                quantity=int(quantity),
                product=PRODUCT,
            )
        )

    return BasketPayload(
        url=KITE_BASKET_URL,
        api_key=api_key,
        items=tuple(items),
        excluded=tuple(excluded),
    )
