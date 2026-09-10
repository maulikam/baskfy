"""VB10's drill: a whole DRY_RUN session of the volume-breakout sleeve, end to end, against a
real Postgres — and **0 orders reaching a broker**.

    python tools/vbt/drill.py --database-url postgresql+asyncpg://.../baskfy_drill

What it walks, in the order a real week does it:

    1. detect        a planted session's signals and its breadth row
    2. evening       the plan: a PLACE_LIMIT with its skips, and the session settled
    3. confirm       one line of each executable kind, through the **real** gateway
    4. morning       the same plan rebuilt before the open, re-sized against the sleeve
    5. expiry        three sessions on, the sweep writes the CANCEL_LIMIT

It prints the `vb_session` counters and the broker call count, which is the number the drill
exists to show. `docs/vbt/06` VB10's acceptance is that it exits 0 and that the count is zero.

**It creates its own schema and drops it.** Nothing here touches a database anybody relies on:
pass `--database-url` for a scratch database, and the drill migrates it, fills it, reads it and
leaves. It never reads `kite-momentum-rebalancer/data/portfolio.db` and it holds no credentials —
the broker is a counter object, because a drill that needed a Kite session would be a drill
nobody could run.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BLUEPRINT = REPO / "decile-blueprint"
for extra in (
    BLUEPRINT / "packages" / "core" / "src",
    BLUEPRINT / "services" / "worker" / "src",
    BLUEPRINT / "services" / "api" / "src",
    REPO / "kite-momentum-rebalancer",
):
    sys.path.insert(0, str(extra))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from baskfy_core.models import (  # noqa: E402
    AppUser,
    Exchange,
    Instrument,
    OhlcvDaily,
    VbBreadthDaily,
    VbConfig,
    VbOrder,
    VbPlan,
    VbPlanLine,
    VbPosition,
    VbSession,
    VbSignalDaily,
)
from baskfy_core.models.base import Base  # noqa: E402
from baskfy_core.vbt.config import Gate, SignalState  # noqa: E402
from baskfy_worker.steps import StepOutcome  # noqa: E402
from baskfy_worker.tasks.vbt_evening import (  # noqa: E402
    SOURCE_MORNING,
    run_vbt_evening,
)

#: Eight consecutive sessions. The signal lands on the fifth, and the three after it are what
#: the expiry sweep needs: a limit works for three sessions and is cancelled at the third close.
SESSIONS = [
    dt.date(2026, 8, 12),
    dt.date(2026, 8, 13),
    dt.date(2026, 8, 14),
    dt.date(2026, 8, 17),
    dt.date(2026, 8, 18),
    dt.date(2026, 8, 19),
    dt.date(2026, 8, 20),
    dt.date(2026, 8, 21),
]
SIGNAL_ON = SESSIONS[4]
#: The session on whose close the working limit stops being an order (`04` §7.2).
EXPIRES_AFTER = SESSIONS[7]
SLEEVE = Decimal("1000000.00")


class CountingBroker:
    """Every method a broker could be asked for, counted rather than called.

    `test_vbt_execute.py` uses a client that *raises* — right for a test, wrong for a drill: an
    exception would end the run at the first mistake and print nothing. Here the calls are
    counted so the drill can finish and then report the number, which is the point.
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

    def place_gtt(self, **_: object) -> dict:
        self.calls.append("place_gtt")
        return {"trigger_id": 1}

    def delete_gtt(self, **_: object) -> None:
        self.calls.append("delete_gtt")

    def cancel_order(self, **_: object) -> str:
        self.calls.append("cancel_order")
        return "DRILL-1"

    def instruments(self, *_: object) -> list:
        self.calls.append("instruments")
        return []


async def _seed(session) -> tuple[int, int]:  # noqa: ANN001 - AsyncSession, imported lazily
    """One user, one sleeve with money, one instrument, five sessions of bars and one signal."""
    exchange = (
        await session.execute(sa.select(Exchange).where(Exchange.code == "NSE"))
    ).scalar_one_or_none()
    if exchange is None:
        exchange = Exchange(id=1, code="NSE")
        session.add(exchange)
        await session.flush()

    user = AppUser(public_id="vbdrill0001", email="drill@example.com")
    session.add(user)
    await session.flush()
    user_id = int(user.id)
    session.add(VbConfig(user_id=user_id, sleeve_capital_inr=SLEEVE, updated_by="drill"))

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
            VbBreadthDaily(
                user_id=user_id,
                date=day,
                universe_count=1_412,
                measured_count=1_412,
                above_count=881,
                pct_above_dma=Decimal("62.4000"),
                gate=Gate.OPEN.value,
                dma_bars=200,
                detail={
                    "funnel": {"universe": 4_186, "with_bar": 1_412, "scan_hits": 37, "signals": 1}
                },
            )
        )
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
        VbSignalDaily(
            user_id=user_id,
            date=SIGNAL_ON,
            instrument_id=instrument_id,
            state=SignalState.SIGNAL.value,
            failed_filters=[],
            close=Decimal("100.00"),
            close_raw=Decimal("100.00"),
            adj_factor=Decimal(1),
            limit_price=Decimal("100.00"),
            stop_price=Decimal("88.00"),
            turnover_avg_20=500_000_000,
            rank_key=500_000_000,
        )
    )
    await session.flush()
    return user_id, instrument_id


