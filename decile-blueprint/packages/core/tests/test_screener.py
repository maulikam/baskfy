"""The screener query builder — docs/06-screener-semantics.md (Prompt 6 deliverables 1-4, 6).

These are the assertions that do not need a database: what SQL comes out of a definition, what
never comes out of it, and how a result serialises. The behavioural half — that the SQL returns
the right rows — is in ``services/api/tests/test_screener.py``, which runs against PostgreSQL,
because a query builder that only ever gets asserted against its own output proves nothing.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from decimal import Decimal

import pytest

from decile_core.factor_registry import (
    COLUMN_PICKER_KEYS,
    CUSTOM_FILTER_OPERANDS,
    FACTORS,
    SORT_FACTOR_KEYS,
)
from decile_core.screen_definition import (
    AwayFromHighFilter,
    CircuitsFilter,
    CustomFilter,
    ExtraFactor,
    MovingAverageFilter,
    PeFilter,
    PositiveDaysFilter,
    RangeFilter,
    ScreenDefinition,
    TopRiskFilter,
)
from decile_core.screener import (
    BUCKET_FRACTIONS,
    BUCKET_ROW_LIMITS,
    DEFAULT_RESULT_COLUMNS,
    IDENTITY_COLUMNS,
    RankingFactor,
    ScreenQueryError,
    ScreenResult,
    ScreenResultRow,
    build_screen_query,
    cache_key,
    canonical_json,
    resolve_columns,
    validate_definition,
)
from decile_core.seed_data import EXAMPLE_SCREENS
from decile_core.universes import UNIVERSE_BY_SLUG, UNIVERSES

AS_OF = dt.date(2026, 8, 18)


def base(**overrides: object) -> ScreenDefinition:
    payload: dict[str, object] = {"index": "nifty-500", "sort_by": "ret_12m"}
    payload.update(overrides)
    return ScreenDefinition.model_validate(payload)


def sql_for(definition: ScreenDefinition, columns: Sequence[str] = ()) -> str:
    return build_screen_query(definition, AS_OF, columns=columns).sql()


def cte(text: str, name: str) -> str:
    """The rendered text of one CTE, so an assertion cannot match a forwarded column name.

    Every CTE re-selects all sixty ``factor_daily`` columns, so a bare ``"ma_200" in sql`` is true
    of any query whatsoever. Assertions about *predicates* have to look at the WHERE clause.
    """
    order = ["universe AS", "bucketed AS", "selected AS", "filtered AS", "relative AS", "ranked AS"]
    start = text.index(f"{name} AS")
    later = [
        text.index(marker) for marker in order if marker in text and text.index(marker) > start
    ]
    end = min(later) if later else len(text)
    return text[start:end]


def where_of(text: str, name: str) -> str:
    """Just the predicates of one CTE — empty when it has none."""
    segment = cte(text, name)
    return segment.split("\nWHERE ", 1)[1] if "\nWHERE " in segment else ""


class TestTheWhitelistGate:
    """Prompt 6 acceptance criterion 1 — "both must be rejected before query construction"."""

    @pytest.mark.parametrize(
        "malicious",
        [
            "ret_12m; DROP TABLE factor_daily",
            "ret_12m) OR 1=1 --",
            "(SELECT password FROM app_user)",
            "pg_sleep(10)",
            "ret_12m'",
        ],
    )
    def test_a_malicious_sort_key_never_reaches_query_construction(self, malicious: str) -> None:
        """It does not even survive ``ScreenDefinition``, which is the first of two gates."""
        with pytest.raises(ValueError, match=r"factor-registry keys|String should match"):
            base(sort_by=malicious)

    def test_a_malicious_sort_key_smuggled_past_the_model_is_still_refused(self) -> None:
        """Defence in depth: the builder re-checks rather than trusting its caller.

        ``model_construct`` skips validation, which is exactly how a key could arrive here from a
        row persisted before the registry existed, or from a caller that built the object by hand.
        """
        smuggled = ScreenDefinition.model_construct(
            index="nifty-500", sort_by="ret_12m); DROP TABLE factor_daily --"
        )
        with pytest.raises(ScreenQueryError, match="not a factor-registry key"):
            build_screen_query(smuggled, AS_OF)

    @pytest.mark.parametrize("side", ["left", "right"])
    def test_a_malicious_custom_filter_operand_is_refused(self, side: str) -> None:
        payload = {"enabled": True, "left": "ma_50", "op": ">=", "right": "ma_200"}
        payload[side] = "ma_200 OR 1=1"
        with pytest.raises(ValueError, match=r"custom-filter operands|String should match"):
            base(custom_filters=[payload])

    def test_a_smuggled_custom_filter_operand_is_still_refused(self) -> None:
        smuggled = ScreenDefinition.model_construct(
            index="nifty-500",
            sort_by="ret_12m",
            custom_filters=[
                CustomFilter.model_construct(enabled=True, left="ma_50", op=">=", right="ma_200 --")
            ],
        )
        with pytest.raises(ScreenQueryError, match="not a custom-filter operand"):
            build_screen_query(smuggled, AS_OF)

    def test_a_blend_is_a_sort_key_but_not_a_custom_filter_operand(self) -> None:
        """docs/01 §2.14's operand list is a strict subset of the 64 sort factors."""
        assert "avg_sharpe_12_6_3_1" in FACTORS
        assert "avg_sharpe_12_6_3_1" not in CUSTOM_FILTER_OPERANDS
        with pytest.raises(ValueError, match="custom-filter operands"):
            base(
                custom_filters=[
                    {"enabled": True, "left": "avg_sharpe_12_6_3_1", "op": ">=", "right": "close"}
                ]
            )

    def test_an_unknown_result_column_is_refused_rather_than_dropped(self) -> None:
        with pytest.raises(ScreenQueryError, match="selectable result columns"):
            resolve_columns(["definitely_not_a_column"])

    def test_every_registry_expression_is_inert_sql(self) -> None:
        """The only strings that reach the SQL text are these, so they carry the whole trust.

        No quotes, no semicolons, no comment markers, no dollar-quoting: nothing that could end an
        expression and begin a statement.
        """
        allowed = re.compile(r"^[A-Za-z0-9_ ()+\-*/.>=<]+$")
        offenders = {
            key: factor.sql_expr
            for key, factor in FACTORS.items()
            if not allowed.fullmatch(factor.sql_expr)
        }
        assert offenders == {}

    def test_user_supplied_values_are_bound_not_interpolated(self) -> None:
        """Thresholds are parameters; the rendered text carries placeholders, not numbers."""
        query = build_screen_query(base(min_return_1y=Decimal("37.5")), AS_OF)
        assert "37.5" not in query.sql()
        assert Decimal("37.5") in query.params().values()


