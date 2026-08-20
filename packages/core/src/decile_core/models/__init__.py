"""SQLAlchemy 2.0 models for every table in docs/04-data-model.md.

Importing this package registers every table on ``Base.metadata``, which is what Alembic's
``target_metadata`` points at.
"""

from decile_core.models.accounts import (
    AppUser,
    Backtest,
    Payment,
    PipelineRun,
    PipelineRunStep,
    Plan,
    Portfolio,
    PortfolioHolding,
    Subscription,
)
from decile_core.models.auth import (
    CONSENT_KINDS,
    TOKEN_PURPOSES,
    AccountDeletion,
    AuthLockout,
    AuthToken,
    AuthVerificationToken,
    ConsentRecord,
    RefreshToken,
)
from decile_core.models.base import Base, JsonObject
from decile_core.models.facts import FactorDaily
from decile_core.models.market import (
    MEMBERSHIP_SOURCES,
    CorporateAction,
    FundamentalDaily,
    IndexDef,
    IndexMemberDaily,
    IndexSnapshotDaily,
    OhlcvDaily,
)
from decile_core.models.pipeline import CURSOR_KIND_BARS, IngestCursor
from decile_core.models.reference import Exchange, Instrument, SymbolAlias, TradingDay
from decile_core.models.screens import MarketHealthDaily, Screen, ScreenRun

__all__ = [
    "CONSENT_KINDS",
    "CURSOR_KIND_BARS",
    "MEMBERSHIP_SOURCES",
    "TOKEN_PURPOSES",
    "AccountDeletion",
    "AppUser",
    "AuthLockout",
    "AuthToken",
    "AuthVerificationToken",
    "Backtest",
    "Base",
    "ConsentRecord",
    "CorporateAction",
    "Exchange",
    "FactorDaily",
    "FundamentalDaily",
    "IndexDef",
    "IndexMemberDaily",
    "IndexSnapshotDaily",
    "IngestCursor",
    "Instrument",
    "JsonObject",
    "MarketHealthDaily",
    "OhlcvDaily",
    "Payment",
    "PipelineRun",
    "PipelineRunStep",
    "Plan",
    "Portfolio",
    "PortfolioHolding",
    "RefreshToken",
    "Screen",
    "ScreenRun",
    "Subscription",
    "SymbolAlias",
    "TradingDay",
]
