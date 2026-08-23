"""Curated-basket catalog metrics - docs/smallcase/04 sections 2-4 (SC2, SC-hardening).

Tests assert the *spec*, never current behaviour (CLAUDE.md house rule 2). Several of them
exist because the behaviour was measurably wrong and the number is now pinned:

* the history a basket is credited with is the history of the weights it held (section 4);
* a window called "1Y" spans a calendar year and compounds every return inside it (docs/05);
* a CAGR is annualised by the span it actually covers;
* a risk label is never published from a sample too short to carry one (section 3);
* NAV is rounded once, at write time (house rule 8);
* the return convention (price return, dividends excluded) is stated, not assumed.

``TestChainLinkedSeries.test_golden_nav_is_not_rounded_day_by_day`` **replaces** a test that
asserted the per-day ``money()`` rounding of the NAV loop. That test locked in the defect
house rule 8 forbids, so it was rewritten to assert the spec rather than deleted.
"""

from __future__ import annotations

import datetime as dt
import inspect
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from baskfy_core.curated_baskets import WEIGHT_QUANTIZE, assert_weights_sum_to_one
from baskfy_core.curated_metrics import (
    DIVIDENDS_INCLUDED,
    ESTIMATED_DIVIDEND_DRAG_PCT_PER_YEAR,
    MIN_BASKET_VOL_DAYS,
    NO_HEADLINE_LABEL,
    RETURN_CONVENTION,
    RETURN_CONVENTION_NOTE,
    TERCILE_SWITCH_PUBLISHED_COUNT,
    VOL_BUCKET_HIGH_FLOOR,
    VOL_BUCKET_MED_FLOOR,
    VOL_WINDOW_BARS,
    BasketVersion,
    absolute_return,
    annualized_volatility,
    basket_day_return,
    cagr,
    catalog_volatility,
    chain_link_nav,
    constituent_volatilities,
    constituent_weighted_volatility,
    daily_returns_for_symbol,
    headline_return,
    min_amount,
    nav_for_storage,
    resolve_nav_window,
    return_convention_fields,
    shares_at_amount,
    since_inception_anchor,
    since_inception_return,
    version_aware_nav,
    volatility_bucket,
    volatility_bucket_for_catalog,
    weights_on,
    whole_months_between,
    window_cagr,
    window_return,
)
from baskfy_core.gst import money


def _d(value: str) -> Decimal:
    return Decimal(value)


#: A synthetic NSE-shaped calendar: weekdays minus fifteen holidays a year, which lands on
#: ~246 trading days - the length docs/13 section 3 recovered for the 12-month window (247).
_HOLIDAY_DAYS: frozenset[tuple[int, int]] = frozenset(
    {(month, 15) for month in range(1, 13)} | {(1, 26), (8, 14), (10, 2)}
)


def _is_trading_day(day: dt.date) -> bool:
    return day.weekday() < 5 and (day.month, day.day) not in _HOLIDAY_DAYS


def _trading_days(start: dt.date, count: int) -> list[dt.date]:
    days: list[dt.date] = []
    day = start
    while len(days) < count:
        if _is_trading_day(day):
            days.append(day)
        day += dt.timedelta(days=1)
    return days


def _compound(rate: Decimal, periods: int) -> Decimal:
    out = Decimal("1")
    for _ in range(periods):
        out = out * (Decimal("1") + rate)
    return out


def _flat_nav(days: list[dt.date], rate: Decimal) -> list[tuple[dt.date, Decimal]]:
    """A NAV series growing at a constant daily *rate*, base 100 on ``days[0]``."""
    nav = Decimal("100")
    out = [(days[0], nav)]
    for day in days[1:]:
        nav = nav * (Decimal("1") + rate)
        out.append((day, nav))
    return out


