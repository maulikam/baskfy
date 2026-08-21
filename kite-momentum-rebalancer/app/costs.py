"""The cost model moved to :mod:`baskfy_core.costs` at M15 (P3.3).

Kept as a re-export so every existing `from . import costs` keeps working and the move stays a
move rather than a rewrite of fifty call sites. It was already pure — no config, no I/O — so
nothing had to change to make it fit core's first law.
"""

from baskfy_core.costs import *  # noqa: F401,F403
from baskfy_core.costs import (  # noqa: F401  — names the star import does not carry
    BROKERAGE_DELIVERY,
    DP_CHARGE_PER_SELL,
    EXCHANGE_TXN_NSE,
    GST,
    SEBI_TURNOVER,
    STAMP_DUTY_BUY,
    STT_DELIVERY,
    Cost,
    cost_pct,
    order_cost,
    plan_cost,
)
