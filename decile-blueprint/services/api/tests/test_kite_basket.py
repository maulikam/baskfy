"""The Publisher hand-off — `baskfy_api.kite_basket` and `GET /baskets/plan/kite`.

The thing under test is a *refusal to execute* as much as it is a payload. Every assertion here
is either "the basket says what Kite needs" or "the basket cannot carry something it must not".
"""

from __future__ import annotations

import pytest

from baskfy_api.kite_basket import (
    EXCHANGE,
    KITE_BASKET_URL,
    MAX_BASKET_ITEMS,
    ORDER_TYPE,
    PRODUCT,
    VARIETY,
    BasketPayload,
    build_basket,
)

KEY = "publisher-key-abc123"


def symbols(payload: BasketPayload) -> list[str]:
    """The tradingsymbols in order. Typed against the real dataclass rather than `object` with a
    suppression — house rule 3, and the annotation is what makes the assertions below checkable."""
    return [item.tradingsymbol for item in payload.items]


class TestTheBasketKiteReceives:
    def test_it_carries_the_fields_kites_form_requires(self) -> None:
        payload = build_basket([("INFY", "BUY", 10)], api_key=KEY)
        assert payload.configured is True
        assert payload.url == KITE_BASKET_URL
        item = payload.items[0].as_dict()
        assert item == {
            "variety": VARIETY,
            "tradingsymbol": "INFY",
            "exchange": EXCHANGE,
            "transaction_type": "BUY",
            "order_type": ORDER_TYPE,
            "quantity": 10,
            "product": PRODUCT,
            "readonly": False,
        }

    def test_it_is_cnc_and_nothing_else(self) -> None:
        """The desk's non-negotiable #5: CNC-only, with MIS and options behind flags that have no
        meaning in a hand-off the user confirms in their own terminal."""
        payload = build_basket([("INFY", "BUY", 1), ("TCS", "SELL", 2)], api_key=KEY)
        assert {i.product for i in payload.items} == {"CNC"}

    def test_rows_are_editable_in_kite(self) -> None:
        """`readonly` false: the basket is a suggestion, and someone who wants half of it must be
        able to say so before they confirm."""
        payload = build_basket([("INFY", "BUY", 10)], api_key=KEY)
        assert payload.items[0].readonly is False

    def test_sells_survive_as_sells(self) -> None:
        payload = build_basket([("INFY", "SELL", 3)], api_key=KEY)
        assert payload.items[0].transaction_type == "SELL"

    def test_order_is_preserved(self) -> None:
        """The desk orders a plan sells-first; re-sorting it here would change what a user sees
        without changing what the desk decided."""
        given = [("A", "SELL", 1), ("B", "BUY", 2), ("C", "BUY", 3)]
        assert symbols(build_basket(given, api_key=KEY)) == ["A", "B", "C"]


class TestWhatTheUserIsAllowedToTrade:
    """Everything. The plan is a suggestion; the account is theirs.

    An earlier version of this module applied the desk's untouchable-instrument guard, which drops
    SGB*, the GB/GS series and `EXCLUDED_SYMBOLS`. That was a mistake worth a test rather than a
    deletion: non-negotiable #7 protects **one account — the desk owner's** — from a momentum
    strategy selling a sovereign gold bond he holds long-term. Applying his position guard to a
    basket handed to somebody else's Kite session removed their rows for a reason that had nothing
    to do with them (Maulik, 27 Aug 2026; `docs/DECISIONS-MERGE.md` M47).

    These assert the *absence* of that filter, so re-adding it fails here and is argued for again
    rather than reintroduced quietly.
    """

    @pytest.mark.parametrize(
        "symbol",
        ["SGBDE31III", "SGBAUG28", "SGBSEP27VIII"],
        ids=["the desk's own protected symbol", "an SGB by prefix", "another SGB by prefix"],
    )
    def test_a_symbol_the_desk_protects_still_reaches_the_user(self, symbol: str) -> None:
        payload = build_basket([(symbol, "SELL", 5)], api_key=KEY)
        assert symbols(payload) == [symbol]
        assert payload.excluded == ()

    def test_the_desks_own_guard_is_untouched_by_this(self) -> None:
        """The rule still exists and is still absolute — for the account it was written for.

        Asserted here rather than trusted, because "we removed the filter from the hand-off" and
        "we removed the filter" are one careless commit apart.
        """
        from baskfy_execution.guards import (  # noqa: PLC0415
            UntouchableInstrumentError,
            assert_tradeable,
        )

        with pytest.raises(UntouchableInstrumentError):
            assert_tradeable("SGBDE31III")