class TestMinAmountSpec:
    def test_equal_weight_two_names(self) -> None:
        # max(100/0.5, 200/0.5) = 400 -> ceil 400; each gets floor(400*0.5/price) >= 1.
        amount = min_amount([_d("100"), _d("200")], [_d("0.5"), _d("0.5")])
        assert amount == _d("400")
        shares = shares_at_amount(amount, [_d("100"), _d("200")], [_d("0.5"), _d("0.5")])
        assert shares == [2, 1]

    def test_expensive_low_weight_dominates(self) -> None:
        # price/weight = 500/0.1 = 5000.
        amount = min_amount(
            [_d("100"), _d("500")],
            [_d("0.9"), _d("0.1")],
        )
        assert amount == _d("5000")
        shares = shares_at_amount(amount, [_d("100"), _d("500")], [_d("0.9"), _d("0.1")])
        assert all(s >= 1 for s in shares)

    def test_lot_size_scales_the_bound(self) -> None:
        unit = min_amount([_d("100")], [_d("1")], lot_size=1)
        lots = min_amount([_d("100")], [_d("1")], lot_size=5)
        assert lots == unit * 5

    def test_rejects_non_positive_price(self) -> None:
        with pytest.raises(ValueError, match="price"):
            min_amount([_d("0")], [_d("1")])

    @given(
        n=st.integers(min_value=2, max_value=8),
        seed=st.integers(min_value=1, max_value=10_000),
    )
    def test_property_min_amount_buys_at_least_one_share(self, n: int, seed: int) -> None:
        """docs/smallcase/04 section 2: buying exactly min_amount yields >=1 share of every name.

        Weights are quantized to 4 dp (``cb_constituent.weight`` storage) before the bound
        is computed - the same inputs the EOD job and plan preview will pass.
        """
        prices = [Decimal(10 + ((seed * (i + 3)) % 500)) for i in range(n)]
        raw = [Decimal(1 + ((seed * (i + 7)) % 20)) for i in range(n)]
        total = sum(raw)
        weights = [(w / total).quantize(WEIGHT_QUANTIZE) for w in raw]
        # Absorb residual on the last weight so the domain contract holds.
        residual = Decimal("1.0000") - sum(weights)
        weights[-1] = (weights[-1] + residual).quantize(WEIGHT_QUANTIZE)
        assert_weights_sum_to_one(weights)
        amount = min_amount(prices, weights)
        shares = shares_at_amount(amount, prices, weights)
        assert all(s >= 1 for s in shares), (amount, prices, weights, shares)


class TestVolatilityBucket:
    def test_pack1_boundaries(self) -> None:
        assert volatility_bucket(_d("0.1499")) == "LOW"
        assert volatility_bucket(VOL_BUCKET_MED_FLOOR) == "MED"
        assert volatility_bucket(_d("0.2499")) == "MED"
        assert volatility_bucket(VOL_BUCKET_HIGH_FLOOR) == "HIGH"
        assert volatility_bucket(_d("0.50")) == "HIGH"

    def test_fixed_until_twelve_published(self) -> None:
        assert (
            volatility_bucket_for_catalog(
                _d("0.20"),
                published_count=TERCILE_SWITCH_PUBLISHED_COUNT - 1,
                peer_vols=[_d("0.10")] * 11,
            )
            == "MED"
        )

    def test_a_single_peer_does_not_crash(self) -> None:
        """A one-element tercile set is degenerate, not an ``IndexError``."""
        assert (
            volatility_bucket_for_catalog(
                _d("0.30"),
                published_count=TERCILE_SWITCH_PUBLISHED_COUNT,
                peer_vols=[_d("0.20")],
            )
            == "HIGH"
        )
        assert (
            volatility_bucket_for_catalog(
                _d("0.10"),
                published_count=TERCILE_SWITCH_PUBLISHED_COUNT,
                peer_vols=[_d("0.20")],
            )
            == "LOW"
        )

    def test_a_tie_on_a_tercile_boundary_takes_the_higher_bucket(self) -> None:
        """The docstring's promise, now the code's behaviour: ties go up, as at 15% and 25%."""
        peers = [_d("0.10")] * 4 + [_d("0.20")] * 4 + [_d("0.30")] * 4
        published = TERCILE_SWITCH_PUBLISHED_COUNT
        assert (
            volatility_bucket_for_catalog(_d("0.05"), published_count=published, peer_vols=peers)
            == "LOW"
        )
        assert (
            volatility_bucket_for_catalog(_d("0.10"), published_count=published, peer_vols=peers)
            == "MED"
        )
        assert (
            volatility_bucket_for_catalog(_d("0.20"), published_count=published, peer_vols=peers)
            == "HIGH"
        )

    def test_empty_peer_set_falls_back_to_fixed_thresholds(self) -> None:
        assert (
            volatility_bucket_for_catalog(
                _d("0.30"), published_count=TERCILE_SWITCH_PUBLISHED_COUNT, peer_vols=[]
            )
            == "HIGH"
        )


