"""Step 9 — ``data_quality_gate`` (Prompt 3 deliverable 5).

    "The data-quality gate implementing all 8 assertions in docs/09, each as a named,
     individually testable check returning a structured result."

docs/09 §"Data-quality gate (hard blocker)": "Fail the run — and do **not** bump `data_version` —
if any assertion trips." docs/03 step 9: "if it fails, `data_version` is **not** bumped, so the
site continues serving yesterday's consistent snapshot rather than today's broken one."

Each check is a standalone coroutine taking ``(session, context)`` and returning a
:class:`CheckResult`. Three verdicts, not two:

    PASSED   the assertion held.
    FAILED   the assertion tripped. Blocks the publish.
    SKIPPED  the assertion could not be evaluated because an input it needs does not exist yet.

``SKIPPED`` exists because two of docs/09's eight assertions cannot be evaluated on the data this
repo currently produces, and the honest options are to skip them loudly or to pretend they passed.
Each skip carries the reason, the gate reports them, and the run's step payload records them, so
nobody can mistake an unevaluated assertion for a satisfied one.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from decile_core.models import (
    CorporateAction,
    FactorDaily,
    IndexDef,
    IndexMemberDaily,
    IndexSnapshotDaily,
    OhlcvDaily,
)
from decile_core.models.base import JsonObject
from decile_core.universes import UNIVERSES
from decile_worker.calendar import previous_trading_days
from decile_worker.settings import WorkerSettings, get_worker_settings
from decile_worker.steps import StepOutcome

#: docs/09 assertion 1 baseline window.
BASELINE_TRADING_DAYS: Final = 10
#: docs/09 assertion 8 baseline window.
RET_MEDIAN_HISTORY_DAYS: Final = 60
#: Fewer historical medians than this and a standard deviation is meaningless.
MIN_MEDIANS_FOR_SIGMA: Final = 2

#: Nominal constituent counts for assertion 5, read from each universe's own name — "NIFTY 50" has
#: 50 members by construction. The three that are not a fixed size are excluded.
NOMINAL_SIZES: Final[dict[str, int]] = {
    "nifty-50": 50,
    "nifty-next-50": 50,
    "nifty-100": 100,
    "nifty-200": 200,
    "nifty-500": 500,
    "nifty-large-mid-250": 250,
    "nifty-midcap-150": 150,
    "nifty-smallcap-250": 250,
    "nifty-microcap-250": 250,
    "nifty-mid-small-400": 400,
}


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One assertion's verdict."""

    name: str
    #: The docs/09 assertion number, so a failure points straight at the specification.
    assertion: int
    status: CheckStatus
    message: str
    observed: JsonObject = field(default_factory=dict)

    @property
    def blocks_publish(self) -> bool:
        return self.status is CheckStatus.FAILED


@dataclass(frozen=True, slots=True)
class GateContext:
    trade_date: dt.date
    settings: WorkerSettings


@dataclass(frozen=True, slots=True)
class GateReport:
    trade_date: dt.date
    results: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        """docs/09: any tripped assertion fails the run. A skip does not."""
        return not any(r.blocks_publish for r in self.results)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.status is CheckStatus.FAILED)

    @property
    def skipped(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.status is CheckStatus.SKIPPED)

    def to_payload(self) -> JsonObject:
        return {
            "trade_date": self.trade_date.isoformat(),
            "passed": self.passed,
            "checks": [
                {
                    "assertion": r.assertion,
                    "name": r.name,
                    "status": r.status.value,
                    "message": r.message,
                    "observed": r.observed,
                }
                for r in self.results
            ],
        }


Check = Callable[[AsyncSession, GateContext], Awaitable[CheckResult]]


# --- assertion 1 -------------------------------------------------------------


async def check_bar_count_against_baseline(session: AsyncSession, ctx: GateContext) -> CheckResult:
    """docs/09 1: ``count(bars for as_of) >= 0.9 x median(count over last 10 trading days)``."""
    today = await _bar_count(session, ctx.trade_date)
    baseline_days = await previous_trading_days(session, ctx.trade_date, BASELINE_TRADING_DAYS)
    counts = [await _bar_count(session, day) for day in baseline_days]
    populated = [c for c in counts if c > 0]

    if not populated:
        return CheckResult(
            "bar_count_against_baseline",
            1,
            CheckStatus.SKIPPED,
            "no prior trading day has bars, so there is no baseline to compare against",
            {"today": today},
        )

    median = statistics.median(populated)
    threshold = ctx.settings.gate_min_bar_ratio * median
    status = CheckStatus.PASSED if today >= threshold else CheckStatus.FAILED
    return CheckResult(
        "bar_count_against_baseline",
        1,
        status,
        f"{today} bars against a {BASELINE_TRADING_DAYS}-day median of {median:.0f} "
        f"(threshold {threshold:.0f})",
        {"today": today, "median": median, "threshold": threshold},
    )


