"""The screener against a real PostgreSQL — docs/06-screener-semantics.md end to end (Prompt 6).

The pure builder is asserted in ``packages/core/tests/test_screener.py``. What is proven here is
the part a builder cannot prove about itself: that the statement returns the right rows, in the
right order, from the reference export docs/13 calls "a labelled answer key".
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from itertools import pairwise

import pytest
from screener_helpers import (
    AS_OF,
    DATA_VERSION,
    add_factor_row,
    add_instrument,
    add_member,
    export_symbols_in_file_order,
    make_row,
    requires_db,
)
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.screener import (
    AsOfOutOfRange,
    NoPublishedData,
    execute_screen,
    resolve_as_of,
    run_screen,
)
from baskfy_core.factor_registry import FACTORS, SORT_FACTOR_KEYS
from baskfy_core.models import FactorDaily, IndexMemberDaily, PipelineRun
from baskfy_core.screen_definition import (
    ExtraFactor,
    PositiveDaysFilter,
    RangeFilter,
    ScreenDefinition,
    TopRiskFilter,
)
from baskfy_core.screener import ScreenQueryError, build_screen_query
from baskfy_core.seed_data import EXAMPLE_SCREENS
from baskfy_core.universes import (
    CONTAINMENT_IDENTITIES,
    REFERENCE_EXPORT_UNIVERSES,
    UNION_IDENTITIES,
    UNIVERSE_BY_SLUG,
    UNIVERSES,
)

pytestmark = [pytest.mark.db, requires_db]

#: A second trading day, carrying no reference data, for tests that need a universe they control.
#: 2026-08-17 is the Monday before the export's date.
SYNTHETIC = dt.date(2026, 8, 17)

NIFTY_500 = UNIVERSE_BY_SLUG["nifty-500"].index_id
TOTAL_MARKET = UNIVERSE_BY_SLUG["nifty-total-market"].index_id

#: docs/13: the "Investing 001" screen the export captures.
INVESTING_001 = next(s for s in EXAMPLE_SCREENS if s.name == "Investing 001").definition

#: docs/13 §2 finding 3: the export's order is only monotonic in the blend to within the rounding
#: granularity of its own 2-dp inputs — "the only 48 'inversions' are <= 0.0075". Any comparison
#: of our order against the file's has to allow exactly that much slack, and no more.
BLEND_ROUNDING_SLACK = Decimal("0.0075")


def is_descending(values: Sequence[object]) -> bool:
    """True when the projected sorting-factor column never rises.

    Takes ``object`` because a result cell is whatever the column's type is — ``marketcap_cr`` is
    a ``bigint`` and arrives as ``int``, everything else is ``numeric``. Anything that is neither
    means the projection returned something unrankable, which is a failure in itself rather than
    something to sort around.
    """
    decimals = [
        Decimal(value) if isinstance(value, int) and not isinstance(value, bool) else value
        for value in values
        if isinstance(value, Decimal) or (isinstance(value, int) and not isinstance(value, bool))
    ]
    assert len(decimals) == len(values), f"non-numeric sorting factor in {values}"
    return all(first >= second for first, second in pairwise(decimals))


def defn(**overrides: object) -> ScreenDefinition:
    payload: dict[str, object] = {
        "index": "nifty-500",
        "sort_by": "ret_12m",
        "historical_date": SYNTHETIC.isoformat(),
    }
    payload.update(overrides)
    return ScreenDefinition.model_validate(payload)


async def symbols_for(
    session: AsyncSession, definition: ScreenDefinition, columns: Sequence[str] = ()
) -> tuple[str, ...]:
    outcome = await run_screen(session, definition, columns=columns)
    assert outcome.result is not None
    return outcome.result.symbols


# ---------------------------------------------------------------------------
# A synthetic universe where every factor is populated and strictly ordered
# ---------------------------------------------------------------------------

RANKED_ROWS = 8


def _row_values(i: int) -> dict[str, object]:
    """Row ``i``'s factor values, built so that *every* registry factor increases with ``i``.

    Strict monotonicity is what makes "correctly ordered" a real assertion rather than a
    tautology: with distinct values there is exactly one right answer for every sort key,
    including the blends and the beta-guarded ratios.
    """
    values: dict[str, object] = {
        "close": Decimal(100 + i),
        "close_raw": Decimal(101 + i),
        "beta_12m": Decimal("0.5") + Decimal(i) / 10,
        "pe": Decimal(10 + i),
        "marketcap_cr": 1000 * (i + 1),
        "away_high_ath": Decimal(-50 + i),
        "away_high_1y": Decimal(-40 + i),
        "high_1y": Decimal(200 + i),
        "high_ath": Decimal(300 + i),
        "median_vol_12m": 10_000_000 * (i + 1),
        "vol_day_val": 1_000_000 * (i + 1),
        "vol_avg_1w": 1_100_000 * (i + 1),
        "ret_12m_minus_1m": Decimal(20 + i),
        "ret_12m_minus_2m": Decimal(30 + i),
        "regime": "BULL",
    }
    for months in (1, 3, 6, 9, 12):
        values[f"ret_{months}m"] = Decimal(i * 10 + months)
        values[f"sharpe_{months}m"] = Decimal(i * 10 + months) / 10
        values[f"rsi_{months}m"] = Decimal(i * 5 + months)
        values[f"vol_{months}m"] = Decimal("0.10") + Decimal(i) / 100
        values[f"pos_days_{months}m"] = Decimal(50 + i)
        values[f"circuits_{months}m"] = i
        values[f"vol_avg_{months}m"] = 1_200_000 * (i + 1)
    for length in (20, 50, 100, 200):
        values[f"ma_{length}"] = Decimal(100 - length // 10 + i)
    return values


async def seed_ranked_universe(
    session: AsyncSession, *, index_id: int = NIFTY_500, rows: int = RANKED_ROWS
) -> tuple[str, ...]:
    """``rows`` fully-populated instruments plus one whose every factor is NULL.

    The NULL row is not decoration: docs/06 §step 4 makes NULL handling load-bearing, so every
    ranking test gets a row that must sort last and every filter test gets a row that must be
    excluded.
    """
    symbols = []
    for i in range(rows):
        symbol = f"RANK{i:02d}"
        await make_row(session, symbol, index_id, on=SYNTHETIC, values=_row_values(i))
        symbols.append(symbol)
    await make_row(session, "NULLROW", index_id, on=SYNTHETIC)
    return tuple(symbols)


# ---------------------------------------------------------------------------
# Step 1 — as-of resolution
# ---------------------------------------------------------------------------


class TestAsOfResolution:
    """docs/06 §step 1."""

    async def test_no_date_means_the_latest_published_day(
        self, screener_session: AsyncSession
    ) -> None:
        resolution = await resolve_as_of(screener_session, None)
        assert resolution.as_of == AS_OF
        assert resolution.snapped is False

    async def test_a_non_trading_day_snaps_backwards(self, screener_session: AsyncSession) -> None:
        """2026-08-16 is a Sunday; the previous trading day is Friday the 14th."""
        resolution = await resolve_as_of(screener_session, dt.date(2026, 8, 16))
        assert resolution.as_of == dt.date(2026, 8, 14)
        assert resolution.snapped is True
        assert resolution.requested == dt.date(2026, 8, 16)

    async def test_a_trading_day_is_used_as_asked(self, screener_session: AsyncSession) -> None:
        resolution = await resolve_as_of(screener_session, SYNTHETIC)
        assert resolution.as_of == SYNTHETIC
        assert resolution.snapped is False

    async def test_a_future_date_is_refused_rather_than_moved(
        self, screener_session: AsyncSession
    ) -> None:
        """docs/07 §"Error catalogue" -> 422 no-trading-day.

        Serving an unpublished day would be docs/06's "half-written day", or plain look-ahead;
        quietly answering for a different date would hide the client bug that asked for it.
        """
        with pytest.raises(AsOfOutOfRange):
            await resolve_as_of(screener_session, dt.date(2026, 11, 30))

    async def test_a_date_before_the_data_start_is_refused(
        self, screener_session: AsyncSession
    ) -> None:
        """docs/01 §2.13: historical data begins 1 Nov 2024."""
        with pytest.raises(AsOfOutOfRange):
            await resolve_as_of(screener_session, dt.date(2024, 10, 31))

    async def test_an_unpublished_database_refuses_to_pick_a_date(
        self, screener_session: AsyncSession
    ) -> None:
        """docs/06 §step 1: "never a half-written day"."""
        await screener_session.execute(update(PipelineRun).values(data_version=None))
        with pytest.raises(NoPublishedData):
            await resolve_as_of(screener_session, None)

    async def test_the_run_reports_the_date_it_actually_used(
        self, screener_session: AsyncSession
    ) -> None:
        """docs/06: "the UI prints 'Results are shown for 19 Aug 2026'"."""
        await seed_ranked_universe(screener_session)
        outcome = await run_screen(screener_session, defn(), requested_as_of=dt.date(2026, 8, 16))
        assert outcome.resolution.requested == dt.date(2026, 8, 16)
        assert outcome.resolution.as_of == dt.date(2026, 8, 14)
        assert '"as_of":"2026-08-14"' in outcome.payload


# ---------------------------------------------------------------------------
# Step 2 — point-in-time universe
# ---------------------------------------------------------------------------


class TestUniverseResolution:
    """docs/06 §step 2 — "the single most common source of backtest look-ahead bias"."""

    async def test_membership_is_read_for_the_as_of_date_only(
        self, screener_session: AsyncSession
    ) -> None:
        """A stock that joins the index tomorrow is not in today's screen."""
        joins_later = await add_instrument(screener_session, "LATECOMER")
        await add_member(screener_session, NIFTY_500, joins_later, AS_OF)
        await add_factor_row(
            screener_session, joins_later, SYNTHETIC, {"series": "EQ", **_row_values(99)}
        )
        symbols = await symbols_for(screener_session, defn())
        assert "LATECOMER" not in symbols

    async def test_a_row_with_no_membership_is_not_in_the_universe(
        self, screener_session: AsyncSession
    ) -> None:
        await seed_ranked_universe(screener_session)
        await make_row(
            screener_session,
            "OUTSIDER",
            NIFTY_500,
            on=SYNTHETIC,
            member=False,
            values=_row_values(99),
        )
        assert "OUTSIDER" not in await symbols_for(screener_session, defn())

    @pytest.mark.parametrize(("subset", "superset"), CONTAINMENT_IDENTITIES)
    async def test_the_containment_identities_hold_with_zero_violations(
        self, screener_session: AsyncSession, subset: str, superset: str
    ) -> None:
        """docs/06 §"Universe flags", verified as exact in the reference export."""
        members = await _members(screener_session)
        assert members[subset] - members[superset] == set()

    @pytest.mark.parametrize(("target", "parts"), UNION_IDENTITIES)
    async def test_the_union_identities_hold_with_zero_violations(
        self, screener_session: AsyncSession, target: str, parts: tuple[str, ...]
    ) -> None:
        members = await _members(screener_session)
        union: set[int] = set()
        for part in parts:
            union |= members[part]
        assert members[target] == union


