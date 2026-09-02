"""A day's plan (docs/swing/04 §9): watch items + account → lines the desk can confirm.

This is the bridge to law 2. The plan is *advice with numbers*: for each watch item it says
the trigger (exchange price), the stop, the share count and the rupees at risk, and for each
open position what the stop rules want done at the open. The desk turns a confirmed line into
``OrderGateway.place`` + ``place_gtt_stop`` (CLAUDE.md non-negotiables 1, 4, 6); nothing here
knows a broker exists.

The plan also says what it **refused** and why. A watchlist of twelve names and a plan of two
lines is only useful if the other ten explain themselves.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final

from baskfy_core.swing.config import (
    TRADEABLE_SETUPS,
    OpeningRangeConfig,
    Setup,
    SizingConfig,
    SwingConfig,
)
from baskfy_core.swing.market import ExposureTier, MarketGate
from baskfy_core.swing.sizing import SizedPosition, size_position
from baskfy_core.swing.stops import Action, ActionKind, TrailMa, choose_trail, widest_stop_pct

_ZERO = Decimal(0)
_ONE = Decimal(1)
_PCT = Decimal(100)
_TICK = Decimal("0.05")


class LineKind(StrEnum):
    BUY_ON_TRIGGER = "BUY_ON_TRIGGER"
    SELL_AT_OPEN = "SELL_AT_OPEN"
    RAISE_GTT_STOP = "RAISE_GTT_STOP"
    #: A live gap found at 09:09 (STANDING-ANSWERS A7, SW10.5): the name is on the MORNING plan
    #: with no quantity and no stop — the opening range has not set one yet — and it reserves
    #: one of the session's new-entry slots so a lower-scored flag cannot crowd it out. It is
    #: information only: it becomes a real line through the monitor's SIGNAL plan at window
    #: close, with ``stop = min(range low, LOD)``, or it is skipped ``STOP_TOO_WIDE`` and the
    #: slot released; a slot still reserved at 10:45 is freed. Never executable.
    PENDING_RANGE = "PENDING_RANGE"


#: The kinds the desk's ``/swing/execute`` may turn into an order. ``PENDING_RANGE`` is not
#: here and must never be: a line with no quantity and no stop is not a buy (A7).
EXECUTABLE_KINDS: Final[frozenset[LineKind]] = frozenset(
    {LineKind.BUY_ON_TRIGGER, LineKind.SELL_AT_OPEN, LineKind.RAISE_GTT_STOP}
)


class SkipReason(StrEnum):
    NOT_TRADEABLE_SETUP = "NOT_TRADEABLE_SETUP"
    GATE_RED = "GATE_RED"
    DRAWDOWN_LOCKOUT = "DRAWDOWN_LOCKOUT"
    TIER_FULL = "TIER_FULL"
    SESSION_CAP = "SESSION_CAP"
    EXPOSURE_FULL = "EXPOSURE_FULL"
    ALREADY_HELD = "ALREADY_HELD"
    LOCKED_UPPER_CIRCUIT = "LOCKED_UPPER_CIRCUIT"
    SIZE_REFUSED = "SIZE_REFUSED"


@dataclass(frozen=True, slots=True)
class WatchItem:
    """One name on the watchlist, with its levels as **exchange** prices.

    ``stop_ref`` is ``None`` for a live gap found before the open (A7): the stop is the opening
    range's to set, and the plan shows the name as a ``PENDING_RANGE`` line rather than sizing
    it against a stop nobody's rule produced.
    """

    symbol: str
    setup: Setup
    trigger: Decimal
    stop_ref: Decimal | None
    adr_pct: Decimal
    avg_turnover_inr: Decimal | None
    score: Decimal
    locked_upper_circuit: bool = False


@dataclass(frozen=True, slots=True)
class SwingAccount:
    """The sleeve the swing book trades from. Rupees."""

    equity: Decimal
    cash_available: Decimal
    open_symbols: frozenset[str]
    open_exposure_inr: Decimal


@dataclass(frozen=True, slots=True)
class PlanLine:
    kind: LineKind
    symbol: str
    setup: Setup | None
    quantity: int
    trigger: Decimal | None
    stop: Decimal | None
    risk_inr: Decimal
    position_value: Decimal
    trail: TrailMa | None
    note: str


@dataclass(frozen=True, slots=True)
class Skipped:
    symbol: str
    reason: SkipReason
    detail: str


@dataclass(frozen=True, slots=True)
class SwingPlan:
    as_of: dt.date
    gate: MarketGate
    tier: ExposureTier
    lines: tuple[PlanLine, ...]
    skipped: tuple[Skipped, ...]
    total_risk_inr: Decimal
    total_new_exposure_inr: Decimal

    def plan_hash(self) -> str:
        """sha256 of the canonical lines — the desk's ``plan_id`` derives from it."""
        payload = {
            "as_of": self.as_of.isoformat(),
            "gate": self.gate.value,
            "tier": self.tier.level,
            "lines": [_jsonable(asdict(line)) for line in self.lines],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def _jsonable(payload: dict[str, object]) -> dict[str, object]:
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in payload.items()}


