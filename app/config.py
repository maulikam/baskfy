"""Every strategy knob lives here. Change behaviour here, not in scoring/rebalance code."""
import datetime as dt
import os
from dotenv import load_dotenv

load_dotenv()

# ---- Kite / runtime ----
KITE_API_KEY = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"   # simulate orders unless explicitly disabled
PORT = int(os.getenv("PORT", "8420"))
TOKEN_FILE = os.getenv("TOKEN_FILE", "data/.kite_token.json")

# ---- Universe hygiene ----
EXCLUDED_SYMBOLS = {"SGBDE31III"}          # untouchable long-term instruments (SGBs etc.)
REJECT_SERIES = {"BE", "BZ"}
MIN_MEDIAN_DAILY_VALUE = 5e7               # ₹5 cr
MAX_CIRCUITS_3M = 5
PENALTY_CIRCUITS_1Y = 8
MAX_AWAY_FROM_HIGH = -30.0                 # reject if further below 1-yr high than this

# ---- Portfolio construction ----
# POSITION-WEIGHT SEMANTICS (regime overlay): these are percentages of the ACTIVE EQUITY
# SLEEVE, not of total NAV. With the regime disabled the sleeve is 100% of NAV and they
# behave exactly as before. In R3 the sleeve is 40%, so a 10% sleeve position = 4% of NAV.
MAX_SINGLE_WEIGHT = 15.0                   # % of the equity sleeve
MIN_POSITION_WEIGHT = 6.0                  # % of the sleeve (half-size entries may sit below)
CLUSTER_CAP = 25.0                         # % per sector cluster, applied on the sleeve
TARGET_POSITIONS = (12, 15)                # soft range, resolved by score dispersion
MAX_POS_VS_DAY_VALUE = 0.01                # position ≤ 1% of median daily traded value
HALF_SIZE_WEIGHT = 3.0                     # for short-history listings
SHORT_HISTORY_MONTHS = 18

# Cash bands by breadth (% of universe above 20-DMA)
CASH_BANDS = [                             # (min_breadth, cash_pct)
    (65.0, 5.0),                           # bullish → 5% cash
    (45.0, 15.0),                          # neutral → 15%
    (0.0, 35.0),                           # weak → 35%
]
FULLY_INVESTED = os.getenv("FULLY_INVESTED", "false").lower() == "true"  # overrides cash bands

# ---- Product gates (read by core/gateway.py, enforced before any order) ----
# The rebalancer is CNC-only. These are the switches gateway.py checks to block MIS and
# F&O. Without them it raised AttributeError instead of returning a clean BLOCKED: it
# failed closed either way, but a missing constant is not a gate — this is.
INTRADAY_ENABLED = os.getenv("INTRADAY_ENABLED", "false").lower() == "true"
OPTIONS_ENABLED = os.getenv("OPTIONS_ENABLED", "false").lower() == "true"

# ---- Options research engines (PAPER-ONLY; the product gates above remain OFF) --------
# Return figures are evaluation benchmarks, never an instruction to trade until hit.
OPTION_UNDERLYING = os.getenv("OPTION_UNDERLYING", "NIFTY")
OPTION_SELL_MONTHLY_BENCHMARK_PCT = float(
    os.getenv("OPTION_SELL_MONTHLY_BENCHMARK_PCT", "5.0"))
OPTION_SELL_ENTRY_START = os.getenv("OPTION_SELL_ENTRY_START", "09:45")
OPTION_SELL_ENTRY_END = os.getenv("OPTION_SELL_ENTRY_END", "13:30")
OPTION_SELL_RISK_PER_TRADE_PCT = float(os.getenv("OPTION_SELL_RISK_PER_TRADE_PCT", "0.75"))
OPTION_SELL_SHORT_DELTA = float(os.getenv("OPTION_SELL_SHORT_DELTA", "0.16"))
OPTION_SELL_WING_DELTA = float(os.getenv("OPTION_SELL_WING_DELTA", "0.05"))
OPTION_SELL_TARGET_CAPTURE_PCT = float(os.getenv("OPTION_SELL_TARGET_CAPTURE_PCT", "35"))
OPTION_SELL_STOP_CREDIT_MULTIPLE = float(
    os.getenv("OPTION_SELL_STOP_CREDIT_MULTIPLE", "1.5"))

