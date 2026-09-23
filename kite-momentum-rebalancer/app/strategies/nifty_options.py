"""The NIFTY options monitor: open positions in, exit verdicts out, never an order (OP9).

`docs/options/06` OP9: a desk process that observes NIFTY 50 from 09:15, "after `OPEN`,
subscribes the legs and marks them … `exits.evaluate` per tick → an exit plan handed to OP10's
executor". `generate_targets` returns `[]` and the process holds no gateway: it decides, and the
executor (OP10) sends.

WHAT IT READS. The index's ticks (the spot, the last-tick clock for `04` §8.5's feed rule, and
minute bars built from the ticks), each open leg's depth ticks (the conservative mark: bid for a
long, ask for a short), and the store's `op_position` rows. Minute bars are reconciled against
`op_index_minute` — the collector's historical-data minutes win where they exist (A4), so an
invalidation reads the same bars the scan did.

WHAT IT DOES. On every tick, and on every idle pass (a feed that stops sending ticks must still
be judged — that is what `FEED_LOST` is), it calls the pure `baskfy_core.options.exits.evaluate`
for each open position without an exit. A verdict becomes one exit plan through the store
(idempotent: a position gets one exit plan, ever), and the position is no longer evaluated. Every
`mark_every_seconds` it records each position's mark on `op_position` (what the web page shows).

WHAT IT DOES NOT DO. Raise entry plans — the worker's minute builders do that, idempotently, behind
the same `BASKFY_OPTIONS_MONITOR_ENABLED` flag (DECISIONS-OP OP9.1). Touch a gateway, a broker or
the wall clock: its time is the tick's `exchange_timestamp`, or the time the runner hands
`check()`, which is what makes `tools/options/replay.py` deterministic.

RESUME. `on_start` loads the day's open positions from the store, so a restart at 11:30 carries on
where the last process stopped; the first reconcile brings back the morning's bars (OP9 AC).

The module is named `nifty_options`, not `options_monitor` as `06` OP9 wrote it: the desk's frozen
boundary forbids any import containing `strategies.options` (DECISIONS-OP OP9.2).
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Protocol

from baskfy_core.options.bars import Bar
from baskfy_core.options.config import OptionsConfig
from baskfy_core.options.execution import LegRole
from baskfy_core.options.exits import (
    FEED_LOST,
    ExitVerdict,
    IndexState,
    LegMark,
    OpenPosition,
    evaluate,
    mark,
)

from .base import BaseStrategy

log = logging.getLogger("desk.options_monitor")

#: NIFTY 50's Kite instrument token (the index the whole book reads).
NIFTY_50_TOKEN = 256265
#: The session's last moment: nothing is watched after the close.
SESSION_CLOSE = dt.time(15, 30)


@dataclass(frozen=True)
class TrackedPosition:
    """An `op_position` the monitor watches: its session, the pure view the rules read, and the
    exit plan already raised for it (``None`` while it is still being evaluated)."""

    session_id: int
    position: OpenPosition
    exit_plan_id: str | None = None

    @property
    def tokens(self) -> tuple[int, ...]:
        return tuple(lg.instrument_token for lg in self.position.legs)


class PositionStore(Protocol):
    """What the monitor asks of the database — `PgPositionStore` on the desk, a list in tests."""

    def open_positions(self, day: dt.date) -> list[TrackedPosition]: ...

    def raise_exit(
        self, tracked: TrackedPosition, verdict: ExitVerdict, at: dt.datetime
    ) -> str | None: ...

    def record_mark(self, tracked: TrackedPosition, value: Decimal, at: dt.datetime) -> None: ...

    def index_minutes(self, day: dt.date, until: dt.datetime) -> list[Bar]: ...


@dataclass
class _Minute:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass
class _State:
    """The monitor's memory for the day. Everything here is rebuilt by a restart."""

    positions: dict[int, TrackedPosition] = field(default_factory=dict)
    marks: dict[int, LegMark] = field(default_factory=dict)
    ticked: dict[dt.datetime, _Minute] = field(default_factory=dict)
    stored: dict[dt.datetime, Bar] = field(default_factory=dict)
    spot: Decimal | None = None
    last_index_tick: dt.datetime | None = None
    refreshed_at: dt.datetime | None = None
    reconciled_at: dt.datetime | None = None
    marked_at: dict[int, dt.datetime] = field(default_factory=dict)