def to_tick(price: Decimal, tick: Decimal = _TICK) -> Decimal:
    """Snap to the exchange tick, rounding a trigger UP and a stop as given by the caller."""
    return (price / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


def first_live_multiplier(
    *, sessions_left: int, execution_enabled: bool, config: SizingConfig
) -> Decimal:
    """`02` §3.5 / STANDING-ANSWERS A9: the risk multiplier a plan is sized with.

    ``risk_multiplier_first_live`` [0.5] while the countdown is running **and** execution is
    enabled; 1 otherwise. A paper plan is full size — the paper record rehearses the rules at
    the size the rules describe; only real money starts small.
    """
    if execution_enabled and sessions_left > 0:
        return Decimal(str(config.risk_multiplier_first_live))
    return _ONE


def sizing_at(config: SizingConfig, risk_multiplier: Decimal) -> SizingConfig:
    """``config`` with ``risk_per_trade_pct`` scaled — the one place the multiplier is applied,
    before ``size_position``, so every cap and every refusal sees the real size."""
    if risk_multiplier == _ONE:
        return config
    scaled = Decimal(str(config.risk_per_trade_pct)) * risk_multiplier
    return replace(config, risk_per_trade_pct=float(scaled))


def marketable_limit(
    *, trigger: Decimal, range_high: Decimal | None, adr_pct: Decimal, config: OpeningRangeConfig
) -> Decimal:
    """STANDING-ANSWERS A8: the live buy's LIMIT price, never MARKET.

    ``min(trigger x (1 + entry_limit_buffer_pct / 100), range_high + entry_limit_max_adr x ADR)``
    — a marketable limit that chases at most half a percent past the trigger and never more
    than a quarter of a normal day's range above the opening range it broke out of (a line
    with no range — an EOD entry — reads the trigger as the range high). Snapped **down** to
    the tick so the price is never above either cap. ADR here is in rupees:
    ``adr_pct / 100 x trigger``.
    """
    chase = trigger * (_ONE + Decimal(str(config.entry_limit_buffer_pct)) / _PCT)
    base = range_high if range_high is not None else trigger
    reach = base + Decimal(str(config.entry_limit_max_adr)) * adr_pct / _PCT * trigger
    limit = min(chase, reach)
    return (limit / _TICK).to_integral_value(rounding=ROUND_DOWN) * _TICK


def _pending_line(
    item: WatchItem,
    *,
    account: SwingAccount,
    cash_left: Decimal,
    sizing: SizingConfig,
    config: SwingConfig,
) -> PlanLine:
    """A7's information-only line: no quantity, no stop, a preview at a 1-ADR stop."""
    trail = choose_trail(item.adr_pct, config.stops)
    trigger = to_tick(item.trigger)
    widest = widest_stop_pct(item.adr_pct, config.stops)
    if item.locked_upper_circuit:
        preview = "locked at the upper band: no fill until it unlocks"
    elif widest <= _ZERO:
        preview = "ADR unknown: no preview"
    else:
        sized = size_position(
            equity=account.equity,
            cash_available=cash_left,
            entry=trigger,
            stop=to_tick(trigger * (_ONE - widest / _PCT)),
            avg_turnover_inr=item.avg_turnover_inr,
            config=sizing,
            max_stop_distance_pct=widest,
        )
        preview = (
            f"≈ {sized.quantity} shares (₹{sized.risk_inr} at risk) if the stop lands 1 ADR "
            f"({widest}%) below {trigger}"
            if sized.refusal is None
            else f"no size at a 1-ADR stop: {sized.refusal.value}"
        )
    return PlanLine(
        kind=LineKind.PENDING_RANGE,
        symbol=item.symbol,
        setup=item.setup,
        quantity=0,
        trigger=trigger,
        stop=None,
        risk_inr=_ZERO,
        position_value=_ZERO,
        trail=trail,
        note=(
            f"{item.setup.value} score {item.score}; live gap, stop set by the opening range "
            f"at window close; slot reserved; preview: {preview}"
        ),
    )


def _entry_line(item: WatchItem, sized: SizedPosition, config: SwingConfig) -> PlanLine:
    trail = choose_trail(item.adr_pct, config.stops)
    return PlanLine(
        kind=LineKind.BUY_ON_TRIGGER,
        symbol=item.symbol,
        setup=item.setup,
        quantity=sized.quantity,
        trigger=to_tick(item.trigger),
        stop=to_tick(sized.stop),
        risk_inr=sized.risk_inr,
        position_value=sized.position_value,
        trail=trail,
        note=(
            f"{item.setup.value} score {item.score}; stop {sized.stop_distance_pct}% below; "
            f"size by {sized.cap.value if sized.cap else 'RISK'}; trail {trail.value}"
        ),
    )


def build_entries(  # noqa: PLR0913 - one keyword per input the plan depends on
    *,
    as_of: dt.date,
    watch: Sequence[WatchItem],
    account: SwingAccount,
    gate: MarketGate,
    tier: ExposureTier,
    config: SwingConfig,
    entries_already_today: int = 0,
    risk_multiplier: Decimal = _ONE,
) -> tuple[list[PlanLine], list[Skipped]]:
    """BUY lines for the watchlist, best score first, until the tier or the session is full.

    The position count is the smaller of the ladder's rung and the trader's own cap; new
    entries per session are capped at ``max_new_entries_per_session`` ("1, 2, 3 stocks per
    day"); each name's widest stop is one ADR (:func:`widest_stop_pct`).

    ``entries_already_today`` (``04`` §5.3, SW10): entries the session has *already* taken
    before this plan — the desk's confirm-time gate re-sizes one line at a time against the
    lines confirmed earlier in the morning, and those count against the same cap. The evening
    and the morning plan pass nothing: a plan is the session's first and only set of lines.

    ``risk_multiplier`` (A9, SW10.5): scales ``risk_per_trade_pct`` before ``size_position``
    — 0.5 for the first live sessions (:func:`first_live_multiplier`) — so the line shown is
    the line sent. Additive; 1 is the plan as it always was.

    A watch item with no ``stop_ref`` (a live gap, A7) becomes a ``PENDING_RANGE`` line after
    the same refusals a buy faces (setup, lock-out, gate, held, session cap) — no quantity, no
    stop, no cash spent — and **reserves one of the session's new-entry slots**: it counts
    against ``max_new_entries_per_session`` for the names after it. It neither counts against
    nor is refused by the rung's position count (it holds nothing yet; the SIGNAL plan at
    range close answers ``TIER_FULL`` if the book is full by then).
    """
    lines: list[PlanLine] = []
    skipped: list[Skipped] = []
    sizing = sizing_at(config.sizing, risk_multiplier)
    open_count = len(account.open_symbols)
    exposure = account.open_exposure_inr
    max_exposure = account.equity * Decimal(str(tier.max_exposure_pct)) / _PCT
    max_positions = min(tier.max_open_positions, config.sizing.max_open_positions)
    ordered = sorted(watch, key=lambda w: (-w.score, w.symbol))
    for item in ordered:
        buys = sum(1 for line in lines if line.kind is LineKind.BUY_ON_TRIGGER)
        if item.setup not in TRADEABLE_SETUPS:
            skipped.append(Skipped(item.symbol, SkipReason.NOT_TRADEABLE_SETUP, item.setup.value))
            continue
        if tier.drawdown_locked:
            skipped.append(Skipped(item.symbol, SkipReason.DRAWDOWN_LOCKOUT, "sleeve in drawdown"))
            continue
        if gate is MarketGate.RED or not tier.new_entries_allowed:
            skipped.append(Skipped(item.symbol, SkipReason.GATE_RED, gate.value))
            continue
        if item.symbol in account.open_symbols:
            skipped.append(Skipped(item.symbol, SkipReason.ALREADY_HELD, ""))
            continue
        if item.locked_upper_circuit and item.stop_ref is not None:
            skipped.append(Skipped(item.symbol, SkipReason.LOCKED_UPPER_CIRCUIT, "no fill at band"))
            continue
        if entries_already_today + len(lines) >= config.sizing.max_new_entries_per_session:
            skipped.append(
                Skipped(
                    item.symbol,
                    SkipReason.SESSION_CAP,
                    f"{config.sizing.max_new_entries_per_session} new entries per session",
                )
            )
            continue
        cash_left = account.cash_available - sum((line.position_value for line in lines), _ZERO)
        if item.stop_ref is None:
            # A7: a slot in the session, not a seat in the rung — the reservation holds no
            # position, so the rung's count is not consulted; the SIGNAL plan at range close
            # answers TIER_FULL if the book is full by then.
            lines.append(
                _pending_line(
                    item, account=account, cash_left=cash_left, sizing=sizing, config=config
                )
            )
            continue
        if open_count + buys >= max_positions:
            skipped.append(
                Skipped(
                    item.symbol,
                    SkipReason.TIER_FULL,
                    f"rung {tier.level} allows {max_positions} positions",
                )
            )
            continue
        sized = size_position(
            equity=account.equity,
            cash_available=cash_left,
            entry=item.trigger,
            stop=item.stop_ref,
            avg_turnover_inr=item.avg_turnover_inr,
            config=sizing,
            max_stop_distance_pct=widest_stop_pct(item.adr_pct, config.stops),
        )
        if sized.refusal is not None:
            skipped.append(Skipped(item.symbol, SkipReason.SIZE_REFUSED, sized.refusal.value))
            continue
        if exposure + sized.position_value > max_exposure:
            skipped.append(
                Skipped(
                    item.symbol, SkipReason.EXPOSURE_FULL, f"{tier.max_exposure_pct}% of sleeve"
                )
            )
            continue
        exposure += sized.position_value
        lines.append(_entry_line(item, sized, config))
    return lines, skipped


def exit_lines(symbol: str, actions: Sequence[Action]) -> list[PlanLine]:
    """The stop rules' actions for one open position, as plan lines for the open."""
    lines: list[PlanLine] = []
    for action in actions:
        if action.kind in (ActionKind.SELL_PARTIAL, ActionKind.SELL_ALL):
            lines.append(
                PlanLine(
                    kind=LineKind.SELL_AT_OPEN,
                    symbol=symbol,
                    setup=None,
                    quantity=action.quantity,
                    trigger=None,
                    stop=None,
                    risk_inr=_ZERO,
                    position_value=_ZERO,
                    trail=None,
                    note=action.reason.value,
                )
            )
        elif action.kind is ActionKind.RAISE_STOP and action.new_stop is not None:
            lines.append(
                PlanLine(
                    kind=LineKind.RAISE_GTT_STOP,
                    symbol=symbol,
                    setup=None,
                    quantity=0,
                    trigger=None,
                    stop=to_tick(action.new_stop),
                    risk_inr=_ZERO,
                    position_value=_ZERO,
                    trail=None,
                    note=action.reason.value,
                )
            )
    return lines


def assemble(  # noqa: PLR0913 - the plan's parts, named
    *,
    as_of: dt.date,
    gate: MarketGate,
    tier: ExposureTier,
    entries: Sequence[PlanLine],
    exits: Sequence[PlanLine],
    skipped: Sequence[Skipped],
) -> SwingPlan:
    """Exits first — the money they free is the money the entries spend."""
    lines = tuple(exits) + tuple(entries)
    return SwingPlan(
        as_of=as_of,
        gate=gate,
        tier=tier,
        lines=lines,
        skipped=tuple(skipped),
        total_risk_inr=sum((line.risk_inr for line in entries), _ZERO),
        total_new_exposure_inr=sum((line.position_value for line in entries), _ZERO),
    )