OPTION_BUY_TRADE_BENCHMARK_PCT = float(
    os.getenv("OPTION_BUY_TRADE_BENCHMARK_PCT", "15.0"))
OPTION_BUY_ENTRY_START = os.getenv("OPTION_BUY_ENTRY_START", "09:35")
OPTION_BUY_ENTRY_END = os.getenv("OPTION_BUY_ENTRY_END", "14:30")
OPTION_BUY_RISK_PER_TRADE_PCT = float(os.getenv("OPTION_BUY_RISK_PER_TRADE_PCT", "0.50"))
OPTION_BUY_DELTA = float(os.getenv("OPTION_BUY_DELTA", "0.60"))
OPTION_BUY_PREMIUM_STOP_PCT = float(os.getenv("OPTION_BUY_PREMIUM_STOP_PCT", "25"))
OPTION_BUY_TRAIL_ACTIVATION_PCT = float(
    os.getenv("OPTION_BUY_TRAIL_ACTIVATION_PCT", "15"))
OPTION_BUY_TRAIL_DRAWDOWN_PCT = float(os.getenv("OPTION_BUY_TRAIL_DRAWDOWN_PCT", "8"))
OPTION_INTRADAY_SQUARE_OFF = os.getenv("OPTION_INTRADAY_SQUARE_OFF", "15:12")


def option_selling_config():
    from .strategies.options import SellingConfig

    cfg = SellingConfig(
        underlying=OPTION_UNDERLYING,
        entry_start=dt.time.fromisoformat(OPTION_SELL_ENTRY_START),
        entry_end=dt.time.fromisoformat(OPTION_SELL_ENTRY_END),
        square_off=dt.time.fromisoformat(OPTION_INTRADAY_SQUARE_OFF),
        short_delta=OPTION_SELL_SHORT_DELTA,
        wing_delta=OPTION_SELL_WING_DELTA,
        risk_per_trade_pct=OPTION_SELL_RISK_PER_TRADE_PCT,
        target_credit_capture_pct=OPTION_SELL_TARGET_CAPTURE_PCT,
        stop_credit_multiple=OPTION_SELL_STOP_CREDIT_MULTIPLE,
        monthly_return_benchmark_pct=OPTION_SELL_MONTHLY_BENCHMARK_PCT,
    )
    cfg.validate()
    return cfg


def option_buying_config():
    from .strategies.options import BuyingConfig

    cfg = BuyingConfig(
        underlying=OPTION_UNDERLYING,
        entry_start=dt.time.fromisoformat(OPTION_BUY_ENTRY_START),
        entry_end=dt.time.fromisoformat(OPTION_BUY_ENTRY_END),
        square_off=dt.time.fromisoformat(OPTION_INTRADAY_SQUARE_OFF),
        target_delta=OPTION_BUY_DELTA,
        risk_per_trade_pct=OPTION_BUY_RISK_PER_TRADE_PCT,
        premium_stop_pct=OPTION_BUY_PREMIUM_STOP_PCT,
        trail_activation_pct=OPTION_BUY_TRAIL_ACTIVATION_PCT,
        trail_drawdown_pct=OPTION_BUY_TRAIL_DRAWDOWN_PCT,
        target_return_benchmark_pct=OPTION_BUY_TRADE_BENCHMARK_PCT,
    )
    cfg.validate()
    return cfg

# ---- Runners: held names that fail filters or go parabolic ----
RUNNER_MAX_VALUE = 300_000                 # ₹ cap for a filter-rejected held name
PARABOLIC_RSI = 82.0                       # trim winners above this to TRIM_TO_WEIGHT
TRIM_TO_WEIGHT = 2.0                       # % kept as trailed runner after trim

# ---- Rebalance discipline ----
RETENTION_BUFFER = 5                       # keep holdings ranked within N+buffer
REPLACEMENT_EDGE = 8.0                     # challenger must beat incumbent score by this
STOP_MIN, STOP_MAX, STOP_VOL_MULT = 0.08, 0.12, 2.2

# ---- Scoring weights (see SKILL.md before editing) ----
MOMENTUM_BLEND = {"one_month": .10, "three_months": .30, "six_months": .30,
                  "nine_months": .15, "one_year": .15}
