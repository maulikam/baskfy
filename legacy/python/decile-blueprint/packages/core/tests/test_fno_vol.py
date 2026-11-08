"""``docs/fno/04`` §4 ``iv_atm`` — nearest monthly with ≥ 8 sessions, strike nearest the future's
settle, Black-76 at r = 0, mean of CE and PE, null if a leg did not trade."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from baskfy_core.fno.config import SeriesConfig
from baskfy_core.fno.vol import OptionPrint, atm_iv, iv_atm_frame, iv_expiry
from baskfy_core.options.config import OptionType
from baskfy_core.options.greeks import black76_price

CFG = SeriesConfig()
DAY = dt.date(2026, 10, 19)
OCT = dt.date(2026, 10, 27)
NOV = dt.date(2026, 11, 24)


def _sessions() -> tuple[dt.date, ...]:
    out: list[dt.date] = []
    day = dt.date(2026, 10, 1)
    while day <= dt.date(2026, 12, 31):
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return tuple(out)


SESSIONS = _sessions()
F = 25_030.0
VOL = 0.14


def _prints(expiry: dt.date, *, volume_pe: int = 10) -> list[OptionPrint]:
    years = (expiry - DAY).days / 365
    rows: list[OptionPrint] = []
    for strike in (24_900.0, 25_000.0, 25_100.0):
        for kind, vol in ((OptionType.CE, 10), (OptionType.PE, volume_pe)):
            price = black76_price(F, strike, years, 0.0, VOL, kind)
            rows.append(OptionPrint(expiry, strike, kind, round(price, 2), vol))
    return rows


def test_nearest_monthly_with_eight_sessions_left() -> None:
    # 19 Oct → 27 Oct is 6 sessions: October does not qualify, November does.
    assert iv_expiry(DAY, [OCT, NOV], SESSIONS, 8) == NOV
    assert iv_expiry(dt.date(2026, 10, 14), [OCT, NOV], SESSIONS, 8) == OCT


def test_recovers_the_vol_at_the_strike_nearest_the_settle() -> None:
    found = atm_iv(
        day=DAY,
        options=_prints(OCT) + _prints(NOV),
        futures_settle={OCT: F, NOV: F},
        sessions=SESSIONS,
        config=CFG,
    )
    assert found is not None
    assert (found.expiry, found.strike) == (NOV, 25_000.0)
    assert found.iv == pytest.approx(VOL, abs=5e-4)


def test_null_if_a_leg_did_not_trade() -> None:
    found = atm_iv(
        day=DAY, options=_prints(NOV, volume_pe=0), futures_settle={NOV: F},
        sessions=SESSIONS, config=CFG,
    )  # fmt: skip
    assert found is not None
    assert found.iv_ce is not None
    assert found.iv is None


def test_none_without_the_future() -> None:
    assert (
        atm_iv(day=DAY, options=_prints(NOV), futures_settle={}, sessions=SESSIONS, config=CFG)
        is None
    )


def test_ties_go_to_the_lower_strike() -> None:
    found = atm_iv(
        day=DAY, options=_prints(NOV), futures_settle={NOV: 25_050.0}, sessions=SESSIONS, config=CFG
    )
    assert found is not None
    assert found.strike == 25_000.0


def test_frame_wrapper() -> None:
    options = pl.DataFrame(
        [
            {
                "trade_date": DAY, "symbol": "NIFTY", "expiry": p.expiry, "strike": p.strike,
                "option_type": p.option_type.value, "close": p.close, "volume": p.volume,
            }
            for p in _prints(NOV)
        ]
    )  # fmt: skip
    futures = pl.DataFrame([{"trade_date": DAY, "symbol": "NIFTY", "expiry": NOV, "settle": F}])
    out = iv_atm_frame(options, futures, SESSIONS, CFG)
    assert out.height == 1
    assert out["iv_atm"][0] == pytest.approx(VOL, abs=5e-4)