async def _members(session: AsyncSession) -> dict[str, set[int]]:
    rows = (
        await session.execute(
            select(IndexMemberDaily.index_id, IndexMemberDaily.instrument_id).where(
                IndexMemberDaily.date == AS_OF
            )
        )
    ).all()
    by_id: dict[int, set[int]] = {}
    for index_id, instrument_id in rows:
        by_id.setdefault(index_id, set()).add(instrument_id)
    return {u.slug: by_id.get(u.index_id, set()) for u in UNIVERSES}


# ---------------------------------------------------------------------------
# Step 3 — the bucket
# ---------------------------------------------------------------------------


class TestBucketing:
    """docs/06 §step 3, ranked by the INFERRED ``DECILE_RANK_KEY``."""

    async def test_all_keeps_the_whole_universe(self, screener_session: AsyncSession) -> None:
        await seed_ranked_universe(screener_session)
        assert len(await symbols_for(screener_session, defn(apply_filters_on="all"))) == (
            RANKED_ROWS + 1
        )

    async def test_a_baskfy_keeps_the_largest_by_marketcap(
        self, screener_session: AsyncSession
    ) -> None:
        """Marketcap increases with the row index, so decile 1 of nine rows is the last one."""
        await seed_ranked_universe(screener_session)
        symbols = await symbols_for(screener_session, defn(apply_filters_on="decile_1"))
        assert symbols == ("RANK07",)

    async def test_top_n_takes_the_first_n_rows(self, screener_session: AsyncSession) -> None:
        await seed_ranked_universe(screener_session, rows=60)
        symbols = await symbols_for(screener_session, defn(apply_filters_on="top_50"))
        assert len(symbols) == 50
        assert symbols[0] == "RANK59"
        assert symbols[-1] == "RANK10"
        assert "RANK09" not in symbols

    async def test_a_null_marketcap_is_never_in_the_top_decile(
        self, screener_session: AsyncSession
    ) -> None:
        """PostgreSQL would sort it first under a bare DESC; docs/06 §step 4 says otherwise."""
        await seed_ranked_universe(screener_session)
        symbols = await symbols_for(screener_session, defn(apply_filters_on="decile_5"))
        assert "NULLROW" not in symbols

    async def test_the_bucket_is_cut_before_the_other_filters(
        self, screener_session: AsyncSession
    ) -> None:
        """docs/06 orders it 3 then 4 — bucket the *universe*, then filter the bucket.

        With the bucket first, decile 1 is RANK07 alone and a filter it fails empties the result.
        Filtering first would leave a different eight-row population to take a decile of, and the
        answer would not be empty.
        """
        await seed_ranked_universe(screener_session)
        symbols = await symbols_for(
            screener_session,
            defn(apply_filters_on="decile_1", price=RangeFilter(to=Decimal("105"))),
        )
        assert symbols == ()