class TestChainLinkedSeries:
    def test_golden_nav_is_not_rounded_day_by_day(self) -> None:
        """House rule 8: round at write time. The NAV inside the loop is never written.

        Replaces the earlier golden, which asserted 101.00 / 100.50 / 102.51 - the per-day
        ``money()`` quantisation. Those intermediates were the defect, so the spec value is the
        exact product, and :func:`nav_for_storage` is where a stored NAV gets its 2 dp.
        """
        dates = [dt.date(2024, 1, d) for d in (2, 3, 4)]
        rets = [_d("0.01"), _d("-0.005"), _d("0.02")]
        series = chain_link_nav(dates, rets, base=_d("100"))
        assert series[0] == (dates[0], _d("101.00"))
        assert series[1][1] == _d("100.49500")  # NOT 100.50: nothing has been written yet
        assert series[2][1] == _d("102.5049000")
        assert nav_for_storage(series[2][1]) == _d("102.50")

    def test_per_day_rounding_drifts_the_index(self) -> None:
        """The measured defect: quantising each day moves the index by basis points.

        1260 trading days of +-1.2% moves - an ordinary equity basket - drift 41 bps when the
        NAV is rounded to paise day. Production measured -25.5 to +29.8 bps over the same
        length, and 0.06pp on the 5Y CAGR that came out of it.
        """
        days = _trading_days(dt.date(2018, 1, 1), 1261)
        rets = [_d("0.0123") if i % 2 else _d("-0.0119") for i in range(len(days) - 1)]
        unrounded = chain_link_nav(days[1:], rets, base=_d("100"))[-1][1]
        rounded = _d("100")
        exact = _d("100")
        for ret in rets:
            rounded = money(rounded * (Decimal("1") + ret))
            exact = exact * (Decimal("1") + ret)
        assert unrounded == exact
        assert rounded != exact
        drift_bps = abs(rounded - exact) / exact * _d("10000")
        assert drift_bps > _d("20")

    def test_absolute_and_cagr(self) -> None:
        assert absolute_return(_d("100"), _d("110")) == _d("10.00")
        assert cagr(nav_start=_d("100"), nav_end=_d("121"), years=_d("2")) == _d("10.00")

    def test_basket_day_return_equal_weight(self) -> None:
        ret = basket_day_return(
            {"AAA": _d("0.5"), "BBB": _d("0.5")},
            {"AAA": _d("100"), "BBB": _d("200")},
            {"AAA": _d("110"), "BBB": _d("200")},
        )
        assert ret == _d("0.05")  # 0.5*0.10 + 0.5*0


def _two_version_prices(
    days: list[dt.date], loser_rate: Decimal, winner_rate: Decimal
) -> dict[dt.date, dict[str, Decimal]]:
    prices: dict[dt.date, dict[str, Decimal]] = {}
    loser = _d("100")
    winner = _d("100")
    for index, day in enumerate(days):
        if index:
            loser = loser * (Decimal("1") + loser_rate)
            winner = winner * (Decimal("1") + winner_rate)
        prices[day] = {"LOSER": loser, "WINNER": winner}
    return prices


