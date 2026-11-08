"""Track B gating helpers (SC10 / docs/smallcase/02).

Flags default OFF. While ``subscriptions_enabled`` is false every basket is Free Access —
fee labels may still say FEE, but locks and paywalls must not apply.
"""

from __future__ import annotations

from baskfy_api.problems import not_found
from baskfy_api.settings import Settings

#: OpenAPI paths that exist only as dark Track-B surfaces (404 while the matching flag is off).
PAYWALL_PATH = "/cb/paywall"
PUBLIC_SIGNUP_PATH = "/cb/public-signup"
FEE_COLLECT_PATH = "/cb/fees/collect"


def free_access(*, basket_access: str, settings: Settings) -> bool:
    """Whether the caller may open constituents without a subscription entitlement.

    docs/smallcase/02 Track B: while ``BASKFY_SUBSCRIPTIONS_ENABLED`` is false, every basket
    renders as Free Access.
    """
    if not settings.subscriptions_enabled:
        return True
    return basket_access.upper() == "FREE"


def require_subscriptions_enabled(settings: Settings) -> None:
    """404 paywall-shaped surfaces while subscriptions stay dark."""
    if not settings.subscriptions_enabled:
        raise not_found("route", "paywall")


def require_public_signup_enabled(settings: Settings) -> None:
    """404 public-signup surfaces while sign-up stays dark."""
    if not settings.public_signup_enabled:
        raise not_found("route", "public-signup")


def require_fee_collection_enabled(settings: Settings) -> None:
    """404 fee-collection surfaces while collection stays dark."""
    if not settings.fee_collection_enabled:
        raise not_found("route", "fee-collect")
