"""Every strategy knob lives here. Change behaviour here, not in scoring/rebalance code."""
import datetime as dt
import os
from dotenv import load_dotenv

load_dotenv()

# ---- Kite / runtime ----
KITE_API_KEY = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"   # simulate orders unless explicitly disabled
# P4.3: the merged gateway requires tenant ids on every order. The operator console is
# still one book; these stamp that pair so Friday rebalance keeps working. A second
# tenant is a different caller — never collapse a foreign principal onto these defaults.
SOLE_USER_ID = int(os.getenv("BASKFY_SOLE_USER_ID", "1"))
SOLE_BROKER_ACCOUNT_ID = int(os.getenv("BASKFY_SOLE_BROKER_ACCOUNT_ID", "1"))

# Kite authorises orders against an allowlist of IPs. This connection offers both families
# and prefers IPv6, so orders were refused for an address that cannot usefully be
# allowlisted (a rotating residential v6 prefix) while the v4 address sat in the console.
# Default ON: the failure it prevents is every order in a batch being rejected.
FORCE_IPV4 = os.getenv("FORCE_IPV4", "true").lower() != "false"

# --- browser-facing security ----------------------------------------------------------
# The interface places real orders. On a cloud host it is reached through an SSH tunnel,
# so it still answers only on loopback — but the browser holding that tunnel also visits
# the rest of the internet, and a page there can both read this app (DNS rebinding) and
# post to it (CSRF) without a single packet arriving from outside. See core/websec.py.
#
# "testserver" is the Host the test client sends. It is not a public name and nothing can
# resolve to it, so allowing it costs nothing and keeps the suite honest about the rest.
DESK_ALLOWED_HOSTS = os.getenv("DESK_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver")
DESK_PORT = int(os.getenv("DESK_PORT", "8420"))
# Empty means no password, which is right for a laptop on loopback. Set it on any shared
# or hosted box: loopback is not a boundary between users of the same machine.
DESK_PASSWORD = os.getenv("DESK_PASSWORD", "")
# The interactive API docs describe every route including the ones that place orders.
DESK_DOCS = os.getenv("DESK_DOCS", "false").lower() == "true"
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

# --- no-trade band -------------------------------------------------------------------
# A rebalance computes an exact target quantity, so a 0.3% price drift produces a one-share
# order. On 18 Aug 2026 a plan proposed trimming WELCORP by 1 share of 362, SONACOMS by 3
# of 861 and HFCL by 4 of 1,308 — Rs 16,102 of sells in total, every one of them costing
# Rs 20 brokerage plus STT plus half a spread to correct a drift worth less than the fee.
#
# So a delta is skipped when it is immaterial by EITHER measure: too small in rupees for
# the fixed costs to be worth paying, or too small a fraction of the position to be worth
# correcting. A full EXIT is never skipped — closing a position is risk reduction, and the
# band must never keep you in something the strategy wants out of.
MIN_TRADE_VALUE = 10_000.0                 # Rs; below this the fee dominates the benefit
MIN_TRADE_PCT = 2.0                        # % of the existing position
# A trade whose own costs exceed this share of its value is not worth making. It bites on
# SELLS, where the DP charge is a flat Rs 15.93 per scrip: that is 0.9% of a Rs 2,000 sale
# and 0.03% of a Rs 50,000 one. Buys have no fixed component and sit near 0.115% at any
# size, so this rule is effectively a floor on small sells — which is exactly where the
# waste was. It cannot reduce STT, which is 0.1% each side and 95% of a real session's
# bill; only trading less does that.
MAX_TRADE_COST_PCT = 0.20                  # % of the trade's own value
MAX_POS_VS_DAY_VALUE = 0.01                # position ≤ 1% of median daily traded value
HALF_SIZE_WEIGHT = 3.0                     # for short-history listings
SHORT_HISTORY_MONTHS = 18

# --- M14 §3: the shadow-mode flag flip (P2.6) --------------------------------------------------
# "upload" — /analyze needs a file, or an explicit generate_for=YYYY-MM-DD. This is the default
#            and stays the default until four consecutive Fridays of shadow mode come back green.
# "generated" — /analyze with neither builds the scan itself for the latest pipeline date.
#
# One flagged config change, per docs/SHADOW-MODE.md. Flipping it early is not a shortcut: on
# 2026-08-18 the two paths disagreed on four orders, and one of them was a substitution caused by
# a missing corporate action (NEEDS-MAULIK item 4), not by rounding.
SCAN_SOURCE_DEFAULT = os.getenv("SCAN_SOURCE_DEFAULT", "upload")

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

# ---- The swing book's gates (docs/swing/02-scope-and-gating.md, Track B) ---------------
# Read here, once, at import — never from a form, and never from the settings table. The
# reasoning is the one already written above LOCKED_KEYS in analytics/settings.py: a switch
# that decides whether real orders can be placed does not belong one click away from the
# button that places them.
#
# BASKFY_SWING_EXECUTION_ENABLED is the swing book's DRY_RUN, and it is DELIBERATELY
# INDEPENDENT of DRY_RUN itself. With it false, /swing/execute runs the entire path —
# guards, risk, rate limits, journal — through the gateway's dry-run adapter and records
# simulated=true whatever DRY_RUN says. That is what makes the twenty paper sessions the
# real-money gate counts (docs/swing/02 §3.2) a rehearsal of this code rather than of a
# different branch. Flipping it needs the five conditions in §3, by Maulik's hand.
SWING_EXECUTION_ENABLED = os.getenv("BASKFY_SWING_EXECUTION_ENABLED", "false").lower() == "true"
# The 09:15-10:45 opening-range monitor. Off until SW6's replay is green and one morning has
# been watched by a person.
SWING_MONITOR_ENABLED = os.getenv("BASKFY_SWING_MONITOR_ENABLED", "false").lower() == "true"
# The 08:50/09:09 pre-open gap scan. Independent of the monitor: either can run alone.
_EP_PREMARKET = os.getenv("BASKFY_SWING_EP_PREMARKET_ENABLED", "false")
SWING_EP_PREMARKET_ENABLED = _EP_PREMARKET.lower() == "true"
# The swing GTT band (PACK.3). The desk's own STOP_MIN/STOP_MAX (8-12%) encode vol-scaled
# stops for the WEEKLY book; a swing stop sits at the low of the day and is supposed to be
# tight, typically 2-6%. Passed to the gateway per call rather than changing the default,
# because widening the default would silently change the weekly book's findings.
SWING_STOP_BAND_MIN = float(os.getenv("BASKFY_SWING_STOP_BAND_MIN", "0.005"))
SWING_STOP_BAND_MAX = float(os.getenv("BASKFY_SWING_STOP_BAND_MAX", "0.10"))
# Where the swing GTT's LIMIT leg rests, as a fraction of its trigger (docs/swing/04 §9.4,
# PACK.8). He uses market stops; a GTT fires a LIMIT, and the gateway's own half-percent
# cushion (GTT_LIMIT_FRACTION, 0.995 — the weekly book's, unchanged) can be walked through by
# a fast-falling book, leaving a tight swing stop resting unfilled. 0.97 rests it 3% under.
# Passed per call to place_gtt_stop, so the weekly book's GTTs are byte-for-byte as before.
SWING_GTT_LIMIT_FRACTION = float(os.getenv("BASKFY_SWING_GTT_LIMIT_FRACTION", "0.97"))

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