# ---------------------------------------------------------------------------
# Step 4 — filters and NULL semantics
# ---------------------------------------------------------------------------


class TestFilters:
    """docs/06 §step 4."""

    async def test_a_stock_listed_three_months_ago_fails_every_one_year_filter(
        self, screener_session: AsyncSession
    ) -> None:
        """docs/06 §step 4: "A stock listed 3 months ago has `ret_12m = NULL` and is therefore
        excluded by any 1-year filter. That is correct and must be documented in the UI."

        Prompt 6's third acceptance criterion, stated as the document states it.
        """
        await seed_ranked_universe(screener_session)
        young = _row_values(3)
        for column in ("ret_12m", "sharpe_12m", "rsi_12m", "vol_12m", "pos_days_12m"):
            young[column] = None
        await make_row(screener_session, "NEWLISTING", NIFTY_500, on=SYNTHETIC, values=young)

        unfiltered = await symbols_for(screener_session, defn())
        assert "NEWLISTING" in unfiltered

        filtered = await symbols_for(
            screener_session, defn(sort_by="close", min_return_1y=Decimal("0"))
        )
        assert "NEWLISTING" not in filtered
        assert "RANK03" in filtered

    async def test_a_null_never_satisfies_a_predicate(self, screener_session: AsyncSession) -> None:
        await seed_ranked_universe(screener_session)
        symbols = await symbols_for(
            screener_session, defn(sort_by="close", positive_days=PositiveDaysFilter(m12=1))
        )
        assert "NULLROW" not in symbols
        assert len(symbols) == RANKED_ROWS

    async def test_filters_are_and_combined(self, screener_session: AsyncSession) -> None:
        await seed_ranked_universe(screener_session)
        symbols = await symbols_for(
            screener_session,
            defn(
                sort_by="close",
                min_return_1y=Decimal("32"),
                marketcap=RangeFilter(to=Decimal("6000")),
            ),
        )
        assert symbols == ("RANK05", "RANK04", "RANK03", "RANK02")

    async def test_the_series_filter_selects_by_series(
        self, screener_session: AsyncSession
    ) -> None:
        await seed_ranked_universe(screener_session)
        await make_row(
            screener_session,
            "BESTOCK",
            NIFTY_500,
            on=SYNTHETIC,
            series="BE",
            values=_row_values(9),
        )
        assert "BESTOCK" not in await symbols_for(screener_session, defn())
        assert "BESTOCK" in await symbols_for(screener_session, defn(series=["EQ", "BE"]))

    async def test_a_custom_filter_compares_two_fields(
        self, screener_session: AsyncSession
    ) -> None:
        """docs/01 §2.14's own example: ``Ma 50 >= Ma 200`` — the golden-cross state."""
        await seed_ranked_universe(screener_session)
        crossed = _row_values(2)
        crossed["ma_50"] = Decimal("10")
        crossed["ma_200"] = Decimal("900")
        await make_row(screener_session, "DEATHCROSS", NIFTY_500, on=SYNTHETIC, values=crossed)
        symbols = await symbols_for(
            screener_session,
            defn(
                sort_by="close",
                custom_filters=[{"enabled": True, "left": "ma_50", "op": ">=", "right": "ma_200"}],
            ),
        )
        assert "DEATHCROSS" not in symbols
        assert len(symbols) == RANKED_ROWS


