#!/usr/bin/env python
"""The swing book's DRY_RUN morning drill (SW10): one full paper session, end to end, 0 orders.

`docs/swing/06` SW10: "The DRY_RUN morning drill in `RUN-AND-TEST.md` §Swing: premarket →
replayed morning → confirm two lines → EOD → next morning's plan, end to end, **0 orders
reaching a broker**." `docs/swing/02` §3.1 makes the same drill the first condition of the
real-money gate. This script is that drill, and the Friday drill (`make friday-drill`) is its
model: it refuses to run unless `DRY_RUN=true`, it counts the orders that reached a broker, and
it refuses to call itself green over nothing.

    cd decile-blueprint
    export BASKFY_DATABASE_URL=<a disposable, migrated-or-empty Postgres>   # it RESETS the db
    export BASKFY_SOLE_USER_ID=1 DRY_RUN=true
    uv run python ../tools/swing/drill.py

WHAT IT RUNS, IN ORDER — EVERY STEP IS THE PRODUCTION CODE PATH
----------------------------------------------------------------
0. The database: `alembic upgrade head`, the pipeline tables reset, the reference rows and
   the trading calendar seeded (exactly what the worker's test conftest does), then the drill's
   own rows: one user + `sw_config` with ₹10 lakh of sleeve capital, one broker account, four
   instruments with 40 sessions of bars, the detectors' rows and the market row for the last
   close. The detectors themselves are not run — they need 125 sessions of real history — so
   their two outputs are written by hand and the report says so.
1. The evening before (`run_swing_eod`, 2026-08-18): the watchlist fills itself from the
   detectors' rows, the `EOD_PREVIEW` plan is built, the first session is counted.
2. 08:50 (`run_swing_premarket --stage LEVELS`): watched levels re-expressed under today's
   `adj_factor`.
3. 09:09 (`run_swing_premarket`, the EP flag ON for the drill with a scripted quote source —
   no Kite): the liquid universe is quoted; `EPSILONGAP` is gapping 15 % on 5x pace and is
   watched as an EP with a trigger and **no stop** (SW6.2); the `MORNING` plan shows it as a
   `PENDING_RANGE` line (STANDING-ANSWERS A7) — no quantity, no stop, a preview at a 1-ADR
   stop, one of the session's three slots reserved — beside the waiting buys.
4. 09:15–10:45: the fixture morning `tools/swing/fixtures/morning-live-gap.csv` (the four
   names of `morning-synthetic.csv` plus the live gap) is replayed through
   `app.strategies.swing_breakout.SwingBreakout` with `app.swing_monitor.PgSignalStore`
   writing `sw_signal` rows and one-line `SIGNAL` plans into the same database — the desk's
   Postgres adapter over a psycopg connection, schema `public`, exactly as the monitor process
   would. Five signals: one locked circuit (`GAMMALOCK`), one flag break (`ALPHAFLAG`), one
   below-pivot (`DELTAWAIT`), the live gap's break (`EPSILONGAP`, whose range stop is wider
   than one ADR — `STOP_TOO_WIDE`, the reserved slot released, A7) and one EP (`BETAEP`).
   `monitor_ran` is marked.
5. Two confirms, and one late partial fill (A8). `ALPHAFLAG`'s SIGNAL line is taken through
   the live shape: the line is marked `SENT` with an order id the way a real marketable LIMIT
   accepted-but-unfilled leaves it (the gateway's dry-run branch cannot produce that state —
   it fills whole — so the drill writes it and says so), and at 10:20 a partial fill of 1,000
   of 1,666 arrives through `app.swing_execute.on_order_update`, the postback handler: a
   position for 1,000, a fill row, and a GTT for exactly 1,000 through the REAL gateway's
   dry-run branch. Then `BETAEP` is confirmed through `execute_line` with a `PgSwingStore`
   and the same gateway over a broker client whose every method raises: re-sized at confirm
   (SW10.4, A5) against a book that holds 1,000 `ALPHAFLAG` at cost **plus the 666 still
   resting at the trigger**, to the ceiling's headroom. The swing journal must contain only
   dry-run events; the book after both is inside the rung's 25 % ceiling (exit 1 if not).
6. 10:45 (`cutoff_open_orders`): the order book is asked once more, `ALPHAFLAG`'s open
   remainder of 666 is cancelled through the gateway (dry-run), the line closes `FILLED` for
   the 1,000, the GTT for 1,000 is untouched, and the pending-range slots nothing claimed are
   freed (none left: `EPSILONGAP`'s was released at its trigger). The 15:15 sweep hook
   (`eod_gtt_sweep`) runs and finds no naked position.
7. The close prints: the day's bars and the detectors' rows for the session.
8. 21:05 (`run_swing_eod`): the book is managed (one position is up more than one R, so the
   rules raise its stop to breakeven), the ladder settles on real closes (A10: none yet), the
   watch funnel and the focus flags are refreshed (A14), tomorrow's preview is built, the
   session is counted (DRY_RUN, so the first-live countdown does not move — A9).
9. Next morning (`run_swing_premarket`, 2026-08-20): the `MORNING` plan carries the raised stop
   as a `RAISE_GTT_STOP` line, skips the two held names as `ALREADY_HELD`, and shows the
   still-watched live gap as `PENDING_RANGE` again.

Then the `sw_session` counters are printed — the rows `02` §3.2 counts twenty of — and
`DRILL OK`. Exit 1 with the reason on any step that does not do what the rules say.

HOW THE BROKER AND THE CLOCK ARE FAKED (DECISIONS-SW SW10.1)
--------------------------------------------------------------
The broker is `ExplodingKC`: `place_order`, `place_gtt`, `delete_gtt` and every other attribute
raise. The gateway's dry-run branch returns before any of them, so a call that reached one is
a call the flag failed to stop — it would surface as a `REJECTED` outcome and fail the drill,
and the fake counts every touch besides. The clock is passed, never read: every job takes a
`now`/`session_date`, `execute_line` takes `now`, and the monitor's strategy reads the ticks'
own timestamps — so the drill runs the same three sessions on any day of the year. The
monitor flag stays false: the strategy is driven directly, the way `tools/swing/replay.py`
drives it, with the Postgres store in place of the harness's list.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DECILE = ROOT / "decile-blueprint"
DESK = ROOT / "kite-momentum-rebalancer"
API_DIR = DECILE / "services" / "api"
FIXTURES = ROOT / "tools" / "swing" / "fixtures"
for entry in (str(DESK), str(ROOT / "tools" / "swing")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

#: The three sessions. The fixture morning is dated 2026-08-19, and that decides the rest.
D0 = dt.date(2026, 8, 18)  # the evening before
D1 = dt.date(2026, 8, 19)  # the drill morning — the fixture's day
D2 = dt.date(2026, 8, 20)  # the next morning

SLEEVE_INR = Decimal(1_000_000)
#: Not `@example.com`: the worker's test conftest sweeps `app_user` rows with that domain between
#: tests, and the drill's user owns a `broker_account` row the sweep would trip over. The drill's
#: rows are meant to outlive the run — they are the record it printed — so its user is named
#: outside the sweep and the suites' own truncations cover the rest.
DRILL_EMAIL = "swing-drill@baskfy.invalid"

#: The four names of `morning-synthetic.watchlist.json`, with what the detectors would have
#: written for them at the 18 Aug close. Triggers are the fixture's pivots, so the replay
#: reproduces the fixture's own signals; stops sit 3% below (`04` §5 sizes off them).
NAMES: tuple[tuple[str, int, str, str, str, str, bool], ...] = (
    # symbol, kite token, setup, status, trigger, stop_ref, locked at the band
    ("ALPHAFLAG", 1001, "FLAG", "SETTING_UP", "100.00", "97.00", False),
    ("BETAEP", 1002, "EP", "GAP_DAY", "205.00", "199.00", False),
    ("GAMMALOCK", 1003, "FLAG", "SETTING_UP", "50.00", "48.50", True),
    ("DELTAWAIT", 1004, "FLAG", "SETTING_UP", "300.00", "291.00", False),
)
#: The live gap (SW10.5, A7): no detection row — the detectors never saw it — but a liquid
#: name in the bar table (a 5% range on ₹8 cr a day), so the 09:09 scan quotes it. Its
#: 18 Aug close is 80; the scripted pre-open quote says 92 on 120,000 shares — a 15% gap at
#: 5x the pace of an average day at 09:09.
LIVE_GAP = ("EPSILONGAP", 1005, "80.00")
LIVE_GAP_QUOTE = ("92.00", 120_000)
#: The 19 Aug bars, as (open, high, low, close). ALPHAFLAG closes more than one R above its
#: 100.80 entry (stop 97.80, so 1R = 3.00) without printing through the stop: `04` §6.4.5
#: moves its stop to breakeven. BETAEP closes green on its gap day: rule 2 stays quiet.
CLOSE_BARS: dict[str, tuple[str, str, str, str]] = {
    "ALPHAFLAG": ("98.50", "105.20", "98.20", "104.50"),
    "BETAEP": ("210.00", "214.00", "204.60", "212.00"),
    "GAMMALOCK": ("52.50", "52.50", "52.50", "52.50"),
    "DELTAWAIT": ("298.00", "299.50", "296.00", "299.00"),
    "EPSILONGAP": ("92.00", "95.00", "88.00", "93.00"),
}
#: Sessions of bars per name: the gap scan's `vol_avg_rvol` window is 50 bars excluding
#: today, so a liquid name needs more than 51 to be quoted at all.
BARS = 60

#: The late partial fill (A8): ALPHAFLAG's 1,666-share line is accepted at 09:50 and 1,000
#: of it fills by 10:20; the remaining 666 are still open at 10:45 and cancelled.
PARTIAL_FILL = (1000, "100.85")
DRILL_ORDER_ID = "DRILL-ORD-1"

#: Journal events the gateway writes on a path that did NOT reach a broker. The drill demands
#: exactly these, in order: BETAEP's buy and stop; the late partial fill's stop for 1,000;
#: the 10:45 cancel of ALPHAFLAG's remainder.
DRY_EVENTS = ("dry_run", "gtt_dry_run", "gtt_dry_run", "order_cancel_dry_run")


class DrillFailed(RuntimeError):
    """A step did not do what the rules say. The message is the reason."""


# --- the fakes: the broker and the mail ------------------------------------------------


class ExplodingKC:
    """A broker client no drill may touch. Every attribute that could reach Zerodha raises,
    and every touch is counted so the verdict can print the number."""

    VARIETY_REGULAR = "regular"
    TRANSACTION_TYPE_BUY, TRANSACTION_TYPE_SELL = "BUY", "SELL"
    PRODUCT_CNC, ORDER_TYPE_LIMIT, ORDER_TYPE_MARKET = "CNC", "LIMIT", "MARKET"
    VALIDITY_DAY, GTT_TYPE_SINGLE = "DAY", "single"

    def __init__(self) -> None:
        self.touched = 0

    def _explode(self, what: str) -> None:
        self.touched += 1
        raise AssertionError(f"{what} reached the broker")

    def place_order(self, **_: object) -> str:
        self._explode("an order")
        return ""

    def place_gtt(self, **_: object) -> dict:
        self._explode("a GTT")
        return {}

    def delete_gtt(self, *_: object, **__: object) -> None:
        self._explode("a GTT cancel")

    def instruments(self, *_: object) -> list:
        self._explode("the instrument dump")
        return []

    def modify_gtt(self, *_: object, **__: object) -> None:
        self._explode("a GTT modify")

    def cancel_order(self, *_: object, **__: object) -> None:
        self._explode("an order cancel")

    def order_history(self, *_: object, **__: object) -> list:
        self._explode("the order book")
        return []


class DrillQuotes:
    """The 09:09 quote source — a script, not Kite. Answers the live gap's pre-open print for
    every symbol it is asked about that is the live gap, and nothing for the rest."""

    def __init__(self) -> None:
        self.requests: list[list[str]] = []

    def quotes(self, symbols):  # noqa: ANN001, ANN201 - the provider's own shape
        from baskfy_providers.records import QuoteRecord

        self.requests.append(list(symbols))
        symbol, _, prev_close = LIVE_GAP
        last, volume = LIVE_GAP_QUOTE
        if symbol not in symbols:
            return []
        return [QuoteRecord(symbol=symbol, last_price=Decimal(last), volume=volume,
                            prev_close=Decimal(prev_close), upper_circuit=Decimal("96.00"))]


class DrillOrders:
    """The order book for A8's poll and the 10:45 sweep — a script, not Kite: the drill's one
    live-shaped order is OPEN with the partial filled, until it is cancelled."""

    def __init__(self, filled: int, average: str) -> None:
        self.filled, self.average = filled, Decimal(average)
        self.asked: list[str] = []

    def order_status(self, order_id: str):  # noqa: ANN201 - an OrderReport
        from app.swing_execute import OrderReport

        self.asked.append(order_id)
        return OrderReport("OPEN", self.filled, self.average)


class RecordingTransport:
    """The evening email is built and handed here rather than to SMTP."""

    def __init__(self) -> None:
        self.subjects: list[str] = []

    async def send(self, message: object) -> None:
        self.subjects.append(str(getattr(message, "subject", "")))


# --- the report --------------------------------------------------------------------------


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)

    def step(self, label: str, text: str) -> None:
        line = f"  {label:<18} {text}"
        self.lines.append(line)
        print(line, flush=True)

    def detail(self, text: str) -> None:
        line = f"  {'':<18}   {text}"
        self.lines.append(line)
        print(line, flush=True)


# --- preflight ---------------------------------------------------------------------------


def _disposable(database: str) -> bool:
    """A database the drill may reset: named as a test, a drill, or a leaf's `_t<n>`."""
    return bool(re.search(r"(test|drill|_t\d+)$", database)) or "test" in database


