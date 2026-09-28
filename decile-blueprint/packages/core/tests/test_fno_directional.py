"""F3, the directional index credit spread — ``04`` §11 as tests (``gates/f3-1-core.md``).

Maulik, 28 Sep 2026 (DECISIONS-FO M.5). Every rule of ``baskfy_core.fno.directional`` against
the spec, never against what the code happens to do.
"""

from __future__ import annotations

import ast
import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from baskfy_core.fno import directional as D
from baskfy_core.fno.config import (
    DEFAULT_FNO_CEILINGS,
    DEFAULT_FNO_CONFIG,
    F3Config,
    FoSleeve,
    FoSleeveGroup,
    PlanKind,
    PlanState,
    Structure,
    f3_sleeve_for,
    group_of,
)
from baskfy_core.fno.sizing import FoSizing
from baskfy_core.options.config import Mode, OptionType

F3 = DEFAULT_FNO_CONFIG.f3
COMMON = DEFAULT_FNO_CONFIG.common
CEIL = DEFAULT_FNO_CEILINGS
ROOT = Path(__file__).resolve().parents[4]  # the Baskfy root, above decile-blueprint
D_ = Decimal


def _sessions(count: int, end: dt.date = dt.date(2026, 9, 25)) -> list[dt.date]:
    days: list[dt.date] = []
    day = end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= dt.timedelta(days=1)
    return sorted(days)


def _daily(closes: Sequence[int | str], *, spread: int = 20) -> list[D.DailyBar]:
    days = _sessions(len(closes))
    bars = []
    for day, close in zip(days, closes, strict=True):
        c = D_(str(close))
        bars.append(D.DailyBar(day, c, c + spread, c - spread, c))
    return bars


def _uptrend(n: int = 70) -> list[D.DailyBar]:
    """A rising series with a swing low a week ago, so support sits below the close."""
    closes: list[int] = []
    level = 24000
    for i in range(n):
        level += 20
        closes.append(level - (300 if i == n - 8 else 0))  # the dip: a pivot low
    return _daily(closes)


def _downtrend(n: int = 70) -> list[D.DailyBar]:
    closes: list[int] = []
    level = 26000
    for i in range(n):
        level -= 20
        closes.append(level + (300 if i == n - 8 else 0))  # the bounce: a pivot high
    return _daily(closes)


# ================================================================================================
# Levels
# ================================================================================================


class TestLevels:
    def test_levels_need_the_lookback(self) -> None:
        assert D.levels(_uptrend(F3.pivot_lookback - 1), F3) is None
        assert D.levels(_uptrend(F3.pivot_lookback), F3) is not None

    def test_support_is_the_highest_pivot_low_below_the_close(self) -> None:
        bars = _uptrend()
        lv = D.levels(bars, F3)
        assert lv is not None
        dip = bars[-8]
        assert lv.support == dip.low
        assert lv.support < lv.close
        assert lv.resistance is None, "nothing above the close has turned the market"

    def test_resistance_is_the_lowest_pivot_high_above_the_close(self) -> None:
        bars = _downtrend()
        lv = D.levels(bars, F3)
        assert lv is not None
        assert lv.resistance == bars[-8].high
        assert lv.support is None

    def test_a_pivot_needs_width_bars_on_both_sides(self) -> None:
        bars = _uptrend()
        highs, lows = D.pivots(bars[-F3.pivot_lookback :], F3.pivot_width)
        newest = max(p.date for p in (*highs, *lows))
        assert newest <= bars[-1 - F3.pivot_width].date

    def test_the_weekly_range_is_the_last_five_sessions(self) -> None:
        bars = _uptrend()
        lv = D.levels(bars, F3)
        assert lv is not None
        week = bars[-F3.weekly_sessions :]
        assert (lv.weekly_high, lv.weekly_low) == (
            max(b.high for b in week),
            min(b.low for b in week),
        )

    def test_the_trend_average_is_the_twenty_session_mean_rounded(self) -> None:
        bars = _uptrend()
        lv = D.levels(bars, F3)
        assert lv is not None
        expected = sum((b.close for b in bars[-20:]), D_(0)) / 20
        assert lv.trend_avg == expected.quantize(D_("0.01"))


# ================================================================================================
# Direction, the 75-minute confirm and the intraday check
# ================================================================================================


