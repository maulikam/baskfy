"""The committed reference export is an answer key; this asserts we can read it faithfully.

docs/13 §5 defines the full parity suite as Prompt 5's definition of done. What belongs *here*
is narrower: that the fixture is intact, that the loader projects it without losing precision,
and that the identities docs/13 §2 verified over the file still hold when we read it — because if
they do not, every later parity failure would be a loader bug rather than a factor bug.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from decile_core.reference_export import (
    EXPORT_COLUMNS,
    FACTOR_COLUMN_MAP,
    UNMAPPED_EXPORT_COLUMNS,
    ReferenceRows,
    read_export,
    to_rows,
)
from decile_core.universes import UNIVERSE_BY_SLUG


@pytest.fixture(scope="module")
def rows() -> ReferenceRows:
    return to_rows()


class TestTheFixtureItself:
    def test_row_count(self) -> None:
        """docs/13: 271 rows."""
        assert read_export().height == 271

    def test_column_count(self) -> None:
        """docs/13: 93 columns."""
        assert len(EXPORT_COLUMNS) == 93
        assert read_export().width == 93

    def test_single_trade_date(self, rows: ReferenceRows) -> None:
        assert rows.as_of.isoformat() == "2026-08-18"

    def test_every_column_is_either_mapped_or_explicitly_unmapped(self) -> None:
        """No export column may be silently ignored."""
        accounted = set(FACTOR_COLUMN_MAP) | UNMAPPED_EXPORT_COLUMNS
        flags = {c for c in EXPORT_COLUMNS if c.startswith("is_")}
        assert set(EXPORT_COLUMNS) - flags == accounted


class TestPrecisionSurvivesTheLoad:
    """docs/13 §4: volatility and beta keep full precision "because they feed divisions"."""

    def test_volatility_is_a_fraction_not_a_percentage(self, rows: ReferenceRows) -> None:
        """docs/13 §2 finding 4: the range is 0.179-0.618, so the UI multiplies by 100."""
        values = [v for r in rows.factors if isinstance(v := r["vol_12m"], Decimal)]
        assert values
        assert all(Decimal("0.05") < v < Decimal("2") for v in values)

    def test_volatility_keeps_more_than_four_decimals(self, rows: ReferenceRows) -> None:
        """The docs/04 DDL sketch's numeric(12,4) would truncate these; docs/13 §4 forbids that."""
        deep = [
            v
            for r in rows.factors
            if isinstance(v := r["vol_12m"], Decimal) and _decimal_places(v) > 4
        ]
        assert deep, "expected volatilities with more than 4 decimal places"

    def test_beta_keeps_ten_decimals(self, rows: ReferenceRows) -> None:
        cupid = next(r for r in rows.factors if r["symbol"] == "CUPID")
        assert cupid["beta_12m"] == Decimal("0.8412554591")

    def test_marketcap_is_an_integer_in_crore(self, rows: ReferenceRows) -> None:
        """docs/13 §2 finding 7: integers in the 4,901-971,984 range."""
        caps = [r["marketcap_cr"] for r in rows.factors if r["marketcap_cr"] is not None]
        assert all(isinstance(c, int) for c in caps)

    def test_median_volume_is_rupees(self, rows: ReferenceRows) -> None:
        """docs/13 §2 finding 6: CUPID 1,687,913,366 -> ₹168.79 cr."""
        cupid = next(r for r in rows.factors if r["symbol"] == "CUPID")
        assert cupid["median_vol_12m"] == 1_687_913_366


