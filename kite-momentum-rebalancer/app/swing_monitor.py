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
THE NOTIFICATION (SW11, STANDING-ANSWERS A2)
--------------------------------------------
The row the desk page (SW7) polls and a log line at INFO, as SW6.3 built it — plus, for a name
in the daily focus (`sw_watch.focus`, A14), one one-way push through `app.swing_notify`: the
whole line (symbol, entry, stop, qty, ₹ risk, plan expiry) or the skip and its reason. The
notifier can only tell; a failure in it is logged and the morning goes on.

THE CLOCK (SW11)
----------------
This process is the desk's clock for the session: it watches from 09:15 to `monitor_close`,
then `app.swing_clock` runs the 10:45 cutoff (cancel every open remainder, free every unclaimed
slot) and, at 15:15, the GTT sweep (no filled quantity without a stop). The strategy never
holds a gateway (SW6.4); the clock builds one only after the strategy has stopped, for those
two order-shaped chores, and `app.swing_clock` is where that happens.

QUOTE FALLBACK (B10)
--------------------
The ticker is the feed. When it goes quiet — no tick for any watched name for
`quote_poll_min_seconds` — the loop asks Kite `/quote` once for the watchlist and feeds the
answer through the same `on_tick`; `QuoteFallback` refuses a second call inside that window
whatever the loop asks, so the cap is a property of the object, not of the loop's timing.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
import uuid
from collections.abc import Callable
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
    first_live_multiplier,
)

