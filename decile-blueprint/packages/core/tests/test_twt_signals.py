"""Chartink's five lines, the three weekly closes, the month-3 low and the entry event.

``docs/twt/04`` §3, asserted one number at a time: each test moves exactly one value across exactly
one threshold, so a failure names the rule. The comparison senses are asserted on purpose — ``>``,
``>=`` and ``<=`` are Chartink's own, and a rule read one tick loose is a rule that fires on days it
should not.

The fixture is flat at ``base_close`` with exactly three weekly closes placed on it, so a test that
wants to break rule 3 moves one of three numbers rather than reasoning about where a week ended.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl
import pytest
from twt_fixtures import (
    calendar_for,
    drop_bar,
    flat_bars,
    last_session_of_week_before,
    sessions,
    set_bar,
    tight_bars,
    week_key,
    with_background,
)

from baskfy_core.twt.config import (
    DEFAULT_TWT_CONFIG,
    EntryConfig,
    ScanConfig,
    SignalState,
    TwtConfig,
)
from baskfy_core.twt.indicators import bucket_keys, with_twt_indicators
from baskfy_core.twt.signals import (
    detect_signals,
    entry_events,
    month_low_back,
    signal_mask,
    tight_state,
    weekly_closes,
    weekly_column,
    with_twt_columns,
)

COUNT = 260
DAYS = sessions(COUNT)
#: The signal session: the last of the frame. Late enough that the 50-session volume average and
#: the 20-session turnover average both exist.
WHEN = DAYS[-1]
SCAN = DEFAULT_TWT_CONFIG.scan
BAND = float(SCAN.tight_band_pct)


def evaluate(bars: pl.DataFrame, config: TwtConfig = DEFAULT_TWT_CONFIG) -> dict[str, object]:
    """One row of the full detection — the subject instrument on the signal session."""
    full = with_background(bars, count=COUNT)
    detected = with_twt_columns(full, calendar_for(full), config)
    return detected.filter((pl.col("date") == WHEN) & (pl.col("instrument_id") == 1)).to_dicts()[0]


def number(row: dict[str, object], column: str) -> float:
    """One numeric cell of a detected row.

    A frame cell is ``object`` to the type checker and a float in fact; naming that here keeps
    every comparison below readable and keeps the type-ignore escape hatch out of the tree
    (house rule 3; the hash is left off deliberately, as the house convention asks).
    """
    value = row[column]
    assert isinstance(value, (int, float)), f"{column} is {value!r}"
    return float(value)


def test_the_baseline_bar_is_a_state_and_an_entry() -> None:
    row = evaluate(tight_bars())
    assert row["tight"] is True
    assert row["above_month_low"] is True
    assert row["tight_state"] is True
    assert row["entry_event"] is True


class TestTheFiveChartinkLines:
    """``04`` §3.1. Line 4 — *market cap > 1* — is a no-op in Chartink and is not implemented."""

    def test_line_one_reads_the_exchange_print_and_is_strict(self) -> None:
        """A ₹28 name that a 1:2 split makes ₹56 in the adjusted series did not clear Chartink's
        ``Close > 30``, and the rule is about what the exchange printed."""
        assert evaluate(tight_bars(close_raw=31.0))["tight_state"] is True
        assert evaluate(tight_bars(close_raw=29.0))["tight_state"] is False
        assert evaluate(tight_bars(close_raw=float(SCAN.min_close_raw)))["tight_state"] is False

    def test_line_two_reads_the_adjusted_close_against_the_month_three_low(self) -> None:
        """``close >= 1.3 x month_low_back``, and the fixture's month-3 low is its flat base."""
        floor = 60.0 * float(SCAN.month_low_multiple)
        assert evaluate(tight_bars(close=floor))["above_month_low"] is True
        assert evaluate(tight_bars(close=floor - 0.05))["above_month_low"] is False

    def test_line_three_is_the_tight_band_and_it_is_not_strict(self) -> None:
        assert evaluate(tight_bars(week_before_close=92.7))["tight"] is True
        assert evaluate(tight_bars(week_before_close=93.0))["tight"] is False

    def test_a_range_exactly_at_the_band_is_tight(self) -> None:
        """``<=``, not ``<``. The boundary is not a value a float can be constructed at, so the
        test reads the range the panel computed and hands it back as the band: a range exactly at
        the threshold must pass, and the same range one ulp under the threshold must not."""
        bars = tight_bars(week_before_close=93.0)
        measured = number(evaluate(bars), "tight_range_pct")
        assert measured > BAND
        at = TwtConfig(scan=ScanConfig(tight_band_pct=Decimal(repr(measured))))
        assert evaluate(bars, at)["tight"] is True
        assert evaluate(bars)["tight"] is False

    def test_line_five_reads_the_volume_average_and_is_not_strict(self) -> None:
        floor = float(SCAN.min_vol_sma)
        assert evaluate(tight_bars(volume=floor))["tight_state"] is True
        assert evaluate(tight_bars(volume=floor - 1))["tight_state"] is False

    def test_an_etf_is_never_in_the_state(self) -> None:
        assert evaluate(tight_bars(is_etf=True))["tight_state"] is False

    def test_a_missing_input_is_not_in_the_state_and_there_is_no_assume_true_branch(self) -> None:
        """``04`` §3.1: a name with any input missing is **not** in the state. Here the 50-session
        volume average is short of its tolerance, so there is no ``vol_sma`` to compare."""
        bars = tight_bars()
        for day in DAYS[COUNT - SCAN.vol_sma_bars :][: SCAN.vol_sma_bars // 2]:
            bars = drop_bar(bars, day)
        row = evaluate(bars)
        assert row["vol_sma"] is None
        assert row["tight_state"] is False


class TestTheThreeWeeklyCloses:
    """``04`` §3.2 — the rule ``01`` §2 is about."""

    def test_w0_is_todays_close_because_the_current_week_is_a_partial_candle(self) -> None:
        row = evaluate(tight_bars(close=91.0, previous_week_close=90.0, week_before_close=90.5))
        assert number(row, weekly_column(0)) == 91.0

    def test_w1_and_w2_are_the_last_close_of_each_preceding_week_bucket(self) -> None:
        row = evaluate(tight_bars(close=91.0, previous_week_close=90.0, week_before_close=90.5))
        assert number(row, weekly_column(1)) == 90.0
        assert number(row, weekly_column(2)) == 90.5

    def test_the_last_close_of_a_week_is_its_last_non_null_close(self) -> None:
        """ "The last close" is what a screener sees: a name that did not print on Friday shows
        Thursday's close for that week, not a blank."""
        friday = last_session_of_week_before(DAYS, WHEN, 1)
        bars = drop_bar(tight_bars(close=91.0, previous_week_close=90.0), friday)
        assert number(evaluate(bars), weekly_column(1)) == 60.0

    def test_a_name_with_fewer_than_three_weekly_closes_is_not_tight(self) -> None:
        """Two of three weekly closes is not a 3 % range, and there is no partial reading."""
        short = flat_bars(count=4, start=DAYS[0], close=90.0)
        full = with_background(short, count=4, start=DAYS[0])
        detected = with_twt_columns(full, calendar_for(full))
        row = detected.filter(
            (pl.col("date") == sessions(4, DAYS[0])[-1]) & (pl.col("instrument_id") == 1)
        ).to_dicts()[0]
        assert row[weekly_column(2)] is None
        assert row["tight"] is False

    def test_the_range_is_max_over_min_and_needs_no_absolute_value(self) -> None:
        row = evaluate(tight_bars(close=90.0, previous_week_close=91.0, week_before_close=89.0))
        assert number(row, "tight_range_pct") == pytest.approx((91.0 / 89.0 - 1) * 100)


class TestTheYearBoundary:
    """``docs/twt/06`` TW1's named case, and the one that silently sorts wrong.

    ISO week 1 of the next year is **after** week 52 of the year before it. A week key built from
    the *calendar* year numbers 2024-12-30 as 202401 and sorts it before 2024-W52, which would hand
    the last sessions of December the previous January's closes — a tight range out of nothing.
    """

    #: A panel that ends inside ISO 2025-W01 while still being in calendar 2024. 2024-12-30 is a
    #: Monday and 2024-12-31 a Tuesday, and both belong to ISO week 2025-W01.
    START = dt.date(2024, 8, 1)
    COUNT = 109

    def days(self) -> list[dt.date]:
        return sessions(self.COUNT, self.START)

    def test_the_fixture_actually_straddles_the_boundary(self) -> None:
        """A fixture that did not cross the year would make every assertion below vacuous."""
        last = self.days()[-1]
        assert last == dt.date(2024, 12, 31)
        assert last.year == 2024
        assert last.isocalendar().year == 2025
        assert last.isocalendar().week == 1

    def test_the_week_key_never_goes_backwards_in_time(self) -> None:
        """The whole of the bug, stated as a property. A calendar-year key is not monotone: it
        numbers 2024-12-30 (202401) below 2024-12-27 (202452), and ``searchsorted`` over a
        non-monotone key answers a question nobody asked."""
        frame = pl.DataFrame({"date": self.days()}).with_columns(bucket_keys()[0])
        keys = frame["week_key"].to_list()
        assert keys == sorted(keys)
        assert keys[-1] == week_key(self.days()[-1]) == 202501

    def test_the_last_session_of_december_reads_decembers_two_earlier_weeks(self) -> None:
        days = self.days()
        last = days[-1]
        bars = tight_bars(
            count=self.COUNT,
            start=self.START,
            close=90.0,
            previous_week_close=91.0,
            week_before_close=89.0,
        )
        full = with_background(bars, count=self.COUNT, start=self.START)
        row = (
            with_twt_columns(full, calendar_for(full))
            .filter((pl.col("date") == last) & (pl.col("instrument_id") == 1))
            .to_dicts()[0]
        )
        assert row[weekly_column(1)] is not None, "week 2025-W01 was sorted before 2024-W52"
        assert number(row, weekly_column(0)) == 90.0
        assert number(row, weekly_column(1)) == 91.0
        assert number(row, weekly_column(2)) == 89.0
        assert last_session_of_week_before(days, last, 1) == dt.date(2024, 12, 27)
        assert last_session_of_week_before(days, last, 2) == dt.date(2024, 12, 20)


class TestTheMonthThreeLow:
    """``04`` §3.3. The minimum adjusted low over **every session of the calendar month** three
    months before the session's own month."""

    START = dt.date(2024, 11, 1)
    COUNT = 90

    def test_march_reads_december_across_the_year_boundary(self) -> None:
        """A month key of ``year x 12 + month - 1`` crosses December -> March by subtraction; any
        expression that subtracts from the *month number* has to special-case the year, and the
        one that forgets is silent."""
        days = sessions(self.COUNT, self.START)
        march = [day for day in days if day.year == 2025 and day.month == 3]
        december = [day for day in days if day.year == 2024 and day.month == 12]
        assert march and december
        bars = flat_bars(count=self.COUNT, start=self.START)
        bars = set_bar(bars, december[3], low=50.0)
        full = with_background(bars, count=self.COUNT, start=self.START)
        detected = month_low_back(
            weekly_closes(with_twt_indicators(full, calendar_for(full))),
        )
        for day in march:
            row = detected.filter(
                (pl.col("date") == day) & (pl.col("instrument_id") == 1)
            ).to_dicts()[0]
            assert number(row, "month_low_back") == 50.0, day

    def test_every_month_boundary_of_a_year_has_a_month_three_low(self) -> None:
        """``04`` §2.3's claim about ``bars_required``, walked rather than argued.

        Every session of the frame whose month-3 bucket **is in the panel** must have a value, and
        every session whose month-3 is not must have none. Walked over all 260 sessions rather than
        sampled, because the failure this guards against — a month boundary the key crosses wrongly
        — would show up on one month and no other.
        """
        bars = with_background(flat_bars(count=COUNT), count=COUNT)
        detected = month_low_back(weekly_closes(with_twt_indicators(bars, calendar_for(bars))))
        subject = detected.filter(pl.col("instrument_id") == 1).sort("date")
        present = set(subject["month_key"].to_list())
        back = DEFAULT_TWT_CONFIG.scan.month_low_months_back
        months_walked = set()
        for row in subject.iter_rows(named=True):
            expected = (row["month_key"] - back) in present
            assert (row["month_low_back"] is not None) is expected, row["date"]
            months_walked.add(row["month_key"])
        assert len(months_walked) >= 12, "the fixture must span a year of month boundaries"

    def test_a_session_whose_month_three_is_not_in_the_panel_has_no_value(self) -> None:
        bars = with_background(flat_bars(count=COUNT), count=COUNT)
        detected = month_low_back(weekly_closes(with_twt_indicators(bars, calendar_for(bars))))
        first = detected.filter(
            (pl.col("date") == DAYS[0]) & (pl.col("instrument_id") == 1)
        ).to_dicts()[0]
        assert first["month_low_back"] is None
        stated = tight_state(
            month_low_back(weekly_closes(with_twt_indicators(bars, calendar_for(bars))))
        )
        first_row = stated.filter((pl.col("date") == DAYS[0]) & (pl.col("instrument_id") == 1))
        assert first_row["tight_state"].item() is False


def state_frame(pattern: str, *, listed_from: int = 0) -> pl.DataFrame:
    """One instrument, one character a session: ``T`` in the state, ``F`` out of it.

    ``listed_from`` is the first session with a bar — everything before it is a densified blank, so
    a fresh listing can be written as ``state_frame("FFFT", listed_from=3)``.
    """
    days = sessions(len(pattern))
    return pl.DataFrame(
        {
            "instrument_id": [1] * len(pattern),
            "date": days,
            "close": [None if i < listed_from else 100.0 for i in range(len(pattern))],
            "tight_state": [char == "T" for char in pattern],
        },
        schema_overrides={"instrument_id": pl.Int64, "close": pl.Float64},
    )


def events(pattern: str, *, listed_from: int = 0, gap: int | None = None) -> list[bool]:
    config = DEFAULT_TWT_CONFIG
    if gap is not None:
        config = TwtConfig(entry=EntryConfig(entry_min_sessions_out=gap))
    return entry_events(state_frame(pattern, listed_from=listed_from), config)[
        "entry_event"
    ].to_list()


class TestTheEntryEvent:
    """``04`` §3.4. The state is true today **and** was false on each of the previous five
    sessions. About 50 names hold the state on an average day and the median stay is five sessions,
    so buying the state means buying the same name repeatedly; the tradable object is the entry."""

    def test_the_first_day_of_a_state_after_five_sessions_out_is_an_entry(self) -> None:
        assert events("TFFFFFT")[-1] is True

    def test_four_sessions_out_is_not_enough(self) -> None:
        assert events("TFFFFT")[-1] is False

    def test_the_second_day_of_a_state_is_not_an_entry(self) -> None:
        assert events("FFFFFFTT")[-2:] == [True, False]

    def test_the_gap_is_a_parameter_and_the_shipped_value_is_five(self) -> None:
        assert DEFAULT_TWT_CONFIG.entry.entry_min_sessions_out == 5
        assert events("TFFFFT", gap=4)[-1] is True

    def test_a_name_that_has_never_been_in_the_state_still_needs_the_history(self) -> None:
        """``sessions_out_before`` is null when the state was never true, which reads as "long
        enough out" — it is the *listing* clause that stops a six-session-old name firing."""
        assert events("FFFFFT")[-1] is True
        assert events("FFFFT")[-1] is False


class TestAFreshListingHasNoEntryEvent:
    """DECISIONS-TW **TW0.6**. "Was false for five sessions" is a claim about five sessions that
    exist, and a name whose first ever bar satisfies the scan has no base to be tight in.

    The research's implementation seeds its sessions-out counter at a large number and fires on a
    listing day. This one does not, and the difference is a decision rather than an accident."""

    def test_a_listing_day_that_satisfies_the_scan_is_not_an_entry(self) -> None:
        assert events("FFFFFFT", listed_from=6)[-1] is False

    def test_the_first_possible_entry_is_the_sixth_session_after_listing(self) -> None:
        for listed_from, expected in ((2, False), (1, True)):
            assert events("FFFFFFT", listed_from=listed_from)[-1] is expected

    def test_the_counter_is_recorded_so_a_past_session_can_be_re_read(self) -> None:
        frame = entry_events(state_frame("TFFFFFT"))
        assert frame["sessions_out_before"].to_list() == [None, 0, 1, 2, 3, 4, 5]
        assert frame["sessions_listed"].to_list() == [1, 2, 3, 4, 5, 6, 7]


class TestTheLiquidityFloorAndTheRanking:
    """``04`` §3.5 and §6.3."""

    def test_an_entry_below_the_floor_is_stored_as_scan_only_and_never_dropped(self) -> None:
        bars = with_background(tight_bars(), count=COUNT)
        detected = with_twt_columns(bars, calendar_for(bars))
        rows = detect_signals(detected, WHEN).to_dicts()
        assert len(rows) == 1
        assert rows[0]["signal_state"] == SignalState.SCAN_ONLY.value

    def test_an_entry_above_the_floor_is_a_signal(self) -> None:
        """The fixture turns over 90 x 100,000 = ₹90 lakh a session, so the floor is reached by
        raising the volume rather than by lowering the floor."""
        bars = with_background(tight_bars(volume=1_000_000.0), count=COUNT)
        detected = with_twt_columns(bars, calendar_for(bars))
        rows = detect_signals(detected, WHEN).to_dicts()
        assert rows[0]["signal_state"] == SignalState.SIGNAL.value
        assert signal_mask(detected.filter(pl.col("date") == WHEN)).sum() == 1

    def test_the_research_floor_is_reachable_only_by_passing_it(self) -> None:
        """DECISIONS-TW TW0.3: ₹2 crore exists for exactly one caller, TW2's goldens."""
        bars = with_background(tight_bars(volume=400_000.0), count=COUNT)
        detected = with_twt_columns(bars, calendar_for(bars))
        shipped = detect_signals(detected, WHEN).to_dicts()[0]
        research = detect_signals(
            detected, WHEN, floor_inr=DEFAULT_TWT_CONFIG.entry.research_min_turnover_inr
        ).to_dicts()[0]
        assert shipped["signal_state"] == SignalState.SCAN_ONLY.value
        assert research["signal_state"] == SignalState.SIGNAL.value

    def test_the_rank_key_is_the_signal_sessions_own_turnover(self) -> None:
        row = evaluate(tight_bars(volume=1_000_000.0))
        assert number(row, "turnover_inr") == 90.0 * 1_000_000.0
        bars = with_background(tight_bars(volume=1_000_000.0), count=COUNT)
        detected = with_twt_columns(bars, calendar_for(bars))
        ranked = detect_signals(detected, WHEN).to_dicts()[0]
        assert number(ranked, "rank_key") == number(ranked, "turnover_inr")
        assert number(ranked, "rank_key") != number(ranked, "turnover_avg_20")


class TestNoLookAhead:
    """House rule 5, at the only level a pure package can assert it.

    **Truncating the panel at session *t* must not change the answer for any session at or before
    *t*.** That is what "point-in-time" means for a detector: the signal a person could have read at
    the close of a Tuesday cannot depend on Wednesday's bar. ``04`` §11.3 is the same rule stated
    for the plan, and TW4 asserts it end to end by shifting the stored series; here it is the
    arithmetic alone.

    One honest caveat, and it is the reason this fixture prints every session: the thin-session rule
    of ``04`` §2.1 uses a **centred** rolling median, so the *calendar* near the end of a panel can
    legitimately change when more sessions arrive. That is a property of the calendar, not of the
    signal, and a fixture with a thin session in its last fortnight would be testing the wrong
    thing.
    """

    CARRIED = ("tight_state", "entry_event", "month_low_back", "tight_range_pct")

    def test_a_later_session_cannot_change_an_earlier_one(self) -> None:
        bars = with_background(tight_bars(volume=1_000_000.0), count=COUNT)
        whole = with_twt_columns(bars, calendar_for(bars))
        cut_at = DAYS[COUNT - 30]
        cut = bars.filter(pl.col("date") <= cut_at)
        truncated = with_twt_columns(cut, calendar_for(cut))
        keys = ["instrument_id", "date"]
        left = whole.filter(pl.col("date") <= cut_at).sort(keys).select([*keys, *self.CARRIED])
        right = truncated.sort(keys).select([*keys, *self.CARRIED])
        assert left.height == right.height
        assert left.equals(right)

    def test_the_fixtures_own_calendar_holds_no_thin_session(self) -> None:
        """So the assertion above is about the signal and not about a calendar that moved."""
        bars = with_background(tight_bars(), count=COUNT)
        assert calendar_for(bars).dropped == ()
