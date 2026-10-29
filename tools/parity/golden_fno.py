"""The F&O lane of the parity goldens: ``baskfy_core.fno`` (Rust rewrite wave 3, 29 Oct 2026).

    cd decile-blueprint
    PYTHONPATH=..:packages/core/src:packages/providers/src:packages/execution/src \\
        .venv/bin/python ../tools/parity/golden_fno.py

Dumps the public pure functions of every ``baskfy_core.fno`` module into
``go/testdata/golden/L1/fno/<module>.<function>.case_NNN.json``, written ``stable=True`` by
``tools/parity/golden.py``'s own encoder, so two runs are byte-identical. The money paths are
covered hardest — ``gating`` (every flag combination), ``exits`` (every exit reason and its
boundary), ``costs``, ``sizing``, ``ledger`` / ``journal`` (every pause) — then the calendar, the
covered-overnight predicate, the checklist, the monitor, F3's ``directional`` rules, and the
frame functions (``series``, ``underlying``, ``vol``, ``scan``, ``research``, ``retest``,
``directional_retest``) over seeded synthetic bhavcopies.

Conventions the reimplementation must not misread (restated in every case's ``meta.notes``):

* Every case's inputs are JSON, and every function is called from the stored inputs after a
  round trip through their JSON text — never from an object that was not written down.
* Decimals are strings; enums are their values; a ``time`` is ``HH:MM:SS``; a naive datetime is
  exchange-local IST. A config input is the config dataclass as a dict (nested for
  ``FnoConfig``), decoded field by field by its **declared** type (golden.py's ``_sub_config``
  rule: a ``Decimal`` field arrives as a string and is decoded as a ``Decimal``). A config dict
  that names only some fields takes the defaults for the rest (only the ``config.*`` constructor
  cases do that).
* A mapping keyed by a leg role is ``{"LONG_PUT": ..., ...}``; a mapping keyed by
  ``(strike, option_type)`` is a list of ``{"strike", "option_type", "price"}`` rows.
* A function that raises stores ``{"raises": {"type", "message"}}`` as its output.
* An output dataclass is its fields, plus the read-only properties a caller reads (``total``,
  ``simulated``, ``reason``, ``win_rate``, ``width``, ...) — named in ``PROPERTIES`` below.
* A polars frame is ``{"columns", "dtypes", "rows"}`` (golden.py's encoding); floats in frames
  are compared at ``meta.tolerance``.
* Where a function takes a non-JSON object built by another public function (``run_symbol``
  takes ``index_panel``'s panel; ``condor_trades``/``debit_trades`` take
  ``build_option_panel``'s panel), the case stores the raw rows and its notes name the
  composition.
* Synthetic bars come from ``numpy.random.default_rng(seed)`` and are rounded to 2 dp before
  they are written, so the stored inputs, not the generator, are the contract.

What is not dumped, and why: ``retest.slice_loader`` returns a closure (exercised through
``run_family``); ``research.build_option_panel`` returns dicts keyed by tuples of floats and dates
(its output is dumped as a summary, and it is exercised whole through ``condor_trades`` and
``debit_trades``); ``retest.run_all`` is ``run_family`` over ``FAMILIES`` with one shared series
(the families it runs are dumped one by one); the private ``_``-prefixed helpers. ``monitor`` is
pure (law 1, ``test_fno_purity.py``) and is dumped.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import importlib.util
import itertools
import json
import math
import sys
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CORE_SRC = ROOT / "decile-blueprint" / "packages" / "core" / "src"

try:  # the venv's editable install can point at a moved checkout; the source tree is the truth
    import baskfy_core  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, str(CORE_SRC))


def _golden() -> Any:
    name = "baskfy_parity_golden"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / "golden.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LANE = "L1/fno"
PREFIX = "baskfy_core.fno."
NOTES = (
    "Inputs are JSON and each case is recomputed from them; a raise is stored as "
    '{"raises": {"type", "message"}}. Decimals are strings, enums their values, times HH:MM:SS, '
    "naive datetimes IST. Config dicts decode by each field's declared type. Polars frames are "
    "{columns, dtypes, rows}. Regenerate with `PYTHONPATH=..:packages/core/src "
    ".venv/bin/python ../tools/parity/golden_fno.py` from decile-blueprint."
)

# ================================================================================================
# Encoding helpers
# ================================================================================================

#: Read-only properties an output dataclass carries beside its fields (a caller reads them).
PROPERTIES: dict[str, tuple[str, ...]] = {
    "FnoGate": ("simulated",),
    "FoPause": ("reason",),
    "CostLines": ("total",),
    "FutureCharges": ("total",),
    "ChargeBreakdown": ("total",),
    "LedgerFigures": ("total_inr", "win_rate"),
    "CondorStrikes": ("call_width", "put_width", "max_width"),
    "StrikeChoice": ("state",),
    "SpreadStrikes": ("width",),
    "AddDecision": ("allowed",),
    "AtmIv": ("iv",),
    "LegQuote": ("two_sided", "mid"),
    "FoFillRecord": ("cash",),
    "CostFill": ("turnover",),
}


def _key(k: Any) -> str:
    if isinstance(k, enum.Enum):
        return str(k.value)
    if isinstance(k, (dt.date, dt.time)):
        return k.isoformat()
    if isinstance(k, tuple):
        return "|".join(_key(x) for x in k)
    if isinstance(k, Decimal):
        return format(k, "f")
    return str(k)


def _out(o: Any) -> Any:  # noqa: PLR0911 - one branch per shape
    """Plain values for the shared encoder: enums to values, times to ISO, dataclasses to their
    fields plus ``PROPERTIES``. Polars frames pass through (golden.py encodes them natively)."""
    if isinstance(o, enum.Enum):
        return o.value
    if isinstance(o, dt.time):
        return o.isoformat()
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        doc = {f.name: _out(getattr(o, f.name)) for f in dataclasses.fields(o)}
        for prop in PROPERTIES.get(type(o).__name__, ()):
            doc[prop] = _out(getattr(o, prop))
        return doc
    if isinstance(o, Mapping):
        return {_key(k): _out(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_out(x) for x in o]
    if isinstance(o, (set, frozenset)):
        return sorted((_out(x) for x in o), key=json.dumps)
    return o


def _raised(exc: BaseException) -> dict[str, Any]:
    message = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
    return {"raises": {"type": type(exc).__name__, "message": str(message)}}


def _capture(call: Callable[[], Any]) -> Any:
    try:
        return _out(call())
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        return _raised(exc)


def D(value: Any) -> Decimal:  # noqa: N802 - reads like the type it builds
    return Decimal(str(value))


def Dn(value: Any) -> Decimal | None:  # noqa: N802
    return None if value is None else Decimal(str(value))


def day(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def dayn(value: str | None) -> dt.date | None:
    return None if value is None else dt.date.fromisoformat(value)


def stamp(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def stampn(value: str | None) -> dt.datetime | None:
    return None if value is None else dt.datetime.fromisoformat(value)


def days(values: list[str]) -> list[dt.date]:
    return [day(v) for v in values]


def _weekdays(start: dt.date, n: int) -> list[dt.date]:
    out: list[dt.date] = []
    cur = start
    while len(out) < n:
        if cur.weekday() < 5:  # noqa: PLR2004
            out.append(cur)
        cur += dt.timedelta(days=1)
    return out


def _iso(values: list[dt.date]) -> list[str]:
    return [v.isoformat() for v in values]


# --- configs -------------------------------------------------------------------------------------


def _config_classes() -> dict[str, Any]:
    from baskfy_core.fno import config as fc
    from baskfy_core.options.config import CostRates

    return {
        name: getattr(fc, name)
        for name in (
            "FnoCeilings",
            "CommonConfig",
            "F1Config",
            "F2Config",
            "F3Config",
            "FutureCostRates",
            "StopVolConfig",
            "SeriesConfig",
            "BookConfig",
            "FnoConfig",
        )
    } | {"CostRates": CostRates}


def _typed(kind: str, value: Any) -> Any:  # noqa: PLR0911 - one branch per declared type
    if value is None:
        return None
    kind = kind.replace(" | None", "")
    if kind == "Decimal":
        return Decimal(str(value))
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    if kind == "bool":
        return bool(value)
    if kind == "str":
        return str(value)
    if kind == "dt.time":
        return dt.time.fromisoformat(value)
    if kind == "dt.date":
        return dt.date.fromisoformat(value)
    if kind.startswith("tuple["):
        return tuple(value)
    classes = _config_classes()
    if kind in classes:
        return cfg(classes[kind], value)
    raise TypeError(f"golden_fno: no JSON rule for declared type {kind!r}")


def cfg(cls: Any, raw: Mapping[str, Any]) -> Any:
    """A config dataclass from its JSON dict, each field decoded by its declared type; a field
    the dict leaves out takes its default."""
    kwargs = {
        f.name: _typed(str(f.type), raw[f.name])
        for f in dataclasses.fields(cls)
        if f.name in raw
    }
    return cls(**kwargs)


def cfg_doc(obj: Any) -> Any:
    return _out(obj)


def _doc(name: str, **over: Any) -> Any:
    """A config's JSON dict with ``over`` applied, built through the decoder so every override
    is held to its declared type (a ``"0.5"`` override of a ``Decimal`` field is ``Decimal``)."""
    cls = _config_classes()[name]
    enc = _golden()._enc
    raw = enc(cfg_doc(cls())) | enc(_out(over))
    return cfg_doc(cfg(cls, raw))


def _fno_config_doc() -> Any:
    from baskfy_core.fno.config import FnoConfig

    return cfg_doc(FnoConfig())


def _fno_config(raw: Mapping[str, Any]) -> Any:
    from baskfy_core.fno.config import FnoConfig

    return cfg(FnoConfig, raw)


def _ceil_doc(**over: Any) -> Any:
    return _doc("FnoCeilings", **over)


def _ceilings(raw: Mapping[str, Any]) -> Any:
    from baskfy_core.fno.config import FnoCeilings

    return cfg(FnoCeilings, raw)


def _common_doc(**over: Any) -> Any:
    return _doc("CommonConfig", **over)


def _f1_doc(**over: Any) -> Any:
    return _doc("F1Config", **over)


def _f2_doc(**over: Any) -> Any:
    return _doc("F2Config", **over)


def _f3_doc(**over: Any) -> Any:
    return _doc("F3Config", **over)


def _stopvol_doc() -> Any:
    from baskfy_core.fno.config import StopVolConfig

    return cfg_doc(StopVolConfig())


def _series_doc(**over: Any) -> Any:
    return _doc("SeriesConfig", **over)


def _future_rates_doc(**over: Any) -> Any:
    return _doc("FutureCostRates", **over)


def _option_rates_doc() -> Any:
    from baskfy_core.options.config import CostRates

    return cfg_doc(CostRates())


def _C(name: str, raw: Mapping[str, Any]) -> Any:  # noqa: N802
    return cfg(_config_classes()[name], raw)


# --- polars frames -------------------------------------------------------------------------------

_PL_DTYPES = ("Float64", "Int64", "Int32", "Int8", "UInt32", "Boolean", "String", "Date", "Null")


def frame_doc(columns: list[str], dtypes: Mapping[str, str], rows: list[list[Any]]) -> dict[str, Any]:
    """A polars frame as golden.py encodes one, written directly (dates as ISO strings)."""
    enc = _golden()._enc
    # A Float64 column is written as floats, as golden.py writes a polars frame: an int landing
    # in one (a whole-number spot) would otherwise print as 50000 where polars holds 50000.0.
    floats = [dtypes[c] in ("Float64", "Float32") for c in columns]

    def cell(v: Any, is_float: bool) -> Any:
        return float(v) if is_float and isinstance(v, int) and not isinstance(v, bool) else v

    return {
        "columns": list(columns),
        "dtypes": {c: dtypes[c] for c in columns},
        "rows": [enc([cell(v, f) for v, f in zip(r, floats, strict=True)]) for r in rows],
    }


def frame(doc: Mapping[str, Any]) -> Any:
    """A polars frame from ``{"columns", "dtypes", "rows"}`` with the dtypes it was written with."""
    import polars as pl

    names = {
        "Float64": pl.Float64,
        "Int64": pl.Int64,
        "Int32": pl.Int32,
        "Int8": pl.Int8,
        "UInt32": pl.UInt32,
        "Boolean": pl.Boolean,
        "String": pl.String,
        "Date": pl.Date,
        "Null": pl.Null,
    }
    columns: list[str] = doc["columns"]
    schema = {c: names[doc["dtypes"][c]] for c in columns}
    rows = [
        [
            day(v) if (schema[c] is pl.Date and v is not None) else v
            for c, v in zip(columns, row, strict=True)
        ]
        for row in doc["rows"]
    ]
    return pl.DataFrame(rows, schema=schema, orient="row")


# ================================================================================================
# config
# ================================================================================================


def call_group_of(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import FoSleeve, group_of

    return {s: group_of(FoSleeve(s)) for s in inputs["sleeves"]}


def call_f1_sleeve_for(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import f1_sleeve_for

    return _capture(lambda: f1_sleeve_for(inputs["underlying"]))


def call_f3_sleeve_for(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import f3_sleeve_for

    return _capture(lambda: f3_sleeve_for(inputs["underlying"]))


def call_risk_per_trade_pct(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import FoSleeve

    config = _fno_config(inputs["config"])
    return {s: config.risk_per_trade_pct(FoSleeve(s)) for s in inputs["sleeves"]}


def call_ceiling_violations(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import ceiling_violations

    return ceiling_violations(_fno_config(inputs["config"]), _ceilings(inputs["ceilings"]))


def call_config_construct(inputs: dict[str, Any]) -> Any:
    """``<Class>(**overrides)``: the config as constructed (every field), or the bound it broke."""
    return _capture(lambda: _C(inputs["class"], inputs["overrides"]))


def _config_cases() -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    p = PREFIX + "config."
    out.append((p + "group_of", "every sleeve to its flag group",
                {"sleeves": ["F1N", "F1B", "F2", "F3N", "F3B"]}))  # fmt: skip
    for u in ("NIFTY", "BANKNIFTY", "FINNIFTY", "nifty"):
        out.append((p + "f1_sleeve_for", f"f1_sleeve_for({u!r})", {"underlying": u}))
    for u in ("NIFTY", "BANKNIFTY", "MIDCPNIFTY"):
        out.append((p + "f3_sleeve_for", f"f3_sleeve_for({u!r})", {"underlying": u}))
    sleeves = ["F1N", "F1B", "F2", "F3N", "F3B"]
    base = _fno_config_doc()
    out.append((p + "FnoConfig.risk_per_trade_pct", "defaults, every sleeve",
                {"config": base, "sleeves": sleeves}))  # fmt: skip
    custom = json.loads(json.dumps(_golden()._enc(base)))
    custom["f1"]["risk_per_trade_pct"] = "0.75"
    custom["f2"]["risk_per_trade_pct"] = "0.5"
    custom["f3"]["risk_per_trade_pct"] = "0.9"
    out.append((p + "FnoConfig.risk_per_trade_pct", "each group its own percent",
                {"config": custom, "sleeves": sleeves}))  # fmt: skip
    cv = p + "ceiling_violations"
    out.append((cv, "defaults sit inside the default ceilings",
                {"config": base, "ceilings": _ceil_doc()}))  # fmt: skip
    over = json.loads(json.dumps(_golden()._enc(base)))
    over["f1"]["risk_per_trade_pct"] = "1.5"
    over["f3"]["risk_per_trade_pct"] = "2.0"
    over["book"]["monthly_pause_inr"] = "100000"
    over["book"]["max_open_positions"] = 12
    over["book"]["max_per_underlying"] = 2
    over["f2"]["max_open"] = 10
    out.append((cv, "every field above its ceiling, in order",
                {"config": over, "ceilings": _ceil_doc()}))  # fmt: skip
    out.append((cv, "f2.max_open above a lowered ceiling only",
                {"config": base, "ceilings": _ceil_doc(max_open_positions_max=4)}))  # fmt: skip
    eq = json.loads(json.dumps(_golden()._enc(base)))
    eq["f1"]["risk_per_trade_pct"] = "1.0"
    eq["book"]["monthly_pause_inr"] = "75000"
    out.append((cv, "a field equal to its ceiling is admissible",
                {"config": eq, "ceilings": _ceil_doc()}))  # fmt: skip
    out.append((cv, "a stricter percent ceiling catches all three sleeves",
                {"config": base, "ceilings": _ceil_doc(risk_pct_max="0.5")}))  # fmt: skip
    cc = p + "construct"
    for cls, over_, why in (
        ("FnoConfig", {}, "the whole default contract"),
        ("CommonConfig", {"hard_exit_before_expiry": 0}, "hard exit 0 refused (never zero)"),
        ("CommonConfig", {"max_lots": 11}, "max lots above its ceiling refused"),
        ("CommonConfig", {"plan_ttl_minutes": 0}, "a zero-minute plan refused"),
        ("F1Config", {"underlyings": ["NIFTY", "FINNIFTY"]}, "F1 underlying outside the pack"),
        ("F1Config", {"entry_sessions_before": 21}, "entry sessions above 20 refused"),
        ("F1Config", {"plan_time": "09:10:00"}, "entry window before 09:15 refused"),
        ("F1Config", {"loss_close_mult": "2.0", "profit_take_pct": "80"}, "both at their upper bounds"),
        ("F1Config", {"pause_loss_r": "0"}, "a non-negative pause R refused"),
        ("F2Config", {"stop_atr": "1.4"}, "stop ATR below 1.5 refused"),
        ("F2Config", {"month_pause_r": "0"}, "a non-negative month pause refused"),
        ("F3Config", {"full_share_pct": "20", "entry_share_pct": "25"}, "full share below entry share refused"),
        ("F3Config", {"underlyings": ["BANKNIFTY"]}, "F3 on BANKNIFTY only"),
        ("F3Config", {"level_buffer_pct": "1.5"}, "level buffer above 1 % refused"),
        ("StopVolConfig", {"stop_min": 0.12, "stop_max": 0.08}, "an inverted stop band refused"),
        ("SeriesConfig", {"ca_down_ratio": 1.1}, "a CA band that does not straddle 1 refused"),
        ("BookConfig", {"max_per_underlying": 0}, "max per underlying 0 refused"),
        ("FnoCeilings", {"risk_pct_max": "0"}, "a zero percent ceiling refused"),
        ("FnoCeilings", {"book_monthly_loss_inr_max": "-1"}, "a negative rupee ceiling refused"),
    ):  # fmt: skip
        out.append((cc, f"{cls}: {why}", {"class": cls, "overrides": over_}))
    return out


# ================================================================================================
# gating
# ================================================================================================


def call_fno_gates(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import FoSleeve
    from baskfy_core.fno.gating import FnoFlags, fno_gates

    flags = FnoFlags(**{k: bool(v) for k, v in inputs["flags"].items()})
    return fno_gates(FoSleeve(inputs["sleeve"]), flags)


def call_execution_enabled(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import FoSleeve
    from baskfy_core.fno.gating import FnoFlags

    flags = FnoFlags(**{k: bool(v) for k, v in inputs["flags"].items()})
    return {s: flags.execution_enabled(FoSleeve(s)) for s in inputs["sleeves"]}


def call_gating_constants(_inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno import gating as g

    return {
        "EXECUTION_FLAG_ENV": dict(g.EXECUTION_FLAG_ENV),
        "CARRY_FLAG_ENV": g.CARRY_FLAG_ENV,
        "OPERATIONAL_FLAG_ENV": g.OPERATIONAL_FLAG_ENV,
        "DESK_SWITCH_ENV": g.DESK_SWITCH_ENV,
        "default_flags": g.FnoFlags(),
    }


def _gating_cases() -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    fn = PREFIX + "gating.fno_gates"
    names = ("dry_run", "options_enabled", "fno_carry_enabled")
    for combo in itertools.product([True, False], repeat=4):
        flags = dict(zip(names, combo[:3], strict=True)) | {"f1_execution_enabled": combo[3]}
        why = "F1N, " + ", ".join(f"{k}={v}" for k, v in flags.items())
        out.append((fn, why, {"sleeve": "F1N", "flags": flags}))
    live = {"dry_run": False, "options_enabled": True, "fno_carry_enabled": True}
    for sleeve, flag in (("F1B", "f1_execution_enabled"), ("F2", "f2_execution_enabled"),
                         ("F3N", "f3_execution_enabled"), ("F3B", "f3_execution_enabled")):  # fmt: skip
        out.append((fn, f"{sleeve} with all four of its switches on: LIVE",
                    {"sleeve": sleeve, "flags": live | {flag: True}}))  # fmt: skip
    out.append((fn, "F2 with F1's and F3's execution flags on but not its own: PAPER, paper gates",
                {"sleeve": "F2", "flags": live | {"f1_execution_enabled": True,
                                                  "f3_execution_enabled": True}}))  # fmt: skip
    out.append((fn, "F3B with only F1's flag on: PAPER",
                {"sleeve": "F3B", "flags": live | {"f1_execution_enabled": True}}))  # fmt: skip
    out.append((fn, "F2 half-flipped (own flag on, carry off): PAPER, switches pass through",
                {"sleeve": "F2", "flags": {"dry_run": False, "options_enabled": True,
                                           "fno_carry_enabled": False,
                                           "f2_execution_enabled": True}}))  # fmt: skip
    out.append((fn, "the default flags: PAPER", {"sleeve": "F1N", "flags": {}}))
    ee = PREFIX + "gating.FnoFlags.execution_enabled"
    sleeves = ["F1N", "F1B", "F2", "F3N", "F3B"]
    for flags in ({"f1_execution_enabled": True}, {"f2_execution_enabled": True},
                  {"f3_execution_enabled": True}, {}):  # fmt: skip
        out.append((ee, f"which sleeves read {flags or 'no flag'}",
                    {"flags": flags, "sleeves": sleeves}))  # fmt: skip
    out.append((PREFIX + "gating.constants", "the env names and the safe default flags", {}))
    return out


# ================================================================================================
# condor
# ================================================================================================


def _roles(raw: Mapping[str, Any]) -> dict[Any, Decimal]:
    from baskfy_core.fno.condor import LegRole

    return {LegRole(k): D(v) for k, v in raw.items()}


def _strikes(raw: Mapping[str, Any]) -> Any:
    from baskfy_core.fno.condor import CondorStrikes

    return CondorStrikes(D(raw["long_call"]), D(raw["short_call"]), D(raw["short_put"]),
                         D(raw["long_put"]))  # fmt: skip


def _quotes(raw: Mapping[str, Any]) -> dict[Any, Any]:
    from baskfy_core.fno.condor import LegQuote, LegRole

    return {LegRole(k): LegQuote(Dn(q["bid"]), Dn(q["ask"]), int(q["oi_lots"]))
            for k, q in raw.items()}  # fmt: skip


def call_one_sd(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.condor import one_sd

    return _capture(lambda: one_sd(D(inputs["forward"]), float(inputs["iv"]),
                                   int(inputs["days_to_expiry"])))  # fmt: skip


def call_select_strikes(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.condor import select_strikes
    from baskfy_core.fno.config import F1Config

    return _capture(lambda: select_strikes(
        forward=D(inputs["forward"]), iv=float(inputs["iv"]),
        days_to_expiry=int(inputs["days_to_expiry"]),
        call_strikes=[D(k) for k in inputs["call_strikes"]],
        put_strikes=[D(k) for k in inputs["put_strikes"]],
        config=cfg(F1Config, inputs["config"]),
    ))  # fmt: skip


def call_credit(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.condor import credit

    return _capture(lambda: credit(_roles(inputs["mids"])))


def call_condor_max_loss(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.condor import max_loss_per_unit

    return max_loss_per_unit(_strikes(inputs["strikes"]), D(inputs["credit"]))


def call_price_structure(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.condor import price_structure

    return _capture(lambda: price_structure(_strikes(inputs["strikes"]), _roles(inputs["mids"])))


def call_liquidity_refusals(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.condor import liquidity_refusals
    from baskfy_core.fno.config import F1Config

    return liquidity_refusals(_strikes(inputs["strikes"]), _quotes(inputs["quotes"]),
                              cfg(F1Config, inputs["config"]))  # fmt: skip


def call_leg_quote(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.condor import LegQuote

    return [LegQuote(Dn(q["bid"]), Dn(q["ask"]), int(q["oi_lots"])) for q in inputs["quotes"]]


def call_cost_to_close(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.condor import cost_to_close

    return _capture(lambda: cost_to_close(_strikes(inputs["strikes"]), _roles(inputs["marks"])))


def call_condor_levels(inputs: dict[str, Any]) -> Any:
    """``profit_take_level``, ``loss_close_level`` and the two predicates at one cost to close."""
    from baskfy_core.fno import condor as c
    from baskfy_core.fno.config import F1Config

    f1 = cfg(F1Config, inputs["config"])
    credit_, strikes = D(inputs["entry_credit"]), _strikes(inputs["strikes"])
    cost = D(inputs["close_cost"])
    return {
        "profit_take_level": c.profit_take_level(credit_, f1),
        "loss_close_level": c.loss_close_level(credit_, strikes, f1),
        "profit_take_hit": c.profit_take_hit(cost, credit_, f1),
        "loss_close_hit": c.loss_close_hit(cost, credit_, strikes, f1),
    }


def call_condor_roles(_inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno import condor as c

    return {
        "ENTRY_SEQUENCE": c.ENTRY_SEQUENCE,
        "EXIT_SEQUENCE": c.EXIT_SEQUENCE,
        "option_type_of": {r: c.option_type_of(r) for r in c.LegRole},
        "sign_of": {r: c.sign_of(r) for r in c.LegRole},
    }


STRIKES_A = {"long_call": "21100", "short_call": "20800", "short_put": "19200", "long_put": "18900"}
STRIKES_SKEW = {"long_call": "21200", "short_call": "20800", "short_put": "19200", "long_put": "19000"}
GOOD_MIDS = {"LONG_PUT": "35", "LONG_CALL": "30", "SHORT_PUT": "70", "SHORT_CALL": "60"}


def _strike_grid(lo: int, hi: int, step: int) -> list[str]:
    return [str(k) for k in range(lo, hi + 1, step)]


def _condor_cases() -> list[tuple[str, str, dict[str, Any]]]:  # noqa: PLR0915 - a flat table
    out: list[tuple[str, str, dict[str, Any]]] = []
    p = PREFIX + "condor."
    out.append((p + "roles", "entry/exit sequences, option type and sign of every leg", {}))
    for fwd, iv, dte, why in (("20000", 0.15, 21, "NIFTY-like, 21 days"),
                              ("48000", 0.18, 30, "BANKNIFTY-like, 30 days"),
                              ("20000", 0.15, 1, "one day left"),
                              ("20000.55", 0.123456789, 17, "a fractional forward and IV"),
                              ("20000", 0.0, 21, "zero IV refused"),
                              ("20000", 0.15, 0, "zero days refused")):  # fmt: skip
        out.append((p + "one_sd", why, {"forward": fwd, "iv": iv, "days_to_expiry": dte}))
    grid = _strike_grid(18000, 22000, 100)
    ss = p + "select_strikes"
    f1 = _f1_doc()
    out.append((ss, "04 §2's table on a 100-point grid",
                {"forward": "20000", "iv": 0.15, "days_to_expiry": 21, "call_strikes": grid,
                 "put_strikes": grid, "config": f1}))  # fmt: skip
    fine = _strike_grid(18000, 22000, 50)
    out.append((ss, "sd exactly 500: every target lands on a listed strike and takes it (>= / <=)",
                {"forward": "20000", "iv": 0.025, "days_to_expiry": 365, "call_strikes": fine,
                 "put_strikes": fine, "config": f1}))  # fmt: skip
    out.append((ss, "a coarse grid puts each wing on its short: REJECTED_STRUCTURE, both named",
                {"forward": "20000", "iv": 0.15, "days_to_expiry": 21,
                 "call_strikes": ["20000", "22000"], "put_strikes": ["18000", "20000"], "config": f1}))  # fmt: skip
    out.append((ss, "no call strike far enough: every missing leg named",
                {"forward": "20000", "iv": 0.15, "days_to_expiry": 21,
                 "call_strikes": ["20000", "20200"], "put_strikes": grid, "config": f1}))  # fmt: skip
    out.append((ss, "no strikes at all",
                {"forward": "20000", "iv": 0.15, "days_to_expiry": 21, "call_strikes": [],
                 "put_strikes": [], "config": f1}))  # fmt: skip
    out.append((ss, "wider sigmas from config: the long put falls off the grid",
                {"forward": "48000", "iv": 0.18, "days_to_expiry": 30,
                 "call_strikes": _strike_grid(42000, 55000, 500),
                 "put_strikes": _strike_grid(42000, 55000, 500),
                 "config": _f1_doc(short_sigma=D("1.5"), wing_sigma=D("1.0"))}))  # fmt: skip
    out.append((ss, "unsorted, duplicated listed strikes",
                {"forward": "20000", "iv": 0.15, "days_to_expiry": 21,
                 "call_strikes": ["21100", "20800", "20800", "22000", "20900"],
                 "put_strikes": ["18900", "19200", "18000", "19200"], "config": f1}))  # fmt: skip
    out.append((ss, "a zero IV refuses before any strike is read",
                {"forward": "20000", "iv": 0.0, "days_to_expiry": 21, "call_strikes": grid,
                 "put_strikes": grid, "config": f1}))  # fmt: skip
    for mids, why in ((GOOD_MIDS, "credit 65"),
                      ({"LONG_PUT": "35.05", "LONG_CALL": "30.10", "SHORT_PUT": "70.25", "SHORT_CALL": "59.95"}, "fractional mids"),
                      ({"LONG_PUT": "50", "LONG_CALL": "50", "SHORT_PUT": "40", "SHORT_CALL": "40"}, "a debit: negative credit"),
                      ({"LONG_PUT": "35", "LONG_CALL": "30", "SHORT_PUT": "70"}, "a missing leg raises")):  # fmt: skip
        out.append((p + "credit", why, {"mids": mids}))
    for strikes, cred, why in ((STRIKES_A, "65", "equal wings"), (STRIKES_SKEW, "65", "the wider width rules"),
                               (STRIKES_A, "320", "a credit beyond the width: negative loss")):  # fmt: skip
        out.append((p + "max_loss_per_unit", why, {"strikes": strikes, "credit": cred}))
    ps = p + "price_structure"
    for strikes, mids, why in (
        (STRIKES_A, GOOD_MIDS, "clean: credit 65, loss 235"),
        (STRIKES_A, {"LONG_PUT": "50", "LONG_CALL": "50", "SHORT_PUT": "50", "SHORT_CALL": "50"}, "zero credit: REJECTED_STRUCTURE"),
        (STRIKES_A, {"LONG_PUT": "60", "LONG_CALL": "60", "SHORT_PUT": "50", "SHORT_CALL": "50"}, "negative credit"),
        (STRIKES_A, {"LONG_PUT": "1", "LONG_CALL": "1", "SHORT_PUT": "200", "SHORT_CALL": "102"}, "credit equal to the width: loss not positive"),
        (STRIKES_SKEW, GOOD_MIDS, "skewed wings"),
    ):  # fmt: skip
        out.append((ps, why, {"strikes": strikes, "mids": mids}))
    q = lambda bid, ask, oi=1000: {"bid": bid, "ask": ask, "oi_lots": oi}  # noqa: E731
    good_q = {"LONG_PUT": q("34", "36"), "LONG_CALL": q("29", "31"),
              "SHORT_PUT": q("69", "71"), "SHORT_CALL": q("59", "61")}  # fmt: skip
    lr = p + "liquidity_refusals"
    out.append((lr, "every leg liquid", {"strikes": STRIKES_A, "quotes": good_q, "config": f1}))
    out.append((lr, "a short below the OI floor and a wide short",
                {"strikes": STRIKES_A, "config": f1,
                 "quotes": good_q | {"SHORT_PUT": q("69", "71", 499),
                                     "SHORT_CALL": q("55", "65")}}))  # fmt: skip
    out.append((lr, "a spread exactly at 5 % of mid passes",
                {"strikes": STRIKES_A, "config": f1,
                 "quotes": good_q | {"SHORT_CALL": q("58.5", "61.5")}}))  # fmt: skip
    out.append((lr, "a wing with no bid, a short with a zero ask, a missing leg",
                {"strikes": STRIKES_A, "config": f1,
                 "quotes": {"LONG_PUT": q(None, "36"), "SHORT_PUT": q("69", "0"),
                            "SHORT_CALL": q("59", "61")}}))  # fmt: skip
    out.append((lr, "a wing with tiny OI is fine (OI is a short's test)",
                {"strikes": STRIKES_A, "config": f1,
                 "quotes": good_q | {"LONG_CALL": q("29", "31", 0)}}))  # fmt: skip
    out.append((lr, "no quotes at all", {"strikes": STRIKES_A, "quotes": {}, "config": f1}))
    out.append((p + "LegQuote", "two_sided and mid over the edge cases",
                {"quotes": [q("10", "12"), q(None, "12"), q("10", None), q("0", "12"),
                            q("12", "10"), q("10.05", "10.10")]}))  # fmt: skip
    ctc = p + "cost_to_close"
    for marks, why in (
        ({"LONG_PUT": "10", "LONG_CALL": "8", "SHORT_PUT": "30", "SHORT_CALL": "25"}, "inside the bounds"),
        ({"LONG_PUT": "10", "LONG_CALL": "30", "SHORT_PUT": "30", "SHORT_CALL": "25"}, "an inverted call vertical clamps to 0"),
        ({"LONG_PUT": "5", "LONG_CALL": "8", "SHORT_PUT": "700", "SHORT_CALL": "25"}, "a put vertical beyond its width clamps to it"),
        ({"LONG_PUT": "0", "LONG_CALL": "0", "SHORT_PUT": "0", "SHORT_CALL": "0"}, "all zero"),
        ({"LONG_PUT": "10", "SHORT_PUT": "30", "SHORT_CALL": "25"}, "a missing mark raises"),
    ):  # fmt: skip
        out.append((ctc, why, {"strikes": STRIKES_A, "marks": marks}))
    lv = p + "levels"
    for cost, credit_, strikes, cfg_, why in (
        ("32.5", "65", STRIKES_A, f1, "exactly the profit-take level: hit"),
        ("32.51", "65", STRIKES_A, f1, "a paisa above the profit-take level: no"),
        ("162.5", "65", STRIKES_A, f1, "exactly (1 + 1.5) x credit: loss close"),
        ("162.49", "65", STRIKES_A, f1, "a paisa below the loss close"),
        ("280", "150", STRIKES_A, f1, "the loss level clamps to the 300 width"),
        ("300", "150", STRIKES_A, f1, "at the clamped width: loss close"),
        ("20", "65", STRIKES_A, _f1_doc(profit_take_pct=D("70"), loss_close_mult=D("1.0")), "config 70 % / 1.0x"),
    ):  # fmt: skip
        out.append((lv, why, {"close_cost": cost, "entry_credit": credit_, "strikes": strikes,
                              "config": cfg_}))  # fmt: skip
    return out


# ================================================================================================
# costs
# ================================================================================================


def call_crossing_cost(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import F1Config
    from baskfy_core.fno.costs import crossing_cost

    f1 = cfg(F1Config, inputs["config"])
    return [crossing_cost(D(p), f1) for p in inputs["premiums"]]


def call_condor_fills(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.costs import condor_fills

    return _capture(lambda: condor_fills(_roles(inputs["entry"]), _roles(inputs["exit"]),
                                         int(inputs["quantity"])))  # fmt: skip


def call_condor_round_trip(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import F1Config
    from baskfy_core.fno.costs import condor_round_trip
    from baskfy_core.options.config import CostRates

    return _capture(lambda: condor_round_trip(
        entry=_roles(inputs["entry"]),
        exit_=None if inputs["exit"] is None else _roles(inputs["exit"]),
        quantity=int(inputs["quantity"]), rates=cfg(CostRates, inputs["rates"]),
        config=cfg(F1Config, inputs["config"]),
    ))  # fmt: skip


def call_future_charges(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import FutureCostRates
    from baskfy_core.fno.costs import future_charges

    return future_charges(D(inputs["buys_inr"]), D(inputs["sells_inr"]), int(inputs["orders"]),
                          cfg(FutureCostRates, inputs["rates"]))  # fmt: skip


def call_future_round_trip(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import FutureCostRates
    from baskfy_core.fno.costs import future_round_trip

    return future_round_trip(D(inputs["entry"]), D(inputs["exit"]), int(inputs["quantity"]),
                             cfg(FutureCostRates, inputs["rates"]))  # fmt: skip


def call_future_roll(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import FutureCostRates
    from baskfy_core.fno.costs import future_roll

    return future_roll(D(inputs["sell_old"]), D(inputs["buy_new"]), int(inputs["quantity"]),
                       cfg(FutureCostRates, inputs["rates"]))  # fmt: skip


def _costs_cases() -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    p = PREFIX + "costs."
    f1 = _f1_doc()
    out.append((p + "crossing_cost", "the floor, the boundary (₹10 at 0.5 %) and the percent",
                {"config": f1, "premiums": ["0", "1", "9.99", "10", "10.01", "35.55", "250"]}))  # fmt: skip
    out.append((p + "crossing_cost", "a zero-slippage config",
                {"config": _f1_doc(slippage_pct=D("0"), slippage_min_inr=D("0")),
                 "premiums": ["0", "35.55"]}))  # fmt: skip
    exit_mids = {"LONG_PUT": "12", "LONG_CALL": "9", "SHORT_PUT": "28", "SHORT_CALL": "21"}
    out.append((p + "condor_fills", "eight orders: longs bought, shorts sold, then reversed",
                {"entry": GOOD_MIDS, "exit": exit_mids, "quantity": 75}))  # fmt: skip
    out.append((p + "condor_fills", "a missing exit leg raises",
                {"entry": GOOD_MIDS, "exit": {"LONG_PUT": "1"}, "quantity": 75}))  # fmt: skip
    rt = p + "condor_round_trip"
    rates = _option_rates_doc()
    for entry, exit_, qty, cfg_, why in (
        (GOOD_MIDS, None, 25, f1, "exit priced at the entry mids, one NIFTY lot"),
        (GOOD_MIDS, exit_mids, 75, f1, "an explicit exit, three lots"),
        ({"LONG_PUT": "1", "LONG_CALL": "1", "SHORT_PUT": "3", "SHORT_CALL": "3"}, None, 25, f1, "a thin credit the costs eat: REJECTED_COST"),
        ({"LONG_PUT": "40", "LONG_CALL": "40", "SHORT_PUT": "40", "SHORT_CALL": "40"}, None, 25, f1, "zero credit: REJECTED_COST, share null"),
        ({"LONG_PUT": "6", "LONG_CALL": "6", "SHORT_PUT": "18", "SHORT_CALL": "18"}, None, 15, f1, "near the 25 % boundary"),
        ({"LONG_PUT": "6", "LONG_CALL": "6", "SHORT_PUT": "18", "SHORT_CALL": "18"}, None, 15, _f1_doc(max_cost_share_pct=D("100")), "a 100 % share cap admits it"),
        ({"LONG_PUT": "35.05", "LONG_CALL": "30.10", "SHORT_PUT": "70.25", "SHORT_CALL": "59.95"}, None, 30, f1, "paise everywhere"),
    ):  # fmt: skip
        out.append((rt, why, {"entry": entry, "exit": exit_, "quantity": qty, "rates": rates,
                              "config": cfg_}))  # fmt: skip
    fr = _future_rates_doc()
    for buys, sells, orders, rates_, why in (
        ("1000000", "1050000", 2, fr, "a round trip's turnover"),
        ("0", "0", 0, fr, "no orders"),
        ("2500000.37", "0", 1, fr, "a buy only: stamp, no STT"),
        ("0", "2500000.37", 1, fr, "a sale only: STT, no stamp"),
        ("1000000", "1000000", 2, _future_rates_doc(slippage_pct=D("0")), "slippage off (the journal's rates)"),
    ):  # fmt: skip
        out.append((p + "future_charges", why,
                    {"buys_inr": buys, "sells_inr": sells, "orders": orders, "rates": rates_}))  # fmt: skip
    for entry, exit_, qty, why in (("2450.5", "2610.25", 250, "a winner"),
                                   ("2450.5", "2300", 250, "a loser"),
                                   ("1500", "1500", 500, "flat")):  # fmt: skip
        out.append((p + "future_round_trip", why,
                    {"entry": entry, "exit": exit_, "quantity": qty, "rates": fr}))  # fmt: skip
    for sell_old, buy_new, qty, why in (("2600", "2615.5", 250, "contango roll"),
                                        ("2600", "2590", 250, "backwardation roll")):  # fmt: skip
        out.append((p + "future_roll", why,
                    {"sell_old": sell_old, "buy_new": buy_new, "quantity": qty, "rates": fr}))  # fmt: skip
    return out


# ================================================================================================
# sizing
# ================================================================================================


def call_risk_budget(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.sizing import risk_budget

    return risk_budget(D(inputs["capital_inr"]), D(inputs["risk_pct"]),
                       _ceilings(inputs["ceilings"]))  # fmt: skip


def call_size(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import CommonConfig
    from baskfy_core.fno.sizing import size
    from baskfy_core.options.config import Mode

    return _capture(lambda: size(
        mode=Mode(inputs["mode"]), capital_inr=D(inputs["capital_inr"]),
        risk_pct=D(inputs["risk_pct"]), risk_per_unit=D(inputs["risk_per_unit"]),
        lot_size=inputs["lot_size"], common=cfg(CommonConfig, inputs["common"]),
        ceilings=_ceilings(inputs["ceilings"]),
    ))  # fmt: skip


def call_margin_check(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.sizing import margin_check

    return margin_check(D(inputs["required_inr"]), D(inputs["free_inr"]))


def _sizing_cases() -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    p = PREFIX + "sizing."
    ceil = _ceil_doc()
    for cap, pct, why in (("2500000", "1.0", "₹25 lakh at 1 %: the ₹25,000 ceiling binds exactly"),
                          ("1000000", "1.0", "₹10 lakh at 1 %"),
                          ("1000000", "2.5", "a percent above the 1 % ceiling is clipped"),
                          ("10000000", "0.5", "₹1 crore at 0.5 %: the rupee ceiling binds"),
                          ("0", "1.0", "no capital"),
                          ("123456.78", "0.75", "fractional")):  # fmt: skip
        out.append((p + "risk_budget", why, {"capital_inr": cap, "risk_pct": pct, "ceilings": ceil}))
    sz = p + "size"
    common = _common_doc()
    base = {"mode": "PAPER", "capital_inr": "2500000", "risk_pct": "1.0", "risk_per_unit": "235",
            "lot_size": 25, "common": common, "ceilings": ceil}  # fmt: skip
    for over, why in (
        ({}, "budget 25,000 / 5,875 per lot = 4, capped at fo_max_lots 2"),
        ({"capital_inr": "1000000"}, "budget 10,000 / 5,875 = 1 lot"),
        ({"capital_inr": "1000000", "lot_size": 75}, "budget 10,000 / 17,625 = 0: REJECTED_SIZE"),
        ({"common": _common_doc(max_lots=10)}, "fo_max_lots 10: four lots from the budget"),
        ({"capital_inr": "1175000", "risk_per_unit": "470"}, "budget exactly one lot: 11,750 / 11,750"),
        ({"capital_inr": "1174999.99", "risk_per_unit": "470"}, "a paisa short of one lot: zero, never rounded up"),
        ({"lot_size": None}, "no lot size in the master: REJECTED_SIZE"),
        ({"lot_size": 0}, "a zero lot size: REJECTED_SIZE"),
        ({"capital_inr": "0"}, "paper at ₹0: one lot, lots_at_ceiling recorded"),
        ({"capital_inr": "0", "mode": "LIVE"}, "live at ₹0: NO_SLEEVE_CAPITAL"),
        ({"capital_inr": "-5000"}, "negative capital is ₹0 on paper"),
        ({"capital_inr": "0", "risk_per_unit": "2000"}, "paper at ₹0 where even the ceiling buys no lot"),
        ({"risk_per_unit": "0"}, "zero risk per unit raises: a structure with no risk is a bug"),
        ({"mode": "LIVE", "risk_per_unit": "123.45", "lot_size": 15}, "live with capital, fractional risk"),
    ):  # fmt: skip
        out.append((sz, why, base | over))
    for req, free, why in (("150000", "200000", "inside free margin"), ("200000", "200000", "exactly free: OK"),
                           ("200000.01", "200000", "a paisa over: REJECTED_MARGIN"), ("0", "0", "nothing needed")):  # fmt: skip
        out.append((p + "margin_check", why, {"required_inr": req, "free_inr": free}))
    return out


# ================================================================================================
# exits
# ================================================================================================


def call_f1_exit(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import CommonConfig, F1Config
    from baskfy_core.fno.exits import f1_exit

    return f1_exit(
        now=stamp(inputs["now"]), hard_exit_date=day(inputs["hard_exit_date"]),
        close_cost=Dn(inputs["close_cost"]), entry_credit=D(inputs["entry_credit"]),
        strikes=_strikes(inputs["strikes"]), f1=cfg(F1Config, inputs["f1"]),
        common=cfg(CommonConfig, inputs["common"]),
    )  # fmt: skip


def call_stop_from_vol(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import StopVolConfig
    from baskfy_core.fno.exits import stop_from_vol

    config = cfg(StopVolConfig, inputs["config"])
    return [stop_from_vol(D(c["price"]), float(c["ann_vol"]), config) for c in inputs["cases"]]


def call_f2_initial_stop(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import F2Config
    from baskfy_core.fno.exits import f2_initial_stop

    return f2_initial_stop(D(inputs["entry"]), D(inputs["atr_at_signal"]),
                           cfg(F2Config, inputs["f2"]))  # fmt: skip


def call_f2_trail(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import F2Config
    from baskfy_core.fno.exits import f2_trail

    return f2_trail(D(inputs["stop"]), D(inputs["highest_close"]), D(inputs["atr_at_entry"]),
                    cfg(F2Config, inputs["f2"]))  # fmt: skip


def call_gtt_trigger(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import StopVolConfig
    from baskfy_core.fno.exits import gtt_trigger

    return gtt_trigger(D(inputs["stop"]), D(inputs["entry"]), float(inputs["ann_vol"]),
                       cfg(StopVolConfig, inputs["config"]))  # fmt: skip


def call_f2_time_exit_session(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import F2Config
    from baskfy_core.fno.exits import f2_time_exit_session

    return f2_time_exit_session(days(inputs["sessions"]), day(inputs["entry_session"]),
                                cfg(F2Config, inputs["f2"]))  # fmt: skip


def call_f2_exit(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import CommonConfig
    from baskfy_core.fno.exits import f2_exit

    return f2_exit(now=stamp(inputs["now"]), price=D(inputs["price"]), stop=D(inputs["stop"]),
                   time_exit_date=dayn(inputs["time_exit_date"]),
                   roll_date=dayn(inputs["roll_date"]),
                   common=cfg(CommonConfig, inputs["common"]))  # fmt: skip


def call_f1_paused(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import F1Config
    from baskfy_core.fno.exits import f1_paused

    return f1_paused([D(r) for r in inputs["closed_r"]], cfg(F1Config, inputs["f1"]))


def call_f2_month_paused(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import F2Config
    from baskfy_core.fno.exits import f2_month_paused

    return f2_month_paused([D(r) for r in inputs["month_closed_r"]], cfg(F2Config, inputs["f2"]))


def call_book_paused(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.exits import book_paused

    return book_paused(D(inputs["month_realised_inr"]), D(inputs["monthly_pause_inr"]))


def _exits_cases() -> list[tuple[str, str, dict[str, Any]]]:  # noqa: PLR0915 - a flat table
    out: list[tuple[str, str, dict[str, Any]]] = []
    p = PREFIX + "exits."
    f1, common = _f1_doc(), _common_doc()
    hx = "2026-10-26"
    base = {"hard_exit_date": hx, "entry_credit": "65", "strikes": STRIKES_A, "f1": f1,
            "common": common}  # fmt: skip
    for now, cost, why in (
        ("2026-10-27T09:16:00", "10", "the day after the hard-exit date: LATE_EXIT before any mark"),
        ("2026-10-20T11:00:00", "162.5", "LOSS_CLOSE at (1 + 1.5) x credit"),
        ("2026-10-20T11:00:00", "300", "LOSS_CLOSE at the clamped width"),
        ("2026-10-20T11:00:00", "32.5", "PROFIT_TAKE at 50 % of the credit"),
        ("2026-10-20T11:00:00", "60", "between the two levels: hold"),
        (hx + "T15:00:00", "60", "HARD_EXIT at 15:00 on E - n"),
        (hx + "T14:59:59", "60", "a second before the hard exit: hold"),
        (hx + "T15:30:00", None, "no marks on the hard-exit day after 15:00: still HARD_EXIT"),
        ("2026-10-20T11:00:00", None, "no marks before the hard exit: hold"),
        (hx + "T15:00:00", "20", "the profit take outranks the hard exit on E - n"),
    ):  # fmt: skip
        out.append((p + "f1_exit", why, base | {"now": now, "close_cost": cost}))
    sv = _stopvol_doc()
    out.append((p + "stop_from_vol", "the 8 % floor, the 12 % cap and the vol-scaled middle",
                {"config": sv, "cases": [
                    {"price": "1000", "ann_vol": 0.10}, {"price": "1000", "ann_vol": 0.40},
                    {"price": "1000", "ann_vol": 0.80}, {"price": "2450.55", "ann_vol": 0.2622},
                    {"price": "1000", "ann_vol": 0.0}, {"price": "99.99", "ann_vol": 0.3}]}))  # fmt: skip
    out.append((p + "stop_from_vol", "rounding to one decimal, half-way prices",
                {"config": sv, "cases": [
                    {"price": "123.45", "ann_vol": 0.35}, {"price": "1234.56", "ann_vol": 0.36},
                    {"price": "777.77", "ann_vol": 0.5}, {"price": "50", "ann_vol": 1.0}]}))  # fmt: skip
    f2 = _f2_doc()
    for entry, atr, f2_, why in (("2450.5", "40.25", f2, "3 x ATR below"), ("100", "40", f2, "a stop below zero is not clamped"),
                                 ("2450.5", "40.25", _f2_doc(stop_atr=D("1.5")), "a 1.5 ATR config")):  # fmt: skip
        out.append((p + "f2_initial_stop", why, {"entry": entry, "atr_at_signal": atr, "f2": f2_}))
    for stop, high, atr, f2_, why in (
        ("2329.75", "2600", "40.25", f2, "the trail rises above the stop"),
        ("2500", "2600", "40.25", f2, "never lowered: the stop stays"),
        ("2479.25", "2600", "40.25", f2, "the trail equal to the stop"),
        ("2329.75", "2600", "40.25", _f2_doc(trail=False), "trail off: the stop stays"),
    ):  # fmt: skip
        out.append((p + "f2_trail", why, {"stop": stop, "highest_close": high, "atr_at_entry": atr,
                                          "f2": f2_}))  # fmt: skip
    for stop, entry, vol, why in (("2329.75", "2450.5", 0.25, "the vol stop is tighter"),
                                  ("2300", "2450.5", 0.80, "the ATR stop at 2300 vs a 12 % vol stop"),
                                  ("2400", "2450.5", 0.25, "the ATR stop is tighter")):  # fmt: skip
        out.append((p + "gtt_trigger", why, {"stop": stop, "entry": entry, "ann_vol": vol, "config": sv}))
    sessions = _iso(_weekdays(dt.date(2026, 9, 1), 60))
    for entry_s, f2_, why in ((sessions[0], f2, "the 40th session, entry counted first"),
                              (sessions[25], f2, "the calendar ends before the 40th session: null"),
                              ("2026-09-05", f2, "an entry that is not a session: null"),
                              (sessions[0], _f2_doc(max_sessions=10), "a 10-session hold"),
                              (sessions[50], _f2_doc(max_sessions=10), "the last session exactly")):  # fmt: skip
        out.append((p + "f2_time_exit_session", why,
                    {"sessions": sessions, "entry_session": entry_s, "f2": f2_}))  # fmt: skip
    fx = p + "f2_exit"
    b2 = {"stop": "2400", "time_exit_date": "2026-10-20", "roll_date": "2026-10-26", "common": common}
    for now, price, over, why in (
        ("2026-10-10T11:00:00", "2400", {}, "price at the stop: STOP"),
        ("2026-10-10T11:00:00", "2400.05", {}, "a paisa above the stop: hold"),
        ("2026-10-21T09:20:00", "2500", {}, "after the time-exit date: TIME_EXIT"),
        ("2026-10-20T15:00:00", "2500", {}, "15:00 on the time-exit date: TIME_EXIT"),
        ("2026-10-20T14:59:00", "2500", {}, "before 15:00 on the time-exit date: hold"),
        ("2026-10-26T15:00:00", "2500", {"time_exit_date": None}, "15:00 on the roll date: ROLL"),
        ("2026-10-26T14:00:00", "2500", {"time_exit_date": None}, "before 15:00 on the roll date: hold"),
        ("2026-10-27T10:00:00", "2500", {"time_exit_date": None}, "after the roll date: not a roll here (the monitor's LATE_EXIT)"),
        ("2026-10-20T15:00:00", "2390", {}, "STOP outranks TIME_EXIT"),
        ("2026-10-26T15:00:00", "2500", {"time_exit_date": "2026-10-26"}, "TIME_EXIT outranks ROLL on the same day"),
        ("2026-10-26T15:00:00", "2500", {"time_exit_date": None, "roll_date": None}, "no calendar exits: hold"),
    ):  # fmt: skip
        out.append((fx, why, b2 | over | {"now": now, "price": price}))
    for rs, f1_, why in ((["-0.6", "-0.7", "-1.0"], f1, "three at or below -0.6R: paused"),
                         (["-0.6", "-0.59", "-1.0"], f1, "one just above breaks the run"),
                         (["-1", "-1"], f1, "only two closed"),
                         (["0.5", "-0.6", "-0.6", "-0.6"], f1, "the last three count, oldest first"),
                         (["-0.6", "-0.6", "-0.6", "0.2"], f1, "a win last breaks it"),
                         ([], f1, "no trades"),
                         (["-0.3", "-0.3"], _f1_doc(pause_consecutive=2, pause_loss_r=D("-0.3")), "a 2 x -0.3R config")):  # fmt: skip
        out.append((p + "f1_paused", why, {"closed_r": rs, "f1": f1_}))
    for rs, why in ((["-3", "-3"], "exactly -6R: paused"), (["-3", "-2.99"], "-5.99R: not"),
                    (["-10", "5"], "net -5R"), ([], "no trades: 0 is not <= -6")):  # fmt: skip
        out.append((p + "f2_month_paused", why, {"month_closed_r": rs, "f2": f2}))
    for real, lim, why in (("-75000", "75000", "exactly the limit: paused"), ("-74999.99", "75000", "a paisa short"),
                           ("10000", "75000", "a profit"), ("0", "0", "a zero limit pauses at zero")):  # fmt: skip
        out.append((p + "book_paused", why, {"month_realised_inr": real, "monthly_pause_inr": lim}))
    return out


# ================================================================================================
# calendar
# ================================================================================================


def call_monthly_expiries(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.calendar import monthly_expiries

    return monthly_expiries(days(inputs["expiries"]))


def call_monthly_expiry(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.calendar import monthly_expiry

    return monthly_expiry(days(inputs["expiries"]), int(inputs["year"]), int(inputs["month"]))


def call_sessions_before(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.calendar import sessions_before

    return _capture(lambda: sessions_before(days(inputs["sessions"]), day(inputs["anchor"]),
                                            int(inputs["n"])))  # fmt: skip


def call_sessions_between(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.calendar import sessions_between

    return sessions_between(days(inputs["sessions"]), day(inputs["start"]), day(inputs["end"]))


def call_entry_session(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.calendar import entry_session

    return _capture(lambda: entry_session(days(inputs["sessions"]), day(inputs["expiry"]),
                                          int(inputs["n"])))  # fmt: skip


def call_hard_exit_session(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.calendar import hard_exit_session

    return _capture(lambda: hard_exit_session(days(inputs["sessions"]), day(inputs["expiry"]),
                                              int(inputs["n"])))  # fmt: skip


def call_hard_exit_at(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.calendar import hard_exit_at
    from baskfy_core.fno.config import CommonConfig

    return hard_exit_at(days(inputs["sessions"]), day(inputs["expiry"]),
                        cfg(CommonConfig, inputs["common"]))  # fmt: skip


def call_is_late_exit(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.calendar import is_late_exit

    return is_late_exit(day(inputs["hard_exit_date"]), day(inputs["today"]))


def call_next_entry(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.calendar import next_entry

    return next_entry(day(inputs["today"]), days(inputs["expiries"]), days(inputs["sessions"]),
                      int(inputs["n"]))  # fmt: skip


def call_is_entry_day(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.calendar import is_entry_day

    return is_entry_day(day(inputs["today"]), days(inputs["expiries"]), days(inputs["sessions"]),
                        int(inputs["n"]))  # fmt: skip


def call_plan_expires_at(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.calendar import plan_expires_at
    from baskfy_core.fno.config import CommonConfig, F1Config

    return plan_expires_at(stamp(inputs["issued"]), cfg(F1Config, inputs["f1"]),
                           cfg(CommonConfig, inputs["common"]))  # fmt: skip


def call_in_entry_window(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.calendar import in_entry_window
    from baskfy_core.fno.config import F1Config

    f1 = cfg(F1Config, inputs["f1"])
    return {n: in_entry_window(stamp(n), f1) for n in inputs["nows"]}


def call_held_expiry(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.calendar import held_expiry

    return held_expiry(day(inputs["previous_session"]), days(inputs["expiries"]))


def call_f2_contract_expiry(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.calendar import f2_contract_expiry

    return f2_contract_expiry(day(inputs["day"]), days(inputs["expiries"]),
                              days(inputs["sessions"]), int(inputs["min_sessions"]))  # fmt: skip


def call_roll_session(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.calendar import roll_session

    return _capture(lambda: roll_session(days(inputs["sessions"]), day(inputs["expiry"]),
                                         int(inputs["n"])))  # fmt: skip


#: An exchange calendar with holidays: weekdays from 1 Sep 2026 to 31 Jan 2027 less a few dates.
HOLIDAYS = {dt.date(2026, 10, 2), dt.date(2026, 10, 20), dt.date(2026, 11, 10),
            dt.date(2026, 11, 24), dt.date(2026, 12, 25)}  # fmt: skip


def _calendar() -> list[str]:
    return _iso([d for d in _weekdays(dt.date(2026, 9, 1), 110) if d not in HOLIDAYS])


#: The master's listed expiries: weeklies (Tuesdays) and monthlies, BANKNIFTY's moved to Monday.
EXPIRIES = ["2026-09-08", "2026-09-15", "2026-09-22", "2026-09-29", "2026-10-06", "2026-10-13",
            "2026-10-27", "2026-11-03", "2026-11-17", "2026-11-23", "2026-12-01", "2026-12-29",
            "2027-01-26"]  # fmt: skip


def _calendar_cases() -> list[tuple[str, str, dict[str, Any]]]:  # noqa: PLR0915 - a flat table
    out: list[tuple[str, str, dict[str, Any]]] = []
    p = PREFIX + "calendar."
    sessions = _calendar()
    out.append((p + "monthly_expiries", "the last listed expiry of each month",
                {"expiries": EXPIRIES}))  # fmt: skip
    out.append((p + "monthly_expiries", "unsorted with duplicates", {"expiries": [
        "2026-10-27", "2026-09-29", "2026-10-27", "2026-10-06"]}))  # fmt: skip
    out.append((p + "monthly_expiries", "empty", {"expiries": []}))
    for y, m, why in ((2026, 11, "November's moved monthly (Mon 23 Nov)"), (2026, 9, "September"),
                      (2027, 3, "a month the master does not list")):  # fmt: skip
        out.append((p + "monthly_expiry", why, {"expiries": EXPIRIES, "year": y, "month": m}))
    sb = p + "sessions_before"
    for anchor, n, why in (("2026-10-27", 0, "n 0 is the anchor"),
                           ("2026-10-27", 15, "15 sessions back across the 2 Oct and 20 Oct holidays"),
                           ("2026-10-20", 1, "an anchor that is a holiday: null"),
                           ("2026-09-03", 5, "before the calendar starts: null"),
                           ("2026-10-27", -1, "negative n raises")):  # fmt: skip
        out.append((sb, why, {"sessions": sessions, "anchor": anchor, "n": n}))
    for start, end, why in (("2026-10-01", "2026-10-27", "across two holidays"),
                            ("2026-10-27", "2026-10-01", "backwards is negative"),
                            ("2026-10-02", "2026-10-27", "a holiday start: null")):  # fmt: skip
        out.append((p + "sessions_between", why, {"sessions": sessions, "start": start, "end": end}))
    for exp, n, why in (("2026-10-27", 15, "F1's entry day for October"), ("2026-11-23", 15, "November's moved monthly"),
                        ("2026-09-08", 15, "the calendar does not reach back: null")):  # fmt: skip
        out.append((p + "entry_session", why, {"sessions": sessions, "expiry": exp, "n": n}))
    for exp, n, why in (("2026-10-27", 1, "E - 1"), ("2026-10-21", 1, "E - 1 skips the 20 Oct holiday"),
                        ("2026-10-27", 0, "n 0 raises: never into expiry day"), ("2026-10-27", 5, "E - 5")):  # fmt: skip
        out.append((p + "hard_exit_session", why, {"sessions": sessions, "expiry": exp, "n": n}))
    for exp, common, why in (("2026-10-27", _common_doc(), "15:00 on E - 1"),
                             ("2026-10-27", _common_doc(hard_exit_before_expiry=3, hard_exit_time=dt.time(14, 30)), "14:30 on E - 3"),
                             ("2026-08-25", _common_doc(), "outside the calendar: null")):  # fmt: skip
        out.append((p + "hard_exit_at", why, {"sessions": sessions, "expiry": exp, "common": common}))
    for hx, today, why in (("2026-10-26", "2026-10-26", "on the date: not late"),
                           ("2026-10-26", "2026-10-27", "the day after: late"),
                           ("2026-10-26", "2026-10-01", "before")):  # fmt: skip
        out.append((p + "is_late_exit", why, {"hard_exit_date": hx, "today": today}))
    for today, n, why in (("2026-09-01", 15, "from 1 Sep: September's entry, 8 Sep"),
                          ("2026-10-05", 15, "exactly October's entry day (15 sessions across two holidays)"),
                          ("2026-10-06", 15, "the day after: November's"),
                          ("2027-01-25", 15, "nothing listed after: null"),
                          ("2026-10-06", 10, "n 10")):  # fmt: skip
        out.append((p + "next_entry", why, {"today": today, "expiries": EXPIRIES, "sessions": sessions, "n": n}))
    for today, why in (("2026-10-05", "October's entry day"), ("2026-10-01", "not an entry day: null"),
                       ("2026-10-30", "November's entry day (the moved monthly)")):  # fmt: skip
        out.append((p + "is_entry_day", why, {"today": today, "expiries": EXPIRIES, "sessions": sessions, "n": 15}))
    f1, common = _f1_doc(), _common_doc()
    for issued, why in (("2026-10-06T09:20:00", "issued 09:20: 09:50"),
                        ("2026-10-06T10:15:00", "issued 10:15: the window's 10:30 binds"),
                        ("2026-10-06T10:45:00", "issued after the window: 10:30, already past"),
                        ("2026-10-06T09:20:00+05:30", "an aware datetime keeps its offset")):  # fmt: skip
        out.append((p + "plan_expires_at", why, {"issued": issued, "f1": f1, "common": common}))
    out.append((p + "in_entry_window", "09:20 <= now < 10:30",
                {"f1": f1, "nows": ["2026-10-06T09:19:59", "2026-10-06T09:20:00",
                                    "2026-10-06T10:29:59", "2026-10-06T10:30:00",
                                    "2026-10-06T15:00:00"]}))  # fmt: skip
    for prev, why in (("2026-10-26", "the next expiry after the previous session"),
                      ("2026-10-27", "the previous session was expiry day: the next"),
                      ("2027-01-26", "nothing after: null")):  # fmt: skip
        out.append((p + "held_expiry", why, {"previous_session": prev, "expiries": EXPIRIES}))
    for d_, mn, why in (("2026-10-01", 3, "the near month with room"),
                        ("2026-10-22", 3, "the near month within three sessions: the next"),
                        ("2026-10-21", 3, "exactly four sessions left: still the near month"),
                        ("2026-10-02", 3, "a holiday day: no gap is countable, null"),
                        ("2027-01-25", 3, "nothing ahead with room: null")):  # fmt: skip
        out.append((p + "f2_contract_expiry", why,
                    {"day": d_, "expiries": EXPIRIES, "sessions": sessions, "min_sessions": mn}))  # fmt: skip
    for exp, n, why in (("2026-10-27", 1, "E - 1"), ("2026-10-27", 3, "E - 3"), ("2026-10-27", 0, "n 0 raises")):  # fmt: skip
        out.append((p + "roll_session", why, {"sessions": sessions, "expiry": exp, "n": n}))
    return out


# ================================================================================================
# covered
# ================================================================================================


def _positions(rows: list[dict[str, Any]]) -> list[Any]:
    from baskfy_core.fno.covered import OptionPosition
    from baskfy_core.options.config import OptionType

    return [OptionPosition(r["underlying"], day(r["expiry"]), OptionType(r["option_type"]),
                           D(r["strike"]), int(r["quantity"])) for r in rows]  # fmt: skip


def call_uncovered(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.covered import is_covered, uncovered

    book = _positions(inputs["book"])
    return {"uncovered": uncovered(book), "is_covered": is_covered(book)}


def call_book_after(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.covered import book_after

    return book_after(_positions(inputs["broker_positions"]), _positions(inputs["plan_filled"]),
                      _positions([inputs["step"]])[0])  # fmt: skip


def call_net_of_plan(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.covered import net_of_plan

    return net_of_plan(_positions(inputs["broker_raw"]), _positions(inputs["plan_filled"]))


def call_assess_step(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.covered import assess_step

    return assess_step(_positions([inputs["step"]])[0],
                       broker_positions=_positions(inputs["broker_positions"]),
                       plan_filled=_positions(inputs["plan_filled"]))  # fmt: skip


def _pos(strike: int, kind: str, qty: int, expiry: str = "2026-10-27", und: str = "NIFTY") -> dict[str, Any]:
    return {"underlying": und, "expiry": expiry, "option_type": kind, "strike": str(strike),
            "quantity": qty}  # fmt: skip


def _covered_cases() -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    p = PREFIX + "covered."
    condor = [_pos(21100, "CE", 75), _pos(20800, "CE", -75), _pos(19200, "PE", -75), _pos(18900, "PE", 75)]
    for book, why in (
        (condor, "a full condor is covered"),
        ([_pos(20800, "CE", -75)], "a naked short call"),
        ([_pos(20500, "CE", 75), _pos(20800, "CE", -75)], "a long nearer the money does not cover a call"),
        ([_pos(19500, "PE", 75), _pos(19200, "PE", -75)], "a long nearer the money does not cover a put"),
        ([_pos(21100, "CE", 50), _pos(20800, "CE", -75)], "short more units than the long"),
        ([_pos(21100, "CE", 75), _pos(20800, "CE", -75, "2026-11-23")], "a long of another expiry does not cover"),
        ([_pos(21100, "CE", 75, und="BANKNIFTY"), _pos(20800, "CE", -75)], "a long of another underlying does not cover"),
        ([_pos(21100, "CE", 75), _pos(21100, "CE", -75)], "a long at the same strike nets to nothing: covered"),
        ([_pos(21200, "CE", 75), _pos(21000, "CE", -75), _pos(20800, "CE", -75), _pos(21300, "CE", 75)], "two shorts, two longs further out: covered"),
        ([_pos(21000, "CE", 75), _pos(20800, "CE", -75), _pos(21100, "CE", -75)], "the far short eats the long first"),
        ([], "an empty book"),
        ([_pos(19200, "PE", -75), _pos(20800, "CE", -75)], "two naked shorts, sorted by side"),
    ):  # fmt: skip
        out.append((p + "uncovered", why, {"book": book}))
    out.append((p + "book_after", "broker + plan fills + the step, in that order",
                {"broker_positions": [_pos(22000, "CE", 75)], "plan_filled": [_pos(18900, "PE", 75)],
                 "step": _pos(19200, "PE", -75)}))  # fmt: skip
    for raw, filled, why in (
        ([_pos(18900, "PE", 75), _pos(21100, "CE", 75)], [_pos(18900, "PE", 75)], "the filled leg is netted out"),
        ([_pos(21100, "CE", 75)], [_pos(18900, "PE", 75)], "a fill the broker has not reflected leaves a short residue"),
        ([_pos(18900, "PE", 150)], [_pos(18900, "PE", 75)], "partly ours"),
        ([], [], "nothing"),
    ):  # fmt: skip
        out.append((p + "net_of_plan", why, {"broker_raw": raw, "plan_filled": filled}))
    a = p + "assess_step"
    for broker, filled, step, why in (
        ([], [_pos(18900, "PE", 75), _pos(21100, "CE", 75)], _pos(19200, "PE", -75), "entry step 3 covered by the filled long put"),
        ([], [_pos(18900, "PE", 75)], _pos(20800, "CE", -75), "a short call before its long: refused"),
        ([_pos(21500, "CE", 75)], [], _pos(20800, "CE", -75), "covered by a broker long further out"),
        ([], [], _pos(21100, "CE", 75), "a long is always covered"),
        ([], condor[:3], _pos(18900, "PE", -75), "exit sequence: selling the long put leaves the short put naked"),
    ):  # fmt: skip
        out.append((a, why, {"broker_positions": broker, "plan_filled": filled, "step": step}))
    return out


# ================================================================================================
# journal
# ================================================================================================


def _fills(rows: list[dict[str, Any]]) -> list[Any]:
    from baskfy_core.fno.config import PlanKind
    from baskfy_core.fno.journal import FoFillRecord
    from baskfy_core.options.config import Side

    return [FoFillRecord(r["plan_id"], PlanKind(r["kind"]), Side(r["side"]), D(r["price"]),
                         int(r["quantity"]), stamp(r["filled_at"])) for r in rows]  # fmt: skip


def call_r_multiple(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.journal import r_multiple

    return _capture(lambda: r_multiple(D(inputs["net_pnl_inr"]), D(inputs["r_inr"])))


def call_option_costs(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.journal import option_costs
    from baskfy_core.options.config import CostRates

    return option_costs(_fills(inputs["fills"]), cfg(CostRates, inputs["rates"]))


def call_future_costs(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import FutureCostRates
    from baskfy_core.fno.journal import future_costs

    return future_costs(_fills(inputs["fills"]), cfg(FutureCostRates, inputs["rates"]))


def call_journal_figures(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import FutureCostRates, Structure
    from baskfy_core.fno.journal import journal_figures
    from baskfy_core.options.config import CostRates

    def run() -> Any:
        figs = journal_figures(
            structure=Structure(inputs["structure"]), fills=_fills(inputs["fills"]),
            r_inr=D(inputs["r_inr"]), marks_inr=[D(m) for m in inputs["marks_inr"]],
            option_rates=cfg(CostRates, inputs["option_rates"]),
            future_rates=cfg(FutureCostRates, inputs["future_rates"]),
        )  # fmt: skip
        return {"figures": figs, "cost_detail": figs.cost_detail()}

    return _capture(run)


def call_one_pool(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import FoSleeve
    from baskfy_core.fno.journal import PoolKey, one_pool

    return _capture(lambda: one_pool(PoolKey(FoSleeve(k["sleeve"]), bool(k["simulated"]))
                                     for k in inputs["keys"]))  # fmt: skip


def _fill(plan: str, kind: str, side: str, price: str, qty: int, at: str) -> dict[str, Any]:
    return {"plan_id": plan, "kind": kind, "side": side, "price": price, "quantity": qty,
            "filled_at": at}  # fmt: skip


def _condor_fills_rows(exit_prices: tuple[str, str, str, str] = ("9.85", "1.20", "9.85", "1.20")) -> list[dict[str, Any]]:
    e, x = "2026-10-06T09:25:00", "2026-10-19T11:40:00"
    return [
        _fill("P1", "ENTRY", "BUY", "35", 75, e), _fill("P1", "ENTRY", "BUY", "30", 75, e),
        _fill("P1", "ENTRY", "SELL", "70", 75, e), _fill("P1", "ENTRY", "SELL", "60", 75, e),
        _fill("P2", "EXIT", "BUY", exit_prices[0], 75, x), _fill("P2", "EXIT", "SELL", exit_prices[1], 75, x),
        _fill("P2", "EXIT", "BUY", exit_prices[2], 75, x), _fill("P2", "EXIT", "SELL", exit_prices[3], 75, x),
    ]  # fmt: skip


def _journal_cases() -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    p = PREFIX + "journal."
    for net, r, why in (("1234.5", "17625", "a winner"), ("-17625", "17625", "a full loss: -1.00"),
                        ("881.25", "17625", "0.05 exactly"), ("-440.625", "17625", "half-up at the cent: -0.025 -> -0.03"),
                        ("100", "0", "a zero R raises"), ("100", "-5", "a negative R raises")):  # fmt: skip
        out.append((p + "r_multiple", why, {"net_pnl_inr": net, "r_inr": r}))
    orates, frates = _option_rates_doc(), _future_rates_doc()
    out.append((p + "option_costs", "a condor's four entry orders", {"fills": _condor_fills_rows()[:4], "rates": orates}))
    out.append((p + "option_costs", "no fills: the zero line", {"fills": [], "rates": orates}))
    fut = [_fill("F1", "ENTRY", "BUY", "2450.5", 250, "2026-10-06T09:21:00"),
           _fill("F9", "EXIT", "SELL", "2610.25", 250, "2026-11-02T15:00:00")]  # fmt: skip
    out.append((p + "future_costs", "a buy and a sale, slippage excluded", {"fills": fut, "rates": frates}))
    out.append((p + "future_costs", "no fills", {"fills": [], "rates": frates}))
    jf = p + "journal_figures"
    marks = ["-1200", "2500.5", "4100"]
    base = {"option_rates": orates, "future_rates": frates}
    out.append((jf, "an iron condor closed at a profit", base | {
        "structure": "IRON_CONDOR", "fills": _condor_fills_rows(), "r_inr": "17625", "marks_inr": marks}))  # fmt: skip
    out.append((jf, "an iron condor closed at a loss, no marks", base | {
        "structure": "IRON_CONDOR", "fills": _condor_fills_rows(("150", "40", "120", "30")),
        "r_inr": "17625", "marks_inr": []}))  # fmt: skip
    rolls = [
        _fill("F1", "ENTRY", "BUY", "2450.5", 250, "2026-10-06T09:21:00"),
        _fill("R1", "ROLL", "SELL", "2600", 250, "2026-10-26T15:00:00"),
        _fill("R1", "ROLL", "BUY", "2615.5", 250, "2026-10-26T15:00:05"),
        _fill("R2", "ROLL", "SELL", "2700", 250, "2026-11-23T15:00:00"),
        _fill("R2", "ROLL", "BUY", "2712", 250, "2026-11-23T15:00:03"),
        _fill("F9", "EXIT", "SELL", "2650", 250, "2026-12-03T11:00:00"),
    ]  # fmt: skip
    out.append((jf, "a future with two rolls, each journalled with its own costs", base | {
        "structure": "FUTURE", "fills": rolls, "r_inr": "30187.5", "marks_inr": ["-5000", "40000"]}))  # fmt: skip
    out.append((jf, "a future stopped out, every mark negative (MFE 0)", base | {
        "structure": "FUTURE", "fills": fut[:1] + [_fill("F9", "EXIT", "SELL", "2329.75", 250, "2026-10-09T10:00:00")],
        "r_inr": "30187.5", "marks_inr": ["-3000", "-12000"]}))  # fmt: skip
    out.append((jf, "a zero R raises", base | {
        "structure": "FUTURE", "fills": fut, "r_inr": "0", "marks_inr": []}))  # fmt: skip
    out.append((jf, "an abandoned partial entry: two longs bought and sold at once", base | {
        "structure": "IRON_CONDOR", "r_inr": "17625", "marks_inr": [],
        "fills": [_fill("P1", "ENTRY", "BUY", "35", 75, "2026-10-06T09:25:00"),
                  _fill("P1", "ENTRY", "BUY", "30", 75, "2026-10-06T09:25:01"),
                  _fill("P1X", "EXIT", "SELL", "34.5", 75, "2026-10-06T09:26:00"),
                  _fill("P1X", "EXIT", "SELL", "29.5", 75, "2026-10-06T09:26:00")]}))  # fmt: skip
    for keys, why in (([{"sleeve": "F1N", "simulated": True}] * 3, "one key"), ([], "empty: null"),
                      ([{"sleeve": "F1N", "simulated": True}, {"sleeve": "F1N", "simulated": False}], "paper and live: PooledRows"),
                      ([{"sleeve": "F1N", "simulated": True}, {"sleeve": "F1B", "simulated": True}], "two sleeves: PooledRows")):  # fmt: skip
        out.append((p + "one_pool", why, {"keys": keys}))
    return out


# ================================================================================================
# ledger
# ================================================================================================


def _trades(rows: list[dict[str, Any]]) -> list[Any]:
    from baskfy_core.fno.config import FoSleeve
    from baskfy_core.fno.ledger import ClosedTrade

    return [ClosedTrade(int(r["position_id"]), FoSleeve(r["sleeve"]), r["symbol"], bool(r["simulated"]),
                        day(r["closed_on"]), stampn(r["closed_at"]), D(r["net_pnl_inr"]),
                        D(r["r_multiple"]), r["closed_reason"], int(r.get("rolls", 0)))
            for r in rows]  # fmt: skip


def _marks(rows: list[dict[str, Any]]) -> list[Any]:
    from baskfy_core.fno.config import FoSleeve
    from baskfy_core.fno.ledger import OpenMark

    return [OpenMark(int(r["position_id"]), FoSleeve(r["sleeve"]), r["symbol"], bool(r["simulated"]),
                     dayn(r["trade_date"]), D(r["pnl_inr"]), Dn(r["r_inr"])) for r in rows]  # fmt: skip


def call_month_bounds(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.ledger import month_end, month_start

    return {d: {"start": month_start(day(d)), "end": month_end(day(d))} for d in inputs["days"]}


def call_figures(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.ledger import figures

    return _capture(lambda: figures(_trades(inputs["trades"]), _marks(inputs["marks"]),
                                    day(inputs["as_of"])))  # fmt: skip


def call_build_ledger(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.ledger import build_ledger

    return build_ledger(_trades(inputs["trades"]), _marks(inputs["marks"]), day(inputs["as_of"]))


def call_mode_tag(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.ledger import mode_tag

    return {str(s).lower(): mode_tag(simulated=s) for s in inputs["simulated"]}


def call_book_pause_limit(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.ledger import book_pause_limit

    return book_pause_limit(D(inputs["monthly_pause_inr"]), _ceilings(inputs["ceilings"]))


def _pauses(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import FoSleeve
    from baskfy_core.fno.ledger import evaluate_pauses

    lifts = inputs.get("lifted_after")
    return evaluate_pauses(
        _trades(inputs["trades"]), simulated=bool(inputs["simulated"]), as_of=day(inputs["as_of"]),
        monthly_pause_inr=D(inputs["monthly_pause_inr"]),
        lifted_after=None if lifts is None else {FoSleeve(k): stamp(v) for k, v in lifts.items()},
        config=_fno_config(inputs["config"]), ceilings=_ceilings(inputs["ceilings"]),
    )  # fmt: skip


def call_evaluate_pauses(inputs: dict[str, Any]) -> Any:
    return _pauses(inputs)


def call_pauses_for(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import FoSleeve
    from baskfy_core.fno.ledger import pauses_for

    found = _pauses(inputs)
    return {s: pauses_for(found, FoSleeve(s)) for s in inputs["sleeves"]}


def call_column_pause_applies(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.ledger import column_pause_applies

    return [
        column_pause_applies(dayn(c["paused_until"]), c["paused_reason"],
                             simulated=bool(c["simulated"]), day=day(c["day"]))
        for c in inputs["cases"]
    ]  # fmt: skip


def _ct(pid: int, sleeve: str, sym: str, sim: bool, on: str, at: str | None, pnl: str, r: str,
        reason: str = "PROFIT_TAKE", rolls: int = 0) -> dict[str, Any]:  # fmt: skip
    return {"position_id": pid, "sleeve": sleeve, "symbol": sym, "simulated": sim, "closed_on": on,
            "closed_at": at, "net_pnl_inr": pnl, "r_multiple": r, "closed_reason": reason,
            "rolls": rolls}  # fmt: skip


def _ledger_book() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trades = [
        _ct(1, "F1N", "NIFTY", True, "2026-08-24", "2026-08-24T15:00:00", "8000", "0.45"),
        _ct(2, "F1N", "NIFTY", True, "2026-09-28", "2026-09-28T15:00:00", "-12000", "-0.68", "LOSS_CLOSE"),
        _ct(3, "F1B", "BANKNIFTY", True, "2026-10-05", "2026-10-05T11:00:00", "-3000", "-0.2", "LOSS_CLOSE"),
        _ct(4, "F2", "RELIANCE", True, "2026-10-09", None, "15000", "0.5", "TIME_EXIT", 1),
        _ct(5, "F2", "TCS", True, "2026-10-09", "2026-10-09T10:00:00", "-30000", "-1", "STOP"),
        _ct(6, "F1N", "NIFTY", False, "2026-10-12", "2026-10-12T15:00:00", "5000", "0.28"),
        _ct(7, "F2", "INFY", True, "2026-11-02", "2026-11-02T10:00:00", "1000", "0.03", "STOP"),
    ]  # fmt: skip
    marks = [
        {"position_id": 8, "sleeve": "F1N", "symbol": "NIFTY", "simulated": True, "trade_date": "2026-10-14", "pnl_inr": "2500", "r_inr": "17625"},
        {"position_id": 9, "sleeve": "F2", "symbol": "SBIN", "simulated": True, "trade_date": None, "pnl_inr": "-700.5", "r_inr": None},
        {"position_id": 10, "sleeve": "F1B", "symbol": "BANKNIFTY", "simulated": False, "trade_date": "2026-10-14", "pnl_inr": "-1200", "r_inr": "9000"},
    ]  # fmt: skip
    return trades, marks


def _ledger_cases() -> list[tuple[str, str, dict[str, Any]]]:  # noqa: PLR0915 - a flat table
    out: list[tuple[str, str, dict[str, Any]]] = []
    p = PREFIX + "ledger."
    out.append((p + "month_bounds", "month_start / month_end, leap February and December",
                {"days": ["2026-10-14", "2028-02-10", "2026-12-31", "2027-02-01"]}))  # fmt: skip
    trades, marks = _ledger_book()
    paper_t = [t for t in trades if t["simulated"]]
    paper_m = [m for m in marks if m["simulated"]]
    fg = p + "figures"
    out.append((fg, "one paper pool: realised, MTD, open marks, drawdown",
                {"trades": paper_t, "marks": paper_m, "as_of": "2026-10-14"}))  # fmt: skip
    out.append((fg, "a trade after as_of is not counted", {"trades": paper_t, "marks": [], "as_of": "2026-10-09"}))
    out.append((fg, "empty: win rate null", {"trades": [], "marks": [], "as_of": "2026-10-14"}))
    out.append((fg, "paper and live together: PooledRows", {"trades": trades, "marks": [], "as_of": "2026-10-14"}))
    out.append((fg, "marks only, live", {"trades": [], "marks": [m for m in marks if not m["simulated"]], "as_of": "2026-10-14"}))
    out.append((fg, "a run of losses from the first trade: drawdown from zero", {
        "trades": [_ct(1, "F2", "A", True, "2026-10-01", None, "-100", "-0.1"),
                   _ct(2, "F2", "A", True, "2026-10-02", None, "-200", "-0.2"),
                   _ct(3, "F2", "A", True, "2026-10-03", None, "500", "0.5"),
                   _ct(4, "F2", "A", True, "2026-10-04", None, "-450", "-0.45")],
        "marks": [], "as_of": "2026-10-31"}))  # fmt: skip
    out.append((fg, "same-day closes ordered by closed_at then position_id", {
        "trades": [_ct(3, "F2", "A", True, "2026-10-01", "2026-10-01T15:00:00", "300", "0.3"),
                   _ct(1, "F2", "A", True, "2026-10-01", None, "-500", "-0.5"),
                   _ct(2, "F2", "A", True, "2026-10-01", "2026-10-01T10:00:00", "-100", "-0.1")],
        "marks": [], "as_of": "2026-10-31"}))  # fmt: skip
    bl = p + "build_ledger"
    out.append((bl, "lines per (sleeve, symbol, simulated), a paper and a live book",
                {"trades": trades, "marks": marks, "as_of": "2026-10-14"}))  # fmt: skip
    out.append((bl, "empty", {"trades": [], "marks": [], "as_of": "2026-10-14"}))
    out.append((bl, "a mark on a line with no closed trade", {"trades": trades[:1], "marks": marks[:1], "as_of": "2026-10-14"}))
    out.append((p + "mode_tag", "PAPER and LIVE", {"simulated": [True, False]}))
    for lim, why in (("0", "the seed 0 is the ceiling"), ("50000", "below the ceiling"),
                     ("100000", "above the ceiling is held to it"), ("-10", "negative is not set: the ceiling")):  # fmt: skip
        out.append((p + "book_pause_limit", why, {"monthly_pause_inr": lim, "ceilings": _ceil_doc()}))
    ep = p + "evaluate_pauses"
    conf = _fno_config_doc()
    ceil = _ceil_doc()
    base = {"config": conf, "ceilings": ceil, "monthly_pause_inr": "0", "lifted_after": None}
    run = [
        _ct(11, "F1N", "NIFTY", True, "2026-07-27", "2026-07-27T15:00:00", "-11000", "-0.62", "LOSS_CLOSE"),
        _ct(12, "F1N", "NIFTY", True, "2026-08-24", "2026-08-24T15:00:00", "-12000", "-0.68", "LOSS_CLOSE"),
        _ct(13, "F1N", "NIFTY", True, "2026-09-28", "2026-09-28T15:00:00", "-10600", "-0.6", "LOSS_CLOSE"),
    ]  # fmt: skip
    out.append((ep, "F1N: three closes at or below -0.6R: paused until lifted",
                base | {"trades": run, "simulated": True, "as_of": "2026-10-05"}))  # fmt: skip
    out.append((ep, "the same run in live rows does not pause paper",
                base | {"trades": [dict(t, simulated=False) for t in run], "simulated": True, "as_of": "2026-10-05"}))  # fmt: skip
    out.append((ep, "a lift clears the run: only closes after it count",
                base | {"trades": run, "simulated": True, "as_of": "2026-10-05",
                        "lifted_after": {"F1N": "2026-08-25T09:00:00"}}))  # fmt: skip
    out.append((ep, "an ABANDONED_PARTIAL neither counts nor breaks the run",
                base | {"trades": [run[0], run[1], _ct(14, "F1N", "NIFTY", True, "2026-09-07", "2026-09-07T09:30:00", "-50", "-0.01", "ABANDONED_PARTIAL"), run[2]],
                        "simulated": True, "as_of": "2026-10-05"}))  # fmt: skip
    out.append((ep, "a close with no closed_at after a lift does not count",
                base | {"trades": [dict(t, closed_at=None) for t in run], "simulated": True, "as_of": "2026-10-05",
                        "lifted_after": {"F1N": "2026-07-01T09:00:00"}}))  # fmt: skip
    f2run = [_ct(21, "F2", "TCS", True, "2026-10-05", None, "-30000", "-1", "STOP"),
             _ct(22, "F2", "INFY", True, "2026-10-07", None, "-30000", "-2.5", "STOP"),
             _ct(23, "F2", "SBIN", True, "2026-10-09", None, "-15000", "-2.5", "STOP"),
             _ct(24, "F2", "HDFC", True, "2026-09-30", None, "-30000", "-3", "STOP")]  # fmt: skip
    out.append((ep, "F2: the month's closes total -6R: paused to month end, the book too",
                base | {"trades": f2run, "simulated": True, "as_of": "2026-10-14"}))  # fmt: skip
    out.append((ep, "last month's -3R does not count toward this month",
                base | {"trades": f2run[:2] + f2run[3:], "simulated": True, "as_of": "2026-10-14"}))  # fmt: skip
    out.append((ep, "the book at a set ₹50,000 limit: paused; F2 at -3.5R is not",
                base | {"trades": f2run[:2], "simulated": True, "as_of": "2026-10-14", "monthly_pause_inr": "50000"}))  # fmt: skip
    out.append((ep, "the book a paisa short of ₹75,000", base | {"trades": [
        _ct(31, "F1B", "BANKNIFTY", False, "2026-10-05", None, "-74999.99", "-0.5", "LOSS_CLOSE")],
        "simulated": False, "as_of": "2026-10-14"}))  # fmt: skip
    out.append((ep, "nothing closed: no pause", base | {"trades": [], "simulated": True, "as_of": "2026-10-14"}))
    pf = p + "pauses_for"
    sleeves = ["F1N", "F1B", "F2", "F3N", "F3B"]
    out.append((pf, "which pauses stop which sleeve: F1N's run, F2's month, the book",
                base | {"trades": run + f2run, "simulated": True, "as_of": "2026-10-14", "sleeves": sleeves}))  # fmt: skip
    out.append((pf, "only F1N's run", base | {"trades": run, "simulated": True, "as_of": "2026-10-05", "sleeves": sleeves}))
    cp = p + "column_pause_applies"
    c = lambda until, reason, sim, d: {"paused_until": until, "paused_reason": reason, "simulated": sim, "day": d}  # noqa: E731
    out.append((cp, "mode-tagged reasons bind their mode only; untagged bind both; inclusive date", {"cases": [
        c("2026-10-31", "PAPER:F2_MONTH_R", True, "2026-10-14"),
        c("2026-10-31", "PAPER:F2_MONTH_R", False, "2026-10-14"),
        c("2026-10-31", "LIVE:BOOK_MONTH_INR", False, "2026-10-31"),
        c("2026-10-31", "LIVE:BOOK_MONTH_INR", False, "2026-11-01"),
        c("2026-10-31", "maulik: holiday", True, "2026-10-14"),
        c("2026-10-31", None, False, "2026-10-14"),
        c(None, "PAPER:F1_LOSS_RUN", True, "2026-10-14"),
        c("2026-10-31", "OTHER:X", True, "2026-10-14"),
        c("2026-10-31", ":X", False, "2026-10-14"),
    ]}))  # fmt: skip
    return out


# ================================================================================================
# checklist
# ================================================================================================


def _violations(rows: list[dict[str, Any]]) -> list[Any]:
    from baskfy_core.fno.checklist import Violation, ViolationKind
    from baskfy_core.fno.config import FoSleeve

    return [Violation(ViolationKind(v["kind"]), FoSleeve(v["sleeve"]), day(v["on"]), v["ref"],
                      v["message"]) for v in rows]  # fmt: skip


def call_uncovered_steps(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.checklist import LegFill, uncovered_steps
    from baskfy_core.fno.config import FoSleeve
    from baskfy_core.options.config import Side

    fills = [LegFill(f["role"], Side(f["side"]), int(f["quantity"]), f["ref"], day(f["on"]))
             for f in inputs["fills"]]  # fmt: skip
    return uncovered_steps(FoSleeve(inputs["sleeve"]), fills)


def call_into_expiry(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.checklist import into_expiry
    from baskfy_core.fno.config import FoSleeve

    return into_expiry(sleeve=FoSleeve(inputs["sleeve"]), ref=inputs["ref"],
                       expiry=dayn(inputs["expiry"]), closed_on=dayn(inputs["closed_on"]),
                       as_of=day(inputs["as_of"]))  # fmt: skip


def call_journal_gap(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.checklist import journal_gap
    from baskfy_core.fno.config import FoSleeve

    return journal_gap(sleeve=FoSleeve(inputs["sleeve"]), ref=inputs["ref"],
                       closed_on=day(inputs["closed_on"]), journalled=bool(inputs["journalled"]))  # fmt: skip


def call_mark_gaps(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.checklist import mark_gaps
    from baskfy_core.fno.config import FoSleeve

    return mark_gaps(sleeve=FoSleeve(inputs["sleeve"]), ref=inputs["ref"],
                     held_sessions=days(inputs["held_sessions"]), marked=days(inputs["marked"]))  # fmt: skip


def _cycles(rows: list[dict[str, Any]]) -> list[Any]:
    from baskfy_core.fno.checklist import CycleOutcome, F1Cycle
    from baskfy_core.fno.config import FoSleeve

    return [F1Cycle(FoSleeve(c["sleeve"]), day(c["entry_session"]), CycleOutcome(c["outcome"]),
                    c.get("note", "")) for c in rows]  # fmt: skip


def call_missed_cycles(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.checklist import missed_cycles

    return missed_cycles(_cycles(inputs["cycles"]))


def call_paper_checklist(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.checklist import F2Close, paper_checklist

    result = paper_checklist(
        as_of=day(inputs["as_of"]), f1_cycles=_cycles(inputs["f1_cycles"]),
        f2_start=dayn(inputs["f2_start"]),
        f2_closes=[F2Close(day(c["closed_on"]), int(c["rolls"])) for c in inputs["f2_closes"]],
        sessions=days(inputs["sessions"]), violations=_violations(inputs["violations"]),
    )  # fmt: skip
    from baskfy_core.fno.config import FoSleeveGroup

    return {"checklist": result, "violations_by_kind": result.violations_by_kind(),
            "violations_by_kind_f1": result.violations_by_kind(FoSleeveGroup.F1),
            "violations_by_kind_f2": result.violations_by_kind(FoSleeveGroup.F2)}  # fmt: skip


def _lf(role: str, side: str, qty: int = 75, ref: str = "P1", on: str = "2026-10-06") -> dict[str, Any]:
    return {"role": role, "side": side, "quantity": qty, "ref": ref, "on": on}


def _checklist_cases() -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    p = PREFIX + "checklist."
    us = p + "uncovered_steps"
    entry = [_lf("LONG_PUT", "BUY"), _lf("LONG_CALL", "BUY"), _lf("SHORT_PUT", "SELL"), _lf("SHORT_CALL", "SELL")]
    exit_ = [_lf("SHORT_CALL", "BUY"), _lf("SHORT_PUT", "BUY"), _lf("LONG_CALL", "SELL"), _lf("LONG_PUT", "SELL")]
    for fills, why in ((entry + exit_, "the 04 §2 sequences: never uncovered"),
                       ([_lf("SHORT_CALL", "SELL"), _lf("LONG_CALL", "BUY")], "short first: one violation"),
                       ([_lf("LONG_PUT", "BUY", 50), _lf("SHORT_PUT", "SELL", 75)], "a short larger than its long"),
                       (entry + [_lf("LONG_PUT", "SELL"), _lf("SHORT_PUT", "BUY")], "a wing sold before its short"),
                       ([], "no fills")):  # fmt: skip
        out.append((us, why, {"sleeve": "F1N", "fills": fills}))
    ie = p + "into_expiry"
    for exp, closed, as_of, why in (("2026-10-27", "2026-10-26", "2026-10-30", "closed the day before: fine"),
                                    ("2026-10-27", "2026-10-27", "2026-10-30", "closed on expiry day: violation"),
                                    ("2026-10-27", None, "2026-10-27", "still open on expiry day"),
                                    ("2026-10-27", None, "2026-10-26", "open, before expiry"),
                                    (None, None, "2026-10-30", "no expiry known")):  # fmt: skip
        out.append((ie, why, {"sleeve": "F1N", "ref": "pos-1", "expiry": exp, "closed_on": closed, "as_of": as_of}))
    for j, why in ((True, "journalled"), (False, "no journal row")):
        out.append((p + "journal_gap", why, {"sleeve": "F2", "ref": "pos-2", "closed_on": "2026-10-09", "journalled": j}))
    out.append((p + "mark_gaps", "two unmarked held sessions, sorted",
                {"sleeve": "F2", "ref": "pos-3", "held_sessions": ["2026-10-08", "2026-10-06", "2026-10-07"],
                 "marked": ["2026-10-07"]}))  # fmt: skip
    out.append((p + "mark_gaps", "all marked", {"sleeve": "F2", "ref": "pos-3", "held_sessions": ["2026-10-06"], "marked": ["2026-10-06", "2026-10-05"]}))
    cyc = [{"sleeve": "F1N", "entry_session": "2026-10-06", "outcome": "MISSED"},
           {"sleeve": "F1B", "entry_session": "2026-10-05", "outcome": "SKIPPED", "note": "event"},
           {"sleeve": "F1B", "entry_session": "2026-11-02", "outcome": "MISSED", "note": "no Kite login"},
           {"sleeve": "F1N", "entry_session": "2026-11-03", "outcome": "OPENED"}]  # fmt: skip
    out.append((p + "missed_cycles", "only MISSED cycles, the note kept or defaulted", {"cycles": cyc}))
    pc = p + "paper_checklist"
    sessions = _iso(_weekdays(dt.date(2026, 4, 1), 200))
    months = ["2026-04-14", "2026-05-12", "2026-06-15", "2026-07-13", "2026-08-11", "2026-09-14"]
    clean = [{"sleeve": s, "entry_session": m, "outcome": "OPENED" if i % 2 == 0 else "SKIPPED"}
             for s in ("F1N", "F1B") for i, m in enumerate(months)]  # fmt: skip
    closes = [{"closed_on": d, "rolls": 1 if i < 3 else 0} for i, d in enumerate(sessions[5:65:4])]
    base = {"as_of": "2026-10-01", "f1_cycles": clean, "f2_start": sessions[0], "f2_closes": closes,
            "sessions": sessions, "violations": []}  # fmt: skip
    out.append((pc, "both sleeves meet 04 §9 with no violations", base))
    v = lambda kind, sleeve, on: {"kind": kind, "sleeve": sleeve, "on": on, "ref": "r", "message": "m"}  # noqa: E731
    out.append((pc, "an F1 violation restarts F1's count; F2 untouched",
                base | {"violations": [v("UNCOVERED_SHORT", "F1N", "2026-06-20")]}))  # fmt: skip
    out.append((pc, "an F2 violation restarts F2 from the next session",
                base | {"violations": [v("NAKED_FUTURE", "F2", "2026-05-20"), v("JOURNAL_GAP", "F2", "2026-05-01")]}))  # fmt: skip
    out.append((pc, "a violation after as_of is ignored",
                base | {"violations": [v("LATE_EXIT", "F1B", "2026-12-01")]}))  # fmt: skip
    out.append((pc, "no F2 start yet: nothing counted", base | {"f2_start": None}))
    out.append((pc, "too few opened F1 cycles: not met",
                base | {"f1_cycles": [dict(c, outcome="SKIPPED") for c in clean[:-1]] + [clean[-1]]}))  # fmt: skip
    out.append((pc, "an F2 violation on the last calendar session: start null",
                base | {"sessions": sessions[:100], "violations": [v("NAKED_FUTURE", "F2", sessions[99])], "as_of": sessions[99]}))  # fmt: skip
    return out


# ================================================================================================
# monitor
# ================================================================================================


def call_reprice_condor(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.monitor import reprice_condor
    from baskfy_core.options.config import CostRates, Mode

    return _capture(lambda: reprice_condor(
        strikes=_strikes(inputs["strikes"]), quotes=_quotes(inputs["quotes"]),
        lot_size=int(inputs["lot_size"]), capital_inr=D(inputs["capital_inr"]),
        mode=Mode(inputs["mode"]), config=_fno_config(inputs["config"]),
        rates=cfg(CostRates, inputs["rates"]), ceilings=_ceilings(inputs["ceilings"]),
    ))  # fmt: skip


def call_f1_close_cost(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.monitor import f1_close_cost

    return f1_close_cost(_strikes(inputs["strikes"]), _roles(inputs["marks"]))


def call_f1_pnl_inr(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.monitor import f1_pnl_inr

    return f1_pnl_inr(D(inputs["entry_credit"]), D(inputs["close_cost"]), int(inputs["quantity"]))


def call_reprice_future(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.monitor import reprice_future
    from baskfy_core.options.config import Mode

    return _capture(lambda: reprice_future(
        bid=Dn(inputs["bid"]), ask=Dn(inputs["ask"]), atr=D(inputs["atr"]),
        ann_vol=float(inputs["ann_vol"]), lot_size=int(inputs["lot_size"]),
        capital_inr=D(inputs["capital_inr"]), mode=Mode(inputs["mode"]),
        config=_fno_config(inputs["config"]), ceilings=_ceilings(inputs["ceilings"]),
    ))  # fmt: skip


def call_f2_pnl_inr(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.monitor import f2_pnl_inr

    return f2_pnl_inr(D(inputs["entry"]), D(inputs["mark"]), int(inputs["quantity"]))


def call_f2_trail_step(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.monitor import f2_trail_step

    return f2_trail_step(stop=D(inputs["stop"]), trigger=D(inputs["trigger"]),
                         highest_close=D(inputs["highest_close"]), close=D(inputs["close"]),
                         entry=D(inputs["entry"]), atr_at_entry=D(inputs["atr_at_entry"]),
                         ann_vol=float(inputs["ann_vol"]), config=_fno_config(inputs["config"]))  # fmt: skip


def call_f2_action(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import CommonConfig
    from baskfy_core.fno.monitor import f2_action

    return f2_action(now=stamp(inputs["now"]), price=Dn(inputs["price"]), trigger=D(inputs["trigger"]),
                     time_exit_date=dayn(inputs["time_exit_date"]), roll_date=dayn(inputs["roll_date"]),
                     naked=bool(inputs["naked"]), common=cfg(CommonConfig, inputs["common"]))  # fmt: skip


def _monitor_cases() -> list[tuple[str, str, dict[str, Any]]]:  # noqa: PLR0915 - a flat table
    out: list[tuple[str, str, dict[str, Any]]] = []
    p = PREFIX + "monitor."
    conf, ceil, rates = _fno_config_doc(), _ceil_doc(), _option_rates_doc()
    q = lambda bid, ask, oi=1000: {"bid": bid, "ask": ask, "oi_lots": oi}  # noqa: E731
    good_q = {"LONG_PUT": q("34", "36"), "LONG_CALL": q("29", "31"),
              "SHORT_PUT": q("69", "71"), "SHORT_CALL": q("59", "61")}  # fmt: skip
    base = {"strikes": STRIKES_A, "quotes": good_q, "lot_size": 25, "capital_inr": "2500000",
            "mode": "PAPER", "config": conf, "rates": rates, "ceilings": ceil}  # fmt: skip
    rc = p + "reprice_condor"
    for over, why in (
        ({}, "clean: two lots at the cap"),
        ({"capital_inr": "1000000", "lot_size": 75}, "a lot beyond the budget: REJECTED_SIZE"),
        ({"quotes": good_q | {"SHORT_PUT": q("69", "71", 10)}}, "a thin short: REJECTED_LIQUIDITY, still priced"),
        ({"quotes": {k: v for k, v in good_q.items() if k != "LONG_PUT"}}, "a leg without a quote: REJECTED_LIQUIDITY, unpriced"),
        ({"quotes": {"LONG_PUT": q("49", "51"), "LONG_CALL": q("49", "51"), "SHORT_PUT": q("49", "51"), "SHORT_CALL": q("49", "51")}}, "zero credit: REJECTED_STRUCTURE"),
        ({"quotes": {"LONG_PUT": q("0.95", "1.05"), "LONG_CALL": q("0.95", "1.05"), "SHORT_PUT": q("2.9", "3.1"), "SHORT_CALL": q("2.9", "3.1")}}, "costs eat the credit: REJECTED_COST"),
        ({"capital_inr": "0", "mode": "LIVE"}, "live at ₹0: NO_SLEEVE_CAPITAL"),
        ({"capital_inr": "0"}, "paper at ₹0: one lot"),
        ({"quotes": good_q | {"SHORT_PUT": q("60", "80", 10)}, "capital_inr": "1000000", "lot_size": 75}, "liquidity and size both: liquidity first"),
    ):  # fmt: skip
        out.append((rc, why, base | over))
    for marks, why in (({"LONG_PUT": "10", "LONG_CALL": "8", "SHORT_PUT": "30", "SHORT_CALL": "25"}, "every leg marked"),
                       ({"LONG_PUT": "10", "SHORT_PUT": "30", "SHORT_CALL": "25"}, "a missing leg is null, not zero")):  # fmt: skip
        out.append((p + "f1_close_cost", why, {"strikes": STRIKES_A, "marks": marks}))
    for cred, cost, qty, why in (("65", "32.5", 75, "half decayed"), ("65", "162.5", 75, "a loss"),
                                 ("65.05", "32.333", 25, "rounded half up to the paisa")):  # fmt: skip
        out.append((p + "f1_pnl_inr", why, {"entry_credit": cred, "close_cost": cost, "quantity": qty}))
    rf = p + "reprice_future"
    fb = {"bid": "2450", "ask": "2450.5", "atr": "40.25", "ann_vol": 0.25, "lot_size": 250,
          "capital_inr": "2500000", "mode": "PAPER", "config": conf, "ceilings": ceil}  # fmt: skip
    for over, why in (
        ({}, "the stop 3 ATR below the ask, the vol GTT tighter, two lots"),
        ({"bid": None}, "no bid: REJECTED_LIQUIDITY"),
        ({"bid": "2451", "ask": "2450.5"}, "a crossed quote: REJECTED_LIQUIDITY"),
        ({"ask": "0"}, "a zero ask: REJECTED_LIQUIDITY"),
        ({"atr": "0"}, "zero ATR: REJECTED_STRUCTURE"),
        ({"bid": "100", "ask": "100.5", "atr": "40"}, "a stop below zero: REJECTED_STRUCTURE"),
        ({"capital_inr": "1000000", "lot_size": 1250}, "a lot beyond the budget: REJECTED_SIZE"),
        ({"capital_inr": "0", "mode": "LIVE"}, "live at ₹0: NO_SLEEVE_CAPITAL"),
        ({"capital_inr": "0"}, "paper at ₹0: one lot"),
        ({"ann_vol": 0.9}, "a high vol: the ATR stop is the tighter"),
    ):  # fmt: skip
        out.append((rf, why, fb | over))
    for e, m, qty, why in (("2450.5", "2610.25", 250, "a gain"), ("2450.5", "2329.755", 250, "a loss, rounded")):  # fmt: skip
        out.append((p + "f2_pnl_inr", why, {"entry": e, "mark": m, "quantity": qty}))
    ts = p + "f2_trail_step"
    tb = {"stop": "2329.75", "trigger": "2345.6", "highest_close": "2450.5", "entry": "2450.5",
          "atr_at_entry": "40.25", "ann_vol": 0.25, "config": conf}  # fmt: skip
    for over, why in (({"close": "2600"}, "a new high: stop and trigger rise, moved"),
                      ({"close": "2400"}, "a lower close: nothing moves"),
                      ({"close": "2480", "highest_close": "2600", "stop": "2479.25", "trigger": "2479.25"}, "the trail equal to the stop: not moved"),
                      ({"close": "2700", "trigger": "2700"}, "a trigger above the candidate is never lowered"),
                      ({"close": "2500", "ann_vol": 0.05}, "the vol floor (8 %) sets the trigger")):  # fmt: skip
        out.append((ts, why, tb | over))
    fa = p + "f2_action"
    ab = {"price": "2500", "trigger": "2400", "time_exit_date": "2026-10-20", "roll_date": "2026-10-26",
          "naked": False, "common": _common_doc()}  # fmt: skip
    for now, over, why in (
        ("2026-10-10T11:00:00", {"naked": True, "price": "2300"}, "no resting GTT: NAKED_FUTURE first"),
        ("2026-10-27T09:16:00", {"time_exit_date": None}, "the roll date passed while down: LATE_EXIT"),
        ("2026-10-10T11:00:00", {"price": "2400"}, "at the trigger: STOP"),
        ("2026-10-10T11:00:00", {"price": None}, "no price: the stop cannot be tested, hold"),
        ("2026-10-20T15:00:00", {"price": None}, "no price at the time exit: TIME_EXIT"),
        ("2026-10-26T15:00:00", {"price": None, "time_exit_date": None}, "no price at the roll: ROLL"),
        ("2026-10-10T11:00:00", {}, "nothing due: hold"),
        ("2026-10-27T09:16:00", {"naked": True, "time_exit_date": None}, "naked outranks the late roll"),
    ):  # fmt: skip
        out.append((fa, why, ab | over | {"now": now}))
    return out


# ================================================================================================
# directional (F3)
# ================================================================================================


def _daily(rows: list[dict[str, Any]]) -> list[Any]:
    from baskfy_core.fno.directional import DailyBar

    return [DailyBar(day(b["date"]), D(b["open"]), D(b["high"]), D(b["low"]), D(b["close"]))
            for b in rows]  # fmt: skip


def _minutes(rows: list[dict[str, Any]]) -> list[Any]:
    from baskfy_core.fno.directional import MinuteBar

    return [MinuteBar(stamp(b["ts"]), D(b["open"]), D(b["high"]), D(b["low"]), D(b["close"]))
            for b in rows]  # fmt: skip


def _bar75s(rows: list[dict[str, Any]]) -> list[Any]:
    from baskfy_core.fno.directional import Bar75

    return [Bar75(stamp(b["start"]), D(b["open"]), D(b["high"]), D(b["low"]), D(b["close"]))
            for b in rows]  # fmt: skip


def _levels_obj(raw: Mapping[str, Any]) -> Any:
    from baskfy_core.fno.directional import Levels, Pivot

    return Levels(
        as_of=day(raw["as_of"]), close=D(raw["close"]), support=Dn(raw["support"]),
        resistance=Dn(raw["resistance"]), weekly_high=D(raw["weekly_high"]),
        weekly_low=D(raw["weekly_low"]), trend_avg=D(raw["trend_avg"]),
        pivot_highs=tuple(Pivot(day(p["date"]), D(p["price"])) for p in raw["pivot_highs"]),
        pivot_lows=tuple(Pivot(day(p["date"]), D(p["price"])) for p in raw["pivot_lows"]),
    )  # fmt: skip


def _spread(raw: Mapping[str, Any]) -> Any:
    from baskfy_core.fno.directional import SpreadStrikes
    from baskfy_core.options.config import OptionType

    return SpreadStrikes(OptionType(raw["option_type"]), D(raw["short"]), D(raw["wing"]))


def _open_spread(raw: Mapping[str, Any]) -> Any:
    from baskfy_core.fno.directional import Direction, OpenSpread

    return OpenSpread(
        direction=Direction(raw["direction"]), level=D(raw["level"]), strikes=_spread(raw["strikes"]),
        expiry=day(raw["expiry"]), entry_session=day(raw["entry_session"]),
        entry_credit=D(raw["entry_credit"]), lots=int(raw["lots"]), lot_size=int(raw["lot_size"]),
        max_loss_per_lot_inr=D(raw["max_loss_per_lot_inr"]),
    )  # fmt: skip


def call_seventy_five(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.directional import seventy_five_minute_bars

    return seventy_five_minute_bars(_minutes(inputs["minutes"]))


def call_pivots(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.directional import pivots

    return pivots(_daily(inputs["bars"]), int(inputs["width"]))


def call_bars_needed(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import F3Config
    from baskfy_core.fno.directional import bars_needed

    return bars_needed(cfg(F3Config, inputs["f3"]))


def call_levels(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import F3Config
    from baskfy_core.fno.directional import daily_direction, levels

    lv = levels(_daily(inputs["bars"]), cfg(F3Config, inputs["f3"]))
    return {"levels": lv, "daily_direction": None if lv is None else daily_direction(lv)}


def call_daily_direction(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.directional import daily_direction

    return daily_direction(_levels_obj(inputs["levels"]))


def call_confirm_75(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import F3Config
    from baskfy_core.fno.directional import Direction, confirm_75

    return confirm_75(_bar75s(inputs["bars"]), Direction(inputs["daily"]), cfg(F3Config, inputs["f3"]))


def call_direction_for(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import F3Config
    from baskfy_core.fno.directional import direction_for

    return direction_for(_levels_obj(inputs["levels"]), _bar75s(inputs["bars"]),
                         cfg(F3Config, inputs["f3"]))  # fmt: skip


def call_key_level(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.directional import Direction, key_level

    lv = _levels_obj(inputs["levels"])
    return {d: key_level(Direction(d), lv) for d in ("UP", "DOWN", "NONE")}


def call_intraday_aligned(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.directional import Direction, intraday_aligned

    return [intraday_aligned(Direction(c["direction"]), D(c["last_price"]), D(c["session_open"]))
            for c in inputs["cases"]]  # fmt: skip


def call_level_broken(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import F3Config
    from baskfy_core.fno.directional import Direction, level_broken

    f3 = cfg(F3Config, inputs["f3"])
    return [level_broken(Direction(c["direction"]), D(c["level"]), D(c["last_price"]), f3)
            for c in inputs["cases"]]  # fmt: skip


def call_spread_strikes(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import F3Config
    from baskfy_core.fno.directional import Direction, spread_strikes

    return _capture(lambda: spread_strikes(Direction(inputs["direction"]), _levels_obj(inputs["levels"]),
                                           D(inputs["step"]), cfg(F3Config, inputs["f3"])))  # fmt: skip


def call_choose_expiry(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.directional import choose_expiry

    return choose_expiry(days(inputs["expiries"]), days(inputs["sessions"]), day(inputs["entry"]),
                         int(inputs["min_sessions"]))  # fmt: skip


def call_spread_arith(inputs: dict[str, Any]) -> Any:
    """``credit_per_unit``, ``max_loss_per_unit``, ``pnl_inr`` at one set of prices."""
    from baskfy_core.fno import directional as d

    credit = d.credit_per_unit(D(inputs["short_price"]), D(inputs["wing_price"]))
    return {
        "credit_per_unit": credit,
        "max_loss_per_unit": d.max_loss_per_unit(_spread(inputs["strikes"]), credit),
        "pnl_inr": d.pnl_inr(credit, D(inputs["mark"]), int(inputs["quantity"])),
    }


def call_size_entry(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import CommonConfig, F3Config
    from baskfy_core.fno.directional import size_entry
    from baskfy_core.options.config import Mode

    return _capture(lambda: size_entry(
        mode=Mode(inputs["mode"]), capital_inr=D(inputs["capital_inr"]),
        max_loss_per_unit=D(inputs["max_loss_per_unit"]), lot_size=inputs["lot_size"],
        f3=cfg(F3Config, inputs["f3"]), common=cfg(CommonConfig, inputs["common"]),
        ceilings=_ceilings(inputs["ceilings"]),
    ))  # fmt: skip


def _prices(rows: list[dict[str, Any]]) -> dict[tuple[Decimal, Any], Decimal]:
    from baskfy_core.options.config import OptionType

    return {(D(r["strike"]), OptionType(r["option_type"])): D(r["price"]) for r in rows}


def call_propose_spread(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import CommonConfig, F3Config
    from baskfy_core.fno.directional import Direction, propose_spread
    from baskfy_core.options.config import Mode

    return _capture(lambda: propose_spread(
        mode=Mode(inputs["mode"]), direction=Direction(inputs["direction"]),
        lv=_levels_obj(inputs["levels"]), step=D(inputs["step"]), prices=_prices(inputs["prices"]),
        lot_size=inputs["lot_size"], capital_inr=D(inputs["capital_inr"]),
        f3=cfg(F3Config, inputs["f3"]), common=cfg(CommonConfig, inputs["common"]),
        ceilings=_ceilings(inputs["ceilings"]),
    ))  # fmt: skip


def call_add_decision(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import CommonConfig, F3Config
    from baskfy_core.fno.directional import Direction, add_decision

    return add_decision(
        position=_open_spread(inputs["position"]), today=day(inputs["today"]),
        direction_now=Direction(inputs["direction_now"]), level_intact=bool(inputs["level_intact"]),
        mark=Dn(inputs["mark"]), capital_inr=D(inputs["capital_inr"]),
        f3=cfg(F3Config, inputs["f3"]), common=cfg(CommonConfig, inputs["common"]),
    )  # fmt: skip


def call_f3_marks(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import F3Config
    from baskfy_core.fno.directional import decay_target_mark, loss_cut_mark

    f3 = cfg(F3Config, inputs["f3"])
    return {c: {"decay_target_mark": decay_target_mark(D(c), f3), "loss_cut_mark": loss_cut_mark(D(c), f3)}
            for c in inputs["credits"]}  # fmt: skip


def call_f3_exit(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import CommonConfig, F3Config
    from baskfy_core.fno.directional import f3_exit

    return f3_exit(now=stamp(inputs["now"]), position=_open_spread(inputs["position"]),
                   last_price=Dn(inputs["last_price"]), mark=Dn(inputs["mark"]),
                   f3=cfg(F3Config, inputs["f3"]), common=cfg(CommonConfig, inputs["common"]))  # fmt: skip


def _walk_bars(seed: int, n: int, *, start: float = 24000.0, drift: float = 0.0, vol: float = 0.008,
               first: dt.date = dt.date(2026, 6, 1)) -> list[dict[str, Any]]:  # fmt: skip
    """Seeded daily index bars, rounded to the paisa."""
    import numpy as np

    rng = np.random.default_rng(seed)
    out: list[dict[str, Any]] = []
    close = start
    for d_ in _weekdays(first, n):
        r = float(rng.normal(drift, vol))
        open_ = close * (1 + float(rng.normal(0, vol / 3)))
        close = close * math.exp(r)
        hi = max(open_, close) * (1 + abs(float(rng.normal(0, vol / 2))))
        lo = min(open_, close) * (1 - abs(float(rng.normal(0, vol / 2))))
        out.append({"date": d_.isoformat(), "open": f"{open_:.2f}", "high": f"{hi:.2f}",
                    "low": f"{lo:.2f}", "close": f"{close:.2f}"})  # fmt: skip
    return out


def _zigzag_bars(n: int, *, base: int = 24000, step: int = 20, swing: int = 300, period: int = 10,
                 first: dt.date = dt.date(2026, 6, 1)) -> list[dict[str, Any]]:  # fmt: skip
    """A deterministic trend with a regular pullback, so pivots exist on both sides."""
    out: list[dict[str, Any]] = []
    for i, d_ in enumerate(_weekdays(first, n)):
        phase = i % period
        wave = swing * (phase if phase <= period // 2 else period - phase) / (period // 2)
        c = base + step * i + wave
        out.append({"date": d_.isoformat(), "open": f"{c - 10:.2f}", "high": f"{c + 25:.2f}",
                    "low": f"{c - 25:.2f}", "close": f"{c:.2f}"})  # fmt: skip
    return out


def _b75(start: str, o: str, h: str, low: str, c: str) -> dict[str, Any]:
    return {"start": start, "open": o, "high": h, "low": low, "close": c}


def _b75_series(closes: list[float], first: dt.date = dt.date(2026, 10, 12)) -> list[dict[str, Any]]:
    opens = [dt.time(9, 15), dt.time(10, 30), dt.time(11, 45), dt.time(13, 0), dt.time(14, 15)]
    out: list[dict[str, Any]] = []
    sessions = _weekdays(first, len(closes) // 5 + 2)
    for i, c in enumerate(closes):
        start = dt.datetime.combine(sessions[i // 5], opens[i % 5]).isoformat()
        out.append(_b75(start, f"{c - 5:.2f}", f"{c + 10:.2f}", f"{c - 10:.2f}", f"{c:.2f}"))
    return out


LEVELS_UP = {"as_of": "2026-10-16", "close": "24800", "support": "24400", "resistance": "25100",
             "weekly_high": "24850", "weekly_low": "24560.4", "trend_avg": "24650",
             "pivot_highs": [{"date": "2026-09-20", "price": "25100"}],
             "pivot_lows": [{"date": "2026-10-01", "price": "24400"}]}  # fmt: skip
LEVELS_DOWN = LEVELS_UP | {"close": "24300", "trend_avg": "24600", "support": None,
                           "weekly_high": "24510.7", "weekly_low": "24250"}  # fmt: skip
LEVELS_NONE = LEVELS_UP | {"close": "24650", "trend_avg": "24650"}


def _directional_cases() -> list[tuple[str, str, dict[str, Any]]]:  # noqa: PLR0915 - a flat table
    out: list[tuple[str, str, dict[str, Any]]] = []
    p = PREFIX + "directional."
    f3, common, ceil = _f3_doc(), _common_doc(), _ceil_doc()

    def mb(ts: str, c: float) -> dict[str, Any]:
        return {"ts": ts, "open": f"{c:.2f}", "high": f"{c + 3:.2f}", "low": f"{c - 3:.2f}", "close": f"{c:.2f}"}

    s75 = p + "seventy_five_minute_bars"
    one_day = [mb("2026-10-12T09:15:00", 24800), mb("2026-10-12T09:50:00", 24830),
               mb("2026-10-12T10:29:00", 24815), mb("2026-10-12T10:30:00", 24816),
               mb("2026-10-12T11:44:00", 24790), mb("2026-10-12T11:45:00", 24795),
               mb("2026-10-12T12:30:00", 24805)]  # fmt: skip
    out.append((s75, "two complete bars; the third still forming is left out", {"minutes": one_day}))
    out.append((s75, "minutes out of order and outside 09:15-15:30 are sorted and ignored",
                {"minutes": [mb("2026-10-12T10:29:00", 24815), mb("2026-10-12T09:14:00", 1),
                             mb("2026-10-12T15:30:00", 2), mb("2026-10-12T09:15:00", 24800),
                             mb("2026-10-12T14:15:00", 24700), mb("2026-10-12T15:29:00", 24750)]}))  # fmt: skip
    out.append((s75, "a bar whose closing minute never arrived is incomplete",
                {"minutes": [mb("2026-10-12T09:15:00", 24800), mb("2026-10-12T10:28:00", 24810)]}))  # fmt: skip
    out.append((s75, "two sessions, the first slot of each",
                {"minutes": [mb("2026-10-12T09:15:00", 24800), mb("2026-10-12T10:29:00", 24815),
                             mb("2026-10-13T09:15:00", 24900), mb("2026-10-13T10:29:00", 24880)]}))  # fmt: skip
    out.append((s75, "no minutes", {"minutes": []}))
    zig = _zigzag_bars(70)
    walk = _walk_bars(7, 70, drift=0.001)
    for bars, w, why in ((zig[:30], 3, "a zigzag: regular pivots, width 3"),
                         (zig[:30], 1, "width 1"),
                         ([{"date": "2026-10-01", "open": "10", "high": "12", "low": "8", "close": "11"},
                           {"date": "2026-10-02", "open": "10", "high": "12", "low": "8", "close": "11"},
                           {"date": "2026-10-05", "open": "10", "high": "12", "low": "8", "close": "11"}], 1, "a tie is not a pivot (strict)"),
                         (walk[:40], 3, "a seeded walk"),
                         (zig[:5], 3, "too short for any pivot")):  # fmt: skip
        out.append((p + "pivots", why, {"bars": bars, "width": w}))
    for over, why in (({}, "defaults: 60"), ({"pivot_lookback": 20, "trend_sessions": 50}, "trend 50 > lookback 20")):
        out.append((p + "bars_needed", why, {"f3": _f3_doc(**over)}))
    lv = p + "levels"
    for bars, f3_, why in ((zig, f3, "a rising zigzag: support below, resistance above"),
                           (walk, f3, "seeded walk, 70 bars"),
                           (_walk_bars(11, 65, drift=-0.002), f3, "a falling walk"),
                           (zig[:59], f3, "59 bars < 60 needed: null"),
                           (zig[:60], f3, "exactly 60 bars"),
                           (_walk_bars(3, 40), _f3_doc(pivot_lookback=20, trend_sessions=10, weekly_sessions=3), "a short config")):  # fmt: skip
        out.append((lv, why, {"bars": bars, "f3": f3_}))
    dd = p + "daily_direction"
    for levels_, why in ((LEVELS_UP, "above trend and support: UP"), (LEVELS_DOWN, "below trend and resistance: DOWN"),
                         (LEVELS_NONE, "at the trend average: NONE"),
                         (LEVELS_UP | {"support": None}, "no support: not UP"),
                         (LEVELS_UP | {"close": "24350", "trend_avg": "24300"}, "above trend but below support: NONE"),
                         (LEVELS_DOWN | {"resistance": None}, "no resistance: not DOWN")):  # fmt: skip
        out.append((dd, why, {"levels": levels_}))
    c75 = p + "confirm_75"
    rising = _b75_series([24700 + 10 * i for i in range(12)])
    falling = _b75_series([24900 - 10 * i for i in range(12)])
    for bars, daily_, why in ((rising, "UP", "the last close above its 10-bar average: agrees"),
                              (rising, "DOWN", "DOWN against rising bars: disagrees"),
                              (falling, "DOWN", "falling bars confirm DOWN"),
                              (rising[:9], "UP", "9 bars < 10: never agrees"),
                              (rising, "NONE", "a NONE daily read never agrees"),
                              (_b75_series([24700.0] * 10), "UP", "flat: the close equals the average, no")):  # fmt: skip
        out.append((c75, why, {"bars": bars, "daily": daily_, "f3": f3}))
    for levels_, bars, why in ((LEVELS_UP, rising, "UP confirmed"), (LEVELS_UP, falling, "UP refused: NONE"),
                               (LEVELS_NONE, rising, "no daily direction")):  # fmt: skip
        out.append((p + "direction_for", why, {"levels": levels_, "bars": bars, "f3": f3}))
    out.append((p + "key_level", "support for UP, resistance for DOWN, none for NONE", {"levels": LEVELS_UP}))
    out.append((p + "intraday_aligned", "above the open for UP, below for DOWN, at the open neither", {"cases": [
        {"direction": "UP", "last_price": "24810", "session_open": "24800"},
        {"direction": "UP", "last_price": "24800", "session_open": "24800"},
        {"direction": "DOWN", "last_price": "24790", "session_open": "24800"},
        {"direction": "DOWN", "last_price": "24810", "session_open": "24800"},
        {"direction": "NONE", "last_price": "24810", "session_open": "24800"}]}))  # fmt: skip
    out.append((p + "level_broken", "a 0.10 % buffer each side, from a print", {"f3": f3, "cases": [
        {"direction": "UP", "level": "24400", "last_price": "24375.6"},
        {"direction": "UP", "level": "24400", "last_price": "24375.59"},
        {"direction": "DOWN", "level": "25100", "last_price": "25125.1"},
        {"direction": "DOWN", "level": "25100", "last_price": "25125.11"},
        {"direction": "NONE", "level": "25100", "last_price": "1"}]}))  # fmt: skip
    out.append((p + "level_broken", "a zero buffer", {"f3": _f3_doc(level_buffer_pct=D("0")), "cases": [
        {"direction": "UP", "level": "24400", "last_price": "24399.99"},
        {"direction": "UP", "level": "24400", "last_price": "24400"}]}))  # fmt: skip
    ss = p + "spread_strikes"
    for direction, levels_, step, f3_, why in (
        ("UP", LEVELS_UP, "50", f3, "UP: a put 1 % below the weekly low, rounded down; the wing 2 % of the close further"),
        ("DOWN", LEVELS_DOWN, "50", f3, "DOWN: a call 1 % above the weekly high, rounded up"),
        ("UP", LEVELS_UP | {"weekly_low": "24620"}, "50", f3, "a 50-point step: 24373.8 floors to 24350"),
        ("UP", LEVELS_UP | {"weekly_low": "24620"}, "100", f3, "a 100-point step: 24373.8 floors to 24300"),
        ("UP", LEVELS_UP | {"close": "1000", "weekly_low": "990"}, "50", _f3_doc(wing_pct=D("0.5")), "a wing gap under one step: one step out"),
        ("NONE", LEVELS_UP, "50", f3, "no direction raises"),
        ("UP", LEVELS_UP, "0", f3, "a zero step raises"),
    ):  # fmt: skip
        out.append((ss, why, {"direction": direction, "levels": levels_, "step": step, "f3": f3_}))
    weekly = ["2026-10-13", "2026-10-20", "2026-10-27", "2026-11-03"]
    sess = _iso(_weekdays(dt.date(2026, 10, 1), 30))
    for entry, mn, exps, why in (("2026-10-16", 2, weekly, "the nearest weekly with two sessions"),
                                 ("2026-10-19", 2, weekly, "one session to the 20th: the 27th"),
                                 ("2026-10-13", 2, weekly, "an expiry on the entry day has zero sessions"),
                                 ("2026-10-16", 5, ["2026-10-27", "2026-11-24"], "monthly, five sessions"),
                                 ("2026-11-04", 2, weekly, "nothing listed after: null"),
                                 ("2026-10-16", 2, ["2026-12-29"], "beyond the calendar: null")):  # fmt: skip
        out.append((p + "choose_expiry", why, {"expiries": exps, "sessions": sess, "entry": entry, "min_sessions": mn}))
    pe = {"option_type": "PE", "short": "24300", "wing": "23800"}
    for sp, wp, mark, why in (("40", "12", "8", "a clean credit and a gain"), ("12", "12", "0", "zero credit"),
                              ("40.05", "12.10", "70", "a loss at 2.5 x the credit")):  # fmt: skip
        out.append((p + "spread_arith", why, {"strikes": pe, "short_price": sp, "wing_price": wp, "mark": mark, "quantity": 65}))
    se = p + "size_entry"
    sb = {"mode": "PAPER", "capital_inr": "2500000", "max_loss_per_unit": "472", "lot_size": 65,
          "f3": f3, "common": common, "ceilings": ceil}  # fmt: skip
    for over, why in (({}, "₹25,000 budget / ₹30,680 per lot: REJECTED_SIZE"),
                      ({"max_loss_per_unit": "150"}, "₹9,750 per lot: two lots"),
                      ({"capital_inr": "0"}, "paper at ₹0: one lot"),
                      ({"capital_inr": "0", "mode": "LIVE"}, "live at ₹0"),
                      ({"f3": _f3_doc(risk_per_trade_pct=D("0.5")), "max_loss_per_unit": "150"}, "the risk percent below the entry share binds"),
                      ({"lot_size": None}, "no lot size")):  # fmt: skip
        out.append((se, why, sb | over))
    ps = p + "propose_spread"
    prices_up = [{"strike": "24300", "option_type": "PE", "price": "42.5"},
                 {"strike": "23800", "option_type": "PE", "price": "14.2"},
                 {"strike": "24350", "option_type": "PE", "price": "47"}]  # fmt: skip
    pb = {"mode": "PAPER", "direction": "UP", "levels": LEVELS_UP, "step": "50", "prices": prices_up,
          "lot_size": 65, "capital_inr": "2500000", "f3": f3, "common": common, "ceilings": ceil}  # fmt: skip
    for over, why in (
        ({}, "UP: strikes, credit, sizing (a lot beyond the budget)"),
        ({"capital_inr": "0"}, "paper at ₹0: clean, one lot"),
        ({"prices": prices_up[1:]}, "no short price: REJECTED_LIQUIDITY"),
        ({"prices": [prices_up[0]]}, "no wing price: REJECTED_LIQUIDITY"),
        ({"prices": [prices_up[0] | {"price": "9.5"}, prices_up[1] | {"price": "2"}]}, "a short below ₹10: REJECTED_COST"),
        ({"prices": [prices_up[0] | {"price": "14"}, prices_up[1] | {"price": "14.2"}]}, "a non-positive credit: REJECTED_COST"),
        ({"prices": [prices_up[0] | {"price": "9"}, prices_up[1] | {"price": "9.5"}]}, "both cost refusals listed"),
        ({"direction": "DOWN", "levels": LEVELS_DOWN, "capital_inr": "0",
          "prices": [{"strike": "24800", "option_type": "CE", "price": "38"}, {"strike": "25300", "option_type": "CE", "price": "11"}]}, "DOWN: a call spread"),
        ({"lot_size": None, "capital_inr": "0"}, "no lot size: REJECTED_SIZE, per-lot null"),
        ({"direction": "NONE"}, "no direction raises"),
    ):  # fmt: skip
        out.append((ps, why, pb | over))
    pos = {"direction": "UP", "level": "24400", "strikes": pe, "expiry": "2026-10-27",
           "entry_session": "2026-10-16", "entry_credit": "28.3", "lots": 1, "lot_size": 65,
           "max_loss_per_lot_inr": "30660.5"}  # fmt: skip
    ad = p + "add_decision"
    ab = {"position": pos, "today": "2026-10-19", "direction_now": "UP", "level_intact": True,
          "mark": "22", "capital_inr": "10000000", "f3": f3, "common": common}  # fmt: skip
    for over, why in (
        ({}, "decayed past 20 %: add the same lots under the full share"),
        ({"today": "2026-10-16"}, "the entry session itself: wait"),
        ({"direction_now": "NONE"}, "the direction changed"),
        ({"level_intact": False}, "the level broke: an exit, not an add"),
        ({"mark": None}, "no mark"),
        ({"mark": "22.65"}, "a paisa short of the working level"),
        ({"mark": "22.64"}, "exactly at the working level (28.3 x 0.8): add"),
        ({"capital_inr": "100000"}, "the full share is reached"),
        ({"capital_inr": "0"}, "paper at ₹0: one add of one lot"),
        ({"capital_inr": "0", "position": pos | {"lots": 2}}, "paper at ₹0, already added"),
        ({"position": pos | {"lots": 2}, "common": _common_doc(max_lots=3)}, "fo_max_lots leaves room for one"),
    ):  # fmt: skip
        out.append((ad, why, ab | over))
    out.append((p + "f3_marks", "decay target 20 % of credit, loss cut 2 x, to the paisa",
                {"f3": f3, "credits": ["28.3", "10", "33.335", "0"]}))  # fmt: skip
    fx = p + "f3_exit"
    xb = {"position": pos, "last_price": "24800", "mark": "20", "f3": f3, "common": common}
    for now, over, why in (
        ("2026-10-20T11:00:00", {"last_price": "24375"}, "the index beyond the level: LEVEL_BREAK"),
        ("2026-10-20T11:00:00", {"mark": "56.6"}, "mark at 2 x credit: LOSS_CUT"),
        ("2026-10-20T11:00:00", {"mark": "5.66"}, "mark at 20 % of credit: DECAY_TARGET"),
        ("2026-10-27T15:00:00", {}, "expiry day at 15:00: HARD_EXIT"),
        ("2026-10-27T14:59:00", {}, "expiry day before 15:00: hold"),
        ("2026-10-28T09:20:00", {"last_price": None, "mark": None}, "past expiry, no quotes: HARD_EXIT"),
        ("2026-10-20T11:00:00", {"last_price": "24375", "mark": "60"}, "LEVEL_BREAK outranks LOSS_CUT"),
        ("2026-10-20T11:00:00", {"last_price": None, "mark": None}, "no quotes mid-hold: hold"),
        ("2026-10-20T11:00:00", {}, "nothing due"),
        ("2026-10-20T11:00:00", {"position": pos | {"direction": "DOWN", "level": "25100"}, "last_price": "25126"}, "DOWN broken upward"),
    ):  # fmt: skip
        out.append((fx, why, xb | over | {"now": now}))
    return out


# ================================================================================================
# Synthetic bhavcopies (seeded; rounded to the paisa before they are written)
# ================================================================================================

#: ``fo_contract_daily``'s columns (the day files say ``date`` for ``trade_date``) and dtypes.
CONTRACT_COLUMNS = ["trade_date", "symbol", "instrument", "expiry", "strike", "option_type", "open",
                    "high", "low", "close", "settle", "underlying", "open_interest", "oi_change",
                    "volume", "turnover", "lot_size"]  # fmt: skip
DTYPES = {"trade_date": "Date", "date": "Date", "symbol": "String", "instrument": "String",
          "expiry": "Date", "strike": "Float64", "option_type": "String", "open": "Float64",
          "high": "Float64", "low": "Float64", "close": "Float64", "settle": "Float64",
          "underlying": "Float64", "open_interest": "Int64", "oi_change": "Int64",
          "volume": "Int64", "turnover": "Float64", "lot_size": "Int64"}  # fmt: skip
#: ``series.FUTURES_COLUMNS``.
SERIES_COLUMNS = ["trade_date", "symbol", "instrument", "expiry", "open", "high", "low", "settle",
                  "underlying", "open_interest", "turnover", "lot_size"]  # fmt: skip
#: ``panel.py``'s ``futures.parquet`` and ``options.parquet`` columns (research, retest).
RESEARCH_FUTURES = ["date", "instrument", "symbol", "expiry", "open", "high", "low", "close",
                    "settle", "underlying", "open_interest", "oi_change", "volume", "turnover",
                    "lot_size"]  # fmt: skip
RESEARCH_OPTIONS = ["date", "instrument", "symbol", "expiry", "strike", "option_type", "close",
                    "settle", "open_interest", "volume"]  # fmt: skip


def rows_doc(rows: list[dict[str, Any]], columns: list[str], *, date_key: str = "trade_date") -> dict[str, Any]:
    """Rows (keyed by ``CONTRACT_COLUMNS`` names) as a frame doc with ``columns``; ``date_key``
    names the session column (``trade_date`` or the files' ``date``)."""
    def get(r: dict[str, Any], c: str) -> Any:
        return r.get("trade_date") if c == date_key else r.get(c)

    return frame_doc(columns, {c: DTYPES[c] for c in columns}, [[get(r, c) for c in columns] for r in rows])


def _walk(seed: int, n: int, *, start: float, drift: float, vol: float) -> list[float]:
    import numpy as np

    rng = np.random.default_rng(seed)
    out = [start]
    for _ in range(n - 1):
        out.append(out[-1] * math.exp(float(rng.normal(drift, vol))))
    return out


def _month_ends(all_days: list[dt.date]) -> list[dt.date]:
    last: dict[tuple[int, int], dt.date] = {}
    for d_ in all_days:
        last[(d_.year, d_.month)] = d_
    return sorted(last.values())


def _futures_rows(  # noqa: PLR0913 - one keyword per way a symbol varies
    seed: int, symbol: str, instrument: str, sessions: list[dt.date], spots: list[float], *,
    lot: int, turnover: float, basis_pct: float = 0.06, with_spot: bool = True,
    expiries: list[dt.date] | None = None,
) -> list[dict[str, Any]]:  # fmt: skip
    """The near month every session, the next month on the last three sessions of the near one.
    Settles carry a basis of ``basis_pct`` a year; OHLC wrap the settle; the next month trades a
    fifth of the near month's turnover."""
    import numpy as np

    rng = np.random.default_rng(seed)
    horizon = _weekdays(sessions[0], len(sessions) + 45)
    ends = expiries or _month_ends(horizon)[:-1]
    index = {d_: i for i, d_ in enumerate(horizon)}
    rows: list[dict[str, Any]] = []
    for d_, spot in zip(sessions, spots, strict=True):
        near = min(e for e in ends if e >= d_)
        listed = [near]
        later = [e for e in ends if e > near]
        if later and index.get(near, 10**6) - index[d_] <= 2:  # noqa: PLR2004
            listed.append(later[0])
        eo = float(rng.normal(0, 0.004))
        eh, el = abs(float(rng.normal(0, 0.006))), abs(float(rng.normal(0, 0.006)))
        for k, e in enumerate(listed):
            settle = round(spot * (1 + basis_pct * (e - d_).days / 365), 2)
            open_ = round(settle * (1 + eo), 2)
            rows.append({
                "trade_date": d_, "symbol": symbol, "instrument": instrument, "expiry": e,
                "strike": 0.0, "option_type": "XX", "open": open_,
                "high": round(max(open_, settle) * (1 + eh), 2),
                "low": round(min(open_, settle) * (1 - el), 2), "close": settle,
                "settle": settle, "underlying": round(spot, 2) if with_spot else None,
                "open_interest": int(100000 + rng.integers(0, 50000)) // (5 if k else 1),
                "oi_change": int(rng.integers(-5000, 5000)),
                "volume": int(1000 + rng.integers(0, 500)),
                "turnover": round(turnover * (1 + 0.2 * float(rng.normal())) / (5 if k else 1), 2),
                "lot_size": lot,
            })  # fmt: skip
    return rows


def _chain_rows(  # noqa: PLR0913 - the chain's knobs
    seed: int, symbol: str, instrument: str, futures: list[dict[str, Any]], expiry: dt.date,
    strikes: list[float], *, vol: float, rate: float = 0.065, drop: float = 0.08,
    since: dt.date | None = None, kinds: tuple[str, ...] = ("CE", "PE"), lot: int = 75,
) -> list[dict[str, Any]]:  # fmt: skip
    """Option rows for ``expiry`` priced by the research's Black-76 on that expiry's settle each
    session; about ``drop`` of the prints did not trade (volume 0, OI kept)."""
    import numpy as np

    from baskfy_core.fno.research import b76

    rng = np.random.default_rng(seed)
    out: list[dict[str, Any]] = []
    for f in futures:
        d_ = f["trade_date"]
        if f["symbol"] != symbol or f["expiry"] != expiry or d_ > expiry:
            continue
        if since is not None and d_ < since:
            continue
        years = max((expiry - d_).days, 0.5) / 365.0
        for k in strikes:
            for kind in kinds:
                price = float(b76(np.array([f["settle"]]), np.array([k]), np.array([years]),
                                  np.array([vol]), np.array([kind]), rate)[0])  # fmt: skip
                price = max(round(price, 2), 0.05)
                traded = float(rng.random()) >= drop
                out.append({
                    "trade_date": d_, "symbol": symbol, "instrument": instrument, "expiry": expiry,
                    "strike": float(k), "option_type": kind, "open": price, "high": price,
                    "low": price, "close": price, "settle": price, "underlying": f["underlying"],
                    "open_interest": int(rng.integers(1000, 90000)), "oi_change": 0,
                    "volume": int(rng.integers(1, 900)) if traded else 0, "turnover": 1e5,
                    "lot_size": lot,
                })  # fmt: skip
    return out


#: Market M1: 76 sessions of 2025, NIFTY's and BANKNIFTY's index futures and six stock futures (S3
#: splits 1:2 on session 40), spot printed (UDiFF era).
M1_SESSIONS = _weekdays(dt.date(2025, 1, 1), 76)


def _market_m1() -> list[dict[str, Any]]:
    s = M1_SESSIONS
    n = len(s)
    rows: list[dict[str, Any]] = []
    rows += _futures_rows(101, "NIFTY", "FUTIDX", s, _walk(1, n, start=23500, drift=0.0012, vol=0.007), lot=75, turnover=9e11)
    rows += _futures_rows(102, "BANKNIFTY", "FUTIDX", s, _walk(2, n, start=50000, drift=0.002, vol=0.006), lot=30, turnover=6e11)
    rows += _futures_rows(103, "S1", "FUTSTK", s, _walk(3, n, start=1000, drift=0.004, vol=0.015), lot=500, turnover=5e9)
    rows += _futures_rows(104, "S2", "FUTSTK", s, _walk(4, n, start=2400, drift=-0.003, vol=0.018), lot=250, turnover=4e9)
    s3 = _walk(5, n, start=800, drift=0.002, vol=0.02)
    s3 = [v / 2 if i >= 40 else v for i, v in enumerate(s3)]  # noqa: PLR2004 - the split session
    rows += _futures_rows(105, "S3", "FUTSTK", s, s3, lot=600, turnover=3e9)
    rows += _futures_rows(106, "S4", "FUTSTK", s, _walk(6, n, start=300, drift=0.003, vol=0.012), lot=1500, turnover=1e9)
    rows += _futures_rows(107, "S5", "FUTSTK", s, _walk(7, n, start=150, drift=0.005, vol=0.01), lot=3000, turnover=4.5e9)
    rows += _futures_rows(108, "S6", "FUTSTK", s, _walk(8, n, start=4000, drift=-0.004, vol=0.012), lot=125, turnover=3.5e9)
    return sorted(rows, key=lambda r: (r["trade_date"], r["symbol"], r["expiry"]))


#: Market M2: 120 sessions from Dec 2024, NIFTY, S1 and S2 futures, plus NIFTY's and S1's chains for the
#: April monthly (30 Apr 2025) over the sessions the condor and debit families read.
M2_SESSIONS = _weekdays(dt.date(2024, 12, 2), 120)
M2_EXPIRY = dt.date(2025, 4, 30)


def _market_m2(nifty_shock: float = 0.0, *, two_cycles: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    s = M2_SESSIONS
    n = len(s)
    nifty = _walk(11, n, start=23500, drift=0.0008, vol=0.004)
    if nifty_shock:
        nifty = [v * (1 + nifty_shock) if d_ >= dt.date(2025, 4, 15) else v for v, d_ in zip(nifty, s, strict=True)]
    stock = [1000 * math.exp(0.004 * i) for i in range(n)]
    fut = _futures_rows(201, "NIFTY", "FUTIDX", s, nifty, lot=75, turnover=9e11)
    fut += _futures_rows(202, "S1", "FUTSTK", s, stock, lot=500, turnover=5e9)
    fut += _futures_rows(203, "S2", "FUTSTK", s, _walk(12, n, start=700, drift=0.0, vol=0.01), lot=1000, turnover=1e9)
    fut.sort(key=lambda r: (r["trade_date"], r["symbol"], r["expiry"]))
    nifty_k = [float(k) for k in range(23000, 27001, 250)]
    stock_k = [float(k) for k in range(1300, 1801, 25)]
    opts = _chain_rows(301, "NIFTY", "OPTIDX", fut, M2_EXPIRY, nifty_k, vol=0.15, since=dt.date(2025, 4, 8), drop=0.05)
    opts += _chain_rows(302, "S1", "OPTSTK", fut, M2_EXPIRY, stock_k, vol=0.30, since=dt.date(2025, 3, 31), drop=0.0, lot=500)
    if two_cycles:  # the March monthly too, so a family's summary has more than one trade
        march = dt.date(2025, 3, 31)
        opts += _chain_rows(303, "NIFTY", "OPTIDX", fut, march, [float(k) for k in range(22750, 26251, 250)],
                            vol=0.15, since=dt.date(2025, 3, 7), drop=0.05)  # fmt: skip
        opts += _chain_rows(304, "S1", "OPTSTK", fut, march, [float(k) for k in range(1150, 1651, 25)],
                            vol=0.30, since=dt.date(2025, 2, 27), drop=0.0, lot=500)  # fmt: skip
    return fut, opts


def _index_contracts(  # noqa: PLR0913 - the synthetic market's knobs
    symbol: str, closes: list[float], *, expiries: list[dt.date], strikes: range,
    kinds: tuple[str, ...] = ("PE",), options_from: int = 58, atm_premium: float = 30.0,
    slope: float = 0.02, decay_from: dt.date | None = None, lot: int = 65,
    end: dt.date = dt.date(2026, 9, 25),
) -> list[dict[str, Any]]:  # fmt: skip
    """``test_fno_directional_retest``'s market: a FUTIDX bar per session (OHLC = close ± 20) and,
    from session ``options_from`` on, every listed strike priced on its distance from the close,
    one rupee from ``decay_from``, and the index's level on an expiry day (NSE's file)."""
    sessions: list[dt.date] = []
    cur = end
    while len(sessions) < len(closes):
        if cur.weekday() < 5:  # noqa: PLR2004
            sessions.append(cur)
        cur -= dt.timedelta(days=1)
    sessions.sort()
    rows: list[dict[str, Any]] = []
    for i, (d_, close) in enumerate(zip(sessions, closes, strict=True)):
        c = float(close)
        rows.append({"trade_date": d_, "symbol": symbol, "instrument": "FUTIDX", "expiry": max(expiries),
                     "strike": 0.0, "option_type": "XX", "open": c, "high": c + 20, "low": c - 20,
                     "close": c, "settle": c, "underlying": None, "open_interest": 1000,
                     "oi_change": 0, "volume": 100, "turnover": 1e9, "lot_size": lot})  # fmt: skip
        if i < options_from:
            continue
        for expiry in expiries:
            if expiry < d_:
                continue
            for k in strikes:
                for kind in kinds:
                    away = max(0.0, c - k) if kind == "PE" else max(0.0, k - c)
                    price = max(1.0, atm_premium - slope * away)
                    if decay_from is not None and d_ >= decay_from:
                        price = 1.0
                    if d_ == expiry:
                        price = c
                    rows.append({"trade_date": d_, "symbol": symbol, "instrument": "OPTIDX",
                                 "expiry": expiry, "strike": float(k), "option_type": kind,
                                 "open": price, "high": price, "low": price, "close": price,
                                 "settle": price, "underlying": None, "open_interest": 500,
                                 "oi_change": 0, "volume": 50, "turnover": 1e5, "lot_size": lot})  # fmt: skip
    return rows


def _uptrend(n: int = 70, *, dip_at: int = 8, level: int = 24000, step: int = 20) -> list[int]:
    closes: list[int] = []
    for i in range(n):
        level += step
        closes.append(level - (300 if i == n - dip_at else 0))
    return closes


# ================================================================================================
# series, underlying, vol
# ================================================================================================


def call_continuous_futures(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import SeriesConfig
    from baskfy_core.fno.series import continuous_futures

    return _capture(lambda: continuous_futures(frame(inputs["futures"]), cfg(SeriesConfig, inputs["config"])))


def call_derive_underlying(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import SeriesConfig
    from baskfy_core.fno.underlying import derive_underlying

    return _capture(lambda: derive_underlying(frame(inputs["window"]), day(inputs["trade_date"]),
                                              days(inputs["sessions"]), cfg(SeriesConfig, inputs["config"])))  # fmt: skip


def call_iv_expiry(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.vol import iv_expiry

    return iv_expiry(day(inputs["day"]), days(inputs["expiries"]), days(inputs["sessions"]),
                     int(inputs["min_left"]))  # fmt: skip


def call_atm_iv(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import SeriesConfig
    from baskfy_core.fno.vol import OptionPrint, atm_iv
    from baskfy_core.options.config import OptionType

    prints = [OptionPrint(day(o["expiry"]), float(o["strike"]), OptionType(o["option_type"]),
                          o["close"], int(o["volume"])) for o in inputs["options"]]  # fmt: skip
    return atm_iv(day=day(inputs["day"]), options=prints,
                  futures_settle={day(k): float(v) for k, v in inputs["futures_settle"].items()},
                  sessions=days(inputs["sessions"]), config=cfg(SeriesConfig, inputs["config"]))  # fmt: skip


def call_iv_atm_frame(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import SeriesConfig
    from baskfy_core.fno.vol import iv_atm_frame

    return _capture(lambda: iv_atm_frame(frame(inputs["options"]), frame(inputs["futures"]),
                                         days(inputs["sessions"]), cfg(SeriesConfig, inputs["config"])))  # fmt: skip


def _series_rows(symbol: str, sessions: list[dt.date], settles: list[float], expiry: dt.date, *,
                 spot: float | None = None, instrument: str = "FUTSTK") -> list[dict[str, Any]]:  # fmt: skip
    return [{"trade_date": d_, "symbol": symbol, "instrument": instrument, "expiry": expiry,
             "strike": 0.0, "option_type": "XX", "open": s_, "high": round(s_ * 1.01, 2),
             "low": round(s_ * 0.99, 2), "close": s_, "settle": s_,
             "underlying": None if spot is None else round(s_ - spot, 2), "open_interest": 1000 + i,
             "oi_change": 0, "volume": 10, "turnover": 1e7, "lot_size": 100}
            for i, (d_, s_) in enumerate(zip(sessions, settles, strict=True))]  # fmt: skip


def _series_cases() -> list[tuple[str, str, dict[str, Any]]]:  # noqa: PLR0915 - a flat table
    out: list[tuple[str, str, dict[str, Any]]] = []
    conf = _series_doc()
    cf = PREFIX + "series.continuous_futures"
    m1 = _market_m1()
    out.append((cf, "market M1: two index futures and four stocks with rolls and a 1:2 split, spot printed",
                {"futures": rows_doc(m1, SERIES_COLUMNS), "config": conf}))  # fmt: skip
    near, nxt = dt.date(2024, 7, 25), dt.date(2024, 8, 29)
    d4 = [dt.date(2024, 7, 23), dt.date(2024, 7, 24), near, dt.date(2024, 7, 26)]
    roll = (_series_rows("ABC", d4[:3], [100.0, 101.0, 102.0], near)
            + _series_rows("ABC", d4, [102.0, 103.02, 104.04, 105.0], nxt))  # fmt: skip
    roll.append(dict(roll[0], trade_date=dt.date(2024, 7, 26), expiry=near, settle=0.0, open=0.0))
    out.append((cf, "a roll is no jump; a row with settle 0 is ignored; held into its own expiry day",
                {"futures": rows_doc(roll, SERIES_COLUMNS), "config": conf}))  # fmt: skip
    d12 = _weekdays(dt.date(2024, 7, 1), 12)
    split = [100.0, 101.0, 102.0, 51.0, 51.5, 52.0, 52.5, 53.0, 53.5, 54.0, 54.5, 55.0]
    out.append((cf, "a 1:2 split is flagged, excluded, and ca_recent for five sessions (UDiFF spot, basis)",
                {"futures": rows_doc(_series_rows("ABC", d12, split, nxt, spot=1.5), SERIES_COLUMNS), "config": conf}))  # fmt: skip
    up = [100.0, 139.0, 139.0, 141.0 * 139 / 100, 69.0 * 141 * 139 / 100 / 100]
    out.append((cf, "the CA band edges: 1.39 not flagged, 1.41 and 0.69 flagged",
                {"futures": rows_doc(_series_rows("ABC", _weekdays(dt.date(2024, 7, 1), 5), [round(v, 2) for v in up], nxt), SERIES_COLUMNS), "config": conf}))  # fmt: skip
    d25 = _weekdays(dt.date(2024, 6, 3), 25)
    zig = [100.0]
    for i in range(1, 25):
        zig.append(round(zig[-1] * (1.01 if i % 2 else 0.995), 4))
    out.append((cf, "ATR14 and RV20 over 25 sessions, pre-UDiFF (no basis)",
                {"futures": rows_doc(_series_rows("ABC", d25, zig, dt.date(2024, 7, 25), spot=0.5), SERIES_COLUMNS), "config": conf}))  # fmt: skip
    out.append((cf, "a shorter ATR/RV window from config",
                {"futures": rows_doc(_series_rows("ABC", d25, zig, dt.date(2024, 7, 25)), SERIES_COLUMNS),
                 "config": _series_doc(atr_sessions=3, rv_sessions=5, turnover_sessions=3, ca_exclusion_sessions=2)}))  # fmt: skip
    out.append((cf, "empty", {"futures": rows_doc([], SERIES_COLUMNS), "config": conf}))
    # --- underlying ----------------------------------------------------------------------------
    du = PREFIX + "underlying.derive_underlying"
    days30 = _weekdays(dt.date(2024, 7, 15), 30)
    cal = _weekdays(days30[0], 80)
    expiry = dt.date(2024, 9, 26)
    window = _series_rows("ABC", days30, [round(1000.0 * math.exp(0.01 * math.sin(i)), 2) for i in range(30)], expiry, spot=2.0)
    nifty = _series_rows("NIFTY", days30, [round(24000.0 * math.exp(0.004 * i), 2) for i in range(30)], expiry, spot=40.0, instrument="FUTIDX")
    chain = _chain_rows(401, "NIFTY", "OPTIDX", nifty, expiry,
                        [float(k) for k in range(26000, 27001, 100)], vol=0.14, rate=0.0, drop=0.0,
                        since=days30[-1])  # fmt: skip
    chain_thin = [dict(r, volume=0) if r["option_type"] == "PE" else r for r in chain]
    sess = _iso(cal)
    out.append((du, "a stock with no options: anchored to the held settle, iv null",
                {"window": rows_doc(window, CONTRACT_COLUMNS), "trade_date": days30[-1].isoformat(), "sessions": sess, "config": conf}))  # fmt: skip
    out.append((du, "NIFTY with its ATM chain: iv_atm from Black-76 at r = 0",
                {"window": rows_doc(window + nifty + chain, CONTRACT_COLUMNS), "trade_date": days30[-1].isoformat(), "sessions": sess, "config": conf}))  # fmt: skip
    out.append((du, "puts that did not trade: iv null",
                {"window": rows_doc(nifty + chain_thin, CONTRACT_COLUMNS), "trade_date": days30[-1].isoformat(), "sessions": sess, "config": conf}))  # fmt: skip
    out.append((du, "a window holding a later session: look-ahead refused",
                {"window": rows_doc(window, CONTRACT_COLUMNS), "trade_date": days30[-2].isoformat(), "sessions": sess, "config": conf}))  # fmt: skip
    out.append((du, "an empty window: the empty frame with the columns",
                {"window": rows_doc([], CONTRACT_COLUMNS), "trade_date": days30[-1].isoformat(), "sessions": sess, "config": conf}))  # fmt: skip
    out.append((du, "options only: no futures, empty",
                {"window": rows_doc(chain, CONTRACT_COLUMNS), "trade_date": days30[-1].isoformat(), "sessions": sess, "config": conf}))  # fmt: skip
    out.append((du, "a trade date the series never printed: empty",
                {"window": rows_doc(window[:-1], CONTRACT_COLUMNS), "trade_date": days30[-1].isoformat(), "sessions": sess, "config": conf}))  # fmt: skip
    # --- vol -----------------------------------------------------------------------------------
    ie = PREFIX + "vol.iv_expiry"
    exps = ["2024-08-29", "2024-08-22", "2024-09-26", "2024-10-31"]
    for d_, ml, why in (("2024-08-14", 8, "eleven sessions to the August monthly"),
                        ("2024-08-20", 8, "seven left: the September monthly"),
                        ("2024-08-19", 8, "exactly eight left"),
                        ("2024-10-30", 8, "nothing far enough within the calendar: null")):  # fmt: skip
        out.append((ie, why, {"day": d_, "expiries": exps, "sessions": sess, "min_left": ml}))
    ai = PREFIX + "vol.atm_iv"
    trade = days30[-1]
    prints = [{"expiry": r["expiry"].isoformat(), "strike": r["strike"], "option_type": r["option_type"],
               "close": r["close"], "volume": r["volume"]} for r in chain]  # fmt: skip
    fwd = {expiry.isoformat(): nifty[-1]["settle"]}
    base = {"day": trade.isoformat(), "options": prints, "futures_settle": fwd, "sessions": sess, "config": conf}
    out.append((ai, "the strike nearest the forward, mean of CE and PE", base))
    out.append((ai, "a CE with no close: iv null, iv_pe kept",
                base | {"options": [dict(o, close=None) if o["option_type"] == "CE" else o for o in prints]}))  # fmt: skip
    out.append((ai, "no settle for the qualifying future: null", base | {"futures_settle": {}}))
    out.append((ai, "the forward midway between strikes takes the lower",
                base | {"futures_settle": {expiry.isoformat(): 26450.0}}))  # fmt: skip
    out.append((ai, "no options: null", base | {"options": []}))
    out.append((ai, "a price below intrinsic: the solver refuses that leg",
                base | {"options": [dict(o, close=0.01) if o["option_type"] == "PE" else o for o in prints]}))  # fmt: skip
    vf = PREFIX + "vol.iv_atm_frame"
    ocols = ["trade_date", "symbol", "expiry", "strike", "option_type", "close", "volume"]
    fcols = ["trade_date", "symbol", "expiry", "settle"]
    out.append((vf, "one row per (trade_date, symbol) with options",
                {"options": rows_doc(chain, ocols), "futures": rows_doc(nifty, fcols), "sessions": sess, "config": conf}))  # fmt: skip
    out.append((vf, "a symbol without a qualifying future: nulls",
                {"options": rows_doc(chain + [dict(r, symbol="XYZ") for r in chain[:4]], ocols),
                 "futures": rows_doc(nifty, fcols), "sessions": sess, "config": conf}))  # fmt: skip
    out.append((vf, "no options: an empty frame", {"options": rows_doc([], ocols), "futures": rows_doc(nifty, fcols), "sessions": sess, "config": conf}))
    return out


# ================================================================================================
# scan
# ================================================================================================


def _leg_prints(rows: list[dict[str, Any]]) -> dict[tuple[Decimal, Any], Any]:
    from baskfy_core.fno.scan import LegPrint
    from baskfy_core.options.config import OptionType

    return {(D(r["strike"]), OptionType(r["option_type"])): LegPrint(D(r["settle"]), r["open_interest"], r["volume"])
            for r in rows}  # fmt: skip


def call_propose_condor(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.scan import propose_condor
    from baskfy_core.options.config import CostRates

    return _capture(lambda: propose_condor(
        forward=D(inputs["forward"]), iv=float(inputs["iv"]), days_to_expiry=int(inputs["days_to_expiry"]),
        call_strikes=[D(k) for k in inputs["call_strikes"]], put_strikes=[D(k) for k in inputs["put_strikes"]],
        prints=_leg_prints(inputs["prints"]), lot_size=int(inputs["lot_size"]),
        capital_inr=D(inputs["capital_inr"]), config=_fno_config(inputs["config"]),
        rates=cfg(CostRates, inputs["rates"]), ceilings=_ceilings(inputs["ceilings"]),
    ))  # fmt: skip


def call_f2_signals(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import F2Config, SeriesConfig
    from baskfy_core.fno.scan import f2_signals

    return _capture(lambda: f2_signals(frame(inputs["futures"]), day(inputs["trade_date"]),
                                       cfg(F2Config, inputs["f2"]), cfg(SeriesConfig, inputs["series"])))  # fmt: skip


def call_propose_future(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.scan import propose_future

    return _capture(lambda: propose_future(
        entry=D(inputs["entry"]), atr=D(inputs["atr"]), ann_vol=float(inputs["ann_vol"]),
        lot_size=int(inputs["lot_size"]), capital_inr=D(inputs["capital_inr"]),
        config=_fno_config(inputs["config"]), ceilings=_ceilings(inputs["ceilings"]),
    ))  # fmt: skip


def call_allocate_capacity(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import F2Config
    from baskfy_core.fno.scan import allocate_capacity

    return allocate_capacity([(c[0], c[1]) for c in inputs["ordered"]], inputs["open_industries"],
                             cfg(F2Config, inputs["f2"]))  # fmt: skip


def _print(k: int, kind: str, settle: str, oi: int | None = 10_000_000, vol: int | None = 100) -> dict[str, Any]:
    return {"strike": str(k), "option_type": kind, "settle": settle, "open_interest": oi, "volume": vol}


def _scan_cases() -> list[tuple[str, str, dict[str, Any]]]:  # noqa: PLR0915 - a flat table
    out: list[tuple[str, str, dict[str, Any]]] = []
    p = PREFIX + "scan."
    conf, ceil, rates = _fno_config_doc(), _ceil_doc(), _option_rates_doc()
    grid = _strike_grid(18000, 22000, 100)
    good = [_print(20800, "CE", "60"), _print(21100, "CE", "30"), _print(19200, "PE", "70"), _print(18900, "PE", "35")]
    base = {"forward": "20000", "iv": 0.15, "days_to_expiry": 21, "call_strikes": grid, "put_strikes": grid,
            "prints": good, "lot_size": 25, "capital_inr": "1000000", "config": conf, "rates": rates,
            "ceilings": ceil}  # fmt: skip
    pc = p + "propose_condor"
    for over, why in (
        ({}, "04 §2's strikes, credit 65, loss 235, one lot"),
        ({"lot_size": 75}, "a lot beyond the budget: REJECTED_SIZE"),
        ({"prints": [dict(r, open_interest=7500) for r in good[:2]] + good[2:3] + [dict(good[3], volume=0)]}, "a short below 500 lots of OI and a wing that did not trade: REJECTED_LIQUIDITY"),
        ({"prints": good[:3]}, "a leg with no print: REJECTED_LIQUIDITY, unpriced"),
        ({"prints": [dict(good[0], settle="0")] + good[1:]}, "a zero settle is no print"),
        ({"prints": [_print(20800, "CE", "3"), _print(21100, "CE", "1"), _print(19200, "PE", "3"), _print(18900, "PE", "1")]}, "a credit the costs eat: REJECTED_COST"),
        ({"call_strikes": ["20000"]}, "no listed strike: REJECTED_STRUCTURE"),
        ({"prints": [_print(20800, "CE", "20"), _print(21100, "CE", "40"), _print(19200, "PE", "20"), _print(18900, "PE", "40")]}, "a debit: REJECTED_STRUCTURE"),
        ({"capital_inr": "0"}, "paper at ₹0: one lot, lots_at_ceiling"),
        ({"prints": [dict(r, open_interest=None, volume=None) for r in good]}, "null OI and volume: refusals by name"),
    ):  # fmt: skip
        out.append((pc, why, base | over))
    # --- F2 signals -------------------------------------------------------------------------------
    fs = p + "f2_signals"
    days60 = _weekdays(dt.date(2030, 1, 1), 60)
    exp = dt.date(2030, 12, 26)

    def rows(sym: str, settles: list[float], turnover: float, inst: str = "FUTSTK") -> list[dict[str, Any]]:
        return [{"trade_date": d_, "symbol": sym, "instrument": inst, "expiry": exp, "open": s_,
                 "high": round(s_ * 1.01, 4), "low": round(s_ * 0.99, 4), "settle": s_, "underlying": None,
                 "open_interest": 1000, "turnover": turnover, "lot_size": 100}
                for d_, s_ in zip(days60, settles, strict=True)]  # fmt: skip

    panel = (rows("AAA", [100.0 + i for i in range(60)], 5e9) + rows("BBB", [200.0 - i for i in range(60)], 4e9)
             + rows("CCC", [50.0] * 60, 3e9) + rows("DDD", [100.0 + i for i in range(60)], 2e9)
             + rows("EEE", [100.0 + i for i in range(60)], 1e9)
             + rows("NIFTY", [20000.0 + 10 * i for i in range(60)], 9e11, "FUTIDX"))  # fmt: skip
    f2, series = _f2_doc(), _series_doc()
    out.append((fs, "breakout, trend, universe top 60 % by turnover, levels in rupees",
                {"futures": rows_doc(panel, SERIES_COLUMNS), "trade_date": days60[-1].isoformat(), "f2": f2, "series": series}))  # fmt: skip
    short = [r for r in panel if r["trade_date"] >= days60[-30]]
    out.append((fs, "a 30-session window: trend null, not False",
                {"futures": rows_doc(short, SERIES_COLUMNS), "trade_date": days60[-1].isoformat(), "f2": f2, "series": series}))  # fmt: skip
    out.append((fs, "a row after the session: look-ahead refused",
                {"futures": rows_doc(panel, SERIES_COLUMNS), "trade_date": days60[-2].isoformat(), "f2": f2, "series": series}))  # fmt: skip
    out.append((fs, "empty", {"futures": rows_doc([], SERIES_COLUMNS), "trade_date": days60[-1].isoformat(), "f2": f2, "series": series}))
    out.append((fs, "a session the window does not hold: empty",
                {"futures": rows_doc(panel, SERIES_COLUMNS), "trade_date": "2030-12-31", "f2": f2, "series": series}))  # fmt: skip
    m1 = _market_m1()
    out.append((fs, "market M1 on its last session: rolls, a split inside the window",
                {"futures": rows_doc(m1, SERIES_COLUMNS), "trade_date": M1_SESSIONS[-1].isoformat(), "f2": f2, "series": series}))  # fmt: skip
    out.append((fs, "market M1 three sessions after the split: ca_recent",
                {"futures": rows_doc([r for r in m1 if r["trade_date"] <= M1_SESSIONS[42]], SERIES_COLUMNS),
                 "trade_date": M1_SESSIONS[42].isoformat(), "f2": _f2_doc(breakout_sessions=10, trend_sessions=20, universe_turnover_pct=D("50")),
                 "series": series}))  # fmt: skip
    # --- F2 proposal and capacity ----------------------------------------------------------------
    pf = p + "propose_future"
    fb = {"entry": "2450.5", "atr": "40.25", "ann_vol": 0.25, "lot_size": 250, "capital_inr": "2500000", "config": conf, "ceilings": ceil}
    for over, why in (({}, "stop, the GTT, lots and the cost in R"), ({"capital_inr": "0"}, "paper at ₹0: one lot"),
                      ({"capital_inr": "1000000", "lot_size": 1250}, "REJECTED_SIZE: zero lots, costed at one"),
                      ({"atr": "0"}, "zero ATR raises"), ({"ann_vol": 0.9}, "a high vol"),
                      ({"entry": "100", "atr": "40"}, "a stop below zero is not refused here (the monitor refuses it): sized on 120 a unit")):  # fmt: skip
        out.append((pf, why, fb | over))
    ac = p + "allocate_capacity"
    for ordered, opened, f2_, why in (
        ([["A", "IT"], ["B", "IT"], ["C", "Bank"], ["D", None]], [], f2, "all admitted"),
        ([["A", "IT"], ["B", "IT"], ["C", "IT"]], [], f2, "the third in one industry is blocked"),
        ([["A", "IT"], ["B", "Bank"]], ["IT", "IT"], f2, "open positions count toward the industry"),
        ([["A", None], ["B", None], ["C", None]], ["X", "Y", "Z", None], f2, "f2_max_open 5 counts the book"),
        ([["A", "IT"]], ["IT", "Bank", "Auto", "Pharma", "Metal"], f2, "a full book"),
        ([["A", "IT"], ["B", "IT"]], [], _f2_doc(max_per_industry=1, max_open=1), "tight caps"),
        ([], ["IT"], f2, "no candidates"),
    ):  # fmt: skip
        out.append((ac, why, {"ordered": ordered, "open_industries": opened, "f2": f2_}))
    return out


# ================================================================================================
# research
# ================================================================================================


def _condor_params(raw: Mapping[str, Any]) -> Any:
    from baskfy_core.fno.research import CondorMode, CondorParams

    kw = dict(raw)
    if "mode" in kw:
        kw["mode"] = CondorMode(kw["mode"])
    if kw.get("symbols") is not None:
        kw["symbols"] = frozenset(kw["symbols"])
    return CondorParams(**kw)


def _futures_params(raw: Mapping[str, Any]) -> Any:
    from baskfy_core.fno.research import FuturesParams

    return FuturesParams(**raw)


def call_research_continuous(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.research import continuous

    return _capture(lambda: continuous(frame(inputs["futures"])))


def call_b76(inputs: dict[str, Any]) -> Any:
    import numpy as np

    from baskfy_core.fno.research import b76

    return b76(np.array(inputs["forward"], dtype=np.float64), np.array(inputs["strike"], dtype=np.float64),
               np.array(inputs["years"], dtype=np.float64), np.array(inputs["vol"], dtype=np.float64),
               np.array(inputs["kind"]), float(inputs["rate"]))  # fmt: skip


def call_implied(inputs: dict[str, Any]) -> Any:
    import numpy as np

    from baskfy_core.fno.research import implied

    return implied(np.array(inputs["forward"], dtype=np.float64), np.array(inputs["strike"], dtype=np.float64),
                   np.array(inputs["years"], dtype=np.float64), np.array(inputs["price"], dtype=np.float64),
                   np.array(inputs["kind"]), float(inputs["rate"]))  # fmt: skip


def call_leg_costs(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.research import leg_costs

    params = _condor_params(inputs["params"])
    return [leg_costs(float(c["price"]), bool(c["sell"]), float(c["lot"]), params) for c in inputs["cases"]]


def call_summarise(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.research import summarise

    return _capture(lambda: summarise(frame(inputs["trades"]), inputs["r_column"], inputs["date_column"]))


def call_futures_panel(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.research import futures_panel

    return futures_panel(frame(inputs["cont"]))


def call_liquid_base(inputs: dict[str, Any]) -> Any:
    """``{share: liquid_base(futures_panel(cont), share)}`` for each share listed."""
    from baskfy_core.fno.research import futures_panel, liquid_base

    panel = futures_panel(frame(inputs["cont"]))
    return {str(share): liquid_base(panel, float(share)) for share in inputs["shares"]}


def call_futures_signals(inputs: dict[str, Any]) -> Any:
    """``{family: futures_signals(futures_panel(cont), family)}`` for each family listed."""
    from baskfy_core.fno.research import FuturesFamily, futures_panel, futures_signals

    panel = futures_panel(frame(inputs["cont"]))
    return {f: futures_signals(panel, FuturesFamily(f)) for f in inputs["families"]}


def call_simulate_futures(inputs: dict[str, Any]) -> Any:
    """Each run is ``simulate_futures(panel, signals, params)`` with ``panel = futures_panel(cont)``
    and the run's ``signals`` frame, or ``futures_signals(panel, family)`` when it names a family."""
    from baskfy_core.fno.research import FuturesFamily, futures_panel, futures_signals, simulate_futures

    panel = futures_panel(frame(inputs["cont"]))
    out = []
    for run in inputs["runs"]:
        signals = (frame(run["signals"]) if run.get("signals") is not None
                   else futures_signals(panel, FuturesFamily(run["family"])))  # fmt: skip
        out.append(simulate_futures(panel, signals, _futures_params(run["params"])))
    return out


def call_f2_trades(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.research import f2_trades

    return f2_trades(frame(inputs["cont"]))


def call_liquid_symbols(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.research import liquid_symbols

    cont = frame(inputs["cont"])
    return {str(n): liquid_symbols(cont, int(n)) for n in inputs["top_ns"]}


def _option_panel(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.research import build_option_panel, continuous

    futures = frame(inputs["futures"])
    return build_option_panel(frame(inputs["options"]), futures, continuous(futures),
                              frozenset(inputs["symbols"]))  # fmt: skip


def call_build_option_panel(inputs: dict[str, Any]) -> Any:
    """The panel summarised: its dates, lots, the chains' keys and sizes, the futures-settle count,
    the rv20 map's non-null entries, and ``trading_days_before`` at a few probes."""
    panel = _option_panel(inputs)
    return {
        "dates": list(panel.dates),
        "lots": panel.lots,
        "groups": {f"{s}|{e.isoformat()}": {d_.isoformat(): len(ch) for d_, ch in sorted(by_day.items())}
                   for (s, e), by_day in sorted(panel.groups.items())},
        "fgroups": len(panel.fgroups),
        "rv20": {f"{s}|{d_.isoformat()}": v for (s, d_), v in sorted(panel.rv20.items()) if v is not None},
        "trading_days_before": {f"{p['expiry']}|{p['n']}": panel.trading_days_before(day(p["expiry"]), int(p["n"]))
                                for p in inputs["probes"]},
    }  # fmt: skip


def call_condor_trades(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.research import condor_trades

    return condor_trades(_option_panel(inputs), _condor_params(inputs["params"]))


def call_debit_trades(inputs: dict[str, Any]) -> Any:
    """Each run is ``debit_trades(panel, horizon, structure)`` on the one panel the inputs build."""
    from baskfy_core.fno.research import DebitStructure, debit_trades

    panel = _option_panel(inputs)
    return [debit_trades(panel, int(r["horizon"]), DebitStructure(r["structure"])) for r in inputs["runs"]]


def call_basis_study(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.research import basis_study

    return _capture(lambda: basis_study(frame(inputs["futures"]), float(inputs["cycle_cost"])))


def call_research_constants(_inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno import research as r

    return {"RESEARCH_RATE": r.RESEARCH_RATE, "FUTURES_ROUND_TRIP_COST": r.FUTURES_ROUND_TRIP_COST,
            "INDEX_UNDERLYINGS": r.INDEX_UNDERLYINGS, "B4": r.B4, "B4_LOSS_CLOSE": r.B4_LOSS_CLOSE,
            "F2_SPEC": r.F2_SPEC, "CondorParams()": r.CondorParams()}  # fmt: skip


def _cont_doc(rows: list[dict[str, Any]]) -> Any:
    from baskfy_core.fno.research import continuous

    return _golden()._enc(continuous(frame(rows_doc(rows, RESEARCH_FUTURES, date_key="date"))))


def _research_cases() -> list[tuple[str, str, dict[str, Any]]]:  # noqa: PLR0915 - a flat table
    out: list[tuple[str, str, dict[str, Any]]] = []
    p = PREFIX + "research."
    out.append((p + "constants", "the research's rate, cost, universe and named parameter sets", {}))
    m1 = _market_m1()
    fut_doc = rows_doc(m1, RESEARCH_FUTURES, date_key="date")
    out.append((p + "continuous", "market M1: the split session dropped, rolls compounded", {"futures": fut_doc}))
    near, nxt = dt.date(2024, 7, 25), dt.date(2024, 8, 29)
    d4 = [dt.date(2024, 7, 23), dt.date(2024, 7, 24), near, dt.date(2024, 7, 26)]
    roll = (_series_rows("ABC", d4[:3], [100.0, 101.0, 102.0], near) + _series_rows("ABC", d4, [102.0, 103.02, 104.04, 105.0], nxt))
    out.append((p + "continuous", "a roll across expiry, pre-UDiFF (no spot)",
                {"futures": rows_doc(roll, RESEARCH_FUTURES, date_key="date")}))  # fmt: skip
    out.append((p + "continuous", "empty", {"futures": rows_doc([], RESEARCH_FUTURES, date_key="date")}))
    b = p + "b76"
    out.append((b, "calls and puts across moneyness at the research rate",
                {"forward": [1000.0] * 6, "strike": [900.0, 1000.0, 1100.0, 900.0, 1000.0, 1100.0],
                 "years": [0.08] * 6, "vol": [0.2] * 6, "kind": ["CE", "CE", "CE", "PE", "PE", "PE"], "rate": 0.065}))  # fmt: skip
    out.append((b, "the vol and time floors (1e-4, 1e-6), rate 0",
                {"forward": [24000.0, 24000.0, 24000.0], "strike": [24000.0, 24500.0, 23500.0],
                 "years": [0.0, 0.05, 0.05], "vol": [0.15, 0.0, 0.15], "kind": ["CE", "CE", "PE"], "rate": 0.0}))  # fmt: skip
    im = p + "implied"
    out.append((im, "50 bisections invert b76 at the research rate",
                {"forward": [1000.0, 1000.0, 24000.0], "strike": [1000.0, 1050.0, 23000.0], "years": [0.0575, 0.0575, 0.04],
                 "price": [19.0, 3.5, 60.0], "kind": ["CE", "CE", "PE"], "rate": 0.065}))  # fmt: skip
    out.append((im, "a price above any vol's: pinned at 3.0; below intrinsic: 0.01",
                {"forward": [1000.0, 1000.0], "strike": [1000.0, 900.0], "years": [0.05, 0.05],
                 "price": [900.0, 50.0], "kind": ["CE", "CE"], "rate": 0.065}))  # fmt: skip
    lc = p + "leg_costs"
    for params, why in (({"slip_pct": 0.005}, "index slippage 0.5 %, ₹0.05 floor"), ({"slip_pct": 0.03}, "stock slippage 3 %")):
        out.append((lc, why, {"params": params, "cases": [
            {"price": 100.0, "sell": True, "lot": 75.0}, {"price": 100.0, "sell": False, "lot": 75.0},
            {"price": 1.0, "sell": False, "lot": 75.0}, {"price": 0.0, "sell": True, "lot": 500.0}]}))  # fmt: skip
    su = p + "summarise"
    tcols = {"entry": "Date", "R": "Float64", "cost_R": "Float64"}
    out.append((su, "n, mean, t, win rate, worst, gross and cost, per year",
                {"trades": frame_doc(["entry", "R", "cost_R"], tcols, [["2022-01-03", 0.5, 0.01], ["2022-02-01", -1.0, 0.02], ["2023-01-02", 0.2, 0.03]]),
                 "r_column": "R", "date_column": "entry"}))  # fmt: skip
    out.append((su, "no cost_R column: gross and cost null; identical R: t 0",
                {"trades": frame_doc(["entry", "R"], tcols, [["2022-01-03", 0.5], ["2022-02-01", 0.5]]), "r_column": "R", "date_column": "entry"}))  # fmt: skip
    out.append((su, "empty", {"trades": frame_doc(["entry", "R"], tcols, []), "r_column": "R", "date_column": "entry"}))
    out.append((su, "another R column name",
                {"trades": frame_doc(["entry", "R_spread"], {"entry": "Date", "R_spread": "Float64"}, [["2024-03-01", 0.3], ["2024-04-01", -0.1], ["2025-01-02", 0.0]]),
                 "r_column": "R_spread", "date_column": "entry"}))  # fmt: skip
    out.append((su, "one trade: std null, t 0",
                {"trades": frame_doc(["entry", "R"], tcols, [["2022-01-03", 0.5]]), "r_column": "R", "date_column": "entry"}))  # fmt: skip
    cont = _cont_doc(m1)
    out.append((p + "futures_panel", "M1's indicators: ATR, 20-day high/low, MAs, OI, momentum, NIFTY's regime", {"cont": cont}))
    out.append((p + "liquid_base", "the top 60 % and every stock by 20-day turnover on M1 "
                "(the composition: liquid_base(futures_panel(cont), share))", {"cont": cont, "shares": [0.6, 1.0]}))  # fmt: skip
    out.append((p + "futures_signals", "every family on M1 (the composition: futures_signals(futures_panel(cont), family))",
                {"cont": cont, "families": ["A1", "A1R", "A0", "A0_LONG", "A3", "A2", "A4"]}))  # fmt: skip
    sig_cols = {"symbol": "String", "di": "UInt32", "side": "Int32"}
    out.append((p + "simulate_futures", "each family's spec on M1, signals from futures_signals(futures_panel(cont), family)", {"cont": cont, "runs": [
        {"family": "A0_LONG", "params": {"horizon": 40, "k_atr": 3.0, "trail": True, "charge_rolls": True}},
        {"family": "A0", "params": {"horizon": 40, "k_atr": 3.0, "trail": True}},
        {"family": "A1", "params": {"horizon": 10, "k_atr": 2.0}},
        {"family": "A3", "params": {"horizon": 5, "k_atr": 2.0}},
        {"family": "A4", "params": {"horizon": 20, "k_atr": 2.0}},
        {"family": "A2", "params": {"horizon": 5, "k_atr": 3.0, "cost": 0.0}}]}))  # fmt: skip
    out.append((p + "simulate_futures", "hand-placed signals: a hold that swallows the next signal, a short, the "
                "last session (no t+1), an unknown symbol's di, a 1-session horizon", {"cont": cont, "runs": [
        {"params": {"horizon": 10, "k_atr": 2.0, "trail": True},
         "signals": frame_doc(["symbol", "di", "side"], sig_cols,
                              [["S1", 20, 1], ["S1", 22, 1], ["S2", 25, -1], ["S1", 75, 1], ["S4", 30, -1], ["S4", 500, 1]])},
        {"params": {"horizon": 1, "k_atr": 0.5},
         "signals": frame_doc(["symbol", "di", "side"], sig_cols, [["S1", 30, 1], ["S2", 30, -1]])}]}))  # fmt: skip
    out.append((p + "f2_trades", "res_f2.py on M1", {"cont": cont}))
    out.append((p + "liquid_symbols", "max(top_n, 60): every stock ever ranked, for 0 and 2", {"cont": cont, "top_ns": [0, 2]}))
    fut2, opts2 = _market_m2(two_cycles=True)
    nifty_opts = [r for r in opts2 if r["symbol"] == "NIFTY" and r["expiry"] == M2_EXPIRY]
    stock_opts = [r for r in opts2 if r["symbol"] == "S1"]
    f2doc = rows_doc(fut2, RESEARCH_FUTURES, date_key="date")
    nifty_doc = rows_doc(nifty_opts, RESEARCH_OPTIONS, date_key="date")
    stock_doc = rows_doc(stock_opts, RESEARCH_OPTIONS, date_key="date")
    nf2doc = rows_doc([r for r in fut2 if r["symbol"] == "NIFTY"], RESEARCH_FUTURES, date_key="date")
    out.append((p + "build_option_panel", "M2's NIFTY chain (summary of the panel)",
                {"options": nifty_doc, "futures": nf2doc, "symbols": ["NIFTY", "BANKNIFTY"],
                 "probes": [{"expiry": M2_EXPIRY.isoformat(), "n": 15}, {"expiry": M2_EXPIRY.isoformat(), "n": 0},
                            {"expiry": M2_EXPIRY.isoformat(), "n": 500}, {"expiry": "2025-04-29", "n": 1}]}))  # fmt: skip
    ct = p + "condor_trades"
    note = " (the composition: condor_trades(build_option_panel(options, futures, continuous(futures), symbols), params))"
    for params, why in (({"n_before": 15, "slip_pct": 0.005, "symbols": ["NIFTY", "BANKNIFTY"]}, "B4 on M2's NIFTY monthly"),
                        ({"n_before": 15, "slip_pct": 0.005, "loss_mult": 1.5, "symbols": ["NIFTY", "BANKNIFTY"]}, "B4 with the 1.5x loss close"),
                        ({"n_before": 15, "slip_pct": 0.005, "defend": True, "profit_take": 0.9, "symbols": ["NIFTY"]}, "defend on, a 90 % take"),
                        ({"n_before": 10, "exit_n": 4, "slip_pct": 0.005, "k_short": 0.75, "symbols": ["NIFTY"]}, "N 10, exit E-4, 0.75 sigma")):  # fmt: skip
        out.append((ct, why + note, {"options": nifty_doc, "futures": nf2doc, "symbols": ["NIFTY", "BANKNIFTY"], "params": params}))
    fut_shock, opts_shock = _market_m2(nifty_shock=0.06)
    out.append((ct, "a 6 % rally mid-cycle: the loss close" + note,
                {"options": rows_doc([r for r in opts_shock if r["symbol"] == "NIFTY"], RESEARCH_OPTIONS, date_key="date"),
                 "futures": rows_doc([r for r in fut_shock if r["symbol"] == "NIFTY"], RESEARCH_FUTURES, date_key="date"), "symbols": ["NIFTY"],
                 "params": {"n_before": 15, "slip_pct": 0.005, "loss_mult": 1.0, "symbols": ["NIFTY"]}}))  # fmt: skip
    out.append((ct, "trend_credit on a trending stock: a put spread" + note,
                {"options": stock_doc, "futures": f2doc, "symbols": ["S1"],
                 "params": {"n_before": 15, "slip_pct": 0.03, "k_short": 0.5, "mode": "trend_credit"}}))  # fmt: skip
    out.append((ct, "top_n on a stock condor" + note,
                {"options": stock_doc, "futures": f2doc, "symbols": ["S1"],
                 "params": {"n_before": 15, "slip_pct": 0.03, "top_n": 30}}))  # fmt: skip
    out.append((p + "debit_trades", "C1 (spread, 10), C2 (long, 10) and a five-session spread on S1's breakout "
                "(the composition: debit_trades(build_option_panel(options, futures, continuous(futures), symbols), horizon, structure))",
                {"options": stock_doc, "futures": f2doc, "symbols": ["S1"],
                 "runs": [{"structure": "spread", "horizon": 10}, {"structure": "long", "horizon": 10},
                          {"structure": "spread", "horizon": 5}]}))  # fmt: skip
    bs = p + "basis_study"
    out.append((bs, "M1's stock futures, spot printed", {"futures": fut_doc, "cycle_cost": 0.0045}))
    out.append((bs, "a zero cycle cost", {"futures": fut_doc, "cycle_cost": 0.0}))
    out.append((bs, "no spot printed: no qualifying row, so the quantile is null and _as_float raises "
                "(run_family checks for spot before it calls)",
                {"futures": rows_doc([dict(r, underlying=None) for r in m1], RESEARCH_FUTURES, date_key="date"), "cycle_cost": 0.0045}))  # fmt: skip
    return out


# ================================================================================================
# directional_retest
# ================================================================================================


def call_index_panel(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.directional_retest import index_panel

    panel = index_panel(frame(inputs["contracts"]), inputs["symbol"])
    return {
        "symbol": panel.symbol,
        "bars": panel.bars,
        "lot_by_date": panel.lot_by_date,
        "settle": [{"date": k[0], "expiry": k[1], "strike": k[2], "option_type": k[3], "settle": v}
                   for k, v in sorted(panel.settle.items(), key=lambda kv: _key(kv[0]))],
        "traded": [{"date": k[0], "expiry": k[1], "strike": k[2], "option_type": k[3], "traded": v}
                   for k, v in sorted(panel.traded.items(), key=lambda kv: _key(kv[0]))],
        "expiries_by_date": panel.expiries_by_date,
        "strikes_by_expiry": panel.strikes_by_expiry,
    }  # fmt: skip


def call_dr_monthly_expiries(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.directional_retest import monthly_expiries

    return monthly_expiries(tuple(days(inputs["listed"])))


def call_strike_step(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.directional_retest import strike_step

    return strike_step(tuple(D(k) for k in inputs["strikes"]), D(inputs["near"]))


def call_sessions_left(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.directional_retest import index_panel, sessions_left

    panel = index_panel(frame(inputs["contracts"]), inputs["symbol"])
    return {f"{c['entry']}|{c['expiry']}": sessions_left(panel, day(c["entry"]), day(c["expiry"]))
            for c in inputs["cases"]}  # fmt: skip


def call_dr_choose_expiry(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.directional_retest import choose_expiry, index_panel

    panel = index_panel(frame(inputs["contracts"]), inputs["symbol"])
    return choose_expiry(panel, tuple(days(inputs["listed"])), day(inputs["entry"]), int(inputs["min_sessions"]))


def call_run_symbol(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import F3Config
    from baskfy_core.fno.directional_retest import index_panel, run_symbol

    return run_symbol(index_panel(frame(inputs["contracts"]), inputs["symbol"]), cfg(F3Config, inputs["f3"]),
                      start=dayn(inputs["start"]), end=dayn(inputs["end"]))  # fmt: skip


def call_run_f3_retest(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.config import F3Config
    from baskfy_core.fno.directional_retest import run_f3_retest

    return run_f3_retest(frame(inputs["contracts"]), cfg(F3Config, inputs["f3"]),
                         start=dayn(inputs["start"]), end=dayn(inputs["end"]))  # fmt: skip


def call_dr_constants(_inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno import directional_retest as x

    return {"INDEX_COSTS": x.INDEX_COSTS, "FALLBACK_LOT": x.FALLBACK_LOT, "NOT_TESTED": x.NOT_TESTED}


#: The F3 proxy's market: the session calendar ends Fri 25 Sep 2026; NIFTY weeklies and monthlies.
DR_WEEKLY = [dt.date(2026, 9, 29)]
DR_MONTHLY = [dt.date(2026, 9, 29), dt.date(2026, 10, 27)]


#: The columns ``index_panel`` reads (``fo_contract_daily``'s, less the ones it never touches).
DR_COLUMNS = ["trade_date", "symbol", "instrument", "expiry", "strike", "option_type", "open", "high",
              "low", "close", "settle", "open_interest", "volume", "lot_size"]  # fmt: skip


def _dr_market(closes: list[int], *, symbol: str = "NIFTY", expiries: list[dt.date] | None = None,
               decay_from_index: int | None = None, kinds: tuple[str, ...] = ("PE",),
               strikes: range = range(24000, 25301, 50), options_from: int = 58) -> list[dict[str, Any]]:  # fmt: skip
    sessions: list[dt.date] = []
    cur = dt.date(2026, 9, 25)
    while len(sessions) < len(closes):
        if cur.weekday() < 5:  # noqa: PLR2004
            sessions.append(cur)
        cur -= dt.timedelta(days=1)
    sessions.sort()
    decay = None if decay_from_index is None else sessions[decay_from_index]
    return _index_contracts(symbol, [float(c) for c in closes], expiries=expiries or DR_WEEKLY,
                            strikes=strikes, kinds=kinds, options_from=options_from, decay_from=decay)  # fmt: skip


def _directional_retest_cases() -> list[tuple[str, str, dict[str, Any]]]:  # noqa: PLR0915 - a flat table
    out: list[tuple[str, str, dict[str, Any]]] = []
    p = PREFIX + "directional_retest."
    f3 = _f3_doc()
    out.append((p + "constants", "the proxy's costs, fallback lots and the rules it cannot test", {}))
    up = _uptrend(70)
    market = _dr_market(up)
    cdoc = rows_doc(market, DR_COLUMNS)
    fdoc = rows_doc(_dr_market(up, options_from=10**6), DR_COLUMNS)
    out.append((p + "index_panel", "the front-month future as the bar; settles, traded flags, listings",
                {"contracts": rows_doc(_dr_market(_uptrend(62), options_from=60, strikes=range(24900, 25101, 100)), DR_COLUMNS), "symbol": "NIFTY"}))  # fmt: skip
    out.append((p + "index_panel", "a symbol not in the rows: an empty panel", {"contracts": fdoc, "symbol": "BANKNIFTY"}))
    for listed, why in ((["2026-10-06", "2026-10-13", "2026-10-27", "2026-11-24", "2026-12-29"], "the last listed of each month"), ([], "none")):
        out.append((p + "monthly_expiries", why, {"listed": listed}))
    ks = p + "strike_step"
    for strikes, near, why in ((list(range(23000, 26000, 50)), "24500", "the modal gap 50"),
                               (["1"], "1", "one strike: null"),
                               ([24000, 24100, 24150, 24200, 24300, 24350], "24200", "a tie between 50 and 100: the finer"),
                               (list(range(20000, 30001, 100)) + [25025, 25050, 25075], "25050", "the window is the 21 strikes nearest")):  # fmt: skip
        out.append((ks, why, {"strikes": [str(k) for k in strikes], "near": near}))
    out.append((p + "sessions_left", "inside the panel, and weekdays beyond its last bar (composition with index_panel)",
                {"contracts": fdoc, "symbol": "NIFTY", "cases": [
                    {"entry": "2026-09-21", "expiry": "2026-09-25"}, {"entry": "2026-09-21", "expiry": "2026-09-29"},
                    {"entry": "2026-09-25", "expiry": "2026-10-06"}, {"entry": "2026-09-25", "expiry": "2026-09-25"}]}))  # fmt: skip
    ce = p + "choose_expiry"
    for listed, entry, mn, why in ((["2026-09-29", "2026-10-06"], "2026-09-24", 2, "the 29th has three sessions left"),
                                   (["2026-09-29", "2026-10-06"], "2026-09-28", 2, "one left: the next weekly"),
                                   (["2026-09-29"], "2026-09-28", 5, "nothing qualifies: null")):  # fmt: skip
        out.append((ce, why + " (composition with index_panel)",
                    {"contracts": fdoc, "symbol": "NIFTY", "listed": listed, "entry": entry, "min_sessions": mn}))  # fmt: skip
    rs = p + "run_symbol"
    rnote = " (the composition: run_symbol(index_panel(contracts, symbol), f3, start, end))"
    out.append((rs, "an uptrend signal enters next session and decays to the target" + rnote,
                {"contracts": rows_doc(_dr_market(up, decay_from_index=67), DR_COLUMNS), "symbol": "NIFTY",
                 "f3": f3, "start": None, "end": None}))  # fmt: skip
    out.append((rs, "held to the end of the sample: OPEN_AT_END, a next-session add" + rnote,
                {"contracts": cdoc, "symbol": "NIFTY", "f3": f3, "start": None, "end": None}))  # fmt: skip
    crash = up[:67] + [up[66] - 900, up[66] - 1000, up[66] - 950]
    out.append((rs, "a crash through support: LEVEL_BREAK at that session's settle" + rnote,
                {"contracts": rows_doc(_dr_market(crash), DR_COLUMNS), "symbol": "NIFTY", "f3": f3, "start": None, "end": None}))  # fmt: skip
    longer = _uptrend(73, dip_at=11)
    out.append((rs, "an expiry inside the sample: HARD_EXIT at the expiry settle (intrinsic)" + rnote,
                {"contracts": rows_doc(_index_contracts("NIFTY", [float(c) for c in longer],
                                                        expiries=[dt.date(2026, 9, 22), dt.date(2026, 9, 29)],
                                                        strikes=range(24000, 25301, 50), options_from=60), DR_COLUMNS),
                 "symbol": "NIFTY", "f3": f3, "start": None, "end": None}))  # fmt: skip
    out.append((rs, "BANKNIFTY trades the monthly with five sessions" + rnote,
                {"contracts": rows_doc(_dr_market(up, symbol="BANKNIFTY", expiries=DR_MONTHLY), DR_COLUMNS),
                 "symbol": "BANKNIFTY", "f3": f3, "start": None, "end": None}))  # fmt: skip
    out.append((rs, "no option rows: every signal skipped, counted by reason" + rnote,
                {"contracts": rows_doc(_dr_market(up, options_from=10**6), DR_COLUMNS), "symbol": "NIFTY",
                 "f3": f3, "start": None, "end": None}))  # fmt: skip
    out.append((rs, "a thin chain: every premium ₹1, thin_credit" + rnote,
                {"contracts": rows_doc(_dr_market(up, decay_from_index=0), DR_COLUMNS), "symbol": "NIFTY",
                 "f3": f3, "start": None, "end": None}))  # fmt: skip
    out.append((rs, "a sample window that ends before any entry" + rnote,
                {"contracts": cdoc, "symbol": "NIFTY", "f3": f3, "start": "2026-09-01", "end": "2026-09-15"}))  # fmt: skip
    out.append((rs, "too few bars for a level: no trade" + rnote,
                {"contracts": rows_doc(_dr_market(_uptrend(50), options_from=44, strikes=range(24300, 25201, 100)), DR_COLUMNS),
                 "symbol": "NIFTY", "f3": f3, "start": None, "end": None}))  # fmt: skip
    both = _dr_market(up) + _dr_market(up, symbol="BANKNIFTY", expiries=DR_MONTHLY)
    out.append((p + "run_f3_retest", "both underlyings, keyed by symbol",
                {"contracts": rows_doc(both, DR_COLUMNS), "f3": f3, "start": None, "end": None}))  # fmt: skip
    out.append((p + "run_f3_retest", "F3 on NIFTY only",
                {"contracts": cdoc, "f3": _f3_doc(underlyings=("NIFTY",)), "start": None, "end": None}))  # fmt: skip
    return out


# ================================================================================================
# retest
# ================================================================================================


def call_retest_constants(_inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno import retest as rt

    return {"FAMILIES": rt.FAMILIES, "FAMILY_KEYS": rt.FAMILY_KEYS, "TIER_2E_CAVEAT": rt.TIER_2E_CAVEAT,
            "TIER_2E_CAVEAT_MEASURED": rt.TIER_2E_CAVEAT_MEASURED, "TIER_2E_CAVEAT_F3": rt.TIER_2E_CAVEAT_F3,
            "MIN_MEASURED_SESSIONS": rt.MIN_MEASURED_SESSIONS, "MIN_MEASURED_NAMES": rt.MIN_MEASURED_NAMES,
            "RETEST_MONTHS": rt.RETEST_MONTHS, "DIRECTIONAL_EXPIRY_DAYS": rt.DIRECTIONAL_EXPIRY_DAYS,
            "DIRECTIONAL_STRIKE_BAND": rt.DIRECTIONAL_STRIKE_BAND, "SYMBOL_BATCH": rt.SYMBOL_BATCH}  # fmt: skip


def call_empty_options(_inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.retest import empty_options

    return empty_options()


def call_trim_index_contracts(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.retest import trim_index_contracts

    return trim_index_contracts(frame(inputs["contracts"]), inputs["symbol"])


def call_panels_from_contracts(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.retest import panels_from_contracts

    return panels_from_contracts(frame(inputs["contracts"]))


def _family(key: str) -> Any:
    from baskfy_core.fno.retest import FAMILIES

    return next(f for f in FAMILIES if f.key == key)


def call_family_slippage(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.retest import family_slippage

    measured = {k: (float(v[0]), int(v[1])) for k, v in inputs["measured"].items()}
    return {k: family_slippage(_family(k), measured) for k in inputs["families"]}


def call_retest_due(inputs: dict[str, Any]) -> Any:
    from baskfy_core.fno.retest import retest_due

    return [retest_due(day(c["today"]), dayn(c["last_run"])) for c in inputs["cases"]]


def call_run_family(inputs: dict[str, Any]) -> Any:
    """Each run is ``run_family(family, futures, slice_loader(options), measured, batch=...,
    load_index=...)`` over the one stored panel: the options loader slices the stored options rows;
    the index loader (when ``index_contracts`` is stored) hands those raw index rows back."""
    from baskfy_core.fno.retest import run_family, slice_loader

    futures = frame(inputs["futures"])
    options = frame(inputs["options"])
    index = None if inputs.get("index_contracts") is None else frame(inputs["index_contracts"])

    def load_index(symbol: str) -> Any:
        assert index is not None
        return index

    out = []
    for run in inputs["runs"]:
        measured = {k: (float(v[0]), int(v[1])) for k, v in run["measured"].items()}
        out.append(_capture(lambda run=run, measured=measured: run_family(
            _family(run["family"]), futures, slice_loader(options), measured, batch=int(run["batch"]),
            load_index=None if index is None else load_index)))  # fmt: skip
    return out


def _panel_day(d_: dt.date) -> list[dict[str, Any]]:
    near, far, week, third = dt.date(2026, 9, 29), dt.date(2026, 10, 27), dt.date(2026, 10, 6), dt.date(2026, 11, 24)
    spec = []
    for expiry in (near, far, third):
        spec.append(("FUTSTK", "ABC", expiry, 0.0, "XX", 100.0, 10, 10))
    for expiry in (near, far, third):
        spec.append(("OPTSTK", "ABC", expiry, 100.0, "CE", 5.0, 10, 0))
    spec.append(("OPTSTK", "ABC", near, 110.0, "CE", 1.0, 0, 0))
    spec.append(("FUTIDX", "NIFTY", near, 0.0, "XX", 23000.0, 10, 10))
    spec.append(("OPTIDX", "NIFTY", near, 23000.0, "PE", 90.0, 10, 5))
    spec.append(("OPTIDX", "NIFTY", week, 23000.0, "PE", 40.0, 10, 5))
    spec.append(("OPTIDX", "FINNIFTY", near, 23000.0, "PE", 40.0, 10, 5))
    return [{"trade_date": d_, "instrument": i, "symbol": s, "expiry": e, "strike": k, "option_type": t,
             "open": pr, "high": pr, "low": pr, "close": pr, "settle": pr, "underlying": None,
             "open_interest": oi, "oi_change": 0, "volume": v, "turnover": 1e6, "lot_size": 100}
            for i, s, e, k, t, pr, oi, v in spec]  # fmt: skip


def _retest_cases() -> list[tuple[str, str, dict[str, Any]]]:  # noqa: PLR0915 - a flat table
    out: list[tuple[str, str, dict[str, Any]]] = []
    p = PREFIX + "retest."
    out.append((p + "constants", "every family with its parameters, the caveats and the knobs", {}))
    out.append((p + "empty_options", "panel.py's option columns, no rows", {}))
    pdays = [dt.date(2026, 9, 24), dt.date(2026, 9, 25)]
    pdoc = rows_doc([r for d_ in pdays for r in _panel_day(d_)], CONTRACT_COLUMNS)
    out.append((p + "panels_from_contracts", "futures kept; options of the two nearest expiries with a future, OI or volume",
                {"contracts": pdoc}))  # fmt: skip
    out.append((p + "panels_from_contracts", "the day files' date column",
                {"contracts": rows_doc(_panel_day(pdays[0]), ["date" if c == "trade_date" else c for c in CONTRACT_COLUMNS], date_key="date")}))  # fmt: skip
    wide = _index_contracts("NIFTY", [float(c) for c in _uptrend(62)], expiries=[dt.date(2026, 9, 29), dt.date(2026, 12, 29)],
                            strikes=range(18000, 31001, 1000), kinds=("PE", "CE"), options_from=60)  # fmt: skip
    wide += [dict(r, symbol="BANKNIFTY") for r in wide[:3]]
    out.append((p + "trim_index_contracts", "far strikes and far expiries dropped, other symbols dropped",
                {"contracts": rows_doc(wide, CONTRACT_COLUMNS), "symbol": "NIFTY"}))  # fmt: skip
    out.append((p + "trim_index_contracts", "a symbol with futures only",
                {"contracts": rows_doc(wide, CONTRACT_COLUMNS), "symbol": "BANKNIFTY"}))  # fmt: skip
    fams = [f for f in ("B4", "B4_LOSS_CLOSE", "B1", "B2", "C1", "F2", "A0", "E", "F3N")]
    fsl = p + "family_slippage"
    stocks20 = {f"S{i:02d}": (0.01 + i / 1000, 25) for i in range(20)}
    for measured, why in (({}, "nothing measured: ASSUMED everywhere"),
                          ({"NIFTY": (0.004, 25), "BANKNIFTY": (0.006, 20)}, "both indices at 20+ sessions: the worse"),
                          ({"NIFTY": (0.004, 25), "BANKNIFTY": (0.006, 19)}, "one index short of 20 sessions"),
                          (stocks20, "twenty stock names: their median"),
                          ({k: v for k, v in list(stocks20.items())[:19]}, "nineteen names: ASSUMED"),
                          (stocks20 | {"S99": (0.5, 5), "NIFTY": (0.004, 25)}, "a name below 20 sessions and an index are not stock names")):  # fmt: skip
        out.append((fsl, why, {"families": fams, "measured": {k: list(v) for k, v in measured.items()}}))
    out.append((p + "retest_due", "January, April, July, October, once a month", {"cases": [
        {"today": "2026-10-01", "last_run": None}, {"today": "2026-10-29", "last_run": "2026-10-01"},
        {"today": "2026-10-29", "last_run": "2026-07-02"}, {"today": "2026-11-02", "last_run": None},
        {"today": "2027-01-04", "last_run": "2026-10-01"}, {"today": "2026-04-30", "last_run": "2025-04-03"}]}))  # fmt: skip
    rf = p + "run_family"
    m1 = _market_m1()
    m1doc = rows_doc(m1, RESEARCH_FUTURES, date_key="date")
    empty_opts = frame_doc(RESEARCH_OPTIONS, {c: DTYPES[c] for c in RESEARCH_OPTIONS}, [])
    plain = {"measured": {}, "batch": 20}
    out.append((rf, "every futures family, the basis study and a stock condor family with no chain stored, on M1",
                {"futures": m1doc, "options": empty_opts, "index_contracts": None,
                 "runs": [plain | {"family": f} for f in ("F2", "A0", "A1", "A1R", "A3", "A2", "A4", "E", "B1")]}))  # fmt: skip
    out.append((rf, "E before UDiFF (no spot printed): n 0, no basis",
                {"futures": rows_doc([dict(r, underlying=None) for r in m1], RESEARCH_FUTURES, date_key="date"),
                 "options": empty_opts, "index_contracts": None, "runs": [plain | {"family": "E"}]}))  # fmt: skip
    fut2, opts2 = _market_m2(two_cycles=True)
    out.append((rf, "M2 with two monthlies: B4 assumed and measured, B4's loss close, B1 in batches of one, B3, C1 and C2",
                {"futures": rows_doc(fut2, RESEARCH_FUTURES, date_key="date"),
                 "options": rows_doc(opts2, RESEARCH_OPTIONS, date_key="date"), "index_contracts": None,
                 "runs": [plain | {"family": "B4"},
                          {"family": "B4", "measured": {"NIFTY": [0.004, 25], "BANKNIFTY": [0.006, 25]}, "batch": 20},
                          plain | {"family": "B4_LOSS_CLOSE"}, {"family": "B1", "measured": {}, "batch": 1}, plain | {"family": "B3"},
                          plain | {"family": "C1"}, plain | {"family": "C2"}]}))  # fmt: skip
    idx = rows_doc(_dr_market(_uptrend(70)), ["date" if c == "trade_date" else c for c in DR_COLUMNS], date_key="date")
    small = rows_doc(m1[:40], RESEARCH_FUTURES, date_key="date")
    out.append((rf, "F3N through the index loader", {"futures": small, "options": empty_opts, "index_contracts": idx,
                                                     "runs": [plain | {"family": "F3N"}]}))  # fmt: skip
    out.append((rf, "F3N without the index loader: refused",
                {"futures": small, "options": empty_opts, "index_contracts": None, "runs": [plain | {"family": "F3N"}]}))  # fmt: skip
    out.append((rf, "an empty futures panel: refused",
                {"futures": rows_doc([], RESEARCH_FUTURES, date_key="date"), "options": empty_opts, "index_contracts": None,
                 "runs": [plain | {"family": "F2"}]}))  # fmt: skip
    return out


# ================================================================================================
# Registry, rendering, entry point
# ================================================================================================

CALLS: dict[str, Callable[[dict[str, Any]], Any]] = {
    PREFIX + "config.group_of": call_group_of,
    PREFIX + "config.f1_sleeve_for": call_f1_sleeve_for,
    PREFIX + "config.f3_sleeve_for": call_f3_sleeve_for,
    PREFIX + "config.FnoConfig.risk_per_trade_pct": call_risk_per_trade_pct,
    PREFIX + "config.ceiling_violations": call_ceiling_violations,
    PREFIX + "config.construct": call_config_construct,
    PREFIX + "gating.fno_gates": call_fno_gates,
    PREFIX + "gating.FnoFlags.execution_enabled": call_execution_enabled,
    PREFIX + "gating.constants": call_gating_constants,
    PREFIX + "condor.roles": call_condor_roles,
    PREFIX + "condor.one_sd": call_one_sd,
    PREFIX + "condor.select_strikes": call_select_strikes,
    PREFIX + "condor.credit": call_credit,
    PREFIX + "condor.max_loss_per_unit": call_condor_max_loss,
    PREFIX + "condor.price_structure": call_price_structure,
    PREFIX + "condor.liquidity_refusals": call_liquidity_refusals,
    PREFIX + "condor.LegQuote": call_leg_quote,
    PREFIX + "condor.cost_to_close": call_cost_to_close,
    PREFIX + "condor.levels": call_condor_levels,
    PREFIX + "costs.crossing_cost": call_crossing_cost,
    PREFIX + "costs.condor_fills": call_condor_fills,
    PREFIX + "costs.condor_round_trip": call_condor_round_trip,
    PREFIX + "costs.future_charges": call_future_charges,
    PREFIX + "costs.future_round_trip": call_future_round_trip,
    PREFIX + "costs.future_roll": call_future_roll,
    PREFIX + "sizing.risk_budget": call_risk_budget,
    PREFIX + "sizing.size": call_size,
    PREFIX + "sizing.margin_check": call_margin_check,
    PREFIX + "exits.f1_exit": call_f1_exit,
    PREFIX + "exits.stop_from_vol": call_stop_from_vol,
    PREFIX + "exits.f2_initial_stop": call_f2_initial_stop,
    PREFIX + "exits.f2_trail": call_f2_trail,
    PREFIX + "exits.gtt_trigger": call_gtt_trigger,
    PREFIX + "exits.f2_time_exit_session": call_f2_time_exit_session,
    PREFIX + "exits.f2_exit": call_f2_exit,
    PREFIX + "exits.f1_paused": call_f1_paused,
    PREFIX + "exits.f2_month_paused": call_f2_month_paused,
    PREFIX + "exits.book_paused": call_book_paused,
    PREFIX + "calendar.monthly_expiries": call_monthly_expiries,
    PREFIX + "calendar.monthly_expiry": call_monthly_expiry,
    PREFIX + "calendar.sessions_before": call_sessions_before,
    PREFIX + "calendar.sessions_between": call_sessions_between,
    PREFIX + "calendar.entry_session": call_entry_session,
    PREFIX + "calendar.hard_exit_session": call_hard_exit_session,
    PREFIX + "calendar.hard_exit_at": call_hard_exit_at,
    PREFIX + "calendar.is_late_exit": call_is_late_exit,
    PREFIX + "calendar.next_entry": call_next_entry,
    PREFIX + "calendar.is_entry_day": call_is_entry_day,
    PREFIX + "calendar.plan_expires_at": call_plan_expires_at,
    PREFIX + "calendar.in_entry_window": call_in_entry_window,
    PREFIX + "calendar.held_expiry": call_held_expiry,
    PREFIX + "calendar.f2_contract_expiry": call_f2_contract_expiry,
    PREFIX + "calendar.roll_session": call_roll_session,
    PREFIX + "covered.uncovered": call_uncovered,
    PREFIX + "covered.book_after": call_book_after,
    PREFIX + "covered.net_of_plan": call_net_of_plan,
    PREFIX + "covered.assess_step": call_assess_step,
    PREFIX + "journal.r_multiple": call_r_multiple,
    PREFIX + "journal.option_costs": call_option_costs,
    PREFIX + "journal.future_costs": call_future_costs,
    PREFIX + "journal.journal_figures": call_journal_figures,
    PREFIX + "journal.one_pool": call_one_pool,
    PREFIX + "ledger.month_bounds": call_month_bounds,
    PREFIX + "ledger.figures": call_figures,
    PREFIX + "ledger.build_ledger": call_build_ledger,
    PREFIX + "ledger.mode_tag": call_mode_tag,
    PREFIX + "ledger.book_pause_limit": call_book_pause_limit,
    PREFIX + "ledger.evaluate_pauses": call_evaluate_pauses,
    PREFIX + "ledger.pauses_for": call_pauses_for,
    PREFIX + "ledger.column_pause_applies": call_column_pause_applies,
    PREFIX + "checklist.uncovered_steps": call_uncovered_steps,
    PREFIX + "checklist.into_expiry": call_into_expiry,
    PREFIX + "checklist.journal_gap": call_journal_gap,
    PREFIX + "checklist.mark_gaps": call_mark_gaps,
    PREFIX + "checklist.missed_cycles": call_missed_cycles,
    PREFIX + "checklist.paper_checklist": call_paper_checklist,
    PREFIX + "monitor.reprice_condor": call_reprice_condor,
    PREFIX + "monitor.f1_close_cost": call_f1_close_cost,
    PREFIX + "monitor.f1_pnl_inr": call_f1_pnl_inr,
    PREFIX + "monitor.reprice_future": call_reprice_future,
    PREFIX + "monitor.f2_pnl_inr": call_f2_pnl_inr,
    PREFIX + "monitor.f2_trail_step": call_f2_trail_step,
    PREFIX + "monitor.f2_action": call_f2_action,
    PREFIX + "directional.seventy_five_minute_bars": call_seventy_five,
    PREFIX + "directional.pivots": call_pivots,
    PREFIX + "directional.bars_needed": call_bars_needed,
    PREFIX + "directional.levels": call_levels,
    PREFIX + "directional.daily_direction": call_daily_direction,
    PREFIX + "directional.confirm_75": call_confirm_75,
    PREFIX + "directional.direction_for": call_direction_for,
    PREFIX + "directional.key_level": call_key_level,
    PREFIX + "directional.intraday_aligned": call_intraday_aligned,
    PREFIX + "directional.level_broken": call_level_broken,
    PREFIX + "directional.spread_strikes": call_spread_strikes,
    PREFIX + "directional.choose_expiry": call_choose_expiry,
    PREFIX + "directional.spread_arith": call_spread_arith,
    PREFIX + "directional.size_entry": call_size_entry,
    PREFIX + "directional.propose_spread": call_propose_spread,
    PREFIX + "directional.add_decision": call_add_decision,
    PREFIX + "directional.f3_marks": call_f3_marks,
    PREFIX + "directional.f3_exit": call_f3_exit,
    PREFIX + "series.continuous_futures": call_continuous_futures,
    PREFIX + "underlying.derive_underlying": call_derive_underlying,
    PREFIX + "vol.iv_expiry": call_iv_expiry,
    PREFIX + "vol.atm_iv": call_atm_iv,
    PREFIX + "vol.iv_atm_frame": call_iv_atm_frame,
    PREFIX + "scan.propose_condor": call_propose_condor,
    PREFIX + "scan.f2_signals": call_f2_signals,
    PREFIX + "scan.propose_future": call_propose_future,
    PREFIX + "scan.allocate_capacity": call_allocate_capacity,
    PREFIX + "research.constants": call_research_constants,
    PREFIX + "research.continuous": call_research_continuous,
    PREFIX + "research.b76": call_b76,
    PREFIX + "research.implied": call_implied,
    PREFIX + "research.leg_costs": call_leg_costs,
    PREFIX + "research.summarise": call_summarise,
    PREFIX + "research.futures_panel": call_futures_panel,
    PREFIX + "research.liquid_base": call_liquid_base,
    PREFIX + "research.futures_signals": call_futures_signals,
    PREFIX + "research.simulate_futures": call_simulate_futures,
    PREFIX + "research.f2_trades": call_f2_trades,
    PREFIX + "research.liquid_symbols": call_liquid_symbols,
    PREFIX + "research.build_option_panel": call_build_option_panel,
    PREFIX + "research.condor_trades": call_condor_trades,
    PREFIX + "research.debit_trades": call_debit_trades,
    PREFIX + "research.basis_study": call_basis_study,
    PREFIX + "directional_retest.constants": call_dr_constants,
    PREFIX + "directional_retest.index_panel": call_index_panel,
    PREFIX + "directional_retest.monthly_expiries": call_dr_monthly_expiries,
    PREFIX + "directional_retest.strike_step": call_strike_step,
    PREFIX + "directional_retest.sessions_left": call_sessions_left,
    PREFIX + "directional_retest.choose_expiry": call_dr_choose_expiry,
    PREFIX + "directional_retest.run_symbol": call_run_symbol,
    PREFIX + "directional_retest.run_f3_retest": call_run_f3_retest,
    PREFIX + "retest.constants": call_retest_constants,
    PREFIX + "retest.empty_options": call_empty_options,
    PREFIX + "retest.trim_index_contracts": call_trim_index_contracts,
    PREFIX + "retest.panels_from_contracts": call_panels_from_contracts,
    PREFIX + "retest.family_slippage": call_family_slippage,
    PREFIX + "retest.retest_due": call_retest_due,
    PREFIX + "retest.run_family": call_run_family,
}


def cases() -> list[tuple[str, str, str, dict[str, Any]]]:
    """(fn, case file stem, why, JSON inputs) for every golden, in a fixed order."""
    out: list[tuple[str, str, str, dict[str, Any]]] = []
    counts: dict[str, int] = {}
    for builder in (_config_cases, _gating_cases, _condor_cases, _costs_cases, _sizing_cases,
                    _exits_cases, _calendar_cases, _covered_cases, _journal_cases, _ledger_cases,
                    _checklist_cases, _monitor_cases, _directional_cases, _series_cases, _scan_cases,
                    _research_cases, _directional_retest_cases, _retest_cases):  # fmt: skip
        for fn, why, inputs in builder():
            if fn not in CALLS:
                raise KeyError(f"golden_fno: no call registered for {fn}")
            counts[fn] = counts.get(fn, 0) + 1
            out.append((fn, f"{fn.removeprefix(PREFIX)}.case_{counts[fn]:03d}", why, inputs))
    return out


#: ``research.summarise`` reads ``std()`` of one trade, which polars gives as null, and
#: ``_as_float(None)`` raises; ``directional_retest._summarise`` guards it, ``retest.run_family``
#: does not. Recorded as the behaviour the port must match, and flagged.
ONE_TRADE_SUMMARY_BUG = (
    "research.summarise raises TypeError on a one-trade frame (std of one value is null); "
    "retest.run_family inherits it for a family with exactly one trade"
)


def render_case(fn: str, why: str, inputs: dict[str, Any]) -> str:
    golden = _golden()
    # Round-trip the inputs through their JSON text first, so the stored output is the output of
    # exactly what the file says the inputs are.
    stored = json.loads(golden.render(golden._enc(inputs)))
    output = _out(CALLS[fn](stored))
    one_trade = fn.endswith((".summarise", ".run_family"))
    bug = (ONE_TRADE_SUMMARY_BUG
           if one_trade and "expected a number, got NoneType" in json.dumps(golden._enc(output)) else None)  # fmt: skip
    doc = golden.document(fn=fn, inputs=stored, output=output, known_bug=bug,
                          notes=f"{why}. {NOTES}", stable=True)  # fmt: skip
    return golden.render(doc)


def dump_fno() -> list[Path]:
    golden = _golden()
    directory = golden.GOLDEN_DIR / LANE
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for fn, stem, why, inputs in cases():
        path = directory / f"{stem}.json"
        path.write_text(render_case(fn, why, inputs), encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    paths = dump_fno()
    print(f"{len(paths)} fno goldens written under go/testdata/golden/{LANE}/")
