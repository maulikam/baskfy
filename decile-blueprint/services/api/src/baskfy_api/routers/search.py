"""``GET /search`` — the ⌘K palette's one call.

`baskfynavrefactorreport.md` §F11 asked for "one global search [covering] stocks, indices, baskets,
and screens"; §"Global search" fixes the surface as "⌘K / tap-search opens a command palette
searching stocks, indices, baskets, and screens, with recent items. Replaces both existing scoped
search boxes as the primary entry."

The ranking, the visibility rules and the reasoning behind both live in `baskfy_api.search`. This
module is the HTTP edge: parameter bounds, the principal, and the response shape.

**No `as_of` / `data_version` headers.** Every other read of published data here carries them
(docs/07 §Conventions) because the number in the payload is only meaningful against the day it was
computed. A search hit carries no computed number — a symbol, a name, a slug — so a snapshot
header would be decoration, and the ETag derived from it would cache a list of *names* against a
version that changes nightly for reasons that never touch them. `Cache-Control: no-store` instead:
the result set depends on the caller (their screens, whether they are signed in at all), and a
shared cache must not hold it.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Query, Response

from baskfy_api.auth import PrincipalDep
from baskfy_api.db import SessionDep
from baskfy_api.schemas import CatalogHitOut, CatalogSearchOut
from baskfy_api.search import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_QUERY_LENGTH,
    MIN_QUERY_LENGTH,
    search_catalog,
)

router = APIRouter(tags=["search"])

#: The response is per-caller (a signed-in user's own screens are in it) and cheap to recompute.
#: A shared cache holding one user's hits and serving them to the next is the failure this
#: prevents; `private` would be enough for that, but there is nothing here worth a revalidation
#: round trip either.
NO_STORE: Final = "no-store"


@router.get("/search", response_model=CatalogSearchOut, summary="Search the whole catalog")
async def catalog_search(
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    q: Annotated[
        str,
        Query(
            min_length=MIN_QUERY_LENGTH,
            max_length=MAX_QUERY_LENGTH,
            description="Symbol, company, index, basket or screen name.",
        ),
    ],
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_LIMIT, description="Hits **per kind**, not in total."),
    ] = DEFAULT_LIMIT,
) -> CatalogSearchOut:
    """Stocks, indices, baskets and screens in one round trip.

    Open to anonymous callers, and honest about it: they get stocks, indices and example screens —
    exactly what they can already reach by navigating — and no baskets, because `/explore` requires
    a user. A 401 for the whole search would take ⌘K away from every marketing page to protect one
    of its four groups.
    """
    response.headers["Cache-Control"] = NO_STORE
    results = await search_catalog(session, q, limit, principal)
    return CatalogSearchOut(
        query=results.query,
        limit=results.limit,
        data=[
            CatalogHitOut(kind=hit.kind, id=hit.id, title=hit.title, subtitle=hit.subtitle)
            for hit in results.hits
        ],
    )