class TestTopRiskFlags:
    """docs/06 §step 4 ⚠, Prompt 6 §2b, docs/13 §2 finding 10."""

    async def test_the_cut_is_universe_wide_not_over_the_survivors(
        self, screener_session: AsyncSession
    ) -> None:
        """Prompt 6's second acceptance criterion.

        Ten instruments; beta rises with the index. The nightly job flags the top decile of the
        *universe* — RISK09 alone. A filter then removes the two highest-beta rows anyway.

        If "ignore top 5 beta" were a windowed exclusion over the survivors, it would drop the
        five highest-beta rows that remain (RISK03..RISK07) and return three. Reading the
        precomputed flag returns eight, because the flagged row had already gone.
        """
        bit = UNIVERSE_BY_SLUG["nifty-500"].mask_value
        for i in range(10):
            values = _row_values(i)
            values["beta_12m"] = Decimal(i + 1)
            values["ret_12m"] = Decimal(100) if i < 8 else Decimal(10)
            if i == 9:
                values["top_beta_mask"] = bit
            await make_row(screener_session, f"RISK{i:02d}", NIFTY_500, on=SYNTHETIC, values=values)

        definition = defn(
            sort_by="close",
            min_return_1y=Decimal("50"),
            ignore_top_beta=TopRiskFilter(enabled=True, count=5),
        )
        symbols = await symbols_for(screener_session, definition)
        assert len(symbols) == 8
        assert set(symbols) == {f"RISK{i:02d}" for i in range(8)}

    async def test_a_flagged_row_is_dropped_even_when_it_passes_every_filter(
        self, screener_session: AsyncSession
    ) -> None:
        bit = UNIVERSE_BY_SLUG["nifty-500"].mask_value
        await seed_ranked_universe(screener_session)
        flagged = _row_values(4)
        flagged["top_volatility_mask"] = bit
        await make_row(screener_session, "WILD", NIFTY_500, on=SYNTHETIC, values=flagged)
        assert "WILD" in await symbols_for(screener_session, defn())
        assert "WILD" not in await symbols_for(
            screener_session, defn(ignore_top_volatility=TopRiskFilter(enabled=True))
        )

    async def test_the_flag_is_per_universe(self, screener_session: AsyncSession) -> None:
        """A row flagged in NIFTY 500 is not thereby flagged in NIFTY TOTAL MARKET."""
        await seed_ranked_universe(screener_session, index_id=TOTAL_MARKET)
        flagged = _row_values(4)
        flagged["top_beta_mask"] = UNIVERSE_BY_SLUG["nifty-500"].mask_value
        instrument_id = await make_row(
            screener_session, "MIXED", TOTAL_MARKET, on=SYNTHETIC, values=flagged
        )
        await add_member(screener_session, NIFTY_500, instrument_id, SYNTHETIC)

        in_total = defn(index="nifty-total-market", ignore_top_beta=TopRiskFilter(enabled=True))
        assert "MIXED" in await symbols_for(screener_session, in_total)

        in_500 = defn(index="nifty-500", ignore_top_beta=TopRiskFilter(enabled=True))
        assert "MIXED" not in await symbols_for(screener_session, in_500)

    async def test_the_flagged_set_is_separable_by_a_strict_beta_threshold(
        self, screener_session: AsyncSession
    ) -> None:
        """Prompt 6 §2b's own test, on the fixture: docs/13 §2 finding 10.

        "Within every universe the flagged rows' minimum beta strictly exceeds the unflagged rows'
        maximum" — the property that is impossible if the cut is computed after other filters have
        removed rows, and therefore the proof that it is not.
        """
        checked = 0
        for universe in UNIVERSES:
            rows = (
                await screener_session.execute(
                    select(FactorDaily.beta_12m, FactorDaily.top_beta_mask).where(
                        FactorDaily.date == AS_OF,
                        FactorDaily.universe_mask.op("&")(universe.mask_value) != 0,
                    )
                )
            ).all()
            flagged = [b for b, mask in rows if b is not None and mask & universe.mask_value]
            unflagged = [b for b, mask in rows if b is not None and not mask & universe.mask_value]
            if not flagged or not unflagged:
                continue
            assert min(flagged) > max(unflagged), universe.slug
            checked += 1
        # Against REFERENCE_EXPORT_UNIVERSES, not UNIVERSES. This walks the reference export —
        # momoindiascreener.in's own file, the read-only regression corpus — which carries
        # fourteen universes and always will. M59 added a fifteenth to Baskfy's catalog, so
        # `len(UNIVERSES) - 1` started asking the export for a universe it has no column for.
        assert checked == len(REFERENCE_EXPORT_UNIVERSES) - 1, (
            "only `etf` has no members in the export"
        )

    async def test_the_flag_share_matches_the_calibrated_percentile(
        self, screener_session: AsyncSession
    ) -> None:
        """``TOP_RISK_FLAG_PERCENTILE = 0.10`` is docs/06's reading; the fixture agrees."""
        rows = (
            (
                await screener_session.execute(
                    select(FactorDaily.top_beta_mask).where(
                        FactorDaily.date == AS_OF,
                        FactorDaily.universe_mask.op("&")(TOTAL_MARKET_BIT) != 0,
                    )
                )
            )
            .scalars()
            .all()
        )
        flagged = sum(1 for mask in rows if mask & TOTAL_MARKET_BIT)
        assert 0.09 <= flagged / len(rows) <= 0.11


