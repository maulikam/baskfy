"""Portfolio-aware selection over a quality rank — does not mutate SCORE.

Two layers live here, and neither writes back into the rank it reads.

* **Phase 1.4** — :func:`select_from_ranks` overlays holdings on a quality rank through the one
  hold-band rule in :mod:`baskfy_core.rank_buffer`. Kept as it was.
* **Phase 2, contract C5** (docs/ranking/PLAN.md) — :func:`select_portfolio`, the
  portfolio-construction view: retention and entry limits, a sector cap, a liquidity (capacity)
  cap, a correlation cap, a turnover budget and a name limit, every decision carrying
  machine-readable reason codes and a sentence a person can read.

Hard rules
----------
* Selection **must not mutate SCORE** (or any quality column). Ranks and scores are inputs; the
  scorer is upstream. Every input type is a frozen dataclass, and the property test in
  ``tests/test_ranking_selection.py`` proves ranks and scores come out byte-identical.
* **Informational only.** Nothing here places, plans or routes an order. The output is a
  proposal for a person to read. Desk non-negotiable #1 is untouched.
* Does not invent a second hold-band formula — the retention test calls
  :func:`baskfy_core.rank_buffer.inside_hold_band` with ``top_n = entry_rank`` and
  ``hold_buffer = retention_rank - entry_rank``.
* Pure: no database, no network, no disk, no clock (root CLAUDE.md, law 1).

``select_portfolio`` — the rules, in order
------------------------------------------
Matching is on ``instrument_id``, never on symbol (NSE renames symbols).

1. **Holdings.** A holding whose candidate row has ``quality_rank <= retention_rank`` is kept
   (``hold``, ``RANK_WITHIN_RETENTION``). One ranked worse would exit
   (``RANK_OUTSIDE_RETENTION``); one absent from ``candidates`` would exit (``NOT_IN_RESULTS``).
2. **Turnover budget on exits.** ``turnover_budget_names`` caps entries + exits. When the
   would-exit holdings alone exceed it, the **worst** are exited first — ``NOT_IN_RESULTS``
   before any ranked name, then rank descending, then ``instrument_id`` descending (the engine
   breaks rank ties by ascending id, so the higher id is the worse of a tie). The remaining,
   better-ranked would-exits are kept as ``hold`` with ``RETAINED_BY_TURNOVER_BUDGET`` followed
   by the reason they would have exited. Entries then get whatever budget the exits left.
3. **Entries.** Candidates not held, with ``quality_rank <= entry_rank``, are considered in
   ``(quality_rank, instrument_id)`` order. Each is ``enter`` (``RANK_WITHIN_ENTRY``) unless a cap
   refuses it; a refused one is ``skip`` and carries **every** refusal that applied, in this
   fixed order: ``SECTOR_CAP``, ``CAPACITY``, ``CORRELATION``, ``TURNOVER_BUDGET``, ``FULL``.
   A skipped name consumes nothing — no slot, no sector count, no budget, and it is not a
   correlation peer for later entrants. Names ranked worse than ``entry_rank`` are never
   considered, so skips can leave the book short (``unfilled_slots``); selection does not dig
   deeper into the rank to fill it, because that would enter a name the entry rule refused.

The caps
--------
* **SECTOR_CAP** — ``max_per_sector``: refused when the entrant's sector already holds that many
  names, counting kept holdings (retained ones included) and entrants accepted so far. A
  holding's sector is its own ``sector`` field, falling back to its candidate row's when that is
  unclassified. ``None``, an empty string and ``"unclassified"`` (the ranking engine's label for
  a name with no point-in-time sectoral index) never count against a cap and are never refused
  by one; the entrant is flagged ``SECTOR_UNCLASSIFIED`` instead, so the gap is visible.
  Kept holdings are never exited to satisfy a cap; a sector already over it from holdings alone
  produces the result note ``SECTOR_OVER_CAP_FROM_HOLDINGS``.
* **CAPACITY** — needs ``capital_inr`` and ``max_adv_participation_pct``. The desk's liquidity
  cap (``MAX_POS_VS_DAY_VALUE`` = 1% of median daily traded value, :mod:`baskfy_core.basket`)
  expressed as a percentage. Proposed value = ``capital_inr / max_names`` (equal weight, rounded
  **down** to the paisa so ``max_names`` slots never exceed capital); proposed quantity =
  ``floor(value / close_raw)``; participation = ``value / adv_value_inr * 100`` rounded half-up
  to 4 dp. Refused when that **rounded** participation is strictly greater than the cap, so the
  figure on the row and the decision can never disagree (house rule 8), and a participation
  exactly at the cap is allowed, as the desk allows it (``>`` in ``basket.py``).
  ``adv_value_inr`` of zero is refused (no traded value; participation is unbounded).
  ``adv_value_inr`` **missing** is *not* refused — the entrant is flagged ``CAPACITY_UNKNOWN``.
  The desk treats a missing day value the same way (``np.inf``, no cap), a missing 12-month
  median usually means under a year of listing rather than illiquidity, and a refusal on missing
  data would be a verdict the data cannot support. ``close_raw`` missing only removes the
  quantity (``PRICE_UNKNOWN``); a slot too small for one share flags ``PRICE_ABOVE_SLOT_VALUE``.
  Without ``capital_inr`` (or with ``max_adv_participation_pct=None``) capacity is not checked
  and the result says so (``CAPACITY_NOT_CHECKED``).
* **CORRELATION** — needs ``max_correlation`` and ``returns``: a DataFrame of **daily simple
  returns**, indexed by date, one column per ``instrument_id`` (int). The last
  ``correlation_window`` rows (after sorting by date) are the window; each pair is Pearson over
  the dates where **both** are present and finite (pairwise-complete), and needs at least
  :data:`MIN_CORRELATION_OBSERVATIONS` (60) of them. Peers are kept holdings (retained included)
  and entrants accepted so far — never exiting holdings. Refused when the highest correlation to
  any peer, rounded to 4 dp, is strictly greater than the limit. The limit is on the **signed**
  coefficient, not its absolute value: in a long-only book a strongly negatively correlated
  name diversifies, and refusing it would refuse the hedge. A pair short of history is skipped
  and the entrant flagged ``CORRELATION_HISTORY_SHORT`` (never refused for it); a pair with a
  constant return series has no defined coefficient and flags ``CORRELATION_UNDEFINED``.
  ``max_correlation`` without ``returns`` raises — a requested check with no data is a caller
  bug, not a pass.
* **TURNOVER_BUDGET** — refused when accepted entries already use the budget left after exits.
* **FULL** — refused when kept holdings + accepted entrants already reach ``max_names``. Kept
  holdings are never trimmed to ``max_names``; when they alone exceed it the result carries
  ``HOLDINGS_EXCEED_MAX_NAMES``.

Every ``Decimal`` in :meth:`SelectionResult.to_dict` is a string (exact, JSON-safe); floats stay
floats; enums are their string values.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final

import numpy as np
import pandas as pd

from baskfy_core.rank_buffer import (
    HeldName,
    RebalancePlan,
    ScreenRank,
    inside_hold_band,
    plan_rebalance,
)

__all__ = [
    "MIN_CORRELATION_OBSERVATIONS",
    "SELECTION_VERSION",
    "UNCLASSIFIED_SECTOR",
    "Candidate",
    "Holding",
    "QualityRow",
    "SelectionAction",
    "SelectionConstraints",
    "SelectionFlag",
    "SelectionInput",
    "SelectionNote",
    "SelectionReason",
    "SelectionResult",
    "SelectionRow",
    "SelectionWarning",
    "assert_score_unchanged",
    "held_from_quantities",
    "quality_rows_from_scored",
    "select_from_ranks",
    "select_portfolio",
]

SCORE_COLUMN: Final = "SCORE"

#: Bumped whenever a rule in :func:`select_portfolio` changes meaning.
SELECTION_VERSION: Final = "selection-2.0.0"

#: Fewer overlapping daily returns than this and a pair's correlation is not trusted.
MIN_CORRELATION_OBSERVATIONS: Final = 60

#: The ranking engine's label for a name with no point-in-time sectoral index (C4).
UNCLASSIFIED_SECTOR: Final = "unclassified"

#: Money at the paisa; participation at 4 dp; correlation at 4 dp.
_VALUE_STEP: Final = Decimal("0.01")
_PARTICIPATION_STEP: Final = Decimal("0.0001")
_CORRELATION_DIGITS: Final = 4
_HUNDRED: Final = Decimal(100)


# ---------------------------------------------------------------------------------------------
# Phase 1.4 — unchanged
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QualityRow:
    """One ranked name. ``score`` is informational; selection keys only on ``rank``."""

    instrument_id: int
    symbol: str
    name: str
    rank: int
    score: float | None = None


@dataclass(frozen=True, slots=True)
class SelectionInput:
    """Quality ranks + holdings. Selection never writes back into ``ranked``."""

    ranked: Sequence[QualityRow]
    holdings: Sequence[HeldName]
    top_n: int
    hold_buffer: int


def select_from_ranks(inp: SelectionInput) -> RebalancePlan:
    """Overlay holdings on a quality rank. Does not mutate SCORE.

    Delegates to :func:`plan_rebalance` so the web portfolio path and the ranking engine share
    one hold-band rule.
    """
    screen = tuple(
        ScreenRank(
            instrument_id=row.instrument_id,
            symbol=row.symbol,
            name=row.name,
            rank=row.rank,
        )
        for row in inp.ranked
    )
    return plan_rebalance(
        screen,
        tuple(inp.holdings),
        top_n=inp.top_n,
        hold_buffer=inp.hold_buffer,
    )


def assert_score_unchanged(
    before: Mapping[str, float | None] | pd.DataFrame,
    after: Mapping[str, float | None] | pd.DataFrame,
    *,
    score_column: str = SCORE_COLUMN,
) -> None:
    """Raise if selection (or anything) changed quality scores.

    Callers that hold a scored DataFrame should snapshot SCORE before selection and pass both
    frames here. Selection itself never touches the frame; this is the contract pin.
    """
    if isinstance(before, pd.DataFrame) or isinstance(after, pd.DataFrame):
        if not (isinstance(before, pd.DataFrame) and isinstance(after, pd.DataFrame)):
            raise TypeError("compare a frame with a frame, or a score map with a score map")
        if score_column not in before.columns:
            raise ValueError(f"before frame has no {score_column!r} column")
        if score_column not in after.columns:
            raise ValueError(f"after frame has no {score_column!r} column")
        left = before[score_column]
        right = after[score_column]
        if not left.equals(right):
            raise AssertionError(f"{score_column} changed during selection — forbidden")
        return

    if set(before) != set(after):
        raise AssertionError("score maps have different symbol keys")
    for symbol, value in before.items():
        if after[symbol] != value:
            raise AssertionError(
                f"{score_column} for {symbol!r} changed: {value!r} -> {after[symbol]!r}"
            )


def quality_rows_from_scored(
    scored: pd.DataFrame,
    *,
    score_column: str = SCORE_COLUMN,
    rank_column: str = "screen_rank",
) -> tuple[QualityRow, ...]:
    """Build selection inputs from a frame ``rerank_survivors_by_desk_score`` already ordered."""
    if rank_column not in scored.columns and "rank" in scored.columns:
        rank_column = "rank"
    rows: list[QualityRow] = []
    for position, (_, row) in enumerate(scored.iterrows(), start=1):
        rank_val = row[rank_column] if rank_column in scored.columns else position
        score_val = row[score_column] if score_column in scored.columns else None
        score: float | None
        if score_val is None or (isinstance(score_val, float) and pd.isna(score_val)):
            score = None
        else:
            score = float(score_val)
        rows.append(
            QualityRow(
                instrument_id=int(row["instrument_id"]),
                symbol=str(row["symbol"]),
                name=str(row.get("name", row["symbol"])),
                rank=int(rank_val),
                score=score,
            )
        )
    return tuple(rows)


def held_from_quantities(
    holdings: Mapping[str, Decimal | int],
    *,
    id_by_symbol: Mapping[str, int],
    name_by_symbol: Mapping[str, str] | None = None,
) -> tuple[HeldName, ...]:
    """Convenience for tests: symbol → quantity map to :class:`HeldName`."""
    names = name_by_symbol or {}
    return tuple(
        HeldName(
            instrument_id=id_by_symbol[symbol],
            symbol=symbol,
            name=names.get(symbol, symbol),
            quantity=Decimal(quantity),
        )
        for symbol, quantity in holdings.items()
    )


# ---------------------------------------------------------------------------------------------
# Phase 2 — contract C5
# ---------------------------------------------------------------------------------------------


class SelectionAction(StrEnum):
    """What the proposal says to do with a name."""

    HOLD = "hold"
    EXIT = "exit"
    ENTER = "enter"
    SKIP = "skip"


class SelectionReason(StrEnum):
    """Why a row has its action. The first reason on a row is the deciding one."""

    #: hold — ranked inside ``retention_rank``.
    RANK_WITHIN_RETENTION = "RANK_WITHIN_RETENTION"
    #: exit (or retained hold) — ranked worse than ``retention_rank``.
    RANK_OUTSIDE_RETENTION = "RANK_OUTSIDE_RETENTION"
    #: exit (or retained hold) — not in the screen's results at all.
    NOT_IN_RESULTS = "NOT_IN_RESULTS"
    #: hold — would exit, but the turnover budget was spent on worse-ranked exits.
    RETAINED_BY_TURNOVER_BUDGET = "RETAINED_BY_TURNOVER_BUDGET"
    #: enter — ranked inside ``entry_rank`` and no cap refused it.
    RANK_WITHIN_ENTRY = "RANK_WITHIN_ENTRY"
    #: skip — the refusals, in the fixed order they are reported.
    SECTOR_CAP = "SECTOR_CAP"
    CAPACITY = "CAPACITY"
    CORRELATION = "CORRELATION"
    TURNOVER_BUDGET = "TURNOVER_BUDGET"
    FULL = "FULL"


class SelectionFlag(StrEnum):
    """A data-quality or sizing caveat that did not change the row's action."""

    SECTOR_UNCLASSIFIED = "SECTOR_UNCLASSIFIED"
    CAPACITY_UNKNOWN = "CAPACITY_UNKNOWN"
    HOLD_ABOVE_CAPACITY = "HOLD_ABOVE_CAPACITY"
    PRICE_UNKNOWN = "PRICE_UNKNOWN"
    PRICE_ABOVE_SLOT_VALUE = "PRICE_ABOVE_SLOT_VALUE"
    CORRELATION_HISTORY_SHORT = "CORRELATION_HISTORY_SHORT"
    CORRELATION_UNDEFINED = "CORRELATION_UNDEFINED"


