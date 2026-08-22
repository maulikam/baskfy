"""SQLAlchemy 2.0 models for every table in docs/04-data-model.md.

Importing this package registers every table on ``Base.metadata``, which is what Alembic's
``target_metadata`` points at.
"""

from baskfy_core.models.accounts import (
    PAYMENT_STATUSES,
    AppUser,
    Backtest,
    Payment,
    PipelineRun,
    PipelineRunStep,
    Plan,
    Portfolio,
    PortfolioHolding,
    PortfolioRebalance,
    PortfolioSleeve,
    Subscription,
)
from baskfy_core.models.admin import (
    ADMIN_ACTIONS,
    OVERRIDE_EFFECTS,
    AdminAction,
    EntitlementOverride,
)
from baskfy_core.models.auth import (
    CONSENT_KINDS,
    TOKEN_PURPOSES,
    AccountDeletion,
    AuthLockout,
    AuthToken,
    AuthVerificationToken,
    ConsentRecord,
    RefreshToken,
)
from baskfy_core.models.base import Base, JsonObject
from baskfy_core.models.billing import (
    RAZORPAY_PROVIDER,
    WEBHOOK_STATUSES,
    InvoiceCounter,
    WebhookEvent,
)
from baskfy_core.models.facts import FactorDaily
from baskfy_core.models.integrations import (
    ALERT_DELIVERY_STATUSES,
    ALERT_FREQUENCIES,
    WEBHOOK_DELIVERY_STATUSES,
    WEBHOOK_EVENTS,
    ApiKey,
    ApiKeyUsageDaily,
    ScreenAlert,
    ScreenAlertDelivery,
    WebhookDelivery,
    WebhookEndpoint,
)
from baskfy_core.models.market import (
    MEMBERSHIP_SOURCES,
    CorporateAction,
    FundamentalDaily,
    IndexDef,
    IndexMemberDaily,
    IndexSnapshotDaily,
    OhlcvDaily,
)
from baskfy_core.models.pipeline import CURSOR_KIND_BARS, IngestCursor
from baskfy_core.models.reference import Exchange, Instrument, SymbolAlias, TradingDay
from baskfy_core.models.screens import (
    BasketSnapshot,
    MarketHealthDaily,
    Screen,
    ScreenRun,
)

__all__ = [
    "ADMIN_ACTIONS",
    "ALERT_DELIVERY_STATUSES",
    "ALERT_FREQUENCIES",
    "CONSENT_KINDS",
    "CURSOR_KIND_BARS",
    "MEMBERSHIP_SOURCES",
    "OVERRIDE_EFFECTS",
    "PAYMENT_STATUSES",
    "RAZORPAY_PROVIDER",
    "TOKEN_PURPOSES",
    "WEBHOOK_DELIVERY_STATUSES",
    "WEBHOOK_EVENTS",
    "WEBHOOK_STATUSES",
    "AccountDeletion",
    "AdminAction",
    "ApiKey",
    "ApiKeyUsageDaily",
    "AppUser",
    "AuthLockout",
    "AuthToken",
    "AuthVerificationToken",
    "Backtest",
    "Base",
    "BasketSnapshot",
    "ConsentRecord",
    "CorporateAction",
    "EntitlementOverride",
    "Exchange",
    "FactorDaily",
    "FundamentalDaily",
    "IndexDef",
    "IndexMemberDaily",
    "IndexSnapshotDaily",
    "IngestCursor",
    "Instrument",
    "InvoiceCounter",
    "JsonObject",
    "MarketHealthDaily",
    "OhlcvDaily",
    "Payment",
    "PipelineRun",
    "PipelineRunStep",
    "Plan",
    "Portfolio",
    "PortfolioHolding",
    "PortfolioRebalance",
    "PortfolioSleeve",
    "RefreshToken",
    "Screen",
    "ScreenAlert",
    "ScreenAlertDelivery",
    "ScreenRun",
    "Subscription",
    "SymbolAlias",
    "TradingDay",
    "WebhookDelivery",
    "WebhookEndpoint",
    "WebhookEvent",
]
