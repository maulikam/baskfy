"""The corporate-action adjustment algorithm, exactly as docs/09 specifies it.

    "Process corporate actions in reverse-chronological order. For each action with ex-date `e`,
     all bars with `date < e` are multiplied by a factor:

        Split  a:b              b/a          volume x a/b
        Bonus  a:b              b/(a+b)      volume x (a+b)/b
        Rights                  (P_cum - theoretical value of right) / P_cum   volume unchanged
        Cash dividend D         (P_cum - D) / P_cum                            volume unchanged

     Cumulative product of all factors after date `d` = `adj_factor[d]`; then
     `close = close_raw x adj_factor`. Store `adj_factor` per row so any adjusted number can be
     reverse-engineered."

This module is pure — frames in, frames out — because docs/02 §"Repo layout" requires
``packages/core`` to stay I/O-free: "That is what makes the factor math unit-testable and the
backtest engine reusable." The worker owns the database reads and writes.

Ratio orientation follows docs/04: split 10:1 -> ``ratio_from=10, ratio_to=1``;
bonus 4:1 -> ``ratio_from=4, ratio_to=1``. So in docs/09's table, ``a = ratio_from`` and
``b = ratio_to``. Checking that against reality: a 10:1 split multiplies the share count by ten,
so pre-ex prices must be scaled by ``b/a = 1/10`` and volumes by ``a/b = 10``. A 4:1 bonus leaves
a holder with five shares for every one, so prices scale by ``b/(a+b) = 1/5`` and volumes by five.

Everything is computed in :class:`~decimal.Decimal`. Adjustment factors are a running product
across fifteen years; in float that product accumulates error that shows up as a price series
that no longer reconciles with the exchange print (docs/04: money in numeric, never float).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

import polars as pl

#: docs/04 §"Corporate actions".
ACTION_SPLIT: Final = "split"
ACTION_BONUS: Final = "bonus"
ACTION_RIGHTS: Final = "rights"
ACTION_DIVIDEND: Final = "dividend"
ACTION_DEMERGER: Final = "demerger"

#: The precision docs/04 stores ``ohlcv_daily.adj_factor`` at.
ADJ_FACTOR_EXPONENT: Final = Decimal("0.0000000001")

#: An action that changes nothing.
UNIT_FACTOR: Final = Decimal(1)


class AdjustmentOutcome(StrEnum):
    """Why an action produced the factor it did.

    Recorded per action so an operator can tell "this action was applied" from "this action was
    recognised but could not be quantified" — the two look identical in the resulting price
    series, and only one of them is correct.
    """

    APPLIED = "applied"
    #: The action type does not adjust prices (a demerger needs a per-case entitlement ratio).
    NOT_PRICE_AFFECTING = "not_price_affecting"
    #: Recognised, but a required input is missing. Factor left at 1.0 rather than guessed.
    INSUFFICIENT_DATA = "insufficient_data"
    #: The ratio or amount present in the row cannot produce a sane factor.
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class CorporateActionInput:
    """One action, as the adjustment step needs it."""

    action_type: str
    ex_date: dt.date
    ratio_from: Decimal | None = None
    ratio_to: Decimal | None = None
    amount: Decimal | None = None
    #: Subscription price of a rights issue, when known. See :func:`rights_factor`.
    issue_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AppliedAction:
    """The factor an action contributed, and why."""

    action: CorporateActionInput
    price_factor: Decimal
    volume_factor: Decimal
    outcome: AdjustmentOutcome
    detail: str = ""

    @property
    def is_effective(self) -> bool:
        return self.price_factor != UNIT_FACTOR or self.volume_factor != UNIT_FACTOR


def split_factor(ratio_from: Decimal, ratio_to: Decimal) -> tuple[Decimal, Decimal]:
    """docs/09: split ``a:b`` -> price ``b/a``, volume ``a/b``."""
    _require_positive(ratio_from, ratio_to)
    return ratio_to / ratio_from, ratio_from / ratio_to


def bonus_factor(ratio_from: Decimal, ratio_to: Decimal) -> tuple[Decimal, Decimal]:
    """docs/09: bonus ``a:b`` (a new for b held) -> price ``b/(a+b)``, volume ``(a+b)/b``."""
    _require_positive(ratio_from, ratio_to)
    total = ratio_from + ratio_to
    return ratio_to / total, total / ratio_to


def dividend_factor(previous_close: Decimal, amount: Decimal) -> Decimal:
    """docs/09: cash dividend ``D`` -> ``(P_cum - D) / P_cum``.

    ``P_cum`` is the last close *before* the ex-date — the cum-dividend price, which is what the
    dividend is being stripped out of.
    """
    if previous_close <= 0:
        raise ValueError(f"cum price must be positive; got {previous_close}")
    if amount < 0:
        raise ValueError(f"dividend amount cannot be negative; got {amount}")
    factor = (previous_close - amount) / previous_close
    if factor <= 0:
        raise ValueError(
            f"dividend {amount} is not less than the cum price {previous_close}; "
            "the resulting factor would be zero or negative"
        )
    return factor


def rights_factor(
    previous_close: Decimal, ratio_from: Decimal, ratio_to: Decimal, issue_price: Decimal
) -> Decimal:
    """docs/09: ``(P_cum - theoretical value of right) / P_cum``.

    docs/09 states the shape but not how the theoretical value is derived, so the standard TERP
    identity is used: for a rights issue of ``a`` new shares for every ``b`` held at subscription
    price ``S``, the value of one right attached to an existing share is

        ``(P_cum - S) x a / (a + b)``

    which makes the factor ``TERP / P_cum`` where ``TERP = (b x P_cum + a x S) / (a + b)``.

    A rights issue priced at or above the market carries no value, so the factor is clamped at 1:
    a negative right would otherwise *raise* historical prices.
    """
    _require_positive(ratio_from, ratio_to)
    if previous_close <= 0:
        raise ValueError(f"cum price must be positive; got {previous_close}")
    if issue_price < 0:
        raise ValueError(f"issue price cannot be negative; got {issue_price}")
    right_value = (previous_close - issue_price) * ratio_from / (ratio_from + ratio_to)
    if right_value <= 0:
        return UNIT_FACTOR
    return (previous_close - right_value) / previous_close


def resolve_action(action: CorporateActionInput, previous_close: Decimal | None) -> AppliedAction:
    """Turn one action into its price and volume factors.

    Never guesses. An action whose inputs are missing gets a factor of 1.0 and an
    ``INSUFFICIENT_DATA`` outcome, because a wrong factor silently rewrites every price before
    the ex-date and there is nothing in the resulting series to reveal it.
    """
    try:
        return _resolve(action, previous_close)
    except (ValueError, InvalidOperation, ZeroDivisionError) as exc:
        return AppliedAction(action, UNIT_FACTOR, UNIT_FACTOR, AdjustmentOutcome.INVALID, str(exc))


def _resolve_split(action: CorporateActionInput, previous_close: Decimal | None) -> AppliedAction:
    del previous_close
    if action.ratio_from is None or action.ratio_to is None:
        return _insufficient(action, "split has no ratio")
    price, volume = split_factor(action.ratio_from, action.ratio_to)
    return AppliedAction(action, price, volume, AdjustmentOutcome.APPLIED)


def _resolve_bonus(action: CorporateActionInput, previous_close: Decimal | None) -> AppliedAction:
    del previous_close
    if action.ratio_from is None or action.ratio_to is None:
        return _insufficient(action, "bonus has no ratio")
    price, volume = bonus_factor(action.ratio_from, action.ratio_to)
    return AppliedAction(action, price, volume, AdjustmentOutcome.APPLIED)


def _resolve_dividend(
    action: CorporateActionInput, previous_close: Decimal | None
) -> AppliedAction:
    if action.amount is None:
        return _insufficient(action, "dividend has no amount")
    if previous_close is None:
        return _insufficient(action, "no cum-dividend close before the ex-date")
    return AppliedAction(
        action,
        dividend_factor(previous_close, action.amount),
        UNIT_FACTOR,
        AdjustmentOutcome.APPLIED,
    )


def _resolve_rights(action: CorporateActionInput, previous_close: Decimal | None) -> AppliedAction:
    if action.ratio_from is None or action.ratio_to is None:
        return _insufficient(action, "rights issue has no ratio")
    if action.issue_price is None:
        # NSE's corporate-action feed publishes a free-text purpose that usually omits the
        # subscription price, and TERP is undefined without it. Leaving the series unadjusted is
        # visibly wrong at the ex-date; a guessed price is invisibly wrong everywhere before it.
        return _insufficient(action, "rights issue has no subscription price")
    if previous_close is None:
        return _insufficient(action, "no cum-rights close before the ex-date")
    return AppliedAction(
        action,
        rights_factor(previous_close, action.ratio_from, action.ratio_to, action.issue_price),
        UNIT_FACTOR,
        AdjustmentOutcome.APPLIED,
    )


def _resolve_demerger(
    action: CorporateActionInput, previous_close: Decimal | None
) -> AppliedAction:
    del previous_close
    # The entitlement ratio and the resulting-entity value are case-by-case and are not in any
    # file we ingest. docs/09's table gives no formula for it either.
    return AppliedAction(
        action,
        UNIT_FACTOR,
        UNIT_FACTOR,
        AdjustmentOutcome.NOT_PRICE_AFFECTING,
        "demerger adjustment needs a per-case entitlement ratio",
    )


#: docs/09's adjustment table, one resolver per row.
_RESOLVERS: Final[dict[str, Callable[[CorporateActionInput, Decimal | None], AppliedAction]]] = {
    ACTION_SPLIT: _resolve_split,
    ACTION_BONUS: _resolve_bonus,
    ACTION_DIVIDEND: _resolve_dividend,
    ACTION_RIGHTS: _resolve_rights,
    ACTION_DEMERGER: _resolve_demerger,
}


def _resolve(action: CorporateActionInput, previous_close: Decimal | None) -> AppliedAction:
    resolver = _RESOLVERS.get(action.action_type)
    if resolver is None:
        return AppliedAction(
            action,
            UNIT_FACTOR,
            UNIT_FACTOR,
            AdjustmentOutcome.NOT_PRICE_AFFECTING,
            f"unrecognised action type {action.action_type!r}",
        )
    return resolver(action, previous_close)


def _insufficient(action: CorporateActionInput, reason: str) -> AppliedAction:
    return AppliedAction(
        action, UNIT_FACTOR, UNIT_FACTOR, AdjustmentOutcome.INSUFFICIENT_DATA, reason
    )


def _require_positive(*values: Decimal) -> None:
    for value in values:
        if value <= 0:
            raise ValueError(f"ratio legs must be positive; got {value}")


@dataclass(frozen=True, slots=True)
class AdjustmentResult:
    """Adjusted bars plus the audit trail that produced them."""

    #: ``date``, ``adj_factor``, ``volume_factor``, and the adjusted ``open/high/low/close/volume``.
    bars: pl.DataFrame
    applied: tuple[AppliedAction, ...]

    @property
    def effective_actions(self) -> tuple[AppliedAction, ...]:
        return tuple(a for a in self.applied if a.is_effective)

    @property
    def unquantified_actions(self) -> tuple[AppliedAction, ...]:
        """Actions we recognised but could not turn into a factor. Never silently dropped."""
        return tuple(
            a
            for a in self.applied
            if a.outcome in (AdjustmentOutcome.INSUFFICIENT_DATA, AdjustmentOutcome.INVALID)
        )


#: The columns :func:`adjust_bars` expects on the way in.
RAW_BAR_COLUMNS: Final[tuple[str, ...]] = (
    "date",
    "open_raw",
    "high_raw",
    "low_raw",
    "close_raw",
    "volume_raw",
)


def adjust_bars(bars: pl.DataFrame, actions: list[CorporateActionInput]) -> AdjustmentResult:
    """Apply ``actions`` to a single instrument's raw bar history.

    ``bars`` must carry :data:`RAW_BAR_COLUMNS`, sorted or not, one row per trading day.

    The output adds:
        ``adj_factor``  cumulative product of the price factors of every action with a *later*
                        ex-date — docs/09's "cumulative product of all factors after date d";
        ``volume_factor`` the same for volumes;
        ``open/high/low/close`` = raw x ``adj_factor``, and ``volume`` = raw x ``volume_factor``.

    The most recent bars therefore have ``adj_factor = 1`` and equal their exchange prints, which
    is what makes the adjusted series comparable with a quote screen today.
    """
    missing = [c for c in RAW_BAR_COLUMNS if c not in bars.columns]
    if missing:
        raise ValueError(f"bars are missing required columns {missing}; got {bars.columns}")
    if bars.height == 0:
        return AdjustmentResult(_empty_adjusted(bars), ())

    ordered = bars.sort("date")
    closes: dict[dt.date, Decimal] = {
        row["date"]: row["close_raw"]
        for row in ordered.select(["date", "close_raw"]).iter_rows(named=True)
        if row["close_raw"] is not None
    }
    dates = list(closes.keys()) if closes else []

    # docs/09: "Process corporate actions in reverse-chronological order."
    applied: list[AppliedAction] = []
    for action in sorted(actions, key=lambda a: a.ex_date, reverse=True):
        applied.append(resolve_action(action, _cum_close(dates, closes, action.ex_date)))

    price_expr = pl.lit(UNIT_FACTOR, dtype=pl.Decimal(38, 10))
    volume_expr = pl.lit(UNIT_FACTOR, dtype=pl.Decimal(38, 10))
    for entry in applied:
        if not entry.is_effective:
            continue
        # "all bars with date < e are multiplied by a factor"
        before_ex = pl.col("date") < entry.action.ex_date
        price_expr = price_expr * pl.when(before_ex).then(
            pl.lit(entry.price_factor, dtype=pl.Decimal(38, 10))
        ).otherwise(pl.lit(UNIT_FACTOR, dtype=pl.Decimal(38, 10)))
        volume_expr = volume_expr * pl.when(before_ex).then(
            pl.lit(entry.volume_factor, dtype=pl.Decimal(38, 10))
        ).otherwise(pl.lit(UNIT_FACTOR, dtype=pl.Decimal(38, 10)))

    adjusted = ordered.with_columns(
        price_expr.alias("adj_factor"), volume_expr.alias("volume_factor")
    ).with_columns(
        (pl.col("open_raw") * pl.col("adj_factor")).alias("open"),
        (pl.col("high_raw") * pl.col("adj_factor")).alias("high"),
        (pl.col("low_raw") * pl.col("adj_factor")).alias("low"),
        (pl.col("close_raw") * pl.col("adj_factor")).alias("close"),
        (pl.col("volume_raw") * pl.col("volume_factor")).alias("volume"),
    )
    return AdjustmentResult(adjusted, tuple(applied))


def _cum_close(
    dates: list[dt.date], closes: dict[dt.date, Decimal], ex_date: dt.date
) -> Decimal | None:
    """The last close strictly before ``ex_date`` — the cum price docs/09's formulas use."""
    candidate: Decimal | None = None
    for day in dates:
        if day >= ex_date:
            break
        candidate = closes[day]
    return candidate


def _empty_adjusted(bars: pl.DataFrame) -> pl.DataFrame:
    return bars.with_columns(
        pl.lit(None, dtype=pl.Decimal(38, 10)).alias("adj_factor"),
        pl.lit(None, dtype=pl.Decimal(38, 10)).alias("volume_factor"),
        pl.lit(None, dtype=pl.Decimal(38, 4)).alias("open"),
        pl.lit(None, dtype=pl.Decimal(38, 4)).alias("high"),
        pl.lit(None, dtype=pl.Decimal(38, 4)).alias("low"),
        pl.lit(None, dtype=pl.Decimal(38, 4)).alias("close"),
        pl.lit(None, dtype=pl.Int64).alias("volume"),
    )
