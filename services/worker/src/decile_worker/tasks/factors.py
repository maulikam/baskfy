"""Step 7 — ``compute_factors`` (Prompt 5 deliverables 5 and 9).

    "compute_factors task that materialises factor_daily for a date (and for a date range in
     backfill), vectorised — no Python loops over instruments."

    "Materialise the denormalised universe / top-beta / top-volatility masks onto factor_daily
     (docs/04, docs/06 'Universe flags'), with TOP_RISK_FLAG_PERCENTILE = 0.10 as a named
     constant, computed over the WHOLE universe, and an assertion that the masks agree with
     index_member_daily."

The arithmetic lives in ``decile_core.factors`` (pure, per docs/02's I/O-free rule for
``packages/core``); this module reads the bars, runs the engine and writes ``factor_daily``.

Why the risk masks are computed here and not in the engine
----------------------------------------------------------
docs/06 §"Step 4", corrected by the CSV export:

    "'Ignore Top Beta / Volatility' is **not** a relative filter over the surviving rows. The
     reference product stores a precomputed boolean per instrument **per universe** ... Proof:
     within every universe in the export, the minimum beta among flagged rows strictly exceeds
     the maximum beta among unflagged rows — a clean rank threshold that is impossible if the cut
     were computed after the other filters had removed rows. So the cut is taken over the **whole
     universe**, nightly, and the screener just tests a boolean."

A per-universe rank needs membership, which is database state, so it belongs on this side of the
I/O boundary. Doing it after filtering — the tempting shortcut — would change which stocks are
excluded depending on what else the user filtered on, and the export proves the reference product
does not work that way.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Protocol

import polars as pl
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from decile_core.factors import TOP_RISK_FLAG_PERCENTILE, FactorResult
from decile_core.models import FactorDaily, IndexMemberDaily, TradingDay
from decile_core.seed_data import NSE_EXCHANGE_ID
from decile_core.universes import UNIVERSE_BY_SLUG, UNIVERSES
from decile_worker.engine import LoadedHistory, PolarsFactorEngine, load_fundamentals, load_history
from decile_worker.steps import StepOutcome

UPSERT_CHUNK: int = 2000

#: PostgreSQL's wire protocol caps one statement at 32,767 bound parameters.
#:
#: `factor_daily` is wide — around seventy columns — so a chunk sized in *rows* is sized in the
#: wrong unit: 2,000 rows is fine for the 40-instrument fixture and impossible for the ~2,400
#: instruments a real NSE day carries (2,000 x 70 = 140,000 parameters). The first real backfill
#: hit exactly that: "the number of query arguments cannot exceed 32767".
#:
#: So the row count is derived from the payload's own width at call time rather than guessed,
#: which keeps it correct if a factor is ever added to the registry.
MAX_BIND_PARAMS: int = 32_767

#: A step payload is for diagnosis, not for a full dump; report enough to act on.
MAX_REPORTED_MISMATCHES: int = 20


class FactorEngine(Protocol):
    """Turns a per-instrument daily frame into one wide ``factor_daily`` row per instrument.

    docs/05: "Implement it in `packages/core/factors.py` as pure Polars expressions over a
    per-instrument daily frame, and pin it with golden tests." ``PolarsFactorEngine`` in
    ``decile_worker.engine`` is the implementation; the protocol stays so a recalibrated engine
    (docs/05 §8, §12 are both INFERRED) can be swapped in without touching the pipeline.
    """

    @property
    def name(self) -> str: ...

    def run(self, history: LoadedHistory, as_of: dt.date) -> FactorResult: ...


async def run_compute_factors(
    session: AsyncSession,
    outcome: StepOutcome,
    on: dt.date,
    engine: FactorEngine | None = None,
) -> int:
    """Materialise one ``factor_daily`` row per instrument that has a bar on ``on``.

    One row per bar is the invariant the quality gate's assertion 4 checks
    ("`factor_daily` row count == `ohlcv_daily` row count for the date, ±0").
    """
    active = engine or PolarsFactorEngine()
    history = await load_history(session, on)
    outcome.rows_in = history.bars.filter(pl.col("date") == on).height
    if outcome.rows_in == 0:
        outcome.note(engine=active.name, reason="no bars for this date")
        return 0

    result = active.run(history, on)
    frame = await _attach_fundamentals(session, result.frame, on)

    masks = await universe_masks(session, on)
    risk = await risk_masks(session, frame, on)

    values = []
    for row in frame.iter_rows(named=True):
        instrument_id = int(row["instrument_id"])
        payload = {key: _clean(value) for key, value in row.items() if key in _WRITABLE_COLUMNS}
        payload["instrument_id"] = instrument_id
        payload["date"] = on
        payload["universe_mask"] = masks.get(instrument_id, 0)
        payload["top_beta_mask"] = risk.beta.get(instrument_id, 0)
        payload["top_volatility_mask"] = risk.volatility.get(instrument_id, 0)
        values.append(payload)

    written = 0
    rows_per_statement = min(UPSERT_CHUNK, max(1, MAX_BIND_PARAMS // max(1, len(values[0]))))
    for offset in range(0, len(values), rows_per_statement):
        chunk = values[offset : offset + rows_per_statement]
        stmt = insert(FactorDaily).values(chunk)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[FactorDaily.instrument_id, FactorDaily.date],
                set_={
                    key: stmt.excluded[key]
                    for key in chunk[0]
                    if key not in ("instrument_id", "date")
                },
            )
        )
        written += len(chunk)

    mismatches = await assert_mask_matches_membership(session, on)
    outcome.rows_out = written
    outcome.note(
        engine=active.name,
        date=on.isoformat(),
        factors_computed=True,
        window_lengths={str(k): v for k, v in result.window_lengths.items()},
        # docs/05 §13: "record which was used" — how many rows used exchange turnover rather
        # than the close x volume fallback.
        turnover_sources=_turnover_sources(frame),
        top_beta_flagged=sum(1 for v in risk.beta.values() if v),
        top_volatility_flagged=sum(1 for v in risk.volatility.values() if v),
        mask_mismatches=mismatches or None,
    )
    return written


async def run_compute_factors_range(
    session: AsyncSession,
    outcome: StepOutcome,
    start: dt.date,
    end: dt.date,
    engine: FactorEngine | None = None,
) -> int:
    """Backfill mode: every trading day in ``[start, end]`` (Prompt 5 deliverable 5)."""
    days = await session.execute(
        select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date >= start,
            TradingDay.date <= end,
        )
        .order_by(TradingDay.date)
    )
    total = 0
    computed = 0
    for (day,) in days.tuples():
        step = StepOutcome()
        total += await run_compute_factors(session, step, day, engine)
        computed += 1
    outcome.rows_in = computed
    outcome.rows_out = total
    outcome.note(range=f"{start.isoformat()}..{end.isoformat()}", trading_days=computed)
    return total


def _clean(value: object) -> object:
    """Polars yields NaN and inf where the schema wants NULL.

    A division by a zero volatility or a non-positive beta is undefined, and docs/05 §3 and §7
    both answer NULL. Letting a NaN reach `numeric` would either error or store something that is
    not a number.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _turnover_sources(frame: pl.DataFrame) -> dict[str, int]:
    if "turnover_source" not in frame.columns:
        return {}
    counts = frame.group_by("turnover_source").len()
    return {str(row[0]): int(row[1]) for row in counts.iter_rows()}