class TestVersionAwareChainLinking:
    """docs/smallcase/04 section 4 + house rule 5: history belongs to the weights that held it."""

    def test_weights_on_walks_versions_forward(self) -> None:
        versions = [
            BasketVersion(dt.date(2024, 1, 2), {"AAA": _d("1")}),
            BasketVersion(dt.date(2024, 6, 3), {"BBB": _d("1")}),
        ]
        assert weights_on(versions, dt.date(2023, 12, 31)) is None
        assert weights_on(versions, dt.date(2024, 1, 2)) == {"AAA": _d("1")}
        assert weights_on(versions, dt.date(2024, 6, 2)) == {"AAA": _d("1")}
        assert weights_on(versions, dt.date(2024, 6, 3)) == {"BBB": _d("1")}
        assert weights_on(versions, dt.date(2025, 1, 1)) == {"BBB": _d("1")}

    def test_nav_chains_across_the_version_boundary(self) -> None:
        days = _trading_days(dt.date(2020, 1, 1), 500)
        loser_rate, winner_rate = _d("-0.001"), _d("0.003")
        prices = _two_version_prices(days, loser_rate, winner_rate)
        versions = [
            BasketVersion(days[0], {"LOSER": _d("1")}),
            BasketVersion(days[250], {"WINNER": _d("1")}),
        ]
        nav = version_aware_nav(prices, versions)

        assert nav.points[0] == (days[0], _d("100"))
        assert len(nav.points) == len(days)
        assert len(nav.daily_returns) == len(days) - 1
        expected = _d("100") * _compound(loser_rate, 249) * _compound(winner_rate, 250)
        assert abs(nav.points[-1][1] - expected) < _d("0.0000001")

    def test_replaying_todays_weights_over_history_overstates_the_return(self) -> None:
        """The measured defect: one weight map applied to five years it never held.

        The naive series below is what ``_latest_version`` produced - today's winners replayed
        over the whole window. It is not a rounding difference; it is a different basket.
        """
        days = _trading_days(dt.date(2020, 1, 1), 500)
        loser_rate, winner_rate = _d("-0.001"), _d("0.003")
        prices = _two_version_prices(days, loser_rate, winner_rate)
        versions = [
            BasketVersion(days[0], {"LOSER": _d("1")}),
            BasketVersion(days[250], {"WINNER": _d("1")}),
        ]
        chained = version_aware_nav(prices, versions)
        naive = version_aware_nav(prices, [BasketVersion(days[0], {"WINNER": _d("1")})])

        chained_pct = absolute_return(chained.points[0][1], chained.points[-1][1])
        naive_pct = absolute_return(naive.points[0][1], naive.points[-1][1])
        assert naive_pct > chained_pct
        assert naive_pct - chained_pct > _d("50")

    def test_no_history_before_the_first_version(self) -> None:
        """A basket that did not exist has no returns - it does not borrow today's."""
        days = _trading_days(dt.date(2020, 1, 1), 60)
        prices = _two_version_prices(days, _d("-0.001"), _d("0.003"))
        nav = version_aware_nav(prices, [BasketVersion(days[40], {"WINNER": _d("1")})])
        assert nav.points[0][0] == days[40]
        assert len(nav.points) == 20

    def test_no_versions_means_no_series(self) -> None:
        days = _trading_days(dt.date(2020, 1, 1), 10)
        prices = _two_version_prices(days, _d("0"), _d("0"))
        assert version_aware_nav(prices, []).points == ()
        assert version_aware_nav({}, [BasketVersion(days[0], {"WINNER": _d("1")})]).points == ()