class TestPipelineOrder:
    """docs/06 §"The pipeline" — "The order of operations is load-bearing"."""

    def test_the_statement_follows_the_reference_skeleton(self) -> None:
        text = sql_for(base(apply_filters_on="decile_1"))
        stages = [
            text.index("universe AS"),
            text.index("bucketed AS"),
            text.index("selected AS"),
            text.index("filtered AS"),
            text.index("relative AS"),
            text.index("ranked AS"),
        ]
        assert stages == sorted(stages)

    def test_it_is_one_statement(self) -> None:
        assert sql_for(base()).count(";") == 0

    def test_the_universe_is_resolved_point_in_time(self) -> None:
        """docs/06 §step 2 — membership is read for ``as_of``, never for today."""
        query = build_screen_query(base(index="nifty-50"), AS_OF)
        text = query.sql()
        assert "FROM index_member_daily" in text
        assert "index_member_daily.date =" in text
        assert query.index_id == UNIVERSE_BY_SLUG["nifty-50"].index_id
        assert AS_OF in query.params().values()

    def test_the_final_sort_is_combined_then_rank_one(self) -> None:
        """docs/06 §step 6: ``ORDER BY combined ASC, rank_1 ASC``."""
        text = sql_for(base())
        assert "ORDER BY combined_rank ASC, ranked.r1 ASC" in text


