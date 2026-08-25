"""The trending ranking domain — SC9's "computable rankings only, no fake most invested".

These assert the *spec* (CLAUDE.md house rule 2), which for a ranking layer is four claims:

1. Every list is defined once and says what it ranks.
2. A metric that is absent excludes a basket; it never ranks it last.
3. A list that cannot be computed withholds itself with a named reason.
4. The same catalog always produces the same page.

The single-tenant case at the bottom is the one the product actually runs in today, and it is
asserted positively rather than left as an assumption.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import fields
from decimal import Decimal
from typing import get_args

import pytest

from baskfy_core.curated_trending import (
    LIST_LENGTH,
    MIN_ENTRIES,
    MIN_POPULATION,
    RETURN_RANKED_KEYS,
    TRENDING_LISTS,
    TrendingCandidate,
    TrendingList,
    TrendingListKey,
    TrendingPopulation,
    definition_for,
    rank_lists,
)

#: A population comfortably above the floor, for the tests that are not about the floor.
CROWDED = TrendingPopulation(watchers=40, investors=30, contributors=25)


def _lists(
    candidates: list[TrendingCandidate],
    population: TrendingPopulation = CROWDED,
) -> dict[str, TrendingList]:
    return {row.key: row for row in rank_lists(candidates, population=population)}


def _basket(  # noqa: PLR0913 - one keyword per metric a list can rank on
    slug: str,
    *,
    ret_1m: Decimal | None = None,
    ret_1y: Decimal | None = None,
    cagr_5y: Decimal | None = None,
    min_amount: Decimal | None = None,
    launched_at: dt.date | None = None,
    last_rebalanced_on: dt.date | None = None,
    watchers: int | None = None,
    investors: int | None = None,
    inflow_amount: Decimal | None = None,
) -> TrendingCandidate:
    """One candidate, named after its slug.

    Spelled out rather than ``**kwargs`` because house rule 3 forbids the ``type: ignore`` that
    unpacking a heterogeneous mapping into this dataclass would need. Every metric is optional
    by design: absent means "not known", which is what the ranking layer excludes on.
    """
    return TrendingCandidate(
        slug=slug,
        name=slug.replace("-", " ").title(),
        ret_1m=ret_1m,
        ret_1y=ret_1y,
        cagr_5y=cagr_5y,
        min_amount=min_amount,
        launched_at=launched_at,
        last_rebalanced_on=last_rebalanced_on,
        watchers=watchers,
        investors=investors,
        inflow_amount=inflow_amount,
    )


# --- 1. the definitions ------------------------------------------------------------------


def test_every_list_is_defined_exactly_once() -> None:
    keys = [definition.key for definition in TRENDING_LISTS]
    assert len(keys) == len(set(keys)), keys
    assert len(keys) == 9


def test_the_key_literal_and_the_definitions_cannot_drift() -> None:
    """A key in the type but not in the tuple is a list nobody built, and vice versa."""
    assert set(get_args(TrendingListKey)) == {definition.key for definition in TRENDING_LISTS}


def test_exactly_the_people_metrics_are_population_based() -> None:
    population_based = {d.key for d in TRENDING_LISTS if d.population_based}
    assert population_based == {"MOST_WATCHED", "MOST_INVESTED", "MOST_INFLOWS"}


def test_every_definition_ranks_a_field_that_exists() -> None:
    candidate_fields = {field.name for field in fields(TrendingCandidate)}
    population_fields = {field.name for field in fields(TrendingPopulation)}
    for definition in TRENDING_LISTS:
        assert definition.field in candidate_fields, definition.key
        if definition.population_based:
            assert definition.population_field in population_fields, definition.key
        else:
            assert definition.population_field == "", definition.key


def test_every_list_states_what_it_actually_ranks() -> None:
    """`ranks_by` is the contract with the reader — SC9 AC, "labeled with what it ranks"."""
    for definition in TRENDING_LISTS:
        assert definition.ranks_by.strip().endswith("."), definition.key
        assert "Ranked by" in definition.ranks_by, definition.key
        assert definition.metric_label.strip()


def test_definition_for_raises_on_a_list_that_does_not_exist() -> None:
    with pytest.raises(KeyError):
        definition_for("MOST_LOVED")


def test_the_return_ranked_lists_are_exactly_the_percent_ones() -> None:
    assert {"TOP_1M", "TOP_1Y", "TOP_CAGR_5Y"} == RETURN_RANKED_KEYS
    for row in rank_lists([], population=CROWDED):
        assert row.price_return_caveat is (row.key in RETURN_RANKED_KEYS)


# --- 2. absent is excluded, not last -----------------------------------------------------


def test_a_basket_without_the_metric_is_excluded_not_ranked_last() -> None:
    ranked = _lists(
        [
            _basket("alpha", ret_1y=Decimal("12.00")),
            _basket("bravo", ret_1y=Decimal("8.00")),
            _basket("charlie", ret_1y=Decimal("4.00")),
            _basket("delta"),  # too young for a 1Y number
        ]
    )["TOP_1Y"]
    assert [entry.slug for entry in ranked.entries] == ["alpha", "bravo", "charlie"]
    assert ranked.eligible == 3


def test_a_zero_count_is_not_the_last_of_the_most_watched() -> None:
    ranked = _lists(
        [
            _basket("alpha", watchers=9),
            _basket("bravo", watchers=4),
            _basket("charlie", watchers=2),
            _basket("delta", watchers=0),
        ]
    )["MOST_WATCHED"]
    assert [entry.slug for entry in ranked.entries] == ["alpha", "bravo", "charlie"]


def test_a_zero_minimum_is_an_uncomputed_metric_not_a_cheap_basket() -> None:
    ranked = _lists(
        [
            _basket("alpha", min_amount=Decimal("0")),
            _basket("bravo", min_amount=Decimal("4500")),
            _basket("charlie", min_amount=Decimal("2500")),
            _basket("delta", min_amount=Decimal("9000")),
        ]
    )["BUDGET_FRIENDLY"]
    assert [entry.slug for entry in ranked.entries] == ["charlie", "bravo", "delta"]


# --- 3. direction, ties, caps ------------------------------------------------------------


def test_cost_ranks_ascending_and_returns_rank_descending() -> None:
    catalog = [
        _basket("alpha", min_amount=Decimal("9000"), ret_1m=Decimal("1.00")),
        _basket("bravo", min_amount=Decimal("1000"), ret_1m=Decimal("3.00")),
        _basket("charlie", min_amount=Decimal("5000"), ret_1m=Decimal("2.00")),
    ]
    ranked = _lists(catalog)
    assert [e.slug for e in ranked["BUDGET_FRIENDLY"].entries] == ["bravo", "charlie", "alpha"]
    assert [e.slug for e in ranked["TOP_1M"].entries] == ["bravo", "charlie", "alpha"]


def test_dates_rank_newest_first() -> None:
    ranked = _lists(
        [
            _basket("alpha", launched_at=dt.date(2021, 1, 1)),
            _basket("bravo", launched_at=dt.date(2026, 6, 1)),
            _basket("charlie", launched_at=dt.date(2024, 3, 1)),
        ]
    )["RECENTLY_LAUNCHED"]
    assert [entry.slug for entry in ranked.entries] == ["bravo", "charlie", "alpha"]


def test_ties_break_on_slug_ascending_in_both_directions() -> None:
    tied_desc = _lists(
        [
            _basket("zulu", ret_1y=Decimal("5.00")),
            _basket("alpha", ret_1y=Decimal("5.00")),
            _basket("mike", ret_1y=Decimal("5.00")),
        ]
    )["TOP_1Y"]
    assert [entry.slug for entry in tied_desc.entries] == ["alpha", "mike", "zulu"]

    tied_asc = _lists(
        [
            _basket("zulu", min_amount=Decimal("5000")),
            _basket("alpha", min_amount=Decimal("5000")),
            _basket("mike", min_amount=Decimal("5000")),
        ]
    )["BUDGET_FRIENDLY"]
    assert [entry.slug for entry in tied_asc.entries] == ["alpha", "mike", "zulu"]


def test_a_list_is_capped_but_reports_how_many_were_eligible() -> None:
    catalog = [_basket(f"basket-{index:02d}", ret_1y=Decimal(index)) for index in range(1, 13)]
    ranked = _lists(catalog)["TOP_1Y"]
    assert len(ranked.entries) == LIST_LENGTH
    assert ranked.eligible == 12
    assert [entry.rank for entry in ranked.entries] == [1, 2, 3, 4, 5]


def test_the_same_catalog_always_produces_the_same_page() -> None:
    catalog = [
        _basket("alpha", ret_1y=Decimal("5.00"), min_amount=Decimal("1000"), watchers=7),
        _basket("bravo", ret_1y=Decimal("5.00"), min_amount=Decimal("1000"), watchers=7),
        _basket("charlie", ret_1y=Decimal("9.00"), min_amount=Decimal("2000"), watchers=3),
        _basket("delta", ret_1y=Decimal("1.00"), min_amount=Decimal("500"), watchers=11),
    ]
    reference = rank_lists(catalog, population=CROWDED)
    shuffler = random.Random(20260825)
    for _ in range(8):
        shuffled = list(catalog)
        shuffler.shuffle(shuffled)
        assert rank_lists(shuffled, population=CROWDED) == reference


# --- 4. the floors -----------------------------------------------------------------------


def test_a_list_below_the_basket_floor_is_withheld_and_says_the_real_count() -> None:
    ranked = _lists(
        [_basket("alpha", ret_1y=Decimal("5.00")), _basket("bravo", ret_1y=Decimal("2.00"))]
    )["TOP_1Y"]
    assert ranked.published is False
    assert ranked.withheld_reason == "TOO_FEW_BASKETS"
    assert ranked.entries == ()
    assert ranked.withheld_note is not None
    assert "2" in ranked.withheld_note
    assert str(MIN_ENTRIES) in ranked.withheld_note


def test_a_population_list_is_withheld_however_many_baskets_there_are() -> None:
    """The floor protects people, not sample size — plenty of baskets does not lift it."""
    catalog = [_basket(f"basket-{index}", watchers=index + 1) for index in range(20)]
    thin = TrendingPopulation(watchers=MIN_POPULATION - 1, investors=99, contributors=99)
    ranked = _lists(catalog, thin)["MOST_WATCHED"]
    assert ranked.withheld_reason == "TOO_FEW_PEOPLE"
    assert ranked.entries == ()
    assert ranked.population == MIN_POPULATION - 1
    assert ranked.withheld_note is not None
    assert str(MIN_POPULATION) in ranked.withheld_note


def test_the_population_floor_is_reported_before_the_basket_floor() -> None:
    """Both fail; "there are three of us" is the true reason the ranking does not exist."""
    ranked = _lists([_basket("alpha", investors=2)], TrendingPopulation(investors=3))[
        "MOST_INVESTED"
    ]
    assert ranked.withheld_reason == "TOO_FEW_PEOPLE"


def test_exactly_at_the_floors_a_list_publishes() -> None:
    catalog = [_basket(f"basket-{index}", investors=index + 1) for index in range(MIN_ENTRIES)]
    ranked = _lists(catalog, TrendingPopulation(investors=MIN_POPULATION))["MOST_INVESTED"]
    assert ranked.published is True
    assert len(ranked.entries) == MIN_ENTRIES


def test_a_non_population_list_carries_no_population_number() -> None:
    """`None`, not zero: a population is not a meaningful figure for a basket's own metric."""
    for row in rank_lists([], population=CROWDED):
        assert (row.population is None) is (not row.population_based)