class SelectionWarning(StrEnum):
    """A result-level caveat."""

    HOLDINGS_EXCEED_MAX_NAMES = "HOLDINGS_EXCEED_MAX_NAMES"
    SECTOR_OVER_CAP_FROM_HOLDINGS = "SECTOR_OVER_CAP_FROM_HOLDINGS"
    CAPACITY_NOT_CHECKED = "CAPACITY_NOT_CHECKED"


_REFUSAL_ORDER: Final = (
    SelectionReason.SECTOR_CAP,
    SelectionReason.CAPACITY,
    SelectionReason.CORRELATION,
    SelectionReason.TURNOVER_BUDGET,
    SelectionReason.FULL,
)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One ranked screen row. ``quality_rank`` and ``score`` are read, never written.

    ``adv_value_inr`` is the 12-month median daily traded value in rupees (``median_vol_12m``);
    ``close_raw`` is the exchange print used for the proposed quantity.
    """

    instrument_id: int
    symbol: str
    quality_rank: int
    score: float | Decimal | None = None
    sector: str | None = None
    adv_value_inr: Decimal | None = None
    close_raw: Decimal | None = None

    def __post_init__(self) -> None:
        if self.quality_rank < 1:
            raise ValueError(f"{self.symbol}: quality_rank must be at least 1")
        if isinstance(self.score, float) and not math.isfinite(self.score):
            raise ValueError(f"{self.symbol}: score must be finite; pass None for a missing score")
        if isinstance(self.score, Decimal) and not self.score.is_finite():
            raise ValueError(f"{self.symbol}: score must be finite; pass None for a missing score")
        if self.adv_value_inr is not None and not (
            self.adv_value_inr.is_finite() and self.adv_value_inr >= 0
        ):
            raise ValueError(f"{self.symbol}: adv_value_inr must be a finite value >= 0")
        if self.close_raw is not None and not (self.close_raw.is_finite() and self.close_raw > 0):
            raise ValueError(f"{self.symbol}: close_raw must be a finite price > 0")


@dataclass(frozen=True, slots=True)
class Holding:
    """One held position. ``quantity`` is the desk's full count (quantity + T1 + collateral)."""

    instrument_id: int
    symbol: str
    quantity: Decimal
    sector: str | None = None

    def __post_init__(self) -> None:
        if not (self.quantity.is_finite() and self.quantity > 0):
            raise ValueError(f"{self.symbol}: a holding's quantity must be > 0")