class TestBucketing:
    """docs/06 §step 3 and its INFERRED ranking key."""

    def test_all_takes_the_whole_universe(self) -> None:
        text = sql_for(base(apply_filters_on="all"))
        assert "percent_rank" not in text
        assert "bucket_row_number" not in text

    @pytest.mark.parametrize(("bucket", "fraction"), sorted(BUCKET_FRACTIONS.items()))
    def test_deciles_cut_by_percent_rank_over_marketcap(
        self, bucket: str, fraction: Decimal
    ) -> None:
        query = build_screen_query(base(apply_filters_on=bucket), AS_OF)
        text = query.sql()
        assert "percent_rank() OVER (ORDER BY factor_daily.marketcap_cr DESC NULLS LAST)" in text
        assert "bucket_percent_rank <=" in text
        assert fraction in query.params().values()

    @pytest.mark.parametrize(("bucket", "rows"), sorted(BUCKET_ROW_LIMITS.items()))
    def test_top_n_cuts_by_row_number(self, bucket: str, rows: int) -> None:
        query = build_screen_query(base(apply_filters_on=bucket), AS_OF)
        assert "bucket_row_number <=" in query.sql()
        assert rows in query.params().values()

    def test_an_unknown_marketcap_never_lands_in_the_top_decile(self) -> None:
        """PostgreSQL sorts NULLs first under DESC; docs/06 §step 4 says NULLs satisfy nothing."""
        assert "marketcap_cr DESC NULLS LAST" in sql_for(base(apply_filters_on="decile_1"))

    def test_the_bucket_is_taken_before_the_filters(self) -> None:
        """docs/06 orders the pipeline 3 then 4: bucket the universe, then filter the bucket."""
        text = sql_for(base(apply_filters_on="decile_2", min_return_1y=Decimal("10")))
        assert text.index("bucket_percent_rank <=") < text.index("selected.ret_12m >=")


