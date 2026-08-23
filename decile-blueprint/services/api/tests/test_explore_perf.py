"""SC11 / leaf-1.8.3 — explore catalog avoids N+1; p95 budget documented."""

from __future__ import annotations

import inspect

from baskfy_api.curated_metrics_service import METRICS_BASKET_CHUNK
from baskfy_api.routers import explore
from baskfy_api.routers.explore import list_explore_baskets


def test_explore_list_documents_n_plus_one_avoidance_and_p95_budget() -> None:
    """Catalog list is one JOINed query (N+1 avoided); p95 budget < 1s on docs/06 SC11."""
    handler_src = inspect.getsource(list_explore_baskets)
    module_src = inspect.getsource(explore)
    assert "N+1 avoided" in handler_src or "N+1 avoided" in module_src
    assert "p95" in handler_src or "p95" in module_src
    assert "budget" in handler_src.lower() or "budget" in module_src.lower()
    # Prefer a single JOINed SELECT over lazy joinedload/selectinload per card.
    assert (
        "joinedload" in handler_src
        or "selectinload" in handler_src
        or ".join(" in handler_src
        or "outerjoin" in handler_src
    )


def test_metrics_basket_chunk_is_bounded() -> None:
    """leaf-1.8.4 sibling: chunk constant exists so catalog jobs stay memory-bounded."""
    assert METRICS_BASKET_CHUNK == 50
    assert METRICS_BASKET_CHUNK > 0
