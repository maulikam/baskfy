"""ATM implied volatility per underlying per session (``04`` §4 ``iv_atm``).

The rule: the **nearest monthly with ≥ 8 sessions left**; the **strike nearest the future's
settle** (the lower one on a tie); **Black-76 on that settle, r = 0**, via the options pack's
solver (``baskfy_core.options.greeks.implied_vol``); the **mean of CE and PE**. It is null if
either leg did not trade that session (volume 0 or no close) or the solver refuses a leg — a
number is never manufactured from a stale print. ``T`` is calendar days to expiry ÷ 365, as the
research priced it (``RESEARCH.md`` method).

Floats, deliberately: an IV is a model output, not money (options OP1.8, carried).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import polars as pl

from baskfy_core.fno.calendar import monthly_expiries, sessions_between
from baskfy_core.fno.config import SeriesConfig
from baskfy_core.options.config import OptionType
from baskfy_core.options.greeks import implied_vol

_DAYS_PER_YEAR = 365.0


@dataclass(frozen=True, slots=True)
class OptionPrint:
    """One option row of the bhavcopy for one underlying and session."""

    expiry: dt.date
    strike: float
    option_type: OptionType
    close: float | None
    volume: int


@dataclass(frozen=True, slots=True)
class AtmIv:
    """The answer for one underlying-session. ``iv`` is ``None`` when a leg did not trade."""

    expiry: dt.date
    strike: float
    forward: float
    iv_ce: float | None
    iv_pe: float | None

    @property
    def iv(self) -> float | None:
        if self.iv_ce is None or self.iv_pe is None:
            return None
        return (self.iv_ce + self.iv_pe) / 2.0


def iv_expiry(
    day: dt.date, expiries: Iterable[dt.date], sessions: Sequence[dt.date], min_left: int
) -> dt.date | None:
    """The nearest monthly with at least ``min_left`` sessions from ``day`` to expiry."""
    for expiry in monthly_expiries(expiries):
        if expiry <= day:
            continue
        left = sessions_between(sessions, day, expiry)
        if left is not None and left >= min_left:
            return expiry
    return None


def _leg_iv(  # noqa: PLR0913, PLR0917 - the model's inputs
    prints: Mapping[tuple[float, OptionType], OptionPrint],
    strike: float,
    kind: OptionType,
    forward: float,
    years: float,
    config: SeriesConfig,
) -> float | None:
    row = prints.get((strike, kind))
    if row is None or row.volume <= 0 or row.close is None or row.close <= 0:
        return None
    return implied_vol(
        row.close,
        forward,
        strike,
        years,
        config.iv_rate,
        kind,
        lower=config.iv_lower,
        upper=config.iv_upper,
        tolerance=config.iv_tolerance,
    )


def atm_iv(
    *,
    day: dt.date,
    options: Iterable[OptionPrint],
    futures_settle: Mapping[dt.date, float],
    sessions: Sequence[dt.date],
    config: SeriesConfig,
) -> AtmIv | None:
    """``04`` §4's ``iv_atm`` for one underlying on ``day``.

    ``options`` are that underlying's option rows of the day; ``futures_settle`` maps each
    listed futures expiry to its settle that day. ``None`` when no monthly qualifies, its future
    has no settle, or no strike is listed for it.
    """
    rows = list(options)
    expiry = iv_expiry(day, (r.expiry for r in rows), sessions, config.iv_min_sessions_left)
    if expiry is None:
        return None
    forward = futures_settle.get(expiry)
    if forward is None or forward <= 0:
        return None
    prints = {(r.strike, r.option_type): r for r in rows if r.expiry == expiry}
    strikes = sorted({strike for strike, _ in prints})
    if not strikes:
        return None
    strike = min(strikes, key=lambda k: (abs(k - forward), k))
    years = (expiry - day).days / _DAYS_PER_YEAR
    return AtmIv(
        expiry=expiry,
        strike=strike,
        forward=forward,
        iv_ce=_leg_iv(prints, strike, OptionType.CE, forward, years, config),
        iv_pe=_leg_iv(prints, strike, OptionType.PE, forward, years, config),
    )


def iv_atm_frame(
    options: pl.DataFrame,
    futures: pl.DataFrame,
    sessions: Sequence[dt.date],
    config: SeriesConfig,
) -> pl.DataFrame:
    """``atm_iv`` over frames: one row per (trade_date, symbol) that has options.

    ``options``: ``trade_date, symbol, expiry, strike, option_type, close, volume``;
    ``futures``: ``trade_date, symbol, expiry, settle``. Returns ``trade_date, symbol,
    iv_expiry, iv_strike, iv_atm`` (``iv_atm`` null where ``04`` §4 says null).
    """
    settles: dict[tuple[dt.date, str], dict[dt.date, float]] = {}
    for day, symbol, expiry, settle in futures.select(
        "trade_date", "symbol", "expiry", "settle"
    ).iter_rows():
        if settle is not None:
            settles.setdefault((day, symbol), {})[expiry] = float(settle)
    out: list[tuple[dt.date, str, dt.date | None, float | None, float | None]] = []
    grouped = options.select(
        "trade_date", "symbol", "expiry", "strike", "option_type", "close", "volume"
    ).partition_by("trade_date", "symbol", as_dict=True)
    for key, group in sorted(grouped.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        day, symbol = key[0], key[1]
        if not isinstance(day, dt.date) or not isinstance(symbol, str):
            raise TypeError("trade_date must be a date and symbol a string")
        prints = [
            OptionPrint(
                expiry=e,
                strike=float(k),
                option_type=OptionType(t),
                close=None if c is None else float(c),
                volume=int(v or 0),
            )
            for _, _, e, k, t, c, v in group.iter_rows()
        ]
        found = atm_iv(
            day=day,
            options=prints,
            futures_settle=settles.get((day, symbol), {}),
            sessions=sessions,
            config=config,
        )
        if found is None:
            out.append((day, symbol, None, None, None))
        else:
            out.append((day, symbol, found.expiry, found.strike, found.iv))
    return pl.DataFrame(
        out,
        schema={
            "trade_date": pl.Date,
            "symbol": pl.String,
            "iv_expiry": pl.Date,
            "iv_strike": pl.Float64,
            "iv_atm": pl.Float64,
        },
        orient="row",
    )