class TestWhatTheBasketRefusesToCarry:
    """Malformed rows only — never unwelcome ones."""

    def test_every_drop_is_reported(self) -> None:
        """Surfaced, not silent. A nine-row basket for a ten-row plan with no explanation reads as
        a bug in the app, and the user's correct response would be to distrust the whole thing."""
        payload = build_basket(
            [("INFY", "HOLD", 2), ("TCS", "BUY", 0), ("WIPRO", "BUY", 3)], api_key=KEY
        )
        assert symbols(payload) == ["WIPRO"]
        assert set(payload.excluded) == {"INFY", "TCS"}

    def test_a_dropped_row_does_not_take_the_basket_with_it(self) -> None:
        payload = build_basket(
            [("INFY", "BUY", 1), ("TCS", "HOLD", 2), ("WIPRO", "BUY", 3)], api_key=KEY
        )
        assert symbols(payload) == ["INFY", "WIPRO"]

    @pytest.mark.parametrize("side", ["HOLD", "", "buy sell", "SHORT"])
    def test_an_instruction_we_do_not_understand_is_dropped_not_guessed(self, side: str) -> None:
        payload = build_basket([("INFY", side, 5)], api_key=KEY)
        assert payload.items == ()
        assert payload.excluded == ("INFY",)

    @pytest.mark.parametrize("quantity", [0, -1, -100])
    def test_a_non_positive_quantity_is_dropped(self, quantity: int) -> None:
        payload = build_basket([("INFY", "BUY", quantity)], api_key=KEY)
        assert payload.items == ()

    def test_lower_case_sides_are_accepted(self) -> None:
        """The desk writes upper case, but a case difference is not a reason to drop a real
        instruction — that would be strictness bought at the user's expense."""
        payload = build_basket([("INFY", "buy", 1)], api_key=KEY)
        assert payload.items[0].transaction_type == "BUY"


class TestWhenItIsNotConfigured:
    def test_no_key_means_the_hand_off_is_off_not_permissive(self) -> None:
        """An empty key must close the button, never post without one — a form submitted with no
        `api_key` lands the user on an error page inside Kite, which reads as our app breaking."""
        payload = build_basket([("INFY", "BUY", 1)], api_key="")
        assert payload.configured is False

    def test_the_items_are_still_built_so_the_ui_can_explain_itself(self) -> None:
        """`configured` is the switch; the basket is still computed, so a page can say "ten orders,
        hand-off unavailable" rather than showing an empty panel with no reason."""
        payload = build_basket([("INFY", "BUY", 1)], api_key="")
        assert symbols(payload) == ["INFY"]


class TestKitesTenInstrumentLimit:
    """https://kite.trade/docs/connect/v3/publisher/ — "maximum 10".

    Not an edge case. `DEFAULT_SCAN_TOP_N` is 15, so the *ordinary* momentum basket is half again
    the limit, and a rebalance plan carries sells as well as buys. Before this cap existed the
    hand-off would have posted fifteen rows on the normal path, every time.
    """

    def test_a_plan_larger_than_the_limit_is_batched(self) -> None:
        payload = build_basket([(f"SYM{i}", "BUY", 1) for i in range(15)], api_key=KEY)
        assert [len(b) for b in payload.batches] == [10, 5]
        assert all(len(b) <= MAX_BASKET_ITEMS for b in payload.batches)

    def test_batching_loses_nothing_and_keeps_the_order(self) -> None:
        """Truncating would drop names silently; the desk ordered the plan sells-first and the
        second basket has to continue where the first stopped, not restart."""
        given = [(f"SYM{i}", "BUY", 1) for i in range(23)]
        payload = build_basket(given, api_key=KEY)
        flat = [item.tradingsymbol for batch in payload.batches for item in batch]
        assert flat == [symbol for symbol, _, _ in given]

    def test_a_plan_that_fits_is_one_basket(self) -> None:
        """The common case stays a single button."""
        payload = build_basket([(f"SYM{i}", "BUY", 1) for i in range(10)], api_key=KEY)
        assert len(payload.batches) == 1

    def test_an_empty_plan_has_no_batches(self) -> None:
        assert build_basket([], api_key=KEY).batches == ()

    def test_the_limit_is_kites_documented_one(self) -> None:
        assert MAX_BASKET_ITEMS == 10