def preflight(args: argparse.Namespace) -> tuple[str, int]:
    """Refuse before anything is built. The Friday drill's rule: not a warning, a refusal."""
    if os.environ.get("DRY_RUN", "true").strip().lower() == "false":
        raise DrillFailed("DRY_RUN=false in the environment; the drill only runs under DRY_RUN")
    if os.environ.get("BASKFY_SWING_EXECUTION_ENABLED", "false").strip().lower() == "true":
        raise DrillFailed("BASKFY_SWING_EXECUTION_ENABLED=true in the environment; the drill "
                          "is the proof that the flag stays false")
    url = os.environ.get("BASKFY_DATABASE_URL", "").strip()
    if not url:
        raise DrillFailed("BASKFY_DATABASE_URL is not set")
    database = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if not _disposable(database) and not args.database_is_disposable:
        raise DrillFailed(
            f"the drill RESETS the database it is pointed at, and {database!r} is not named "
            "like a disposable one; pass --database-is-disposable if it really is"
        )
    raw = os.environ.get("BASKFY_SOLE_USER_ID", "").strip() or "1"
    sole = int(raw)
    # Pinned for every module read below: the desk's `config.py` and the worker's providers
    # read these once at import, and the drill's rows must be keyed the same way.
    os.environ["DRY_RUN"] = "true"
    os.environ["BASKFY_SWING_EXECUTION_ENABLED"] = "false"
    os.environ["BASKFY_SWING_MONITOR_ENABLED"] = "false"
    os.environ["BASKFY_SOLE_USER_ID"] = str(sole)
    return url, sole


