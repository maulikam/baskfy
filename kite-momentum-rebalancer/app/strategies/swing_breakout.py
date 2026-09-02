"""The opening-range monitor — a desk strategy that NEVER places (SW6, docs/swing/06).

His entry is the break of the opening-range high in the first sixty to ninety minutes: the
1-, 5- or 60-minute first candle's high, bought when price takes it out, stop at the range
low or the low of the day. On NSE the session opens at 09:15 IST and a 5-minute range
closes at 09:20. This process watches the watchlist's names from 09:15 to 10:45, builds
each name's range at window close, evaluates every tick against it, and when the range
breaks it RAISES A SIGNAL — a row in `sw_signal`, a one-line plan, a line on the desk page.

WHAT IT DOES NOT DO, AND WHY THAT IS THE DESIGN
------------------------------------------------
`generate_targets` returns `[]` and `on_tick` never touches `self.gw`. docs/swing/02 Track C
§3: "A trigger is a row in `sw_signal` and a line on a page. An order requires
`POST /swing/execute` with `confirm=true` and a `plan_id` issued in the last 30 minutes."
The person confirms; the monitor raises. SW10 scans this file's source for `place` and
fails the build if it ever appears.

It is a `BaseStrategy` because that is the desk's plugin shape and `intraday_skeleton.py`
is the template — the gateway is passed in like every other engine's, and deliberately
unused, so the class can be hosted by the same runner as any strategy that does trade.

WHAT IT READS
-------------
Every verdict is `baskfy_core.swing.opening_range.evaluate_trigger`, a pure function.
This module owns the plumbing: which names are watched, what makes the range, what the low
of the day has been so far, and the fact that a name that has triggered once is done for
the session. Law 1 applies to the core; the clock and the store live here.

THE RANGE IS THE TICKS' (SW11, STANDING-ANSWERS A4)
---------------------------------------------------
The opening range is built from the ticks seen inside ``[open, open + window)`` at the first
tick at or after the window's end — the same half-open window `opening_range` reads off
candles. Zerodha's historical API "was never built for polling during market hours", so the
minute candles are fetched **once**, ``range_reconcile_delay_minutes`` after the window closed,
and only to reconcile: a candle range that differs replaces the tick range for every verdict
that follows (a tick feed that connected late, or dropped a print, is corrected by the
exchange's own bar), and a signal already raised on the tick range stands — it is a row and a
line, not an order, and the confirm re-reads everything. A name with no tick inside the window
has no range until the candles say otherwise; the candle source is then asked at the reconcile
moment, never on every tick.

The store is a protocol, so the replay harness (`tools/swing/replay.py`) can feed a
recorded morning through exactly this class and assert the signals it raises, with no
broker, no bus and no database.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from baskfy_core.swing.config import DEFAULT_SWING_CONFIG, Setup, SwingConfig
from baskfy_core.swing.opening_range import (
    Candle,
    OpeningRange,
    TriggerState,
    TriggerVerdict,
    evaluate_trigger,
    opening_range,
)

from .base import BaseStrategy

log = logging.getLogger("swing_breakout")


@dataclass(frozen=True)
class WatchedName:
    """One row of the morning's watchlist, as the monitor needs it.

    `pivot_high` is None for an EP — its pivot is the gap itself (docs/swing/04 §7.2). The
    circuit band is the exchange's print for the day, read from the quote at start-up.
    """

    watch_id: int
    instrument_id: int
    symbol: str
    token: int
    setup: Setup
    pivot_high: Decimal | None
    upper_circuit: Decimal | None
    #: A14 / A2: in today's daily focus (top ``focus_top_n`` by score plus every EP) — the
    #: names whose signals are pushed. Everything else is a row and a line, never a push.
    focus: bool = False


@dataclass(frozen=True)
class Signal:
    """What the monitor raised. One per name per session, at most."""

    watch: WatchedName
    at: dt.datetime
    verdict: TriggerVerdict
    last_price: Decimal
    low_of_day: Decimal
    window_minutes: int


class SignalStore(Protocol):
    """Where a signal goes. The real one writes `sw_signal` + a one-line `sw_plan`;
    the replay harness keeps a list."""

    def raise_signal(self, signal: Signal) -> None: ...


class CandleSource(Protocol):
    """Minute candles for one token, from the session open to `until`.

    The runner backs this with `kc.historical_data(token, open, until, "minute")`; the
    replay harness backs it with a CSV. Returning an empty list means "no candles yet",
    and the strategy then falls back to the range it has built from the ticks themselves.
    """

    def minute_candles(self, token: int, day: dt.date, until: dt.datetime) -> list[Candle]: ...


@dataclass
class _NameState:
    """Per-name mutable state for one session. Prices are Decimal (house rule 9)."""

    low_of_day: Decimal | None = None
    #: The ticks' own high/low **inside the window** — what the range is built from (A4).
    tick_high: Decimal | None = None
    tick_low: Decimal | None = None
    opening: OpeningRange | None = None
    #: Whether the minute candles have been asked for (once) to reconcile the tick range.
    reconciled: bool = False
    #: Set when the candle range replaced a tick range that differed — for the log and tests.
    reconcile_note: str | None = None
    triggered: bool = False
    last_state: TriggerState | None = None
    #: The non-TRIGGERED verdicts already recorded this session, so a LOCKED or BELOW_PIVOT
    #: is written once rather than on every tick.
    recorded: set[TriggerState] = field(default_factory=set)


class SwingBreakout(BaseStrategy):
    """Watch the list, build each range at window close, raise the break. Never place."""

    name = "swing_breakout"
    products = ("CNC",)

    def __init__(  # noqa: PLR0913 - one keyword per collaborator the monitor is wired to
        self,
        gateway,  # noqa: ANN001 - the desk's BaseStrategy signature; unused here on purpose
        *,
        watchlist: list[WatchedName],
        store: SignalStore,
        candles: CandleSource,
        day: dt.date,
        window_minutes: int | None = None,
        config: SwingConfig = DEFAULT_SWING_CONFIG,
    ) -> None:
        super().__init__(gateway)
        self.watchlist = {w.token: w for w in watchlist}
        self.store = store
        self.candles = candles
        self.day = day
        self.config = config
        self.window_minutes = window_minutes or config.opening_range.default_window_minutes
        if self.window_minutes not in config.opening_range.windows_minutes:
            # Refused at start-up rather than at 09:20 — the same rule `opening_range` applies,
            # applied before a morning is spent waiting on a window that can never close.
            raise ValueError(
                f"window {self.window_minutes} is not one of "
                f"{config.opening_range.windows_minutes} (docs/swing/04 §7)"
            )
        self.state: dict[int, _NameState] = {token: _NameState() for token in self.watchlist}
        self.signals: list[Signal] = []
        hour, minute = config.opening_range.session_open
        self._open = dt.datetime.combine(day, dt.time(hour, minute))
        self._window_end = self._open + dt.timedelta(minutes=self.window_minutes)
        self._reconcile_at = self._window_end + dt.timedelta(
            minutes=config.opening_range.range_reconcile_delay_minutes
        )
        #: SW11: how long each verdict took, in seconds — the tick→verdict budget (B9) is
        #: measured off this by the desk suite; the telemetry sink reads it too.
        self.verdict_seconds: list[float] = []
        self._observe: Callable[[float], None] | None = None

    @property
    def tokens(self) -> list[int]:
        return list(self.watchlist)

    def observe_with(self, sink: Callable[[float], None] | None) -> None:
        """Hand every verdict's duration to ``sink`` (the desk's telemetry). A sink that raises
        is dropped and logged — observability never stops a verdict (SW11, B8)."""
        self._observe = sink

    # --- the one hook that matters ------------------------------------------------------

    async def on_tick(self, tick: dict) -> None:
        """One quote snapshot. Evaluates the name it belongs to; raises at most one signal."""
        started = time.perf_counter()
        token = int(tick.get("instrument_token") or 0)
        watched = self.watchlist.get(token)
        if watched is None:
            return
        state = self.state[token]
        if state.triggered:
            return
        at = _tick_time(tick, self.day)
        price = _decimal(tick.get("last_price"))
        if price is None:
            return

        # The low of the day is the stop reference (docs/swing/04 §6.1): the exchange's own
        # running low when the tick carries it, else the lowest print this process has seen.
        day_low = _decimal((tick.get("ohlc") or {}).get("low"))
        state.low_of_day = (
            day_low if day_low is not None
            else (price if state.low_of_day is None else min(state.low_of_day, price))
        )
        # The range is the ticks' inside the window, half-open like the candle rule (A4).
        if self._open <= at < self._window_end:
            state.tick_high = price if state.tick_high is None else max(state.tick_high, price)
            state.tick_low = price if state.tick_low is None else min(state.tick_low, price)

        if state.opening is None and at >= self._window_end:
            state.opening = self._tick_range(state)
        if not state.reconciled and at >= self._reconcile_at:
            self._reconcile(token, at, state)
        if state.opening is None:
            self._took(started)
            return

        verdict = evaluate_trigger(
            last_price=price,
            opening=state.opening,
            pivot_high=watched.pivot_high if watched.setup is Setup.FLAG else None,
            low_of_day=state.low_of_day or price,
            upper_circuit=watched.upper_circuit,
            at=at,
            config=self.config.opening_range,
        )
        state.last_state = verdict.state
        self._took(started)
        if verdict.state is TriggerState.TRIGGERED:
            state.triggered = True
            self._raise(watched, at, price, verdict, state)
        elif verdict.state in (TriggerState.LOCKED_UPPER_CIRCUIT, TriggerState.BELOW_PIVOT):
            # Kept for the record (docs/swing/03 §5), once each.
            if verdict.state not in state.recorded:
                state.recorded.add(verdict.state)
                self._raise(watched, at, price, verdict, state)

    def _took(self, started: float) -> None:
        elapsed = time.perf_counter() - started
        self.verdict_seconds.append(elapsed)
        if self._observe is None:
            return
        try:
            self._observe(elapsed)
        except Exception as exc:  # noqa: BLE001 - telemetry never stops a verdict
            log.debug("verdict sink failed and was dropped: %s", exc)
            self._observe = None

    def _tick_range(self, state: _NameState) -> OpeningRange | None:
        """The range at window close, from the ticks inside the window (A4).

        ``None`` when no tick landed inside the window — the ticker connected late, or the
        name did not print — and then the reconcile a minute later is the first chance at a
        range. Marked complete because the window has closed: that is the fact `opening_range`
        reads off the candles, and here the clock the tick carries says it.
        """
        if state.tick_high is None or state.tick_low is None:
            return None
        return OpeningRange(
            high=state.tick_high, low=state.tick_low, window_minutes=self.window_minutes,
            complete=True, candles=1,
        )

    def _reconcile(self, token: int, at: dt.datetime, state: _NameState) -> None:
        """Ask the candle source once, ``range_reconcile_delay_minutes`` after the window
        closed, and let the exchange's own bars correct the tick range if they differ."""
        state.reconciled = True
        try:
            candles = self.candles.minute_candles(token, self.day, at)
        except Exception as exc:  # noqa: BLE001 - a candle failure must not stop the monitor
            log.warning("minute candles failed for %s: %s; the tick range stands", token, exc)
            return
        if not candles:
            return
        built = opening_range(
            candles, day=self.day, window_minutes=self.window_minutes,
            config=self.config.opening_range,
        )
        if not built.complete or built.candles == 0:
            return
        before = state.opening
        if before is not None and (before.high, before.low) == (built.high, built.low):
            return
        state.opening = built
        state.reconcile_note = (
            f"range from ticks {before.low}-{before.high} reconciled to candles "
            f"{built.low}-{built.high}" if before is not None
            else f"range from candles {built.low}-{built.high} (no tick inside the window)"
        )
        log.info("%s: %s", token, state.reconcile_note)

    def _raise(  # noqa: PLR0913 - the verdict and what it was reached from
        self,
        watched: WatchedName,
        at: dt.datetime,
        price: Decimal,
        verdict: TriggerVerdict,
        state: _NameState,
    ) -> None:
        signal = Signal(
            watch=watched,
            at=at,
            verdict=verdict,
            last_price=price,
            low_of_day=state.low_of_day or Decimal(0),
            window_minutes=self.window_minutes,
        )
        self.signals.append(signal)
        try:
            self.store.raise_signal(signal)
        except Exception as exc:  # noqa: BLE001 - the store failing must not stop the monitor
            log.error("could not record signal for %s: %s", watched.symbol, exc)
        log.info(
            "%s %s at %s entry=%s stop=%s",
            verdict.state.value,
            watched.symbol,
            at.time(),
            verdict.entry,
            verdict.stop,
        )

    def session_over(self, at: dt.datetime) -> bool:
        hour, minute = self.config.opening_range.monitor_close
        return at.time() > dt.time(hour, minute)

    async def generate_targets(self, context: dict) -> list[dict]:
        """Nothing. Ever. The monitor raises; the person confirms (docs/swing/02 Track C §3)."""
        return []


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


def _tick_time(tick: dict, day: dt.date) -> dt.datetime:
    """The tick's exchange timestamp, or the last-trade time, or now — in that order.

    Kite's full-mode tick carries `exchange_timestamp` (a naive IST datetime). A replayed tick
    carries whatever the recording did. Falling through to the wall clock is the last resort
    and is the one place this module consults it, because a tick with no time is still a tick.
    """
    for key in ("exchange_timestamp", "last_trade_time", "timestamp"):
        value = tick.get(key)
        if isinstance(value, dt.datetime):
            return value.replace(tzinfo=None)
        if isinstance(value, str):
            try:
                return dt.datetime.fromisoformat(value).replace(tzinfo=None)
            except ValueError:
                continue
    return dt.datetime.combine(day, dt.datetime.now().time())