@dataclass(frozen=True, slots=True)
class SelectionConstraints:
    """The C5 knobs. Defaults are the contract's."""

    max_names: int = 15
    entry_rank: int = 15
    retention_rank: int = 30
    max_per_sector: int | None = None
    capital_inr: Decimal | None = None
    max_adv_participation_pct: Decimal | None = Decimal("1.0")
    turnover_budget_names: int | None = None
    max_correlation: float | None = None
    correlation_window: int = 126

    def __post_init__(self) -> None:
        if self.max_names < 1:
            raise ValueError("max_names must be at least 1")
        if self.entry_rank < 1:
            raise ValueError("entry_rank must be at least 1")
        if self.retention_rank < self.entry_rank:
            # The hold band would be negative: a name entered at entry_rank would exit on the
            # very next run without its rank moving. rank_buffer refuses the same thing.
            raise ValueError("retention_rank must be >= entry_rank")
        if self.max_per_sector is not None and self.max_per_sector < 1:
            raise ValueError("max_per_sector must be at least 1 (or None for no cap)")
        if self.capital_inr is not None and not (
            self.capital_inr.is_finite() and self.capital_inr > 0
        ):
            raise ValueError("capital_inr must be > 0 (or None)")
        if self.max_adv_participation_pct is not None and not (
            self.max_adv_participation_pct.is_finite() and self.max_adv_participation_pct > 0
        ):
            raise ValueError("max_adv_participation_pct must be > 0 (or None for no cap)")
        if self.turnover_budget_names is not None and self.turnover_budget_names < 0:
            raise ValueError("turnover_budget_names must be >= 0 (or None for no budget)")
        if self.max_correlation is not None and not (
            math.isfinite(self.max_correlation) and -1.0 <= self.max_correlation <= 1.0
        ):
            raise ValueError("max_correlation must be within [-1, 1] (or None)")
        if self.correlation_window < MIN_CORRELATION_OBSERVATIONS:
            raise ValueError(
                f"correlation_window must be at least {MIN_CORRELATION_OBSERVATIONS} sessions"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "max_names": self.max_names,
            "entry_rank": self.entry_rank,
            "retention_rank": self.retention_rank,
            "max_per_sector": self.max_per_sector,
            "capital_inr": _dec(self.capital_inr),
            "max_adv_participation_pct": _dec(self.max_adv_participation_pct),
            "turnover_budget_names": self.turnover_budget_names,
            "max_correlation": self.max_correlation,
            "correlation_window": self.correlation_window,
        }