TOTAL_MARKET_BIT = UNIVERSE_BY_SLUG["nifty-total-market"].mask_value


# ---------------------------------------------------------------------------
# Steps 5-6 — ranking
# ---------------------------------------------------------------------------


class TestRanking:
    """docs/06 §step 5-6."""

    @pytest.mark.parametrize("key", SORT_FACTOR_KEYS)
    async def test_every_factor_sorts_a_non_empty_result_correctly(
        self, screener_session: AsyncSession, key: str
    ) -> None:
        """Prompt 6's sixth acceptance criterion, over the whole registry.

        The synthetic universe is built so that every registry factor — singles, blends and the
        beta-guarded ratios alike — increases strictly with the row index, so "correctly ordered"
        has exactly one right answer for each of them.
        """
        expected = await seed_ranked_universe(screener_session)
        outcome = await run_screen(screener_session, defn(sort_by=key))
        assert outcome.result is not None
        rows = outcome.result.rows
        assert len(rows) == RANKED_ROWS + 1

        assert outcome.result.sorting_factor.key == key
        assert outcome.result.sorting_factor.label == FACTORS[key].label

        values = [row.values["sorting_factor"] for row in rows]
        assert all(v is not None for v in values[:-1]), f"{key} produced unexpected NULLs"
        assert values[-1] is None, "the all-NULL row must sort last"
        assert is_descending(values[:-1])
        assert tuple(row.symbol for row in rows[:-1]) == tuple(reversed(expected))
        assert [row.rank for row in rows] == list(range(1, len(rows) + 1))

    async def test_ascending_reverses_the_order(self, screener_session: AsyncSession) -> None:
        expected = await seed_ranked_universe(screener_session)
        symbols = await symbols_for(screener_session, defn(sort_by="close", sort_direction="asc"))
        assert symbols[: len(expected)] == expected

    async def test_nulls_sort_last_in_both_directions(self, screener_session: AsyncSession) -> None:
        """docs/06 §step 4: "NULLS LAST in every ranking" — not "first when ascending"."""
        await seed_ranked_universe(screener_session)
        for direction in ("asc", "desc"):
            symbols = await symbols_for(
                screener_session, defn(sort_by="close", sort_direction=direction)
            )
            assert symbols[-1] == "NULLROW"

    async def test_combined_rank_is_the_sum_and_factor_one_breaks_ties(
        self, screener_session: AsyncSession
    ) -> None:
        """docs/01 §2.12's four-step algorithm, and docs/06's ``ORDER BY combined, rank_1``.

        Four rows, arranged so factor one and factor two disagree completely: the combined sums
        are all equal, so the tie-break on rank one decides, and the result is factor one's order.
        """
        for i, (ret, vol) in enumerate(
            [("40", "0.40"), ("30", "0.30"), ("20", "0.20"), ("10", "0.10")]
        ):
            values = _row_values(i)
            values["ret_12m"] = Decimal(ret)
            values["vol_12m"] = Decimal(vol)
            await make_row(screener_session, f"TIE{i}", NIFTY_500, on=SYNTHETIC, values=values)

        outcome = await run_screen(
            screener_session,
            defn(
                sort_by="ret_12m",
                sort_direction="desc",
                factor_two=ExtraFactor(enabled=True, sort_by="vol_12m", sort_direction="asc"),
            ),
        )
        assert outcome.result is not None
        rows = outcome.result.rows
        assert [row.symbol for row in rows] == ["TIE0", "TIE1", "TIE2", "TIE3"]
        assert {row.combined_rank for row in rows} == {5}
        assert [row.ranks[0] for row in rows] == [1, 2, 3, 4]

    async def test_a_third_factor_joins_the_sum(self, screener_session: AsyncSession) -> None:
        await seed_ranked_universe(screener_session)
        outcome = await run_screen(
            screener_session,
            defn(
                sort_by="ret_12m",
                factor_two=ExtraFactor(enabled=True, sort_by="vol_12m", sort_direction="desc"),
                factor_three=ExtraFactor(enabled=True, sort_by="beta_12m", sort_direction="desc"),
            ),
        )
        assert outcome.result is not None
        first = outcome.result.rows[0]
        assert first.ranks == (1, 1, 1)
        assert first.combined_rank == 3

    async def test_ties_receive_consecutive_integers(self, screener_session: AsyncSession) -> None:
        """docs/06 §step 5: ROW_NUMBER, not RANK, "so that combined sums stay comparable"."""
        for i in range(4):
            values = _row_values(i)
            values["ret_12m"] = Decimal("50")
            await make_row(screener_session, f"SAME{i}", NIFTY_500, on=SYNTHETIC, values=values)
        outcome = await run_screen(screener_session, defn(sort_by="ret_12m"))
        assert outcome.result is not None
        assert sorted(row.ranks[0] for row in outcome.result.rows) == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Step 7 — projection