class TestCalendarWindows:
    """docs/05 section Notation: windows are calendar offsets snapped to trading days."""

    def test_one_year_window_spans_a_calendar_year_not_252_bars(self) -> None:
        days = _trading_days(dt.date(2018, 1, 1), 900)
        points = _flat_nav(days, _d("0.0005"))
        window = resolve_nav_window(points, months=12)
        assert window is not None
        span = (window.end_date - window.start_date).days
        assert 365 <= span <= 372, span
        # The 252-bar convention this replaced reaches strictly further back than a year:
        # on the real NSE calendar it landed 373-378 days back and called it "1Y".
        assert (window.end_date - days[-253]).days > span

    def test_window_return_compounds_every_day_in_the_window(self) -> None:
        """``navs[-N]`` against ``navs[-1]`` compounds N-1 returns; the window has N."""
        rate = _d("0.001")
        days = _trading_days(dt.date(2018, 1, 1), 400)
        points = _flat_nav(days, rate)
        window = resolve_nav_window(points, months=1)
        assert window is not None
        n = window.trading_days
        expected = (_compound(rate, n) - Decimal("1")) * Decimal("100")
        off_by_one = (_compound(rate, n - 1) - Decimal("1")) * Decimal("100")
        value = window_return(points, months=1)
        assert value == expected.quantize(_d("0.01"))
        assert value != off_by_one.quantize(_d("0.01"))

    def test_cagr_is_annualised_by_the_measured_span(self) -> None:
        """A 3Y slice spans ~3.01 years. Passing a nominal 3 biases the rate upward."""
        days = _trading_days(dt.date(2015, 1, 1), 1500)
        points = _flat_nav(days, _d("0.0004"))
        window = resolve_nav_window(points, months=36)
        assert window is not None
        assert _d("3.00") <= window.years <= _d("3.05")
        measured = cagr(nav_start=window.nav_start, nav_end=window.nav_end, years=window.years)
        nominal = cagr(nav_start=window.nav_start, nav_end=window.nav_end, years=_d("3"))
        assert window_cagr(points, months=36) == measured
        assert nominal is not None and measured is not None
        assert nominal > measured

    def test_a_window_the_series_does_not_span_is_none(self) -> None:
        days = _trading_days(dt.date(2023, 1, 2), 100)
        points = _flat_nav(days, _d("0.001"))
        assert window_return(points, months=12) is None
        assert window_cagr(points, months=60) is None
        assert resolve_nav_window(points[:1], months=1) is None

    def test_a_window_starting_on_the_first_bar_has_no_base_and_is_none(self) -> None:
        """The base of an N-day window is the bar before it. Without that bar there is no window."""
        days = _trading_days(dt.date(2023, 1, 2), 10)
        points = _flat_nav(days, _d("0.001"))
        # One calendar month back lands before the series starts: no base bar, no window.
        assert window_return(points, months=1) is None


class TestSinceInception:
    def test_anchored_to_launched_at_not_the_fetch_window(self) -> None:
        days = _trading_days(dt.date(2014, 1, 1), 2000)
        points = _flat_nav(days, _d("0.0003"))
        launched_at = days[0]
        anchored = since_inception_return(points, launched_at=launched_at)
        five_year_slice = points[-1200:]
        truncated = since_inception_return(five_year_slice, launched_at=five_year_slice[0][0])
        assert anchored == absolute_return(points[0][1], points[-1][1])
        assert anchored is not None and truncated is not None
        assert anchored > truncated

    def test_launch_after_the_series_starts_moves_the_anchor(self) -> None:
        days = _trading_days(dt.date(2019, 1, 1), 400)
        points = _flat_nav(days, _d("0.001"))
        launched_at = days[200]
        assert since_inception_return(points, launched_at=launched_at) == absolute_return(
            points[200][1], points[-1][1]
        )

    def test_no_launch_date_anchors_on_the_first_version(self) -> None:
        days = _trading_days(dt.date(2019, 1, 1), 50)
        points = _flat_nav(days, _d("0.001"))
        assert since_inception_return(points, launched_at=None) == absolute_return(
            points[0][1], points[-1][1]
        )

    def test_the_anchor_is_the_span_the_label_must_name(self) -> None:
        """A basket launched before its price history begins is measured - and labelled -
        from where the history begins, not from its nominal launch."""
        days = _trading_days(dt.date(2019, 1, 1), 400)
        points = _flat_nav(days, _d("0.001"))
        launched_at = dt.date(2010, 1, 1)
        anchor = since_inception_anchor(points, launched_at=launched_at)
        assert anchor == days[0]
        assert whole_months_between(anchor, days[-1]) < whole_months_between(launched_at, days[-1])
        assert since_inception_anchor(points, launched_at=days[200]) == days[200]

    def test_launch_after_the_last_bar_is_none(self) -> None:
        days = _trading_days(dt.date(2019, 1, 1), 50)
        points = _flat_nav(days, _d("0.001"))
        assert since_inception_return(points, launched_at=dt.date(2030, 1, 1)) is None
        assert since_inception_return(points[:1], launched_at=days[0]) is None
        assert since_inception_anchor(points, launched_at=dt.date(2030, 1, 1)) is None


class TestAnnualizedVol:
    def test_constant_series_is_zero(self) -> None:
        assert annualized_volatility([_d("0.01")] * 5) == _d("0")

    def test_too_short_is_none(self) -> None:
        assert annualized_volatility([_d("0.01")]) is None


