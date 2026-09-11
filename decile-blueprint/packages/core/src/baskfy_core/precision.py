"""Storage precision (Prompt 5 deliverable 7).

    "Storage precision exactly as docs/13 §4: round at WRITE time (prices/returns/sharpe/
     away-from-high/positive-days 2 dp, RSI 4 dp, volatility and beta 10 dp with volatility
     stored as a decimal FRACTION not a percentage, marketcap integer ₹ crore, volumes bigint
     rupees)."

docs/13 §4: "The reference product rounds **at write time**, not at render time. Do the same for
the columns above so exports and UI agree byte-for-byte, but keep full precision for volatility
and beta because they feed divisions."

Rounding at write time is what makes the CSV export, the API response and the screen table agree.
Round at render and three surfaces each round independently — from a value that was never itself
rounded — and they disagree in the last digit, which is exactly the kind of discrepancy that
destroys trust in a numbers product.

Half-up, not banker's rounding: the reference product's values are consistent with half-up, and
Python's default (half-even) would differ on exact halves. Over 271 rows x 93 columns that is a
handful of cells, all of them avoidable.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

import polars as pl

#: docs/13 §4, column family -> decimal places.
PRICE_DP: Final = 2
PERCENT_DP: Final = 2
RSI_DP: Final = 4
RATIO_DP: Final = 10

_EXPONENTS: Final[dict[int, Decimal]] = {
    0: Decimal(1),
    2: Decimal("0.01"),
    4: Decimal("0.0001"),
    10: Decimal("0.0000000001"),
}

#: Which precision each ``factor_daily`` column is written at. The engine rounds by looking a
#: column up here, so adding a column without deciding its precision is impossible.
COLUMN_PRECISION: Final[dict[str, int]] = {
    # Prices, moving averages, highs — 2 dp.
    "close": PRICE_DP,
    "close_raw": PRICE_DP,
    "ma_20": PRICE_DP,
    "ma_50": PRICE_DP,
    "ma_100": PRICE_DP,
    "ma_200": PRICE_DP,
    "high_1y": PRICE_DP,
    "high_ath": PRICE_DP,
    # Returns, sharpe returns, away-from-high, positive days — 2 dp.
    **{f"ret_{k}m": PERCENT_DP for k in (1, 3, 6, 9, 12)},
    "ret_12m_minus_1m": PERCENT_DP,
    "ret_12m_minus_2m": PERCENT_DP,
    **{f"sharpe_{k}m": PERCENT_DP for k in (1, 3, 6, 9, 12)},
    "away_high_1y": PERCENT_DP,
    "away_high_ath": PERCENT_DP,
    **{f"pos_days_{k}m": PERCENT_DP for k in (1, 3, 6, 9, 12)},
    # RSI — 4 dp.
    **{f"rsi_{k}m": RSI_DP for k in (1, 3, 6, 9, 12)},
    # Volatility (a decimal FRACTION) and beta — 10 dp, because they feed divisions.
    **{f"vol_{k}m": RATIO_DP for k in (1, 3, 6, 9, 12)},
    "beta_12m": RATIO_DP,
    # P/E keeps NSE's own 4 dp (docs/04 numeric(14,4)).
    "pe": RSI_DP,
    # --- SW3: `sw_setup_daily` (docs/swing/03 §2) --------------------------------------
    #
    # The same rule for the same reason: the detector's numbers are written once, rounded, and
    # every surface then reads the stored value. Without this the API would round a level to two
    # places for display, the CSV would round it again from full precision, and a trigger shown
    # as 149.60 would be sent to the broker as 149.6025.
    #
    # Levels and the measurements are 2 dp (`PRICE` and `numeric(10,2)` in `03` §2); `adj_factor`
    # keeps ten, because it is a divisor -- the level stored is `adjusted / adj_factor`, and a
    # factor rounded to two places would move the exchange price it produces.
    "trigger": PRICE_DP,
    "stop_ref": PRICE_DP,
    "pivot_high": PRICE_DP,
    "score": PERCENT_DP,
    "adr_pct": PERCENT_DP,
    "prior_move_pct": PERCENT_DP,
    "base_depth_pct": PERCENT_DP,
    "tightness_adr": PERCENT_DP,
    "dryup_ratio": PERCENT_DP,
    "dist_ma_fast_pct": PERCENT_DP,
    "dist_ma_slow_pct": PERCENT_DP,
    "rvol": PERCENT_DP,
    "gap_pct": PERCENT_DP,
    "adj_factor": RATIO_DP,
    # --- VB4: `vb_signal_daily` and `vb_breadth_daily` (docs/vbt/03 §2, §3) ---------------
    #
    # The same rule for the same reason: the detector's numbers are written once, rounded, and
    # every surface reads the stored value. A limit shown as 137.35 must not be sent to the
    # broker as 137.3512.
    #
    # `close_position` and `pct_above_dma` keep four places, because both are shares rather than
    # prices: two would round a 0.6015 close position to 0.60, which is the exact value filter D
    # tests against, and a breadth of 40.0049% to 40.00, which is the exact value the gate tests
    # against. Rounding a number to the precision of its own threshold is how a rule starts
    # disagreeing with the row that recorded it.
    "limit_price": PRICE_DP,
    "stop_price": PRICE_DP,
    "sma_200": PRICE_DP,
    "ema_21": PRICE_DP,
    "high_20_prior": PRICE_DP,
    "change_pct": PERCENT_DP,
    "ret_20_pct": PERCENT_DP,
    "close_position": RSI_DP,
    "pct_above_dma": RSI_DP,
    # --- TW4: `tw_state_daily`, `tw_signal_daily` and `tw_position` (docs/twt/03 §2, §3, §5) ---
    #
    # The same rule for the same reason, and on this sleeve it has teeth the others do not have:
    # a TWT line is held for months, so the stop that is resting at the exchange was written from
    # a number computed one specific evening. A trigger shown as 241.65 must not be sent as
    # 241.6512, and `high_since` must not drift in the fourth place over six hundred sessions of
    # maxima.
    #
    # `week_range_pct` and `month_low_ratio` keep **four** places because both are the exact
    # quantities the rules compare against: `04` §3.1 line 3 tests the range against 3.01 and
    # line 2 tests the ratio against 1.3, and `03` §2 stores them as numeric(10,4). Rounding a
    # number to the precision of its own threshold is how a rule starts disagreeing with the row
    # that recorded it -- the same argument `close_position` and `pct_above_dma` make above.
    #
    # The prices are 2 dp even where the column is PRICE_RAW (18,4), exactly as `close_raw` is:
    # the exchange quotes cash equities in paise, and the extra two places are headroom for an
    # adjusted series rather than precision anybody trades on.
    "week_close_0": PRICE_DP,
    "week_close_1": PRICE_DP,
    "week_close_2": PRICE_DP,
    "week_range_pct": RSI_DP,
    "month_low_3": PRICE_DP,
    "month_low_ratio": RSI_DP,
    "sma_dma": PRICE_DP,
    "entry_reference_close": PRICE_DP,
    "stop_preview": PRICE_DP,
    "high_since": PRICE_DP,
    "next_trigger": PRICE_DP,
}

#: Columns stored as whole numbers: marketcap in ₹ crore, turnover and volumes in ₹.
INTEGER_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "marketcap_cr",
        "vol_day_val",
        "vol_avg_1w",
        "vol_avg_1m",
        "vol_avg_3m",
        "vol_avg_6m",
        "vol_avg_9m",
        "vol_avg_12m",
        "median_vol_12m",
        "circuits_1m",
        "circuits_3m",
        "circuits_6m",
        "circuits_9m",
        "circuits_12m",
        # SW3: `sw_setup_daily.turnover_avg` is bigint rupees, like every other volume column.
        "turnover_avg",
        # VB4: `vb_signal_daily`'s volumes and rupee turnovers, and the rank key, which is one
        # of them (`04` §3.4 ranks by the signal day's own turnover).
        "vol_sma_50",
        "turnover_inr",
        "turnover_avg_20",
        "rank_key",
    }
)


def quantise(value: object, places: int) -> Decimal | None:
    """Round one value half-up to ``places`` decimals. ``None`` passes through."""
    if value is None:
        return None
    try:
        exponent = _EXPONENTS[places]
    except KeyError as exc:
        raise ValueError(f"unsupported precision {places}; expected {sorted(_EXPONENTS)}") from exc
    try:
        candidate = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not candidate.is_finite():
        # A division by a zero volatility or a zero beta produces inf/nan. docs/05 §3 and §7 both
        # say the answer there is NULL, and a NULL is what the schema stores.
        return None
    return candidate.quantize(exponent, rounding=ROUND_HALF_UP)


def round_expr(column: str, places: int) -> pl.Expr:
    """Round a float column half-up at write time, as a Polars expression."""
    scale = pl.lit(10.0**places)
    value = pl.col(column)
    return (
        pl.when(value.is_null() | value.is_infinite() | value.is_nan())
        .then(None)
        .otherwise((value.abs() * scale + 0.5).floor() / scale * value.sign())
        .alias(column)
    )


def integer_expr(column: str) -> pl.Expr:
    """Round to a whole number half-up, for the ₹ and ₹-crore columns."""
    value = pl.col(column)
    return (
        pl.when(value.is_null() | value.is_infinite() | value.is_nan())
        .then(None)
        .otherwise((value.abs() + 0.5).floor() * value.sign())
        .cast(pl.Int64)
        .alias(column)
    )


def apply_storage_precision(frame: pl.DataFrame) -> pl.DataFrame:
    """Round every recognised column to its documented precision.

    Columns not in the table are left alone — they are keys, labels or masks, none of which is a
    measurement. A *numeric* column that is missing from the table is a bug, and
    :func:`unpriced_numeric_columns` is what the tests use to catch one.
    """
    expressions = [
        round_expr(name, places)
        for name, places in COLUMN_PRECISION.items()
        if name in frame.columns and frame.schema[name] in (pl.Float64, pl.Float32)
    ]
    expressions += [
        integer_expr(name)
        for name in INTEGER_COLUMNS
        if name in frame.columns and frame.schema[name] in (pl.Float64, pl.Float32)
    ]
    return frame.with_columns(expressions) if expressions else frame


def unpriced_numeric_columns(frame: pl.DataFrame) -> list[str]:
    """Float columns with no documented storage precision — always a bug."""
    known = set(COLUMN_PRECISION) | INTEGER_COLUMNS
    return sorted(
        name
        for name, dtype in frame.schema.items()
        if dtype in (pl.Float64, pl.Float32) and name not in known
    )