# ---------------------------------------------------------------------------


class TestProjection:
    """docs/06 §step 7, docs/07 §"Running a screen"."""

    async def test_the_default_columns_are_always_present(
        self, screener_session: AsyncSession
    ) -> None:
        await seed_ranked_universe(screener_session)
        outcome = await run_screen(screener_session, defn())
        assert outcome.result is not None
        assert outcome.result.columns[:3] == ("symbol", "name", "sorting_factor")
        for column in ("ret_12m", "vol_12m", "close_raw"):
            assert column in outcome.result.columns

    async def test_saved_columns_are_added(self, screener_session: AsyncSession) -> None:
        await seed_ranked_universe(screener_session)
        outcome = await run_screen(
            screener_session, defn(), columns=["rsi_6m", "circuits_3m", "high_ath"]
        )
        assert outcome.result is not None
        assert outcome.result.columns[-3:] == ("rsi_6m", "circuits_3m", "high_ath")
        row = outcome.result.rows[0]
        assert row.values["rsi_6m"] is not None
        assert row.values["high_ath"] is not None

    async def test_the_last_close_column_is_the_exchange_print(
        self, screener_session: AsyncSession
    ) -> None:
        """CLAUDE.md house rule 6: display uses ``close_raw``, factors use ``close``."""
        await seed_ranked_universe(screener_session)
        outcome = await run_screen(screener_session, defn(sort_by="close"))
        assert outcome.result is not None
        row = outcome.result.rows[0]
        assert row.values["sorting_factor"] == Decimal(100 + RANKED_ROWS - 1)
        assert row.values["close_raw"] == Decimal(101 + RANKED_ROWS - 1)

    async def test_rows_are_capped_and_the_cap_is_reported(
        self, screener_session: AsyncSession
    ) -> None:
        """docs/03 §"Request path": "Result rows (<= 4,000)"."""
        await seed_ranked_universe(screener_session)
        result = await execute_screen(
            screener_session,
            defn(),
            as_of=SYNTHETIC,
            data_version=DATA_VERSION,
            limit=3,
        )
        assert result.result_count == 3
        assert result.truncated is True