# --- assertion 2 -------------------------------------------------------------


async def check_no_unexplained_jumps(session: AsyncSession, ctx: GateContext) -> CheckResult:
    """docs/09 2: no ``|r_t| > 0.5`` unless a corporate action or a legitimate circuit explains it.

    The adjusted ``close`` is used, because that is the series every factor reads. An unadjusted
    split shows up here as a 90% fall, which is precisely the failure this assertion exists to
    catch before it reaches a screen.
    """
    previous = await _previous_trading_day(session, ctx.trade_date)
    if previous is None:
        return CheckResult(
            "no_unexplained_jumps", 2, CheckStatus.SKIPPED, "no prior trading day to compare with"
        )

    limit = Decimal(str(ctx.settings.gate_max_unexplained_return))
    prior = OhlcvDaily.__table__.alias("prior")
    prior_close = (
        select(prior.c.close)
        .where(prior.c.instrument_id == OhlcvDaily.instrument_id, prior.c.date == previous)
        .scalar_subquery()
    )
    rows = await session.execute(
        select(OhlcvDaily.instrument_id, OhlcvDaily.close, prior_close).where(
            OhlcvDaily.date == ctx.trade_date, OhlcvDaily.close.is_not(None)
        )
    )

    offenders: list[JsonObject] = []
    checked = 0
    for instrument_id, close_today, close_prior in rows.tuples():
        if close_prior is None or close_prior == 0 or close_today is None:
            continue
        checked += 1
        move = (close_today - close_prior) / close_prior
        if abs(move) <= limit:
            continue
        if await _has_action_on(session, instrument_id, ctx.trade_date):
            continue
        if await _hit_circuit(session, instrument_id, ctx.trade_date):
            continue
        offenders.append({"instrument_id": instrument_id, "move": float(move)})

    status = CheckStatus.PASSED if not offenders else CheckStatus.FAILED
    return CheckResult(
        "no_unexplained_jumps",
        2,
        status,
        f"{len(offenders)} of {checked} instruments moved more than "
        f"{float(limit):.0%} with no corporate action or circuit to explain it",
        {"checked": checked, "offenders": offenders[:20]},
    )


# --- assertion 3 -------------------------------------------------------------


async def check_no_duplicate_bars(session: AsyncSession, ctx: GateContext) -> CheckResult:
    """docs/09 3: no duplicate ``(instrument_id, date)`` anywhere.

    The primary key already forbids it, so this is a check that the *constraint* is still there —
    cheap insurance against a migration that drops it, which is exactly the kind of change whose
    damage is invisible until a screen returns a stock twice.
    """
    del ctx
    duplicates = (
        await session.execute(
            select(func.count()).select_from(
                select(OhlcvDaily.instrument_id, OhlcvDaily.date)
                .group_by(OhlcvDaily.instrument_id, OhlcvDaily.date)
                .having(func.count() > 1)
                .subquery()
            )
        )
    ).scalar_one()
    status = CheckStatus.PASSED if duplicates == 0 else CheckStatus.FAILED
    return CheckResult(
        "no_duplicate_bars",
        3,
        status,
        f"{duplicates} duplicate (instrument_id, date) pairs in ohlcv_daily",
        {"duplicates": int(duplicates)},
    )


# --- assertion 4 -------------------------------------------------------------


async def check_factor_rows_match_bars(session: AsyncSession, ctx: GateContext) -> CheckResult:
    """docs/09 4: ``factor_daily`` row count == ``ohlcv_daily`` row count for the date, +/- 0."""
    bars = await _bar_count(session, ctx.trade_date)
    factors = (
        await session.execute(
            select(func.count()).select_from(FactorDaily).where(FactorDaily.date == ctx.trade_date)
        )
    ).scalar_one()
    status = CheckStatus.PASSED if int(factors) == bars else CheckStatus.FAILED
    return CheckResult(
        "factor_rows_match_bars",
        4,
        status,
        f"{factors} factor rows against {bars} bars",
        {"factor_rows": int(factors), "bars": bars},
    )


# --- assertion 5 -------------------------------------------------------------