class TestFilters:
    """docs/06 §step 4's table, sentinel by sentinel (docs/01 §2.4-§2.10)."""

    def test_a_bare_definition_filters_only_on_series(self) -> None:
        """Every other filter defaults to its documented "ignore" value."""
        where = where_of(sql_for(base()), "filtered")
        assert "series IN" in where
        for absent in ("ret_12m >=", "median_vol_12m >=", "abs(", "beta_12m <=", "pe IS NOT NULL"):
            assert absent not in where

    @pytest.mark.parametrize(
        ("overrides", "fragment"),
        [
            ({"min_return_1y": Decimal("25")}, "ret_12m >="),
            ({"median_volume_1y": 10_000_000}, "median_vol_12m >="),
            ({"ignore_above_beta": 2}, "beta_12m <="),
            ({"marketcap": RangeFilter(**{"from": 500})}, "marketcap_cr >="),
            ({"marketcap": RangeFilter(to=Decimal("50000"))}, "marketcap_cr <="),
            ({"price": RangeFilter(**{"from": 10})}, "close_raw >="),
            ({"price": RangeFilter(to=Decimal("5000"))}, "close_raw <="),
            ({"away_from_high": AwayFromHighFilter(ath=25)}, "abs(bucketed.away_high_ath) <="),
            ({"away_from_high": AwayFromHighFilter(one_year=10)}, "abs(bucketed.away_high_1y) <="),
            ({"positive_days": PositiveDaysFilter(m12=55)}, "pos_days_12m >="),
            ({"positive_days": PositiveDaysFilter(m1=60)}, "pos_days_1m >="),
            ({"circuits": CircuitsFilter(m6=3)}, "circuits_6m <="),
            ({"pe": PeFilter(enabled=True, to=Decimal("40"))}, "pe IS NOT NULL"),
            (
                {"moving_average": MovingAverageFilter(enabled=True, above_200=True)},
                "bucketed.close > bucketed.ma_200",
            ),
            (
                {"moving_average": MovingAverageFilter(enabled=True, below_50=True)},
                "bucketed.close < bucketed.ma_50",
            ),
        ],
    )
    def test_an_active_filter_emits_its_clause(
        self, overrides: dict[str, object], fragment: str
    ) -> None:
        assert fragment in where_of(sql_for(base(**overrides)), "filtered")

    @pytest.mark.parametrize(
        ("overrides", "fragment"),
        [
            ({"away_from_high": AwayFromHighFilter(ath=100, one_year=100)}, "abs("),
            ({"circuits": CircuitsFilter(m12=999)}, "circuits_12m <="),
            ({"circuits": CircuitsFilter(m12=251)}, "circuits_12m <="),
            ({"positive_days": PositiveDaysFilter()}, "pos_days_12m >="),
            ({"ignore_above_beta": 100}, "beta_12m <="),
            ({"pe": PeFilter(enabled=False, to=Decimal("40"))}, "pe IS NOT NULL"),
            (
                {"moving_average": MovingAverageFilter(enabled=False, above_200=True)},
                "> bucketed.ma_200",
            ),
            ({"marketcap": RangeFilter()}, "marketcap_cr >="),
            ({"series": []}, "series IN"),
        ],
    )
    def test_a_sentinel_disables_its_filter_entirely(
        self, overrides: dict[str, object], fragment: str
    ) -> None:
        """The sentinel is not compared in SQL; the clause is simply never emitted."""
        assert fragment not in where_of(sql_for(base(**overrides)), "filtered")

    def test_a_circuits_cap_at_the_boundary_still_binds(self) -> None:
        """docs/01 §2.6: "> 250 = ignore" — so 250 itself is an active cap."""
        assert "circuits_12m <=" in where_of(
            sql_for(base(circuits=CircuitsFilter(m12=250))), "filtered"
        )

    def test_custom_filters_compare_field_to_field(self) -> None:
        text = sql_for(
            base(
                custom_filters=[
                    {"enabled": True, "left": "ma_50", "op": ">=", "right": "ma_200"},
                    {"enabled": True, "left": "vol_avg_1w", "op": "<=", "right": "vol_avg_12m"},
                    {"enabled": True, "left": "close", "op": "=", "right": "close_raw"},
                ]
            )
        )
        assert "bucketed.ma_50 >= bucketed.ma_200" in text
        assert "bucketed.vol_avg_1w <= bucketed.vol_avg_12m" in text
        assert "bucketed.close = bucketed.close_raw" in text

    def test_a_disabled_custom_filter_slot_emits_nothing(self) -> None:
        text = sql_for(
            base(
                custom_filters=[{"enabled": False, "left": "ma_50", "op": ">=", "right": "ma_200"}]
            )
        )
        assert "ma_50 >=" not in where_of(text, "filtered")

    def test_no_filter_uses_coalesce(self) -> None:
        """docs/06 §step 4: "NULLs never satisfy a predicate."

        A COALESCE anywhere in the WHERE would readmit exactly the young listings the document
        says must be excluded, and it would do it silently.
        """
        busy = base(
            min_return_1y=Decimal("10"),
            median_volume_1y=1,
            away_from_high=AwayFromHighFilter(ath=25, one_year=25),
            positive_days=PositiveDaysFilter(m12=55, m9=55, m6=55, m3=55, m1=55),
            circuits=CircuitsFilter(m12=5, m9=5, m6=5, m3=5, m1=5),
            marketcap=RangeFilter(**{"from": 100, "to": 900000}),
            pe=PeFilter(enabled=True, **{"from": 1, "to": 60}),
            ignore_above_beta=2,
            price=RangeFilter(**{"from": 10, "to": 100000}),
            moving_average=MovingAverageFilter(enabled=True, above_200=True, above_50=True),
        )
        assert "coalesce" not in where_of(sql_for(busy), "filtered").lower()