@dataclass(frozen=True, slots=True)
class SelectionRow:
    """One name in the proposal. ``quality_rank`` and ``score`` are the candidate's, verbatim."""

    instrument_id: int
    symbol: str
    action: SelectionAction
    reasons: tuple[SelectionReason, ...]
    flags: tuple[SelectionFlag, ...]
    explanation: str
    quality_rank: int | None
    score: float | Decimal | None
    sector: str | None
    current_quantity: Decimal | None
    proposed_value_inr: Decimal | None
    proposed_qty: int | None
    adv_participation_pct: Decimal | None
    max_correlation: float | None
    correlation_peer: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "action": self.action.value,
            "reasons": [reason.value for reason in self.reasons],
            "flags": [flag.value for flag in self.flags],
            "explanation": self.explanation,
            "quality_rank": self.quality_rank,
            "score": _dec(self.score) if isinstance(self.score, Decimal) else self.score,
            "sector": self.sector,
            "current_quantity": _dec(self.current_quantity),
            "proposed_value_inr": _dec(self.proposed_value_inr),
            "proposed_qty": self.proposed_qty,
            "adv_participation_pct": _dec(self.adv_participation_pct),
            "max_correlation": self.max_correlation,
            "correlation_peer": self.correlation_peer,
        }


@dataclass(frozen=True, slots=True)
class SelectionNote:
    """A result-level warning with its sentence."""

    code: SelectionWarning
    message: str


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """The proposal. Informational only — it carries no plan and reaches no order path."""

    constraints: SelectionConstraints
    rows: tuple[SelectionRow, ...]
    kept_count: int
    entry_count: int
    exit_count: int
    skip_count: int
    turnover_used: int
    unfilled_slots: int
    #: Post-selection names per sector (kept + entering), sorted by sector; unclassified names
    #: are counted under ``"unclassified"`` and never capped.
    sector_counts: tuple[tuple[str, int], ...]
    notes: tuple[SelectionNote, ...]
    version: str = SELECTION_VERSION

    def _with(self, action: SelectionAction) -> tuple[SelectionRow, ...]:
        return tuple(row for row in self.rows if row.action is action)

    @property
    def holds(self) -> tuple[SelectionRow, ...]:
        return self._with(SelectionAction.HOLD)

    @property
    def exits(self) -> tuple[SelectionRow, ...]:
        return self._with(SelectionAction.EXIT)

    @property
    def entries(self) -> tuple[SelectionRow, ...]:
        return self._with(SelectionAction.ENTER)

    @property
    def skips(self) -> tuple[SelectionRow, ...]:
        return self._with(SelectionAction.SKIP)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "informational_only": True,
            "constraints": self.constraints.to_dict(),
            "summary": {
                "kept": self.kept_count,
                "entries": self.entry_count,
                "exits": self.exit_count,
                "skips": self.skip_count,
                "turnover_used": self.turnover_used,
                "turnover_budget": self.constraints.turnover_budget_names,
                "unfilled_slots": self.unfilled_slots,
                "sector_counts": dict(self.sector_counts),
            },
            "notes": [{"code": note.code.value, "message": note.message} for note in self.notes],
            "rows": [row.to_dict() for row in self.rows],
        }