async def _attach_fundamentals(
    session: AsyncSession, frame: pl.DataFrame, on: dt.date
) -> pl.DataFrame:
    """docs/05 §14 — marketcap and P/E come from ``fundamental_daily``.

    Nothing in docs/03's ten-step pipeline populates that table, so these are NULL until a
    fundamentals source is chosen (recorded in CLAUDE.md). NULL is the honest value: docs/06
    §"Step 4" already requires the P/E filter to exclude NULLs when enabled.
    """
    fundamentals = await load_fundamentals(session, on)
    if fundamentals.height == 0:
        return frame
    return frame.drop([c for c in ("marketcap_cr", "pe") if c in frame.columns]).join(
        fundamentals.with_columns(pl.col("instrument_id").cast(frame.schema["instrument_id"])),
        on="instrument_id",
        how="left",
    )


@dataclass(frozen=True, slots=True)
class RiskMasks:
    """The top-beta and top-volatility cuts, one bitmask per instrument."""

    beta: dict[int, int]
    volatility: dict[int, int]


async def risk_masks(session: AsyncSession, frame: pl.DataFrame, on: dt.date) -> RiskMasks:
    """docs/06 §"Step 4" — the top-decile beta and volatility cut, **per universe**.

    "the cut is taken over the **whole universe**, nightly, and the screener just tests a
    boolean. ... Threshold: top decile by beta / by 1-year volatility within each universe."

    Computed over every member of each universe, before any other filter — which is what makes
    the flag order-independent, and what the export proves the reference product does (docs/13 §2
    finding 10: "the minimum beta among flagged rows strictly exceeds the maximum beta among
    unflagged rows").
    """
    beta_mask: dict[int, int] = {}
    volatility_mask: dict[int, int] = {}
    if frame.height == 0:
        return RiskMasks(beta_mask, volatility_mask)

    values = {
        int(row["instrument_id"]): (row.get("beta_12m"), row.get("vol_12m"))
        for row in frame.iter_rows(named=True)
    }

    members = await session.execute(
        select(IndexMemberDaily.index_id, IndexMemberDaily.instrument_id).where(
            IndexMemberDaily.date == on
        )
    )
    by_universe: dict[int, list[int]] = {}
    for index_id, instrument_id in members.tuples():
        by_universe.setdefault(index_id, []).append(instrument_id)

    by_index = {u.index_id: u for u in UNIVERSES}
    for index_id, instrument_ids in by_universe.items():
        universe = by_index.get(index_id)
        if universe is None:
            continue
        for slot, mask in ((0, beta_mask), (1, volatility_mask)):
            ranked = [
                (instrument_id, values[instrument_id][slot])
                for instrument_id in instrument_ids
                if instrument_id in values and values[instrument_id][slot] is not None
            ]
            if not ranked:
                continue
            ranked.sort(key=lambda pair: float(pair[1] or 0), reverse=True)
            cut = max(1, round(len(ranked) * TOP_RISK_FLAG_PERCENTILE))
            for instrument_id, _value in ranked[:cut]:
                mask[instrument_id] = mask.get(instrument_id, 0) | universe.mask_value

    return RiskMasks(beta_mask, volatility_mask)


