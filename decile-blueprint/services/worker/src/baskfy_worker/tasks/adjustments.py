"""Step 4 — ``apply_adjustments`` and ``reprocess_instrument`` (Prompt 3 deliverable 4).

docs/03: "recompute adj factors; rewrite close/open/high/low/volume".
docs/09 §"Adjustment algorithm" gives the maths, which lives in ``baskfy_core.adjustments``
(pure, per docs/02's I/O-free rule). This module is the database half.

The rebuild rule, quoted:

    "when a new corporate action arrives, recompute adjustments for that instrument over its whole
     history, then recompute *all* of its `factor_daily` rows. Make this a single idempotent task
     `reprocess_instrument(instrument_id)`."

Whole history, not the affected window: ``adj_factor[d]`` is the cumulative product of every
action after ``d``, so one new action changes the factor on every earlier bar. Recomputing from
scratch each time is also what makes the task idempotent — there is no incremental state to get
out of step with reality.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

import polars as pl
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.adjustments import (
    AdjustmentOutcome,
    CorporateActionInput,
    adjust_bars,
)
from baskfy_core.models import CorporateAction, OhlcvDaily
from baskfy_worker.steps import StepOutcome

#: docs/04: ohlcv_daily prices are numeric(18,4); adj_factor is numeric(18,10).
PRICE_EXPONENT: Decimal = Decimal("0.0001")
FACTOR_EXPONENT: Decimal = Decimal("0.0000000001")

UPSERT_CHUNK: int = 2000


@dataclass(frozen=True, slots=True)
class InstrumentAdjustment:
    """What reprocessing one instrument did."""

    instrument_id: int
    bars_rewritten: int
    actions_applied: int
    unquantified: tuple[str, ...]


async def reprocess_instrument(session: AsyncSession, instrument_id: int) -> InstrumentAdjustment:
    """Rebuild one instrument's whole adjusted history from its raw prints.

    Idempotent by construction: the inputs are ``*_raw`` (never written by this step) and the
    corporate-action table, so running it twice writes identical values the second time.
    """
    actions = await _load_actions(session, instrument_id)
    bars = await _load_raw_bars(session, instrument_id)
    if bars.height == 0:
        return InstrumentAdjustment(instrument_id, 0, 0, ())

    result = adjust_bars(bars, actions)
    values = [
        {
            "instrument_id": instrument_id,
            "date": row["date"],
            "open": _price(row["open"]),
            "high": _price(row["high"]),
            "low": _price(row["low"]),
            "close": _price(row["close"]),
            "volume": int(row["volume"]) if row["volume"] is not None else 0,
            "adj_factor": _factor(row["adj_factor"]),
        }
        for row in result.bars.iter_rows(named=True)
    ]

    written = 0
    for offset in range(0, len(values), UPSERT_CHUNK):
        chunk = values[offset : offset + UPSERT_CHUNK]
        stmt = insert(OhlcvDaily).values(
            [
                # The conflict target rows already exist; the raw columns are carried through
                # unchanged so the statement is a legal INSERT ... ON CONFLICT.
                {
                    **value,
                    "close_raw": value["close"],
                    "volume_raw": value["volume"],
                    "source": "nse",
                }
                for value in chunk
            ]
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[OhlcvDaily.instrument_id, OhlcvDaily.date],
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume,
                    "adj_factor": stmt.excluded.adj_factor,
                },
            )
        )
        written += len(chunk)

    unquantified = tuple(
        f"{a.action.action_type}@{a.action.ex_date.isoformat()}: {a.detail}"
        for a in result.unquantified_actions
    )
    return InstrumentAdjustment(instrument_id, written, len(result.effective_actions), unquantified)


async def run_apply_adjustments(
    session: AsyncSession, outcome: StepOutcome, instrument_ids: Iterable[int]
) -> int:
    """docs/09 §Schedule: "apply_adjustments (only instruments with new actions)"."""
    targets = sorted(set(instrument_ids))
    outcome.rows_in = len(targets)
    if not targets:
        outcome.note(reason="no instrument had a new corporate action")
        return 0

    rewritten = 0
    applied = 0
    unquantified: list[str] = []
    for instrument_id in targets:
        result = await reprocess_instrument(session, instrument_id)
        rewritten += result.bars_rewritten
        applied += result.actions_applied
        unquantified.extend(f"instrument {instrument_id}: {u}" for u in result.unquantified)

    outcome.rows_out = rewritten
    outcome.note(
        instruments=len(targets),
        actions_applied=applied,
        # Surfaced rather than dropped: an action we recognised but could not quantify leaves the
        # series unadjusted across its ex-date, and only this line says so.
        unquantified_actions=unquantified or None,
    )
    return rewritten


async def instruments_with_actions(session: AsyncSession) -> list[int]:
    """Every instrument that has any corporate action at all — the full-rebuild target set."""
    rows = await session.execute(
        select(CorporateAction.instrument_id).distinct().order_by(CorporateAction.instrument_id)
    )
    return [row[0] for row in rows]


async def _load_actions(session: AsyncSession, instrument_id: int) -> list[CorporateActionInput]:
    rows = await session.execute(
        select(CorporateAction)
        .where(CorporateAction.instrument_id == instrument_id)
        .order_by(CorporateAction.ex_date)
    )
    return [
        CorporateActionInput(
            action_type=row.action_type,
            ex_date=row.ex_date,
            ratio_from=row.ratio_from,
            ratio_to=row.ratio_to,
            amount=row.amount,
            issue_price=_issue_price(row.raw),
        )
        for row in rows.scalars()
    ]


def _issue_price(raw: object) -> Decimal | None:
    """A rights issue's subscription price, when the source payload happens to carry one.

    ``baskfy_core.adjustments`` refuses to adjust a rights issue without it rather than guessing;
    this is the one place that price can enter the system.
    """
    if not isinstance(raw, dict):
        return None
    for key in ("issue_price", "subscription_price", "premium"):
        value = raw.get(key)
        if isinstance(value, (int, float, str)):
            try:
                return Decimal(str(value))
            except (ValueError, ArithmeticError):
                return None
    return None


async def _load_raw_bars(session: AsyncSession, instrument_id: int) -> pl.DataFrame:
    """Recover the unadjusted OHLCV for one instrument.

    ``close_raw`` and ``volume_raw`` are stored directly (docs/04), but there is no
    ``open_raw``/``high_raw``/``low_raw`` column — and ``open``/``high``/``low`` are *outputs* of
    this very step. Reading them back as if they were raw would re-adjust an already-adjusted
    series, compounding the factor on every run and breaking idempotency.

    docs/09 anticipates exactly this: "Store `adj_factor` per row so any adjusted number can be
    reverse-engineered." So the raw prints are recovered as ``open / adj_factor``. On a first run
    ``adj_factor`` is 1 and this is the identity; on every later run it undoes precisely what the
    previous run applied.
    """
    rows = await session.execute(
        select(
            OhlcvDaily.date,
            OhlcvDaily.open,
            OhlcvDaily.high,
            OhlcvDaily.low,
            OhlcvDaily.close_raw,
            OhlcvDaily.volume_raw,
            OhlcvDaily.adj_factor,
        )
        .where(OhlcvDaily.instrument_id == instrument_id)
        .order_by(OhlcvDaily.date)
    )
    records = rows.all()
    if not records:
        return pl.DataFrame(
            schema={
                "date": pl.Date(),
                "open_raw": pl.Decimal(38, 4),
                "high_raw": pl.Decimal(38, 4),
                "low_raw": pl.Decimal(38, 4),
                "close_raw": pl.Decimal(38, 4),
                "volume_raw": pl.Int64(),
            }
        )
    return pl.DataFrame(
        [
            {
                "date": r[0],
                "open_raw": _unadjust(r[1], r[6]),
                "high_raw": _unadjust(r[2], r[6]),
                "low_raw": _unadjust(r[3], r[6]),
                "close_raw": r[4],
                "volume_raw": r[5],
            }
            for r in records
        ],
        strict=False,
    )


def _unadjust(value: Decimal | None, adj_factor: Decimal | None) -> Decimal | None:
    """Recover an unadjusted price from a stored adjusted one and its factor."""
    if value is None:
        return None
    if adj_factor is None or adj_factor == 0:
        return value
    return value / adj_factor


def _price(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(PRICE_EXPONENT, rounding=ROUND_HALF_UP)


def _factor(value: object) -> Decimal:
    if value is None:
        return Decimal(1)
    return Decimal(str(value)).quantize(FACTOR_EXPONENT, rounding=ROUND_HALF_UP)


def outcome_note_for(results: Sequence[InstrumentAdjustment]) -> dict[str, object]:
    return {
        "instruments": len(results),
        "bars_rewritten": sum(r.bars_rewritten for r in results),
        "actions_applied": sum(r.actions_applied for r in results),
    }


__all__ = [
    "AdjustmentOutcome",
    "InstrumentAdjustment",
    "instruments_with_actions",
    "outcome_note_for",
    "reprocess_instrument",
    "run_apply_adjustments",
]