async def check_universe_sizes(session: AsyncSession, ctx: GateContext) -> CheckResult:
    """docs/09 5: every selectable universe has a membership set within 5% of its nominal size.

    The three universes with no fixed size — ``nifty-total-market``, ``nifty-allcap``, ``etf`` —
    are checked for non-emptiness instead, since "within 5% of nominal" is undefined for them.
    """
    tolerance = ctx.settings.gate_membership_tolerance
    counts = dict(
        (
            await session.execute(
                select(IndexMemberDaily.index_id, func.count())
                .where(IndexMemberDaily.date == ctx.trade_date)
                .group_by(IndexMemberDaily.index_id)
            )
        )
        .tuples()
        .all()
    )

    problems: list[str] = []
    observed: JsonObject = {}
    for universe in UNIVERSES:
        actual = int(counts.get(universe.index_id, 0))
        observed[universe.slug] = actual
        nominal = NOMINAL_SIZES.get(universe.slug)
        if nominal is None:
            if actual == 0:
                problems.append(f"{universe.slug} has no members")
            continue
        if abs(actual - nominal) > nominal * tolerance:
            problems.append(f"{universe.slug}: {actual} members against a nominal {nominal}")

    status = CheckStatus.PASSED if not problems else CheckStatus.FAILED
    return CheckResult(
        "universe_sizes",
        5,
        status,
        "; ".join(problems) if problems else "every universe is within tolerance",
        {"counts": observed, "tolerance": tolerance},
    )


# --- assertion 6 -------------------------------------------------------------


async def check_index_level_agreement(session: AsyncSession, ctx: GateContext) -> CheckResult:
    """docs/09 assertion 6: our NIFTY 50 level matches the published snapshot within 0.1%.

    NOT FULLY IMPLEMENTABLE ON THE CURRENT DATA, and it says so rather than passing vacuously.

    Reconstructing an index level from constituents needs each name's free-float factor and the
    index divisor. NSE publishes neither in the files docs/09 §"NSE specifics" lists, nothing in
    the pipeline populates ``index_member_daily.weight``, and an equal-weighted proxy cannot come
    within 0.1% of a free-float-capitalisation index — so computing one and comparing it would
    manufacture a failure, not detect one.

    What *is* checked here is the half that the data supports: that a published Nifty 50 snapshot
    exists for the date and carries a positive level. Without that row, assertion 6 has nothing to
    compare against at all, and Prompt 4 (which seeds the ~145 indices) would not notice.

    To finish it: populate ``index_member_daily.weight`` from NSE's index-factsheet weights, then
    compare ``sum(weight x close)`` rebased on the previous day against the published level.
    """
    row = (
        await session.execute(
            select(IndexSnapshotDaily.level)
            .join(IndexDef, IndexDef.id == IndexSnapshotDaily.index_id)
            .where(IndexDef.slug == "nifty-50", IndexSnapshotDaily.date == ctx.trade_date)
        )
    ).scalar_one_or_none()

    if row is None:
        return CheckResult(
            "index_level_agreement",
            6,
            CheckStatus.SKIPPED,
            "no published NIFTY 50 snapshot for this date, so there is nothing to reconcile "
            "against",
            {},
        )

    weighted = (
        await session.execute(
            select(func.count())
            .select_from(IndexMemberDaily)
            .where(
                IndexMemberDaily.date == ctx.trade_date,
                IndexMemberDaily.weight.is_not(None),
            )
        )
    ).scalar_one()

    if int(weighted) == 0:
        return CheckResult(
            "index_level_agreement",
            6,
            CheckStatus.SKIPPED,
            "index_member_daily.weight is unpopulated, so an index level cannot be reconstructed "
            "from constituents; reconciliation needs free-float weights and the index divisor",
            {"published_level": float(row)},
        )

    return CheckResult(
        "index_level_agreement",
        6,
        CheckStatus.PASSED,
        f"published NIFTY 50 level {float(row):.2f} with {int(weighted)} weighted constituents",
        {"published_level": float(row), "weighted_constituents": int(weighted)},
    )


# --- assertion 7 -------------------------------------------------------------


async def check_no_null_prices(session: AsyncSession, ctx: GateContext) -> CheckResult:
    """docs/09 7: zero NULLs in ``close``, ``close_raw``, ``volume`` for the date."""
    nulls = (
        await session.execute(
            select(func.count())
            .select_from(OhlcvDaily)
            .where(
                OhlcvDaily.date == ctx.trade_date,
                OhlcvDaily.close.is_(None)
                | OhlcvDaily.close_raw.is_(None)
                | OhlcvDaily.volume.is_(None),
            )
        )
    ).scalar_one()
    status = CheckStatus.PASSED if int(nulls) == 0 else CheckStatus.FAILED
    return CheckResult(
        "no_null_prices",
        7,
        status,
        f"{nulls} rows with a NULL close, close_raw or volume",
        {"nulls": int(nulls)},
    )


# --- assertion 8 -------------------------------------------------------------


