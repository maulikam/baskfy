"""TW9: the study, re-run from the plant's own bars, with its drift named (`docs/twt/06` TW9).

    the `04` §1 universe and its bars → drop the thin sessions → `with_twt_columns`
      → `signal_mask` at the **shipped** ₹5 crore floor → breadth → the gate vector
      → two books over **one** detection pass
      → one appended `tw_backtest_run` row

**The engine is parameterised, not forked** (DECISIONS-TW **TW2.13**). TW2 runs exactly these
functions over the research panel with two keywords changed — the study's paisa tick and its ₹2
crore floor. This module passes neither, which is the whole difference: `BacktestParams`' four
relevant defaults are already the shipped sleeve's, so *a run that passes nothing is the shipped
sleeve*.

**It is sized against `params.sleeve_inr` and never against `tw_config.sleeve_capital_inr`**
(`03` §9, Track C §6). Nothing here reads the `tw_config` table at all — not the capital, not the
thresholds — because reading the live sleeve's money into a backtest is how a research number
quietly becomes a claim about the user's own money, and because a backtest whose history changed
the day somebody funded the book would not be a backtest. `test_twt_backtest_job.py` asserts it
with a spy over every ORM statement the run issues.

**The two books share a detection pass on purpose.** `full` and `gate_off` differ only in whether
the breadth gate is allowed to refuse an entry, so detecting twice would spend twice the work to
produce the same signals and would leave open the possibility of them disagreeing. `gate_off` is
not decoration: it is the **only** argument for the gate (`01` §5 — 17.2 % ungated at -43 %
against 20.9 % gated at -24.7 %), and `05` §3 gives it a cell on the card that nothing else fills.

**The row is written on the way in and finished on the way out**, which is `03` §9's contract: a
run that dies still says what it was asked for. `params` before, `stats` and `drift` after,
`error` instead of `stats` when it raised — and `finished_at` in every case, so "still running"
and "failed" are different states on the page rather than the same silence. The exception is
**re-raised** after it is recorded: a failed run that looks finished is worse than one that is
obviously broken.

**Append-only.** Nothing here edits a stored row. The number that was on the page when the
execution flag was considered has to survive a recalibration that produces a different one.

Two things TW2 measured as **zero on the research panel only**, and this module therefore measures
rather than assumes (DECISIONS-TW **TW2.2**, **TW2.12**, and **TW2.13**'s warning):

* **ETFs in the breadth denominator.** `04` §1.3 drops them from the plant's universe; the study
  kept them as rows and excluded them inside the scan, so its denominator counted them. On the
  export that made no difference to any decimal place, because the export barely carries ETFs. On
  the plant they print daily. :func:`etf_denominator_delta` runs the breadth series a second time
  with the ETF rows added and stores what changed — the percentage and, more importantly, **how
  many sessions' gate verdicts flip**.
* **`clamped_below_stop`**, `04` §7.2's fallback producing a trigger below the stop in force. It
  fired 0 times over the study's whole run. It is counted here, on `stats`, so the day it stops
  being zero somebody finds out.

This module places no order, reads no broker and touches no flag. It reads `ohlcv_daily`,
`instrument`, `index_member_daily` and `trading_day`, and writes one `tw_backtest_run` row.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import traceback
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

import numpy as np
import polars as pl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import OhlcvDaily, TwBacktestRun
from baskfy_core.models.base import JsonObject
from baskfy_core.twt.backtest import (
    BacktestParams,
    BacktestResult,
    BacktestStats,
    gate_vector,
    panel_from_frame,
    run_backtest,
    summarise,
    yearly,
)
from baskfy_core.twt.breadth import breadth_series
from baskfy_core.twt.calendar import drop_thin_sessions
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, TwtConfig
from baskfy_core.twt.drift import compare
from baskfy_core.twt.indicators import with_twt_indicators
from baskfy_core.twt.signals import signal_mask, with_twt_columns
from baskfy_worker.tasks.twt import (
    LOOKBACK_SESSIONS,
    _universe_query,
    load_twt_bars,
    load_universe,
    lookback_start,
)

log = logging.getLogger(__name__)

#: `03` §9: the run's own bars, as against TW2's reproduction from the research panel.
SOURCE_PLANT: Final = "PLANT"

#: Two decimals, the precision `01` §6 prints and `05` §3 renders. Every percentage leaves this
#: module as a **decimal string** so the page rounds exactly once (`@/lib/twt/numbers`).
_TWO_PLACES: Final = Decimal("0.01")

#: `04` §12's own split, so a run and the study's in/out-of-sample halves mean the same thing.
_LABEL_FULL: Final = "full"
_LABEL_GATE_OFF: Final = "gate_off"

#: A curve needs two points before it has a return — the same guard :func:`summarise` applies.
_TWO_POINTS: Final = 2

__all__ = [
    "SOURCE_PLANT",
    "BacktestBooks",
    "etf_denominator_delta",
    "excluded_etf_ids",
    "last_published_session",
    "latest_finished",
    "run_twt_backtest",
    "stats_payload",
    "two_books",
]


@dataclasses.dataclass(frozen=True, slots=True)
class BacktestBooks:
    """`01` §5's two, over one detection pass.

    The gate's contribution is `full - gate_off`, recomputed on whatever bars the plant currently
    holds rather than quoted from the note. There is no third book: TWT-1 has no trend filters to
    ablate — `01` §4 measured them and they make it *worse* — so VBT-1's `raw_scan` would be the
    same run twice under two names.
    """

    full: BacktestResult
    gate_off: BacktestResult


def _pct(value: float | Decimal | None) -> str | None:
    """A percentage as a decimal string of two places, or `None` when there is no number.

    `None` rather than `"0.00"`: `05` §3's `Figure` renders the reason there is no value, and a
    zero standing in for an unknown on a page about money is the failure that rule exists for.
    """
    if value is None:
        return None
    quantised = Decimal(str(value))
    if not quantised.is_finite():
        return None
    return str(quantised.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))


def two_books(tagged: pl.DataFrame, breadth: pl.DataFrame, params: BacktestParams) -> BacktestBooks:
    """Run the same history twice: as specified, and with the gate forced open."""
    panel = panel_from_frame(tagged, "signal")
    gate = gate_vector(breadth, panel.sessions)
    always_open = np.ones(panel.sessions_count, dtype=bool)
    return BacktestBooks(
        full=run_backtest(panel, gate, dataclasses.replace(params, label=_LABEL_FULL)),
        gate_off=run_backtest(
            panel, always_open, dataclasses.replace(params, label=_LABEL_GATE_OFF)
        ),
    )


def _half(
    result: BacktestResult, *, before: dt.date | None, on_or_after: dt.date | None
) -> str | None:
    """The annualised return of one slice of the equity curve, as a percentage string.

    `04` §12's in/out-of-sample split, computed off the stored curve rather than by re-running the
    book over a shorter window: a re-run would start from a different cash position and would be
    answering "what if we had begun in 2023", which is a different question from "what did the
    book do in its second half".
    """
    points = [
        (session, value)
        for session, value in zip(result.sessions, result.equity, strict=True)
        if value > 0
        and (before is None or session < before)
        and (on_or_after is None or session >= on_or_after)
    ]
    if len(points) < _TWO_POINTS:
        return None
    (first_day, first_value), (last_day, last_value) = points[0], points[-1]
    days = Decimal((last_day - first_day).days)
    if days <= 0 or first_value <= 0:
        return None
    years = days / Decimal("365.25")
    grown = float(last_value / first_value) ** (1 / float(years))
    return _pct((Decimal(str(grown)) - Decimal(1)) * Decimal(100))


def _window(
    sessions: tuple[dt.date, ...], *, before: dt.date | None, on_or_after: dt.date | None
) -> str | None:
    """`"2017-10-16 → 2022-12-30"`, or `None` when the run does not reach into that half."""
    inside = [
        session
        for session in sessions
        if (before is None or session < before) and (on_or_after is None or session >= on_or_after)
    ]
    if not inside:
        return None
    return f"{inside[0].isoformat()} → {inside[-1].isoformat()}"


def stats_payload(
    books: BacktestBooks, primary: BacktestStats, config: TwtConfig, extra: JsonObject
) -> JsonObject:
    """The JSONB `tw_backtest_run.stats` stores — **every key `05` §3's card reads**.

    The card is TW8's and it was written before this module existed, which makes the key list a
    contract rather than a convention: a name that does not appear here renders as the reason
    there is no figure, not as a dash, so a missing key is silent on the page and loud only here.
    `test_twt_backtest_job.py` asserts the whole list.

    Every percentage is a decimal string, which is `@/lib/twt/numbers`' rule: a rate converts and
    rounds exactly once, and it does so on the page. Money is a string of its exact decimal
    (house rule 9), all the way to the database.
    """
    split = config.backtest.is_oos_split
    gate_off = summarise(books.gate_off)
    payload: JsonObject = {
        "cagr_pct": _pct(primary.cagr_pct),
        "max_drawdown_pct": _pct(primary.max_drawdown_pct),
        "calmar": None if primary.calmar is None else _pct(primary.calmar),
        "sharpe": _pct(primary.sharpe_ratio),
        "trades": primary.trades,
        "win_rate_pct": _pct(primary.win_rate_pct),
        "profit_factor": None if primary.profit_factor is None else _pct(primary.profit_factor),
        "avg_hold_sessions": _pct(primary.avg_hold_sessions),
        "exposure_pct": _pct(primary.exposure_pct),
        "in_sample_cagr_pct": _half(books.full, before=split, on_or_after=None),
        "out_of_sample_cagr_pct": _half(books.full, before=None, on_or_after=split),
        "in_sample_window": _window(books.full.sessions, before=split, on_or_after=None),
        "out_of_sample_window": _window(books.full.sessions, before=None, on_or_after=split),
        # `05` §3's one number that justifies the gate. Nothing else on the card fills this cell,
        # and a card that showed the gated figure alone would be showing a decision without its
        # counterfactual.
        "gate_off_cagr_pct": None if gate_off is None else _pct(gate_off.cagr_pct),
        "gate_off_max_drawdown_pct": (
            None if gate_off is None else _pct(gate_off.max_drawdown_pct)
        ),
        "gate_off_trades": 0 if gate_off is None else gate_off.trades,
        "yearly": [
            {
                "year": row.year,
                "return_pct": _pct(row.return_pct),
                "trades": row.trades,
                "win_rate_pct": _pct(row.win_rate_pct),
            }
            for row in yearly(books.full)
        ],
        "equity_curve": [
            {"date": session.isoformat(), "equity_inr": str(value)}
            for session, value in zip(books.full.sessions, books.full.equity, strict=True)
        ],
        # The rest is the honest record `03` §9 asks for: what the book did not do, and why.
        "start": primary.start.isoformat(),
        "end": primary.end.isoformat(),
        "years": str(primary.years),
        "final_equity_inr": str(primary.final_equity_inr),
        "drawdown_peak_on": primary.drawdown_peak_on.isoformat(),
        "drawdown_trough_on": primary.drawdown_trough_on.isoformat(),
        "avg_win_pct": _pct(primary.avg_win_pct),
        "avg_loss_pct": _pct(primary.avg_loss_pct),
        "avg_trade_pct": _pct(primary.avg_trade_pct),
        "avg_open_positions": str(primary.avg_open_positions),
        "by_reason": dict(primary.by_reason),
        "skipped": dict(books.full.skipped),
        # DECISIONS-TW TW2.12: zero over the study's whole run, and zero **on that panel only**.
        "clamped_below_stop": books.full.clamped_below_stop,
    }
    # A key whose value is `None` is **dropped**, never stored as a JSON null. `05` §3's
    # `Figure` renders the reason a figure is missing, and it recognises "missing" as `undefined`;
    # a null would reach `percent()` and print the word. An absent key is the honest encoding of
    # "this run could not produce that number" — a book with no losing trade has no profit
    # factor, and a window that does not reach across `is_oos_split` has no in-sample half.
    payload = {key: value for key, value in payload.items() if value is not None}
    payload.update(extra)
    return payload


async def last_published_session(session: AsyncSession) -> dt.date | None:
    """The newest date `ohlcv_daily` holds — `06` § TW9's "the last published session".

    The bars, not the calendar: a trading day exists on the calendar the moment the year is
    seeded, and a backtest that ran to a session with no bars in it would be reporting a book
    sitting in cash through a window that simply has not been ingested yet.
    """
    return (
        await session.execute(select(OhlcvDaily.date).order_by(OhlcvDaily.date.desc()).limit(1))
    ).scalar_one_or_none()


async def excluded_etf_ids(
    session: AsyncSession, config: TwtConfig, universe: set[int]
) -> set[int]:
    """Every instrument ``04`` §1.1/§1.2 admits and §1.3 refused — the plant's ETFs, exactly.

    Not :func:`~baskfy_worker.tasks.twt.etf_instrument_ids`, which reads membership of the ``etf``
    universe alone: ``load_universe`` also refuses a name whose symbol ends ``BEES``/``ETF``/
    ``IETF`` or whose name carries the word, and on a plant whose ``etf`` index has never been
    populated those two patterns are the *whole* of the exclusion. The counterfactual has to add
    back what was actually taken out, or it measures a different question from the one TW2.2 asked.
    """
    rows = await session.execute(_universe_query(config))
    return {int(instrument_id) for instrument_id, _, _ in rows} - universe


async def etf_denominator_delta(  # noqa: PLR0913 - one keyword per input the counterfactual needs
    session: AsyncSession,
    *,
    start: dt.date,
    end: dt.date,
    universe: set[int],
    config: TwtConfig,
    baseline: pl.DataFrame,
    baseline_breadth: pl.DataFrame,
) -> JsonObject:
    """What counting ETFs in the breadth denominator would do to **this** run (`04` §1.3).

    DECISIONS-TW **TW2.2** measured it at zero on the study's panel — the export carries 2,786 ETF
    bars in 3.58 million — and said in the same entry that the number is a property of the sparse
    export and not of the plant, which prints them daily. **TW9 must not inherit that assumption**,
    so it is measured here rather than asserted away.

    Only the breadth arithmetic is re-run, not the signal chain: breadth reads one column, the
    200-session average, and the whole question is whose closes are in the denominator. The ETF
    rows are never allowed near the *book* — `04` §1.3 keeps them out of the universe and
    `tight_state` excludes them itself — so this measures the counterfactual and changes nothing.

    Reports the largest percentage-point difference on any session and, the number that actually
    matters, **how many sessions' gate verdicts flip**: the gate decides whether the book may
    enter at all, so one flipped verdict is a different trade and 0.01 of a point is not.
    """
    etfs = await excluded_etf_ids(session, config, universe)
    if not etfs:
        return {
            "etf_instruments": 0,
            "etf_bars": 0,
            "sessions_compared": 0,
            "max_pct_above_dma_delta": None,
            "gate_verdicts_changed": None,
        }
    etf_bars = await load_twt_bars(session, start, end, etfs)
    if etf_bars.is_empty():
        return {
            "etf_instruments": len(etfs),
            "etf_bars": 0,
            "sessions_compared": 0,
            "max_pct_above_dma_delta": "0.0000",
            "gate_verdicts_changed": 0,
        }
    combined = pl.concat(
        [baseline, etf_bars.with_columns(pl.lit(True).alias("is_etf")).select(baseline.columns)],
        how="vertical_relaxed",
    )
    clean, calendar = drop_thin_sessions(combined, config)
    with_etfs = breadth_series(with_twt_indicators(clean, calendar, config), config)
    joined = baseline_breadth.join(with_etfs, on="date", how="inner", suffix="_etf")
    if joined.is_empty():
        return {
            "etf_instruments": len(etfs),
            "etf_bars": int(etf_bars.height),
            "sessions_compared": 0,
            "max_pct_above_dma_delta": None,
            "gate_verdicts_changed": None,
        }
    deltas = (joined["pct_above_dma_etf"] - joined["pct_above_dma"]).abs()
    flipped = int((joined["gate"] != joined["gate_etf"]).sum())
    return {
        "etf_instruments": len(etfs),
        "etf_bars": int(etf_bars.height),
        "universe_instruments": len(universe),
        "sessions_compared": int(joined.height),
        "max_pct_above_dma_delta": str(deltas.max()),
        "gate_verdicts_changed": flipped,
    }


def _params_json(params: BacktestParams, start: dt.date, end: dt.date | None) -> JsonObject:
    """`03` §9: `start`, `end`, `sleeve_inr`, the cost and the whole config, written on the way in.

    The config is flattened field by field rather than stored as a repr, so a reader comparing two
    runs can diff them, and so a field added to `TwtConfig` appears here without anybody
    remembering to add it. `sleeve_inr` is here and `tw_config.sleeve_capital_inr` is not, which is
    the difference this whole module is careful about.
    """
    config: JsonObject = {}
    for group in dataclasses.fields(params.config):
        sub = getattr(params.config, group.name)
        for spec in dataclasses.fields(sub):
            value = getattr(sub, spec.name)
            if isinstance(value, tuple):
                config[f"{group.name}.{spec.name}"] = list(value)
            elif isinstance(value, (Decimal, dt.date)):
                config[f"{group.name}.{spec.name}"] = str(value)
            else:
                config[f"{group.name}.{spec.name}"] = value
    return {
        "start": start.isoformat(),
        "end": end.isoformat() if end else None,
        "sleeve_inr": str(params.sleeve_inr),
        "cost_bps_per_side": str(params.config.costs.cost_bps_per_side),
        "cost_pct_per_side": str(params.cost_pct_per_side),
        "tick": str(params.tick),
        "min_turnover_inr": str(params.config.entry.min_turnover_inr),
        "source": SOURCE_PLANT,
        "config": config,
    }


async def _open_run(
    session: AsyncSession, *, user_id: int, params: JsonObject, now: dt.datetime
) -> TwBacktestRun:
    row = TwBacktestRun(user_id=user_id, source=SOURCE_PLANT, params=params, started_at=now)
    session.add(row)
    await session.flush()
    return row


async def run_twt_backtest(  # noqa: PLR0913 - one keyword per input the stored row records
    session: AsyncSession,
    *,
    user_id: int,
    start: dt.date | None = None,
    end: dt.date | None = None,
    sleeve_inr: Decimal | None = None,
    config: TwtConfig = DEFAULT_TWT_CONFIG,
    now: dt.datetime | None = None,
    measure_etf_denominator: bool = True,
) -> TwBacktestRun:
    """Re-run the study over the plant's bars and append one `tw_backtest_run` row.

    `start` defaults to `04` §12's `BacktestConfig.start`; `end` to the last **published** session.
    `sleeve_inr` defaults to `04` §12's `initial_capital_inr` — ₹10 lakh, the amount `01` §6's
    numbers were produced at, and deliberately *not* whatever the live sleeve is funded with.

    Raises whatever the engine raised, **after** recording it: `03` §9 wants the error on the row
    and the exception in the logs, because a run that failed silently would leave the page showing
    the last good number with nothing to say why it had not moved.
    """
    stamp = now or dt.datetime.now(tz=dt.UTC)
    first = start or config.backtest.start
    params = BacktestParams(
        sleeve_inr=sleeve_inr or config.backtest.initial_capital_inr,
        start=first,
        end=end,
        config=config,
    )
    row = await _open_run(
        session, user_id=user_id, params=_params_json(params, first, end), now=stamp
    )
    try:
        last = end or await last_published_session(session)
        if last is None:
            raise ValueError(
                "the plant holds no bars at all, so there is no last published session to run "
                "to; run the backfill before asking for a backtest"
            )
        universe = await load_universe(session, config)
        warmup = await lookback_start(session, first, LOOKBACK_SESSIONS)
        bars = await load_twt_bars(session, warmup, last, universe)
        if bars.is_empty():
            raise ValueError(
                "the plant holds no bars for the three-weeks-tight universe in this window; "
                "run the backfill before asking for a backtest"
            )
        clean, calendar = drop_thin_sessions(bars, config)
        detected = with_twt_columns(clean, calendar, config)
        # No `floor_inr`: the shipped ₹5 crore (`04` §3.5, DECISIONS-TW TW0.3). TW2 passes the
        # research's ₹2 crore here and that is the one difference between the two runs' scans.
        tagged = detected.with_columns(signal_mask(detected, config).alias("signal"))
        breadth = breadth_series(detected, config)
        books = two_books(tagged, breadth, params)
        primary = summarise(books.full)
        if primary is None:
            raise ValueError(
                "the run produced no equity curve, so there is no result to compare — usually "
                "too little history for the 260-session window `04` §2.3 asks for"
            )
        etf = (
            await etf_denominator_delta(
                session,
                start=warmup,
                end=last,
                universe=universe,
                config=config,
                baseline=bars,
                baseline_breadth=breadth,
            )
            if measure_etf_denominator
            else {}
        )
        row.stats = stats_payload(
            books,
            primary,
            config,
            {
                "universe": len(universe),
                "sessions": len(calendar.sessions),
                "thin_sessions_dropped": [day.isoformat() for day in calendar.dropped],
                "etf_denominator": etf,
            },
        )
        row.drift = compare(
            cagr_pct=primary.cagr_pct,
            max_drawdown_pct=primary.max_drawdown_pct,
            trades=primary.trades,
        ).to_json()
    except Exception as error:
        row.error = f"{type(error).__name__}: {error}\n{traceback.format_exc()}"
        row.finished_at = dt.datetime.now(tz=dt.UTC)
        await session.flush()
        log.exception("twt backtest failed", extra={"run_id": row.id, "user_id": user_id})
        raise
    row.finished_at = dt.datetime.now(tz=dt.UTC)
    await session.flush()
    return row


async def latest_finished(
    session: AsyncSession, *, user_id: int, source: str = SOURCE_PLANT
) -> TwBacktestRun | None:
    """The page's one query: this user's newest **finished** run for a source.

    Finished *and* carrying stats, not merely started: a run in flight has no `finished_at`, a run
    that failed has one and no `stats`, and neither may displace the last good number (`03` §9).
    The page's own `latestFinished` filters on the same two facts, so the two halves of the rule
    cannot disagree.
    """
    return (
        await session.execute(
            select(TwBacktestRun)
            .where(
                TwBacktestRun.user_id == user_id,
                TwBacktestRun.source == source,
                TwBacktestRun.finished_at.is_not(None),
                TwBacktestRun.stats.is_not(None),
            )
            .order_by(TwBacktestRun.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