def _minutes(
    day: dt.date, closes_by_bar: Sequence[Decimal | None], *, drop_last_minute_of: int = -1
) -> list[D.MinuteBar]:
    """Five 75-minute bars' worth of minutes on ``day``; a bar may be ``None`` (absent)."""
    out: list[D.MinuteBar] = []
    for slot, close in enumerate(closes_by_bar):
        if close is None:
            continue
        start = dt.datetime.combine(day, dt.time(9, 15)) + dt.timedelta(minutes=75 * slot)
        for m in range(75):
            if slot == drop_last_minute_of and m == 74:
                continue
            ts = start + dt.timedelta(minutes=m)
            price = close - 5 + m * D_("0.1")
            out.append(D.MinuteBar(ts, price, price + 1, price - 1, close if m == 74 else price))
    return out


class TestSeventyFiveMinuteBars:
    def test_seventy_five_minute_bars_are_five_a_session_closing_at_the_last_minute(self) -> None:
        day = dt.date(2026, 9, 25)
        closes = [D_(24000 + i) for i in range(5)]
        bars = D.seventy_five_minute_bars(_minutes(day, closes))
        assert [b.start.time() for b in bars] == list(D.BAR_75_OPENS)
        assert [b.close for b in bars] == closes
        assert all(b.high >= b.close >= b.low for b in bars)

    def test_seventy_five_minute_bar_without_its_closing_minute_is_left_out(self) -> None:
        day = dt.date(2026, 9, 25)
        closes = [D_(24000 + i) for i in range(5)]
        bars = D.seventy_five_minute_bars(_minutes(day, closes, drop_last_minute_of=4))
        assert len(bars) == 4

    def test_seventy_five_minute_minutes_outside_the_session_are_ignored(self) -> None:
        day = dt.date(2026, 9, 25)
        stray = D.MinuteBar(dt.datetime.combine(day, dt.time(15, 31)), D_(1), D_(1), D_(1), D_(1))
        assert D.seventy_five_minute_bars([stray]) == ()

    def test_seventy_five_minute_bars_come_oldest_first_across_sessions(self) -> None:
        d1, d2 = dt.date(2026, 9, 24), dt.date(2026, 9, 25)
        bars = D.seventy_five_minute_bars([*_minutes(d2, [D_(2)] * 5), *_minutes(d1, [D_(1)] * 5)])
        assert [b.start.date() for b in bars] == [d1] * 5 + [d2] * 5


def _bars75(closes: list[int]) -> list[D.Bar75]:
    day = dt.date(2026, 9, 25)
    out = []
    for i, c in enumerate(closes):
        start = dt.datetime.combine(day, dt.time(9, 15)) + dt.timedelta(minutes=75 * i)
        out.append(D.Bar75(start, D_(c), D_(c) + 5, D_(c) - 5, D_(c)))
    return out


class TestDirection:
    def test_direction_up_needs_close_above_trend_and_support(self) -> None:
        lv = D.levels(_uptrend(), F3)
        assert lv is not None
        assert D.daily_direction(lv) is D.Direction.UP

    def test_direction_down_needs_close_below_trend_and_resistance(self) -> None:
        lv = D.levels(_downtrend(), F3)
        assert lv is not None
        assert D.daily_direction(lv) is D.Direction.DOWN

    def test_direction_is_none_without_a_level_on_the_trade_side(self) -> None:
        lv = D.levels(_uptrend(), F3)
        assert lv is not None
        flat = D.Levels(
            lv.as_of, lv.close, None, None, lv.weekly_high, lv.weekly_low, lv.trend_avg, (), ()
        )
        assert D.daily_direction(flat) is D.Direction.NONE

    def test_confirm_agrees_when_the_last_75_close_is_above_its_average(self) -> None:
        bars = _bars75([100] * 9 + [110])
        verdict = D.confirm_75(bars, D.Direction.UP, F3)
        assert verdict.agrees and verdict.last_close == 110 and verdict.average == D_("101.00")
        assert not D.confirm_75(bars, D.Direction.DOWN, F3).agrees

    def test_confirm_needs_enough_bars_and_a_daily_direction(self) -> None:
        assert not D.confirm_75(_bars75([100] * 5), D.Direction.UP, F3).agrees
        assert not D.confirm_75(_bars75([100] * 9 + [110]), D.Direction.NONE, F3).agrees

    def test_direction_for_is_none_when_daily_and_75_disagree(self) -> None:
        lv = D.levels(_uptrend(), F3)
        assert lv is not None
        agreeing = _bars75([100] * 9 + [110])
        disagreeing = _bars75([100] * 9 + [90])
        assert D.direction_for(lv, agreeing, F3)[0] is D.Direction.UP
        assert D.direction_for(lv, disagreeing, F3)[0] is D.Direction.NONE

    def test_the_key_level_is_support_up_and_resistance_down(self) -> None:
        up = D.levels(_uptrend(), F3)
        down = D.levels(_downtrend(), F3)
        assert up is not None and down is not None
        assert D.key_level(D.Direction.UP, up) == up.support
        assert D.key_level(D.Direction.DOWN, down) == down.resistance
        assert D.key_level(D.Direction.NONE, up) is None

    def test_intraday_check_reads_the_index_against_the_session_open(self) -> None:
        assert D.intraday_aligned(D.Direction.UP, D_(24100), D_(24000))
        assert not D.intraday_aligned(D.Direction.UP, D_(24000), D_(24000))
        assert D.intraday_aligned(D.Direction.DOWN, D_(23900), D_(24000))
        assert not D.intraday_aligned(D.Direction.NONE, D_(23900), D_(24000))

    def test_the_level_breaks_only_beyond_the_buffer(self) -> None:
        level = D_(24000)
        assert not D.level_broken(D.Direction.UP, level, D_("23980"), F3)  # inside 0.10 %
        assert D.level_broken(D.Direction.UP, level, D_("23975"), F3)
        assert D.level_broken(D.Direction.DOWN, level, D_("24025"), F3)
        assert not D.level_broken(D.Direction.DOWN, level, D_("24020"), F3)


