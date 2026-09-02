"""The opening-range monitor's runner: watchlist in, signals out, never an order (SW6).

`docs/swing/06` SW6: "Desk process `app/strategies/swing_breakout.py` implementing
`BaseStrategy`, flag `BASKFY_SWING_MONITOR_ENABLED`: subscribe the watchlist's tokens on the
`TickBus`; at window close build the range from `historical_data(interval="minute")` (fallback:
the ticks' own high/low), then `evaluate_trigger` on every tick; `TRIGGERED` → `sw_signal` row +
a one-line `sw_plan(source=SIGNAL)` + desk notification. `generate_targets` returns `[]` — this
strategy **never** places. Stops at 10:45."

This module is everything around the strategy that touches the world: the flag, the database,
the broker's candles and the websocket. The strategy itself (`app/strategies/swing_breakout.py`)
touches none of them, which is what lets `tools/swing/replay.py` drive it with a CSV.

THE FLAG
--------
`build_monitor` returns ``None`` when `BASKFY_SWING_MONITOR_ENABLED` is false and the strategy
class is not constructed at all — not "constructed and idle", not "constructed and muted".
`tests/test_swing_monitor.py` asserts that, because a process that exists is a process that
can be pointed at something.

THE STORE
---------
`PgSignalStore` writes the screener's `sw_` tables (schema `public`) through the desk's own
Postgres adapter, qualified by schema because the desk's connection sits on `search_path=desk`.
A `TRIGGERED` verdict becomes one `sw_signal` row and one `sw_plan` of `source=SIGNAL` with at
most one line, sized by the same pure `build_entries` the evening uses — so the gate, the tier,
the sleeve's cash and the "never averaged down" rule apply to a live trigger exactly as they
apply to a planned one. A `LOCKED_UPPER_CIRCUIT` or `BELOW_PIVOT` verdict is a signal row and
nothing else: kept for the record (`03` §5), never a line.

The context a SIGNAL plan is sized against is **re-read per trigger** (SW10, STANDING-ANSWERS
A5): the 09:45 trigger is sized against a book that already holds what was confirmed at 09:31
— `load_context` counts the open positions at cost, every BUY line confirmed today that is not
yet a position, and the entries the session has already taken — so the line the page shows is
the line `/swing/execute` will send, and the confirm-time gate (`app.swing_execute`) re-derives
the same context under a row lock and answers the same way.

THE NOTIFICATION
----------------
The desk has no push channel. The notification is the row the desk page (SW7) polls plus a log
line at INFO; `docs/swing/DECISIONS-SW.md` SW6.3 says why and how to add a channel later.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from baskfy_core.models.swing import PLAN_TTL_MINUTES
from baskfy_core.swing.config import (
    DEFAULT_SWING_CONFIG,
    LiquidityConfig,
    Setup,
    SizingConfig,
    SwingConfig,
)
from baskfy_core.swing.market import ExposureTier, MarketGate
from baskfy_core.swing.opening_range import Candle, TriggerState
from baskfy_core.swing.plan import (
    PlanLine,
    Skipped,
    SkipReason,
    SwingAccount,
    WatchItem,
    assemble,
    build_entries,
)

from . import config as C
from .strategies.swing_breakout import Signal, SwingBreakout, WatchedName

log = logging.getLogger("swing_monitor")

#: The screener's tables live in `public`; the desk's connection is on `search_path=desk`.
SCHEMA = "public"


# --- reading the morning -----------------------------------------------------------------


def _t(schema: str, table: str) -> str:
    """`schema.table`, or the bare name when the schema is empty (the desk tests' sqlite twin)."""
    return f"{schema}.{table}" if schema else table


def load_config(conn: Any, *, user_id: int, schema: str = SCHEMA) -> SwingConfig:
    """`DEFAULT_SWING_CONFIG` with this user's settings — the worker's rule (`load_swing_config`,
    SW9.5.3): the three liquidity floors and the three sizing knobs `03` §1 calls the person's
    own. The monitor sized with the pack's risk knobs until SW10; a SIGNAL line is a preview of
    what the confirm will send, and the confirm sizes with the person's numbers."""
    row = conn.execute(
        f"SELECT adr_min_pct, turnover_min_inr, price_min, risk_per_trade_pct, "
        f"max_position_pct, max_open_positions FROM {_t(schema, 'sw_config')} "
        "WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return DEFAULT_SWING_CONFIG
    return replace(
        DEFAULT_SWING_CONFIG,
        liquidity=replace(
            LiquidityConfig(),
            adr_min_pct=float(row["adr_min_pct"]),
            turnover_min_inr=float(row["turnover_min_inr"]),
            price_min=float(row["price_min"]),
        ),
        sizing=replace(
            SizingConfig(),
            risk_per_trade_pct=float(row["risk_per_trade_pct"]),
            max_position_pct=float(row["max_position_pct"]),
            max_open_positions=int(row["max_open_positions"]),
        ),
    )


def load_watchlist(
    conn: Any, *, user_id: int, circuits: dict[str, Decimal] | None = None
) -> list[WatchedName]:
    """Every `WATCHING` row with a Kite token, as the strategy wants it.

    A FLAG's pivot is its trigger (`04` §2.6: `trigger = pivot_high` while setting up); an EP's
    pivot is the gap itself, so `pivot_high` is None. A name with no token cannot be subscribed
    and is skipped with a warning rather than silently — it is on the list because somebody
    wanted it watched. The circuit bands come from the morning's quote, keyed by symbol.
    """
    rows = conn.execute(
        f"SELECT w.id, w.instrument_id, i.symbol, i.kite_token, w.setup, w.trigger "
        f"FROM {SCHEMA}.sw_watch w JOIN {SCHEMA}.instrument i ON i.id = w.instrument_id "
        "WHERE w.user_id = ? AND w.state = 'WATCHING' ORDER BY i.symbol",
        (user_id,),
    ).fetchall()
    bands = circuits or {}
    names: list[WatchedName] = []
    for row in rows:
        token = row["kite_token"]
        if token is None:
            log.warning("%s is watched but has no Kite token; not subscribed", row["symbol"])
            continue
        setup = Setup(row["setup"])
        if setup not in (Setup.FLAG, Setup.EP):
            continue
        trigger = row["trigger"]
        names.append(
            WatchedName(
                watch_id=int(row["id"]),
                instrument_id=int(row["instrument_id"]),
                symbol=str(row["symbol"]),
                token=int(token),
                setup=setup,
                pivot_high=(
                    Decimal(str(trigger)) if setup is Setup.FLAG and trigger is not None else None
                ),
                upper_circuit=bands.get(str(row["symbol"])),
            )
        )
    return names


def circuit_bands(kite: Any, symbols: list[str]) -> dict[str, Decimal]:
    """The day's upper circuit per symbol, from one quote pass. Read-only."""
    out: dict[str, Decimal] = {}
    if not symbols:
        return out
    for i in range(0, len(symbols), 400):
        try:
            data = kite.kc.quote([f"NSE:{s}" for s in symbols[i : i + 400]])
        except Exception as exc:  # noqa: BLE001 - a missing band is a missing band, not a halt
            log.warning("circuit quote failed: %s", exc)
            continue
        for key, row in data.items():
            band = row.get("upper_circuit_limit")
            if band:
                out[key.split(":", 1)[1]] = Decimal(str(band))
    return out


# --- writing what the monitor raised -------------------------------------------------------


#: `sw_plan_line.state` values that mean the session has taken (or is taking) the entry: the
#: order went out, or is going out, or filled. A `PROPOSED` line is an offer; a `REJECTED`,
#: `EXPIRED` or `SKIPPED` one never was an entry.
ENTRY_TAKEN_STATES: tuple[str, ...] = ("CONFIRMED", "SENT", "FILLED")


@dataclass(frozen=True)
class PendingLine:
    """A BUY line confirmed today that is not (yet) a position: a live order accepted and not
    filled (`SENT`, SW7.1), or a confirm in flight (`CONFIRMED`). It is counted as held —
    exposure at the trigger, a symbol on the book — because the shares may arrive any moment."""

    line_id: int
    symbol: str
    quantity: int
    value_inr: Decimal
    state: str


@dataclass(frozen=True)
class SignalContext:
    """What a SIGNAL plan — and a confirm — is sized against. Re-read per trigger (A5).

    `account` counts the book at cost plus every `PendingLine`; `entries_today` is how many BUY
    lines the session has already confirmed, sent or filled — they count against the
    per-session cap (`04` §5.3) whatever plan they came from.
    """

    gate: MarketGate
    tier: ExposureTier
    account: SwingAccount
    #: symbol → (adr_pct, avg_turnover_inr, score) from the latest detection row.
    detected: dict[str, tuple[Decimal, Decimal | None, Decimal]]
    entries_today: int = 0
    pending: tuple[PendingLine, ...] = ()


def load_context(conn: Any, *, user_id: int, day: dt.date, schema: str = SCHEMA) -> SignalContext:
    """The last close's gate and rung, the sleeve's money as of *now*, each watched name's
    stats, and what the session has already entered.

    The market row is the latest strictly before `day` — the close the plan was built on; a
    row for `day` itself is written after the close and never exists during the session.
    Exposure is every OPEN/PARTIAL position at cost plus every BUY line confirmed today that
    is not a position yet; cash is the sleeve's capital less that. Three cheap queries, read
    per trigger and per confirm, so two plans of one morning see each other's confirms
    (DECISIONS-SW SW10.2, fixed by SW10.4).
    """
    market = conn.execute(
        f"SELECT gate, exposure_level, max_open_positions, max_exposure_pct, "
        f"new_entries_allowed, drawdown_locked FROM {_t(schema, 'sw_market_daily')} "
        "WHERE user_id = ? AND date < ? ORDER BY date DESC LIMIT 1",
        (user_id, day.isoformat()),
    ).fetchone()
    if market is None:
        gate = MarketGate.RED
        tier = ExposureTier(
            level=0, max_open_positions=0, max_exposure_pct=0.0, new_entries_allowed=False
        )
    else:
        gate = MarketGate(market["gate"])
        tier = ExposureTier(
            level=int(market["exposure_level"]),
            max_open_positions=int(market["max_open_positions"]),
            max_exposure_pct=float(market["max_exposure_pct"]),
            new_entries_allowed=bool(market["new_entries_allowed"]),
            drawdown_locked=bool(market["drawdown_locked"]),
        )
    capital_row = conn.execute(
        f"SELECT sleeve_capital_inr FROM {_t(schema, 'sw_config')} WHERE user_id = ?", (user_id,)
    ).fetchone()
    capital = Decimal(str(capital_row["sleeve_capital_inr"])) if capital_row else Decimal(0)
    held = conn.execute(
        f"SELECT i.symbol, p.entry_avg, p.quantity_open FROM {_t(schema, 'sw_position')} p "
        f"JOIN {_t(schema, 'instrument')} i ON i.id = p.instrument_id "
        "WHERE p.user_id = ? AND p.state IN ('OPEN', 'PARTIAL') AND p.quantity_open > 0",
        (user_id,),
    ).fetchall()
    exposure = sum(
        (Decimal(str(r["entry_avg"])) * Decimal(int(r["quantity_open"])) for r in held), Decimal(0)
    )
    open_symbols = {str(r["symbol"]) for r in held}
    taken = conn.execute(
        f"SELECT l.id, i.symbol, l.quantity, l.trigger, l.state, l.position_id "
        f"FROM {_t(schema, 'sw_plan_line')} l "
        f"JOIN {_t(schema, 'sw_plan')} p ON p.id = l.plan_id "
        f"JOIN {_t(schema, 'instrument')} i ON i.id = l.instrument_id "
        "WHERE l.user_id = ? AND p.as_of = ? AND l.kind = 'BUY_ON_TRIGGER' "
        f"AND l.state IN ({', '.join('?' for _ in ENTRY_TAKEN_STATES)}) ORDER BY l.id",
        (user_id, day.isoformat(), *ENTRY_TAKEN_STATES),
    ).fetchall()
    pending: list[PendingLine] = []
    for r in taken:
        if r["position_id"] is not None or str(r["symbol"]) in open_symbols:
            continue  # already on the book as a position: counted at cost above
        value = Decimal(str(r["trigger"] or 0)) * Decimal(int(r["quantity"]))
        pending.append(
            PendingLine(
                line_id=int(r["id"]),
                symbol=str(r["symbol"]),
                quantity=int(r["quantity"]),
                value_inr=value,
                state=str(r["state"]),
            )
        )
        exposure += value
        open_symbols.add(str(r["symbol"]))
    account = SwingAccount(
        equity=capital,
        cash_available=max(capital - exposure, Decimal(0)),
        open_symbols=frozenset(open_symbols),
        open_exposure_inr=exposure,
    )
    # The latest detection row per name before `day`; ties on a date go to the higher score.
    # Portable (no DISTINCT ON): the desk's tests run this SQL on sqlite.
    stats = conn.execute(
        f"SELECT i.symbol, s.adr_pct, s.turnover_avg, s.score "
        f"FROM {_t(schema, 'sw_setup_daily')} s "
        f"JOIN {_t(schema, 'instrument')} i ON i.id = s.instrument_id "
        "WHERE s.user_id = ? AND s.date < ? AND s.date = ("
        f"SELECT max(x.date) FROM {_t(schema, 'sw_setup_daily')} x "
        "WHERE x.user_id = s.user_id AND x.instrument_id = s.instrument_id AND x.date < ?) "
        "ORDER BY s.instrument_id, s.score DESC",
        (user_id, day.isoformat(), day.isoformat()),
    ).fetchall()
    detected: dict[str, tuple[Decimal, Decimal | None, Decimal]] = {}
    for r in stats:
        symbol = str(r["symbol"])
        if symbol in detected:
            continue
        detected[symbol] = (
            Decimal(str(r["adr_pct"])) if r["adr_pct"] is not None else Decimal(0),
            Decimal(int(r["turnover_avg"])) if r["turnover_avg"] is not None else None,
            Decimal(str(r["score"])) if r["score"] is not None else Decimal(0),
        )
    return SignalContext(
        gate=gate,
        tier=tier,
        account=account,
        detected=detected,
        entries_today=len(taken),
        pending=tuple(pending),
    )


def entries_now(
    item: WatchItem, context: SignalContext, config: SwingConfig, *, day: dt.date
) -> tuple[list[PlanLine], list[Skipped]]:
    """One triggered name against the book as it is now — the SIGNAL plan's size and the
    confirm's, from one function (SW10.4 / SW10.5).

    `build_entries` (`04` §9.1) answers the gate, the drawdown lock, `ALREADY_HELD`, the
    per-session cap (today's entries count, `entries_already_today`), the position count, the
    size and the rung's exposure ceiling. When the ceiling is the *only* thing in the way the
    name is tried once more with the cash bounded by the ceiling's headroom — A5's "re-size or
    refuse": a live trigger that does not fit whole is taken at the size that fits, if the
    rules still line it (below the minimum trade value is `SIZE_REFUSED`, so a sliver never
    goes out). The evening and the morning plans do not re-size — a plan of many names skips
    the one that does not fit (`04` §9.1); this is the rule for one name at the moment of its
    trigger, and the page shows exactly what the confirm will send.
    """
    lines, skipped = build_entries(
        as_of=day, watch=[item], account=context.account, gate=context.gate, tier=context.tier,
        config=config, entries_already_today=context.entries_today,
    )
    if lines or not skipped or skipped[0].reason is not SkipReason.EXPOSURE_FULL:
        return lines, skipped
    account = context.account
    ceiling = account.equity * Decimal(str(context.tier.max_exposure_pct)) / Decimal(100)
    headroom = max(ceiling - account.open_exposure_inr, Decimal(0))
    lines, retry = build_entries(
        as_of=day, watch=[item],
        account=replace(account, cash_available=min(account.cash_available, headroom)),
        gate=context.gate, tier=context.tier, config=config,
        entries_already_today=context.entries_today,
    )
    if lines:
        return lines, []
    why = f"{retry[0].reason.value} {retry[0].detail}".strip() if retry else "nothing"
    return [], [
        Skipped(
            item.symbol,
            SkipReason.EXPOSURE_FULL,
            f"{skipped[0].detail}: book ₹{account.open_exposure_inr:,.2f} of ₹{ceiling:,.2f} "
            f"leaves ₹{headroom:,.2f}, and at that size {why}",
        )
    ]


class PgSignalStore:
    """`sw_signal` + a one-line `sw_plan(SIGNAL)` for a trigger; a signal row alone otherwise.

    Nothing here places anything. The line it writes is `PROPOSED`, and the only thing that can
    move it past that is a person clicking confirm on the desk page (SW7) — with the execution
    flag on, which it is not.
    """

    def __init__(
        self,
        conn: Any,
        *,
        user_id: int,
        day: dt.date,
        config: SwingConfig,
        context: SignalContext | None = None,
    ) -> None:
        self.conn = conn
        self.user_id = user_id
        self.day = day
        self.config = config
        #: The reading the morning started with (or the last one taken). Kept for the log and
        #: the drill's report; never what a plan is sized against — `_plan_for` re-reads.
        self.context = context if context is not None else self.read_context()
        self.lines_written: list[int] = []

    def read_context(self) -> SignalContext:
        """The book as it is *now* — A5: read per trigger, so the second SIGNAL plan of a
        morning sees the first's confirm. Three queries; a few triggers a morning."""
        self.context = load_context(self.conn, user_id=self.user_id, day=self.day)
        return self.context

    def raise_signal(self, signal: Signal) -> None:
        verdict = signal.verdict
        raised_at = signal.at.replace(tzinfo=IST)
        row = self.conn.execute(
            f"INSERT INTO {SCHEMA}.sw_signal (user_id, watch_id, instrument_id, setup, "
            "session_date, raised_at, state, or_window_minutes, range_high, range_low, "
            "low_of_day, last_price, entry, stop) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                self.user_id,
                signal.watch.watch_id,
                signal.watch.instrument_id,
                signal.watch.setup.value,
                self.day,
                raised_at,
                verdict.state.value,
                signal.window_minutes,
                verdict.range_high,
                verdict.range_low,
                signal.low_of_day,
                signal.last_price,
                verdict.entry,
                verdict.stop,
            ),
        ).fetchone()
        signal_id = int(row["id"]) if row is not None else None
        if verdict.state is not TriggerState.TRIGGERED or verdict.entry is None or verdict.stop is None:
            return
        line_id = self._plan_for(signal, raised_at)
        if line_id is not None and signal_id is not None:
            self.conn.execute(
                f"UPDATE {SCHEMA}.sw_signal SET plan_line_id = ? WHERE id = ?",
                (line_id, signal_id),
            )
            self.lines_written.append(line_id)

    def _plan_for(self, signal: Signal, raised_at: dt.datetime) -> int | None:
        """One `sw_plan(SIGNAL)`: a line if the rules allow it, a skip if they do not.

        Sized against the context as re-read at this moment, not the one the morning started
        with: a name confirmed at 09:31 is on the book when the 09:45 trigger is sized, and the
        session's entries so far count against the per-session cap. What the page shows is what
        the confirm-time gate (`app.swing_execute`) will re-derive under its lock.
        """
        verdict = signal.verdict
        context = self.read_context()
        adr, turnover, score = context.detected.get(
            signal.watch.symbol, (Decimal(0), None, Decimal(0))
        )
        item = WatchItem(
            symbol=signal.watch.symbol,
            setup=signal.watch.setup,
            trigger=verdict.entry or Decimal(0),
            stop_ref=verdict.stop or Decimal(0),
            adr_pct=adr,
            avg_turnover_inr=turnover,
            score=score,
            locked_upper_circuit=False,
        )
        entries, skipped = entries_now(item, context, self.config, day=self.day)
        plan = assemble(
            as_of=self.day,
            gate=context.gate,
            tier=context.tier,
            entries=entries,
            exits=[],
            skipped=skipped,
        )
        plan_id = uuid.uuid4()
        plan_row = self.conn.execute(
            f"INSERT INTO {SCHEMA}.sw_plan (plan_id, user_id, as_of, source, built_at, "
            "expires_at, plan_hash, gate, exposure_level, total_risk_inr, "
            "total_new_exposure_inr) VALUES (?, ?, ?, 'SIGNAL', ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                plan_id,
                self.user_id,
                self.day,
                raised_at,
                raised_at + dt.timedelta(minutes=PLAN_TTL_MINUTES),
                plan.plan_hash(),
                plan.gate.value,
                plan.tier.level,
                plan.total_risk_inr,
                plan.total_new_exposure_inr,
            ),
        ).fetchone()
        plan_pk = int(plan_row["id"])
        for skip in plan.skipped:
            self.conn.execute(
                f"INSERT INTO {SCHEMA}.sw_plan_skip (plan_id, user_id, instrument_id, symbol, "
                "reason, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    plan_pk,
                    self.user_id,
                    signal.watch.instrument_id,
                    skip.symbol,
                    skip.reason.value,
                    skip.detail or None,
                ),
            )
        line_id: int | None = None
        for line in plan.lines:
            row = self.conn.execute(
                f"INSERT INTO {SCHEMA}.sw_plan_line (plan_id, user_id, kind, instrument_id, "
                "setup, quantity, trigger, stop, risk_inr, position_value, trail, note, state, "
                "client_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PROPOSED', ?) "
                "RETURNING id",
                (
                    plan_pk,
                    self.user_id,
                    line.kind.value,
                    signal.watch.instrument_id,
                    line.setup.value if line.setup else None,
                    line.quantity,
                    line.trigger,
                    line.stop,
                    line.risk_inr,
                    line.position_value,
                    line.trail.value if line.trail else None,
                    line.note,
                    f"{plan_id}:{line.symbol}:{line.kind.value}",
                ),
            ).fetchone()
            line_id = int(row["id"])
        if plan.lines:
            log.info(
                "SIGNAL plan %s: BUY %s x%d @ %s stop %s (expires %s)",
                plan_id,
                signal.watch.symbol,
                plan.lines[0].quantity,
                plan.lines[0].trigger,
                plan.lines[0].stop,
                (raised_at + dt.timedelta(minutes=PLAN_TTL_MINUTES)).time(),
            )
        else:
            log.info(
                "SIGNAL plan %s: %s triggered but skipped — %s",
                plan_id,
                signal.watch.symbol,
                "; ".join(f"{s.reason.value} {s.detail}".strip() for s in plan.skipped),
            )
        return line_id


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