# --- internals -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Sizing:
    value: Decimal | None
    qty: int | None
    participation: Decimal | None
    over_cap: bool
    flags: tuple[SelectionFlag, ...]


@dataclass(frozen=True, slots=True)
class _Correlation:
    value: float | None
    peer: str | None
    short: int
    undefined: int


@dataclass(frozen=True, slots=True)
class _Kept:
    holding: Holding
    candidate: Candidate | None
    #: ``None`` for an ordinary hold; the would-exit reason for a budget-retained one.
    retained_from: SelectionReason | None


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _classified(sector: str | None) -> str | None:
    """The sector a cap can count, or ``None`` for an unclassified name."""
    if sector is None:
        return None
    text = sector.strip()
    if not text or text == UNCLASSIFIED_SECTOR:
        return None
    return sector


def _holding_sector(holding: Holding, candidate: Candidate | None) -> str | None:
    """A holding's own sector, falling back to its candidate row's when that is unclassified."""
    own = _classified(holding.sector)
    if own is not None or candidate is None:
        return own
    return _classified(candidate.sector)


def _refuse_duplicates(candidates: Sequence[Candidate], holdings: Sequence[Holding]) -> None:
    for label, ids in (
        ("candidates", [c.instrument_id for c in candidates]),
        ("holdings", [h.instrument_id for h in holdings]),
    ):
        seen: set[int] = set()
        for instrument_id in ids:
            if instrument_id in seen:
                raise ValueError(f"duplicate instrument_id {instrument_id} in {label}")
            seen.add(instrument_id)


def _worst_first(item: tuple[Holding, Candidate | None, SelectionReason]) -> tuple[int, int, int]:
    """Exit order under a turnover budget: unranked first, then rank desc, then id desc."""
    holding, candidate, _ = item
    if candidate is None:
        return (0, 0, -holding.instrument_id)
    return (1, -candidate.quality_rank, -holding.instrument_id)


def _size(candidate: Candidate, slot_value: Decimal | None, cap: Decimal | None) -> _Sizing:
    if slot_value is None:
        return _Sizing(None, None, None, over_cap=False, flags=())
    flags: list[SelectionFlag] = []
    qty: int | None = None
    if candidate.close_raw is None:
        flags.append(SelectionFlag.PRICE_UNKNOWN)
    else:
        qty = int((slot_value / candidate.close_raw).to_integral_value(rounding=ROUND_DOWN))
        if qty == 0:
            flags.append(SelectionFlag.PRICE_ABOVE_SLOT_VALUE)
    participation: Decimal | None = None
    over_cap = False
    if candidate.adv_value_inr is None:
        flags.append(SelectionFlag.CAPACITY_UNKNOWN)
    elif candidate.adv_value_inr == 0:
        over_cap = cap is not None
    else:
        participation = (slot_value / candidate.adv_value_inr * _HUNDRED).quantize(
            _PARTICIPATION_STEP, rounding=ROUND_HALF_UP
        )
        over_cap = cap is not None and participation > cap
    return _Sizing(slot_value, qty, participation, over_cap=over_cap, flags=tuple(flags))


_ReturnsWindow = Mapping[int, "np.ndarray[tuple[int], np.dtype[np.float64]]"]


def _returns_window(returns: pd.DataFrame, rows: int) -> _ReturnsWindow:
    """The last ``rows`` dates of ``returns``, one float array per instrument, NaN for missing.

    Built once per call so each pair costs two array lookups, not a frame slice. The caller's
    frame is never touched: ``sort_index`` and ``to_numpy`` both return new objects.
    """
    if returns.index.has_duplicates:
        raise ValueError("returns has duplicate dates in its index")
    if returns.columns.has_duplicates:
        raise ValueError("returns has duplicate instrument columns")
    tail = returns.sort_index().iloc[-rows:]
    window: dict[int, np.ndarray[tuple[int], np.dtype[np.float64]]] = {}
    for column in tail.columns:
        values = tail[column].to_numpy(dtype="float64", na_value=np.nan)
        window[int(column)] = np.where(np.isfinite(values), values, np.nan)
    return window


def _pair_correlation(window: _ReturnsWindow, left: int, right: int) -> tuple[str, float | None]:
    """``("ok", r)``, ``("short", None)`` or ``("undefined", None)`` for one pair."""
    x_all = window.get(left)
    y_all = window.get(right)
    if x_all is None or y_all is None:
        return ("short", None)
    both = ~np.isnan(x_all) & ~np.isnan(y_all)
    if int(both.sum()) < MIN_CORRELATION_OBSERVATIONS:
        return ("short", None)
    x = x_all[both]
    y = y_all[both]
    if float(np.std(x)) == 0.0 or float(np.std(y)) == 0.0:
        return ("undefined", None)
    return ("ok", round(float(np.corrcoef(x, y)[0, 1]), _CORRELATION_DIGITS))