class NiftyOptionsMonitor(BaseStrategy):
    """Marks every open options position and raises its exit — the desk's half of OP9."""

    name = "nifty_options_monitor"
    products = ("MIS",)

    def __init__(  # noqa: PLR0913 - one keyword per collaborator and cadence
        self,
        gateway,  # noqa: ANN001 - the desk's BaseStrategy signature; always None here
        *,
        store: PositionStore,
        day: dt.date,
        options: OptionsConfig | None = None,
        index_token: int = NIFTY_50_TOKEN,
        refresh_seconds: int = 30,
        reconcile_seconds: int = 60,
        mark_every_seconds: int = 30,
    ) -> None:
        # No gateway, by construction: `BaseStrategy` stores what it is given, and the runner
        # gives `None`. The executor that closes positions is OP10's and lives elsewhere.
        super().__init__(gateway)
        self.store = store
        self.day = day
        self.options = options or OptionsConfig()
        self.index_token = index_token
        self.refresh_every = dt.timedelta(seconds=refresh_seconds)
        self.reconcile_every = dt.timedelta(seconds=reconcile_seconds)
        self.mark_every = dt.timedelta(seconds=mark_every_seconds)
        self.state = _State()
        #: Every verdict raised this session, in order: (session_id, code, reason, at).
        self.exits: list[tuple[int, str, str, dt.datetime]] = []

    # --- what the runner reads --------------------------------------------------------------

    @property
    def tokens(self) -> list[int]:
        """The index, then every leg of every tracked position — what the ticker must follow."""
        legs = {t for p in self.state.positions.values() for t in p.tokens}
        return [self.index_token, *sorted(legs)]

    def session_over(self, at: dt.datetime) -> bool:
        return _naive(at).time() >= SESSION_CLOSE

    def bars(self) -> tuple[Bar, ...]:
        """The day's minute bars: the store's where it has them, the ticks' otherwise."""
        merged = {
            minute: Bar(ts=minute, open=m.open, high=m.high, low=m.low, close=m.close)
            for minute, m in self.state.ticked.items()
        }
        merged.update(self.state.stored)
        return tuple(merged[k] for k in sorted(merged))

    # --- lifecycle --------------------------------------------------------------------------

    async def on_start(self) -> None:
        self._refresh(None)

    async def on_tick(self, tick: dict) -> None:
        token = tick.get("instrument_token")
        at = _tick_time(tick, self.day)
        if token == self.index_token:
            price = _decimal(tick.get("last_price"))
            if price is not None:
                self._index(at, price)
        elif isinstance(token, int):
            self._leg(token, tick, at)
        self.check(at)

    def check(self, now: dt.datetime) -> list[tuple[int, ExitVerdict]]:
        """One evaluation pass at `now`: refresh, reconcile, then every open position's exit.

        The runner calls this on every idle pass too, so a silent feed is judged by the clock.
        Returns the verdicts raised by this pass.
        """
        now = _naive(now)
        if self._due(self.state.refreshed_at, self.refresh_every, now):
            self._refresh(now)
        if self._due(self.state.reconciled_at, self.reconcile_every, now):
            self._reconcile(now)
        raised: list[tuple[int, ExitVerdict]] = []
        index = IndexState(
            spot=self.state.spot, last_tick_at=self.state.last_index_tick, bars=self.bars()
        )
        for session_id, tracked in list(self.state.positions.items()):
            if tracked.exit_plan_id is not None:
                continue
            marks = {
                lg.role: self.state.marks[lg.instrument_token]
                for lg in tracked.position.legs
                if lg.instrument_token in self.state.marks
            }
            self._record_mark(tracked, marks, now)
            verdict = evaluate(tracked.position, marks, index, now, options=self.options)
            if verdict is None:
                continue
            plan_id = self._raise(tracked, verdict, now)
            self.state.positions[session_id] = replace(tracked, exit_plan_id=plan_id or "RAISED")
            self.exits.append((session_id, verdict.code, verdict.reason, now))
            raised.append((session_id, verdict))
        return raised

    def token_lost(self, now: dt.datetime) -> list[tuple[int, ExitVerdict]]:
        """The Kite token was refused with positions open: `HARD_EXIT / FEED_LOST` for each, now.

        `04` §8.5's feed rule waits for 14:00 (O1, O3) or the grace (O2) because a quiet tape is
        not a dead one. A refused token is: nothing will mark these legs again until a person logs
        in, so every open position gets its exit raised at once and the sweep closes it as soon
        as a quote can be read (OP14; runbook 12 says what the human does meanwhile).
        """
        now = _naive(now)
        raised: list[tuple[int, ExitVerdict]] = []
        for session_id, tracked in list(self.state.positions.items()):
            if tracked.exit_plan_id is not None:
                continue
            verdict = ExitVerdict("HARD_EXIT", FEED_LOST, None, None, True)
            plan_id = self._raise(tracked, verdict, now)
            self.state.positions[session_id] = replace(tracked, exit_plan_id=plan_id or "RAISED")
            self.exits.append((session_id, verdict.code, verdict.reason, now))
            raised.append((session_id, verdict))
        return raised

    async def generate_targets(self, context: dict) -> list[dict]:
        """Nothing. Ever. The monitor decides exits; OP10's executor sends them."""
        return []

    # --- internals --------------------------------------------------------------------------

    def _index(self, at: dt.datetime, price: Decimal) -> None:
        self.state.spot = price
        self.state.last_index_tick = at
        minute = at.replace(second=0, microsecond=0)
        bar = self.state.ticked.get(minute)
        if bar is None:
            self.state.ticked[minute] = _Minute(price, price, price, price)
        else:
            bar.high = max(bar.high, price)
            bar.low = min(bar.low, price)
            bar.close = price

    def _leg(self, token: int, tick: dict, at: dt.datetime) -> None:
        depth = tick.get("depth") or {}
        bid = _best(depth.get("buy"))
        ask = _best(depth.get("sell"))
        if bid is None and ask is None:
            return
        self.state.marks[token] = LegMark(bid=bid, ask=ask, at=at)

    def _refresh(self, now: dt.datetime | None) -> None:
        """Take in positions the executor opened since the last look; drop closed ones. A
        position already carrying an exit plan keeps it (never a second exit)."""
        try:
            fresh = self.store.open_positions(self.day)
        except Exception:  # a store error must not stop the marks already running
            log.exception("options monitor: could not read open positions")
            return
        known = self.state.positions
        self.state.positions = {
            t.session_id: (
                replace(t, exit_plan_id=known[t.session_id].exit_plan_id)
                if t.session_id in known and t.exit_plan_id is None
                else t
            )
            for t in fresh
        }
        self.state.refreshed_at = now

    def _reconcile(self, now: dt.datetime) -> None:
        try:
            stored = self.store.index_minutes(self.day, now)
        except Exception:  # the ticks' bars stand in until the store answers
            log.exception("options monitor: could not read op_index_minute")
            return
        self.state.stored = {_naive(b.ts).replace(second=0, microsecond=0): b for b in stored}
        self.state.reconciled_at = now

    def _record_mark(
        self, tracked: TrackedPosition, marks: dict[LegRole, LegMark], now: dt.datetime
    ) -> None:
        last = self.state.marked_at.get(tracked.session_id)
        if last is not None and now - last < self.mark_every:
            return
        current = mark(tracked.position, marks, now, self.options)
        if current.value_points is None:
            return
        try:
            self.store.record_mark(tracked, current.value_points, now)
        except Exception:  # a failed mark write is a stale page, never a stopped monitor
            log.exception("options monitor: could not record a mark")
            return
        self.state.marked_at[tracked.session_id] = now

    def _raise(self, tracked: TrackedPosition, verdict: ExitVerdict, now: dt.datetime) -> str | None:
        log.warning(
            "options exit: session %s %s %s (%s) value=%s loss=%s",
            tracked.session_id,
            tracked.position.sleeve.value,
            verdict.code,
            verdict.reason,
            verdict.value_points,
            verdict.marked_loss_inr,
        )
        try:
            return self.store.raise_exit(tracked, verdict, now)
        except Exception:  # the verdict stands; the next pass must not raise it twice
            log.exception("options monitor: could not write the exit plan")
            return None

    @staticmethod
    def _due(last: dt.datetime | None, every: dt.timedelta, now: dt.datetime) -> bool:
        return last is None or now - last >= every


def _naive(at: dt.datetime) -> dt.datetime:
    """Naive IST, the desk's tick time (`exchange_timestamp` arrives naive IST)."""
    if at.tzinfo is None:
        return at
    return at.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30))).replace(tzinfo=None)


def _best(levels: object) -> Decimal | None:
    """The top of one side of Kite's depth; a zero price is an empty level, not a quote."""
    if not isinstance(levels, list) or not levels:
        return None
    top = levels[0]
    price = _decimal(top.get("price")) if isinstance(top, dict) else None
    return price if price is not None and price > 0 else None


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


def _tick_time(tick: dict, day: dt.date) -> dt.datetime:
    """The tick's exchange timestamp, or the last-trade time, or now — the swing monitor's rule."""
    for key in ("exchange_timestamp", "last_trade_time", "timestamp"):
        value = tick.get(key)
        if isinstance(value, dt.datetime):
            return _naive(value)
        if isinstance(value, str):
            try:
                return _naive(dt.datetime.fromisoformat(value))
            except ValueError:
                continue
    return dt.datetime.combine(day, dt.datetime.now().time())
