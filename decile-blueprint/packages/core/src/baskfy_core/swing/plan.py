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
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from baskfy_core.swing.config import TRADEABLE_SETUPS, Setup, SwingConfig
from baskfy_core.swing.market import ExposureTier, MarketGate
from baskfy_core.swing.sizing import SizedPosition, size_position
from baskfy_core.swing.stops import Action, ActionKind, TrailMa, choose_trail, widest_stop_pct

_ZERO = Decimal(0)
_PCT = Decimal(100)
_TICK = Decimal("0.05")


class LineKind(StrEnum):
    BUY_ON_TRIGGER = "BUY_ON_TRIGGER"
    SELL_AT_OPEN = "SELL_AT_OPEN"
    RAISE_GTT_STOP = "RAISE_GTT_STOP"


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
    """One name on the watchlist, with its levels as **exchange** prices."""

    symbol: str
    setup: Setup
    trigger: Decimal
    stop_ref: Decimal
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
) -> tuple[list[PlanLine], list[Skipped]]:
    """BUY lines for the watchlist, best score first, until the tier or the session is full.

    The position count is the smaller of the ladder's rung and the trader's own cap; new
    entries per session are capped at ``max_new_entries_per_session`` ("1, 2, 3 stocks per
    day"); each name's widest stop is one ADR (:func:`widest_stop_pct`).

    ``entries_already_today`` (``04`` §5.3, SW10): entries the session has *already* taken
    before this plan — the desk's confirm-time gate re-sizes one line at a time against the
    lines confirmed earlier in the morning, and those count against the same cap. The evening
    and the morning plan pass nothing: a plan is the session's first and only set of lines.
    """
    lines: list[PlanLine] = []
    skipped: list[Skipped] = []
    open_count = len(account.open_symbols)
    exposure = account.open_exposure_inr
    max_exposure = account.equity * Decimal(str(tier.max_exposure_pct)) / _PCT
    max_positions = min(tier.max_open_positions, config.sizing.max_open_positions)
    ordered = sorted(watch, key=lambda w: (-w.score, w.symbol))
    for item in ordered:
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
        if item.locked_upper_circuit:
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
        if open_count + len(lines) >= max_positions:
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
            cash_available=account.cash_available
            - sum((line.position_value for line in lines), _ZERO),
            entry=item.trigger,
            stop=item.stop_ref,
            avg_turnover_inr=item.avg_turnover_inr,
            config=config.sizing,
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