# ---------------------------------------------------------------------------
# The determinism guarantee
# ---------------------------------------------------------------------------


class TestDeterminism:
    """docs/06 §"Determinism guarantee" — Prompt 6's fourth acceptance criterion."""

    async def test_the_same_inputs_produce_byte_identical_json(
        self, screener_session: AsyncSession
    ) -> None:
        await seed_ranked_universe(screener_session)
        definition = defn(
            factor_two=ExtraFactor(enabled=True, sort_by="vol_12m", sort_direction="asc")
        )
        first = await run_screen(screener_session, definition)
        second = await run_screen(screener_session, definition)
        assert first.payload == second.payload
        assert first.data_version == second.data_version == DATA_VERSION

    async def test_ties_do_not_reshuffle_between_runs(self, screener_session: AsyncSession) -> None:
        """Without the instrument_id tie-breaker inside ROW_NUMBER this is not guaranteed."""
        for i in range(20):
            values = _row_values(i)
            values["ret_12m"] = Decimal("50")
            values["close"] = Decimal("100")
            await make_row(screener_session, f"DUP{i:02d}", NIFTY_500, on=SYNTHETIC, values=values)
        definition = defn(sort_by="ret_12m")
        payloads = {(await run_screen(screener_session, definition)).payload for _ in range(3)}
        assert len(payloads) == 1

    async def test_the_payload_carries_the_definition_hash_inputs(
        self, screener_session: AsyncSession
    ) -> None:
        await seed_ranked_universe(screener_session)
        outcome = await run_screen(screener_session, defn())
        assert f'"data_version":{DATA_VERSION}' in outcome.payload
        assert f'"as_of":"{SYNTHETIC.isoformat()}"' in outcome.payload


# ---------------------------------------------------------------------------
# The reference export, end to end
# ---------------------------------------------------------------------------