class TestTopRiskFlags:
    """docs/06 §step 4 ⚠ and Prompt 6 §2b — precomputed, per universe, not a post-filter cut."""

    def test_the_flag_is_read_as_a_bit_of_the_universe_mask(self) -> None:
        query = build_screen_query(
            base(index="nifty-500", ignore_top_beta=TopRiskFilter(enabled=True)), AS_OF
        )
        text = query.sql()
        assert "top_beta_mask &" in text
        assert UNIVERSE_BY_SLUG["nifty-500"].mask_value in query.params().values()

    def test_each_universe_selects_its_own_bit(self) -> None:
        bits = set()
        for universe in UNIVERSES:
            query = build_screen_query(
                base(index=universe.slug, ignore_top_volatility=TopRiskFilter(enabled=True)), AS_OF
            )
            assert "top_volatility_mask &" in query.sql()
            bits.add(universe.mask_value)
            assert universe.mask_value in query.params().values()
        assert len(bits) == len(UNIVERSES)

    def test_it_is_not_a_windowed_exclusion(self) -> None:
        """The correction in docs/06 §step 4 ⚠: the cut is nightly and universe-wide.

        A post-filter implementation would need a second ranking window over ``beta_12m``; there
        is exactly one ranking window per enabled sort factor and no more.
        """
        text = sql_for(base(ignore_top_beta=TopRiskFilter(enabled=True, count=5)))
        assert text.count("row_number() OVER") == 2  # r1, plus the final rank
        assert "beta_12m DESC" not in text

    def test_the_switch_alone_decides_and_the_count_is_inert(self) -> None:
        """A documented limitation, not an oversight — see docs/06a §3.

        docs/01 §2.10 gives the switch "a count/percentile input", but docs/06 §step 4 settles the
        semantics as a boolean precomputed at ``TOP_RISK_FLAG_PERCENTILE``. A per-request count
        cannot be served by testing a bit, so two different counts produce the same SQL.
        """
        five = sql_for(base(ignore_top_beta=TopRiskFilter(enabled=True, count=5)))
        fifty = sql_for(base(ignore_top_beta=TopRiskFilter(enabled=True, count=50)))
        assert five == fifty


class TestRanking:
    """docs/06 §step 5-6."""

    def test_a_single_factor_ranks_once_and_pads_with_zero(self) -> None:
        query = build_screen_query(base(sort_by="sharpe_12m"), AS_OF)
        text = query.sql()
        assert "AS r1" in text and "AS r2" in text and "AS r3" in text
        assert query.ranking_factors == (
            RankingFactor(1, "sharpe_12m", FACTORS["sharpe_12m"].label, "desc"),
        )
        assert list(query.params().values()).count(0) == 2

    def test_three_factors_each_get_their_own_direction(self) -> None:
        """docs/06: "a user can rank by 'highest 12-month Sharpe' *and* 'lowest volatility'"."""
        query = build_screen_query(
            base(
                sort_by="sharpe_12m",
                sort_direction="desc",
                factor_two=ExtraFactor(enabled=True, sort_by="vol_12m", sort_direction="asc"),
                factor_three=ExtraFactor(enabled=True, sort_by="beta_12m", sort_direction="asc"),
            ),
            AS_OF,
        )
        text = query.sql()
        assert "ORDER BY sharpe_12m DESC NULLS LAST" in text
        assert "ORDER BY vol_12m ASC NULLS LAST" in text
        assert "ORDER BY beta_12m ASC NULLS LAST" in text
        assert [f.direction for f in query.ranking_factors] == ["desc", "asc", "asc"]

    def test_every_ranking_window_is_nulls_last(self) -> None:
        """docs/06 §step 4: "NULLS LAST in every ranking"."""
        text = sql_for(
            base(
                sort_by="ret_12m",
                factor_two=ExtraFactor(enabled=True, sort_by="rsi_6m", sort_direction="asc"),
            )
        )
        windows = re.findall(r"row_number\(\) OVER \(ORDER BY (.*?)\)", text)
        ranking = [w for w in windows if "NULLS LAST" in w or "ranked.r1" in w]
        assert len(ranking) == 3
        assert all("NULLS LAST" in w for w in ranking if "ranked.r1 + " not in w)

    def test_every_ranking_window_has_a_deterministic_tie_breaker(self) -> None:
        """docs/06 §"Determinism guarantee" needs it; ROW_NUMBER alone does not provide it.

        Without a tie-breaker two rows with the same factor value can swap ranks between runs,
        and the cached JSON would differ byte for byte with identical inputs.
        """
        text = sql_for(base(factor_two=ExtraFactor(enabled=True, sort_by="vol_12m")))
        assert text.count("relative.instrument_id ASC") == 2

    def test_ties_get_consecutive_integers_not_shared_ones(self) -> None:
        """docs/06 §step 5: ROW_NUMBER, "not RANK", so combined sums stay comparable."""
        text = sql_for(base())
        assert "rank() OVER" not in text.replace("row_number() OVER", "")
        assert "dense_rank" not in text

    def test_the_sorting_factor_column_is_factor_one(self) -> None:
        """docs/06 §step 6: the Sorting Factor column always shows factor one's value."""
        query = build_screen_query(
            base(
                sort_by="avg_sharpe_12_6_3_1",
                factor_two=ExtraFactor(enabled=True, sort_by="vol_12m", sort_direction="asc"),
            ),
            AS_OF,
        )
        assert query.sorting_factor.key == "avg_sharpe_12_6_3_1"
        assert FACTORS["avg_sharpe_12_6_3_1"].sql_expr + " AS sorting_factor" in query.sql()

    @pytest.mark.parametrize("key", SORT_FACTOR_KEYS)
    def test_every_registry_factor_builds_a_statement(self, key: str) -> None:
        query = build_screen_query(base(sort_by=key), AS_OF)
        assert FACTORS[key].sql_expr in query.sql()