class TestIdentitiesFromDocs13:
    def test_sharpe_equals_return_over_volatility(self, rows: ReferenceRows) -> None:
        """docs/13 §2 finding 1: 1,355/1,355 cells match, max error 0.0051 (2-dp rounding)."""
        checked = 0
        for row in rows.factors:
            for window in (1, 3, 6, 9, 12):
                ret, vol, sharpe = (
                    row[f"ret_{window}m"],
                    row[f"vol_{window}m"],
                    row[f"sharpe_{window}m"],
                )
                if not all(isinstance(v, Decimal) for v in (ret, vol, sharpe)):
                    continue
                assert isinstance(ret, Decimal) and isinstance(vol, Decimal)
                assert isinstance(sharpe, Decimal)
                if vol == 0:
                    continue
                assert abs(ret / (vol * 100) - sharpe) <= Decimal("0.0051")
                checked += 1
        assert checked == 1355

    def test_away_from_high_equals_close_over_high(self, rows: ReferenceRows) -> None:
        """docs/13 §2 finding 2: 542/542 match, max error 0.005."""
        checked = 0
        for row in rows.factors:
            for high, away in (("high_1y", "away_high_1y"), ("high_ath", "away_high_ath")):
                close_value, high_value, away_value = row["close"], row[high], row[away]
                if not all(isinstance(v, Decimal) for v in (close_value, high_value, away_value)):
                    continue
                assert isinstance(close_value, Decimal) and isinstance(high_value, Decimal)
                assert isinstance(away_value, Decimal)
                if high_value == 0:
                    continue
                expected = (close_value / high_value - 1) * 100
                assert abs(expected - away_value) <= Decimal("0.005")
                checked += 1
        assert checked == 542

    def test_positive_days_denominators_are_the_recovered_window_lengths(
        self, rows: ReferenceRows
    ) -> None:
        """docs/13 §3: every value is an integer multiple of 1/N for N = 22/64/121/185/247.

        This is the property that proved the windows are calendar offsets with a *shared* start
        date, which is why decile_core.trading_calendar exists at all.
        """
        expected = {1: 22, 3: 64, 6: 121, 9: 185, 12: 247}
        for window, denominator in expected.items():
            values = [
                r[f"pos_days_{window}m"]
                for r in rows.factors
                if isinstance(r[f"pos_days_{window}m"], Decimal)
            ]
            assert values, f"no positive-days values for the {window}m window"
            step = Decimal(100) / denominator
            for value in values:
                assert isinstance(value, Decimal)
                multiples = value / step
                assert abs(multiples - round(multiples)) < Decimal("0.02"), (
                    f"{window}m value {value} is not a multiple of 1/{denominator}"
                )

    def test_index_union_identities_hold(self, rows: ReferenceRows) -> None:
        """docs/13 §2 finding 11 / docs/06: 0 violations in the export."""
        members = _members_by_universe(rows)
        assert members["nifty-500"] == (
            members["nifty-100"] | members["nifty-midcap-150"] | members["nifty-smallcap-250"]
        )
        assert members["nifty-large-mid-250"] == (
            members["nifty-100"] | members["nifty-midcap-150"]
        )
        assert members["nifty-mid-small-400"] == (
            members["nifty-midcap-150"] | members["nifty-smallcap-250"]
        )

    @pytest.mark.parametrize(
        ("subset", "superset"),
        [
            ("nifty-50", "nifty-100"),
            ("nifty-100", "nifty-200"),
            ("nifty-200", "nifty-500"),
            ("nifty-500", "nifty-total-market"),
            ("nifty-total-market", "nifty-allcap"),
            ("nifty-next-50", "nifty-100"),
            ("nifty-microcap-250", "nifty-total-market"),
        ],
    )
    def test_containment_chain_holds(self, rows: ReferenceRows, subset: str, superset: str) -> None:
        members = _members_by_universe(rows)
        assert members[subset] <= members[superset]

    def test_top_risk_flags_are_a_clean_rank_threshold(self, rows: ReferenceRows) -> None:
        """docs/13 §2 finding 10: within a universe, min flagged beta > max unflagged beta.

        Only a cut taken over the whole universe *before* filtering can produce that, which is
        why docs/06 stores it as a precomputed per-universe flag rather than a relative filter.
        """
        universe = UNIVERSE_BY_SLUG["nifty-total-market"]
        flagged: list[Decimal] = []
        unflagged: list[Decimal] = []
        for row in rows.factors:
            mask = row["universe_mask"]
            beta = row["beta_12m"]
            assert isinstance(mask, int)
            if not mask & universe.mask_value or not isinstance(beta, Decimal):
                continue
            top_beta = row["top_beta_mask"]
            assert isinstance(top_beta, int)
            (flagged if top_beta & universe.mask_value else unflagged).append(beta)
        assert flagged and unflagged
        assert min(flagged) > max(unflagged)


class TestProjection:
    def test_one_instrument_and_one_factor_row_per_export_row(self, rows: ReferenceRows) -> None:
        assert len(rows.instruments) == 271
        assert len(rows.factors) == 271

    def test_symbols_are_unique(self, rows: ReferenceRows) -> None:
        symbols = [i.symbol for i in rows.instruments]
        assert len(set(symbols)) == len(symbols)

    def test_etf_rows_are_typed_as_etfs(self, rows: ReferenceRows) -> None:
        """docs/04: instrument_type ∈ EQ | ETF | INDEX; the export's is_etf flag decides."""
        assert {i.instrument_type for i in rows.instruments} <= {"EQ", "ETF"}

    def test_membership_rows_agree_with_the_universe_mask(self, rows: ReferenceRows) -> None:
        """index_member_daily is the normalised truth; the mask is a denormalisation of it."""
        by_symbol: dict[str, int] = {}
        for membership in rows.memberships:
            bit = UNIVERSE_BY_SLUG[membership.universe_slug].mask_value
            by_symbol[membership.symbol] = by_symbol.get(membership.symbol, 0) | bit
        for row in rows.factors:
            symbol = str(row["symbol"])
            assert by_symbol.get(symbol, 0) == row["universe_mask"]


def _decimal_places(value: Decimal) -> int:
    """``Decimal.as_tuple().exponent`` is an int only for finite values; NaN/Inf give a string."""
    exponent = value.as_tuple().exponent
    return -exponent if isinstance(exponent, int) else 0


def _members_by_universe(rows: ReferenceRows) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for membership in rows.memberships:
        out.setdefault(membership.universe_slug, set()).add(membership.symbol)
    return out