# --- the broker's candles ----------------------------------------------------------------


class KiteCandles:
    """`historical_data(interval="minute")` from the open to `until`, as core `Candle`s."""

    def __init__(self, kite: Any) -> None:
        self.kite = kite

    def minute_candles(self, token: int, day: dt.date, until: dt.datetime) -> list[Candle]:
        start = dt.datetime.combine(day, dt.time(9, 15))
        raw = self.kite.kc.historical_data(token, start, until, "minute")
        candles: list[Candle] = []
        for row in raw:
            stamp = row["date"]
            if isinstance(stamp, dt.datetime):
                stamp = stamp.replace(tzinfo=None)
            candles.append(
                Candle(
                    start=stamp,
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=int(row.get("volume") or 0),
                )
            )
        return candles


# --- the flag, and the loop --------------------------------------------------------------


def build_monitor(  # noqa: PLR0913 - one keyword per collaborator
    *,
    enabled: bool,
    gateway: Any,
    watchlist: list[WatchedName],
    store: Any,
    candles: Any,
    day: dt.date,
    config: SwingConfig = DEFAULT_SWING_CONFIG,
    window_minutes: int | None = None,
) -> SwingBreakout | None:
    """The strategy, or ``None`` with the flag off — in which case nothing is constructed."""
    if not enabled:
        log.info("BASKFY_SWING_MONITOR_ENABLED is false; the opening-range monitor is not built")
        return None
    return SwingBreakout(
        gateway,
        watchlist=watchlist,
        store=store,
        candles=candles,
        day=day,
        window_minutes=window_minutes,
        config=config,
    )