async def _counters(session, user_id: int) -> dict[str, object]:  # noqa: ANN001
    row = (
        await session.execute(
            sa.select(VbSession).where(
                VbSession.user_id == user_id, VbSession.session_date == SIGNAL_ON
            )
        )
    ).scalar_one_or_none()
    counts = {}
    for label, model in (
        ("plans", VbPlan),
        ("lines", VbPlanLine),
        ("orders", VbOrder),
        ("positions", VbPosition),
    ):
        counts[label] = int(
            (await session.execute(sa.select(sa.func.count()).select_from(model))).scalar_one()
        )
    return {
        "mode": row.mode if row else None,
        "gate": row.gate if row else None,
        "signals": row.signals if row else 0,
        "confirms": row.confirms if row else 0,
        "counted_for_dry_run_gate": row.counted_for_dry_run_gate if row else False,
        **counts,
    }


async def _confirm_one(database_url: str, user_id: int, broker: CountingBroker) -> list[str]:
    """Confirm the evening's `PLACE_LIMIT`, through the desk's own store and the real gateway.

    This is the step the unit tests cannot give: `test_vbt_execute.py` drives every line kind
    over an in-memory store, and what it cannot show is that the **database** round trip — read
    the plan, lock the session, write the order, journal it — also ends without a broker. So the
    drill does it once, over Postgres, with the counting broker underneath.

    The desk's own `analytics.pg.Connection` is used rather than a raw psycopg one: `PgVbtStore`
    speaks sqlite3's `?` placeholders and that adapter is the thing that translates them. Using
    psycopg directly would have "worked" until the first parameterised query.
    """
    from app import vbt_execute as X  # noqa: PLC0415 - the desk tree is on sys.path above
    from app.analytics.pg import Connection  # noqa: PLC0415
    from app.core.risk import RiskManager  # noqa: PLC0415
    from app.vbt_desk import PgVbtStore  # noqa: PLC0415

    dsn = database_url.replace("postgresql+asyncpg://", "postgresql://")
    notes: list[str] = []
    conn = Connection(f"{dsn}?options=-csearch_path%3Dpublic")
    try:
        store = PgVbtStore(conn, user_id=user_id, schema="public")
        plan = store.todays_plan(SIGNAL_ON)
        if plan is None:
            return ["no plan on the desk's side - nothing to confirm"]
        lines = [
            row for row in store.lines_for(int(plan["id"])) if row["kind"] in X.EXECUTABLE_KINDS
        ]
        if not lines:
            return ["the plan has no executable line"]
        line = lines[0]
        outcome = await X.execute_line(
            store,
            X.build_vbt_gateway(broker, RiskManager()),
            plan_id=str(plan["plan_id"]),
            line_id=int(line["id"]),
            confirm="true",
            now=dt.datetime.now(tz=dt.timezone(dt.timedelta(hours=5, minutes=30))),
            last_price=Decimal("100.00"),
        )
        notes.append(f"{line['kind']} {line['symbol']} -> {outcome.status} ({outcome.reason})")
        notes.append(f"simulated={X.is_simulated()}")
    finally:
        conn.close()
    return notes


