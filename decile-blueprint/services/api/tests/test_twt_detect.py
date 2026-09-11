"""TW4's acceptance: the nightly three-weeks-tight job, against a real database.

Eight claims, and ``docs/twt/06-module-plan.md`` § TW4 states most of them almost word for word:

1. **Running the task twice for a date changes no rows.** House rule 7. A night that was killed
   and restarted must leave the same three tables — and the same four ratchet columns — as one
   that ran once.
2. **It upserts; it never clears the day first.** ``tw_order.signal_date`` is a real composite
   foreign key into ``tw_signal_daily`` (``03`` §6), so a delete-and-reinsert would fail on the
   one day it mattered: the day an order referenced the signal.
3. **A date with no published bars writes nothing and says so.** Not an exception, and not a
   silent zero either: `SKIPPED` with a reason, because "nothing is tight" and "no data" look
   identical on a page and are opposite problems.
4. **Tomorrow's trigger is computed tonight**, and ``high_since`` agrees with a recomputation
   from ``ohlcv_daily`` over the whole hold — which is the assertion ``03`` §5 asks this module
   for when it explains why the column is stored rather than queried.
5. **A split stores an exchange price and never lowers a stop.** ``entry_reference_close`` comes
   out equal to the exchange print, and the re-derived trigger — which a split makes *smaller* —
   is not written at all. ``04`` §7.3 and DECISIONS-TW TW0.7.
6. **The gate is read at the signal's own close.** Shifting the panel by one session moves the
   gate, the signal and the trigger by exactly one — house rule 5, measured rather than asserted.
7. **Truncating the panel at the session changes no answer for that session**, which is the same
   rule stated the other way round and is the form that catches a future bar leaking in.
8. And the one about neighbours: nothing here writes a `vb_` or an `sw_` row.

THE PANEL, AND WHY IT IS LAID OUT BACKWARDS
-------------------------------------------
``04`` §3.2's tight test reads **ISO weeks**, so a fixture indexed from the start of the panel
would change shape whenever the as-of moved — the week buckets would slide underneath it. Every
panel here is therefore drawn **backwards from its own as-of session**: a bar's close is a
function of how many ISO weeks back its week is, so "the same shape one session later" is a
single parameter. That is what makes claim 6 expressible at all.

The shape satisfies ``04`` §3 exactly once, on the as-of session:

===============  ==============================================================
weeks back        close
0                 100 on the as-of session, 96 on every other session of it
1, 2              100          -> the two weekly closes ``W`` compares with
3                 115          -> so the session before the as-of is NOT tight
4 … 11            95 down to 60 in fives, read backwards: the rise into the base
>= 12             55 / 50, alternating by week: a base nobody would call tight
20, 21, 22        80 for the one name that was tight **once before**, so that
                  `sessions_out_before` is a measured count on one row and the
                  fresh-panel fallback of DECISIONS-TW TW4.2 on another
===============  ==============================================================

Only the as-of session has ``max(W)/min(W) - 1 <= 3.01 %``. Every other session of the panel is
out by at least 4 %, so the entry event of ``04`` §3.4 — the state true today and false on each
of the five sessions before — holds on exactly one day, which is what makes "moves by exactly
one" a measurement rather than a coincidence.

This module carries its own database bootstrap and **never commits**: every test runs inside a
transaction that is rolled back, so it adds no row to any database and leaves the schema exactly
as it found it. The shared local Postgres is not this suite's to empty — and `test_twt_schema.py`
established the pattern for the same reason. It writes nothing to `trading_day` either: the
seeded NSE calendar already covers this window, and `lookback_start`'s fallback covers the case
where it does not, so there is no reason to `INSERT` into a table `services/worker/tests` holds
an `ACCESS EXCLUSIVE` lock on while it truncates.

**Run it in one pytest process, as `make test-db` does.** Two suites against one Postgres
deadlock on `instrument` — the worker suite truncates it while this one inserts into it — and
that is a property of the arrangement rather than of either suite.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_core.models import (
    AppUser,
    Exchange,
    Instrument,
    OhlcvDaily,
    TwBreadthDaily,
    TwConfig,
    TwPosition,
    TwSignalDaily,
    TwStateDaily,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, Gate, SignalState
from baskfy_worker.steps import StepOutcome, StepStatus
from baskfy_worker.tasks.twt import (
    LOOKBACK_SESSIONS,
    detect_session,
    high_since_from_bars,
    lookback_start,
    published_session_count,
    run_detect_twt,
)

ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"
#: ``services/api/tests`` -> ``services/api`` -> ``services`` -> ``decile-blueprint``.
REPO_ROOT: Final = Path(__file__).resolve().parents[3]
MONOREPO_ROOT: Final = Path(__file__).resolve().parents[4]
API_DIR: Final = REPO_ROOT / "services" / "api"


def _database_url() -> str | None:
    """``BASKFY_TEST_DATABASE_URL`` from the environment, or from the ``.env`` the stack writes.

    The ``.env`` fallback is `test_twt_schema.py`'s, for the reason it gives: a db-marked test
    that silently skips under the gate command that is supposed to prove it is worse than no
    test, because the skip reads as a pass.
    """
    from_env = os.environ.get(ENV_VAR)
    if from_env:
        return from_env
    for candidate in (REPO_ROOT / ".env", MONOREPO_ROOT / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == ENV_VAR and value.strip():
                return value.strip()
    return None


requires_db = pytest.mark.db(
    pytest.mark.skipif(
        _database_url() is None,
        reason=f"{ENV_VAR} is set neither in the environment nor in .env; run `make up`",
    )
)

pytestmark = requires_db

#: A Friday. **2019 deliberately**, not a date near today: this module shares a Postgres with
#: every other suite in the tree, and a fixture whose window overlaps another suite's committed
#: bars would have its breadth denominator decided by somebody else's test. Nothing else in the
#: repository writes bars here.
AS_OF: Final = dt.date(2019, 9, 13)
#: The next trading session, for the shift of claim 6.
NEXT_SESSION: Final = dt.date(2019, 9, 16)
#: 260 sessions, which is ``DataConfig.bars_required`` and therefore what the job asks for.
HISTORY: Final = LOOKBACK_SESSIONS

#: `04` §3.1 line 1 reads the exchange print, so every close below is one, and the *adjusted*
#: series a split fixture writes is this divided by the factor.
CLOSE_ON_THE_SIGNAL_SESSION: Final = 100.0
CLOSE_ELSEWHERE_IN_THE_WEEK: Final = 96.0
CLOSE_THREE_WEEKS_BACK: Final = 115.0
#: `weekday()` is 0..4 for Monday..Friday.
_SATURDAY: Final = 5
#: The week whose close is deliberately 15 % away, so that the session before the as-of is out.
_WEEK_OF_THE_STEP_DOWN: Final = 3


# --- the panel -----------------------------------------------------------------


def weekdays_ending(last: dt.date, count: int) -> list[dt.date]:
    """``count`` weekdays ending at ``last``, oldest first.

    Weekdays rather than the seeded NSE calendar, so the panel's ISO weeks are five sessions each
    and the shape the module docstring draws is the shape the detector sees. A real holiday
    inside the window is irrelevant here: the detector reads ``ohlcv_daily``, and ``trading_day``
    only decides how far back :func:`lookback_start` reaches.
    """
    dates: list[dt.date] = []
    day = last
    while len(dates) < count:
        if day.weekday() < _SATURDAY:
            dates.append(day)
        day -= dt.timedelta(days=1)
    return sorted(dates)


def weeks_back(dates: list[dt.date], as_of: dt.date) -> dict[dt.date, int]:
    """How many ISO-week buckets **the panel holds** each date is before the as-of's week.

    The panel's own buckets, not calendar weeks — ``04`` §3.2's own wording, and
    :func:`baskfy_core.twt.signals.weekly_closes` numbers them the same way.
    """
    key = {d: (d.isocalendar().year, d.isocalendar().week) for d in dates}
    order = {k: i for i, k in enumerate(sorted(set(key.values())))}
    base = order[key[as_of]]
    return {d: base - order[key[d]] for d in dates}


#: Week offsets 4..11: the rise into the base, read backwards.
_RISE_FIRST_WEEK: Final = 4
_RISE_LAST_WEEK: Final = 11
_RISE_TOP: Final = 95.0
_RISE_STEP: Final = 5.0
#: Week offsets 20..22 of a name that was tight once before, so `sessions_out_before` is a real
#: count rather than the fresh-panel fallback of DECISIONS-TW TW4.2. The plateau is 80 and not
#: the 55 of the base around it, because `04` §3.1's line 2 has to hold there too: a shelf at the
#: base is not 30 % above the low of the month three months earlier, and a name that fails line 2
#: is not in the state however tight its weekly closes are.
_EARLY_TIGHT_WEEKS: Final = (20, 21, 22)
_EARLY_TIGHT_CLOSE: Final = 80.0
_BASE_HIGH: Final = 55.0
_BASE_LOW: Final = 50.0


def tight_close(week: int, *, is_as_of: bool, early_tight: bool) -> float:
    """The exchange close of one session of the tight fixture (the docstring's table)."""
    if week == 0:
        return CLOSE_ON_THE_SIGNAL_SESSION if is_as_of else CLOSE_ELSEWHERE_IN_THE_WEEK
    if week in (1, 2):
        return CLOSE_ON_THE_SIGNAL_SESSION
    if week == _WEEK_OF_THE_STEP_DOWN:
        return CLOSE_THREE_WEEKS_BACK
    if _RISE_FIRST_WEEK <= week <= _RISE_LAST_WEEK:
        return _RISE_TOP - _RISE_STEP * (week - _RISE_FIRST_WEEK)
    if early_tight and week in _EARLY_TIGHT_WEEKS:
        return _EARLY_TIGHT_CLOSE
    return _BASE_HIGH if week % 2 == 0 else _BASE_LOW


def background_close(week: int) -> float:
    """A name that is never tight: its weekly closes alternate by 10 %."""
    return 110.0 if week % 2 else 100.0


@dataclass(frozen=True, slots=True)
class NameSpec:
    """One instrument of a panel."""

    symbol: str
    #: Shares a day. 600,000 at ₹100 is ₹6 crore, which clears `04` §3.5's ₹5 crore floor;
    #: 20,000 is ₹20 lakh, which does not and produces the `SCAN_ONLY` row `03` §3 keeps.
    volume: int = 600_000
    #: The stored adjusted series is the exchange print times this. 0.5 is a 1:2 split.
    factor: Decimal = Decimal(1)
    tight: bool = True
    early_tight: bool = False


@dataclass(slots=True)
class Panel:
    user_id: int
    as_of: dt.date
    dates: list[dt.date]
    ids: dict[str, int] = field(default_factory=dict)


async def _exchange(session: AsyncSession) -> int:
    found = (
        await session.execute(sa.select(Exchange.id).where(Exchange.id == NSE_EXCHANGE_ID))
    ).scalar_one_or_none()
    if found is None:
        session.add(Exchange(id=NSE_EXCHANGE_ID, code="NSE"))
        await session.flush()
    return NSE_EXCHANGE_ID


async def _user(session: AsyncSession) -> int:
    tag = uuid.uuid4().hex[:8]
    user = AppUser(public_id=f"tw4-{tag}", email=f"tw4-{tag}@example.test")
    session.add(user)
    await session.flush()
    session.add(TwConfig(user_id=user.id, updated_by="test"))
    await session.flush()
    return int(user.id)


async def build_panel(
    session: AsyncSession,
    *,
    as_of: dt.date = AS_OF,
    history: int = HISTORY,
    names: tuple[NameSpec, ...],
    extra_session: bool = False,
) -> Panel:
    """Write the calendar, the instruments and ``history`` sessions of bars, drawn backwards.

    ``extra_session`` appends **one more weekday after the as-of**, with an absurd close and an
    absurd volume. Nothing about the as-of session's answer may move because of it — that is
    house rule 5 at its most literal, and it is what claim 7 asserts.
    """
    await _exchange(session)
    user_id = await _user(session)
    dates = weekdays_ending(as_of, history)
    after = weekdays_ending(as_of + dt.timedelta(days=7), 5)
    tail = [day for day in after if day > as_of][:1] if extra_session else []
    # **No `trading_day` row is written.** The seeded NSE calendar already reaches this window,
    # and `lookback_start` falls back to a generous span of calendar days when it does not — both
    # of which put the window's start before this panel's first bar. Writing the calendar here
    # would mean an `INSERT` into a table the worker suite `TRUNCATE`s, which is a deadlock
    # waiting for the first time the two suites overlap. It happened; this is the fix.
    reach = await lookback_start(session, as_of, LOOKBACK_SESSIONS)
    assert reach <= dates[0], (
        f"the lookback reaches only {reach}, which is inside this panel — `trading_day` has "
        f"fewer sessions than the fixture assumes and the detector would see a short history"
    )
    offsets = weeks_back(dates, as_of)
    panel = Panel(user_id=user_id, as_of=as_of, dates=dates)
    for spec in names:
        instrument = Instrument(
            exchange_id=NSE_EXCHANGE_ID,
            symbol=f"{spec.symbol}{uuid.uuid4().hex[:6].upper()}",
            name=f"{spec.symbol} LIMITED",
            series="EQ",
            instrument_type="EQ",
            is_active=True,
            listed_on=dt.date(2011, 1, 1),
        )
        session.add(instrument)
        await session.flush()
        panel.ids[spec.symbol] = int(instrument.id)
        for day in dates:
            raw = (
                tight_close(offsets[day], is_as_of=day == as_of, early_tight=spec.early_tight)
                if spec.tight
                else background_close(offsets[day])
            )
            _add_bar(session, int(instrument.id), day, raw, spec)
        for day in tail:
            _add_bar(session, int(instrument.id), day, 500.0, spec, volume=99_000_000)
    await session.flush()
    return panel


def _add_bar(  # noqa: PLR0913 - one argument per field of the row it writes
    session: AsyncSession,
    instrument_id: int,
    day: dt.date,
    raw: float,
    spec: NameSpec,
    *,
    volume: int | None = None,
) -> None:
    """One ``ohlcv_daily`` row, with ``raw`` as the **exchange** close.

    ``close`` is stored adjusted — ``raw x adj_factor`` — which is how the plant stores it and is
    what makes the split fixture of claim 5 internally consistent rather than a row with two
    unrelated prices in it. ``high`` is the close and ``low`` is a percent under it, so the
    session's high is the number the ratchet raises ``high_since`` to.
    """
    adjusted = Decimal(str(raw)) * spec.factor
    session.add(
        OhlcvDaily(
            instrument_id=instrument_id,
            date=day,
            open=adjusted,
            high=adjusted,
            low=adjusted * Decimal("0.99"),
            close=adjusted,
            close_raw=Decimal(str(raw)),
            volume=volume if volume is not None else spec.volume,
            volume_raw=volume if volume is not None else spec.volume,
            adj_factor=spec.factor,
            source="nse",
        )
    )


#: The composition every behavioural test uses: one liquid name (a `SIGNAL`), one illiquid name
#: (a `SCAN_ONLY`), one name that was tight once before (so `sessions_out_before` is a real
#: count), and one that is never tight at all (so the breadth denominator is not the numerator).
STANDARD: Final[tuple[NameSpec, ...]] = (
    NameSpec("TIGHTLIQ"),
    NameSpec("TIGHTTHIN", volume=20_000),
    NameSpec("TIGHTAGAIN", early_tight=True),
    NameSpec("CHOPPY", tight=False),
)


# --- the bootstrap -------------------------------------------------------------


def _migrate(url: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=API_DIR,
        env={
            **{k: v for k, v in os.environ.items() if k != "BASKFY_DATABASE_URL"},
            "BASKFY_DATABASE_URL": url,
        },
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture(scope="module")
def twt_url() -> str:
    """A database migrated to head. **Nothing is dropped**: other suites share this server."""
    url = _database_url()
    if url is None:  # pragma: no cover - guarded by requires_db
        pytest.skip(f"{ENV_VAR} is not set")
    _migrate(url)
    return url


@asynccontextmanager
async def rolled_back(url: str) -> AsyncIterator[AsyncSession]:
    """A session whose work is **never committed.**"""
    engine = create_async_engine(url)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


async def _states(session: AsyncSession, panel: Panel) -> dict[int, TwStateDaily]:
    rows = (
        (
            await session.execute(
                sa.select(TwStateDaily).where(
                    TwStateDaily.user_id == panel.user_id, TwStateDaily.date == panel.as_of
                )
            )
        )
        .scalars()
        .all()
    )
    return {int(row.instrument_id): row for row in rows}


async def _signals(session: AsyncSession, panel: Panel) -> dict[int, TwSignalDaily]:
    rows = (
        (
            await session.execute(
                sa.select(TwSignalDaily).where(
                    TwSignalDaily.user_id == panel.user_id, TwSignalDaily.date == panel.as_of
                )
            )
        )
        .scalars()
        .all()
    )
    return {int(row.instrument_id): row for row in rows}


async def _breadth(
    session: AsyncSession, panel: Panel, on: dt.date | None = None
) -> TwBreadthDaily:
    return (
        await session.execute(
            sa.select(TwBreadthDaily).where(
                TwBreadthDaily.user_id == panel.user_id,
                TwBreadthDaily.date == (on or panel.as_of),
            )
        )
    ).scalar_one()


async def _snapshot(session: AsyncSession, panel: Panel) -> list[tuple[object, ...]]:
    """Every stored number of the session, in one comparable shape.

    ``created_at`` is deliberately absent: an upsert leaves it where it was, and a test that
    compared it would be asserting the default rather than the write. Everything else is the
    contract.
    """
    out: list[tuple[object, ...]] = []
    for row in sorted((await _states(session, panel)).items()):
        state = row[1]
        out.append(
            (
                "state",
                row[0],
                state.close,
                state.close_raw,
                state.adj_factor,
                state.week_close_0,
                state.week_close_1,
                state.week_close_2,
                state.week_range_pct,
                state.month_low_3,
                state.month_low_ratio,
                state.vol_sma_50,
                state.volume,
                state.turnover_inr,
                state.turnover_avg_20,
                state.sma_dma,
                state.sessions_in_state,
                state.bars_in_window,
                state.locked_upper_circuit,
            )
        )
    for entry in sorted((await _signals(session, panel)).items()):
        signal = entry[1]
        out.append(
            (
                "signal",
                entry[0],
                signal.state,
                tuple(signal.failed_filters),
                signal.entry_reference_close,
                signal.stop_preview,
                signal.sessions_out_before,
                signal.rank_key,
                signal.turnover_avg_20,
            )
        )
    breadth = await _breadth(session, panel)
    out.append(
        (
            "breadth",
            breadth.universe_count,
            breadth.measured_count,
            breadth.above_count,
            breadth.pct_above_dma,
            breadth.gate,
            breadth.thin_session,
            breadth.detail,
        )
    )
    positions = (
        (await session.execute(sa.select(TwPosition).where(TwPosition.user_id == panel.user_id)))
        .scalars()
        .all()
    )
    for position in sorted(positions, key=lambda p: int(p.id)):
        out.append(
            (
                "position",
                int(position.instrument_id),
                position.high_since,
                position.high_since_date,
                position.next_trigger,
                position.next_trigger_for,
                position.stop_price,
                position.gtt_trigger,
            )
        )
    return out


async def _open_position(  # noqa: PLR0913 - a fixture is its knobs, and each is a column
    session: AsyncSession,
    panel: Panel,
    symbol: str,
    *,
    entry_avg: Decimal,
    stop: Decimal,
    high_since: Decimal,
    initial_stop: Decimal | None = None,
    entry_adj_factor: Decimal = Decimal(1),
    sessions_held: int = 5,
) -> TwPosition:
    """An ``OPEN`` line the sleeve bought ``sessions_held`` sessions ago, with a resting GTT.

    ``initial_stop`` is separate from ``stop`` because ``tw_position`` constrains the first to be
    **below the entry** and the second to be at or above the first: a line whose stop has
    ratcheted past its entry is an ordinary state of this book after a few months, and a fixture
    that collapsed the two could not express it.
    """
    entry_date = panel.dates[-sessions_held]
    position = TwPosition(
        user_id=panel.user_id,
        instrument_id=panel.ids[symbol],
        signal_date=entry_date,
        entry_date=entry_date,
        entry_avg=entry_avg,
        quantity_entered=10,
        entry_adj_factor=entry_adj_factor,
        initial_stop=initial_stop if initial_stop is not None else stop,
        stop_price=stop,
        high_since=high_since,
        high_since_date=entry_date,
        gtt_id="gtt-1",
        gtt_trigger=stop,
        quantity_open=10,
        state="OPEN",
        simulated=True,
    )
    session.add(position)
    await session.flush()
    return position


async def _detect(session: AsyncSession, panel: Panel, on: dt.date | None = None) -> StepOutcome:
    outcome = StepOutcome()
    await run_detect_twt(session, outcome, on or panel.as_of, user_id=panel.user_id)
    return outcome


# --- 1 & 2: idempotence, and an upsert rather than a rewrite --------------------


class TestRunningItTwiceChangesNothing:
    async def test_a_second_run_of_the_date_is_idempotent(self, twt_url: str) -> None:
        """House rule 7, over every column the job writes — not over a row count.

        A count would pass while a percentage moved. This compares the stored numbers, the
        funnel JSON and the four ratchet columns of the book.
        """
        async with rolled_back(twt_url) as session:
            panel = await build_panel(session, names=STANDARD)
            await _open_position(
                session,
                panel,
                "TIGHTLIQ",
                entry_avg=Decimal("96.00"),
                stop=Decimal("76.80"),
                high_since=Decimal("96.00"),
            )
            await _detect(session, panel)
            first = await _snapshot(session, panel)
            await _detect(session, panel)
            second = await _snapshot(session, panel)
            assert first == second
            assert first, "the fixture produced no rows at all, so this would prove nothing"

    async def test_the_detector_writes_the_three_tables_the_plan_names(self, twt_url: str) -> None:
        """`06` § TW4's Goal, as a read: state, signals and one breadth row."""
        async with rolled_back(twt_url) as session:
            panel = await build_panel(session, names=STANDARD)
            await _detect(session, panel)
            states = await _states(session, panel)
            signals = await _signals(session, panel)
            breadth = await _breadth(session, panel)

            tight = {panel.ids[name] for name in ("TIGHTLIQ", "TIGHTTHIN", "TIGHTAGAIN")}
            assert set(states) == tight
            assert panel.ids["CHOPPY"] not in states
            assert set(signals) == tight

            assert signals[panel.ids["TIGHTLIQ"]].state == SignalState.SIGNAL.value
            assert signals[panel.ids["TIGHTLIQ"]].failed_filters == []
            assert signals[panel.ids["TIGHTTHIN"]].state == SignalState.SCAN_ONLY.value
            assert signals[panel.ids["TIGHTTHIN"]].failed_filters == ["TURNOVER"]

            assert breadth.gate == Gate.OPEN.value
            assert breadth.thin_session is False
            assert breadth.pct_above_dma is not None
            assert breadth.dma_bars == DEFAULT_TWT_CONFIG.breadth.dma_bars
            assert breadth.above_count >= len(tight)
            assert breadth.measured_count >= breadth.above_count

    async def test_the_stored_state_row_is_auditable_from_its_own_columns(
        self, twt_url: str
    ) -> None:
        """`03` §2: `week_range_pct` and `month_low_ratio` must be derivable from the row.

        They are only derivable if the comparands beside them are in the same price space, which
        is why `tw_state_daily` keeps the **adjusted** weekly closes and the adjusted month-3
        low rather than converting them the way `tw_signal_daily`'s two levels are converted.
        """
        async with rolled_back(twt_url) as session:
            panel = await build_panel(session, names=STANDARD)
            await _detect(session, panel)
            row = (await _states(session, panel))[panel.ids["TIGHTLIQ"]]

            assert row.week_close_0 == row.close
            assert row.week_close_1 == row.week_close_2 == row.close
            assert row.week_range_pct == Decimal("0.0000")
            assert row.week_range_pct <= DEFAULT_TWT_CONFIG.scan.tight_band_pct
            assert row.month_low_ratio >= DEFAULT_TWT_CONFIG.scan.month_low_multiple
            derived = (row.close / row.month_low_3).quantize(Decimal("0.0001"))
            assert row.month_low_ratio == derived
            assert row.sessions_in_state == 1
            assert row.bars_in_window == DEFAULT_TWT_CONFIG.breadth.dma_bars
            assert row.locked_upper_circuit is False
            assert row.turnover_inr == int(row.close_raw * row.volume)

    async def test_sessions_out_before_counts_the_sessions_the_state_was_false(
        self, twt_url: str
    ) -> None:
        """`03` §3 and DECISIONS-TW TW4.2's two branches, side by side.

        `TIGHTAGAIN` held the state about a hundred sessions ago, so the core's own counter
        answers. `TIGHTLIQ` has never held it, so the counter is null and the row falls back to
        every session the name has been listed and out — a number that is still, and always,
        at least `entry_min_sessions_out`.
        """
        async with rolled_back(twt_url) as session:
            panel = await build_panel(session, names=STANDARD)
            await _detect(session, panel)
            signals = await _signals(session, panel)
            gap = DEFAULT_TWT_CONFIG.entry.entry_min_sessions_out

            once = signals[panel.ids["TIGHTAGAIN"]].sessions_out_before
            never = signals[panel.ids["TIGHTLIQ"]].sessions_out_before
            assert once >= gap
            assert once < len(panel.dates) - 1, "this name was tight once; it is not a fresh panel"
            assert never == len(panel.dates) - 1

    async def test_the_task_upserts_and_never_deletes_the_day_first(self, twt_url: str) -> None:
        """TW3's finding: `tw_order.signal_date` is a composite FK into `tw_signal_daily`.

        The proof is not a grep — it is a second run of a day whose signal row a **real order**
        references. A delete-and-reinsert raises `ForeignKeyViolation` here; an upsert does not.
        """
        async with rolled_back(twt_url) as session:
            panel = await build_panel(session, names=STANDARD)
            await _detect(session, panel)
            await session.execute(
                sa.insert(sa.table("tw_order", *[sa.column(c) for c in _ORDER_COLUMNS])).values(
                    user_id=panel.user_id,
                    instrument_id=panel.ids["TIGHTLIQ"],
                    signal_date=panel.as_of,
                    side="BUY",
                    quantity=10,
                    stop_price=Decimal("80.00"),
                    state="PROPOSED",
                    simulated=True,
                )
            )
            await session.flush()
            await _detect(session, panel)
            signals = await _signals(session, panel)
            assert panel.ids["TIGHTLIQ"] in signals


_ORDER_COLUMNS: Final = (
    "user_id",
    "instrument_id",
    "signal_date",
    "side",
    "quantity",
    "stop_price",
    "state",
    "simulated",
)


# --- the two breadth rows the schema's CHECK constraints keep apart ---------------


#: Eight names, so that a session on which one of them prints is **below** `04` §2.1's
#: `thin_session_min_share` [0.25] of the rolling median of the traded count. With four names the
#: threshold is 1.0 and a one-name session is not below it — the rule is a share, and a fixture
#: that cannot cross it cannot test it.
THIN_PANEL: Final[tuple[NameSpec, ...]] = (
    NameSpec("TIGHTLIQ"),
    *(NameSpec(f"CHOPPY{index}", tight=False) for index in range(7)),
)


class TestTheTwoKindsOfShutSession:
    async def test_a_measured_session_with_no_denominator_writes_a_zero_and_shuts_the_gate(
        self, twt_url: str
    ) -> None:
        """`04` §4.2, and the CHECK that holds it: a **non-thin** row must carry a percentage.

        A panel of thirty sessions has no name with a valid 200-day average under `04` §2.2's
        tolerance, so `measured_count` is 0. The gate is `SHUT` and `pct_above_dma` is `0.0000` —
        not null, because the schema allows a null only on the thin-session row, and not "open
        because we cannot tell", because an empty universe must never read as a healthy market.
        """
        async with rolled_back(twt_url) as session:
            panel = await build_panel(session, names=STANDARD, history=30)
            await _detect(session, panel)
            breadth = await _breadth(session, panel)

            assert breadth.measured_count == 0
            assert breadth.above_count == 0
            assert breadth.pct_above_dma == Decimal("0.0000")
            assert breadth.gate == Gate.SHUT.value
            assert breadth.thin_session is False
            assert await _states(session, panel) == {}

    async def test_a_thin_session_is_recorded_shut_and_the_book_does_not_ratchet_on_it(
        self, twt_url: str
    ) -> None:
        """`04` §2.1 and DECISIONS-TW **TW4.4**.

        One name prints out of eight, which is below a quarter of the rolling median, so the rule
        removes the session from the calendar. The row is written **for the record** — the history
        must say *why* there was no signal rather than leave a hole that reads like a failed job —
        with `thin_session = true`, `gate = SHUT` and a null percentage, which is the one case the
        schema allows a null in.

        And the trail does not move. A stop derived from a 200-name muhurat session is a stop
        nobody can reconcile with the calendar the rest of the sleeve reads.
        """
        async with rolled_back(twt_url) as session:
            panel = await build_panel(session, names=THIN_PANEL)
            thin_day = panel.dates[-30]
            await session.execute(
                sa.delete(OhlcvDaily).where(
                    OhlcvDaily.date == thin_day,
                    OhlcvDaily.instrument_id != panel.ids["TIGHTLIQ"],
                )
            )
            position = await _open_position(
                session,
                panel,
                "TIGHTLIQ",
                entry_avg=Decimal("96.00"),
                stop=Decimal("76.80"),
                high_since=Decimal("96.00"),
                sessions_held=40,
            )
            outcome = await _detect(session, panel, on=thin_day)

            breadth = await _breadth(session, panel, on=thin_day)
            assert breadth.thin_session is True
            assert breadth.gate == Gate.SHUT.value
            assert breadth.pct_above_dma is None
            assert outcome.status is StepStatus.SKIPPED
            assert "thin session" in str(outcome.detail["skipped_reason"])

            await session.refresh(position)
            assert position.next_trigger is None
            assert position.next_trigger_for is None
            assert position.high_since == Decimal("96.00")


# --- 3: a date with nothing to read --------------------------------------------


class TestADateWithNothingToRead:
    async def test_a_date_with_no_bars_writes_nothing_and_says_so(self, twt_url: str) -> None:
        """`06` § TW4: "a date with no published bars writes nothing and says so"."""
        async with rolled_back(twt_url) as session:
            panel = await build_panel(session, names=STANDARD)
            beyond = panel.as_of + dt.timedelta(days=30)
            outcome = await _detect(session, panel, on=beyond)

            assert outcome.status is StepStatus.SKIPPED
            assert "not a session the bars know about" in str(outcome.detail["skipped_reason"])
            assert await published_session_count(session, panel.user_id, beyond) == 0

    async def test_an_unpublished_session_leaves_no_breadth_row_behind(self, twt_url: str) -> None:
        """Silence and "nothing happened" are different answers, and a false breadth row is the
        worse of the two: the evening job reads that table as its calendar."""
        async with rolled_back(twt_url) as session:
            panel = await build_panel(session, names=STANDARD)
            await _detect(session, panel, on=panel.as_of + dt.timedelta(days=30))
            rows = (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(TwBreadthDaily)
                    .where(TwBreadthDaily.user_id == panel.user_id)
                )
            ).scalar_one()
            assert rows == 0

    async def test_an_empty_universe_writes_nothing_and_names_the_reason(
        self, twt_url: str
    ) -> None:
        """A deployment with no instruments is a seeding problem, not a detector failure."""
        async with rolled_back(twt_url) as session:
            await _exchange(session)
            user_id = await _user(session)
            outcome = StepOutcome()
            written = await run_detect_twt(session, outcome, AS_OF, user_id=user_id)
            assert written == 0
            assert outcome.status is StepStatus.SKIPPED
            assert "no bars" in str(outcome.detail["skipped_reason"])


# --- 4: tomorrow's trigger, computed tonight ------------------------------------


class TestTomorrowsTriggerIsComputedTonight:
    async def test_the_ratchet_raises_high_since_and_stores_the_next_trigger(
        self, twt_url: str
    ) -> None:
        """`04` §7.2, arithmetic and all.

        The session's high is 100, so ``high_since`` goes to 100 and the trail sits 20 % under
        it at 80.00 — under the close, so the clamp does not bind, and above the resting 76.80,
        so it is a line the morning can carry.
        """
        async with rolled_back(twt_url) as session:
            panel = await build_panel(session, names=STANDARD)
            position = await _open_position(
                session,
                panel,
                "TIGHTLIQ",
                entry_avg=Decimal("96.00"),
                stop=Decimal("76.80"),
                high_since=Decimal("96.00"),
            )
            await _detect(session, panel)
            await session.refresh(position)

            assert position.high_since == Decimal("100.00")
            assert position.high_since_date == panel.as_of
            assert position.next_trigger == Decimal("80.00")
            assert position.next_trigger_for == panel.as_of

    async def test_the_trigger_is_never_written_without_the_session_it_was_computed_for(
        self, twt_url: str
    ) -> None:
        """`03` §5's CHECK, and `04` §11.3's rule that a plan never reads a trigger computed for
        another session. The constraint exists; this is the job honouring it."""
        async with rolled_back(twt_url) as session:
            panel = await build_panel(session, names=STANDARD)
            position = await _open_position(
                session,
                panel,
                "TIGHTLIQ",
                entry_avg=Decimal("96.00"),
                stop=Decimal("76.80"),
                high_since=Decimal("96.00"),
            )
            await _detect(session, panel)
            await session.refresh(position)
            assert position.next_trigger is None or position.next_trigger_for is not None

    async def test_the_ratchet_never_moves_the_stop_in_force(self, twt_url: str) -> None:
        """`02` Track C §3: the ratchet is a plan line, never a job.

        The evening computes the level. ``stop_price``, ``gtt_id`` and ``gtt_trigger`` belong to
        the resting order at the exchange and move only when a person has confirmed a
        ``RAISE_GTT_STOP`` and the desk has actually replaced it (TW6).
        """
        async with rolled_back(twt_url) as session:
            panel = await build_panel(session, names=STANDARD)
            position = await _open_position(
                session,
                panel,
                "TIGHTLIQ",
                entry_avg=Decimal("96.00"),
                stop=Decimal("76.80"),
                high_since=Decimal("96.00"),
            )
            await _detect(session, panel)
            await session.refresh(position)
            assert position.stop_price == Decimal("76.80")
            assert position.gtt_trigger == Decimal("76.80")
            assert position.gtt_id == "gtt-1"

    async def test_high_since_agrees_with_a_recomputation_over_the_hold(self, twt_url: str) -> None:
        """`03` §5 explains why the column is stored and then asks TW4 to prove it is right.

        The stored number is an accumulation, one close at a time; the recomputation reads every
        bar of the hold in one pass. They are different arithmetic and they must agree.
        """
        async with rolled_back(twt_url) as session:
            panel = await build_panel(session, names=STANDARD)
            position = await _open_position(
                session,
                panel,
                "TIGHTLIQ",
                entry_avg=Decimal("96.00"),
                stop=Decimal("76.80"),
                high_since=Decimal("96.00"),
            )
            await _detect(session, panel)
            await session.refresh(position)
            recomputed = await high_since_from_bars(session, position, panel.as_of)
            assert position.high_since == recomputed

    async def test_a_trigger_at_or_above_the_close_is_clamped_under_it(self, twt_url: str) -> None:
        """`04` §7.2's clamp. A trigger at the last traded price fires the moment it is armed,
        which on a GTT means selling the position at the next tick for no reason.

        A line bought at 130.00 that has since fallen to 100.00 trails to 104.00 — *above* the
        last traded price — so §7.2's second branch applies and the trigger becomes
        ``tick_floor(close x close_clamp_fallback)`` = 99.90. Strictly under the close, and still
        above the resting 90.00, so it is a line the morning can carry.
        """
        async with rolled_back(twt_url) as session:
            panel = await build_panel(session, names=STANDARD)
            position = await _open_position(
                session,
                panel,
                "TIGHTLIQ",
                entry_avg=Decimal("130.00"),
                stop=Decimal("90.00"),
                high_since=Decimal("130.00"),
            )
            await _detect(session, panel)
            await session.refresh(position)
            assert position.next_trigger == Decimal("99.90")
            assert position.next_trigger < Decimal("100.00"), "a trigger at the last price fires"
            assert position.high_since == Decimal("130.00"), "the session's high did not beat it"


# --- 5: the corporate action ----------------------------------------------------


class TestASplitStoresAnExchangePriceAndNeverLowersAStop:
    async def test_a_split_stores_an_entry_reference_equal_to_the_raw_price(
        self, twt_url: str
    ) -> None:
        """`03` §10: levels leave core adjusted and the task converts them.

        The whole fixture is a 1:2 split — every stored close is half the exchange print and
        ``adj_factor`` is 0.5 — so a converted level and the exchange print are the same number
        exactly when the conversion is right.
        """
        async with rolled_back(twt_url) as session:
            panel = await build_panel(
                session,
                names=(NameSpec("SPLITCO", factor=Decimal("0.5")), NameSpec("CHOPPY", tight=False)),
            )
            await _detect(session, panel)
            state = (await _states(session, panel))[panel.ids["SPLITCO"]]
            signal = (await _signals(session, panel))[panel.ids["SPLITCO"]]

            assert state.adj_factor == Decimal("0.5000000000")
            assert state.close == Decimal("50.00")
            assert state.close_raw == Decimal("100.0000")
            assert signal.entry_reference_close == Decimal("100.00")
            assert signal.entry_reference_close == state.close_raw.quantize(Decimal("0.01"))
            assert signal.stop_preview == Decimal("80.00")
            assert signal.stop_preview < signal.entry_reference_close

    async def test_a_corporate_action_never_lowers_a_resting_stop(self, twt_url: str) -> None:
        """`04` §7.3 and DECISIONS-TW **TW0.7**, which is the rule this sleeve must never break.

        The position was bought before the split at an ``entry_adj_factor`` of 1 and is protected
        at 144.00 in pre-split money. Re-deriving the trail off the newly adjusted series gives
        80.00 — arithmetically correct and **lower than the resting stop**. Cancelling a resting
        stop to arm a lower one is the one thing this sleeve must never do on its own, so no
        trigger is written at all and the alert is what happens instead.
        """
        async with rolled_back(twt_url) as session:
            panel = await build_panel(
                session,
                names=(NameSpec("SPLITCO", factor=Decimal("0.5")), NameSpec("CHOPPY", tight=False)),
            )
            position = await _open_position(
                session,
                panel,
                "SPLITCO",
                entry_avg=Decimal("180.00"),
                stop=Decimal("144.00"),
                high_since=Decimal("200.00"),
                entry_adj_factor=Decimal(1),
            )
            await _detect(session, panel)
            await session.refresh(position)

            assert position.next_trigger is None, (
                "a split re-derives a lower trail, and `04` §7.3 refuses to emit it"
            )
            assert position.stop_price == Decimal("144.00")
            assert position.gtt_trigger == Decimal("144.00")
            # The level is re-expressed in today's money so a person can read the two against
            # each other; it is not a stop, so lowering it is the honest answer.
            assert position.high_since == Decimal("100.00")
            assert position.high_since_date == panel.as_of

    async def test_an_adjustment_that_raises_the_trail_is_emitted(self, twt_url: str) -> None:
        """The other half of TW0.7, so the refusal above is a rule rather than a dead branch.

        The same split, but the position's resting stop is 40.00 in post-split money — a line a
        person has already re-armed. The re-derived trail of 80.00 is **above** it, so it is
        written and the morning carries a `RAISE_GTT_STOP`.
        """
        async with rolled_back(twt_url) as session:
            panel = await build_panel(
                session,
                names=(NameSpec("SPLITCO", factor=Decimal("0.5")), NameSpec("CHOPPY", tight=False)),
            )
            position = await _open_position(
                session,
                panel,
                "SPLITCO",
                entry_avg=Decimal("50.00"),
                stop=Decimal("40.00"),
                high_since=Decimal("50.00"),
                entry_adj_factor=Decimal(1),
            )
            await _detect(session, panel)
            await session.refresh(position)

            assert position.next_trigger == Decimal("80.00")
            assert position.next_trigger_for == panel.as_of
            assert position.next_trigger > position.stop_price


# --- 6 & 7: the look-ahead measurements -----------------------------------------


class TestTheLookAheadMeasurement:
    async def test_shifting_the_panel_by_one_session_moves_the_signal_by_exactly_one(
        self, twt_url: str
    ) -> None:
        """House rule 5, measured. `06` § TW4's own words, and `04` §11.3's.

        Two panels of the same shape, one drawn to a Friday and one to the Monday after it. The
        signal, the gate and the trigger all land on the panel's own as-of and on nothing before
        it — in particular the shifted panel is **silent on the Friday**, which is what a
        detector that had read the following Monday's bar would not be.
        """
        async with rolled_back(twt_url) as first:
            panel = await build_panel(first, names=STANDARD, as_of=AS_OF)
            await _open_position(
                first,
                panel,
                "TIGHTLIQ",
                entry_avg=Decimal("96.00"),
                stop=Decimal("76.80"),
                high_since=Decimal("96.00"),
            )
            await _detect(first, panel)
            before = await _summary(first, panel, panel.as_of)

        async with rolled_back(twt_url) as second:
            shifted = await build_panel(second, names=STANDARD, as_of=NEXT_SESSION)
            await _open_position(
                second,
                shifted,
                "TIGHTLIQ",
                entry_avg=Decimal("96.00"),
                stop=Decimal("76.80"),
                high_since=Decimal("96.00"),
            )
            # The session **before** the shifted as-of: the shape is not complete there, and a
            # detector that peeked at the next bar would already be calling it tight.
            await _detect(second, shifted, on=AS_OF)
            early = await _summary(second, shifted, AS_OF)
            await _detect(second, shifted)
            after = await _summary(second, shifted, shifted.as_of)

        assert before["signals"], "the unshifted panel produced no signal; nothing to compare"
        assert after["signals"] == before["signals"]
        assert after["gate"] == before["gate"]
        assert after["pct"] == before["pct"]
        assert after["trigger"] == before["trigger"]
        assert early["signals"] == [], "the shifted panel signalled a session early"
        assert early["states"] == []

    async def test_no_look_ahead_a_bar_after_the_session_changes_no_answer_for_it(
        self, twt_url: str
    ) -> None:
        """The same rule stated the other way round, and the form that catches a leak.

        The second panel carries one extra session **after** the as-of, with a close of 500 on
        99 million shares. Every stored number for the as-of session must be byte-identical, and
        so must the trigger the book was given that evening.
        """
        async with rolled_back(twt_url) as first:
            panel = await build_panel(first, names=STANDARD)
            await _open_position(
                first,
                panel,
                "TIGHTLIQ",
                entry_avg=Decimal("96.00"),
                stop=Decimal("76.80"),
                high_since=Decimal("96.00"),
            )
            await _detect(first, panel)
            without = _anonymise(await _snapshot(first, panel), panel)

        async with rolled_back(twt_url) as second:
            longer = await build_panel(second, names=STANDARD, extra_session=True)
            await _open_position(
                second,
                longer,
                "TIGHTLIQ",
                entry_avg=Decimal("96.00"),
                stop=Decimal("76.80"),
                high_since=Decimal("96.00"),
            )
            await _detect(second, longer)
            with_a_future_bar = _anonymise(await _snapshot(second, longer), longer)

        assert without == with_a_future_bar


async def _summary(session: AsyncSession, panel: Panel, on: dt.date) -> dict[str, object]:
    """What a session decided, keyed by symbol rather than by instrument id.

    Ids differ between two panels built in two transactions; the symbols do not, and neither do
    the numbers — which is the whole point of the comparison.
    """
    by_id = {value: key for key, value in panel.ids.items()}
    signals = (
        (
            await session.execute(
                sa.select(TwSignalDaily).where(
                    TwSignalDaily.user_id == panel.user_id, TwSignalDaily.date == on
                )
            )
        )
        .scalars()
        .all()
    )
    states = (
        (
            await session.execute(
                sa.select(TwStateDaily).where(
                    TwStateDaily.user_id == panel.user_id, TwStateDaily.date == on
                )
            )
        )
        .scalars()
        .all()
    )
    breadth = (
        await session.execute(
            sa.select(TwBreadthDaily).where(
                TwBreadthDaily.user_id == panel.user_id, TwBreadthDaily.date == on
            )
        )
    ).scalar_one_or_none()
    positions = (
        (await session.execute(sa.select(TwPosition).where(TwPosition.user_id == panel.user_id)))
        .scalars()
        .all()
    )
    return {
        "signals": sorted(
            (by_id[int(row.instrument_id)], row.state, row.entry_reference_close, row.stop_preview)
            for row in signals
        ),
        "states": sorted(by_id[int(row.instrument_id)] for row in states),
        "gate": None if breadth is None else breadth.gate,
        "pct": None if breadth is None else breadth.pct_above_dma,
        "trigger": sorted(
            (by_id[int(row.instrument_id)], row.next_trigger, row.high_since) for row in positions
        ),
    }


def _anonymise(rows: list[tuple[object, ...]], panel: Panel) -> list[tuple[object, ...]]:
    """The snapshot with instrument ids replaced by symbols, so two panels compare."""
    by_id = {value: key for key, value in panel.ids.items()}
    # Column 1 and column 1 only. A blanket replacement reads `sessions_out_before = 99` as
    # instrument 99 the moment the sequence happens to hand one out, which is a comparison that
    # passes or fails on the order of the tests before it.
    swapped: list[tuple[object, ...]] = []
    for row in rows:
        if row[0] == "breadth":
            swapped.append(row)
            continue
        swapped.append((row[0], by_id[int(str(row[1]))], *row[2:]))
    return swapped


# --- 8: the neighbours ----------------------------------------------------------


class TestTheNeighboursAreUntouched:
    async def test_the_detector_writes_no_row_of_another_sleeve(self, twt_url: str) -> None:
        """`04` §1.4 and TW10's rule: this sleeve stores its own breadth row and its own state,
        and reaches into no other book's tables."""
        async with rolled_back(twt_url) as session:
            panel = await build_panel(session, names=STANDARD)
            await _detect(session, panel)
            neighbours = (
                "vb_signal_daily",
                "vb_breadth_daily",
                "sw_setup_daily",
                "sw_market_daily",
            )
            for table in neighbours:
                count = (
                    await session.execute(sa.select(sa.func.count()).select_from(sa.table(table)))
                ).scalar_one()
                assert count == 0, f"the TWT detector wrote a {table} row"


# --- the 21:00 retry -------------------------------------------------------------


class TestTheRetryAsksBeforeItWorks:
    """`06` § TW4: "a 21:00 IST retry Beat entry, a no-op when the session already has rows".

    The Beat task and ``make twt`` share one rule and one helper, which is why the test drives
    the CLI's own body rather than restating the condition: a retry that re-detected a session
    the chain had already written would densify 260 sessions over the whole cash universe to
    arrive at the same rows, every weeknight.
    """

    async def test_the_retry_is_a_no_op_when_the_session_already_has_rows(
        self, twt_url: str
    ) -> None:
        async with rolled_back(twt_url) as session:
            panel = await build_panel(session, names=STANDARD)
            first = await detect_session(session, panel.as_of, user_id=panel.user_id, force=False)
            assert "skipped" not in first

            again = await detect_session(session, panel.as_of, user_id=panel.user_id, force=False)
            assert again["skipped"] == "already detected"
            assert int(str(again["rows"])) == 1

    async def test_the_retry_asks_the_breadth_table_and_not_the_signal_table(
        self, twt_url: str
    ) -> None:
        """DECISIONS-TW **TW4.3**, and it is the difference from VBT-1's equivalent.

        This book signals about eighteen times a **year**, so a session with no signal row is
        what a session that ran perfectly looks like on most days. Here is such a session: the
        panel is one name that is never tight, so nothing signals and nothing holds the state —
        and the retry must still answer "already detected", because the breadth row was written.
        """
        async with rolled_back(twt_url) as session:
            panel = await build_panel(session, names=(NameSpec("CHOPPY", tight=False),))
            await detect_session(session, panel.as_of, user_id=panel.user_id, force=False)

            assert await _signals(session, panel) == {}
            assert await _states(session, panel) == {}
            assert (await _breadth(session, panel)).gate in {Gate.OPEN.value, Gate.SHUT.value}

            again = await detect_session(session, panel.as_of, user_id=panel.user_id, force=False)
            assert again["skipped"] == "already detected"

    async def test_force_re_detects_a_session_the_retry_would_have_left_alone(
        self, twt_url: str
    ) -> None:
        """`make twt DATE=… FORCE=1`, which is what a threshold change needs — and it is still
        idempotent, because the write underneath it is an upsert."""
        async with rolled_back(twt_url) as session:
            panel = await build_panel(session, names=STANDARD)
            await detect_session(session, panel.as_of, user_id=panel.user_id, force=False)
            before = await _snapshot(session, panel)
            forced = await detect_session(session, panel.as_of, user_id=panel.user_id, force=True)
            assert "skipped" not in forced
            assert await _snapshot(session, panel) == before