async def run_until_close(strategy: SwingBreakout, bus: Any, *, poll_seconds: float = 1.0) -> int:
    """Consume the bus until `monitor_close`; returns the number of signals raised."""
    queues = {token: bus.subscribe(token) for token in strategy.tokens}
    await strategy.on_start()
    try:
        while True:
            now = dt.datetime.now(tz=IST).replace(tzinfo=None)
            if strategy.session_over(now):
                log.info("monitor close reached; %d signals raised", len(strategy.signals))
                break
            drained = False
            for q in queues.values():
                while not q.empty():
                    await strategy.on_tick(q.get_nowait())
                    drained = True
            if not drained:
                await asyncio.sleep(poll_seconds)
    finally:
        await strategy.on_stop()
    return len(strategy.signals)


def main() -> int:
    """`python -m app.swing_monitor` — the morning process. Exits 0 and does nothing when
    the flag is off, so a launchd/cron entry can exist before the flag does."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if not C.SWING_MONITOR_ENABLED:
        log.info("BASKFY_SWING_MONITOR_ENABLED is false; exiting without building the monitor")
        return 0
    from .analytics.db import connect  # noqa: PLC0415 - the DB is only needed with the flag on
    from .core.ticker import TickBus, start_ticker  # noqa: PLC0415
    from .kite_client import Kite  # noqa: PLC0415

    user_id = int(os.environ.get("BASKFY_SOLE_USER_ID", "0") or 0)
    if not user_id:
        log.error("BASKFY_SOLE_USER_ID is not set; the watchlist is keyed by user")
        return 2
    day = dt.datetime.now(tz=IST).date()
    kite = Kite()
    with connect() as conn:
        config = load_config(conn, user_id=user_id)
        symbols = [
            str(r["symbol"])
            for r in conn.execute(
                f"SELECT i.symbol FROM {SCHEMA}.sw_watch w JOIN {SCHEMA}.instrument i "
                "ON i.id = w.instrument_id WHERE w.user_id = ? AND w.state = 'WATCHING'",
                (user_id,),
            ).fetchall()
        ]
        watchlist = load_watchlist(conn, user_id=user_id, circuits=circuit_bands(kite, symbols))
        context = load_context(conn, user_id=user_id, day=day)
        store = PgSignalStore(conn, user_id=user_id, day=day, config=config, context=context)
        # No gateway at all. `BaseStrategy` takes one because every other engine trades through
        # it; this one raises signals, and a process that holds no gateway cannot be talked into
        # using one.
        strategy = build_monitor(
            enabled=C.SWING_MONITOR_ENABLED,
            gateway=None,
            watchlist=watchlist,
            store=store,
            candles=KiteCandles(kite),
            day=day,
            config=config,
        )
        if strategy is None or not watchlist:
            log.info("nothing to watch today (%d names)", len(watchlist))
            return 0

        async def _serve() -> int:
            bus = TickBus()
            start_ticker(C.KITE_API_KEY, kite.kc.access_token, strategy.tokens, bus)
            return await run_until_close(strategy, bus)

        raised = asyncio.run(_serve())
        record_monitor_ran(conn, user_id=user_id, day=day, signals=raised)
    return 0


def record_monitor_ran(conn: Any, *, user_id: int, day: dt.date, signals: int) -> None:
    """`sw_session.monitor_ran` for today — the evening job (SW5) fills in the rest.

    The row may not exist yet: the evening writes it after the close. So this is an upsert
    that claims only the two columns the morning knows, and the mode it writes is DRY_RUN
    unless the execution flag says otherwise, exactly as the evening would.
    """
    conn.execute(
        f"INSERT INTO {SCHEMA}.sw_session (user_id, session_date, mode, monitor_ran, signals, "
        "notes) VALUES (?, ?, ?, true, ?, 'swing-monitor') "
        "ON CONFLICT (user_id, session_date) DO UPDATE SET monitor_ran = true, "
        "signals = EXCLUDED.signals",
        (user_id, day, "LIVE" if C.SWING_EXECUTION_ENABLED else "DRY_RUN", signals),
    )


if __name__ == "__main__":
    raise SystemExit(main())
