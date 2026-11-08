"""``docs/fno/04`` §1, §7, §10 — F1 exits, F2 stops, the GTT trigger, time exit, roll, pauses."""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection
from decimal import Decimal

import pytest

from baskfy_core.fno.condor import CondorStrikes
from baskfy_core.fno.config import CommonConfig, F1Config, F2Config, StopVolConfig
from baskfy_core.fno.exits import (
    ExitReason,
    book_paused,
    f1_exit,
    f1_paused,
    f2_exit,
    f2_initial_stop,
    f2_month_paused,
    f2_time_exit_session,
    f2_trail,
    gtt_trigger,
    stop_from_vol,
)
from baskfy_core.score import stop_from_vol as desk_stop


class DeskBand:
    """The desk's ``app/config.py`` values, shaped as ``score.ScoringConfig``."""

    EXCLUDED_SYMBOLS: Collection[str] = ()
    REJECT_SERIES: Collection[str] = ()
    MIN_MEDIAN_DAILY_VALUE: float = 0.0
    MAX_AWAY_FROM_HIGH: float = 0.0
    MAX_CIRCUITS_3M: float = 0.0
    PENALTY_CIRCUITS_1Y: float = 0.0
    STOP_VOL_MULT: float = 2.2
    STOP_MIN: float = 0.08
    STOP_MAX: float = 0.12

    def __init__(self) -> None:
        self.MOMENTUM_BLEND: dict[str, float] = {}
        self.SHARPE_BLEND: dict[str, float] = {}


F1, F2, COMMON, BAND = F1Config(), F2Config(), CommonConfig(), StopVolConfig()
STRUCT = CondorStrikes(Decimal(26100), Decimal(25800), Decimal(24200), Decimal(23900))
HARD = dt.date(2026, 11, 20)


def _f1(now: dt.datetime, cost: Decimal | None) -> ExitReason | None:
    found = f1_exit(
        now=now, hard_exit_date=HARD, close_cost=cost, entry_credit=Decimal(55),
        strikes=STRUCT, f1=F1, common=COMMON,
    )  # fmt: skip
    return None if found is None else found.reason


class TestF1:
    def test_nothing_mid_hold(self) -> None:
        assert _f1(dt.datetime(2026, 11, 10, 11, 0), Decimal(50)) is None

    def test_profit_take(self) -> None:
        assert _f1(dt.datetime(2026, 11, 10, 11, 0), Decimal("27.5")) is ExitReason.PROFIT_TAKE

    def test_loss_close(self) -> None:
        assert _f1(dt.datetime(2026, 11, 10, 11, 0), Decimal("137.5")) is ExitReason.LOSS_CLOSE

    def test_hard_exit_at_1500_on_e_minus_one(self) -> None:
        assert _f1(dt.datetime(2026, 11, 20, 14, 59), Decimal(50)) is None
        assert _f1(dt.datetime(2026, 11, 20, 15, 0), Decimal(50)) is ExitReason.HARD_EXIT
        assert _f1(dt.datetime(2026, 11, 20, 15, 0), None) is ExitReason.HARD_EXIT

    def test_late_exit_when_the_date_passed(self) -> None:
        assert _f1(dt.datetime(2026, 11, 23, 9, 15), Decimal(10)) is ExitReason.LATE_EXIT


class TestStopFromVol:
    def test_band_floor_and_ceiling(self) -> None:
        assert stop_from_vol(Decimal(1000), 0.05, BAND) == Decimal("920.0")  # 8 % floor
        assert stop_from_vol(Decimal(1000), 0.90, BAND) == Decimal("880.0")  # 12 % ceiling

    def test_inside_the_band(self) -> None:
        # 0.30 / √52 x 2.2 = 9.152 %
        assert stop_from_vol(Decimal(1000), 0.30, BAND) == Decimal("908.5")

    @pytest.mark.parametrize("vol", [0.0, 0.1, 0.25, 0.3, 0.33, 0.4, 0.6, 1.2])
    @pytest.mark.parametrize("price", [87.35, 1000.0, 2456.8, 24_999.95])
    def test_agrees_with_the_desks_formula(self, price: float, vol: float) -> None:
        expected = desk_stop(price, vol, DeskBand())
        assert float(stop_from_vol(Decimal(repr(price)), vol, BAND)) == expected