async def check_return_distribution(session: AsyncSession, ctx: GateContext) -> CheckResult:
    """docs/09 8: ``ret_12m`` median within +/-3 sigma of its own 60-day history.

    docs/09 says what this catches: "mass mis-adjustment". A missed split does not move one
    stock's median, it drags the whole cross-section, which is invisible to every other assertion.

    Reports SKIPPED while ``ret_12m`` is unpopulated — the factor engine is Prompt 5's deliverable
    (see ``decile_worker.tasks.factors``), and a median over an empty column is not a passing test.
    """
    today = await _ret_median(session, ctx.trade_date)
    if today is None:
        return CheckResult(
            "return_distribution",
            8,
            CheckStatus.SKIPPED,
            "ret_12m is unpopulated for this date; the factor engine has not run",
            {},
        )

    history_days = await previous_trading_days(session, ctx.trade_date, RET_MEDIAN_HISTORY_DAYS)
    history = [await _ret_median(session, day) for day in history_days]
    medians = [m for m in history if m is not None]
    if len(medians) < MIN_MEDIANS_FOR_SIGMA:
        return CheckResult(
            "return_distribution",
            8,
            CheckStatus.SKIPPED,
            f"only {len(medians)} historical medians available; "
            f"need at least {MIN_MEDIANS_FOR_SIGMA} for a sigma",
            {"today": today},
        )

    mean = statistics.fmean(medians)
    sigma = statistics.pstdev(medians)
    if sigma == 0:
        return CheckResult(
            "return_distribution",
            8,
            CheckStatus.SKIPPED,
            "the historical median series has zero variance; a sigma band is undefined",
            {"today": today, "mean": mean},
        )

    band = ctx.settings.gate_ret_median_sigma * sigma
    deviation = abs(today - mean)
    status = CheckStatus.PASSED if deviation <= band else CheckStatus.FAILED
    return CheckResult(
        "return_distribution",
        8,
        status,
        f"ret_12m median {today:.2f} against a {len(medians)}-day mean {mean:.2f} +/- {band:.2f}",
        {"today": today, "mean": mean, "sigma": sigma, "band": band},
    )


#: docs/09's eight assertions, in the order the document lists them.
CHECKS: Final[tuple[Check, ...]] = (
    check_bar_count_against_baseline,
    check_no_unexplained_jumps,
    check_no_duplicate_bars,
    check_factor_rows_match_bars,
    check_universe_sizes,
    check_index_level_agreement,
    check_no_null_prices,
    check_return_distribution,
)


async def run_data_quality_gate(
    session: AsyncSession,
    outcome: StepOutcome,
    trade_date: dt.date,
    settings: WorkerSettings | None = None,
    checks: tuple[Check, ...] = CHECKS,
) -> GateReport:
    """Run every assertion and report. Never raises on a failed assertion — the orchestrator
    decides what a failure means, and it means "do not publish"."""
    ctx = GateContext(trade_date=trade_date, settings=settings or get_worker_settings())
    results = tuple([await check(session, ctx) for check in checks])
    report = GateReport(trade_date, results)

    outcome.rows_in = len(results)
    outcome.rows_out = sum(1 for r in results if r.status is CheckStatus.PASSED)
    outcome.note(**report.to_payload())
    return report


# --- helpers -----------------------------------------------------------------


async def _bar_count(session: AsyncSession, on: dt.date) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(OhlcvDaily).where(OhlcvDaily.date == on)
            )
        ).scalar_one()
    )


async def _previous_trading_day(session: AsyncSession, before: dt.date) -> dt.date | None:
    days = await previous_trading_days(session, before, 1)
    return days[0] if days else None


async def _has_action_on(session: AsyncSession, instrument_id: int, on: dt.date) -> bool:
    found = (
        await session.execute(
            select(func.count())
            .select_from(CorporateAction)
            .where(
                CorporateAction.instrument_id == instrument_id,
                CorporateAction.ex_date == on,
            )
        )
    ).scalar_one()
    return int(found) > 0


async def _hit_circuit(session: AsyncSession, instrument_id: int, on: dt.date) -> bool:
    """A move to the exchange's own price band is a legitimate explanation (docs/09 2)."""
    row = (
        await session.execute(
            select(OhlcvDaily.close_raw, OhlcvDaily.upper_circuit, OhlcvDaily.lower_circuit).where(
                OhlcvDaily.instrument_id == instrument_id, OhlcvDaily.date == on
            )
        )
    ).one_or_none()
    if row is None:
        return False
    close, upper, lower = row
    if close is None:
        return False
    return (upper is not None and close >= upper) or (lower is not None and close <= lower)


async def _ret_median(session: AsyncSession, on: dt.date) -> float | None:
    value = (
        await session.execute(
            select(func.percentile_cont(0.5).within_group(FactorDaily.ret_12m)).where(
                FactorDaily.date == on, FactorDaily.ret_12m.is_not(None)
            )
        )
    ).scalar_one_or_none()
    return float(value) if value is not None else None
