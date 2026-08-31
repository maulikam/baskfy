"""``client_id = plan_id:symbol`` — minted here, honoured by the gateway.

WHY THIS FILE EXISTS. Non-negotiable #6 ends "``client_id = plan_id:symbol`` so a re-posted
plan cannot double-send". The gateway has always *honoured* that id — ``place()`` keys
``self._sent`` on it and answers ``DUPLICATE`` the second time — but nothing in ``packages/``
ever *made* one. The only place the string was built was the desk, at
``kite-momentum-rebalancer/app/main.py:581``::

    client_id=f"{plan_id}:{o['symbol']}"

an f-string inside a route handler, in the tree that is being retired. Idempotency that
depends on every future caller remembering to format a string the same way is a convention,
not a mechanism: one caller who writes ``plan_id + "-" + symbol`` gets a *different* key for
the same order, the gateway sees no duplicate, and the plan double-sends. So the format is a
function now, with the validation the f-string could not carry.

WHAT THIS IS NOT. A client id is a **local** idempotency key: it is the dictionary key inside
one ``OrderGateway`` process, and it is written to the journal. It is not sent to the broker
and it is not a broker order id. Nothing here places anything.

STOPS SHARE THE ID, AND THAT IS SAFE HERE. A GTT armed for the same plan and symbol mints the
same string as the buy that created it. The gateway keeps a *separate* map for GTTs
(``_gtt_sent``) precisely so the two cannot collide — see its comment. Do not "fix" this by
adding a suffix: the maps are the mechanism, and changing the id would change what an existing
journal reconciles against.
"""

from __future__ import annotations

from typing import Final

__all__ = ["CLIENT_ID_SEPARATOR", "mint_client_id", "parse_client_id"]

#: The one character between the two halves. The desk's f-string, as a constant.
CLIENT_ID_SEPARATOR: Final = ":"


def _reject(part: str, value: str) -> None:
    """Refuse anything that would make two different orders share one key, or one order two.

    The three refusals are not style checks:

    * **empty** — ``":RELIANCE"`` and ``"PLAN1:"`` are keys that collide with every other
      order missing the same half.
    * **contains the separator** — ``"a:b"`` + ``"c"`` and ``"a"`` + ``"b:c"`` mint the *same*
      string. One of the two plans would then be reported ``DUPLICATE`` and silently skipped.
    * **contains whitespace** — ``"PLAN1 "`` and ``"PLAN1"`` are different keys for the same
      plan, so the second presentation double-sends. Stripping it instead would hide the bug
      that produced the padding.
    """
    if not value:
        raise ValueError(f"{part} cannot be empty: a client id needs both halves")
    if CLIENT_ID_SEPARATOR in value:
        raise ValueError(
            f"{part} cannot contain {CLIENT_ID_SEPARATOR!r} ({value!r}): the client id would "
            f"be ambiguous, and two different orders could mint the same key"
        )
    if any(character.isspace() for character in value):
        raise ValueError(
            f"{part} cannot contain whitespace ({value!r}): {value.strip()!r} and {value!r} "
            f"would be two keys for one order, which is how a re-posted plan double-sends"
        )


def mint_client_id(*, plan_id: str, symbol: str) -> str:
    """Build the deterministic per-plan, per-symbol idempotency key.

    Deterministic is the whole property: the same plan re-presented mints the same id, the
    gateway finds it in ``_sent`` and answers ``DUPLICATE`` instead of placing a second order.

    *symbol* is upper-cased because an NSE trading symbol is case-insensitively unique, so
    ``"infy"`` and ``"INFY"`` name one instrument and must not mint two keys. This is safe in
    a way normalising an *order* field would not be: the client id never reaches the broker.
    """
    _reject("plan_id", plan_id)
    _reject("symbol", symbol)
    return f"{plan_id}{CLIENT_ID_SEPARATOR}{symbol.upper()}"


def parse_client_id(client_id: str) -> tuple[str, str]:
    """Split a minted id back into ``(plan_id, symbol)``.

    The half of reconciliation the journal needs: given a line, which plan ordered it. Because
    neither half may contain the separator, exactly one split exists — anything else was not
    minted here and is refused rather than guessed at.
    """
    parts = client_id.split(CLIENT_ID_SEPARATOR)
    expected_parts = 2
    if len(parts) != expected_parts:
        raise ValueError(
            f"{client_id!r} is not a minted client id: expected exactly one "
            f"{CLIENT_ID_SEPARATOR!r} between plan_id and symbol"
        )
    plan_id, symbol = parts
    _reject("plan_id", plan_id)
    _reject("symbol", symbol)
    return plan_id, symbol