def _max_correlation(
    window: _ReturnsWindow | None, candidate: Candidate, peers: Sequence[tuple[int, str]]
) -> _Correlation:
    if window is None:
        return _Correlation(None, None, 0, 0)
    best: float | None = None
    best_peer: str | None = None
    short = 0
    undefined = 0
    for peer_id, peer_symbol in peers:
        status, value = _pair_correlation(window, candidate.instrument_id, peer_id)
        if status == "short":
            short += 1
        elif status == "undefined":
            undefined += 1
        elif value is not None and (best is None or value > best):
            best, best_peer = value, peer_symbol
    return _Correlation(best, best_peer, short, undefined)


def _names(count: int | None) -> str:
    return "1 name" if count == 1 else f"{count} names"


def _inr(value: Decimal) -> str:
    return f"₹{value:,.2f}"


def _pct(value: Decimal) -> str:
    return f"{value.normalize():f}%"


def _row_order(row: SelectionRow) -> tuple[int, int, str, int]:
    if row.quality_rank is None:
        return (1, 0, row.symbol, row.instrument_id)
    return (0, row.quality_rank, "", row.instrument_id)


@dataclass(slots=True)
class _Book:
    """The running state of the post-selection book while entrants are considered."""

    kept_total: int
    entry_allowance: int | None
    sector_counts: dict[str, int]
    unclassified: int
    peers: list[tuple[int, str]]
    accepted: int = 0

    def add(self, instrument_id: int, symbol: str, sector: str | None) -> None:
        if sector is None:
            self.unclassified += 1
        else:
            self.sector_counts[sector] = self.sector_counts.get(sector, 0) + 1
        self.peers.append((instrument_id, symbol))


def select_portfolio(
    candidates: Iterable[Candidate],
    holdings: Iterable[Holding],
    constraints: SelectionConstraints,
    returns: pd.DataFrame | None = None,
) -> SelectionResult:
    """Propose holds, exits, entries and skips for a ranked screen. Contract C5.

    The rules and every cap are specified in this module's docstring. Informational only: the
    result names no order and carries no plan identifier. ``candidates`` and ``returns`` are
    read and never modified.
    """
    cands = tuple(candidates)
    held = tuple(holdings)
    _refuse_duplicates(cands, held)
    c = constraints
    if c.max_correlation is not None and returns is None:
        raise ValueError("max_correlation is set but no returns frame was given")

    by_id = {candidate.instrument_id: candidate for candidate in cands}
    kept, exiting, entry_allowance = _split_holdings(held, by_id, c)

    slot_value: Decimal | None = None
    if c.capital_inr is not None:
        slot_value = (c.capital_inr / Decimal(c.max_names)).quantize(
            _VALUE_STEP, rounding=ROUND_DOWN
        )
    window = (
        _returns_window(returns, c.correlation_window)
        if c.max_correlation is not None and returns is not None
        else None
    )

    book = _Book(
        kept_total=len(kept),
        entry_allowance=entry_allowance,
        sector_counts={},
        unclassified=0,
        peers=[],
    )
    rows: list[SelectionRow] = []
    for item in kept:
        sector = _holding_sector(item.holding, item.candidate)
        book.add(item.holding.instrument_id, item.holding.symbol, sector)
        rows.append(_hold_row(item, sector, c, slot_value))
    rows.extend(_exit_row(holding, candidate, reason, c) for holding, candidate, reason in exiting)
    notes = _notes(book, c, slot_value)

    held_ids = {holding.instrument_id for holding in held}
    entrants = sorted(
        (
            candidate
            for candidate in cands
            if candidate.instrument_id not in held_ids and candidate.quality_rank <= c.entry_rank
        ),
        key=lambda candidate: (candidate.quality_rank, candidate.instrument_id),
    )
    for candidate in entrants:  # in order: each accepted entrant changes the book for the next
        rows.append(_entrant_row(candidate, book, c, slot_value, window))

    counts = dict(book.sector_counts)
    if book.unclassified:
        counts[UNCLASSIFIED_SECTOR] = counts.get(UNCLASSIFIED_SECTOR, 0) + book.unclassified
    ordered_rows = tuple(sorted(rows, key=_row_order))
    return SelectionResult(
        constraints=c,
        rows=ordered_rows,
        kept_count=book.kept_total,
        entry_count=book.accepted,
        exit_count=len(exiting),
        skip_count=sum(1 for row in ordered_rows if row.action is SelectionAction.SKIP),
        turnover_used=book.accepted + len(exiting),
        unfilled_slots=max(0, c.max_names - book.kept_total - book.accepted),
        sector_counts=tuple(sorted(counts.items())),
        notes=tuple(notes),
    )


_WouldExit = tuple[Holding, Candidate | None, SelectionReason]


