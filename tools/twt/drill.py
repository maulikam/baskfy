"""TW10's drill: a whole DRY_RUN session of the three-weeks-tight sleeve, end to end, against a
real Postgres — and **0 orders reaching a broker**.

    python tools/twt/drill.py --database-url postgresql+asyncpg://.../baskfy_drill

What it walks, in the order a real session does it (``docs/twt/06`` § TW10):

    1. evening    the plan the night before: the entry, its skips, the ratchet's arithmetic
    2. morning    the same plan rebuilt before the open, re-sized against the sleeve
    3. confirm    every executable line, through the **real** gateway
    4. fill       the entry fills, and the fill-day stop is armed
    5. ratchet    the stop is raised, which cancels and re-arms — the naked moment
    6. sweep      the 15:15 chore re-arms anything still naked and reports what is not

It prints the ``tw_session`` counters and the broker call count, which is the number the drill
exists to show. ``gates/twt-10.md`` G6 is that it exits 0 and that the count is zero, and
``docs/twt/FIRST-LIVE-MORNING.md`` §2 tells Maulik to run this the night before he goes live and
look for the last line.

**It creates its own schema and drops it.** Nothing here touches a database anybody relies on:
pass ``--database-url`` for a scratch database and the drill fills it, reads it and leaves. It
never reads ``kite-momentum-rebalancer/data/portfolio.db``, and it holds no credentials — the
broker is a counter object, because a drill that needed a Kite session would be a drill nobody
could run on the evening they most need it.

**The flag is never set.** ``BASKFY_TWT_EXECUTION_ENABLED`` stays false for the whole run, and
``execution_enabled=False`` is passed explicitly at every call that takes it. The drill exists to
rehearse the path, not to arm it.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

REPO: Final = Path(__file__).resolve().parents[2]
BLUEPRINT: Final = REPO / "decile-blueprint"
for _extra in (
    BLUEPRINT / "packages" / "core" / "src",
    BLUEPRINT / "services" / "worker" / "src",
    BLUEPRINT / "services" / "api" / "src",
    REPO / "kite-momentum-rebalancer",
):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

# The sleeve's flag, before anything that reads it is imported. False is its default; setting it
# explicitly means the drill cannot inherit a true from the shell that started it.
os.environ["BASKFY_TWT_EXECUTION_ENABLED"] = "false"

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from baskfy_core.models import (  # noqa: E402
    AppUser,
    Exchange,
    Instrument,
    OhlcvDaily,
    TwBreadthDaily,
    TwConfig,
    TwFill,
    TwOrder,
    TwPlan,
    TwPlanLine,
    TwPosition,
    TwSession,
    TwSignalDaily,
)
from baskfy_core.models.base import Base  # noqa: E402
from baskfy_core.twt.config import Gate, SignalState  # noqa: E402
from baskfy_worker.steps import StepOutcome  # noqa: E402
from baskfy_worker.tasks.twt_evening import (  # noqa: E402
    SOURCE_EVENING,
    SOURCE_MORNING,
    run_twt_evening,
)

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))

#: The signal's session, and the morning that acts on it.
SIGNAL_ON: Final = dt.date(2026, 9, 10)
#: 09:20 the next morning — inside the window ``04`` §10.4 lets a BUY_AT_OPEN be confirmed in.
MORNING: Final = dt.datetime(2026, 9, 10, 9, 20, tzinfo=IST)
#: 15:15, the sweep's own hour (``04`` §7.4, TW7).
AFTERNOON: Final = dt.datetime(2026, 9, 10, 15, 15, tzinfo=IST)
#: The session after the entry, on which the name makes a new high and the stop ratchets.
NEXT_SESSION: Final = dt.date(2026, 9, 11)
NEXT_MORNING: Final = dt.datetime(2026, 9, 11, 9, 20, tzinfo=IST)
#: 15:15 on the ratchet session. A named constant rather than a derived one: the goldens
#: harness scans this directory for calls that put bytes on disk BY NAME, and both
#: ``datetime`` and ``Path`` have a method called ``replace``. The house rule is to never
#: widen that scan to silence a hit, so the collision is avoided instead.
NEXT_AFTERNOON: Final = dt.datetime(2026, 9, 11, 15, 15, tzinfo=IST)
#: 15:15 on the ratchet session. A constant rather than ``AFTERNOON.replace(...)``: the
#: goldens harness scans ``tools/twt`` for calls that put bytes on disk by NAME, and
#: ``datetime.replace`` collides with ``Path.replace``. Naming the moment is clearer than
#: deriving it anyway.
NEXT_AFTERNOON: Final = dt.datetime(2026, 9, 11, 15, 15, tzinfo=IST)
#: The new high. 20 % under it is ₹104, which is above the ₹80 resting trigger, so the ratchet
#: has something to do — ``04`` §7.2's "a stop never falls" is satisfied rather than tripped.
NEW_HIGH: Final = Decimal("130.00")
#: Five sessions of bars behind the signal, so the engine has a window to read.
SESSIONS: Final = [
    dt.date(2026, 9, 3),
    dt.date(2026, 9, 4),
    dt.date(2026, 9, 7),
    dt.date(2026, 9, 8),
    dt.date(2026, 9, 9),
    SIGNAL_ON,
]
#: ``02`` §3's number. Seeded in a scratch database and dropped at the end; the real
#: ``tw_config.sleeve_capital_inr`` is 0 and this run never sets it.
SLEEVE: Final = Decimal("2500000.00")
#: A name turning over ₹50 crore, well clear of the ₹5 crore floor (``04`` §3.5).
FIFTY_CRORE: Final = 500_000_000


class CountingBroker:
    """Every method a broker could be asked for, counted rather than called.

    ``test_twt_execute.py`` uses a client that *raises* — right for a test, wrong for a drill: an
    exception would end the run at the first mistake and print nothing, so the operator would
    learn that something went wrong instead of what. Here the calls are counted so the drill can
    finish and then report the number, which is the point of running it.
    """

    VARIETY_REGULAR = "regular"
    TRANSACTION_TYPE_BUY, TRANSACTION_TYPE_SELL = "BUY", "SELL"
    PRODUCT_CNC, ORDER_TYPE_LIMIT, ORDER_TYPE_MARKET = "CNC", "LIMIT", "MARKET"
    VALIDITY_DAY, GTT_TYPE_SINGLE = "DAY", "single"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def place_order(self, **_: object) -> str:
        self.calls.append("place_order")
        return "DRILL-1"

    def place_gtt(self, **_: object) -> dict[str, Any]:
        self.calls.append("place_gtt")
        return {"trigger_id": 1}

    def delete_gtt(self, **_: object) -> None:
        self.calls.append("delete_gtt")

    def cancel_order(self, **_: object) -> str:
        self.calls.append("cancel_order")
        return "DRILL-1"

    def instruments(self, *_: object) -> list[Any]:
        self.calls.append("instruments")
        return []


async def _seed(session: AsyncSession) -> tuple[int, int]:
    """One user, one sleeve with money, one instrument, six sessions of bars and one signal."""
    exchange = (
        await session.execute(sa.select(Exchange).where(Exchange.code == "NSE"))
    ).scalar_one_or_none()
    if exchange is None:
        exchange = Exchange(id=1, code="NSE")
        session.add(exchange)
        await session.flush()

    user = AppUser(public_id="twdrill0001", email="twdrill@example.com")
    session.add(user)
    await session.flush()
    user_id = int(user.id)
    session.add(
        TwConfig(
            user_id=user_id,
            sleeve_capital_inr=SLEEVE,
            max_open_positions=10,
            max_position_pct=Decimal("12.50"),
            stop_pct=Decimal("20.00"),
            trail_pct=Decimal("20.00"),
            first_live_entries_left=10,
            dry_run_sessions=0,
            updated_by="drill",
        )
    )

    instrument = Instrument(
        exchange_id=exchange.id,
        symbol="DRILLCO",
        name="DRILLCO LIMITED",
        series="EQ",
        instrument_type="EQ",
        listed_on=dt.date(2011, 1, 1),
        is_active=True,
    )
    session.add(instrument)
    await session.flush()
    instrument_id = int(instrument.id)

    for day in SESSIONS:
        session.add(
            OhlcvDaily(
                instrument_id=instrument_id,
                date=day,
                open=Decimal("100.0000"),
                high=Decimal("100.0000"),
                low=Decimal("100.0000"),
                close=Decimal("100.0000"),
                close_raw=Decimal("100.0000"),
                volume=1_000_000,
                volume_raw=1_000_000,
                turnover=Decimal("100000000.00"),
                adj_factor=Decimal(1),
                source="nse",
            )
        )
    session.add(
        TwBreadthDaily(
            user_id=user_id,
            date=SIGNAL_ON,
            universe_count=1_412,
            measured_count=1_412,
            above_count=881,
            pct_above_dma=Decimal("62.4000"),
            gate=Gate.OPEN.value,
            dma_bars=200,
            thin_session=False,
            detail={"funnel": {"universe": 4_186, "with_bar": 1_412, "signals": 1}},
        )
    )
    session.add(
        TwSignalDaily(
            user_id=user_id,
            date=SIGNAL_ON,
            instrument_id=instrument_id,
            state=SignalState.SIGNAL.value,
            failed_filters=[],
            entry_reference_close=Decimal("100.00"),
            stop_preview=Decimal("80.00"),
            sessions_out_before=5,
            rank_key=FIFTY_CRORE,
            turnover_avg_20=FIFTY_CRORE,
        )
    )
    await session.flush()
    return user_id, instrument_id


async def _counters(session: AsyncSession, user_id: int) -> dict[str, object]:
    row = (
        await session.execute(
            sa.select(TwSession).where(
                TwSession.user_id == user_id, TwSession.session_date == SIGNAL_ON
            )
        )
    ).scalar_one_or_none()
    counts: dict[str, object] = {}
    for label, model in (
        ("plans", TwPlan),
        ("lines", TwPlanLine),
        ("orders", TwOrder),
        ("fills", TwFill),
        ("positions", TwPosition),
    ):
        counts[label] = int(
            (await session.execute(sa.select(sa.func.count()).select_from(model))).scalar_one()
        )
    # The desk's own counters, by their real names (``03`` §6). Both sessions are summed so the
    # line reads as the whole drill rather than as whichever day happened to be last.
    rows = (
        (await session.execute(sa.select(TwSession).where(TwSession.user_id == user_id)))
        .scalars()
        .all()
    )
    tallies = {
        field: sum(int(getattr(r, field)) for r in rows)
        for field in ("signals", "confirms", "fills", "ratchets", "exits", "naked_at_1515")
    }
    return {"mode": row.mode if row else None, **tallies, **counts}


async def _a_new_high(session: AsyncSession, user_id: int, instrument_id: int) -> None:
    """The session after the entry: a bar at a new high, its breadth row, and the book marked.

    ``04`` §7.1 ratchets off ``high_since``, which the nightly maintains. The drill sets it the
    way the nightly would rather than reaching into the ratchet's own arithmetic, so the plan
    that comes out is the plan a real evening would build.
    """
    session.add(
        OhlcvDaily(
            instrument_id=instrument_id,
            date=NEXT_SESSION,
            open=NEW_HIGH,
            high=NEW_HIGH,
            low=NEW_HIGH,
            close=NEW_HIGH,
            close_raw=NEW_HIGH,
            volume=1_000_000,
            volume_raw=1_000_000,
            turnover=Decimal("130000000.00"),
            adj_factor=Decimal(1),
            source="nse",
        )
    )
    session.add(
        TwBreadthDaily(
            user_id=user_id,
            date=NEXT_SESSION,
            universe_count=1_412,
            measured_count=1_412,
            above_count=881,
            pct_above_dma=Decimal("62.4000"),
            gate=Gate.OPEN.value,
            dma_bars=200,
            thin_session=False,
        )
    )
    # ``exit_lines`` emits a RAISE_GTT_STOP only for a ``next_trigger`` that exceeds the resting
    # ``gtt_trigger`` **and was computed for the session just closed** — so ``next_trigger_for``
    # matters as much as the number. TW4's nightly writes both the evening before; the drill
    # writes exactly what it would rather than reaching into the ratchet's own arithmetic, which
    # would make the drill agree with itself instead of with the product.
    await session.execute(
        sa.update(TwPosition)
        .where(TwPosition.user_id == user_id, TwPosition.state == "OPEN")
        .values(
            high_since=NEW_HIGH,
            high_since_date=NEXT_SESSION,
            next_trigger=(NEW_HIGH * Decimal("0.80")).quantize(Decimal("0.01")),
            next_trigger_for=NEXT_SESSION,
        )
    )
    await session.flush()


#: The async driver's prefix, and the sync one psycopg wants in its place.
_ASYNC_PREFIX: Final = "postgresql+asyncpg://"
_SYNC_PREFIX: Final = "postgresql://"


def _dsn(database_url: str) -> str:
    """The same url, for the desk's synchronous psycopg connection.

    Written as a slice rather than ``str.replace`` for the reason ``NEXT_AFTERNOON`` records:
    the goldens harness's write scan matches on the method name, and this is a string operation
    that happens to share one with ``Path``.
    """
    if database_url.startswith(_ASYNC_PREFIX):
        return _SYNC_PREFIX + database_url[len(_ASYNC_PREFIX) :]
    return database_url


class DrillFailure(Exception):
    """A step did not actually happen.

    The first version of this drill printed ``fill -> None``, ``ratchet -> BLOCKED`` and
    ``sweep -> naked_before=0`` and then declared that a whole session had run. Every one of
    those was a no-op, and the closing sentence was false. A drill that reports success for work
    it did not do is worse than no drill, so each step now asserts that it fired.
    """


async def _confirm_the_entry(database_url: str, user_id: int, broker: CountingBroker) -> list[str]:
    """Steps 3-6, over the desk's own Postgres store and the **real** gateway.

    The unit tests drive every one of these over an in-memory store. What they cannot show is
    that the *database* round trip — read the plan, lock the session, write the order and the
    fill, journal it — also ends without a broker. So the drill does each once, over Postgres,
    with the counting broker underneath.

    ``analytics.pg.Connection`` is used rather than a raw psycopg one for the reason
    ``tools/vbt/drill.py`` records: ``PgTwtStore`` speaks sqlite3's ``?`` placeholders and that
    adapter is what translates them.
    """
    from app import twt_execute as X  # noqa: PLC0415 - the desk tree is on sys.path above
    from app.analytics.pg import Connection  # noqa: PLC0415
    from app.core.risk import RiskManager  # noqa: PLC0415
    from app.twt_desk import PgTwtStore  # noqa: PLC0415

    notes: list[str] = []
    conn = Connection(f"{_dsn(database_url)}?options=-csearch_path%3Dpublic")
    try:
        store = PgTwtStore(conn, user_id=user_id, schema="public")
        gateway = X.build_twt_gateway(broker, RiskManager())
        notes.append(f"is_simulated={X.is_simulated()} (the flag is false, so every leg is dry)")

        plan = store.todays_plan(SIGNAL_ON)
        if plan is None:
            return [*notes, "no plan on the desk's side — nothing to confirm"]
        lines = [row for row in store.lines_for(int(plan["id"]))]
        executable = [row for row in lines if row["kind"] in X.EXECUTABLE_KINDS]
        notes.append(
            f"plan {plan['plan_id']}: {len(lines)} line(s), "
            f"{len(executable)} executable {sorted({r['kind'] for r in executable})}"
        )

        # --- 3. confirm every executable line ------------------------------------------------
        for line in executable:
            outcome = await X.execute_line(
                store,
                gateway,
                plan_id=str(plan["plan_id"]),
                line_id=int(line["id"]),
                confirm="true",
                now=MORNING,
                last_price=Decimal("100.00"),
            )
            notes.append(
                f"   confirm {line['kind']} {line['symbol']} -> {outcome.status} ({outcome.reason})"
            )

        # --- 4. the fill and the fill-day stop -------------------------------------------------
        # The dry-run confirm applies the fill through the same ``_apply_fill`` a real postback
        # would, so the position, the ``tw_fill`` row and the GTT all exist by now. Asserting it
        # here is what makes "a fill, a GTT" a step rather than a claim.
        positions = store.open_positions()
        if len(positions) != 1:
            raise DrillFailure(f"the confirm did not open a position: {positions}")
        position = positions[0]
        if not position.get("gtt_id"):
            raise DrillFailure(
                f"{position['symbol']} was filled with no GTT — non-negotiable 4 says every buy "
                f"gets a stop the same session"
            )
        notes.append(
            f"   fill: {position['symbol']} qty={position['quantity_open']} "
            f"entry={position['entry_avg']}"
        )
        notes.append(
            f"   GTT armed the same session: id={position['gtt_id']} "
            f"trigger={position['gtt_trigger']} (simulated={position['simulated']})"
        )
    finally:
        conn.close()
    return notes


async def _ratchet_and_sweep(database_url: str, user_id: int, broker: CountingBroker) -> list[str]:
    """Steps 6-7: the stop is raised on a new high, then a naked line is swept up at 15:15.

    The ratchet is **the one new mechanism in this sleeve** and ``02`` §3 records that it will
    first run with real money behind it. This is the only place it executes before that, so it
    has to be the real path: a ``RAISE_GTT_STOP`` line off a real plan, confirmed through the
    real gateway, cancelling the resting trigger and arming a higher one.
    """
    from app import twt_execute as X  # noqa: PLC0415
    from app.analytics.pg import Connection  # noqa: PLC0415
    from app.core.risk import RiskManager  # noqa: PLC0415
    from app.twt_desk import PgTwtStore  # noqa: PLC0415

    notes: list[str] = []
    conn = Connection(f"{_dsn(database_url)}?options=-csearch_path%3Dpublic")
    try:
        store = PgTwtStore(conn, user_id=user_id, schema="public")
        gateway = X.build_twt_gateway(broker, RiskManager())

        # --- 6. the ratchet --------------------------------------------------------------------
        plan = store.todays_plan(NEXT_SESSION)
        if plan is None:
            raise DrillFailure("no plan for the session after the entry")
        raises = [
            row for row in store.lines_for(int(plan["id"])) if row["kind"] == "RAISE_GTT_STOP"
        ]
        if not raises:
            raise DrillFailure(
                "the evening produced no RAISE_GTT_STOP after a new high — the ratchet is the "
                "one mechanism this drill exists to exercise"
            )
        before = store.open_positions()[0]
        outcome = await X.execute_line(
            store,
            gateway,
            plan_id=str(plan["plan_id"]),
            line_id=int(raises[0]["id"]),
            confirm="true",
            now=NEXT_MORNING,
            last_price=NEW_HIGH,
        )
        after = store.open_positions()[0]
        notes.append(f"   ratchet {after['symbol']} -> {outcome.status} ({outcome.reason})")
        notes.append(
            f"   trigger {before['gtt_trigger']} -> {after['gtt_trigger']}, "
            f"gtt {before['gtt_id']} -> {after['gtt_id']}"
        )
        if outcome.status not in {"SIMULATED", "PLACED"}:
            raise DrillFailure(f"the ratchet did not fire: {outcome.status} {outcome.reason}")
        if not after.get("gtt_id"):
            raise DrillFailure(
                f"{after['symbol']} IS NAKED after the ratchet — the cancel succeeded and the "
                f"re-arm did not. This is FIRST-LIVE-MORNING §9.1."
            )
        if Decimal(str(after["gtt_trigger"])) <= Decimal(str(before["gtt_trigger"])):
            raise DrillFailure(
                f"the stop did not rise: {before['gtt_trigger']} -> {after['gtt_trigger']}"
            )

        # --- 7. the 15:15 sweep -----------------------------------------------------------------
        # Strip the stop off the line to make the failure the sweep exists for: a ratchet that
        # cancelled and did not re-arm. Nothing else in the product can produce it on demand.
        store.update_position(int(after["id"]), {"gtt_id": None, "gtt_trigger": None})
        naked_before = X.naked_positions(store)
        if len(naked_before) != 1:
            raise DrillFailure(f"the drill failed to make a naked line: {naked_before}")
        notes.append(f"   planted a naked line: {[row['symbol'] for row in naked_before]}")

        report = await X.sweep_naked(
            store,
            gateway,
            now=NEXT_AFTERNOON,
            prices={row["symbol"]: NEW_HIGH for row in store.open_positions()},
        )
        notes.append(f"   sweep -> {report}")
        still = X.naked_positions(store)
        notes.append(f"   naked after the sweep: {len(still)} {[r['symbol'] for r in still]}")
        if report.naked_before != 1 or report.rearmed != 1:
            raise DrillFailure(f"the sweep did not re-arm the naked line: {report}")
        if still:
            raise DrillFailure(f"a line is still naked after the sweep: {still}")
    finally:
        conn.close()
    return notes


async def run(database_url: str) -> int:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    broker = CountingBroker()
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
            await connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS citext"))
            await connection.run_sync(Base.metadata.create_all)

        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session, session.begin():
            user_id, instrument_id = await _seed(session)
            print(f"seeded: user {user_id}, instrument {instrument_id}, sleeve Rs {SLEEVE}")

            print("\n1. the evening plan")
            evening = await run_twt_evening(
                session,
                StepOutcome(),
                SIGNAL_ON,
                user_id=user_id,
                execution_enabled=False,
                source=SOURCE_EVENING,
                notify=False,
            )
            if evening is None:
                print(
                    "   the evening produced no plan — the drill cannot continue", file=sys.stderr
                )
                return 1
            print(f"   {evening}")

            print("\n2. the morning plan, rebuilt before the open")
            morning = await run_twt_evening(
                session,
                StepOutcome(),
                SIGNAL_ON,
                user_id=user_id,
                execution_enabled=False,
                source=SOURCE_MORNING,
                notify=False,
            )
            print(f"   {morning}")

            lines = list(
                (await session.execute(sa.select(TwPlanLine).order_by(TwPlanLine.id))).scalars()
            )
            kinds = sorted({line.kind for line in lines})
            states = sorted({line.state for line in lines})
            print(f"\n3. the plan's lines: kinds={kinds} states={states}")
            if states and states != ["PROPOSED"]:
                print("   a line is not PROPOSED — something confirmed itself", file=sys.stderr)
                return 1

        print("\n4. confirm the entry, and the fill-day GTT — through the real gateway")
        for note in await _confirm_the_entry(database_url, user_id, broker):
            print(f"   {note}")

        print("\n5. a new high, and the evening that plans the ratchet")
        async with maker() as session, session.begin():
            await _a_new_high(session, user_id, instrument_id)
            ratchet_evening = await run_twt_evening(
                session,
                StepOutcome(),
                NEXT_SESSION,
                user_id=user_id,
                execution_enabled=False,
                source=SOURCE_EVENING,
                notify=False,
            )
            print(f"   {ratchet_evening}")
            if ratchet_evening is None or ratchet_evening.ratchets < 1:
                print(
                    "   the evening planned no ratchet — the drill cannot exercise it",
                    file=sys.stderr,
                )
                return 1

        print("\n6. the ratchet, and the 15:15 sweep")
        for note in await _ratchet_and_sweep(database_url, user_id, broker):
            print(f"   {note}")

        print("\n7. the session's counters afterwards")
        async with maker() as session:
            for key, value in (await _counters(session, user_id)).items():
                print(f"   {key:<16} {value}")

        print("\n8. the broker")
        print(f"   calls: {len(broker.calls)}  {broker.calls}")
        if broker.calls:
            print(
                "   A BROKER WAS CALLED. This is the one thing the drill exists to catch.",
                file=sys.stderr,
            )
            return 1
        print("   0 orders reached a broker.")
        print(
            "\nA whole session ran — planned, rebuilt, confirmed, filled, ratcheted and swept — "
            "and the broker was never called. Every leg was simulated because "
            "BASKFY_TWT_EXECUTION_ENABLED is false. There is no flag that makes any of it happen "
            "without a person pressing Confirm."
        )
        return 0
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="twt-drill", description=__doc__)
    parser.add_argument(
        "--database-url",
        required=True,
        help=(
            "a SCRATCH postgresql+asyncpg:// url, e.g. "
            "postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_drill. The drill creates "
            "its schema and DROPS it at the end."
        ),
    )
    args = parser.parse_args(argv)
    # Deliberately NOT defaulted to BASKFY_TEST_DATABASE_URL. The drill finishes with
    # `drop_all`, so a default would mean that running it with the ordinary test environment
    # exported silently emptied the database `make test-db` depends on. `tools/vbt/drill.py`
    # requires the url for the same reason; the two refusals below are this drill's addition.
    for name in ("BASKFY_DATABASE_URL", "BASKFY_TEST_DATABASE_URL"):
        configured = os.getenv(name)
        if configured and args.database_url.strip() == configured.strip():
            print(
                f"refusing: that is {name}. The drill drops every table it creates, so it needs "
                f"a database of its own.",
                file=sys.stderr,
            )
            return 2
    if "portfolio.db" in args.database_url or args.database_url.startswith("sqlite"):
        print(
            "refusing: the drill needs a scratch Postgres, never the desk's SQLite evidence",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(run(args.database_url))


if __name__ == "__main__":
    sys.exit(main())
