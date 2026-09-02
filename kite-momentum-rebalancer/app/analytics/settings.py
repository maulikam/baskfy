"""Runtime-editable settings: DB overrides that beat the environment without a restart.

RESOLUTION ORDER
    settings table  ->  environment (.env)  ->  code default

.env stays the bootstrap so a fresh checkout runs with no database; the table is the live
layer. Every consumer reads C.<NAME> as a module attribute at call time, so applying an
override rewrites the attribute and the next plan uses it — no restart, no stale copy.

WHAT IS NOT EDITABLE HERE, AND WHY
- Credentials. KITE_API_KEY / KITE_API_SECRET never enter the database and are never
  rendered. A settings page that can display a secret is a settings page that can leak it.
- The safety switches: DRY_RUN, INTRADAY_ENABLED, OPTIONS_ENABLED. These decide whether
  real orders can be placed at all. Putting them one click away from /execute in a browser
  removes the deliberate friction that makes them trustworthy; they stay in .env, where
  changing them is a conscious act on the machine that runs the system.
- Scoring weights. MOMENTUM_BLEND and SHARPE_BLEND are the strategy's definition, not a
  tuning knob, and CLAUDE.md fences them off.
- The risk ceilings, since 22 Aug 2026. RISK_MAX_DAILY_LOSS_PCT is the kill switch;
  RISK_POSITION_HEADROOM, RISK_GROSS_MULTIPLE and RISK_MAX_ORDERS_PER_DAY bound position
  size, gross exposure and order flow. docs/03 §3f: "a security boundary, not a
  preference". They were editable while this was a one-person desk, where raising your own
  ceiling escalates nothing; that stops being true at the second account. .env plus a
  restart, and the effective values are logged at startup so the change leaves a trace.

EVERY CHANGE IS AUDITED. You cannot reconstruct why a trade was sized the way it was
without knowing what the parameters were at the time.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from . import db

# Never stored, never shown, never editable from a browser.
SECRET_KEYS = frozenset({"KITE_API_KEY", "KITE_API_SECRET"})
# Deliberately high-friction: env-only.
LOCKED_KEYS = frozenset({"DRY_RUN", "INTRADAY_ENABLED", "OPTIONS_ENABLED",
                         "PORT", "DB_PATH", "TOKEN_FILE",
                         # The risk ceilings and the kill switch (docs/03 §3f). A ceiling a
                         # user can raise is not a ceiling. See the note in SPECS below.
                         "RISK_MAX_DAILY_LOSS_PCT", "RISK_POSITION_HEADROOM",
                         "RISK_GROSS_MULTIPLE", "RISK_MAX_ORDERS_PER_DAY",
                         "RISK_MAX_POSITION_VALUE", "RISK_MAX_GROSS_EXPOSURE",
                         # The swing book (SW2). Same two categories as above, arriving
                         # together. BASKFY_SWING_EXECUTION_ENABLED is a safety switch — it
                         # decides whether a confirmed swing line may reach a broker at all,
                         # and docs/swing/02 §3 puts five conditions in front of it, none of
                         # them a click. The other two flags gate live market processes. The
                         # *_MAX keys are the M4.1 ceilings for sw_config: the value traded
                         # with is user-editable in the web app, the maximum it may take is
                         # server configuration.
                         "BASKFY_SWING_EXECUTION_ENABLED",
                         "BASKFY_SWING_MONITOR_ENABLED",
                         "BASKFY_SWING_EP_PREMARKET_ENABLED",
                         "BASKFY_SWING_RISK_PER_TRADE_PCT_MAX",
                         "BASKFY_SWING_MAX_POSITION_PCT_MAX",
                         "BASKFY_SWING_MAX_OPEN_POSITIONS_MAX"})


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Spec:
    key: str
    label: str
    group: str
    kind: str                       # int | float | bool | choice | intpair
    help: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    unit: str = ""

    def parse(self, raw: Any):
        """Text from a form -> a typed value, or a SettingsError naming the problem."""
        s = str(raw).strip()
        try:
            if self.kind == "int":
                v: Any = int(float(s))
            elif self.kind == "float":
                v = float(s)
            elif self.kind == "bool":
                v = s.lower() in {"1", "true", "on", "yes"}
            elif self.kind == "choice":
                if s not in self.choices:
                    raise SettingsError(
                        f"{self.label}: {s!r} is not one of {', '.join(self.choices)}")
                v = s
            elif self.kind == "intpair":
                parts = [int(float(p)) for p in s.replace(" ", "").split(",") if p]
                if len(parts) != 2:
                    raise SettingsError(f"{self.label}: expected two numbers, got {s!r}")
                if parts[0] > parts[1]:
                    raise SettingsError(
                        f"{self.label}: minimum {parts[0]} exceeds maximum {parts[1]}")
                v = tuple(parts)
            else:
                v = s
        except SettingsError:
            raise
        except (TypeError, ValueError):
            raise SettingsError(f"{self.label}: {s!r} is not a valid {self.kind}")

        check = v[0] if self.kind == "intpair" else v
        if self.kind in {"int", "float", "intpair"}:
            hi = v[1] if self.kind == "intpair" else v
            if self.minimum is not None and check < self.minimum:
                raise SettingsError(f"{self.label}: {v} is below the minimum {self.minimum}")
            if self.maximum is not None and hi > self.maximum:
                raise SettingsError(f"{self.label}: {v} is above the maximum {self.maximum}")
        return v

    def to_text(self, value: Any) -> str:
        if self.kind == "intpair":
            return f"{value[0]},{value[1]}"
        if self.kind == "bool":
            return "true" if value else "false"
        return str(value)


# =====================================================================================
# what can be edited
# =====================================================================================
SPECS: tuple[Spec, ...] = (
    # --- position sizing -------------------------------------------------------------
    Spec("TARGET_POSITIONS", "Target positions", "Position sizing", "intpair",
         "Soft range, resolved by score dispersion. Half-size provisional entries count "
         "toward the ceiling.", minimum=1, maximum=60),
    Spec("MAX_SINGLE_WEIGHT", "Max single weight", "Position sizing", "float",
         "Percent of the ACTIVE EQUITY SLEEVE, not of NAV. In R3 the sleeve is 40% of "
         "NAV, so 15% here is 6% of NAV.", minimum=1.0, maximum=100.0, unit="%"),
    Spec("MIN_POSITION_WEIGHT", "Min position weight", "Position sizing", "float",
         "Also sleeve-relative. Half-size entries may sit below it deliberately.",
         minimum=0.1, maximum=100.0, unit="%"),
    Spec("CLUSTER_CAP", "Cluster cap", "Position sizing", "float",
         "Max weight per sector cluster, applied on the sleeve.",
         minimum=1.0, maximum=100.0, unit="%"),
    Spec("MAX_POS_VS_DAY_VALUE", "Liquidity cap", "Position sizing", "float",
         "Position must stay under this fraction of median daily traded value.",
         minimum=0.0001, maximum=1.0),
    Spec("FULLY_INVESTED", "Ignore cash bands", "Position sizing", "bool",
         "True holds no strategic cash regardless of breadth."),

    # --- rebalance discipline ---------------------------------------------------------
    Spec("RETENTION_BUFFER", "Retention buffer", "Rebalance discipline", "int",
         "Keep holdings ranked within N + buffer of the cutoff.", minimum=0, maximum=50),
    Spec("REPLACEMENT_EDGE", "Replacement edge", "Rebalance discipline", "float",
         "A challenger must beat the incumbent's score by this to force a swap.",
         minimum=0.0, maximum=50.0, unit="pts"),
    Spec("RUNNER_MAX_VALUE", "Runner cap", "Rebalance discipline", "float",
         "Rupee ceiling for a filter-rejected name kept as a runner.",
         minimum=0.0, maximum=100_000_000.0, unit="Rs"),
    Spec("PARABOLIC_RSI", "Parabolic RSI", "Rebalance discipline", "float",
         "Trim winners above this RSI to the runner weight.", minimum=50.0, maximum=100.0),
    Spec("TRIM_TO_WEIGHT", "Trim-to weight", "Rebalance discipline", "float",
         "Weight retained as a trailed runner after a parabolic trim.",
         minimum=0.0, maximum=100.0, unit="%"),

    # --- stops -------------------------------------------------------------------------
    Spec("STOP_MIN", "Stop floor", "Stops", "float",
         "Minimum GTT stop distance below the reference price.",
         minimum=0.01, maximum=0.5),
    Spec("STOP_MAX", "Stop ceiling", "Stops", "float",
         "Maximum stop distance.", minimum=0.01, maximum=0.9),
    Spec("STOP_VOL_MULT", "Stop volatility multiple", "Stops", "float",
         "Weekly volatility multiple before clamping to the floor and ceiling.",
         minimum=0.1, maximum=10.0),

    # --- risk limits: NOT EDITABLE HERE ---------------------------------------------------
    # The four ceilings that used to sit here (RISK_MAX_DAILY_LOSS_PCT, the kill switch;
    # RISK_POSITION_HEADROOM; RISK_GROSS_MULTIPLE; RISK_MAX_ORDERS_PER_DAY) were moved to
    # LOCKED_KEYS on 22 Aug 2026. docs/03 §3f names this exact case: "This split is a
    # security boundary, not a preference: RISK_MAX_DAILY_LOSS_PCT must not become a form
    # field." For a single operator nothing was escalated by editing your own ceiling; with
    # a second account the same form is privilege escalation, and the boundary has to exist
    # before the account does. They change in .env plus a restart, and the effective values
    # are logged at startup so a change is still visible after the fact.
    # `risk_preview()` below still SHOWS what they resolve to -- read-only display is the
    # point, and it matters more now that they cannot be typed over.

    # --- regime overlay -----------------------------------------------------------------
    Spec("REGIME_ENABLED", "Overlay enabled", "Regime overlay", "bool",
         "Master switch. Off means the overlay displays a view and changes nothing."),
    Spec("REGIME_MODE", "Rollout mode", "Regime overlay", "choice",
         "observe displays only; propose builds a plan for review; enforce lets an "
         "approved plan reach the gateway. DRY_RUN still overrides all three.",
         choices=("observe", "propose", "enforce")),
    Spec("REGIME_BUFFER_BPS", "Signal buffer", "Regime overlay", "int",
         "Schmitt-trigger band around each moving average. 150 = 1.50%.",
         minimum=0, maximum=1000, unit="bps"),
    Spec("REGIME_CONFIRM_DAYS", "Confirmation days", "Regime overlay", "int",
         "Consecutive closes needed to confirm a side. Each is compared against that "
         "day's own moving average.", minimum=1, maximum=20),
    Spec("REGIME_WEEKLY_EVAL_WEEKDAY", "Evaluation day", "Regime overlay", "choice",
         "Weekly evaluation day. A holiday falls back to the last completed session.",
         choices=("0", "1", "2", "3", "4", "5", "6")),
    Spec("REGIME_INDEX_STALE_DAYS", "Index stale after", "Regime overlay", "int",
         "Older index candles block new buys but never force selling.",
         minimum=1, maximum=90, unit="days"),
    Spec("REGIME_BREADTH_STALE_DAYS", "Breadth stale after", "Regime overlay", "int",
         "", minimum=1, maximum=180, unit="days"),
    Spec("REGIME_MIN_BREADTH_COVERAGE_PCT", "Min breadth coverage", "Regime overlay",
         "float", "Below this, breadth can neither force selling nor permit Risk-On.",
         minimum=0.0, maximum=100.0, unit="%"),
    Spec("REGIME_R4_EQUITY_PCT", "R4 equity cap", "Regime overlay", "float",
         "Set 0 for genuine full cash rather than calling a 10% residual a full exit.",
         minimum=0.0, maximum=100.0, unit="%"),
    Spec("REGIME_R4_MAX_RESIDUAL_NAMES", "R4 residual names", "Regime overlay", "int",
         "0 forces R4 to full cash rather than inventing names to retain.",
         minimum=0, maximum=20),
    Spec("REGIME_HALF_SIZE_FACTOR", "Half-size factor", "Regime overlay", "float",
         "Fraction of full size for provisional R2 entries.", minimum=0.05, maximum=1.0),
    Spec("REGIME_NEARBY_RANK_BAND", "Nearby rank band", "Regime overlay", "int",
         "Within this band, unrealised losers are sold before winners.",
         minimum=1, maximum=30),
    Spec("REGIME_BOOTSTRAP_POLICY", "Bootstrap policy", "Regime overlay", "choice",
         "observe_only never forces sells merely because no prior state exists.",
         choices=("observe_only", "adopt_candidate")),
    Spec("REGIME_LTCG_REVIEW_DAYS", "LTCG review window", "Regime overlay", "int",
         "Flag a profitable lot this many days short of long-term treatment.",
         minimum=0, maximum=365, unit="days"),

    # --- analytics ------------------------------------------------------------------------
    Spec("INCLUDE_UNTOUCHABLE_IN_NAV", "Count untouchables in NAV", "Analytics", "bool",
         "Off keeps SGB/G-sec out of NAV so their P&L is not attributed to momentum."),
)

BY_KEY: Mapping[str, Spec] = {s.key: s for s in SPECS}
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(s.group for s in SPECS))


# =====================================================================================
# resolution
# =====================================================================================
def stored(conn) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def _code_default(key: str):
    from .. import config as C
    return getattr(C, key, None)


def effective(conn) -> dict[str, dict]:
    """Every editable setting with its value and where that value came from."""
    db_values = stored(conn)
    out: dict[str, dict] = {}
    for spec in SPECS:
        if key_in := db_values.get(spec.key):
            value, source = spec.parse(key_in), "database"
        elif os.getenv(spec.key) is not None:
            value, source = spec.parse(os.getenv(spec.key)), "environment"
        else:
            value, source = _code_default(spec.key), "code default"
        out[spec.key] = {"spec": spec, "value": value, "text": spec.to_text(value),
                         "source": source}
    return out


def apply_to_config(conn) -> dict[str, Any]:
    """Push stored overrides onto the config module.

    Safe because every consumer reads C.<NAME> as a module attribute at call time, so the
    next plan sees the new value. Nothing binds a copy at import.
    """
    from .. import config as C
    applied = {}
    for key, text in stored(conn).items():
        spec = BY_KEY.get(key)
        if spec is None:                       # an override for a key we no longer expose
            continue
        value = spec.parse(text)
        setattr(C, key, value)
        applied[key] = value
    return applied


# =====================================================================================
# validation
# =====================================================================================
def validate(candidate: Mapping[str, Any]) -> list[str]:
    """Cross-field checks. A value can be individually legal and jointly impossible."""
    from ..core.regime import ConfigError, RegimeConfig
    from ..core.regime_alloc import feasible_position_range

    problems: list[str] = []
    g = lambda k: candidate.get(k, _code_default(k))

    lo_w, hi_w = g("MIN_POSITION_WEIGHT"), g("MAX_SINGLE_WEIGHT")
    if lo_w > hi_w:
        problems.append(f"Min position weight ({lo_w}%) exceeds max single weight ({hi_w}%)")
    else:
        lo_n, hi_n = feasible_position_range(lo_w, hi_w)
        want_lo, want_hi = g("TARGET_POSITIONS")
        if want_hi > hi_n:
            problems.append(
                f"{want_hi} positions at a {lo_w}% floor need {want_hi * lo_w:.0f}% of the "
                f"sleeve; only 100% exists. Feasible range is {lo_n}-{hi_n}.")
        if want_lo < lo_n:
            problems.append(
                f"{want_lo} positions cannot absorb the sleeve without breaching the "
                f"{hi_w}% cap; at least {lo_n} are needed.")

    if g("STOP_MIN") > g("STOP_MAX"):
        problems.append(f"Stop floor ({g('STOP_MIN')}) exceeds stop ceiling ({g('STOP_MAX')})")

    if g("REGIME_R4_EQUITY_PCT") > 40.0:
        problems.append("R4 equity cap must stay at or below the R3 cap of 40%")
    if g("REGIME_R4_EQUITY_PCT") > 0 and g("REGIME_R4_MAX_RESIDUAL_NAMES") == 0:
        problems.append(
            "R4 permits equity but names no residual positions. Set the residual name "
            "count above zero, or set the R4 cap to 0 for genuine full cash.")

    try:
        RegimeConfig(
            buffer_bps=int(g("REGIME_BUFFER_BPS")),
            confirm_days=int(g("REGIME_CONFIRM_DAYS")),
            weekly_evaluation_weekday=int(g("REGIME_WEEKLY_EVAL_WEEKDAY")),
            index_stale_days=int(g("REGIME_INDEX_STALE_DAYS")),
            breadth_stale_days=int(g("REGIME_BREADTH_STALE_DAYS")),
            min_breadth_coverage_pct=float(g("REGIME_MIN_BREADTH_COVERAGE_PCT")),
            tier_exposure_pct={"R1": 100.0, "R2": 70.0, "R3": 40.0,
                               "R4": float(g("REGIME_R4_EQUITY_PCT"))},
        ).validate()
    except ConfigError as exc:
        problems.extend(str(exc).splitlines()[1:])
    return problems


# =====================================================================================
# writing
# =====================================================================================
def save(conn, updates: Mapping[str, Any], *, note: str = "") -> dict:
    """Validate, persist, audit and apply. Nothing is written unless everything passes."""
    typed: dict[str, Any] = {}
    errors: list[str] = []
    for key, raw in updates.items():
        if key in SECRET_KEYS or key in LOCKED_KEYS:
            errors.append(f"{key} is not editable from the interface")
            continue
        spec = BY_KEY.get(key)
        if spec is None:
            errors.append(f"{key} is not a known setting")
            continue
        try:
            typed[key] = spec.parse(raw)
        except SettingsError as exc:
            errors.append(str(exc))
    if errors:
        raise SettingsError("; ".join(errors))

    current = {k: v["value"] for k, v in effective(conn).items()}
    problems = validate({**current, **typed})
    if problems:
        raise SettingsError("; ".join(problems))

    now = dt.datetime.now().isoformat(timespec="seconds")
    existing = stored(conn)
    # Compare against the value actually IN FORCE, not against the stored row. The stored
    # row is absent the first time, which would log every field as a change and bury the
    # real ones.
    def in_force(key: str) -> str:
        return BY_KEY[key].to_text(current[key])

    changed = {k: v for k, v in typed.items() if BY_KEY[k].to_text(v) != in_force(k)}

    with db.transaction(conn):
        for key, value in typed.items():
            text = BY_KEY[key].to_text(value)
            if text == in_force(key) and key not in existing:
                continue                       # identical to what is already in force
            conn.execute(
                "INSERT INTO settings(key, value, updated_at, note) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at, note=excluded.note",
                (key, text, now, note or None))
            if key in changed:
                conn.execute(
                    "INSERT INTO settings_audit(key, old_value, new_value, changed_at, note)"
                    " VALUES(?,?,?,?,?)", (key, in_force(key), text, now, note or None))
    apply_to_config(conn)
    return {"saved": len(typed), "changed": sorted(changed), "at": now}


def reset(conn, keys: Sequence[str] | None = None, *, note: str = "reset") -> dict:
    """Drop overrides so the environment/code default takes over again."""
    targets = list(keys) if keys else list(stored(conn))
    now = dt.datetime.now().isoformat(timespec="seconds")
    existing = stored(conn)
    with db.transaction(conn):
        for key in targets:
            if key in existing:
                conn.execute(
                    "INSERT INTO settings_audit(key, old_value, new_value, changed_at, note)"
                    " VALUES(?,?,?,?,?)", (key, existing[key], "<reset>", now, note))
                conn.execute("DELETE FROM settings WHERE key=?", (key,))
    # Re-import so the module attributes fall back to their environment/code values.
    import importlib
    from .. import config as C
    importlib.reload(C)
    apply_to_config(conn)
    return {"reset": [k for k in targets if k in existing]}


def history(conn, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT key, old_value, new_value, changed_at, note FROM settings_audit "
        "ORDER BY id DESC LIMIT ?", (limit,))]


def locked_view() -> list[dict]:
    """The env-only settings, shown read-only so the page is honest about them."""
    from .. import config as C
    rows = []
    for key, why in (
        ("DRY_RUN", "Global no-order guarantee. Kept in .env so enabling live trading is "
                    "a deliberate act on the machine, not a click in a browser."),
        ("INTRADAY_ENABLED", "Product gate. Same reasoning."),
        ("OPTIONS_ENABLED", "Product gate. Same reasoning."),
        ("PORT", "Read once at startup."),
        ("DB_PATH", "Changing it mid-run would split the book across two databases."),
        ("TOKEN_FILE", "Holds the Kite access token."),
    ):
        rows.append({"key": key, "value": getattr(C, key, None), "why": why})
    return rows


def inr_short(v: float) -> str:
    """Lakh/crore gloss. `15,750,000` is arithmetic; `1.58 cr` is a quantity you can
    weigh against a portfolio. The full figure is always shown beside it."""
    a = abs(v)
    if a >= 1e7:
        return f"{v / 1e7:,.2f} cr"
    if a >= 1e5:
        return f"{v / 1e5:,.2f} L"
    return f"{v:,.0f}"


def risk_preview(nav: float) -> dict:
    """What the risk limits actually mean, in rupees, at the current NAV.

    WHY THIS EXISTS
    "Daily loss cap: 5.0%" is not a number anyone can accept or reject. "Trading halts
    for the day after losing Rs 5,25,000" is. Every limit here is derived from NAV, so
    the percentage alone tells you nothing until it is multiplied out — and a limit
    nobody can evaluate is a limit nobody sets deliberately.

    Calls config.risk_config, the SAME function the gateway uses, rather than repeating
    the arithmetic. A preview that re-derived these would eventually disagree with what
    is actually enforced, which is worse than showing nothing.
    """
    from .. import config as C

    cfg = C.risk_config(nav)
    derived = not (C.RISK_MAX_POSITION_VALUE or C.RISK_MAX_GROSS_EXPOSURE)
    legal_max = nav * C.MAX_SINGLE_WEIGHT / 100.0 if nav > 0 else 0.0
    return {
        "nav": nav, "nav_short": inr_short(nav),
        "have_nav": nav > 0,
        "derived": derived,
        "rows": [
            {"key": "RISK_MAX_DAILY_LOSS_PCT",
             "value": f"₹{cfg.max_daily_loss:,.0f}",
             "short": inr_short(cfg.max_daily_loss),
             "means": "Trading halts for the rest of the day once losses reach this."},
            {"key": "RISK_POSITION_HEADROOM",
             "value": f"₹{cfg.max_position_value:,.0f}",
             "short": inr_short(cfg.max_position_value),
             "means": (f"Largest single position the gateway will allow. The strategy's "
                       f"own cap is {C.MAX_SINGLE_WEIGHT}% of NAV = "
                       f"₹{legal_max:,.0f}.")},
            {"key": "RISK_GROSS_MULTIPLE",
             "value": f"₹{cfg.max_gross_exposure:,.0f}",
             "short": inr_short(cfg.max_gross_exposure),
             "means": "Total exposure ceiling across all positions."},
            {"key": "RISK_MAX_ORDERS_PER_DAY",
             "value": f"{cfg.max_orders_per_day:,}", "short": "",
             "means": (f"A full rebalance of {C.TARGET_POSITIONS[1]} positions is at most "
                       f"{C.TARGET_POSITIONS[1] * 2} orders plus stops.")},
        ],
        # Surfaced rather than only logged: an incoherent limit blocks something the
        # strategy is configured to do, and the warning belongs where it is edited.
        "problems": C.risk_coherence(cfg, nav) if nav > 0 else [],
    }
