"""The one predicate that decides a curated basket may be shown at all.

It lived as a private helper in ``routers/explore.py``, whose docstring already said why there
must be exactly one: ``cb_basket`` has no owner column, so ``visibility`` is the only thing
standing between a PRIVATE basket and the caller.

It is here, in a module of its own, because a second copy went wrong the first time one was made.
AF I.1's version routes wrote their own and filtered on ``visibility == "LISTED"`` — a value
``BASKET_VISIBILITY`` does not contain and a check constraint forbids, so no row could ever match
and both routes answered 404 for every basket that exists. ``routers/explore.py`` imports the
version router at the bottom of the file, so the shared helper cannot live in either of them.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement

from baskfy_core.models import CbBasket


def visible_baskets() -> tuple[ColumnElement[bool], ...]:
    """Not archived, and published rather than private."""
    return (CbBasket.archived_at.is_(None), CbBasket.visibility == "PUBLISHED")