#: Columns a factor engine may write.
#: Columns a factor engine may write. `instrument_id` and `date` key the row; the masks are
#: computed here from index_member_daily rather than by the engine.
_WRITABLE_COLUMNS: frozenset[str] = frozenset(
    c.name
    for c in FactorDaily.__table__.columns
    if c.name not in ("universe_mask", "top_beta_mask", "top_volatility_mask")
)


async def universe_masks(session: AsyncSession, on: dt.date) -> dict[int, int]:
    """Denormalise ``index_member_daily`` into one bitmask per instrument (docs/04, docs/06)."""
    rows = await session.execute(
        select(IndexMemberDaily.instrument_id, IndexMemberDaily.index_id).where(
            IndexMemberDaily.date == on
        )
    )
    by_id = {u.index_id: u for u in UNIVERSES}
    masks: dict[int, int] = {}
    for instrument_id, index_id in rows.tuples():
        universe = by_id.get(index_id)
        if universe is None:
            # A dashboard-only index (docs/01 §7) has no mask bit by design; ids above the
            # universe range are never masked. See decile_core.universes.
            continue
        masks[instrument_id] = masks.get(instrument_id, 0) | universe.mask_value
    return masks


async def assert_mask_matches_membership(session: AsyncSession, on: dt.date) -> list[str]:
    """docs/06: "The nightly job asserts the two representations agree."

    Returns a list of human-readable disagreements — empty when the denormalisation is faithful.
    Reported rather than raised, because the quality gate is what decides publishability.
    """
    expected = await universe_masks(session, on)
    rows = await session.execute(
        select(FactorDaily.instrument_id, FactorDaily.universe_mask).where(FactorDaily.date == on)
    )
    problems: list[str] = []
    for instrument_id, mask in rows.tuples():
        want = expected.get(instrument_id, 0)
        if mask != want:
            problems.append(f"instrument {instrument_id}: mask {mask} != membership {want}")
        if len(problems) >= MAX_REPORTED_MISMATCHES:
            break
    return problems


async def factor_row_count(session: AsyncSession, on: dt.date) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(FactorDaily).where(FactorDaily.date == on)
            )
        ).scalar_one()
    )


__all__ = [
    "TOP_RISK_FLAG_PERCENTILE",
    "UNIVERSE_BY_SLUG",
    "FactorEngine",
    "RiskMasks",
    "assert_mask_matches_membership",
    "factor_row_count",
    "risk_masks",
    "run_compute_factors",
    "run_compute_factors_range",
    "universe_masks",
]