class TestReferenceExport:
    """Prompt 6's last acceptance criterion, against docs/13's answer key."""

    async def test_investing_001_returns_the_same_271_symbols(
        self, screener_session: AsyncSession
    ) -> None:
        outcome = await run_screen(screener_session, INVESTING_001, requested_as_of=AS_OF)
        assert outcome.result is not None
        assert outcome.result.result_count == 271
        assert set(outcome.result.symbols) == set(export_symbols_in_file_order())

    async def test_investing_001_reproduces_the_export_order(
        self, screener_session: AsyncSession
    ) -> None:
        """Same order, to the only tolerance the file itself supports.

        docs/13 §2 finding 3: the export's own order is monotonic in the blend "the only 48
        'inversions' are <= 0.0075, i.e. exactly the rounding granularity of 2-dp inputs". The
        reference product ranked on unrounded values; the file preserves only two decimals, so an
        exact position-for-position match is arithmetically unavailable to anyone reading it —
        including the reference product itself, re-run from its own export. What *is* available,
        and what is asserted here, is that at every rank our row and the file's row are within
        that granularity of each other: no disagreement is larger than the rounding that caused it.
        """
        outcome = await run_screen(screener_session, INVESTING_001, requested_as_of=AS_OF)
        assert outcome.result is not None
        ours = outcome.result.rows
        theirs = export_symbols_in_file_order()
        assert len(ours) == len(theirs)

        by_symbol = {row.symbol: row.values["sorting_factor"] for row in ours}
        gaps = []
        for position, (mine, file_symbol) in enumerate(zip(ours, theirs, strict=True)):
            if mine.symbol == file_symbol:
                continue
            mine_value = by_symbol[mine.symbol]
            file_value = by_symbol[file_symbol]
            assert isinstance(mine_value, Decimal) and isinstance(file_value, Decimal)
            gaps.append((position, abs(mine_value - file_value)))

        assert all(gap <= BLEND_ROUNDING_SLACK for _, gap in gaps), max(gaps, key=lambda g: g[1])

    async def test_our_own_ordering_is_exact(self, screener_session: AsyncSession) -> None:
        """Whatever the file's tie-order was, ours is monotonic in the blend by construction."""
        outcome = await run_screen(screener_session, INVESTING_001, requested_as_of=AS_OF)
        assert outcome.result is not None
        values = [row.values["sorting_factor"] for row in outcome.result.rows]
        assert is_descending(values)

    async def test_the_sorting_factor_is_the_documented_blend(
        self, screener_session: AsyncSession
    ) -> None:
        """docs/13: sort factor AVERAGE SHARPE RETURN 12 6 3 1 MONTHS, computed from stored rows."""
        outcome = await run_screen(screener_session, INVESTING_001, requested_as_of=AS_OF)
        assert outcome.result is not None
        top = outcome.result.rows[0]
        row = (
            await screener_session.execute(
                select(FactorDaily).where(
                    FactorDaily.instrument_id == top.instrument_id, FactorDaily.date == AS_OF
                )
            )
        ).scalar_one()
        components = [row.sharpe_12m, row.sharpe_6m, row.sharpe_3m, row.sharpe_1m]
        assert all(component is not None for component in components)
        present = [component for component in components if component is not None]
        expected = sum(present, start=Decimal(0)) / len(present)
        assert top.values["sorting_factor"] == pytest.approx(expected)

    async def test_the_median_volume_floor_admits_every_export_row(
        self, screener_session: AsyncSession
    ) -> None:
        """The export is a result set: every row in it passed the screen's own filters."""
        outcome = await run_screen(screener_session, INVESTING_001, requested_as_of=AS_OF)
        assert outcome.result is not None
        assert outcome.result.result_count == 271

    async def test_dropping_the_be_series_would_lose_two_rows(
        self, screener_session: AsyncSession
    ) -> None:
        """Why the seeded definition carries ``series: ["EQ", "BE"]`` — see seed_data.py."""
        eq_only = INVESTING_001.model_copy(update={"series": ["EQ"]})
        outcome = await run_screen(screener_session, eq_only, requested_as_of=AS_OF)
        assert outcome.result is not None
        assert outcome.result.result_count == 269


# ---------------------------------------------------------------------------
# Injection, at the service boundary
# ---------------------------------------------------------------------------


class TestInjectionAtTheBoundary:
    """Prompt 6's first acceptance criterion, one layer out from the builder."""

    async def test_a_malicious_definition_never_reaches_the_database(
        self, screener_session: AsyncSession
    ) -> None:
        smuggled = ScreenDefinition.model_construct(
            index="nifty-500",
            sort_by="ret_12m FROM factor_daily; DROP TABLE screen; --",
            historical_date=SYNTHETIC,
        )
        with pytest.raises(ScreenQueryError):
            await run_screen(screener_session, smuggled)
        still_there = (await screener_session.execute(select(PipelineRun.id))).scalars().all()
        assert still_there

    async def test_a_hostile_column_list_is_refused(self, screener_session: AsyncSession) -> None:
        with pytest.raises(ScreenQueryError):
            build_screen_query(defn(), SYNTHETIC, columns=["close_raw; DROP TABLE screen"])