# --- 5. rounding happens once, here ------------------------------------------------------


def test_display_strings_are_rounded_at_write_time() -> None:
    ranked = _lists(
        [
            _basket("alpha", ret_1y=Decimal("12.345"), min_amount=Decimal("2499.6")),
            _basket("bravo", ret_1y=Decimal("8.001"), min_amount=Decimal("15000")),
            _basket("charlie", ret_1y=Decimal("4.004"), min_amount=Decimal("999999.4")),
        ]
    )
    assert [e.metric_display for e in ranked["TOP_1Y"].entries] == ["12.35%", "8.00%", "4.00%"]
    assert [e.metric_display for e in ranked["BUDGET_FRIENDLY"].entries] == [
        "₹2,500",
        "₹15,000",
        "₹999,999",
    ]


def test_a_date_entry_carries_its_date_and_a_number_entry_carries_its_number() -> None:
    ranked = _lists(
        [
            _basket("alpha", launched_at=dt.date(2026, 6, 1), ret_1m=Decimal("3.00")),
            _basket("bravo", launched_at=dt.date(2025, 6, 1), ret_1m=Decimal("2.00")),
            _basket("charlie", launched_at=dt.date(2024, 6, 1), ret_1m=Decimal("1.00")),
        ]
    )
    launched = ranked["RECENTLY_LAUNCHED"].entries[0]
    assert launched.metric_date == dt.date(2026, 6, 1)
    assert launched.metric_value is None
    assert launched.metric_display == "2026-06-01"

    moved = ranked["TOP_1M"].entries[0]
    assert moved.metric_value == Decimal("3.00")
    assert moved.metric_date is None


