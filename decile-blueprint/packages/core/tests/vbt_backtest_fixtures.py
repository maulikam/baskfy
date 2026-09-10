"""A planted year: one signal, one fill, one exit, and the arithmetic worked by hand.

The reproduction against the study (``test_vbt_goldens.py``) needs the 68 MB research export and
skips without it. This fixture needs nothing, runs everywhere, and pins the engine's arithmetic
to a trade a person can check on paper — which is the only kind of regression test that survives
somebody deciding the goldens are "probably fine".

The shape is the strategy's own: a long rise into a twenty-session shelf at ₹90, one breakout bar
closing at ₹96 on four times its usual volume, a pullback that fills the limit, and then a close
below the 21-day EMA that sells at the next open.

**The trade, by hand** (``docs/vbt/04`` §5, §6, §7, §8):

===========================  ==================================================================
the limit                    the signal bar's close, **₹96.00**
the fill                     the next session's low is ₹95 — it trades through — so the fill is
                             ``min(open ₹97, limit ₹96)`` = **₹96.00**
the cost basis               ``96.00 x (1 + 0.0025)`` = **₹96.24** a share (25 bps a side)
the slot                     ``₹10,00,000 / 10`` = **₹1,00,000**
the quantity                 ``floor(100000 / 96.24)`` = **1,039** shares
the stop                     ``floor_to_paise(96.00 x 0.88)`` = **₹84.48**
the exit                     two sessions later the close (₹88) is under the 21-day EMA, so the
                             sell is the next open: **₹90.00**
the proceeds                 ``90.00 x 1039 x (1 - 0.0025)`` = **₹93,276.225**
the profit                   ``93276.225 - 96.24 x 1039`` = **-₹6,717.135**
the return                   ``-6717.135 / 99993.36 x 100`` = **-6.7176%**
the R                        ``(-0.067176) / ((96.24 - 84.48) / 96.24)`` = **-0.55**
===========================  ==================================================================

Every one of those numbers is asserted in ``test_vbt_backtest.py``. If the engine changes, one of
them moves, and the docstring above says which rule was supposed to produce it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl
from vbt_fixtures import sessions

#: Sessions of history before the signal — enough for the 200-day average and the 21-day EMA.
BASE_SESSIONS = 260
#: The signal, the pullback that fills, the close under the EMA, and the morning it sells.
TRADE_SESSIONS = 4
COUNT = BASE_SESSIONS + TRADE_SESSIONS
DAYS = sessions(COUNT)

SIGNAL_ON = DAYS[BASE_SESSIONS - 1]
FILLED_ON = DAYS[BASE_SESSIONS]
CLOSED_BELOW_EMA_ON = DAYS[BASE_SESSIONS + 1]
SOLD_ON = DAYS[BASE_SESSIONS + 2]

SLEEVE = Decimal("1000000")
EXPECTED_LIMIT = Decimal("96.00")
EXPECTED_ENTRY = Decimal("96.24")
EXPECTED_QUANTITY = 1_039
EXPECTED_STOP = Decimal("84.48")
EXPECTED_EXIT = Decimal("90.00")


def planted_bars() -> pl.DataFrame:
    """Two names: the subject, and a flat one that keeps every session in the calendar."""
    shelf, breakout = 90.0, 96.0
    closes = [20.0 + 0.3 * index for index in range(BASE_SESSIONS - 21)]
    closes += [shelf] * 20
    closes.append(breakout)
    #: the pullback that fills, the close under the EMA, the morning it sells, one more session
    opens = list(closes)
    highs = list(closes)
    lows = [value * 0.99 for value in closes]
    lows[-1] = shelf
    volumes = [250_000.0] * (BASE_SESSIONS - 1) + [1_000_000.0]

    for open_, high, low, close, volume in (
        (97.0, 98.0, 95.0, 97.0, 200_000.0),
        (96.0, 96.0, 88.0, 88.0, 200_000.0),
        (90.0, 91.0, 89.0, 90.0, 200_000.0),
        (90.0, 91.0, 89.0, 90.0, 200_000.0),
    ):
        opens.append(open_)
        highs.append(high)
        lows.append(low)
        closes.append(close)
        volumes.append(volume)

    subject = pl.DataFrame(
        {
            "instrument_id": [1] * COUNT,
            "symbol": ["PLANTED"] * COUNT,
            "date": DAYS,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "close_raw": closes,
            "volume": volumes,
            "upper_circuit": [None] * COUNT,
            "adj_factor": [1.0] * COUNT,
        },
        schema_overrides={"upper_circuit": pl.Float64, "instrument_id": pl.Int64},
    )
    background = pl.DataFrame(
        {
            "instrument_id": [2] * COUNT,
            "symbol": ["FLATCO"] * COUNT,
            "date": DAYS,
            "open": [100.0] * COUNT,
            "high": [100.0] * COUNT,
            "low": [100.0] * COUNT,
            "close": [100.0] * COUNT,
            "close_raw": [100.0] * COUNT,
            "volume": [100_000.0] * COUNT,
            "upper_circuit": [None] * COUNT,
            "adj_factor": [1.0] * COUNT,
        },
        schema_overrides={"upper_circuit": pl.Float64, "instrument_id": pl.Int64},
    )
    return pl.concat([subject, background])


def first_traded_session() -> dt.date:
    """Where the book starts: the session after the planted signal."""
    return FILLED_ON