class TestF2Stops:
    def test_initial_stop_is_three_atr_below_entry(self) -> None:
        assert f2_initial_stop(Decimal(1000), Decimal(20), F2) == Decimal(940)

    def test_trail_follows_the_highest_close_and_is_never_lowered(self) -> None:
        stop = Decimal(940)
        stop = f2_trail(stop, Decimal(1030), Decimal(20), F2)
        assert stop == Decimal(970)
        assert f2_trail(stop, Decimal(1000), Decimal(20), F2) == Decimal(970)

    def test_trail_off_keeps_the_stop(self) -> None:
        off = F2Config(trail=False)
        assert f2_trail(Decimal(940), Decimal(1100), Decimal(20), off) == Decimal(940)

    def test_gtt_is_the_tighter_of_the_two(self) -> None:
        # 3 ATR stop 940 vs stop_from_vol 920 (8 %): 940 is higher, so tighter.
        assert gtt_trigger(Decimal(940), Decimal(1000), 0.05, BAND) == Decimal(940)
        # A wide 3 ATR stop (850) loses to the vol stop (880 at the 12 % ceiling).
        assert gtt_trigger(Decimal(850), Decimal(1000), 0.9, BAND) == Decimal("880.0")


class TestF2Exits:
    sessions = tuple(dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(80))

    def test_time_exit_is_the_fortieth_session_counting_entry(self) -> None:
        entry = self.sessions[5]
        assert f2_time_exit_session(self.sessions, entry, F2) == self.sessions[44]

    def test_stop_then_time_then_roll(self) -> None:
        now = dt.datetime(2026, 2, 1, 15, 0)
        day = now.date()

        def reason(
            price: int, time_exit: dt.date | None, roll: dt.date | None
        ) -> ExitReason | None:
            found = f2_exit(
                now=now, price=Decimal(price), stop=Decimal(940), time_exit_date=time_exit,
                roll_date=roll, common=COMMON,
            )  # fmt: skip
            return None if found is None else found.reason

        assert reason(939, day, day) is ExitReason.STOP
        assert reason(1000, day, day) is ExitReason.TIME_EXIT
        assert reason(1000, None, day) is ExitReason.ROLL
        assert reason(1000, None, None) is None

    def test_roll_waits_for_1500(self) -> None:
        found = f2_exit(
            now=dt.datetime(2026, 2, 1, 14, 59), price=Decimal(1000), stop=Decimal(940),
            time_exit_date=None, roll_date=dt.date(2026, 2, 1), common=COMMON,
        )  # fmt: skip
        assert found is None


class TestPauses:
    def test_f1_three_consecutive_at_or_below_minus_point_six(self) -> None:
        assert f1_paused([Decimal("0.2"), Decimal("-0.6"), Decimal("-0.73"), Decimal("-0.7")], F1)
        assert not f1_paused([Decimal("-0.7"), Decimal("-0.59"), Decimal("-0.7")], F1)
        assert not f1_paused([Decimal("-0.7"), Decimal("-0.7")], F1)

    def test_f2_month_at_minus_six_r(self) -> None:
        assert f2_month_paused([Decimal(-2), Decimal(-3), Decimal(-1)], F2)
        assert not f2_month_paused([Decimal(-2), Decimal(-3), Decimal("-0.99")], F2)

    def test_book_monthly_rupees(self) -> None:
        assert book_paused(Decimal(-75000), Decimal(75000))
        assert not book_paused(Decimal("-74999.99"), Decimal(75000))