def _psycopg_dsn(url: str) -> str:
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


# --- step 0: the database ----------------------------------------------------------------


def migrate(url: str) -> str:
    """`alembic upgrade head` in the API's tree, the way the test conftests do it."""
    env = {k: v for k, v in os.environ.items() if k != "BASKFY_DATABASE_URL"}
    env["BASKFY_DATABASE_URL"] = url
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=API_DIR, env=env, capture_output=True, text=True, check=True,
    )
    head = subprocess.run(
        ["uv", "run", "alembic", "current"],
        cwd=API_DIR, env=env, capture_output=True, text=True, check=True,
    ).stdout.strip().split("\n")[-1]
    return head or "head"


#: Every table the drill writes or the pipeline seeds, reset so a re-run starts clean. The
#: worker's test conftest keeps the same list for the same reason; `sw_` tables are found by
#: name so a migration that adds one (0029's `sw_backtest_run`) is covered without an edit.
PIPELINE_TABLES = (
    "basket_snapshot, ohlcv_daily, factor_daily, index_member_daily, index_snapshot_daily, "
    "market_health_daily, corporate_action, ingest_cursor, pipeline_run_step, pipeline_run, "
    "instrument, trading_day, index_def"
)


async def reset_and_seed(url: str, sole: int, report: Report) -> int:
    """Reset, seed the reference rows, then the drill's own. Returns the broker account id."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from baskfy_api.seed import seed_reference, seed_trading_days

    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            sw_tables = [
                str(row[0]) for row in await conn.execute(text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name LIKE 'sw\\_%' ORDER BY 1"
                ))
            ]
            await conn.execute(text(
                f"TRUNCATE {', '.join(sw_tables)}, {PIPELINE_TABLES} RESTART IDENTITY CASCADE"
            ))
            # Whatever sits on the drill's id or email from an earlier run, or from a suite that
            # ran since: its broker accounts first (the FK), then the rows themselves.
            await conn.execute(text(
                "DELETE FROM broker_account WHERE user_id IN "
                "(SELECT id FROM app_user WHERE email = :email OR id = :id)"
            ), {"email": DRILL_EMAIL, "id": sole})
            await conn.execute(text(
                "DELETE FROM app_user WHERE email = :email AND id <> :id"
            ), {"email": DRILL_EMAIL, "id": sole})
            # The sole user, under the id the environment names. The database is disposable
            # (preflight refused it otherwise), so a row already sitting on that id is made the
            # drill's — identity and all — rather than borrowed: a test suite's `@example.com`
            # user with the drill's broker account attached would trip that suite's own sweep.
            await conn.execute(text(
                "INSERT INTO app_user (id, public_id, email, name) OVERRIDING SYSTEM VALUE "
                "VALUES (:id, 'swing-drill', :email, 'Swing drill') "
                "ON CONFLICT (id) DO UPDATE SET public_id = EXCLUDED.public_id, "
                "email = EXCLUDED.email, name = EXCLUDED.name, deleted_at = NULL"
            ), {"id": sole, "email": DRILL_EMAIL})
            await conn.execute(text(
                "SELECT setval(pg_get_serial_sequence('app_user', 'id'), "
                "GREATEST((SELECT max(id) FROM app_user), 1))"
            ))
            broker = (await conn.execute(text(
                "SELECT id FROM broker_account WHERE user_id = :id AND broker_id = 'zerodha'"
            ), {"id": sole})).scalar()
            if broker is None:
                broker = (await conn.execute(text(
                    "INSERT INTO broker_account (user_id, broker_id, label) "
                    "VALUES (:id, 'zerodha', 'swing-drill') RETURNING id"
                ), {"id": sole})).scalar_one()
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session, session.begin():
            await seed_reference(session)
            await seed_trading_days(session, today=dt.date(2026, 12, 31))
        async with maker() as session, session.begin():
            await seed_drill_rows(session, sole)
    finally:
        await engine.dispose()
    report.step("0. database", f"reset + seeded: user {sole}, broker account {int(broker)}, "
                f"sleeve ₹{SLEEVE_INR:,.0f}, {len(NAMES)} names + the live gap × {BARS} bars, "
                f"detectors' rows and the market row for {D0}")
    return int(broker)


async def _sessions_before(session, on: dt.date, count: int) -> list[dt.date]:
    import sqlalchemy as sa

    from baskfy_core.models import TradingDay
    from baskfy_core.seed_data import NSE_EXCHANGE_ID

    rows = await session.execute(
        sa.select(TradingDay.date)
        .where(TradingDay.exchange_id == NSE_EXCHANGE_ID, TradingDay.is_trading_day.is_(True),
               TradingDay.date <= on)
        .order_by(TradingDay.date.desc())
        .limit(count)
    )
    return sorted(row[0] for row in rows)


def _bar(instrument_id: int, on: dt.date, ohlc: tuple[str, str, str, str]):
    from baskfy_core.models import OhlcvDaily

    open_, high, low, close = (Decimal(v) for v in ohlc)
    return OhlcvDaily(
        instrument_id=instrument_id, date=on, open=open_, high=high, low=low, close=close,
        volume=1_000_000, close_raw=close, volume_raw=1_000_000, adj_factor=Decimal(1),
        source="nse",
    )


def _setup_row(user_id: int, instrument_id: int, on: dt.date, spec: tuple) -> object:
    from baskfy_core.models import SwSetupDaily

    _, _, setup, status, trigger, stop_ref, locked = spec
    return SwSetupDaily(
        user_id=user_id, date=on, instrument_id=instrument_id, setup=setup, status=status,
        score=Decimal("72.00"), close=Decimal(trigger) * Decimal("0.98"),
        trigger=Decimal(trigger), stop_ref=Decimal(stop_ref), adj_factor=Decimal(1),
        adr_pct=Decimal("5.00"), turnover_avg=100_000_000, locked_upper_circuit=locked,
        listed_within_2y=False,
    )


def _market_row(user_id: int, on: dt.date) -> object:
    from baskfy_core.models import SwMarketDaily

    return SwMarketDaily(
        user_id=user_id, date=on, constituent_count=40, pct_up_strong_1m=Decimal("8.0000"),
        gate="GREEN", exposure_level=3, max_open_positions=8, max_exposure_pct=Decimal("100.00"),
        new_entries_allowed=True, parabolic_count=0, detail={"source": "swing-drill"},
    )


async def seed_drill_rows(session, sole: int) -> None:
    """The user's sleeve, the four names with 40 sessions of flat bars, and what the detectors
    would have written for the 18 Aug close."""
    from baskfy_core.models import Instrument, SwConfig
    from baskfy_core.seed_data import NSE_EXCHANGE_ID

    session.add(SwConfig(user_id=sole, sleeve_capital_inr=SLEEVE_INR, updated_by="swing-drill"))
    dates = await _sessions_before(session, D0, BARS)
    if not dates or dates[-1] != D0:
        raise DrillFailed(f"{D0} is not a trading day in the seeded calendar")
    for spec in NAMES:
        symbol, token = spec[0], spec[1]
        instrument = Instrument(
            exchange_id=NSE_EXCHANGE_ID, symbol=symbol, name=f"{symbol} LIMITED", series="EQ",
            instrument_type="EQ", kite_token=token, listed_on=dt.date(2011, 1, 1),
            is_active=True,
        )
        session.add(instrument)
        await session.flush()
        close = Decimal(spec[4]) * Decimal("0.98")
        # A 1% range: below the 4% ADR floor, so the 09:09 scan never quotes a flag it has a
        # detection row for — the live gap is the only name it finds.
        flat = (str(close), str(close * Decimal("1.01")), str(close * Decimal("0.99")), str(close))
        for on in dates:
            session.add(_bar(instrument.id, on, flat))
        session.add(_setup_row(sole, instrument.id, D0, spec))
    symbol, token, close_text = LIVE_GAP
    gap = Instrument(
        exchange_id=NSE_EXCHANGE_ID, symbol=symbol, name=f"{symbol} LIMITED", series="EQ",
        instrument_type="EQ", kite_token=token, listed_on=dt.date(2011, 1, 1), is_active=True,
    )
    session.add(gap)
    await session.flush()
    close = Decimal(close_text)
    # A 5% range on a million shares at ₹80 — liquid by `04` §1 — and no detection row.
    wide = (str(close), str(close * Decimal("1.025")), str(close * Decimal("0.975")), str(close))
    for on in dates:
        session.add(_bar(gap.id, on, wide))
    session.add(_market_row(sole, D0))
    await session.flush()


async def instrument_ids(session) -> dict[str, int]:
    import sqlalchemy as sa

    from baskfy_core.models import Instrument

    rows = await session.execute(
        sa.select(Instrument.symbol, Instrument.id).where(
            Instrument.symbol.in_([*(spec[0] for spec in NAMES), LIVE_GAP[0]])
        )
    )
    return {str(symbol): int(instrument_id) for symbol, instrument_id in rows}


# --- the plans, as printed ---------------------------------------------------------------


async def describe_plan(session, plan_id: str | None) -> list[str]:
    import uuid

    import sqlalchemy as sa

    from baskfy_core.models import Instrument, SwPlan, SwPlanLine, SwPlanSkip

    if plan_id is None:
        return ["(no plan)"]
    plan = (await session.execute(
        sa.select(SwPlan).where(SwPlan.plan_id == uuid.UUID(plan_id))
    )).scalar_one()
    lines = (await session.execute(
        sa.select(SwPlanLine, Instrument.symbol)
        .join(Instrument, Instrument.id == SwPlanLine.instrument_id)
        .where(SwPlanLine.plan_id == plan.id).order_by(SwPlanLine.id)
    )).all()
    skips = (await session.execute(
        sa.select(SwPlanSkip).where(SwPlanSkip.plan_id == plan.id).order_by(SwPlanSkip.id)
    )).scalars().all()
    out = [f"{plan.source} {plan.plan_id} gate={plan.gate} rung={plan.exposure_level} "
           f"expires {plan.expires_at.astimezone(IST):%H:%M} IST"]
    for line, symbol in lines:
        if line.kind == "BUY_ON_TRIGGER":
            out.append(f"SWING BUY {symbol} x{line.quantity} @ {line.trigger} stop {line.stop} "
                       f"risk ₹{line.risk_inr} [{line.state}]")
        elif line.kind == "SELL_AT_OPEN":
            out.append(f"SWING SELL {symbol} x{line.quantity} at open — {line.note} [{line.state}]")
        elif line.kind == "PENDING_RANGE":
            out.append(f"SWING PENDING {symbol} — range at {line.trigger}, no stop yet; "
                       f"{line.note} [{line.state}]")
        else:
            out.append(f"SWING RAISE GTT {symbol} to {line.stop} — {line.note} [{line.state}]")
    for skip in skips:
        out.append(f"skip {skip.symbol}: {skip.reason} {skip.detail or ''}".rstrip())
    return out


# --- steps 1-3 and 6-8: the worker's jobs ---------------------------------------------------


async def run_eod(url: str, sole: int, on: dt.date, report: Report, label: str) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from baskfy_api.email.sender import Mailer
    from baskfy_worker.steps import StepOutcome, StepStatus
    from baskfy_worker.tasks.swing_eod import run_swing_eod

    transport = RecordingTransport()
    engine = create_async_engine(url)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session, session.begin():
            outcome = StepOutcome()
            result = await run_swing_eod(
                session, outcome, on, user_id=sole, execution_enabled=False,
                now=dt.datetime.combine(on, dt.time(21, 5), tzinfo=IST), mailer=Mailer(transport),
            )
            if outcome.status is not StepStatus.SUCCEEDED or result.plan_id is None:
                raise DrillFailed(f"EOD {on} did not run: {outcome.detail}")
            report.step(label, f"EOD {on}: gate {result.gate} rung {result.rung_before}→"
                               f"{result.exposure_level} (real closes: "
                               f"{result.closed_r or 'none'}), watch +{result.watch_added} "
                               f"−{result.watch_expired}, managed {result.positions_managed}, "
                               f"exits {result.exit_lines}, entries {result.entry_lines}, "
                               f"pending {result.pending_lines}, skips {result.skips}, naked "
                               f"{result.naked_positions or 'none'}, sessions logged "
                               f"{result.sessions_logged}, first live sessions left "
                               f"{result.first_live_sessions_left} (risk x"
                               f"{result.risk_multiplier}: {result.risk_pct_in_force}%)")
            for text in await describe_plan(session, result.plan_id):
                report.detail(text)
            report.detail(f"email: {transport.subjects[0] if transport.subjects else 'NOT SENT'}")
    finally:
        await engine.dispose()


async def run_premarket(url: str, sole: int, on: dt.date, stage: str, report: Report,
                        label: str, *, scan: bool = False) -> str | None:
    """The morning's job. With ``scan`` the EP flag is on for the run and the quote source is
    the drill's script (no Kite): the live gap is found and watched, and the plan shows it as
    a PENDING_RANGE line (A7)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from baskfy_worker.steps import StepOutcome, StepStatus
    from baskfy_worker.tasks.swing_premarket import STAGE_LEVELS, run_swing_premarket

    quotes = DrillQuotes() if scan else None
    engine = create_async_engine(url)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session, session.begin():
            outcome = StepOutcome()
            clock = dt.time(8, 50) if stage == STAGE_LEVELS else dt.time(9, 9)
            result = await run_swing_premarket(
                session, outcome, on, user_id=sole, stage=stage, ep_premarket_enabled=scan,
                quotes=quotes, now=dt.datetime.combine(on, clock), execution_enabled=False,
            )
            if outcome.status is not StepStatus.SUCCEEDED:
                raise DrillFailed(f"premarket {stage} {on} did not run: {outcome.detail}")
            if stage == STAGE_LEVELS:
                report.step(label, f"LEVELS {on}: refreshed {result.levels_refreshed}, "
                                   f"unchanged {result.levels_unchanged}")
                return None
            report.step(label, f"MORNING plan {on}: universe {result.universe}, quotes pulled "
                               f"{result.quotes_pulled} ({'scripted' if scan else 'flag off'}), "
                               f"gaps {result.gap_candidates or 'none'} (+{result.gaps_added}), "
                               f"entries {result.entry_lines}, pending {result.pending_lines}, "
                               f"exits {result.exit_lines}, skips {result.skips}, focus "
                               f"{result.focus}, ladder caught up: {result.ladder_caught_up}")
            for text in await describe_plan(session, result.plan_id):
                report.detail(text)
            if scan:
                if quotes is None or quotes.requests != [[LIVE_GAP[0]]]:
                    raise DrillFailed(f"the scan quoted {quotes.requests if quotes else None}, "
                                      f"not exactly the liquid universe [{LIVE_GAP[0]}]")
                if result.gaps_added != 1 or result.pending_lines != 1:
                    raise DrillFailed(f"the live gap was not watched and shown pending: "
                                      f"{result.as_detail()}")
                if result.ladder_caught_up:
                    raise DrillFailed("the 09:09 job settled a session the evening had settled")
            return result.plan_id
    finally:
        await engine.dispose()


