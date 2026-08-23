"""Persist curated basket versions and compute apply-preview diffs (SC3).

Thin service over ``baskfy_core.curated_versions``: append-only version rows, ENGINE
update posts, ``REBALANCE_AVAILABLE`` pending actions, and holdings→target diffs.
Does **not** generate desk plans or call the order gateway (plans leaf / Track C).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.curated_versions import (
    ConstituentDraft,
    DeskOrderLine,
    HoldingsDiff,
    PublishedVersionDraft,
    PublishSideEffects,
    build_published_version,
    desk_orders_from_diff,
    diff_holdings_vs_weights,
    publish_side_effects,
    refuse_version_mutation,
)
from baskfy_core.models import (
    CbBasketVersion,
    CbConstituent,
    CbInvestment,
    CbInvestmentHolding,
    CbPendingAction,
    CbUpdatePost,
    CbUserRebalanceState,
    Instrument,
)

__all__ = [
    "RowMapping",
    "holdings_to_qty_map",
    "persist_published_version",
    "preview_holdings_diff",
    "publish_version_with_side_effects",
    "refuse_version_row_mutation",
    "weights_from_constituents",
]


#: A version/holding row as a plain mapping rather than an ORM instance. The values are
#: ``object`` and not ``Any`` on purpose (CLAUDE.md house rule 3): a cell read out of an
#: untyped dict is genuinely unknown, and :func:`_cell_int` / :func:`_cell_decimal` are where
#: that unknown is checked instead of being asserted away.
RowMapping = Mapping[str, object]


def _cell_int(value: object, *, field: str) -> int:
    """Coerce one mapping cell to ``int``, naming the field when it is not numeric."""
    if isinstance(value, bool):
        raise TypeError(f"{field} must be an integer, got a bool")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal | str):
        return int(value)
    raise TypeError(f"{field} must be an integer, got {type(value).__name__}")


def _cell_decimal(value: object, *, field: str) -> Decimal:
    """Coerce one mapping cell to ``Decimal`` (house rule 9: money is never a float)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | str):
        return Decimal(str(value))
    raise TypeError(f"{field} must be a Decimal, got {type(value).__name__}")


def weights_from_constituents(
    rows: Sequence[CbConstituent] | Sequence[RowMapping],
) -> dict[int, Decimal]:
    """Build ``instrument_id → weight`` from ORM rows or plain dicts."""
    out: dict[int, Decimal] = {}
    for row in rows:
        if isinstance(row, Mapping):
            iid = _cell_int(row["instrument_id"], field="instrument_id")
            weight = _cell_decimal(row["weight"], field="weight")
        else:
            iid = int(row.instrument_id)
            weight = Decimal(row.weight)
        out[iid] = weight
    return out


def holdings_to_qty_map(
    rows: Sequence[CbInvestmentHolding] | Sequence[RowMapping],
) -> dict[int, Decimal]:
    """Qty map from intended holdings (Decimal; domain floors to whole shares)."""
    out: dict[int, Decimal] = {}
    for row in rows:
        if isinstance(row, Mapping):
            iid = _cell_int(row["instrument_id"], field="instrument_id")
            qty = _cell_decimal(row["qty"], field="qty")
        else:
            iid = int(row.instrument_id)
            qty = Decimal(row.qty)
        if qty > 0:
            out[iid] = qty
    return out


def refuse_version_row_mutation() -> None:
    """Service-layer refuse for UPDATE on version/constituent rows."""
    refuse_version_mutation()


def preview_holdings_diff(
    *,
    holdings: Mapping[int, Decimal],
    target_weights: Mapping[int, Decimal],
    prices: Mapping[int, Decimal],
) -> HoldingsDiff:
    """04 §5 apply preview: intended holdings vs target weights at prices."""
    return diff_holdings_vs_weights(holdings, target_weights, prices)