def _split_holdings(
    held: Sequence[Holding], by_id: Mapping[int, Candidate], c: SelectionConstraints
) -> tuple[list[_Kept], list[_WouldExit], int | None]:
    """Rules 1 and 2: kept holdings, exits after the turnover budget, entry budget left."""
    band = c.retention_rank - c.entry_rank
    kept: list[_Kept] = []
    would_exit: list[_WouldExit] = []
    for holding in held:
        candidate = by_id.get(holding.instrument_id)
        if candidate is None:
            would_exit.append((holding, None, SelectionReason.NOT_IN_RESULTS))
        elif inside_hold_band(candidate.quality_rank, c.entry_rank, band):
            kept.append(_Kept(holding, candidate, None))
        else:
            would_exit.append((holding, candidate, SelectionReason.RANK_OUTSIDE_RETENTION))

    exiting = sorted(would_exit, key=_worst_first)
    if c.turnover_budget_names is None:
        return kept, exiting, None
    retained = exiting[c.turnover_budget_names :]
    exiting = exiting[: c.turnover_budget_names]
    kept.extend(_Kept(holding, candidate, reason) for holding, candidate, reason in retained)
    return kept, exiting, c.turnover_budget_names - len(exiting)


def _notes(book: _Book, c: SelectionConstraints, slot_value: Decimal | None) -> list[SelectionNote]:
    """Result-level warnings about the book as the holdings leave it, before any entry."""
    notes: list[SelectionNote] = []
    if book.kept_total > c.max_names:
        notes.append(
            SelectionNote(
                SelectionWarning.HOLDINGS_EXCEED_MAX_NAMES,
                f"{book.kept_total} holdings are kept, more than max_names ({c.max_names}); "
                "kept holdings are never trimmed, so no entries fit.",
            )
        )
    if c.max_per_sector is not None:
        cap = c.max_per_sector
        over = sorted(sector for sector, n in book.sector_counts.items() if n > cap)
        if over:
            notes.append(
                SelectionNote(
                    SelectionWarning.SECTOR_OVER_CAP_FROM_HOLDINGS,
                    f"Kept holdings alone exceed the cap of {cap} per sector in: "
                    f"{', '.join(over)}. Holdings are never exited for a cap.",
                )
            )
    if slot_value is None:
        notes.append(
            SelectionNote(
                SelectionWarning.CAPACITY_NOT_CHECKED,
                "Capacity was not checked and no sizes are proposed: capital_inr was not given.",
            )
        )
    elif c.max_adv_participation_pct is None:
        notes.append(
            SelectionNote(
                SelectionWarning.CAPACITY_NOT_CHECKED,
                "Capacity was not checked: max_adv_participation_pct was not given.",
            )
        )
    return notes


def _entrant_row(
    candidate: Candidate,
    book: _Book,
    c: SelectionConstraints,
    slot_value: Decimal | None,
    window: _ReturnsWindow | None,
) -> SelectionRow:
    """Rule 3 for one entrant: collect every refusal, accept into ``book`` when there is none."""
    clauses: dict[SelectionReason, str] = {}
    flags: list[SelectionFlag] = []

    sector = _classified(candidate.sector)
    if c.max_per_sector is not None:
        in_sector = book.sector_counts.get(sector, 0) if sector is not None else 0
        if sector is None:
            flags.append(SelectionFlag.SECTOR_UNCLASSIFIED)
        elif in_sector >= c.max_per_sector:
            clauses[SelectionReason.SECTOR_CAP] = (
                f"{sector} already has {_names(in_sector)}, the cap is {c.max_per_sector}"
            )

    sizing = _size(candidate, slot_value, c.max_adv_participation_pct)
    flags.extend(sizing.flags)
    capacity = _capacity_clause(sizing, c.max_adv_participation_pct)
    if capacity is not None:
        clauses[SelectionReason.CAPACITY] = capacity

    corr = _max_correlation(window, candidate, book.peers)
    if corr.short:
        flags.append(SelectionFlag.CORRELATION_HISTORY_SHORT)
    if corr.undefined:
        flags.append(SelectionFlag.CORRELATION_UNDEFINED)
    if c.max_correlation is not None and corr.value is not None and corr.value > c.max_correlation:
        clauses[SelectionReason.CORRELATION] = (
            f"its correlation with {corr.peer} is {corr.value:.4f} over the last "
            f"{c.correlation_window} sessions, above the {c.max_correlation:g} limit"
        )

    if book.entry_allowance is not None and book.accepted >= book.entry_allowance:
        clauses[SelectionReason.TURNOVER_BUDGET] = (
            f"the turnover budget of {_names(c.turnover_budget_names)} is used up"
        )
    if book.kept_total + book.accepted >= c.max_names:
        clauses[SelectionReason.FULL] = f"the book is full at {_names(c.max_names)}"

    refusals = tuple(reason for reason in _REFUSAL_ORDER if reason in clauses)
    head = f"ranked {candidate.quality_rank}, inside the entry limit of {c.entry_rank}"
    action: SelectionAction
    reasons: tuple[SelectionReason, ...]
    if refusals:
        action, reasons = SelectionAction.SKIP, refusals
        explanation = f"Skip: {head}, but " + "; and ".join(clauses[r] for r in refusals) + "."
    else:
        action, reasons = SelectionAction.ENTER, (SelectionReason.RANK_WITHIN_ENTRY,)
        explanation = f"Enter: {head}." + _sizing_sentence(sizing)
        book.accepted += 1
        book.add(candidate.instrument_id, candidate.symbol, sector)

    return SelectionRow(
        instrument_id=candidate.instrument_id,
        symbol=candidate.symbol,
        action=action,
        reasons=reasons,
        flags=tuple(flags),
        explanation=explanation + _flag_sentence(tuple(flags), corr),
        quality_rank=candidate.quality_rank,
        score=candidate.score,
        sector=candidate.sector,
        current_quantity=None,
        proposed_value_inr=sizing.value,
        proposed_qty=sizing.qty,
        adv_participation_pct=sizing.participation,
        max_correlation=corr.value,
        correlation_peer=corr.peer,
    )