# ================================================================================================
# Strikes, expiry, size, add
# ================================================================================================


class TestStrikes:
    def test_strike_up_sells_a_put_one_percent_below_the_weekly_low_rounded_down(self) -> None:
        lv = D.levels(_uptrend(), F3)
        assert lv is not None
        s = D.spread_strikes(D.Direction.UP, lv, D_(50), F3)
        raw = lv.weekly_low * D_("0.99")
        assert s.option_type is OptionType.PE
        assert s.short == (raw // 50) * 50 and s.short <= raw
        assert s.wing < s.short and s.wing % 50 == 0
        assert s.short - s.wing >= lv.close * D_("0.02") - 50

    def test_strike_down_sells_a_call_one_percent_above_the_weekly_high_rounded_up(self) -> None:
        lv = D.levels(_downtrend(), F3)
        assert lv is not None
        s = D.spread_strikes(D.Direction.DOWN, lv, D_(100), F3)
        raw = lv.weekly_high * D_("1.01")
        assert s.option_type is OptionType.CE
        assert s.short >= raw and s.short % 100 == 0 and s.short - raw < 100
        assert s.wing > s.short and s.wing % 100 == 0

    def test_strike_wing_is_never_the_short_strike(self) -> None:
        lv = D.levels(_uptrend(), F3)
        assert lv is not None
        tight = F3Config(wing_pct=D_("0.5"))
        s = D.spread_strikes(D.Direction.UP, lv, D_(500), tight)
        assert s.wing == s.short - 500

    def test_strike_needs_a_direction(self) -> None:
        lv = D.levels(_uptrend(), F3)
        assert lv is not None
        with pytest.raises(ValueError, match="direction"):
            D.spread_strikes(D.Direction.NONE, lv, D_(50), F3)


class TestExpiry:
    def test_expiry_weekly_is_the_nearest_with_two_sessions_left(self) -> None:
        sessions = _sessions(40, end=dt.date(2026, 10, 30))
        entry = dt.date(2026, 10, 5)  # Monday
        weeklies = [dt.date(2026, 10, 6), dt.date(2026, 10, 13), dt.date(2026, 10, 20)]
        assert D.choose_expiry(weeklies, sessions, entry, F3.min_sessions_weekly) == dt.date(
            2026, 10, 13
        )
        assert D.choose_expiry(
            weeklies, sessions, dt.date(2026, 10, 1), F3.min_sessions_weekly
        ) == dt.date(2026, 10, 6)

    def test_expiry_monthly_needs_five_sessions_else_the_next_month(self) -> None:
        sessions = _sessions(60, end=dt.date(2026, 12, 31))
        monthlies = [dt.date(2026, 10, 27), dt.date(2026, 11, 24)]
        assert D.choose_expiry(
            monthlies, sessions, dt.date(2026, 10, 22), F3.min_sessions_monthly
        ) == dt.date(2026, 11, 24)
        assert D.choose_expiry(
            monthlies, sessions, dt.date(2026, 10, 19), F3.min_sessions_monthly
        ) == dt.date(2026, 10, 27)

    def test_expiry_is_none_when_nothing_listed_qualifies(self) -> None:
        sessions = _sessions(10, end=dt.date(2026, 10, 9))
        assert D.choose_expiry([dt.date(2026, 9, 29)], sessions, dt.date(2026, 10, 5), 2) is None


class TestSize:
    def _lv(self) -> D.Levels:
        lv = D.levels(_uptrend(), F3)
        assert lv is not None
        return lv

    def test_size_spends_the_entry_share_under_the_risk_budget_and_ceiling(self) -> None:
        # ₹50 lakh: 25 % share is ₹12.5 lakh, 1 % risk is ₹50,000, the ceiling ₹25,000 -> ₹25,000
        sizing = D.size_entry(
            mode=Mode.LIVE, capital_inr=D_(5_000_000), max_loss_per_unit=D_(150),
            lot_size=65, f3=F3, common=COMMON, ceilings=CEIL,
        )  # fmt: skip
        assert sizing.risk_budget_inr == D_(25000)
        assert sizing.lots == 2

    def test_size_zero_lots_is_rejected_never_rounded_up(self) -> None:
        sizing = D.size_entry(
            mode=Mode.LIVE, capital_inr=D_(1_000_000), max_loss_per_unit=D_(480),
            lot_size=65, f3=F3, common=COMMON, ceilings=CEIL,
        )  # fmt: skip
        assert (sizing.lots, sizing.state) == (0, PlanState.REJECTED_SIZE)

    def test_size_paper_at_zero_capital_runs_one_lot(self) -> None:
        sizing = D.size_entry(
            mode=Mode.PAPER, capital_inr=D_(0), max_loss_per_unit=D_(480),
            lot_size=65, f3=F3, common=COMMON, ceilings=CEIL,
        )  # fmt: skip
        assert sizing.lots == 1 and sizing.lots_at_ceiling == 0

    def test_size_proposal_refuses_a_missing_price_then_a_thin_short_then_no_credit(self) -> None:
        lv = self._lv()
        s = D.spread_strikes(D.Direction.UP, lv, D_(50), F3)

        def propose(prices: dict[tuple[Decimal, OptionType], Decimal]) -> D.SpreadProposal:
            return D.propose_spread(
                mode=Mode.PAPER, direction=D.Direction.UP, lv=lv, step=D_(50), prices=prices,
                lot_size=65, capital_inr=D_(0), f3=F3, common=COMMON, ceilings=CEIL,
            )  # fmt: skip

        missing = propose({(s.short, OptionType.PE): D_(30)})
        assert missing.state is PlanState.REJECTED_LIQUIDITY and "wing" in missing.reasons[0]
        thin = propose({(s.short, OptionType.PE): D_(8), (s.wing, OptionType.PE): D_(2)})
        assert thin.state is PlanState.REJECTED_COST
        inverted = propose({(s.short, OptionType.PE): D_(30), (s.wing, OptionType.PE): D_(31)})
        assert inverted.state is PlanState.REJECTED_COST and inverted.credit == D_(-1)

    def test_size_proposal_carries_credit_max_loss_and_lots(self) -> None:
        lv = self._lv()
        s = D.spread_strikes(D.Direction.UP, lv, D_(50), F3)
        p = D.propose_spread(
            mode=Mode.PAPER, direction=D.Direction.UP, lv=lv, step=D_(50),
            prices={(s.short, OptionType.PE): D_(30), (s.wing, OptionType.PE): D_(6)},
            lot_size=65, capital_inr=D_(0), f3=F3, common=COMMON, ceilings=CEIL,
        )  # fmt: skip
        assert p.state is None
        assert p.credit == D_(24)
        assert p.max_loss_per_unit == s.width - 24
        assert p.max_loss_per_lot_inr == (s.width - 24) * 65
        assert isinstance(p.sizing, FoSizing) and p.sizing.lots == 1


WORKING_MARK = D_(18)
NO_CAPITAL = D_(0)
INDEX_PRINT = D_(24100)
HOLD_MARK = D_(20)
OPEN = D.OpenSpread(
    direction=D.Direction.UP,
    level=D_(24000),
    strikes=D.SpreadStrikes(OptionType.PE, D_(23500), D_(23000)),
    expiry=dt.date(2026, 10, 6),
    entry_session=dt.date(2026, 10, 1),
    entry_credit=D_(24),
    lots=1,
    lot_size=65,
    max_loss_per_lot_inr=D_(476) * 65,
)


def _open(*, lots: int = 1) -> D.OpenSpread:
    return replace(OPEN, lots=lots)


class TestAdd:
    def _decide(  # noqa: PLR0913 - one keyword per input the rule reads
        self,
        *,
        position: D.OpenSpread = OPEN,
        today: dt.date = dt.date(2026, 10, 5),
        direction_now: D.Direction = D.Direction.UP,
        level_intact: bool = True,
        mark: Decimal | None = WORKING_MARK,
        capital_inr: Decimal = NO_CAPITAL,
    ) -> D.AddDecision:
        return D.add_decision(
            position=position, today=today, direction_now=direction_now,
            level_intact=level_intact, mark=mark, capital_inr=capital_inr, f3=F3, common=COMMON,
        )  # fmt: skip

    def test_add_waits_for_the_next_session(self) -> None:
        assert not self._decide(today=dt.date(2026, 10, 1)).allowed
        assert self._decide(today=dt.date(2026, 10, 5)).allowed

    def test_add_needs_the_trade_to_be_working(self) -> None:
        assert not self._decide(mark=D_("19.5")).allowed  # 24 x 0.8 = 19.2
        assert self._decide(mark=D_("19.2")).allowed

    def test_add_needs_the_direction_and_the_level(self) -> None:
        assert not self._decide(direction_now=D.Direction.NONE).allowed
        assert not self._decide(level_intact=False).allowed
        assert not self._decide(mark=None).allowed

    def test_add_paper_at_zero_capital_is_one_lot_once(self) -> None:
        assert self._decide().lots == 1
        assert not self._decide(position=_open(lots=2)).allowed

    def test_add_stops_at_the_full_share(self) -> None:
        # capital ₹20 lakh: full share 50 % = ₹10 lakh; a lot risks ₹30,940
        d = self._decide(capital_inr=D_(2_000_000), position=_open(lots=1))
        assert d.lots == 1, "the same lots again, capped by fo_max_lots"
        capped = self._decide(capital_inr=D_(2_000_000), position=_open(lots=2))
        assert not capped.allowed
        small = self._decide(capital_inr=D_(60_000), position=_open(lots=1))  # full ₹30,000 < a lot
        assert not small.allowed and "full share" in small.message


# ================================================================================================
# Exits
# ================================================================================================


class TestExit:
    def _exit(
        self,
        *,
        now: dt.datetime = dt.datetime(2026, 10, 5, 11, 0),
        last_price: Decimal | None = INDEX_PRINT,
        mark: Decimal | None = HOLD_MARK,
    ) -> D.F3Exit | None:
        return D.f3_exit(
            now=now, position=OPEN, last_price=last_price, mark=mark, f3=F3, common=COMMON
        )

    def test_hold_while_nothing_is_due(self) -> None:
        assert self._exit() is None

    def test_exit_level_break_wins_and_reads_the_print(self) -> None:
        e = self._exit(last_price=D_(23970), mark=D_(60))  # the cut is also due
        assert e is not None and e.reason is D.F3ExitReason.LEVEL_BREAK

    def test_exit_loss_cut_at_twice_the_credit(self) -> None:
        assert self._exit(mark=D_("47.9")) is None
        e = self._exit(mark=D_(48))
        assert e is not None and e.reason is D.F3ExitReason.LOSS_CUT

    def test_exit_decay_target_at_eighty_percent(self) -> None:
        assert self._exit(mark=D_("4.81")) is None
        e = self._exit(mark=D_("4.80"))
        assert e is not None and e.reason is D.F3ExitReason.DECAY_TARGET

    def test_exit_hard_on_expiry_day_at_the_hard_exit_time(self) -> None:
        assert self._exit(now=dt.datetime(2026, 10, 6, 14, 59)) is None
        e = self._exit(now=dt.datetime(2026, 10, 6, 15, 0))
        assert e is not None and e.reason is D.F3ExitReason.HARD_EXIT
        late = self._exit(now=dt.datetime(2026, 10, 7, 9, 16))
        assert late is not None and late.reason is D.F3ExitReason.HARD_EXIT

    def test_exit_without_quotes_still_honours_the_hard_exit(self) -> None:
        e = self._exit(last_price=None, mark=None, now=dt.datetime(2026, 10, 6, 15, 1))
        assert e is not None and e.reason is D.F3ExitReason.HARD_EXIT
        assert self._exit(last_price=None, mark=None) is None

    def test_exit_pnl_is_credit_less_mark_times_units(self) -> None:
        assert D.pnl_inr(D_(24), D_(4), 65) == D_(1300)
        assert D.pnl_inr(D_(24), D_(48), 65) == D_(-1560)


# ================================================================================================
# The vocabulary, law 1 and the doc
# ================================================================================================


class TestVocabulary:
    def test_the_sleeves_and_their_group(self) -> None:
        assert f3_sleeve_for("NIFTY") is FoSleeve.F3N and f3_sleeve_for("BANKNIFTY") is FoSleeve.F3B
        assert group_of(FoSleeve.F3N) is FoSleeveGroup.F3 is group_of(FoSleeve.F3B)
        assert (
            group_of(FoSleeve.F1N) is FoSleeveGroup.F1 and group_of(FoSleeve.F2) is FoSleeveGroup.F2
        )
        with pytest.raises(ValueError, match="F3"):
            f3_sleeve_for("FINNIFTY")

    def test_the_structure_and_the_add_kind(self) -> None:
        assert Structure.CREDIT_SPREAD == "CREDIT_SPREAD" and PlanKind.ADD == "ADD"
        assert DEFAULT_FNO_CONFIG.risk_per_trade_pct(FoSleeve.F3N) == F3.risk_per_trade_pct

    def test_pure_module_touches_nothing(self) -> None:
        """Law 1: no I/O, no clock, no env in the module."""
        src = (
            ROOT / "decile-blueprint/packages/core/src/baskfy_core/fno/directional.py"
        ).read_text()
        tree = ast.parse(src)
        imported = {
            (n.module or "") if isinstance(n, ast.ImportFrom) else n.names[0].name
            for n in ast.walk(tree)
            if isinstance(n, ast.Import | ast.ImportFrom)
        }
        for banned in ("os", "sys", "sqlalchemy", "httpx", "requests", "pathlib", "time"):
            assert not any(m == banned or m.startswith(banned + ".") for m in imported), banned
        assert "datetime.now" not in src and ".today()" not in src and "environ" not in src

    def test_every_config_number_is_documented_in_04_section_11(self) -> None:
        doc = (ROOT / "docs/fno/04-business-rules.md").read_text()
        section = doc.split("## §11")[1]
        names = re.findall(r"`f3_(\w+)`", section)
        fields = set(F3Config.__dataclass_fields__)
        documented = set(names)
        # the window and slippage are F1's numbers, cited from §1 and §3, not §11's own rows
        own = fields - {
            "underlyings",
            "plan_time",
            "entry_window_end",
            "slippage_pct",
            "slippage_min_inr",
        }
        assert own <= documented, sorted(own - documented)
        for name in documented:
            assert name in fields, f"`f3_{name}` in 04 §11 has no F3Config field"

    def test_config_defaults_are_04_section_11(self) -> None:
        assert (F3.pivot_lookback, F3.pivot_width, F3.trend_sessions) == (60, 3, 20)
        assert (F3.weekly_sessions, F3.confirm_bars) == (5, 10)
        assert (F3.distance_pct, F3.wing_pct) == (D_("1.0"), D_("2.0"))
        assert F3.short_premium_min_inr == D_(10)
        assert (F3.min_sessions_weekly, F3.min_sessions_monthly) == (2, 5)
        assert (F3.entry_share_pct, F3.full_share_pct, F3.add_working_pct) == (
            D_(25),
            D_(50),
            D_(20),
        )
        assert (F3.decay_target_pct, F3.loss_cut_mult, F3.level_buffer_pct) == (
            D_(80),
            D_("2.0"),
            D_("0.10"),
        )
        assert F3.max_open_per_underlying == 1

    def test_config_bounds_refuse(self) -> None:
        with pytest.raises(ValueError, match="f3_decay_target_pct"):
            F3Config(decay_target_pct=D_(96))
        with pytest.raises(ValueError, match="full_share"):
            F3Config(entry_share_pct=D_(40), full_share_pct=D_(30))
        with pytest.raises(ValueError, match="underlyings"):
            F3Config(underlyings=("FINNIFTY",))
