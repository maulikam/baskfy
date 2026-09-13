"""``factor_daily`` — the wide fact table, one row per instrument per trading day (docs/04).

PRECISION NOTE — a resolved conflict inside docs/
------------------------------------------------
The inline DDL sketch in docs/04 and the "Precision matters" note directly beneath it disagree.
The note says: "Match the reference product's storage precision exactly (see
docs/13-csv-export-schema.md §4): prices/MAs/returns/sharpe/away-from-high/positive-days at 2 dp,
RSI at 4 dp, volatility and beta at 10 dp (volatility as a decimal fraction, not a percentage),
marketcap as an integer in ₹ crore, volumes as bigint rupees. Round at write time."

The DDL above it uses numeric(18,4) for prices, numeric(12,4) for volatility, numeric(10,4) for
beta, numeric(18,2) for marketcap and numeric(20,2) for volumes. Those two cannot both hold.

This module follows the note (and therefore docs/13 §4), because:
  * the note explicitly defers to docs/13, which README.md calls the acceptance test;
  * numeric(12,4) would round a volatility fraction of 0.5793179400 to 0.5793, and docs/13 §4
    says to keep full precision for volatility and beta "because they feed divisions" —
    sharpe_N = ret_N / (vol_N x 100) is checked to 0.0051 across 1,355 cells in docs/13 §2.
Every column below therefore carries the docs/13 §4 precision. Divergences from the docs/04 DDL
are called out inline.

Nullable everywhere: young listings lack history, and docs/05 requires NULL rather than a
short-window value.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.models.base import (
    AWAY_HIGH,
    FUNDAMENTAL,
    PCT_2DP,
    POS_DAYS,
    PRICE,
    RATIO_10DP,
    RSI,
    Base,
)

#: docs/ranking/PLAN.md C1 — an ATR is a price distance, kept at 4 dp because it divides
#: ``atr_ext_20`` and a 2-dp ATR on a low-priced name would move the extension it produces.
ATR_4DP = Numeric(18, 4)
#: docs/ranking/PLAN.md C1 — dimensionless ratios compared against bands (extension in ATRs,
#: efficiency, scaled acceleration, volume expansion), 4 dp.
RATIO_4DP = Numeric(10, 4)

#: docs/04: factor_daily.regime — Wasserstein regime label.
REGIMES: tuple[str, ...] = ("BULL", "BEAR", "NEUTRAL")


class FactorDaily(Base):
    """TimescaleDB hypertable on ``date`` (1-year chunks). Kept uncompressed for 2 years."""

    __tablename__ = "factor_daily"
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "date"),
        # Covering index for the hot screen query's decile bucketing (docs/06 step 3). ``date`` is
        # the leading column, so this also serves every predicate the dropped
        # ``ix_factor_daily_date`` used to; ``instrument_id`` is in the payload so the bucketing
        # pass reads ``(instrument_id, marketcap_cr)`` for one date index-only. Prompt 16
        # deliverable 2 — migration 0008 has the reasoning.
        Index(
            "ix_factor_daily_date_marketcap_cr",
            "date",
            "marketcap_cr",
            postgresql_using="btree",
            postgresql_include=["instrument_id"],
        ),
    )

    instrument_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instrument.id"))
    date: Mapped[dt.date] = mapped_column(Date)

    # --- Prices (docs/13 §4: 2 dp; docs/04 DDL said 18,4) --------------------
    close: Mapped[Decimal | None] = mapped_column(PRICE)
    close_raw: Mapped[Decimal | None] = mapped_column(PRICE)

    # --- Absolute returns, % (docs/13 §4: 2 dp) ------------------------------
    ret_1m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    ret_3m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    ret_6m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    ret_9m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    ret_12m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    ret_12m_minus_1m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    ret_12m_minus_2m: Mapped[Decimal | None] = mapped_column(PCT_2DP)

    # --- Volatility: annualised DECIMAL FRACTION, 10 dp ----------------------
    # docs/13 §2 finding 4 + §4. docs/04 DDL said numeric(12,4); docs/05 §2 writes the formula
    # with a x100 (percent). The export is the arbiter: values run 0.179-0.618, so it is a
    # fraction, and the UI multiplies by 100. Flagged for Prompt 5.
    vol_1m: Mapped[Decimal | None] = mapped_column(RATIO_10DP)
    vol_3m: Mapped[Decimal | None] = mapped_column(RATIO_10DP)
    vol_6m: Mapped[Decimal | None] = mapped_column(RATIO_10DP)
    vol_9m: Mapped[Decimal | None] = mapped_column(RATIO_10DP)
    vol_12m: Mapped[Decimal | None] = mapped_column(RATIO_10DP)

    # --- "Sharpe return" = ret_N / (vol_N x 100), % , 2 dp -------------------
    sharpe_1m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    sharpe_3m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    sharpe_6m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    sharpe_9m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    sharpe_12m: Mapped[Decimal | None] = mapped_column(PCT_2DP)

    # --- RSI, 4 dp (docs/13 §4; agrees with docs/04 DDL) ---------------------
    rsi_1m: Mapped[Decimal | None] = mapped_column(RSI)
    rsi_3m: Mapped[Decimal | None] = mapped_column(RSI)
    rsi_6m: Mapped[Decimal | None] = mapped_column(RSI)
    rsi_9m: Mapped[Decimal | None] = mapped_column(RSI)
    rsi_12m: Mapped[Decimal | None] = mapped_column(RSI)

    # --- Beta, 10 dp (docs/04 DDL said 10,4) ---------------------------------
    beta_12m: Mapped[Decimal | None] = mapped_column(RATIO_10DP)

    # --- Moving averages and highs, 2 dp -------------------------------------
    ma_20: Mapped[Decimal | None] = mapped_column(PRICE)
    ma_50: Mapped[Decimal | None] = mapped_column(PRICE)
    ma_100: Mapped[Decimal | None] = mapped_column(PRICE)
    ma_200: Mapped[Decimal | None] = mapped_column(PRICE)
    high_1y: Mapped[Decimal | None] = mapped_column(PRICE)
    high_ath: Mapped[Decimal | None] = mapped_column(PRICE)
    away_high_1y: Mapped[Decimal | None] = mapped_column(AWAY_HIGH)
    away_high_ath: Mapped[Decimal | None] = mapped_column(AWAY_HIGH)

    # --- Positive days, % of window, 2 dp (docs/04 DDL said 7,4) -------------
    pos_days_1m: Mapped[Decimal | None] = mapped_column(POS_DAYS)
    pos_days_3m: Mapped[Decimal | None] = mapped_column(POS_DAYS)
    pos_days_6m: Mapped[Decimal | None] = mapped_column(POS_DAYS)
    pos_days_9m: Mapped[Decimal | None] = mapped_column(POS_DAYS)
    pos_days_12m: Mapped[Decimal | None] = mapped_column(POS_DAYS)

    # --- Circuit-hit day counts, integers ------------------------------------
    circuits_1m: Mapped[int | None] = mapped_column(SmallInteger)
    circuits_3m: Mapped[int | None] = mapped_column(SmallInteger)
    circuits_6m: Mapped[int | None] = mapped_column(SmallInteger)
    circuits_9m: Mapped[int | None] = mapped_column(SmallInteger)
    circuits_12m: Mapped[int | None] = mapped_column(SmallInteger)

    # --- Traded value in RUPEES, integer (docs/13 §4 "bigint ₹";
    #     docs/04 DDL said numeric(20,2)) -------------------------------------
    vol_day_val: Mapped[int | None] = mapped_column(BigInteger)
    vol_avg_1w: Mapped[int | None] = mapped_column(BigInteger)
    vol_avg_1m: Mapped[int | None] = mapped_column(BigInteger)
    vol_avg_3m: Mapped[int | None] = mapped_column(BigInteger)
    vol_avg_6m: Mapped[int | None] = mapped_column(BigInteger)
    vol_avg_9m: Mapped[int | None] = mapped_column(BigInteger)
    vol_avg_12m: Mapped[int | None] = mapped_column(BigInteger)
    median_vol_12m: Mapped[int | None] = mapped_column(BigInteger)

    # --- Marketcap: integer ₹ crore (docs/13 §2 finding 7 + §4;
    #     docs/04 DDL said numeric(18,2)) -------------------------------------
    marketcap_cr: Mapped[int | None] = mapped_column(BigInteger)

    # P/E is denormalised here for the screener's WHERE clause even though docs/13 §2 finding 12
    # shows the reference product keeps it off the fact row; `fundamental_daily` remains the
    # source of record and the nightly build copies it forward.
    pe: Mapped[Decimal | None] = mapped_column(FUNDAMENTAL)
    series: Mapped[str | None] = mapped_column(String)

    regime: Mapped[str | None] = mapped_column(String)

    # --- Stored ranking factors (docs/ranking/PLAN.md C1, migration 0046) -------------------
    # Computed by baskfy_core.factors_ranking on adjusted prices, point-in-time, NULL when the
    # window is not full. `mom_pctile` and `rank_persist_20` are cross-sectional/cross-date and
    # are filled by the worker after the day's rows exist.
    atr_14: Mapped[Decimal | None] = mapped_column(ATR_4DP)
    atr_ext_20: Mapped[Decimal | None] = mapped_column(RATIO_4DP)
    ma50_slope_20: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    eff_ratio_63: Mapped[Decimal | None] = mapped_column(RATIO_4DP)
    max_dd_6m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    max_dd_12m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    downside_vol_6m: Mapped[Decimal | None] = mapped_column(RATIO_10DP)
    downside_vol_12m: Mapped[Decimal | None] = mapped_column(RATIO_10DP)
    sortino_6m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    sortino_12m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    underwater_12m: Mapped[Decimal | None] = mapped_column(POS_DAYS)
    ret_ex_top3_12m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    accel_21_105: Mapped[Decimal | None] = mapped_column(RATIO_10DP)
    accel_21_105_vs: Mapped[Decimal | None] = mapped_column(RATIO_4DP)
    vol_exp_21_126: Mapped[Decimal | None] = mapped_column(RATIO_4DP)
    vol_persist_20: Mapped[int | None] = mapped_column(SmallInteger)
    excess_ret_3m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    excess_ret_6m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    excess_ret_12m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    resid_ret_12m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    rs_persist_126: Mapped[Decimal | None] = mapped_column(POS_DAYS)
    mom_pctile: Mapped[Decimal | None] = mapped_column(POS_DAYS)
    rank_persist_20: Mapped[Decimal | None] = mapped_column(POS_DAYS)
    nse_mr6: Mapped[Decimal | None] = mapped_column(RATIO_10DP)
    nse_mr12: Mapped[Decimal | None] = mapped_column(RATIO_10DP)

    # --- Denormalised universe + risk flags (docs/04, docs/06 "Universe flags") ---
    # One bit per index_def.id; see baskfy_core.universes.UNIVERSE_MASK_BIT.
    universe_mask: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    top_beta_mask: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    top_volatility_mask: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