async def the_close_prints(url: str, sole: int, report: Report) -> None:
    """Step 6: the day's bars and the detectors' rows for the session, so the evening has a
    close to manage against and tomorrow's plan knows what is locked."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(url)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session, session.begin():
            ids = await instrument_ids(session)
            for spec in NAMES:
                session.add(_bar(ids[spec[0]], D1, CLOSE_BARS[spec[0]]))
                session.add(_setup_row(sole, ids[spec[0]], D1, spec))
            session.add(_bar(ids[LIVE_GAP[0]], D1, CLOSE_BARS[LIVE_GAP[0]]))
            session.add(_market_row(sole, D1))
            await session.flush()
    finally:
        await engine.dispose()
    closes = ", ".join(f"{symbol} {bar[3]}" for symbol, bar in CLOSE_BARS.items())
    report.step("7. the close", f"bars, detectors' rows and the market row for {D1}: {closes}")


# --- step 4: the replayed morning, through the desk's store -------------------------------


@dataclass
class Morning:
    line_ids: list[int]
    raised: int
    #: The rung's ceiling the SIGNAL plans were sized under — and the confirm is held to.
    ceiling_pct: float
    equity: Decimal


def replay_morning(dsn: str, sole: int, report: Report) -> Morning:
    import replay
    from app import swing_monitor
    from app.analytics.pg import Connection
    from app.strategies.swing_breakout import SwingBreakout

    watchlist_path = FIXTURES / "morning-live-gap.watchlist.json"
    day, window, fixture_names = replay.read_watchlist(watchlist_path)
    if day != D1:
        raise DrillFailed(f"the fixture morning is dated {day}, the drill expects {D1}")
    circuits = {n.symbol: n.upper_circuit for n in fixture_names if n.upper_circuit is not None}
    candles = replay.read_candles(FIXTURES / "morning-live-gap.csv")
    ticks = replay.ticks_from_candles(candles)

    conn = Connection(dsn)
    try:
        config = swing_monitor.load_config(conn, user_id=sole)
        watchlist = swing_monitor.load_watchlist(conn, user_id=sole, circuits=circuits)
        if sorted(n.symbol for n in watchlist) != sorted(n.symbol for n in fixture_names):
            raise DrillFailed(f"the watchlist the evening built is not the fixture's: "
                              f"{[n.symbol for n in watchlist]}")
        context = swing_monitor.load_context(conn, user_id=sole, day=D1)
        store = swing_monitor.PgSignalStore(conn, user_id=sole, day=D1, config=config,
                                            context=context)
        strategy = SwingBreakout(None, watchlist=watchlist, store=store,
                                 candles=replay.CsvCandles(candles), day=D1,
                                 window_minutes=window, config=config)
        if strategy.gw is not None:
            raise DrillFailed("the strategy was handed a gateway")

        async def drive() -> None:
            for tick in ticks:
                await strategy.on_tick(tick)

        asyncio.run(drive())
        swing_monitor.record_monitor_ran(conn, user_id=sole, day=D1, signals=len(strategy.signals))
    finally:
        conn.close()

    raised = [replay.Raised.of(s) for s in strategy.signals]
    expected = [replay.Raised.from_json(e)
                for e in json.loads((FIXTURES / "morning-live-gap.expected.json").read_text())]
    report.step("4. 09:15-10:45", f"replayed morning-live-gap.csv through PgSignalStore: "
                                  f"{len(raised)} signals, {len(store.lines_written)} SIGNAL "
                                  f"lines, gate {context.gate.value} rung {context.tier.level}")
    for r in raised:
        report.detail(f"{r.at[11:16]}  {r.state:<22} {r.symbol:<10} entry={r.entry or '-':>8} "
                      f"stop={r.stop or '-':>8}")
    if raised != expected:
        raise DrillFailed(f"the replay did not raise the fixture's signals: {raised} != {expected}")
    if len(store.lines_written) != 2:
        raise DrillFailed(f"expected two TRIGGERED lines, the store wrote {store.lines_written}")
    # A7: the live gap's break set a stop wider than one ADR — a skip, and its slot released.
    conn = Connection(dsn)
    try:
        skip = conn.execute(
            "SELECT k.reason, k.detail FROM public.sw_plan_skip k JOIN public.sw_plan p "
            "ON p.id = k.plan_id WHERE k.user_id = ? AND k.symbol = ? AND p.source = 'SIGNAL' "
            "AND p.as_of = ?",
            (sole, LIVE_GAP[0], D1),
        ).fetchone()
        pending = conn.execute(
            "SELECT l.state, l.note FROM public.sw_plan_line l JOIN public.sw_plan p "
            "ON p.id = l.plan_id WHERE l.user_id = ? AND l.kind = 'PENDING_RANGE' "
            "AND p.as_of = ? AND p.source = 'MORNING'",
            (sole, D1),
        ).fetchall()
    finally:
        conn.close()
    if skip is None or (skip["reason"], skip["detail"]) != ("SIZE_REFUSED", "STOP_TOO_WIDE"):
        raise DrillFailed(f"the live gap's SIGNAL plan did not skip it STOP_TOO_WIDE: "
                          f"{dict(skip) if skip else None}")
    if len(pending) != 1 or pending[0]["state"] != "SKIPPED":
        raise DrillFailed(f"the reserved slot was not released exactly once at the trigger: "
                          f"{[dict(p) for p in pending]}")
    report.detail(f"{LIVE_GAP[0]}: SIGNAL plan skipped it SIZE_REFUSED STOP_TOO_WIDE (stop 88.00 "
                  f"under 94.60 is 6.98%, wider than its ADR); the PENDING_RANGE slot is "
                  f"released — {pending[0]['note'].split('; ')[-1]}")
    return Morning(line_ids=list(store.lines_written), raised=len(raised),
                   ceiling_pct=context.tier.max_exposure_pct, equity=context.account.equity)


# --- step 5: the two confirms, through the real execute path ------------------------------


@dataclass
class Confirmed:
    outcomes: list[object]
    events: list[str]
    touched: int


def confirm_two_lines(dsn: str, sole: int, broker_account: int, morning: Morning,
                      journal_dir: Path, report: Report) -> Confirmed:
    """Step 5 and 6: the late partial fill, the re-sized confirm, the 10:45 sweep."""
    alpha_line, beta_line = morning.line_ids
    import app.core.gateway as gateway_shim
    from app import swing_execute
    from app.analytics.pg import Connection
    from app.core.risk import RiskManager
    from app.swing_desk import PgSwingStore

    if swing_execute.swing_gates().dry_run is not True:
        raise DrillFailed("swing_gates().dry_run is not True — the flags are not what preflight "
                          "set")
    # The journal goes beside a per-run file, never the desk's real one: the drill's fills are
    # paper and the desk's record must not carry them.
    gateway_shim.JOURNAL = str(journal_dir / "orders_journal.jsonl")
    kc = ExplodingKC()
    gw = swing_execute.build_swing_gateway(kc, RiskManager())
    partial_qty, partial_price = PARTIAL_FILL
    orders = DrillOrders(partial_qty, partial_price)

    conn = Connection(dsn)
    outcomes: list[object] = []
    try:
        store = PgSwingStore(conn, user_id=sole, schema="public", broker_account_id=broker_account)
        alpha = store.line(alpha_line)
        if alpha is None or alpha["state"] != "PROPOSED" or alpha["symbol"] != "ALPHAFLAG":
            raise DrillFailed(f"line {alpha_line} is not ALPHAFLAG's PROPOSED line: {alpha}")
        planned_alpha = int(alpha["quantity"])

        # --- 5a. ALPHAFLAG goes out as a live marketable LIMIT would (A8) --------------------
        # A live marketable LIMIT is accepted, not filled; `execute_line` leaves such a line
        # SENT with the broker's order id (SW7.1). The gateway's dry-run branch cannot produce
        # that state — it fills whole — so the drill writes the SENT line the way the live
        # path would have at 09:50. The fill arrives later the way a live fill does (5c).
        with store.lock_session_for_update(D1):
            store.set_line(alpha_line, state="SENT", journal_ref=DRILL_ORDER_ID)
            store.bump_session(D1, mode="DRY_RUN", confirms=1)
        limit = swing_execute.marketable_limit(
            trigger=alpha["trigger"], range_high=store.range_high_for(alpha_line),
            adr_pct=Decimal("5.00"), config=swing_execute.DEFAULT_SWING_CONFIG.opening_range,
        )
        report.detail(f"09:50 SWING BUY ALPHAFLAG x{planned_alpha} @ {alpha['trigger']} stop "
                      f"{alpha['stop']} → SENT as a live marketable LIMIT would be (limit "
                      f"{limit} = min(100.80 x 1.005, range high 100.50 + 0.25 x 5% ADR)), "
                      f"order {DRILL_ORDER_ID}, nothing filled yet; the drill wrote this state "
                      f"— the dry-run branch cannot")

        # --- 5b. BETAEP's confirm, re-sized against ALPHAFLAG's resting order --------------
        beta = store.line(beta_line)
        if beta is None or beta["state"] != "PROPOSED":
            raise DrillFailed(f"line {beta_line} is not a PROPOSED line: {beta}")
        planned_beta = int(beta["quantity"])
        now = dt.datetime.combine(D1, dt.time(9, 55), tzinfo=IST)
        outcome = asyncio.run(swing_execute.execute_line(
            store, gw, plan_id=str(beta["plan_id"]), line_id=beta_line, confirm="true", now=now,
            orders=orders,
        ))
        outcomes.append(outcome)
        beta = store.line(beta_line) or beta
        beta_pos = store.position(outcome.position_id) if outcome.position_id else None
        sent = int(beta["quantity"])
        report.detail(f"09:55 SWING BUY {beta['symbol']} x{planned_beta} @ {beta['trigger']} "
                      f"stop {beta['stop']} → {outcome.status} {outcome.reason or ''}".rstrip()
                      + (f"; position {beta_pos['id']} x{beta_pos['quantity_open']} "
                         f"gtt {beta_pos['gtt_id']} simulated={beta_pos['simulated']}"
                         if beta_pos else ""))
        resized = 0
        if sent != planned_beta:
            resized += 1
            report.detail(f"  re-sized at confirm {planned_beta} → {sent} (A5): {beta['note']}")
        if outcome.status != "SIMULATED" or not outcome.simulated or beta_pos is None:
            raise DrillFailed(f"line {beta_line} did not simulate end to end: {outcome}")
        if not str(beta_pos["gtt_id"]).startswith("DRY-") or beta_pos["simulated"] is not True:
            raise DrillFailed(f"position {beta_pos['id']} is not a paper position: {beta_pos}")
        if int(beta_pos["quantity_open"]) != sent or sent > planned_beta:
            raise DrillFailed(f"position {beta_pos['id']} holds {beta_pos['quantity_open']}, "
                              f"the line says {sent} (planned {planned_beta})")
        if beta["state"] != "FILLED" or beta["position_id"] != beta_pos["id"]:
            raise DrillFailed(f"line {beta_line} is {beta['state']} after a simulated fill")
        if orders.asked:
            raise DrillFailed("a dry-run confirm polled the order book; the dry-run path "
                              "completes immediately")
        if resized != 1:
            raise DrillFailed(f"{resized} lines were re-sized at confirm; the fixture expects one")

        # --- 5c. the late partial fill (A8): the postback handler ----------------------------
        at_1020 = dt.datetime.combine(D1, dt.time(10, 20), tzinfo=IST)
        late = asyncio.run(swing_execute.on_order_update(
            store, gw, {"order_id": DRILL_ORDER_ID, "status": "OPEN",
                        "filled_quantity": partial_qty, "average_price": partial_price},
            now=at_1020,
        ))
        if late is None or late.position_id is None or late.filled_quantity != partial_qty:
            raise DrillFailed(f"the late partial fill was not applied: {late}")
        outcomes.append(late)
        position = store.position(late.position_id)
        alpha = store.line(alpha_line) or alpha
        report.detail(f"10:20 postback: {partial_qty} of {planned_alpha} ALPHAFLAG filled at "
                      f"{partial_price} → position {position['id']} x{position['quantity_open']} "
                      f"gtt {position['gtt_id']} (for exactly {position['quantity_open']}, the "
                      f"real gateway's dry-run branch), line {alpha['state']} — the remaining "
                      f"{planned_alpha - partial_qty} still resting")
        if position is None or int(position["quantity_open"]) != partial_qty:
            raise DrillFailed(f"the position does not hold the filled quantity: {position}")
        if not str(position["gtt_id"]).startswith("DRY-"):
            raise DrillFailed(f"the partial's stop is not a dry-run GTT: {position}")
        if alpha["state"] != "SENT" or alpha["position_id"] != position["id"]:
            raise DrillFailed(f"ALPHAFLAG's line is {alpha['state']} with position "
                              f"{alpha['position_id']} after a partial fill")
        # Idempotent: the same postback again writes nothing.
        again = asyncio.run(swing_execute.on_order_update(
            store, gw, {"order_id": DRILL_ORDER_ID, "status": "OPEN",
                        "filled_quantity": partial_qty, "average_price": partial_price},
            now=at_1020,
        ))
        if again is None or len(store.fills_for(int(position["id"]))) != 1:
            raise DrillFailed("a repeated postback wrote a second fill")
        context = store.session_context(D1)
        book_now = context.account.open_exposure_inr
        alpha_cost = Decimal(partial_price) * partial_qty
        remainder = Decimal(str(alpha["trigger"])) * (planned_alpha - partial_qty)
        report.detail(f"EXPOSURE after confirms ₹{book_now:,.2f} = "
                      f"{book_now / morning.equity * 100:.1f}% of the sleeve — ALPHAFLAG "
                      f"₹{alpha_cost:,.2f} filled + ₹{remainder:,.2f} still resting at the "
                      f"trigger + BETAEP ₹{beta_pos['entry_avg'] * beta_pos['quantity_open']:,.2f} "
                      f"(rung ceiling {morning.ceiling_pct:.0f}% = "
                      f"₹{morning.equity * Decimal(str(morning.ceiling_pct)) / 100:,.2f}); "
                      f"{context.entries_today} entries today, "
                      f"{len(context.account.open_symbols)} of "
                      f"{context.tier.max_open_positions} positions at rung {context.tier.level}")
        ceiling = morning.equity * Decimal(str(morning.ceiling_pct)) / 100
        if book_now > ceiling:
            raise DrillFailed(f"the book after the confirms is ₹{book_now:,.2f}, over the rung's "
                              f"{morning.ceiling_pct:.0f}% ceiling (₹{ceiling:,.2f}) — the "
                              f"confirm-time gate did not hold")
        if context.entries_today != 2:
            raise DrillFailed(f"the session counts {context.entries_today} entries, not 2")

        # --- 6. 10:45: cancel the open remainder, free the unclaimed slots --------------------
        at_1045 = dt.datetime.combine(D1, dt.time(10, 45), tzinfo=IST)
        sweep = asyncio.run(swing_execute.cutoff_open_orders(store, gw, orders=orders, now=at_1045))
        alpha = store.line(alpha_line) or alpha
        position = store.position(int(position["id"])) or position
        report.step("6. 10:45 sweep", f"reconciled {sweep.reconciled}, cancelled "
                                      f"{sweep.cancelled}, refused {sweep.cancel_failed}, "
                                      f"slots freed {sweep.slots_freed}")
        report.detail(f"ALPHAFLAG order {DRILL_ORDER_ID}: remainder "
                      f"{planned_alpha - partial_qty} cancelled through the gateway (dry-run), "
                      f"line {alpha['state']}, position x{position['quantity_open']} gtt "
                      f"{position['gtt_id']} — {'; '.join(alpha['note'].split('; ')[-2:])}")
        if sweep.cancelled != 1 or sweep.cancel_failed != 0 or sweep.slots_freed != 0:
            raise DrillFailed(f"the sweep did not cancel exactly the one remainder: {sweep}")
        if alpha["state"] != "FILLED" or int(position["quantity_open"]) != partial_qty:
            raise DrillFailed(f"after the cutoff ALPHAFLAG is {alpha['state']} with "
                              f"{position['quantity_open']} open")
        if len(store.fills_for(int(position["id"]))) != 1:
            raise DrillFailed("the cutoff wrote a fill")
        naked = asyncio.run(swing_execute.eod_gtt_sweep(store, gw, now=at_1045.replace(hour=15,
                                                                                     minute=15)))
        report.detail(f"15:15 sweep hook: {len(naked)} naked position(s) re-armed")
        if naked:
            raise DrillFailed(f"a position was naked at 15:15: {naked}")
        session = store.session(D1)
    finally:
        conn.close()

    journal = Path(gw._journal_path)
    events = [json.loads(row)["event"] for row in journal.read_text().splitlines()] \
        if journal.exists() else []
    if events != list(DRY_EVENTS):
        raise DrillFailed(f"the swing journal is not the partial's stop, BETAEP's buy and stop "
                          f"and the 10:45 cancel, all dry: {events}")
    if kc.touched:
        raise DrillFailed(f"the broker client was touched {kc.touched} times")
    if session is None or session["confirms"] != 2 or session["fills"] != 2:
        raise DrillFailed(f"sw_session {D1} did not count two confirms and two fills: {session}")
    report.detail(f"swing journal ({journal.name}): {', '.join(events)}")
    report.detail(f"broker client touched: {kc.touched}")
    return Confirmed(outcomes=outcomes, events=events, touched=kc.touched)


# --- the counters ------------------------------------------------------------------------


async def session_counters(url: str, sole: int) -> list[str]:
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from baskfy_core.models import SwSession

    engine = create_async_engine(url)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            rows = (await session.execute(
                sa.select(SwSession).where(SwSession.user_id == sole)
                .order_by(SwSession.session_date)
            )).scalars().all()
    finally:
        await engine.dispose()
    return [
        f"sw_session {row.session_date}: mode={row.mode} monitor_ran={row.monitor_ran} "
        f"signals={row.signals} confirms={row.confirms} fills={row.fills} "
        f"manage_actions={row.manage_actions} plans={len((row.plan_ids or {}).get('plans', []))}"
        for row in rows
    ]


async def every_sw_row_is_the_soles(url: str, sole: int) -> int:
    """Track C §6, checked on the way out: not one `sw_` row for anyone else."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            tables = [str(r[0]) for r in await conn.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
                "AND table_name LIKE 'sw\\_%'"
            ))]
            total = 0
            for table in tables:
                foreign = (await conn.execute(text(
                    f"SELECT count(*) FROM {table} WHERE user_id <> :sole"  # noqa: S608
                ), {"sole": sole})).scalar_one()
                if foreign:
                    raise DrillFailed(f"{table} holds {foreign} rows that are not user {sole}'s")
                total += int((await conn.execute(text(
                    f"SELECT count(*) FROM {table}"  # noqa: S608
                ))).scalar_one())
    finally:
        await engine.dispose()
    return total