class TestProjection:
    """docs/06 §step 7 and docs/07 §"Running a screen"."""

    def test_defaults_come_first_in_the_documented_order(self) -> None:
        assert resolve_columns() == (*IDENTITY_COLUMNS, *DEFAULT_RESULT_COLUMNS)

    def test_saved_columns_append_without_duplicating_defaults(self) -> None:
        columns = resolve_columns(["series", "rsi_12m", "circuits_1m", "close_raw"])
        assert columns[: len(IDENTITY_COLUMNS) + len(DEFAULT_RESULT_COLUMNS)] == (
            *IDENTITY_COLUMNS,
            *DEFAULT_RESULT_COLUMNS,
        )
        assert columns[-2:] == ("rsi_12m", "circuits_1m")
        assert len(columns) == len(set(columns))

    def test_every_column_picker_key_is_projectable(self) -> None:
        query = build_screen_query(base(), AS_OF, columns=list(COLUMN_PICKER_KEYS))
        for key in COLUMN_PICKER_KEYS:
            assert key in query.columns

    def test_identity_columns_come_from_the_instrument_join(self) -> None:
        text = sql_for(base())
        assert "JOIN instrument ON instrument.id = ranked.instrument_id" in text
        assert "instrument.symbol AS symbol" in text
        assert "instrument.name AS name" in text


