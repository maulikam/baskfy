"""Compatibility imports for the former option-strategy skeleton.

Naked ``ShortStrangle`` planning was deliberately removed.  Use the defined-risk iron
condor or the long-option breakout strategy from :mod:`app.strategies.options`.
"""
from .options import (  # noqa: F401
    BreakoutBuyerPlanner,
    BuyingConfig,
    IntradayIronCondorSeller,
    IntradayOptionBreakoutBuyer,
    IronCondorPlanner,
    OptionInstrument,
    OptionMarketSnapshot,
    OptionPlan,
    OptionPlanRejected,
    OptionQuote,
    SellingConfig,
    evaluate_exit,
    instruments_from_kite,
)