# --- main --------------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    url, sole = preflight(args)
    dsn = _psycopg_dsn(url)
    report = Report()
    print(f"Swing DRY_RUN drill — {D0} evening → {D1} morning → {D2} plan   "
          "DRY_RUN=true   BASKFY_SWING_EXECUTION_ENABLED=false   "
          "BASKFY_SWING_MONITOR_ENABLED=false")
    print(f"  database          {url.rsplit('/', 1)[-1]}   sole user {sole}")

    head = migrate(url)
    report.step("0. migrate", f"alembic upgrade head → {head}")
    broker_account = asyncio.run(reset_and_seed(url, sole, report))

    asyncio.run(run_eod(url, sole, D0, report, "1. evening before"))
    asyncio.run(run_premarket(url, sole, D1, "LEVELS", report, "2. 08:50 LEVELS"))
    morning_plan = asyncio.run(run_premarket(url, sole, D1, "GAPS", report, "3. 09:09 MORNING",
                                             scan=True))
    if morning_plan is None:
        raise DrillFailed("no MORNING plan was built")
    morning = replay_morning(dsn, sole, report)

    report.step("5. confirm", "ALPHAFLAG as a live marketable LIMIT with a late partial fill "
                              "through on_order_update (A8), then BETAEP through execute_line "
                              "+ the real gateway (dry-run) over an exploding broker client, "
                              "under the session lock and re-sized to the rung (A5)")
    with tempfile.TemporaryDirectory(prefix="swing-drill-") as journal_dir:
        confirmed = confirm_two_lines(dsn, sole, broker_account, morning, Path(journal_dir),
                                      report)

    asyncio.run(the_close_prints(url, sole, report))
    asyncio.run(run_eod(url, sole, D1, report, "8. 21:05 EOD"))
    asyncio.run(run_premarket(url, sole, D2, "LEVELS", report, "9. 08:50 LEVELS"))
    next_plan = asyncio.run(run_premarket(url, sole, D2, "GAPS", report, "9. 09:09 MORNING"))
    if next_plan is None:
        raise DrillFailed("no MORNING plan was built for the next session")

    rows = asyncio.run(every_sw_row_is_the_soles(url, sole))
    counters = asyncio.run(session_counters(url, sole))
    print(f"  every sw_ row     {rows} rows across the sw_ tables, all user {sole}'s")
    for line in counters:
        print(f"  {line}")
    print(f"  orders that reached a broker: {confirmed.touched}   "
          f"(journal: {', '.join(confirmed.events)})")
    print(f"  late partial fill: {PARTIAL_FILL[0]} of 1666 ALPHAFLAG at {PARTIAL_FILL[1]} by "
          f"postback, the rest cancelled at 10:45; one GTT, for exactly {PARTIAL_FILL[0]}")
    print("DRILL OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-is-disposable", action="store_true",
                        help="reset a database whose name does not say it is a test/drill one")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except DrillFailed as exc:
        print(f"DRILL FAILED: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"DRILL FAILED: {exc.cmd} exited {exc.returncode}\n{exc.stderr}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
