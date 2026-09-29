"""F3-3: the EOD proxy of F3's daily rules (``gates/f3-3-retest.md``; ``04`` §11)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from baskfy_core.fno import directional as d
from baskfy_core.fno import directional_retest as x
from baskfy_core.fno import retest as rt
from baskfy_core.fno.config import DEFAULT_FNO_CONFIG, F3Config
from baskfy_core.options.config import OptionType

F3 = DEFAULT_FNO_CONFIG.f3
ROOT = Path(__file__).resolve().parents[4]
D_ = Decimal


def _sessions(count: int, end: dt.date = dt.date(2026, 9, 25)) -> list[dt.date]:
    days: list[dt.date] = []
    day = end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= dt.timedelta(days=1)
    return sorted(days)


def _contracts(  # noqa: PLR0913 - the synthetic market's knobs
    symbol: str,
    closes: list[int],
    *,
    expiries: list[dt.date],
    strikes: range,
    atm_premium: float = 30.0,
    slope: float = 0.02,
    decay_from: dt.date | None = None,
) -> pl.DataFrame:
    """A future per session (its OHLC ± 20) and, for each listed expiry, puts and calls at every
    strike priced on distance from the close — ``atm_premium`` at the money, ``slope`` rupees
    less per point away, never below one rupee — so a short ~2.5 % out is worth ~₹17 and a wing
    2 % further ~₹7. From ``decay_from`` on, every option settles at one rupee."""
    days = _sessions(len(closes))
    rows: list[dict[str, object]] = []
    for day, close in zip(days, closes, strict=True):
        c = float(close)
        rows.append(
            {
                "date": day,
                "instrument": "FUTIDX",
                "symbol": symbol,
                "expiry": max(expiries),
                "strike": 0.0,
                "option_type": "XX",
                "open": c,
                "high": c + 20,
                "low": c - 20,
                "close": c,
                "settle": c,
                "open_interest": 1000,
                "volume": 100,
                "lot_size": 65,
            }
        )
        for expiry in expiries:
            if expiry < day:
                continue
            for k in strikes:
                for kind in ("PE", "CE"):
                    away = max(0.0, c - k) if kind == "PE" else max(0.0, k - c)
                    price = max(1.0, atm_premium - slope * away)
                    if decay_from is not None and day >= decay_from:
                        price = 1.0
                    if day == expiry:
                        # NSE's file: an expired option's settle column carries the index's
                        # final settlement level, not a premium.
                        price = c
                    rows.append(
                        {
                            "date": day,
                            "instrument": "OPTIDX",
                            "symbol": symbol,
                            "expiry": expiry,
                            "strike": float(k),
                            "option_type": kind,
                            "open": price,
                            "high": price,
                            "low": price,
                            "close": price,
                            "settle": price,
                            "open_interest": 500,
                            "volume": 50,
                            "lot_size": 65,
                        }
                    )
    return pl.DataFrame(rows)


def _uptrend(n: int = 70, *, dip_at: int = 8, level: int = 24000) -> list[int]:
    closes: list[int] = []
    for i in range(n):
        level += 20
        closes.append(level - (300 if i == n - dip_at else 0))
    return closes


class TestPanel:
    def test_the_panel_takes_the_front_month_future_as_the_index_bar(self) -> None:
        exp = [dt.date(2026, 9, 29), dt.date(2026, 10, 27)]
        frame = _contracts("NIFTY", _uptrend(65), expiries=exp, strikes=range(23000, 26000, 50))
        panel = x.index_panel(frame, "NIFTY")
        assert len(panel.bars) == 65
        assert panel.bars[-1].high == panel.bars[-1].close + 20
        assert panel.lot_by_date[panel.bars[-1].date] == 65
        assert panel.expiries_by_date[panel.bars[-1].date] == tuple(exp)

    def test_monthly_expiries_are_the_last_of_each_month(self) -> None:
        listed = (
            dt.date(2026, 10, 6), dt.date(2026, 10, 13), dt.date(2026, 10, 27),
            dt.date(2026, 11, 24), dt.date(2026, 12, 29),
        )  # fmt: skip
        assert x.monthly_expiries(listed) == (
            dt.date(2026, 10, 27), dt.date(2026, 11, 24), dt.date(2026, 12, 29),
        )  # fmt: skip

    def test_strike_step_is_the_modal_gap(self) -> None:
        assert x.strike_step(tuple(D_(k) for k in range(23000, 26000, 50)), D_(24500)) == D_(50)
        assert x.strike_step((D_(1),), D_(1)) is None


class TestTheProxy:
    def test_a_signal_at_the_close_enters_next_session_and_decays_to_the_target(self) -> None:
        days = _sessions(70)
        decay_from = days[-3]
        frame = _contracts(
            "NIFTY",
            _uptrend(70),
            expiries=[days[-1] + dt.timedelta(days=4), days[-1] + dt.timedelta(days=11)],
            strikes=range(23000, 27000, 50),
            decay_from=decay_from,
        )
        res = x.run_symbol(x.index_panel(frame, "NIFTY"), F3)
        assert res.summary.n >= 1
        first = res.trades.row(0, named=True)
        assert first["entry"] > first["signal"], (
            "no look-ahead: the entry is the session after the signal"
        )
        assert first["direction"] == "UP" and first["option_type"] == "PE"
        assert first["credit"] > 10
        decayed = res.trades.filter(pl.col("reason") == "DECAY_TARGET")
        assert decayed.height >= 1
        assert float(decayed["R"][0]) > 0 and float(decayed["gross_R"][0]) > float(decayed["R"][0])

    def test_a_level_break_on_the_bars_low_exits_at_that_settle(self) -> None:
        closes = _uptrend(70)
        closes[-1] = closes[-8] - 400  # the last bar crashes through the dip's low
        days = _sessions(70)
        frame = _contracts(
            "NIFTY",
            closes,
            expiries=[days[-1] + dt.timedelta(days=4)],
            strikes=range(22000, 27000, 50),
        )
        res = x.run_symbol(x.index_panel(frame, "NIFTY"), F3)
        reasons = set(res.trades["reason"].to_list()) if not res.trades.is_empty() else set()
        assert "LEVEL_BREAK" in reasons
        broke = res.trades.filter(pl.col("reason") == "LEVEL_BREAK").row(0, named=True)
        assert broke["exit"] == days[-1]

    def test_banknifty_uses_the_monthly_and_nifty_the_nearest_weekly(self) -> None:
        days = _sessions(70)  # the last session is Friday 25 Sep 2026
        weekly = dt.date(2026, 9, 29)  # a Tuesday: NIFTY's weekly
        monthly = dt.date(2026, 9, 30)  # the last listed expiry of September: the monthly
        far = dt.date(2026, 10, 20)
        for symbol, expected in (("NIFTY", weekly), ("BANKNIFTY", monthly)):
            frame = _contracts(
                symbol,
                _uptrend(70),
                expiries=[weekly, monthly, far],
                strikes=range(22000, 27000, 100),
            )
            res = x.run_symbol(x.index_panel(frame, symbol), F3)
            assert not res.trades.is_empty(), symbol
            assert res.trades["expiry"][-1] == expected, symbol
            assert res.trades["reason"][-1] == "OPEN_AT_END", "still open at the sample's end"
        del days

    def test_r_is_net_over_max_loss_and_a_full_loss_is_minus_one_before_costs(self) -> None:
        # short 30, wing 6: credit 24 on a 500-point width (2 % of ~25,000): max loss 476/unit
        strikes = d.SpreadStrikes(OptionType.PE, D_(23500), D_(23000))
        credit = d.credit_per_unit(D_(30), D_(6))
        assert d.max_loss_per_unit(strikes, credit) == D_(476)
        gross_full_loss = (credit - strikes.width) / D_(476)
        assert gross_full_loss == D_(-1)

    def test_no_trade_summarises_to_zero_not_an_error(self) -> None:
        days = _sessions(30)
        frame = _contracts(
            "NIFTY", [24000] * 30, expiries=[days[-1]], strikes=range(23000, 25000, 50)
        )
        res = x.run_symbol(x.index_panel(frame, "NIFTY"), F3)
        assert res.summary.n == 0 and res.trades.is_empty()

    def test_the_run_covers_both_underlyings(self) -> None:
        days = _sessions(70)
        exp = [days[-1] + dt.timedelta(days=4), days[-1] + dt.timedelta(days=25)]
        frame = pl.concat(
            [
                _contracts("NIFTY", _uptrend(70), expiries=exp, strikes=range(22000, 27000, 50)),
                _contracts(
                    "BANKNIFTY",
                    _uptrend(70, level=50000),
                    expiries=exp,
                    strikes=range(48000, 56000, 100),
                ),
            ]
        )
        out = x.run_f3_retest(frame, F3Config())
        assert set(out) == {"NIFTY", "BANKNIFTY"}


class TestExpiryDay:
    def test_expiry_day_legs_are_intrinsic_not_the_files_settlement_level(self) -> None:
        days = _sessions(70)
        expiry = days[-1]  # the last session is the expiry: the file prints the index level
        frame = _contracts(
            "NIFTY",
            _uptrend(70),
            expiries=[expiry, dt.date(2026, 10, 20)],
            strikes=range(22000, 27000, 50),
            slope=0.01,  # a small, steady credit that never reaches the target before expiry
        )
        res = x.run_symbol(x.index_panel(frame, "NIFTY"), F3)
        hard = res.trades.filter(pl.col("reason") == "HARD_EXIT")
        assert hard.height == 1
        row = hard.row(0, named=True)
        assert row["exit"] == expiry
        # an OTM put spread at expiry is worth nothing: the whole credit is kept, less costs
        assert 0 < row["R"] < 1 and row["cost_R"] < 0.05


class TestTheEvidenceIsHonest:
    def test_the_untestable_rules_are_named_in_code_and_in_the_evidence(self) -> None:
        assert len(x.NOT_TESTED) == 4
        doc = ROOT / "docs/fno/evidence/f3-retest.md"
        if doc.exists():
            text = doc.read_text()
            assert "cannot test" in text
            for item in x.NOT_TESTED:
                assert item in text


class TestTheQuarterlyFamilies:
    """F3 in ``04`` §6's quarterly re-test (``retest.FAMILIES``), on the raw rows with weeklies."""

    @staticmethod
    def _both() -> pl.DataFrame:
        days = _sessions(70)
        decay_from = days[-3]
        # a weekly, a monthly and a far month, plus strikes well outside the trim's band
        exp = [
            days[-1] + dt.timedelta(days=4),
            days[-1] + dt.timedelta(days=25),
            days[-1] + dt.timedelta(days=120),
        ]
        return pl.concat(
            [
                _contracts(
                    "NIFTY",
                    _uptrend(70),
                    expiries=exp,
                    strikes=range(14000, 34000, 50),
                    decay_from=decay_from,
                ),
                _contracts(
                    "BANKNIFTY",
                    _uptrend(70, level=50000),
                    expiries=exp,
                    strikes=range(30000, 70000, 100),
                    decay_from=decay_from,
                ),
            ]
        )

    def test_the_trim_changes_no_trade(self) -> None:
        frame = self._both()
        for symbol in ("NIFTY", "BANKNIFTY"):
            whole = x.run_symbol(x.index_panel(frame, symbol), F3)
            trimmed_rows = rt.trim_index_contracts(frame, symbol)
            assert trimmed_rows.height < frame.filter(pl.col("symbol") == symbol).height
            trimmed = x.run_symbol(x.index_panel(trimmed_rows, symbol), F3)
            assert not whole.trades.is_empty(), symbol
            assert trimmed.trades.equals(whole.trades), symbol

    def test_the_trim_drops_far_expiries_and_far_strikes_only(self) -> None:
        frame = self._both()
        got = rt.trim_index_contracts(frame, "NIFTY")
        opts = got.filter(pl.col("instrument") == "OPTIDX")
        assert (opts["symbol"] == "NIFTY").all()
        gap = (opts["expiry"] - opts["date"]).dt.total_days()
        assert (gap <= rt.DIRECTIONAL_EXPIRY_DAYS).all()
        days = _sessions(70)
        kept = set(opts["expiry"].to_list())
        assert days[-1] + dt.timedelta(days=4) in kept, "the weekly stays"
        assert days[-1] + dt.timedelta(days=25) in kept, "the monthly stays"
        assert days[-1] + dt.timedelta(days=120) not in kept, "the far month goes"
        front = got.filter(pl.col("instrument") == "FUTIDX").select(
            "date", pl.col("close").alias("index")
        )
        joined = opts.join(front, on="date")
        band = rt.DIRECTIONAL_STRIKE_BAND
        assert (joined["strike"] >= joined["index"] * (1 - band)).all()
        assert (joined["strike"] <= joined["index"] * (1 + band)).all()
        assert got.filter(pl.col("instrument") == "FUTIDX").height == 70

    def test_f3n_and_f3b_are_registered_and_run_from_the_raw_rows(self) -> None:
        frame = self._both()
        futures = frame.filter(pl.col("instrument") == "FUTIDX")
        by_key = {f.key: f for f in rt.FAMILIES}
        for key, symbol in (("F3N", "NIFTY"), ("F3B", "BANKNIFTY")):
            family = by_key[key]
            assert family.scope is rt.Scope.DIRECTIONAL and family.directional == symbol
            got = rt.run_family(
                family,
                futures,
                rt.slice_loader(rt.empty_options()),
                load_index=lambda s: frame.filter(pl.col("symbol") == s),
            )
            want = x.run_symbol(x.index_panel(frame, symbol), F3Config())
            assert got.n == want.summary.n > 0
            assert got.net_r == round(want.summary.exp_r, 4)
            assert got.slippage_source is rt.SlippageSource.ASSUMED
            assert got.caveat == rt.TIER_2E_CAVEAT_F3
            for item in x.NOT_TESTED:
                assert item in got.caveat
            assert got.params["underlying"] == symbol
            assert got.params["expiry_kind"] == ("weekly" if symbol == "NIFTY" else "monthly")

    def test_an_f3_family_without_the_raw_rows_is_refused_not_run_on_the_panel(self) -> None:
        frame = self._both()
        futures = frame.filter(pl.col("instrument") == "FUTIDX")
        f3n = next(f for f in rt.FAMILIES if f.key == "F3N")
        with pytest.raises(ValueError, match="weeklies"):
            rt.run_family(f3n, futures, rt.slice_loader(rt.empty_options()))

    def test_no_option_rows_is_an_honest_zero(self) -> None:
        frame = self._both()
        futures = frame.filter(pl.col("instrument") == "FUTIDX")
        f3b = next(f for f in rt.FAMILIES if f.key == "F3B")
        got = rt.run_family(
            f3b, futures, rt.slice_loader(rt.empty_options()), load_index=lambda s: futures
        )
        assert got.n == 0 and got.net_r is None
