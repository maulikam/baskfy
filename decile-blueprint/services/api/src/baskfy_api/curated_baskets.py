"""Transactional write seam for append-only curated-basket versions."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.curated_baskets import VersionDraft, validate_version_draft
from baskfy_core.models import BasketConstituent, BasketVersion, CuratedBasket


class CuratedBasketRepositoryError(RuntimeError):
    """Base class for expected repository-state refusals."""


class BasketNotFoundError(CuratedBasketRepositoryError):
    """The requested curated basket does not exist."""


class InvalidBasketIdError(CuratedBasketRepositoryError):
    """The basket identity is not a positive, non-boolean integer."""


class EffectiveDateNotIncreasingError(CuratedBasketRepositoryError):
    """A version would not advance its basket's effective date."""


class CuratedBasketRepository:
    """Append versions while serializing writers on their owning basket row."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_version(self, basket_id: int, draft: VersionDraft) -> BasketVersion:
        """Validate and append one immutable version.

        The basket lock makes deriving ``version_no`` safe under concurrent publishers.  This
        method flushes so callers receive an identified version, but transaction ownership and
        commit/rollback stay with the request or job boundary.
        """
        if type(basket_id) is not int or basket_id <= 0:
            raise InvalidBasketIdError("basket_id must be a positive integer")
        validate_version_draft(draft)

        basket = await self._session.scalar(
            select(CuratedBasket).where(CuratedBasket.id == basket_id).with_for_update()
        )
        if basket is None:
            raise BasketNotFoundError(f"curated basket {basket_id} does not exist")

        latest = await self._session.scalar(
            select(BasketVersion)
            .where(BasketVersion.basket_id == basket_id)
            .order_by(BasketVersion.version_no.desc())
            .limit(1)
        )

        previous: dict[int, tuple[str, Decimal]] = {}
        if latest is not None:
            if draft.effective_date <= latest.effective_date:
                raise EffectiveDateNotIncreasingError(
                    "effective_date must be strictly later than the latest version "
                    f"({latest.effective_date.isoformat()})"
                )
            rows = (
                await self._session.execute(
                    select(
                        BasketConstituent.instrument_id,
                        BasketConstituent.segment,
                        BasketConstituent.weight,
                    ).where(BasketConstituent.version_id == latest.id)
                )
            ).tuples()
            previous = {instrument_id: (segment, weight) for instrument_id, segment, weight in rows}

        incoming = {
            constituent.instrument_id: (constituent.segment, constituent.weight)
            for constituent in draft.constituents
        }
        previous_ids = previous.keys()
        incoming_ids = incoming.keys()
        added_count = len(incoming_ids - previous_ids)
        removed_count = len(previous_ids - incoming_ids)
        label = "GENESIS" if latest is None else "NO_CHANGE" if incoming == previous else "CHANGED"

        version = BasketVersion(
            basket_id=basket_id,
            version_no=1 if latest is None else latest.version_no + 1,
            effective_date=draft.effective_date,
            label=label,
            added_count=added_count,
            removed_count=removed_count,
            notes_md=draft.notes_md,
            source_scan_run_id=draft.source_scan_run_id,
        )
        self._session.add(version)
        await self._session.flush()

        self._session.add_all(
            [
                BasketConstituent(
                    version_id=version.id,
                    instrument_id=constituent.instrument_id,
                    segment=constituent.segment,
                    weight=constituent.weight,
                )
                for constituent in draft.constituents
            ]
        )
        await self._session.flush()
        return version