SHARPE_BLEND = {"three_months": .35, "six_months": .35, "nine_months": .15, "one_year": .15}

# ---- Risk limits (core/risk.py; enforced on every order since /execute was routed
#      through the gateway). Absolute rupee values, or blank to DERIVE from live NAV so
#      they cannot contradict the strategy's own sizing as the book grows.
RISK_MAX_POSITION_VALUE = os.getenv("RISK_MAX_POSITION_VALUE", "")      # blank = derive
RISK_MAX_GROSS_EXPOSURE = os.getenv("RISK_MAX_GROSS_EXPOSURE", "")      # blank = derive
RISK_MAX_DAILY_LOSS_PCT = float(os.getenv("RISK_MAX_DAILY_LOSS_PCT", "5.0"))
RISK_MAX_ORDERS_PER_DAY = int(os.getenv("RISK_MAX_ORDERS_PER_DAY", "500"))
# Headroom between the largest position the strategy may hold and the risk cap, for price
# movement between planning and fill. Without it the cap blocks a legal position.
RISK_POSITION_HEADROOM = float(os.getenv("RISK_POSITION_HEADROOM", "1.25"))
RISK_GROSS_MULTIPLE = float(os.getenv("RISK_GROSS_MULTIPLE", "1.5"))


def risk_config(nav: float | None = None):
    """Build RiskConfig, deriving limits from NAV unless explicitly overridden.

    Derivation exists because a hardcoded rupee cap silently contradicts the strategy:
    MAX_SINGLE_WEIGHT of 15% on a Rs 1.05cr book is a Rs 15.8L position, which the old
    Rs 15L cap would have blocked outright.
    """
    from .core.risk import RiskConfig

    base = float(nav) if nav else 0.0
    if RISK_MAX_POSITION_VALUE:
        pos_cap = float(RISK_MAX_POSITION_VALUE)
    elif base > 0:
        pos_cap = base * MAX_SINGLE_WEIGHT / 100.0 * RISK_POSITION_HEADROOM
    else:
        pos_cap = 1_500_000.0
    gross_cap = (float(RISK_MAX_GROSS_EXPOSURE) if RISK_MAX_GROSS_EXPOSURE
                 else (base * RISK_GROSS_MULTIPLE if base > 0 else 12_000_000.0))
    daily_loss = base * RISK_MAX_DAILY_LOSS_PCT / 100.0 if base > 0 else 100_000.0
    return RiskConfig(max_daily_loss=round(daily_loss, 2),
                      max_orders_per_day=RISK_MAX_ORDERS_PER_DAY,
                      max_position_value=round(pos_cap, 2),
                      max_gross_exposure=round(gross_cap, 2))


def risk_coherence(cfg, nav: float) -> list[str]:
    """Contradictions between the risk limits and the strategy's own sizing.

    A risk cap is meant to stop the unintended, not to forbid what the strategy is
    configured to do. Anything reported here would block a legal action.
    """
    problems = []
    if nav > 0:
        legal_max = nav * MAX_SINGLE_WEIGHT / 100.0
        if cfg.max_position_value < legal_max:
            problems.append(
                f"max_position_value Rs {cfg.max_position_value:,.0f} is below the "
                f"largest position the strategy permits "
                f"({MAX_SINGLE_WEIGHT}% of NAV = Rs {legal_max:,.0f}) — it would block a "
                f"legal position")
        if cfg.max_gross_exposure < nav:
            problems.append(
                f"max_gross_exposure Rs {cfg.max_gross_exposure:,.0f} is below current "
                f"NAV Rs {nav:,.0f} — every order would be refused")
        min_positions = TARGET_POSITIONS[0]
        if cfg.max_orders_per_day < min_positions * 2:
            problems.append(
                f"max_orders_per_day {cfg.max_orders_per_day} cannot cover a "
                f"{min_positions}-position rebalance")
    return problems


