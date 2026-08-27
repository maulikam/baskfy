"""The ``desk`` schema, and what to do when a deployment does not have one.

The desk's SQLite migrated into a Postgres schema called ``desk`` at M19, and several read-only
routers query it directly: the rebalance plan (`routers/baskets.py`), holdings and the tradebook
(`routers/desk.py`), drift (`routers/curated_drift.py`), sleeves (`routers/sleeves.py`).

**A deployment can legitimately have no desk schema at all.** Staging is one: it was migrated with
alembic, which owns the application's own tables, and the desk's schema arrives by a separate
migration of that SQLite file (docs/08 D8) that has never been run there. Nothing is wrong with
that box — it simply has no desk history.

What was wrong is how it read from the outside. `asyncpg` raises `UndefinedTableError`, SQLAlchemy
wraps it as `ProgrammingError`, and every one of those endpoints answered **500 Internal Server
Error** — `/baskets/plan`, `/baskets/plan/kite` and `/desk/holdings` all did, on staging, for as
long as staging has existed. A 500 says *we broke*. The truth is *there is nothing here yet*, and
those are different sentences to a reader and different pages to build.

`DESK_SCHEMA` also lived in four separate modules as four separate string literals. One now.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from baskfy_api.db import SessionDep

#: The schema the desk's history migrated into (docs/08 D8). One definition.
DESK_SCHEMA: Final = "desk"


def is_missing_desk_data(error: Exception) -> bool:
    """True when this error is "the desk schema is not on this deployment".

    Matched on asyncpg's `UndefinedTableError` rather than on the message text, which is
    localisable and version-dependent. A schema that exists but whose *table* is missing produces
    the same class and the same honest answer: there is no desk history here.

    Deliberately narrow. A permissions error, a dead connection or a genuine query bug must still
    surface as a 500, because those really are "we broke" and hiding them behind "nothing here
    yet" would turn an outage into an empty page nobody investigates.
    """
    if not isinstance(error, ProgrammingError):
        return False
    original = getattr(error, "orig", None)
    cause = getattr(original, "__cause__", None) or original
    return type(cause).__name__ == "UndefinedTableError"


async def require_desk_schema(session: SessionDep) -> None:
    """Refuse the whole ``/desk`` surface at once when this deployment has no desk history.

    A dependency rather than a try/except around each of the six queries: they are read-only and
    every one of them fails the same way for the same reason, so checking once — before any of
    them runs — says it in one place and cannot be forgotten on the seventh.

    Typed `SessionDep` rather than `AsyncSession`: FastAPI reads the annotation to decide what to
    inject, and a bare `AsyncSession` is not a dependency — it tries to build a request field out
    of it and refuses the whole router at import time.

    404 rather than 503: this is not a service that is temporarily down. A deployment without the
    desk's migrated SQLite (docs/08 D8) has no desk history and is not expected to grow one, and a
    client that retries a 503 forever against a box that will never have the data is worse served
    than one told plainly that there is nothing here.
    """
    present = await session.scalar(
        text("select 1 from information_schema.schemata where schema_name = :name"),
        {"name": DESK_SCHEMA},
    )
    if present is None:
        # Imported here rather than at module scope: `problems` is the HTTP vocabulary and this
        # module is otherwise usable by anything that talks to the desk schema.
        from baskfy_api.problems import Problem, ProblemType

        raise Problem(
            ProblemType.NOT_FOUND,
            detail="this deployment has no desk history.",
        )