from . import config as C
from . import telemetry as _tel
from .strategies.swing_breakout import Signal, SwingBreakout, WatchedName
from .swing_notify import Notifier, SignalNotice, notice_json

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
        f"SELECT w.id, w.instrument_id, i.symbol, i.kite_token, w.setup, w.trigger, w.focus "
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
                focus=bool(row["focus"]) if "focus" in row.keys() else False,
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
    #: symbol → (adr_pct, avg_turnover_inr, score) from the latest detection row — or, for a
    #: name with none (a live gap), from the watch row's own `adr_pct` / `score` (SW10.5).
    detected: dict[str, tuple[Decimal, Decimal | None, Decimal]]
    entries_today: int = 0
    pending: tuple[PendingLine, ...] = ()
    #: A7: symbols whose `PENDING_RANGE` line on today's MORNING plan is still `PROPOSED` —
    #: each holds one of the session's new-entry slots until its range breaks or 10:45.
    reserved: tuple[str, ...] = ()
    #: A9: `sw_config.first_live_sessions_left`, so the SIGNAL plan and the confirm size at
    #: the same half risk while the countdown runs.
    first_live_sessions_left: int = 0


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
        f"SELECT sleeve_capital_inr, first_live_sessions_left FROM {_t(schema, 'sw_config')} "
        "WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    capital = Decimal(str(capital_row["sleeve_capital_inr"])) if capital_row else Decimal(0)
    first_live_left = int(capital_row["first_live_sessions_left"]) if capital_row else 0
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
        f"SELECT l.id, i.symbol, l.quantity, l.trigger, l.state, l.position_id, "
        f"(SELECT q.quantity_entered FROM {_t(schema, 'sw_position')} q "
        "WHERE q.id = l.position_id) AS entered "
        f"FROM {_t(schema, 'sw_plan_line')} l "
        f"JOIN {_t(schema, 'sw_plan')} p ON p.id = l.plan_id "
        f"JOIN {_t(schema, 'instrument')} i ON i.id = l.instrument_id "
        "WHERE l.user_id = ? AND p.as_of = ? AND l.kind = 'BUY_ON_TRIGGER' "
        f"AND l.state IN ({', '.join('?' for _ in ENTRY_TAKEN_STATES)}) ORDER BY l.id",
        (user_id, day.isoformat(), *ENTRY_TAKEN_STATES),
    ).fetchall()
    pending: list[PendingLine] = []
    for r in taken:
        symbol = str(r["symbol"])
        if r["position_id"] is not None or symbol in open_symbols:
            # On the book as a position, counted at cost above. A8: a partially filled live
            # LIMIT still resting for the rest is exposure the book may yet take — the
            # unfilled remainder counts at the trigger until 10:45 cancels it.
            if str(r["state"]) == "SENT" and r["entered"] is not None:
                remainder = int(r["quantity"]) - int(r["entered"])
                if remainder > 0:
                    exposure += Decimal(str(r["trigger"] or 0)) * Decimal(remainder)
            continue
        value = Decimal(str(r["trigger"] or 0)) * Decimal(int(r["quantity"]))
        pending.append(
            PendingLine(
                line_id=int(r["id"]),
                symbol=symbol,
                quantity=int(r["quantity"]),
                value_inr=value,
                state=str(r["state"]),
            )
        )
        exposure += value
        open_symbols.add(symbol)
    # A7: the slots the morning plan reserved for live gaps whose range has not broken yet.
    reserved = tuple(
        str(r["symbol"])
        for r in conn.execute(
            f"SELECT i.symbol FROM {_t(schema, 'sw_plan_line')} l "
            f"JOIN {_t(schema, 'sw_plan')} p ON p.id = l.plan_id "
            f"JOIN {_t(schema, 'instrument')} i ON i.id = l.instrument_id "
            "WHERE l.user_id = ? AND p.as_of = ? AND l.kind = 'PENDING_RANGE' "
            "AND l.state = 'PROPOSED' ORDER BY l.id",
            (user_id, day.isoformat()),
        ).fetchall()
    )
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
    # SW10.5: a watched name with no detection row — a live gap the 09:09 scan wrote — carries
    # its ADR and its provisional score on the watch row (`sw_watch.adr_pct` / `score`), and
    # a stop that cannot be measured against one ADR is a stop the plan refuses (SW9.5.2).
    for r in conn.execute(
        f"SELECT i.symbol, w.adr_pct, w.score FROM {_t(schema, 'sw_watch')} w "
        f"JOIN {_t(schema, 'instrument')} i ON i.id = w.instrument_id "
        "WHERE w.user_id = ? AND w.state = 'WATCHING' ORDER BY w.id",
        (user_id,),
    ).fetchall():
        symbol = str(r["symbol"])
        if symbol in detected or r["adr_pct"] is None:
            continue
        detected[symbol] = (
            Decimal(str(r["adr_pct"])),
            None,
            Decimal(str(r["score"])) if r["score"] is not None else Decimal(0),
        )
    return SignalContext(
        gate=gate,
        tier=tier,
        account=account,
        detected=detected,
        entries_today=len(taken),
        pending=tuple(pending),
        reserved=reserved,
        first_live_sessions_left=first_live_left,
    )


def live_execution() -> bool:
    """Whether a confirm would place a real order: the swing flag on AND the desk not in
    DRY_RUN — the same truth table as `swing_execute.swing_gates`, read from the config module
    at call time. A9's half risk applies exactly when this is true."""
    return bool(C.SWING_EXECUTION_ENABLED) and not bool(C.DRY_RUN)


def auto_execute_enabled() -> bool:
    """SW25: may the monitor confirm its own triggers? All THREE flags, never fewer.

    `live_execution()` is "a confirm would place a real order". This is "and nobody has to make
    it". They are separate questions and separate switches on purpose: going live and going
    unattended are two decisions, and either must be reversible without undoing the other.

    Read from the config module at call time, like every other swing flag, so the answer cannot
    be stale in a long-lived process.
    """
    return live_execution() and bool(C.SWING_AUTO_EXECUTE)


