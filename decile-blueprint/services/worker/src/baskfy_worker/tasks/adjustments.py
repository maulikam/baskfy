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

AF 0.5: nightly ``source='kite'`` bars are adjustable; only ``kite_adjusted`` (M29 deep history)
is excluded from ``adjust_bars``. AF 3.1: after adjusting the post-seam segment, rescale the
``kite_adjusted`` segment by the seam bar's new ``adj_factor`` so a later split cannot reopen a
``1/f`` step at 2024-01-01.
"""

from __future__ import annotations

import datetime as dt
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

#: Provenance that must not be fed to ``adjust_bars`` — vendor-adjusted, no exchange print.
KITE_ADJUSTED_SOURCE: str = "kite_adjusted"


@dataclass(frozen=True, slots=True)
class InstrumentAdjustment:
    """What reprocessing one instrument did."""

    instrument_id: int
    bars_rewritten: int
    actions_applied: int
    unquantified: tuple[str, ...]
    deep_rescaled: int = 0


async def reprocess_instrument(
    session: AsyncSession, instrument_id: int, *, as_of: dt.date | None = None
) -> InstrumentAdjustment:
    """Rebuild one instrument's whole adjusted history from its raw prints.

    Idempotent by construction: the inputs are ``*_raw`` (never written by this step) and the
    corporate-action table, so running it twice writes identical values the second time.

    ``as_of`` bounds which actions may be applied to ``ex_date <= as_of``, and exists because
    ``ohlcv_daily.close`` is an **"as of today" series**: it carries every action known *now*,
    including ones whose ex-date is in the future relative to some earlier date of interest.

    That is not wrong for serving today's screen, and it is wrong for anything reasoning about a
    past date. `docs/DECISIONS.md` §21.9 found it the hard way: eight of the 271 export rows
    disagreed by exactly a dividend, because actions with ``ex_date = 2026-08-21`` had already
    rewritten the adjusted close for **2026-08-18**. House rule 5 is "No look-ahead, ever".

    So the caller states the date it is reconstructing. The nightly pipeline passes nothing and
    gets today's series, which is what it should serve. The parity harness and any point-in-time
    reader pass the as-of they are reproducing, and get the series as it stood that day —
    **without** the storage having to hold one adjusted series per as-of date, which is the larger
    design question §21.9 declined to settle and this does not settle either.
    """
    actions = await _load_actions(session, instrument_id, as_of=as_of)
    bars = await _load_raw_bars(session, instrument_id)
    if bars.height == 0:
        deep_only = await _rescale_deep_segment(session, instrument_id, seam_factor=Decimal(1))
        return InstrumentAdjustment(instrument_id, deep_only, 0, (), deep_only)

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

    # Earliest adjustable bar carries every subsequent action; that factor is what the deep
    # segment must share so the M29 splice stays continuous after a later split.
    earliest = min(values, key=lambda row: row["date"])
    deep_rescaled = await _rescale_deep_segment(
        session, instrument_id, seam_factor=earliest["adj_factor"]
    )

    unquantified = tuple(
        f"{a.action.action_type}@{a.action.ex_date.isoformat()}: {a.detail}"
        for a in result.unquantified_actions
    )
    return InstrumentAdjustment(
        instrument_id,
        written + deep_rescaled,
        len(result.effective_actions),
        unquantified,
        deep_rescaled,
    )


async def _rescale_deep_segment(
    session: AsyncSession, instrument_id: int, *, seam_factor: Decimal
) -> int:
    """Multiply ``kite_adjusted`` OHLC by ``seam_factor / row.adj_factor`` (AF 3.1).

    Deep rows store the vendor-adjusted level with ``adj_factor`` recording how far the post-seam
    series has moved since the splice. Recovering the splice baseline as ``price / adj_factor``
    and writing ``baseline * seam_factor`` keeps the join continuous when a new split lands.
    """
    rows = (
        (
            await session.execute(
                select(OhlcvDaily).where(
                    OhlcvDaily.instrument_id == instrument_id,
                    OhlcvDaily.source == KITE_ADJUSTED_SOURCE,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0
    target = _factor(seam_factor)
    updated = 0
    for row in rows:
        was = row.adj_factor if row.adj_factor not in (None, Decimal(0)) else Decimal(1)
        if was == target:
            continue
        scale = target / was
        row.open = _price(row.open * scale) or row.open
        row.high = _price(row.high * scale) or row.high
        row.low = _price(row.low * scale) or row.low
        row.close = _price(row.close * scale) or row.close
        # close_raw on these rows carries the same adjusted value (no exchange print).
        row.close_raw = _price(row.close_raw * scale) or row.close_raw
        row.adj_factor = target
        updated += 1
    return updated


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


async def _load_actions(
    session: AsyncSession, instrument_id: int, *, as_of: dt.date | None = None
) -> list[CorporateActionInput]:
    """Every action for an instrument, optionally bounded to those already ex as of a date."""
    query = select(CorporateAction).where(CorporateAction.instrument_id == instrument_id)
    if as_of is not None:
        query = query.where(CorporateAction.ex_date <= as_of)
    rows = await session.execute(query.order_by(CorporateAction.ex_date))
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

    Prefer stored ``open_raw``/``high_raw``/``low_raw`` when present (AF 0.6). Otherwise recover
    via ``price / adj_factor`` — identity when ``adj_factor`` is 1. ``kite_adjusted`` rows are
    excluded: they have no exchange print and must not be double-counted (M29); AF 3.1 rescales
    them separately across the seam.
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
            OhlcvDaily.open_raw,
            OhlcvDaily.high_raw,
            OhlcvDaily.low_raw,
        )
        .where(
            OhlcvDaily.instrument_id == instrument_id,
            OhlcvDaily.source != KITE_ADJUSTED_SOURCE,
        )
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
                "open_raw": r[7] if r[7] is not None else _unadjust(r[1], r[6]),
                "high_raw": r[8] if r[8] is not None else _unadjust(r[2], r[6]),
                "low_raw": r[9] if r[9] is not None else _unadjust(r[3], r[6]),
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
    "KITE_ADJUSTED_SOURCE",
    "AdjustmentOutcome",
    "InstrumentAdjustment",
    "instruments_with_actions",
    "outcome_note_for",
    "reprocess_instrument",
    "run_apply_adjustments",
]