# ---- Analytics / persistence (phase 1) ----
DB_PATH = os.getenv("DB_PATH", "data/portfolio.db")
INDEX_BASE = 100.0                         # portfolio index value on the inception date
# Untouchable instruments (SGB/G-sec, see core/guards.py) are recorded in every snapshot's
# holdings_json but kept OUT of nav/invested by default: they are never traded by the
# strategy, so counting them would attribute their P&L to momentum. Flip to include them
# if you want snapshots to track total account value instead of strategy value.
INCLUDE_UNTOUCHABLE_IN_NAV = os.getenv("INCLUDE_UNTOUCHABLE_IN_NAV", "false").lower() == "true"
# Daily TWR convention: external flows are treated as landing at END of day, so they earn
# no return on the day they arrive:  r_t = (nav_t - flow_t) / nav_{t-1} - 1
CASHFLOW_AT_END_OF_DAY = True


# ---- Regime overlay (checkpoint 2) ----------------------------------------------------
# The canonical defaults live in core/regime.RegimeConfig; these env-overridable knobs
# feed it. regime_config() validates on every call, so a bad value fails loudly at the
# adapter boundary rather than silently mis-trading. Nothing here touches scoring.
REGIME_MODE = os.getenv("REGIME_MODE", "observe")            # observe | propose | enforce
REGIME_BUFFER_BPS = int(os.getenv("REGIME_BUFFER_BPS", "150"))
REGIME_CONFIRM_DAYS = int(os.getenv("REGIME_CONFIRM_DAYS", "3"))
REGIME_WEEKLY_EVAL_WEEKDAY = int(os.getenv("REGIME_WEEKLY_EVAL_WEEKDAY", "4"))  # Friday
REGIME_INDEX_STALE_DAYS = int(os.getenv("REGIME_INDEX_STALE_DAYS", "5"))
REGIME_BREADTH_STALE_DAYS = int(os.getenv("REGIME_BREADTH_STALE_DAYS", "10"))
REGIME_MIN_BREADTH_COVERAGE_PCT = float(os.getenv("REGIME_MIN_BREADTH_COVERAGE_PCT", "90"))
REGIME_R4_EQUITY_PCT = float(os.getenv("REGIME_R4_EQUITY_PCT", "10"))
REGIME_BOOTSTRAP_POLICY = os.getenv("REGIME_BOOTSTRAP_POLICY", "observe_only")
REGIME_LTCG_REVIEW_DAYS = int(os.getenv("REGIME_LTCG_REVIEW_DAYS", "45"))
REGIME_ENABLED = os.getenv("REGIME_ENABLED", "false").lower() == "true"
REGIME_NEARBY_RANK_BAND = int(os.getenv("REGIME_NEARBY_RANK_BAND", "3"))
# R4 residual policy must be EXPLICIT. If you set max residual names to 0, R4 becomes full
# cash — which is the honest configuration for "exit everything", rather than describing a
# 10% residual as a full exit.
REGIME_R4_MAX_RESIDUAL_NAMES = int(os.getenv("REGIME_R4_MAX_RESIDUAL_NAMES", "2"))
REGIME_R4_RESIDUAL_ORDERING = tuple(
    os.getenv("REGIME_R4_RESIDUAL_ORDERING", "rank,liquidity").split(","))
REGIME_HALF_SIZE_FACTOR = float(os.getenv("REGIME_HALF_SIZE_FACTOR", "0.5"))


def regime_config():
    """Build and VALIDATE the regime configuration. Raises regime.ConfigError if bad."""
    from .core.regime import BootstrapPolicy, RegimeConfig, RegimeMode

    cfg = RegimeConfig(
        buffer_bps=REGIME_BUFFER_BPS,
        confirm_days=REGIME_CONFIRM_DAYS,
        weekly_evaluation_weekday=REGIME_WEEKLY_EVAL_WEEKDAY,
        index_stale_days=REGIME_INDEX_STALE_DAYS,
        breadth_stale_days=REGIME_BREADTH_STALE_DAYS,
        min_breadth_coverage_pct=REGIME_MIN_BREADTH_COVERAGE_PCT,
        tier_exposure_pct={"R1": 100.0, "R2": 70.0, "R3": 40.0, "R4": REGIME_R4_EQUITY_PCT},
        bootstrap_policy=BootstrapPolicy(REGIME_BOOTSTRAP_POLICY),
        mode=RegimeMode(REGIME_MODE),
    )
    cfg.validate()
    return cfg