def entries_now(
    item: WatchItem,
    context: SignalContext,
    config: SwingConfig,
    *,
    day: dt.date,
    live: bool | None = None,
) -> tuple[list[PlanLine], list[Skipped]]:
    """One triggered name against the book as it is now — the SIGNAL plan's size and the
    confirm's, from one function (SW10.4 / SW10.5).

    Since SW10.5: the session's reserved slots (A7 — every `PENDING_RANGE` line still open,
    less this name's own) count with today's entries against the per-session cap, and the
    first-live half risk (A9) scales the risk budget while `first_live_sessions_left` runs
    and a real order would go out (`live`; read from the flags when not given).

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
    already = context.entries_today + sum(1 for s in context.reserved if s != item.symbol)
    multiplier = first_live_multiplier(
        sessions_left=context.first_live_sessions_left,
        execution_enabled=live_execution() if live is None else live,
        config=config.sizing,
    )
    lines, skipped = build_entries(
        as_of=day, watch=[item], account=context.account, gate=context.gate, tier=context.tier,
        config=config, entries_already_today=already, risk_multiplier=multiplier,
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
        entries_already_today=already, risk_multiplier=multiplier,
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

    Nothing here places anything **unless `BASKFY_SWING_AUTO_EXECUTE` is on** (SW25, 5 Sep
    2026). The line it writes is `PROPOSED`, and by default the only thing that can move it past
    that is a person clicking confirm on the desk page (SW7). With all three flags on — DRY_RUN
    off, `SWING_EXECUTION_ENABLED` on, `SWING_AUTO_EXECUTE` on — the runner's
    `drain_auto_execute` confirms it through the same `execute_line` the button reaches, and a
    real order is placed with nobody watching. This class only records that there is a line to
    confirm (`pending_confirms`); it never reaches a broker itself. That is the desk's first non-negotiable removed at Maulik's instruction;
    `docs/swing/DECISIONS-SW.md` SW25 is the record.
    """

    def __init__(  # noqa: PLR0913 - one keyword per collaborator
        self,
        conn: Any,
        *,
        user_id: int,
        day: dt.date,
        config: SwingConfig,
        context: SignalContext | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.conn = conn
        self.user_id = user_id
        self.day = day
        self.config = config
        #: The reading the morning started with (or the last one taken). Kept for the log and
        #: the drill's report; never what a plan is sized against — `_plan_for` re-reads.
        self.context = context if context is not None else self.read_context()
        self.lines_written: list[int] = []
        #: SW25: what auto-execute did this morning — `(symbol, status)` per confirmed line,
        #: and the symbols whose execution raised. Reported by the runner at the close, so a
        #: morning that placed real orders says so in one line rather than in the log's noise.
        #: SW25: lines this store wrote that auto-execute may confirm, drained by the
        #: runner. Holding them rather than acting on them is what keeps a store a store.
        self.pending_confirms: list[tuple[Signal, Planned]] = []
        #: SW11 / A2: where a daily-focus trigger is told. ``None`` (the tests, the drill)
        #: means the row and the log line only, as SW6.3 built it.
        self.notifier = notifier
        self.notices: list[SignalNotice] = []

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
        _tel.count("swing_signals", state=verdict.state.value)
        if verdict.state is not TriggerState.TRIGGERED or verdict.entry is None or verdict.stop is None:
            return
        planned = self._plan_for(signal, raised_at)
        if planned.line_id is not None and signal_id is not None:
            self.conn.execute(
                f"UPDATE {SCHEMA}.sw_signal SET plan_line_id = ? WHERE id = ?",
                (planned.line_id, signal_id),
            )
            self.lines_written.append(planned.line_id)
        if planned.line_id is not None and planned.plan_id is not None:
            # SW25: RECORDED, not executed. A store writes rows; it does not reach a broker,
            # and `test_the_store_never_names_a_placing_verb` is the check that keeps it that
            # way — it caught exactly this method living here in the first draft. The runner
            # (`drain_auto_execute`) is what confirms these, so the order path stays outside
            # both the store and the strategy.
            self.pending_confirms.append((signal, planned))
        self._notify(signal, raised_at, planned)

    def _notify(self, signal: Signal, raised_at: dt.datetime, planned: Planned) -> None:
        """A2 / A14: the daily focus is pushed; everything else is the row and the log."""
        if not signal.watch.focus:
            return
        line = planned.plan.lines[0] if planned.plan.lines else None
        notice = SignalNotice(
            symbol=signal.watch.symbol,
            setup=signal.watch.setup.value,
            at=raised_at,
            entry=line.trigger if line is not None else signal.verdict.entry,
            stop=line.stop if line is not None else signal.verdict.stop,
            quantity=line.quantity if line is not None else None,
            risk_inr=line.risk_inr if line is not None else None,
            plan_expires_at=planned.expires_at if line is not None else None,
            skipped=(
                None if line is not None
                else "; ".join(
                    f"{s.reason.value} {s.detail}".strip() for s in planned.plan.skipped
                ) or "no line"
            ),
            mode="LIVE" if live_execution() else "DRY_RUN",
        )
        self.notices.append(notice)
        log.info("swing notice %s", notice_json(notice))
        if self.notifier is None:
            return
        try:
            self.notifier.notify(notice)
        except Exception as exc:  # noqa: BLE001 - a channel never stops the morning (A2)
            log.error("swing notifier failed for %s: %s", signal.watch.symbol, exc)

    def release_reservation(self, instrument_id: int, raised_at: dt.datetime) -> int:
        """A7: mark this name's `PENDING_RANGE` line on today's plan `SKIPPED` — the range has
        broken and the SIGNAL plan is the line now, or the skip. Idempotent: only a `PROPOSED`
        line moves. Returns how many moved (0 or 1)."""
        moved = self.conn.execute(
            f"UPDATE {SCHEMA}.sw_plan_line SET state = 'SKIPPED', "
            "note = COALESCE(note, '') || ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE user_id = ? AND kind = 'PENDING_RANGE' AND state = 'PROPOSED' "
            "AND instrument_id = ? AND plan_id IN "
            f"(SELECT id FROM {SCHEMA}.sw_plan WHERE user_id = ? AND as_of = ?) RETURNING id",
            (
                f"; slot released at {raised_at.time():%H:%M} — the range broke, see the "
                f"SIGNAL plan",
                self.user_id,
                instrument_id,
                self.user_id,
                self.day,
            ),
        ).fetchall()
        return len(moved)

    def _plan_for(self, signal: Signal, raised_at: dt.datetime) -> Planned:
        """One `sw_plan(SIGNAL)`: a line if the rules allow it, a skip if they do not.

        Sized against the context as re-read at this moment, not the one the morning started
        with: a name confirmed at 09:31 is on the book when the 09:45 trigger is sized, and the
        session's entries so far count against the per-session cap. What the page shows is what
        the confirm-time gate (`app.swing_execute`) will re-derive under its lock.
        """
        verdict = signal.verdict
        # A7: this name's range has broken — its reserved slot is spent, lined or skipped.
        # Released BEFORE the context is read, and exactly once: the UPDATE moves only a
        # PROPOSED line, so a second trigger (there is none — a name triggers once) or a
        # re-read finds nothing left to release.
        self.release_reservation(signal.watch.instrument_id, raised_at)
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
        expires_at = raised_at + dt.timedelta(minutes=PLAN_TTL_MINUTES)
        plan_row = self.conn.execute(
            f"INSERT INTO {SCHEMA}.sw_plan (plan_id, user_id, as_of, source, built_at, "
            "expires_at, plan_hash, gate, exposure_level, total_risk_inr, "
            "total_new_exposure_inr) VALUES (?, ?, ?, 'SIGNAL', ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                plan_id,
                self.user_id,
                self.day,
                raised_at,
                expires_at,
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
        return Planned(line_id=line_id, plan=plan, expires_at=expires_at, plan_id=str(plan_id))


def drain_auto_execute(store: Any) -> list[tuple[str, str]]:
    """SW25: confirm every line the store just wrote, when all three flags say so.

    THE ONE THING TO UNDERSTAND. This removes the desk's first non-negotiable — the human in
    the loop — and **nothing else**. Each line is confirmed by the same
    `swing_execute.execute_line` a click reaches, with ``confirm="true"`` and the ``plan_id``
    just written, so every guard still runs: A5's re-derivation of the book under the session
    row lock, the EXPOSURE_FULL / TIER_FULL / SESSION_CAP refusals, SW22's
    MARKET-with-protection entry cap and its refusal above that cap, the GTT armed in the same
    call (non-negotiable 4), and the whole gateway chain. What is gone is the person, and the
    person was never the thing enforcing any of those.

    **The queue is drained whether or not the flag is on.** A line left in `pending_confirms`
    across a restart would be confirmed late, at a price the trigger no longer justifies; the
    plan's own 30-minute expiry would refuse most of them, and "most" is not a safety property.
    So with the flag off the queue is emptied and nothing is sent.

    FAIL SOFT, ALWAYS. The monitor's job is to watch the tape; an execution that raises must not
    take the watcher down with it, or one bad symbol at 09:20 costs every trigger for the rest
    of the morning. A failure is logged and counted and the loop goes on — the signal row and
    the plan line are already committed, so the line can be confirmed by hand from the desk
    page exactly as before.

    The price handed down is the tick that triggered it, the freshest this process has and
    seconds old by construction. `execute_line` refuses rather than guesses without one
    (SW22.2). Returns ``(symbol, status)`` per attempt, for the runner's close-of-morning line.
    """
    pending = list(store.pending_confirms)
    store.pending_confirms.clear()
    if not pending or not auto_execute_enabled():
        return []

    # The desk's own store, gateway and order source — the exact collaborators
    # `POST /swing/execute` uses, resolved at call time. Imported here rather than at module
    # scope: `swing_desk` imports the execute module, and a top-level import would tie two
    # processes' import graphs together for a path that is off by default.
    from . import swing_desk  # noqa: PLC0415 - see above
    from . import swing_execute  # noqa: PLC0415 - see above

    done: list[tuple[str, str]] = []
    for signal, planned in pending:
        symbol = signal.watch.symbol
        try:
            with swing_desk.open_store() as desk_store:
                outcome = asyncio.run(
                    swing_execute.execute_line(
                        desk_store,
                        swing_desk.swing_gateway(),
                        plan_id=planned.plan_id,
                        line_id=planned.line_id,
                        confirm="true",
                        now=swing_desk._now(),
                        last_price=signal.last_price,
                        orders=swing_desk.order_source(),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - the watcher must survive any single order
            _tel.count("swing_auto_execute", outcome="error")
            log.exception(
                "AUTO-EXECUTE %s: %s: %s — the line is written and can be confirmed by hand",
                symbol,
                type(exc).__name__,
                exc,
            )
            done.append((symbol, "ERROR"))
            continue
        _tel.count("swing_auto_execute", outcome=outcome.status)
        log.warning(
            "AUTO-EXECUTE %s: %s%s",
            symbol,
            outcome.status,
            f" — {outcome.reason}" if outcome.reason else "",
        )
        done.append((symbol, outcome.status))
    return done


@dataclass(frozen=True)
class Planned:
    """What `_plan_for` wrote: the line's id (or None for a skip), the plan, its expiry.

    ``plan_id`` is the ``sw_plan.plan_id`` uuid as a string — what `/swing/execute` and
    `execute_line` take, and what SW25's auto-execute confirms against. Carried on the result
    rather than re-queried, so the line that is confirmed is provably the line just written.
    """

    line_id: int | None
    plan: Any
    expires_at: dt.datetime
    plan_id: str | None = None


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


class QuoteFallback:
    """Kite ``/quote`` for the watchlist, at most once every ``min_interval`` seconds (B10).

    The cap lives here, on the object, so no loop can call faster than it: `poll` answers
    ``None`` inside the window and only counts a call it actually made. Rows come back as the
    ticker's own shape (`instrument_token`, `last_price`, `ohlc`, `exchange_timestamp`) so the
    strategy cannot tell a polled quote from a pushed tick. Read-only, ≤ 500 names a call.
    """

    def __init__(
        self,
        kite: Any,
        symbols_by_token: dict[int, str],
        *,
        min_interval: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.kite = kite
        self.symbols_by_token = dict(symbols_by_token)
        self.min_interval = (
            DEFAULT_SWING_CONFIG.opening_range.quote_poll_min_seconds
            if min_interval is None else float(min_interval)
        )
        self._clock = clock
        self._last_call: float | None = None
        self.calls = 0

    def due(self) -> bool:
        last = self._last_call
        return last is None or self._clock() - last >= self.min_interval

    def poll(self, now: dt.datetime) -> list[dict] | None:
        """The watchlist's quotes as ticks, or ``None`` when a call is not yet allowed."""
        if not self.due() or not self.symbols_by_token:
            return None
        self._last_call = self._clock()
        self.calls += 1
        ticks: list[dict] = []
        tokens = list(self.symbols_by_token)
        for i in range(0, len(tokens), 500):
            chunk = tokens[i : i + 500]
            try:
                data = self.kite.quote_raw([f"NSE:{self.symbols_by_token[t]}" for t in chunk])
            except Exception as exc:  # noqa: BLE001 - a failed poll is a missed poll, not a halt
                log.warning("quote fallback failed: %s", exc)
                _tel.count("swing_quote_polls", outcome="failed")
                continue
            _tel.count("swing_quote_polls", outcome="ok")
            for row in data.values():
                token = row.get("instrument_token")
                if token is None or row.get("last_price") is None:
                    continue
                stamp = row.get("timestamp") or row.get("last_trade_time") or now
                ticks.append(
                    {
                        "instrument_token": int(token),
                        "last_price": row["last_price"],
                        "ohlc": row.get("ohlc") or {},
                        "exchange_timestamp": stamp,
                    }
                )
        return ticks


async def run_until_close(  # noqa: PLR0913 - the loop's collaborators, named
    strategy: SwingBreakout,
    bus: Any,
    *,
    poll_seconds: float = 1.0,
    quotes: QuoteFallback | None = None,
    now: Callable[[], dt.datetime] | None = None,
    clock: Callable[[], float] = time.monotonic,
    on_first_tick: Callable[[], None] | None = None,
) -> int:
    """Consume the bus until `monitor_close`; returns the number of signals raised.

    ``quotes`` is the B10 fallback: asked only when no tick has arrived for
    `quote_poll_min_seconds`, and it refuses to be asked faster than that itself.
    ``on_first_tick`` fires once, when the first tick — pushed or polled — reaches the
    strategy: that is the moment the monitor is demonstrably watching the market, and it is
    when `main` writes `sw_session.monitor_ran` (SW11.2). A callback that raises is logged;
    the loop is not its concern.
    """
    read_now = now or (lambda: dt.datetime.now(tz=IST).replace(tzinfo=None))
    def _drain(strat: Any) -> None:
        """SW25's queue, emptied every pass. Tolerates a strategy whose store has no queue —
        the replay harness drives this loop with a fake, and a monitor that cannot auto-execute
        must still watch the tape."""
        store = getattr(strat, "store", None)
        if store is not None and hasattr(store, "pending_confirms"):
            drain_auto_execute(store)

    queues = {token: bus.subscribe(token) for token in strategy.tokens}
    quiet_for = DEFAULT_SWING_CONFIG.opening_range.quote_poll_min_seconds
    last_tick_at = clock()
    first_tick_seen = False
    await strategy.on_start()
    _tel.gauge("swing_monitor_up", 1)

    async def handle(tick: dict) -> None:
        nonlocal first_tick_seen
        await strategy.on_tick(tick)
        if not first_tick_seen:
            first_tick_seen = True
            if on_first_tick is not None:
                try:
                    on_first_tick()
                except Exception as exc:  # noqa: BLE001 - the record, not the watching
                    log.error("could not record the monitor's start: %s", exc)

    try:
        while True:
            moment = read_now()
            if strategy.session_over(moment):
                log.info("monitor close reached; %d signals raised", len(strategy.signals))
                break
            drained = False
            for q in queues.values():
                while not q.empty():
                    await handle(q.get_nowait())
                    drained = True
            # SW25: confirm whatever this pass triggered, then go back to watching. Drained
            # here — once per loop, outside the tick handler — so a slow broker round trip
            # cannot stall the bus, and so the queue never carries a trigger across a sleep.
            _drain(strategy)
            if drained:
                last_tick_at = clock()
                continue
            if quotes is not None and clock() - last_tick_at >= quiet_for:
                polled = quotes.poll(moment)
                for tick in polled or ():
                    await handle(tick)
                _drain(strategy)
            await asyncio.sleep(poll_seconds)
    finally:
        _tel.gauge("swing_monitor_up", 0)
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
    # A second process beside the desk server: its own metrics port, or the desk's plus one.
    _tel.install(metrics_port=monitor_metrics_port())
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
        notifier = Notifier(
            observe=lambda channel, outcome: _tel.count(
                "swing_notifications", channel=channel, outcome=outcome
            )
        )
        store = PgSignalStore(
            conn, user_id=user_id, day=day, config=config, context=context, notifier=notifier
        )
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
            # Nothing to watch is still a monitor that ran (`SWING_MONITOR_DID_NOT_START` must
            # not fire on an empty watchlist), and the clock below still owes the day its
            # cutoff and its 15:15 sweep — yesterday's positions do not care about today's list.
            log.info("nothing to watch today (%d names)", len(watchlist))
            record_monitor_ran(conn, user_id=user_id, day=day, signals=0)
        else:
            strategy.observe_with(
                lambda seconds: _tel.observe("swing_verdict_seconds", seconds)
            )
            fallback = QuoteFallback(kite, {w.token: w.symbol for w in watchlist})

            async def _serve() -> int:
                bus = TickBus()
                start_ticker(C.KITE_API_KEY, kite.kc.access_token, strategy.tokens, bus)
                # SW11.2: `monitor_ran` is written on the FIRST tick the strategy handles —
                # pushed or polled — so `SWING_MONITOR_DID_NOT_START` (09:20) reads "the
                # monitor is watching the market", not "the process was launched".
                return await run_until_close(
                    strategy, bus, quotes=fallback,
                    on_first_tick=lambda: record_monitor_ran(
                        conn, user_id=user_id, day=day, signals=0
                    ),
                )

            raised = asyncio.run(_serve())
            record_monitor_ran(conn, user_id=user_id, day=day, signals=raised)
    # The strategy is done and holds nothing. What follows is the desk's clock: the 10:45
    # cutoff now, the 15:15 GTT sweep later — `app.swing_clock` builds what those need.
    from .swing_clock import run_after_close  # noqa: PLC0415 - after the monitor, never before

    return run_after_close(day=day)


def monitor_metrics_port() -> int | None:
    """`DESK_MONITOR_METRICS_PORT`, else the desk's `METRICS_PORT` + 1, else None (metrics off).
    Two processes on one box cannot share a listener."""
    own = os.environ.get("DESK_MONITOR_METRICS_PORT", "").strip()
    if own:
        return int(own)
    return int(_tel.METRICS_PORT) + 1 if _tel.METRICS_PORT else None


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
