"""Moved to :mod:`baskfy_execution.gateway` at M16 (P3.5).

This shim binds the desk's three product switches to the gateway and keeps every existing
`from .core.gateway import OrderGateway` working. It is deliberately thin: the order path itself
is in `packages/execution`, which is the only module allowed to place, modify or cancel an order.

The gates are passed as a CALLABLE that reads `app.config` at the moment of the order, which is
exactly what `C.DRY_RUN` did before the move. Capturing them at construction would have meant a
setting changed mid-session took effect on the next restart rather than the next order.
"""

from baskfy_execution.gateway import (  # noqa: F401
    FAILED_STATUSES,
    JOURNAL,
    ProductGates,
    _DEFINITIVE_REFUSALS,
    log,
)
from baskfy_execution.gateway import OrderGateway as _CoreOrderGateway

from .. import config as C


def _gates_from_config() -> ProductGates:
    """Read the switches off the desk's config, now, not at construction."""
    return ProductGates(
        dry_run=C.DRY_RUN,
        intraday_enabled=C.INTRADAY_ENABLED,
        options_enabled=C.OPTIONS_ENABLED,
    )


class OrderGateway(_CoreOrderGateway):
    """The desk's gateway: `baskfy_execution`'s, bound to the desk's configuration."""

    def __init__(self, kc, risk, *, gates=None, journal_path: str = JOURNAL) -> None:
        super().__init__(kc, risk, gates=gates or _gates_from_config,
                         journal_path=journal_path)