class TestCatalogVolatility:
    """docs/smallcase/04 section 3 - what may be published, and from what."""

    def test_two_days_of_returns_publish_no_risk_label(self) -> None:
        """The measured defect: two +2% days produced vol 0.0112 and a published LOW.

        The arithmetic still yields a number - that is what made it dangerous - so the guard
        lives where the label is decided, not in the std-dev.
        """
        sample = [_d("0.02"), _d("0.02")]
        assert annualized_volatility(sample) == _d("0")
        assert catalog_volatility(basket_daily_returns=sample) is None
        assert catalog_volatility(basket_daily_returns=[_d("0.02"), _d("0.05")]) is None

    def test_full_history_between_sixty_days_and_a_year(self) -> None:
        sample = [_d("0.01") if i % 2 else _d("-0.008") for i in range(MIN_BASKET_VOL_DAYS)]
        result = catalog_volatility(basket_daily_returns=sample)
        assert result is not None
        assert result.basis == "BASKET_FULL_HISTORY"
        assert result.value == annualized_volatility(sample)

    def test_trailing_252_days_once_the_series_is_long_enough(self) -> None:
        sample = [_d("0.02") if i % 3 else _d("-0.015") for i in range(400)]
        result = catalog_volatility(basket_daily_returns=sample)
        assert result is not None
        assert result.basis == "BASKET_252D"
        assert result.value == annualized_volatility(sample[-VOL_WINDOW_BARS:])

    def test_below_sixty_days_uses_the_constituent_weighted_fallback(self) -> None:
        result = catalog_volatility(
            basket_daily_returns=[_d("0.01")] * 10,
            weights={"AAA": _d("0.6"), "BBB": _d("0.4")},
            constituent_vols={"AAA": _d("0.30"), "BBB": _d("0.10")},
        )
        assert result is not None
        assert result.basis == "CONSTITUENT_WEIGHTED"
        assert result.value == _d("0.2200000000")
        assert result.bucket(published_count=1) == "MED"

    def test_no_fallback_data_means_no_value_and_no_bucket(self) -> None:
        assert (
            catalog_volatility(
                basket_daily_returns=[_d("0.01")] * 10,
                weights={"AAA": _d("1")},
                constituent_vols={},
            )
            is None
        )

    def test_constituent_weighted_volatility_renormalises_over_known_names(self) -> None:
        value = constituent_weighted_volatility(
            {"AAA": _d("0.5"), "BBB": _d("0.25"), "CCC": _d("0.25")},
            {"AAA": _d("0.20"), "BBB": _d("0.40")},
        )
        # CCC has no vol: the average is over AAA+BBB, renormalised to 0.75.
        assert value == _d("0.2666666667")
        assert constituent_weighted_volatility({"AAA": _d("1")}, {}) is None

    def test_constituent_vols_skip_series_too_short_to_measure(self) -> None:
        days = _trading_days(dt.date(2021, 1, 1), 80)
        prices: dict[dt.date, dict[str, Decimal]] = {}
        long_price = _d("100")
        short_price = _d("100")
        for index, day in enumerate(days):
            long_price = long_price * (_d("1.01") if index % 2 else _d("0.995"))
            row = {"LONG": long_price}
            if index >= 60:
                short_price = short_price * _d("1.002")
                row["SHORT"] = short_price
            prices[day] = row
        vols = constituent_volatilities(prices, ["LONG", "SHORT"])
        assert "LONG" in vols
        assert "SHORT" not in vols
        assert len(daily_returns_for_symbol(prices, "LONG")) == len(days) - 1