async def persist_published_version(  # noqa: PLR0913 - explicit version columns
    session: AsyncSession,
    *,
    basket_id: int,
    version_no: int,
    effective_date: dt.date,
    constituents: Sequence[tuple[int, Decimal, str]],
    previous_weights: Mapping[int, Decimal] | None = None,
    notes_md: str | None = None,
    source_scan_run_id: str | None = None,
) -> tuple[CbBasketVersion, PublishedVersionDraft]:
    """Insert an immutable version + constituents from a validated domain draft.

    Does not write posts/actions — use :func:`publish_version_with_side_effects` for the
    full 04 §5 path. ``constituents`` is ``(instrument_id, weight, segment)``.
    """
    existing = list(
        (
            await session.execute(
                select(CbBasketVersion.version_no).where(CbBasketVersion.basket_id == basket_id)
            )
        )
        .scalars()
        .all()
    )

    old_weights: dict[int, Decimal] = dict(previous_weights or {})
    if previous_weights is None and existing:
        prev = (
            await session.execute(
                select(CbBasketVersion)
                .where(CbBasketVersion.basket_id == basket_id)
                .order_by(CbBasketVersion.version_no.desc())
                .limit(1)
            )
        ).scalar_one()
        prev_rows = (
            (
                await session.execute(
                    select(CbConstituent).where(CbConstituent.version_id == prev.id)
                )
            )
            .scalars()
            .all()
        )
        old_weights = weights_from_constituents(prev_rows)

    drafts = tuple(ConstituentDraft(iid, weight) for iid, weight, _seg in constituents)
    draft = build_published_version(
        version_no=version_no,
        effective_date=effective_date,
        constituents=drafts,
        existing_version_nos=existing,
        previous_instrument_ids=tuple(old_weights),
        previous_weights_by_id=old_weights or None,
    )

    version = CbBasketVersion(
        basket_id=basket_id,
        version_no=draft.version_no,
        effective_date=draft.effective_date,
        label=draft.label,
        added_count=draft.added_count,
        removed_count=draft.removed_count,
        notes_md=notes_md,
        source_scan_run_id=source_scan_run_id,
    )
    session.add(version)
    await session.flush()

    for iid, weight, segment in constituents:
        session.add(
            CbConstituent(
                version_id=version.id,
                instrument_id=iid,
                segment=segment,
                weight=weight,
            )
        )
    await session.flush()
    return version, draft


async def publish_version_with_side_effects(  # noqa: PLR0913
    session: AsyncSession,
    *,
    basket_id: int,
    version_no: int,
    effective_date: dt.date,
    constituents: Sequence[tuple[int, Decimal, str]],
    published_at: dt.datetime,
    manager_id: int | None = None,
    previous_weights: Mapping[int, Decimal] | None = None,
    notes_md: str | None = None,
    source_scan_run_id: str | None = None,
) -> tuple[CbBasketVersion, PublishSideEffects]:
    """Publish version + ENGINE post + REBALANCE_AVAILABLE + PENDING per ACTIVE investor."""
    version, draft = await persist_published_version(
        session,
        basket_id=basket_id,
        version_no=version_no,
        effective_date=effective_date,
        constituents=constituents,
        previous_weights=previous_weights,
        notes_md=notes_md,
        source_scan_run_id=source_scan_run_id,
    )

    active = (
        (
            await session.execute(
                select(CbInvestment.user_id).where(
                    CbInvestment.basket_id == basket_id,
                    CbInvestment.status == "ACTIVE",
                )
            )
        )
        .scalars()
        .all()
    )

    effects = publish_side_effects(
        version_no=version_no,
        label=draft.label,
        added_count=version.added_count,
        removed_count=version.removed_count,
        active_investor_user_ids=list(active),
        notes_md=notes_md,
    )

    session.add(
        CbUpdatePost(
            basket_id=basket_id,
            manager_id=manager_id,
            title=effects.update_post_title,
            body_md=effects.update_post_body_md,
            published_at=published_at,
            source=effects.update_post_source,
        )
    )

    for user_id in effects.affected_user_ids:
        session.add(
            CbPendingAction(
                user_id=user_id,
                type=effects.pending_action_type,
                payload={
                    "basket_id": basket_id,
                    "version_id": version.id,
                    "version_no": version_no,
                },
            )
        )
        session.add(
            CbUserRebalanceState(
                user_id=user_id,
                version_id=version.id,
                state=effects.rebalance_state,
                decided_at=None,
            )
        )

    await session.flush()
    return version, effects


async def preview_apply_for_investment(
    session: AsyncSession,
    *,
    investment_id: int,
    version_id: int,
    prices: Mapping[int, Decimal],
) -> HoldingsDiff:
    """Load intended holdings + version weights; return 04 §5 diff (no plan / no orders)."""
    holdings = (
        (
            await session.execute(
                select(CbInvestmentHolding).where(
                    CbInvestmentHolding.investment_id == investment_id
                )
            )
        )
        .scalars()
        .all()
    )
    constituents = (
        (await session.execute(select(CbConstituent).where(CbConstituent.version_id == version_id)))
        .scalars()
        .all()
    )
    return preview_holdings_diff(
        holdings=holdings_to_qty_map(holdings),
        target_weights=weights_from_constituents(constituents),
        prices=dict(prices),
    )


async def desk_order_preview(
    session: AsyncSession,
    *,
    investment_id: int,
    version_id: int,
    prices: Mapping[int, Decimal],
) -> tuple[HoldingsDiff, tuple[DeskOrderLine, ...]]:
    """Diff + desk-shaped order lines (symbol/side/qty/weight). Still no execute."""
    diff = await preview_apply_for_investment(
        session,
        investment_id=investment_id,
        version_id=version_id,
        prices=prices,
    )
    ids = [line.instrument_id for line in diff.lines]
    if not ids:
        return diff, ()
    rows = (
        await session.execute(
            select(Instrument.id, Instrument.symbol).where(Instrument.id.in_(ids))
        )
    ).all()
    symbols = {int(r.id): str(r.symbol) for r in rows}
    return diff, desk_orders_from_diff(diff, symbols)
