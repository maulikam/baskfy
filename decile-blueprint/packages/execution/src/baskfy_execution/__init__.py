"""THE ONLY PATH TO AN ORDER (CLAUDE.md, Law 2).

guards -> risk -> rate limits -> idempotency -> journal -> broker. Nothing calls
`kc.place_order` directly, and `guards` refuses an untouchable instrument **before any network
call** -- which is the difference between a plan that cannot propose an SGB and a plan that is
merely executed by a layer that happens to filter one.

Moved out of the desk's `app/core/` at M16 (P3.5). The four modules are byte-identical to the code
that has been placing this account's orders, except that the gateway's three product switches now
arrive as an injected, fail-closed `ProductGates` instead of being read off the desk's config
module -- because core and execution cannot reach the desk, and because a default that refuses is
the only safe default for the module that talks to a broker.

Each of the desk's seven non-negotiables has a named test in
`packages/execution/tests/test_non_negotiables.py` that fails if it is "improved" away.
"""

from baskfy_execution.gateway import (
    FAILED_STATUSES,
    JOURNAL,
    OrderGateway,
    ProductGates,
)
from baskfy_execution.tenancy import TenantIds, refuse_cross_tenant
from baskfy_execution.guards import (
    OvernightOptionError,
    UntouchableInstrumentError,
    assert_not_overnight_option,
    assert_tradeable,
)
from baskfy_execution.ratelimit import KiteLimits
from baskfy_execution.risk import RiskConfig, RiskManager

__all__ = [
    "FAILED_STATUSES",
    "JOURNAL",
    "KiteLimits",
    "OrderGateway",
    "OvernightOptionError",
    "ProductGates",
    "RiskConfig",
    "RiskManager",
    "TenantIds",
    "UntouchableInstrumentError",
    "assert_not_overnight_option",
    "assert_tradeable",
    "refuse_cross_tenant",
]