class TestHeadlineReturn:
    """docs/smallcase/04 section 4 - the label always names the window it measured."""

    def test_five_year_basket_shows_the_five_year_cagr(self) -> None:
        headline = headline_return(
            months_available=61,
            ret_1m=_d("1"),
            ret_6m=_d("5"),
            ret_1y=_d("10"),
            cagr_3y=_d("12"),
            cagr_5y=_d("14"),
            since_inception_pct=_d("120"),
        )
        assert headline.label == "5Y CAGR"
        assert headline.value_pct == _d("14")

    def test_a_seven_month_basket_cannot_show_the_one_month_number(self) -> None:
        """The measured defect: a 210-day-old basket rendered "6M returns" over ret_1m."""
        headline = headline_return(
            months_available=7,
            ret_1m=_d("1.5"),
            ret_6m=_d("9"),
            ret_1y=None,
            cagr_3y=None,
            cagr_5y=None,
            since_inception_pct=_d("11"),
        )
        assert headline.label == "7M returns"
        assert headline.value_pct == _d("11")
        assert headline.value_pct != _d("1.5")

    def test_every_window_value_is_required(self) -> None:
        """The contract that makes a wrong label impossible, asserted on the signature.

        The old shape defaulted ``months_available`` to ``None`` and had no ``ret_6m`` or
        since-inception argument at all, so a caller that passed neither still got a "6M
        returns" label - over the 21-day number. Every window is now a required keyword: a
        caller that cannot supply the value for a window cannot be given its label.
        """
        params = inspect.signature(headline_return).parameters
        assert set(params) == {
            "months_available",
            "ret_1m",
            "ret_6m",
            "ret_1y",
            "cagr_3y",
            "cagr_5y",
            "since_inception_pct",
        }
        for name, param in params.items():
            assert param.kind is inspect.Parameter.KEYWORD_ONLY, name
            assert param.default is inspect.Parameter.empty, name

    def test_falls_back_to_the_longest_window_actually_spanned(self) -> None:
        headline = headline_return(
            months_available=40,
            ret_1m=_d("1"),
            ret_6m=_d("5"),
            ret_1y=_d("10"),
            cagr_3y=None,
            cagr_5y=None,
            since_inception_pct=_d("30"),
        )
        assert headline.label == "1Y returns"
        assert headline.value_pct == _d("10")

    def test_no_window_is_no_label(self) -> None:
        headline = headline_return(
            months_available=0,
            ret_1m=None,
            ret_6m=None,
            ret_1y=None,
            cagr_3y=None,
            cagr_5y=None,
            since_inception_pct=None,
        )
        assert headline.label == NO_HEADLINE_LABEL
        assert headline.value_pct is None

    def test_negative_age_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="months_available"):
            headline_return(
                months_available=-1,
                ret_1m=None,
                ret_6m=None,
                ret_1y=None,
                cagr_3y=None,
                cagr_5y=None,
                since_inception_pct=None,
            )

    def test_whole_months_between(self) -> None:
        assert whole_months_between(dt.date(2024, 1, 15), dt.date(2024, 8, 14)) == 6
        assert whole_months_between(dt.date(2024, 1, 15), dt.date(2024, 8, 15)) == 7
        assert whole_months_between(dt.date(2024, 8, 15), dt.date(2024, 1, 15)) == 0


class TestReturnConventionDisclosure:
    """A6: the catalog's returns exclude dividends, and now say so."""

    def test_convention_is_machine_readable(self) -> None:
        fields = return_convention_fields()
        assert RETURN_CONVENTION == "PRICE_RETURN"
        assert DIVIDENDS_INCLUDED is False
        assert fields["return_convention"] == "PRICE_RETURN"
        # A real boolean, not the string "false" — see TestDisclosureIsMachineReadable.
        assert fields["dividends_included"] is False
        note = fields["return_convention_note"]
        assert isinstance(note, str)
        assert "dividend" in note.lower()
        assert str(ESTIMATED_DIVIDEND_DRAG_PCT_PER_YEAR) in RETURN_CONVENTION_NOTE


class TestDisclosureIsMachineReadable:
    """A6 — the disclosure field must not lie to a truthiness check."""

    def test_dividends_included_is_a_real_boolean(self) -> None:
        """The string "false" is truthy in JavaScript.

        A UI writing ``if (metrics.dividends_included)`` against ``"false"`` would render
        "dividends included" on a series that excludes them — the disclosure stating the
        opposite of what it exists to disclose.
        """
        fields = return_convention_fields()
        value = fields["dividends_included"]
        assert isinstance(value, bool), f"expected a bool, got {type(value).__name__}"
        assert value is False
        assert bool(value) is False

    def test_the_note_names_the_magnitude_and_its_source(self) -> None:
        note = return_convention_fields()["return_convention_note"]
        assert isinstance(note, str)
        assert "1.2" in note
        assert "M39.3" in note