# --- 6. the world the product is actually in today ---------------------------------------


def test_every_list_returns_even_when_every_one_of_them_is_withheld() -> None:
    """A withheld list is information. Dropping it would look like never having built it."""
    rows = rank_lists([], population=TrendingPopulation())
    assert len(rows) == 9
    assert [row.key for row in rows] == [d.key for d in TRENDING_LISTS]
    assert all(row.withheld_reason is not None for row in rows)
    assert all(row.withheld_note for row in rows)


def test_the_single_tenant_reality_withholds_every_ranking() -> None:
    """One basket, one investor — today's live database. Nothing may be published from it."""
    rows = rank_lists(
        [
            _basket(
                "baskfy-momentum",
                ret_1m=Decimal("2.10"),
                ret_1y=Decimal("18.40"),
                min_amount=Decimal("125000"),
                launched_at=dt.date(2026, 1, 2),
                watchers=1,
                investors=1,
                inflow_amount=Decimal("500000"),
            )
        ],
        population=TrendingPopulation(watchers=1, investors=1, contributors=1),
    )
    assert all(row.entries == () for row in rows)
    reasons = {row.key: row.withheld_reason for row in rows}
    assert reasons["MOST_INVESTED"] == "TOO_FEW_PEOPLE"
    assert reasons["TOP_1Y"] == "TOO_FEW_BASKETS"
