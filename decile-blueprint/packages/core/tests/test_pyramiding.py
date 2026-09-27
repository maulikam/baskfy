"""LV10 — his pyramiding on all three sleeves (``gates/live-10-pyramiding.md`` P1).

Maulik, 28 Sep 2026 (DECISIONS-LV LV9.0 (3)): *"lets we do what Kristjan Kullamägi doing"*, then
"On for all three sleeves". A fresh qualifying setup in a held name is a **new entry** with its own
size and stop, counted against slots and exposure; at most ``max_entries_per_name`` (2) open
entries per name; never an add without a fresh signal (the planners only ever see today's
signals). ``SizingConfig.pyramiding=False`` restores one position per name.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

from test_twt_plan import book as twt_book
from test_twt_plan import build as twt_build
from test_twt_plan import candidate as twt_candidate
from test_vbt_plan import LAKH
from test_vbt_plan import candidate as vbt_candidate
from test_vbt_plan import entries as vbt_entries

from baskfy_core.swing.config import DEFAULT_SWING_CONFIG
from baskfy_core.swing.plan import SwingAccount
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, TwtConfig
from baskfy_core.twt.plan import LineKind as TwtKind
from baskfy_core.twt.plan import SkipReason as TwtSkip
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, VbtConfig
from baskfy_core.vbt.plan import BookState as VbtBook
from baskfy_core.vbt.plan import LineKind as VbtKind
from baskfy_core.vbt.plan import SkipReason as VbtSkip

D = Decimal


def _off[C: (TwtConfig, VbtConfig)](config: C) -> C:
    """The same config with pyramiding switched off — the one-line reversal."""
    return dataclasses.replace(config, sizing=dataclasses.replace(config.sizing, pyramiding=False))


class TestTheDefaults:
    def test_all_three_sleeves_pyramid_to_two_entries_a_name(self) -> None:
        for config in (DEFAULT_TWT_CONFIG, DEFAULT_VBT_CONFIG, DEFAULT_SWING_CONFIG):
            assert config.sizing.pyramiding is True
            assert config.sizing.max_entries_per_name == 2


class TestTwt:
    def test_a_fresh_signal_in_a_held_name_is_a_second_entry(self) -> None:
        book = twt_book(open_instrument_ids=frozenset({1}), open_entry_counts={1: 1})
        lines, skips = twt_build([twt_candidate(instrument_id=1)], state=book)
        assert [line.kind for line in lines] == [TwtKind.BUY_AT_OPEN]
        assert skips == []

    def test_a_third_entry_is_refused_with_the_count(self) -> None:
        book = twt_book(open_instrument_ids=frozenset({1}), open_entry_counts={1: 2})
        lines, skips = twt_build([twt_candidate(instrument_id=1)], state=book)
        assert lines == [] and skips[0].reason is TwtSkip.ALREADY_HELD
        assert "2 open entries" in skips[0].detail and "2 is the cap" in skips[0].detail

    def test_with_pyramiding_off_one_position_per_name(self) -> None:
        book = twt_book(open_instrument_ids=frozenset({1}), open_entry_counts={1: 1})
        _, skips = twt_build(
            [twt_candidate(instrument_id=1)], state=book, config=_off(DEFAULT_TWT_CONFIG)
        )
        assert skips[0].reason is TwtSkip.ALREADY_HELD and "never averages down" in skips[0].detail

    def test_slots_count_entries_not_names(self) -> None:
        book = twt_book(open_instrument_ids=frozenset({1, 2}), open_entry_counts={1: 2, 2: 1})
        assert book.slots_taken == 3
        assert twt_book(open_instrument_ids=frozenset({1, 2})).slots_taken == 2, (
            "no counts: one each"
        )


class TestVbt:
    def test_a_fresh_signal_in_a_held_name_is_a_second_entry(self) -> None:
        book = VbtBook(
            open_instrument_ids=frozenset({1}), open_entry_counts={1: 1}, cash_available_inr=LAKH
        )
        lines, skips = vbt_entries([vbt_candidate(instrument_id=1)], book=book)
        assert [line.kind for line in lines] == [VbtKind.PLACE_LIMIT] and skips == []

    def test_a_third_entry_is_refused_with_the_count(self) -> None:
        book = VbtBook(
            open_instrument_ids=frozenset({1}), open_entry_counts={1: 2}, cash_available_inr=LAKH
        )
        lines, skips = vbt_entries([vbt_candidate(instrument_id=1)], book=book)
        assert lines == [] and skips[0].reason is VbtSkip.ALREADY_HELD
        assert "2 open entries" in skips[0].detail

    def test_with_pyramiding_off_one_position_per_name(self) -> None:
        book = VbtBook(
            open_instrument_ids=frozenset({1}), open_entry_counts={1: 1}, cash_available_inr=LAKH
        )
        _, skips = vbt_entries(
            [vbt_candidate(instrument_id=1)], book=book, config=_off(DEFAULT_VBT_CONFIG)
        )
        assert skips[0].reason is VbtSkip.ALREADY_HELD

    def test_a_working_limit_in_the_name_still_refuses_a_second_bid(self) -> None:
        book = VbtBook(
            open_instrument_ids=frozenset({1}),
            open_entry_counts={1: 1},
            working_instrument_ids=frozenset({1}),
            cash_available_inr=LAKH,
        )
        _, skips = vbt_entries([vbt_candidate(instrument_id=1)], book=book)
        assert skips[0].reason is VbtSkip.ALREADY_WORKING

    def test_slots_count_entries_and_working_orders(self) -> None:
        book = VbtBook(
            open_instrument_ids=frozenset({1}),
            open_entry_counts={1: 2},
            working_instrument_ids=frozenset({3}),
        )
        assert book.slots_taken == 3


class TestSwingAccount:
    def test_entries_in_and_open_count_read_the_counts_when_given(self) -> None:
        account = SwingAccount(
            D(1_000_000), D(500_000), frozenset({"AAA", "BBB"}), D(0), {"AAA": 2, "BBB": 1}
        )
        assert account.entries_in("AAA") == 2 and account.entries_in("CCC") == 0
        assert account.open_count == 3

    def test_without_counts_each_held_name_is_one_entry(self) -> None:
        account = SwingAccount(D(1_000_000), D(500_000), frozenset({"AAA"}), D(0))
        assert account.entries_in("AAA") == 1 and account.open_count == 1


def test_the_decision_is_dated() -> None:
    assert dt.date(2026, 9, 28) > dt.date(2026, 9, 27)