def _capacity_clause(sizing: _Sizing, cap: Decimal | None) -> str | None:
    if not sizing.over_cap or cap is None or sizing.value is None:
        return None
    if sizing.participation is None:
        return (
            "its median daily traded value is zero, so any position breaks the "
            f"{_pct(cap)} participation cap"
        )
    return (
        f"{_inr(sizing.value)} would be {_pct(sizing.participation)} of median daily traded "
        f"value, above the {_pct(cap)} cap"
    )


def _hold_row(
    item: _Kept,
    sector: str | None,
    c: SelectionConstraints,
    slot_value: Decimal | None,
) -> SelectionRow:
    holding, candidate = item.holding, item.candidate
    cap_pct = c.max_adv_participation_pct
    sizing = _size(candidate, slot_value, cap_pct) if candidate is not None else None
    flags: list[SelectionFlag] = list(sizing.flags) if sizing is not None else []
    if sizing is not None and sizing.over_cap:
        flags.append(SelectionFlag.HOLD_ABOVE_CAPACITY)

    reasons: tuple[SelectionReason, ...]
    if item.retained_from is not None:
        reasons = (SelectionReason.RETAINED_BY_TURNOVER_BUDGET, item.retained_from)
        explanation = (
            f"Hold: {_would_exit_clause(candidate, c)}, so it would exit, but the turnover "
            f"budget of {_names(c.turnover_budget_names)} is spent on worse-ranked exits."
        )
    elif candidate is not None:
        reasons = (SelectionReason.RANK_WITHIN_RETENTION,)
        explanation = (
            f"Hold: ranked {candidate.quality_rank}, inside the retention limit of "
            f"{c.retention_rank}."
        )
    else:
        raise ValueError(f"{holding.symbol}: a hold with no rank must be a budget retention")
    if sizing is not None and sizing.over_cap and cap_pct is not None:
        explanation += (
            f" Its equal-weight target is above the {_pct(cap_pct)} participation cap; "
            "holdings are never refused for capacity."
        )
    return SelectionRow(
        instrument_id=holding.instrument_id,
        symbol=holding.symbol,
        action=SelectionAction.HOLD,
        reasons=reasons,
        flags=tuple(flags),
        explanation=explanation,
        quality_rank=candidate.quality_rank if candidate is not None else None,
        score=candidate.score if candidate is not None else None,
        sector=sector,
        current_quantity=holding.quantity,
        proposed_value_inr=sizing.value if sizing is not None else None,
        proposed_qty=sizing.qty if sizing is not None else None,
        adv_participation_pct=sizing.participation if sizing is not None else None,
        max_correlation=None,
        correlation_peer=None,
    )


def _exit_row(
    holding: Holding,
    candidate: Candidate | None,
    reason: SelectionReason,
    c: SelectionConstraints,
) -> SelectionRow:
    return SelectionRow(
        instrument_id=holding.instrument_id,
        symbol=holding.symbol,
        action=SelectionAction.EXIT,
        reasons=(reason,),
        flags=(),
        explanation=f"Exit: {_would_exit_clause(candidate, c)}.",
        quality_rank=candidate.quality_rank if candidate is not None else None,
        score=candidate.score if candidate is not None else None,
        sector=_holding_sector(holding, candidate),
        current_quantity=holding.quantity,
        proposed_value_inr=None,
        proposed_qty=None,
        adv_participation_pct=None,
        max_correlation=None,
        correlation_peer=None,
    )


def _would_exit_clause(candidate: Candidate | None, c: SelectionConstraints) -> str:
    if candidate is None:
        return "not in this screen's results (filtered out, or outside its universe)"
    return f"ranked {candidate.quality_rank}, outside the retention limit of {c.retention_rank}"


def _sizing_sentence(sizing: _Sizing) -> str:
    if sizing.value is None:
        return ""
    parts = [f" Proposed {_inr(sizing.value)}"]
    detail: list[str] = []
    if sizing.qty is not None:
        detail.append(f"{sizing.qty} shares")
    if sizing.participation is not None:
        detail.append(f"{_pct(sizing.participation)} of median daily traded value")
    if detail:
        parts.append(f" ({', '.join(detail)})")
    return "".join(parts) + "."


_FLAG_SENTENCES: Final[Mapping[SelectionFlag, str]] = {
    SelectionFlag.SECTOR_UNCLASSIFIED: "it has no sector, so the sector cap cannot apply",
    SelectionFlag.CAPACITY_UNKNOWN: "no median daily traded value, so capacity is unknown",
    SelectionFlag.PRICE_UNKNOWN: "no price, so no proposed quantity",
    SelectionFlag.PRICE_ABOVE_SLOT_VALUE: "one share costs more than the slot's value",
}


def _flag_sentence(flags: tuple[SelectionFlag, ...], corr: _Correlation) -> str:
    notes = [_FLAG_SENTENCES[flag] for flag in flags if flag in _FLAG_SENTENCES]
    if SelectionFlag.CORRELATION_HISTORY_SHORT in flags:
        notes.append(
            f"correlation not checked against {corr.short} name(s) with fewer than "
            f"{MIN_CORRELATION_OBSERVATIONS} shared sessions of returns"
        )
    if SelectionFlag.CORRELATION_UNDEFINED in flags:
        notes.append(
            f"correlation undefined against {corr.undefined} name(s) with a constant return series"
        )
    if not notes:
        return ""
    return " Note: " + "; ".join(notes) + "."
