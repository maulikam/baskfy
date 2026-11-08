"""Publish-version domain helpers — docs/smallcase/04 §5 (SC3).

Pure: holdings / weights / prices in → diffs, version labels, desk-shaped order lists out.
No database, no network, no clock, no broker calls.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, Decimal
from typing import Final, Literal

from baskfy_core.curated_baskets import (
    VersionImmutableError,
    assert_version_is_new,
    assert_weights_sum_to_one,
    refuse_version_mutation,
)
from baskfy_core.curated_metrics import min_amount
from baskfy_core.gst import money

__all__ = [
    "BUY",
    "SELL",
    "VERSION_LABEL_CHANGED",
    "VERSION_LABEL_GENESIS",
    "VERSION_LABEL_NO_CHANGE",
    "ConstituentDraft",
    "DeskOrderLine",
    "HoldingsDiff",
    "HoldingsDiffLine",
    "OrderSide",
    "PublishSideEffects",
    "PublishedVersionDraft",
    "VersionLabel",
    "assert_next_version_no",
    "build_published_version",
    "classify_version_change",
    "desk_orders_from_diff",
    "diff_holdings_vs_weights",
    "publish_side_effects",
    "refuse_version_mutation",
]

OrderSide = Literal["BUY", "SELL"]
VersionLabel = Literal["CHANGED", "NO_CHANGE", "GENESIS"]

BUY: Final = "BUY"
SELL: Final = "SELL"

VERSION_LABEL_CHANGED: Final = "CHANGED"
VERSION_LABEL_NO_CHANGE: Final = "NO_CHANGE"
VERSION_LABEL_GENESIS: Final = "GENESIS"

_WEIGHT_Q: Final = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class ConstituentDraft:
    """One constituent row for a version about to be published."""

    instrument_id: int
    weight: Decimal


@dataclass(frozen=True, slots=True)
class PublishedVersionDraft:
    """Pure description of a version row ready to persist (append-only)."""

    version_no: int
    effective_date: date
    label: VersionLabel
    added_count: int
    removed_count: int
    constituents: tuple[ConstituentDraft, ...]

    @property
    def weights(self) -> tuple[Decimal, ...]:
        return tuple(c.weight for c in self.constituents)


@dataclass(frozen=True, slots=True)
class HoldingsDiffLine:
    """One buy/sell child in an apply preview (04 §5)."""

    instrument_id: int
    side: OrderSide
    qty: Decimal
    amount: Decimal
    current_weight: Decimal
    target_weight: Decimal
    weight_delta: Decimal
    price: Decimal


@dataclass(frozen=True, slots=True)
class HoldingsDiff:
    """Apply preview: children, residual cash after sells fund buys, optional top-up."""

    lines: tuple[HoldingsDiffLine, ...]
    portfolio_value: Decimal
    residual_cash: Decimal
    top_up: Decimal
    min_amount: Decimal

    @property
    def buys(self) -> tuple[HoldingsDiffLine, ...]:
        return tuple(line for line in self.lines if line.side == BUY)

    @property
    def sells(self) -> tuple[HoldingsDiffLine, ...]:
        return tuple(line for line in self.lines if line.side == SELL)


@dataclass(frozen=True, slots=True)
class DeskOrderLine:
    """Desk-shaped order row — pure data; no broker calls."""

    symbol: str
    side: OrderSide
    qty: int
    weight: Decimal


@dataclass(frozen=True, slots=True)
class PublishSideEffects:
    """04 §5 side-effects when a version is published (description for the API layer)."""

    update_post_source: Literal["ENGINE"]
    update_post_title: str
    update_post_body_md: str
    pending_action_type: Literal["REBALANCE_AVAILABLE"]
    rebalance_state: Literal["PENDING"]
    affected_user_ids: tuple[int, ...]


def assert_next_version_no(
    *,
    existing_version_nos: Sequence[int],
    next_version_no: int,
) -> None:
    """Refuse a non-sequential or duplicate ``version_no`` (append-only versions).

    Genesis is ``1`` when none exist; thereafter ``max(existing) + 1``.
    Reuses :func:`assert_version_is_new` / :class:`VersionImmutableError`.
    """
    if next_version_no < 1:
        raise ValueError(f"version_no must be >= 1, got {next_version_no}")
    assert_version_is_new(
        existing_version_nos=existing_version_nos,
        new_version_no=next_version_no,
    )
    expected = 1 if not existing_version_nos else max(existing_version_nos) + 1
    if next_version_no != expected:
        raise VersionImmutableError(
            f"version_no {next_version_no} is not the next sequential number "
            f"(expected {expected}); versions are append-only"
        )


def classify_version_change(
    previous_instrument_ids: Sequence[int],
    new_instrument_ids: Sequence[int],
    *,
    is_genesis: bool = False,
    previous_weights_by_id: Mapping[int, Decimal] | None = None,
    new_weights_by_id: Mapping[int, Decimal] | None = None,
) -> tuple[VersionLabel, int, int]:
    """Return ``(label, added_count, removed_count)`` for a version cut.

    Membership drives added/removed counts. Weight-only edits (same ids, different
    weights) still label ``CHANGED``. Identical membership and weights → ``NO_CHANGE``.
    """
    prev = set(previous_instrument_ids)
    new = set(new_instrument_ids)
    added = len(new - prev)
    removed = len(prev - new)
    if is_genesis:
        return VERSION_LABEL_GENESIS, len(new), 0
    if added or removed:
        return VERSION_LABEL_CHANGED, added, removed
    if previous_weights_by_id is not None and new_weights_by_id is not None:
        for iid in prev & new:
            old_w = previous_weights_by_id.get(iid, Decimal("0")).quantize(_WEIGHT_Q)
            new_w = new_weights_by_id.get(iid, Decimal("0")).quantize(_WEIGHT_Q)
            if old_w != new_w:
                return VERSION_LABEL_CHANGED, 0, 0
    return VERSION_LABEL_NO_CHANGE, 0, 0


def build_published_version(  # noqa: PLR0913 - explicit publish knobs
    *,
    version_no: int,
    effective_date: date,
    constituents: Sequence[ConstituentDraft],
    existing_version_nos: Sequence[int],
    previous_instrument_ids: Sequence[int] = (),
    previous_weights_by_id: Mapping[int, Decimal] | None = None,
) -> PublishedVersionDraft:
    """Build an append-only version draft; refuses duplicate / non-sequential numbers."""
    assert_next_version_no(
        existing_version_nos=existing_version_nos,
        next_version_no=version_no,
    )
    if not constituents:
        raise ValueError("constituents cannot be empty")
    seen: set[int] = set()
    normalized: list[ConstituentDraft] = []
    for row in constituents:
        if row.instrument_id in seen:
            raise ValueError(f"duplicate instrument_id {row.instrument_id}")
        seen.add(row.instrument_id)
        weight = row.weight.quantize(_WEIGHT_Q)
        if weight <= 0:
            raise ValueError(f"weight must be positive for instrument {row.instrument_id}")
        normalized.append(ConstituentDraft(row.instrument_id, weight))
    weights = tuple(c.weight for c in normalized)
    assert_weights_sum_to_one(weights)

    new_ids = tuple(c.instrument_id for c in normalized)
    new_weights = {c.instrument_id: c.weight for c in normalized}
    is_genesis = not existing_version_nos and version_no == 1
    label, added, removed = classify_version_change(
        previous_instrument_ids,
        new_ids,
        is_genesis=is_genesis,
        previous_weights_by_id=previous_weights_by_id,
        new_weights_by_id=new_weights,
    )
    return PublishedVersionDraft(
        version_no=version_no,
        effective_date=effective_date,
        label=label,
        added_count=added,
        removed_count=removed,
        constituents=tuple(normalized),
    )


def diff_holdings_vs_weights(  # noqa: PLR0912 - per-instrument buy/sell branches
    holdings: Mapping[int, Decimal],
    target_weights: Mapping[int, Decimal],
    prices: Mapping[int, Decimal],
) -> HoldingsDiff:
    """Diff intended holdings vs target weights at current prices (04 §5).

    Uses value-delta whole-share floor: for each name,
    ``qty = floor(|target_value - current_value| / price)``, never selling more than held.
    Sells fund buys; ``residual_cash`` is proceeds - buy cost; ``top_up`` covers a cash
    shortfall and any gap to the new basket ``min_amount``.
    """
    if not target_weights:
        raise ValueError("target_weights cannot be empty")
    assert_weights_sum_to_one(list(target_weights.values()))

    for iid, qty in holdings.items():
        if qty < 0:
            raise ValueError(f"holding qty cannot be negative for {iid}")
        if qty > 0 and iid not in prices:
            raise ValueError(f"missing price for instrument {iid}")

    for iid, weight in target_weights.items():
        if weight <= 0:
            raise ValueError(f"target weight must be positive for {iid}")
        if iid not in prices:
            raise ValueError(f"missing price for instrument {iid}")

    for iid, price in prices.items():
        if price <= 0:
            raise ValueError(f"price must be positive for {iid}, got {price}")

    portfolio_value = money(
        sum(
            (qty * prices[iid] for iid, qty in holdings.items() if qty > 0),
            Decimal("0"),
        )
    )

    ordered = tuple(sorted(target_weights))
    min_required = min_amount(
        [prices[iid] for iid in ordered],
        [target_weights[iid] for iid in ordered],
    )

    lines: list[HoldingsDiffLine] = []
    sell_proceeds = Decimal("0")
    buy_cost = Decimal("0")

    all_ids = sorted(set(holdings) | set(target_weights))
    for iid in all_ids:
        price = prices[iid]
        current_qty = holdings.get(iid, Decimal("0"))
        current_value = money(current_qty * price) if current_qty > 0 else Decimal("0.00")
        target_weight = target_weights.get(iid, Decimal("0"))
        target_value = (
            money(portfolio_value * target_weight) if target_weight > 0 else Decimal("0.00")
        )
        current_weight = (
            (current_value / portfolio_value).quantize(_WEIGHT_Q)
            if portfolio_value > 0 and current_value > 0
            else Decimal("0.0000")
        )
        delta_value = target_value - current_value
        if delta_value == 0:
            continue

        if delta_value < 0:
            raw_qty = ((-delta_value) / price).to_integral_value(rounding=ROUND_DOWN)
            held_whole = current_qty.to_integral_value(rounding=ROUND_DOWN)
            qty = min(raw_qty, held_whole)
            if qty <= 0:
                continue
            amount = money(qty * price)
            sell_proceeds += amount
            side: OrderSide = SELL
        else:
            qty = (delta_value / price).to_integral_value(rounding=ROUND_DOWN)
            if qty <= 0:
                continue
            amount = money(qty * price)
            buy_cost += amount
            side = BUY

        tw = target_weight.quantize(_WEIGHT_Q)
        lines.append(
            HoldingsDiffLine(
                instrument_id=iid,
                side=side,
                qty=qty,
                amount=amount,
                current_weight=current_weight,
                target_weight=tw,
                weight_delta=(tw - current_weight).quantize(_WEIGHT_Q),
                price=price,
            )
        )

    residual = money(sell_proceeds - buy_cost)
    top_up = money(
        max(
            Decimal("0"),
            -residual,
            min_required - portfolio_value,
        )
    )

    return HoldingsDiff(
        lines=tuple(lines),
        portfolio_value=portfolio_value,
        residual_cash=residual,
        top_up=top_up,
        min_amount=min_required,
    )


def desk_orders_from_diff(
    diff: HoldingsDiff,
    symbols: Mapping[int, str],
) -> tuple[DeskOrderLine, ...]:
    """Build a desk-shaped order list from a holdings diff (pure data; no broker calls)."""
    out: list[DeskOrderLine] = []
    for line in diff.lines:
        symbol = symbols.get(line.instrument_id)
        if symbol is None:
            raise ValueError(f"missing symbol for instrument {line.instrument_id}")
        cleaned = symbol.strip().upper()
        if not cleaned:
            raise ValueError(f"symbol cannot be empty for instrument {line.instrument_id}")
        out.append(
            DeskOrderLine(
                symbol=cleaned,
                side=line.side,
                qty=int(line.qty),
                weight=line.target_weight,
            )
        )
    return tuple(out)


def publish_side_effects(  # noqa: PLR0913 - explicit 04 §5 side-effect knobs
    *,
    version_no: int,
    label: VersionLabel,
    added_count: int,
    removed_count: int,
    active_investor_user_ids: Sequence[int],
    notes_md: str | None = None,
) -> PublishSideEffects:
    """Describe 04 §5 publish side-effects: ENGINE post + REBALANCE_AVAILABLE + PENDING."""
    seen: set[int] = set()
    users: list[int] = []
    for uid in active_investor_user_ids:
        if uid in seen:
            continue
        seen.add(uid)
        users.append(uid)

    if label == VERSION_LABEL_GENESIS:
        title = f"Version {version_no} went live"
        body = f"Genesis cut published as version {version_no}."
    elif label == VERSION_LABEL_NO_CHANGE:
        title = f"Version {version_no}: no constituent change"
        body = f"Version {version_no} published with the same constituents (+0 / -0)."
    else:
        title = f"Version {version_no}: rebalance available"
        body = f"Version {version_no} published: +{added_count} / -{removed_count} constituents."

    if notes_md:
        body = f"{body}\n\n{notes_md}"

    return PublishSideEffects(
        update_post_source="ENGINE",
        update_post_title=title,
        update_post_body_md=body,
        pending_action_type="REBALANCE_AVAILABLE",
        rebalance_state="PENDING",
        affected_user_ids=tuple(users),
    )