class TestCacheKeyAndSerialisation:
    """docs/06 §Caching and §"Determinism guarantee"."""

    def test_the_key_has_the_shape_the_document_specifies(self) -> None:
        """docs/06 §Caching: ``screen:{sha256(...)}:{as_of}:{data_version}``."""
        key = cache_key(base(), AS_OF, 1421)
        namespace, digest, as_of, version = key.split(":")
        assert namespace == "screen"
        assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
        assert as_of == "2026-08-18"
        assert version == "1421"

    def test_the_digest_covers_the_projection_as_well_as_the_definition(self) -> None:
        """The cached value is the response body, and the body's columns are the user's.

        docs/06's formula hashes the definition alone. Two requests that share a definition and
        differ in columns are exactly what the columns editor produces, and hashing the definition
        alone serves the second one the first one's columns. See the note on `cache_key`.
        """
        definition = base()
        plain = cache_key(definition, AS_OF, 1421)
        wider = cache_key(definition, AS_OF, 1421, columns=["rsi_12m"])
        assert plain != wider
        # The default projection is the same whether it is implied or spelled out.
        assert cache_key(definition, AS_OF, 1421, columns=list(DEFAULT_RESULT_COLUMNS)) == plain

    def test_the_digest_is_not_the_definition_hash(self) -> None:
        """``screen_run.definition_hash`` answers a different question and keeps its own value."""
        definition = base()
        assert definition.definition_hash() not in cache_key(definition, AS_OF, 1421)

    def test_the_key_moves_with_the_definition_the_date_and_the_version(self) -> None:
        first = cache_key(base(), AS_OF, 1)
        assert first != cache_key(base(min_return_1y=Decimal("1")), AS_OF, 1)
        assert first != cache_key(base(), dt.date(2026, 8, 17), 1)
        assert first != cache_key(base(), AS_OF, 2)

    def test_two_spellings_of_the_same_definition_share_a_key(self) -> None:
        terse = ScreenDefinition.model_validate({"index": "nifty-500", "sort_by": "ret_12m"})
        verbose = ScreenDefinition.model_validate(
            {"index": "nifty-500", "sort_by": "ret_12m", "sort_direction": "desc", "series": ["EQ"]}
        )
        assert cache_key(terse, AS_OF, 7) == cache_key(verbose, AS_OF, 7)

    def test_decimals_keep_their_stored_precision(self) -> None:
        """CLAUDE.md house rule 8-9: round at write time, and never route money through float."""
        assert canonical_json({"sharpe_12m": Decimal("13.00")}) == '{"sharpe_12m":13.00}'
        assert canonical_json({"vol_12m": Decimal("0.5793179400")}) == '{"vol_12m":0.5793179400}'

    def test_decimals_never_serialise_in_exponent_form(self) -> None:
        assert canonical_json({"v": Decimal("1E+3")}) == '{"v":1000}'

    def test_the_encoding_is_stable_across_calls(self) -> None:
        payload = {"b": [Decimal("1.10"), None, True], "a": "x", "d": dt.date(2026, 8, 18)}
        assert canonical_json(payload) == canonical_json(dict(payload))

    def test_a_result_serialises_to_the_documented_shape(self) -> None:
        result = ScreenResult(
            as_of=AS_OF,
            data_version=1421,
            sorting_factor=RankingFactor(1, "ret_12m", FACTORS["ret_12m"].label, "desc"),
            columns=("symbol", "name", "sorting_factor"),
            rows=(
                ScreenResultRow(
                    rank=1,
                    instrument_id=42,
                    combined_rank=1,
                    ranks=(1, 0, 0),
                    values={
                        "symbol": "CUPID",
                        "name": "CUPID LIMITED",
                        "sorting_factor": Decimal("753.00"),
                    },
                ),
            ),
        )
        assert result.to_json() == (
            '{"as_of":"2026-08-18","data_version":1421,"result_count":1,'
            '"sorting_factor":{"key":"ret_12m","label":"ABSOLUTE RETURN 1 YEAR"},'
            '"columns":["symbol","name","sorting_factor"],'
            '"rows":[{"rank":1,"symbol":"CUPID","name":"CUPID LIMITED","sorting_factor":753.00}]}'
        )
        assert result.symbols == ("CUPID",)


class TestValidation:
    def test_validate_accepts_every_example_screen(self) -> None:
        for screen in EXAMPLE_SCREENS:
            validate_definition(screen.definition)

    def test_an_unknown_bucket_is_refused(self) -> None:
        # A bucket no ``Literal`` allows, so it has to be smuggled past the model to reach the
        # builder at all — which is the point: the builder does not trust its caller.
        smuggled = base().model_copy(update={"apply_filters_on": "decile_9"})
        with pytest.raises(ScreenQueryError, match="unknown bucket"):
            validate_definition(smuggled)