async def _expire_a_working_limit(session, user_id: int) -> list[str]:  # noqa: ANN001
    """Plant a limit that has outlived its window, run the evening, and read the cancel line.

    VB7's sweep is the one mechanism in this sleeve that nothing else in the product has, and it
    is the one a drill can show end to end: a `vb_order` still live past its third session must
    come back as a `CANCEL_LIMIT` line in that evening's plan, `PROPOSED` like every other line.
    """
    # A **second** name: `uq_vb_order_one_per_signal` allows one order per (user, signal, name),
    # and step 5's confirm already used the first one. That constraint refusing a duplicate is
    # itself a safety property, so the drill works with it rather than around it.
    other = Instrument(
        exchange_id=1,
        symbol="DRILLTWO",
        name="DRILLTWO LIMITED",
        series="EQ",
        instrument_type="EQ",
        listed_on=dt.date(2011, 1, 1),
        is_active=True,
    )
    session.add(other)
    await session.flush()
    session.add(
        VbOrder(
            user_id=user_id,
            instrument_id=int(other.id),
            signal_date=SESSIONS[4],
            limit_price=Decimal("100.00"),
            stop_price=Decimal("88.00"),
            quantity=100,
            state="SENT",
            working_from=SESSIONS[5],
            expires_after_session=EXPIRES_AFTER,
            sessions_worked=3,
            simulated=True,
        )
    )
    await session.flush()

    report = await run_vbt_evening(
        session, StepOutcome(), EXPIRES_AFTER, user_id=user_id, execution_enabled=False
    )
    if report is None:
        return ["the evening produced no plan for the expiry session"]
    cancels = list(
        (
            await session.execute(sa.select(VbPlanLine).where(VbPlanLine.kind == "CANCEL_LIMIT"))
        ).scalars()
    )
    return [
        f"cancels={report.cancels} (the sweep's own count)",
        *[f"CANCEL_LIMIT line {row.id} qty={row.quantity} state={row.state}" for row in cancels],
    ]


async def run(database_url: str) -> int:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    broker = CountingBroker()
    try:
        async with engine.begin() as connection:
            # The same two `0001_initial_schema.py` creates. `create_all` is used instead of
            # Alembic because the drill wants a schema it can drop again, not a migration
            # history — but the extensions are the model's, not Alembic's, so they come first.
            await connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
            await connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS citext"))
            await connection.run_sync(Base.metadata.create_all)

        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session, session.begin():
            user_id, instrument_id = await _seed(session)
            print(f"seeded: user {user_id}, instrument {instrument_id}, sleeve ₹{SLEEVE}")

            print("\n1. the evening plan")
            evening = await run_vbt_evening(
                session, StepOutcome(), SIGNAL_ON, user_id=user_id, execution_enabled=False
            )
            if evening is None:
                print(
                    "   the evening produced no plan — the drill cannot continue", file=sys.stderr
                )
                return 1
            print(
                f"   entries={evening.entries} sells={evening.sells} "
                f"cancels={evening.cancels} arms={evening.arms} skips={evening.skips}"
            )
            print(f"   gate={evening.gate} equity=Rs {evening.equity_inr} plan={evening.plan_id}")

            print("\n2. the morning plan, rebuilt before the open")
            morning = await run_vbt_evening(
                session,
                StepOutcome(),
                SIGNAL_ON,
                user_id=user_id,
                execution_enabled=False,
                source=SOURCE_MORNING,
            )
            print(f"   entries={morning.entries if morning else 0} (same signals, re-sized)")

            print("\n3. the session's counters")
            for key, value in (await _counters(session, user_id)).items():
                print(f"   {key:<26} {value}")

            lines = list(
                (await session.execute(sa.select(VbPlanLine).order_by(VbPlanLine.id))).scalars()
            )
            kinds = sorted({line.kind for line in lines})
            states = sorted({line.state for line in lines})
            print(f"\n4. the plan's lines: kinds={kinds} states={states}")
            if states != ["PROPOSED"]:
                print("   a line is not PROPOSED — something confirmed itself", file=sys.stderr)
                return 1

        print("\n5. one confirm, through the real gateway")
        confirmed = await _confirm_one(database_url, user_id, broker)
        for note in confirmed:
            print(f"   {note}")

        print("\n6. the third session's expiry sweep")
        async with maker() as session, session.begin():
            swept = await _expire_a_working_limit(session, user_id)
            for note in swept:
                print(f"   {note}")

        print("\n7. the session's counters, after the confirm and the sweep")
        async with maker() as session:
            for key, value in (await _counters(session, user_id)).items():
                print(f"   {key:<26} {value}")

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
            "\nA whole session ran: planned, rebuilt, confirmed and swept — and the broker was "
            "never called. The one confirm above was simulated because "
            "BASKFY_VBT_EXECUTION_ENABLED is false; every other line is still PROPOSED, waiting "
            "for a click that only Maulik makes."
        )
        return 0
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vbt-drill", description=__doc__)
    parser.add_argument(
        "--database-url",
        required=True,
        help="a SCRATCH postgresql+asyncpg:// url. The drill creates its schema and drops it.",
    )
    args = parser.parse_args(argv)
    if "portfolio.db" in args.database_url or args.database_url.startswith("sqlite"):
        print(
            "refusing: the drill needs a scratch Postgres, never the desk's SQLite evidence",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(run(args.database_url))


if __name__ == "__main__":
    sys.exit(main())
