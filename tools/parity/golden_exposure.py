"""Parity goldens for baskfy_core.exposure (regime + allocation). Writes go/testdata/golden/L1/exposure.

    cd decile-blueprint
    PYTHONPATH=..:packages/core/src .venv/bin/python ../tools/parity/golden_exposure.py
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import importlib.util
import json
import random
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

from baskfy_core.exposure import allocation as A  # noqa: E402
from baskfy_core.exposure import regime as R  # noqa: E402


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


LANE = "L1/exposure"
NOTES = (
    "Inputs are JSON; each function is called from them after a JSON round trip. Dates are ISO "
    "days, enums their values, IndexSignals/BookWeights/RegimeState their as_dict(), Allocation "
    "and Position/Candidate/OrderLine dataclasses.asdict, tuples lists. A raise is "
    "{raises: {type, message}}. Regenerate with `PYTHONPATH=..:packages/core/src "
    ".venv/bin/python ../tools/parity/golden_exposure.py` from decile-blueprint."
)
D = dt.date


# ------------------------------------------------------------------------------------------
# encoders / decoders
# ------------------------------------------------------------------------------------------
def cfg_doc(cfg: R.RegimeConfig) -> dict[str, Any]:
    d = {f.name: getattr(cfg, f.name) for f in dataclasses.fields(cfg)}
    d["structural_indices"] = dict(cfg.structural_indices)
    d["index_aliases"] = {k: list(v) for k, v in cfg.index_aliases.items()}
    d["ma_lengths"] = list(cfg.ma_lengths)
    d["default_book_weights"] = dict(cfg.default_book_weights)
    d["tier_exposure_pct"] = dict(cfg.tier_exposure_pct)
    d["bootstrap_policy"] = cfg.bootstrap_policy.value
    d["mode"] = cfg.mode.value
    return d


def cfg_from(doc: dict[str, Any]) -> R.RegimeConfig:
    d = dict(doc)
    d["index_aliases"] = {k: tuple(v) for k, v in d["index_aliases"].items()}
    d["ma_lengths"] = tuple(d["ma_lengths"])
    d["bootstrap_policy"] = R.BootstrapPolicy(d["bootstrap_policy"])
    d["mode"] = R.RegimeMode(d["mode"])
    return R.RegimeConfig(**d)


def _od(v: str | None) -> dt.date | None:
    return None if v is None else D.fromisoformat(v)


def sig_from(doc: dict[str, Any] | None) -> R.IndexSignals | None:
    if doc is None:
        return None
    sigs = {
        int(k): R.MaSignal(
            ma_length=int(v["ma_length"]), close=v["close"], ma=v["ma"],
            distance_pct=v["distance_pct"], state=R.SignalState(v["state"]),
            confirming_closes=int(v["confirming_closes"]))
        for k, v in doc["signals"].items()
    }
    return R.IndexSignals(index_name=doc["index_name"], as_of_date=_od(doc["as_of_date"]),
                          close=doc["close"], signals=sigs, is_stale=doc["is_stale"],
                          has_data=doc["has_data"], instrument_token=doc["instrument_token"])


def book_doc(b: R.BookWeights) -> dict[str, Any]:
    return b.as_dict()


def book_from(doc: dict[str, Any] | None) -> R.BookWeights | None:
    if doc is None:
        return None
    return R.BookWeights(dict(doc["weights"]), R.WeightSource(doc["source"]),
                         doc["coverage_pct"], tuple(doc["reason_codes"]))


def breadth_from(doc: dict[str, Any] | None) -> R.BreadthReading | None:
    if doc is None:
        return None
    d = dict(doc)
    d["as_of_date"] = D.fromisoformat(d["as_of_date"])
    return R.BreadthReading(**d)


def exposure_doc(e: R.ExposureSnapshot) -> dict[str, Any]:
    return {"actual_equity_pct": e.actual_equity_pct, "pending_buy_pct": e.pending_buy_pct,
            "pending_sell_pct": e.pending_sell_pct, "execution_status": e.execution_status.value}


def exposure_from(doc: dict[str, Any]) -> R.ExposureSnapshot:
    return R.ExposureSnapshot(doc["actual_equity_pct"], doc["pending_buy_pct"],
                              doc["pending_sell_pct"], R.ExecutionStatus(doc["execution_status"]))


def prev_doc(p: R.PreviousRegime | None) -> dict[str, Any] | None:
    if p is None:
        return None
    return {"policy_tier": p.policy_tier.value,
            "scheduled_week_end": p.scheduled_week_end.isoformat(),
            "last_transition_date": p.last_transition_date.isoformat()
            if p.last_transition_date else None,
            "recovery_confirmations": p.recovery_confirmations,
            "book_weights": book_doc(p.book_weights) if p.book_weights else None}


def prev_from(doc: dict[str, Any] | None) -> R.PreviousRegime | None:
    if doc is None:
        return None
    return R.PreviousRegime(R.RegimeTier(doc["policy_tier"]),
                            D.fromisoformat(doc["scheduled_week_end"]),
                            _od(doc["last_transition_date"]), doc["recovery_confirmations"],
                            book_from(doc["book_weights"]))


def candle_doc(c: R.Candle) -> dict[str, Any]:
    return {"date": c.date.isoformat(), "open": c.open, "high": c.high, "low": c.low,
            "close": c.close, "is_final": c.is_final}


def candle_from(d: dict[str, Any]) -> R.Candle:
    return R.Candle(D.fromisoformat(d["date"]), d["open"], d["high"], d["low"], d["close"],
                    d["is_final"])


def health_from(d: dict[str, Any]) -> dict[int, float]:
    return {int(k): v for k, v in d.items()}


def cand_from(d: dict[str, Any]) -> A.Candidate:
    return A.Candidate(**d)


def pos_from(d: dict[str, Any]) -> A.Position:
    return A.Position(**d)


def alloc_from(d: dict[str, Any]) -> A.Allocation:
    return A.Allocation(dict(d["weights_sleeve"]), dict(d["weights_nav"]),
                        frozenset(d["half_sized"]), d["sleeve_pct"], dict(d["cluster_weights"]),
                        tuple(d["notes"]))


# ------------------------------------------------------------------------------------------
# fixtures
# ------------------------------------------------------------------------------------------
ABOVE, BELOW, UNKNOWN = R.SignalState.ABOVE, R.SignalState.BELOW, R.SignalState.UNKNOWN


def forced(name: str, states: dict[int, R.SignalState], *, close: float = 100.0,
           mas: dict[int, float] | None = None, stale: bool = False,
           has_data: bool = True) -> R.IndexSignals:
    sig = {}
    for length, state in states.items():
        ma = (mas or {}).get(length, close)
        sig[length] = R.MaSignal(length, close, ma, (close / ma - 1) * 100 if ma else None,
                                 state, 3)
    return R.IndexSignals(name, D(2026, 6, 1), close, sig, stale, has_data)


def candles(closes: list[float], start: dt.date = D(2026, 1, 1),
            final: bool = True) -> list[R.Candle]:
    return [R.Candle(start + dt.timedelta(days=i), c, round(c * 1.01, 4), round(c * 0.99, 4),
                     float(c), final) for i, c in enumerate(closes)]


def breadth(pct: float = 60.0, *, as_of: dt.date = D(2026, 6, 1),
            coverage: float = 100.0) -> R.BreadthReading:
    return R.BreadthReading(as_of, pct, 500, 500, coverage, "nifty500", "hash1", "run1", "v1")


def walk(rng: random.Random, n: int, start: float, drift: float, vol: float) -> list[float]:
    out, x = [], start
    for _ in range(n):
        x = max(1.0, x * (1 + drift + rng.gauss(0, vol)))
        out.append(round(x, 2))
    return out


# ------------------------------------------------------------------------------------------
# dumping
# ------------------------------------------------------------------------------------------
COUNTS: dict[str, int] = {}
WRITTEN: list[Path] = []


def _json(x: Any) -> Any:
    return json.loads(json.dumps(_golden()._enc(x)))


def case(module: str, fn: str, inputs: dict[str, Any], call: Callable[[dict[str, Any]], Any],
         note: str) -> None:
    inputs = _json(inputs)
    try:
        out = call(inputs)
    except (R.ConfigError, A.AllocationError, KeyError, ZeroDivisionError) as e:
        out = {"raises": {"type": type(e).__name__, "message": str(e)}}
    key = f"{module}.{fn}"
    COUNTS[key] = COUNTS.get(key, 0) + 1
    name = f"{LANE}/{key}.case_{COUNTS[key]:03d}"
    WRITTEN.append(_golden().dump(name, fn=f"baskfy_core.exposure.{key}", inputs=inputs,
                                  output=out, notes=f"{note}. {NOTES}", stable=True))


def eval_call(i: dict[str, Any]) -> Any:
    return R.evaluate(
        as_of_date=D.fromisoformat(i["as_of_date"]),
        scheduled_week_end=D.fromisoformat(i["scheduled_week_end"]),
        signal_session_date=D.fromisoformat(i["signal_session_date"]),
        index_signals={k: sig_from(v) for k, v in i["index_signals"].items()},
        sentinel=sig_from(i["sentinel"]), breadth=breadth_from(i["breadth"]),
        book=book_from(i["book"]), exposure=exposure_from(i["exposure"]),
        cfg=cfg_from(i["cfg"]), previous=prev_from(i["previous"])).as_dict()


def eval_case(note: str, cfg: R.RegimeConfig, *, indices: dict[str, R.IndexSignals],
              sentinel: R.IndexSignals | None, brd: R.BreadthReading | None,
              bk: R.BookWeights, exposure: R.ExposureSnapshot,
              previous: R.PreviousRegime | None, as_of: dt.date = D(2026, 6, 5),
              week_end: dt.date = D(2026, 6, 5), session: dt.date = D(2026, 6, 5)) -> None:
    case("regime", "evaluate", {
        "as_of_date": as_of, "scheduled_week_end": week_end, "signal_session_date": session,
        "index_signals": {k: v.as_dict() for k, v in indices.items()},
        "sentinel": sentinel.as_dict() if sentinel else None,
        "breadth": dataclasses.asdict(brd) if brd else None, "book": book_doc(bk),
        "exposure": exposure_doc(exposure), "cfg": cfg_doc(cfg), "previous": prev_doc(previous),
    }, eval_call, note)


def dump_regime() -> None:  # noqa: PLR0915
    cfg = R.RegimeConfig()
    rng = random.Random(20261030)

    # moving averages + hysteresis
    series = [
        [100.0] * 60 + [80.0] * 30 + [120.0, 121.0],
        [100.0] * 60 + [80.0] * 30 + [120.0, 121.0, 122.0],
        [10.0] * 19 + [10.0, 40.0, 40.0, 12.0, 12.0],
        [100.0] * 60 + [130.0] * 5,
        [100.0] * 25,
        walk(rng, 120, 100.0, 0.001, 0.02),
        walk(rng, 80, 100.0, -0.002, 0.03),
    ]
    for s in series:
        for length in (3, 20):
            case("regime", "moving_average", {"closes": s, "length": length},
                 lambda i: R.moving_average(i["closes"], i["length"]), "simple MA")
        for cd, buf in ((3, 0.015), (2, 0.015), (1, 0.0), (5, 0.03)):
            case("regime", "confirmed_state_series",
                 {"closes": s, "length": 20, "buffer": buf, "confirm_days": cd},
                 lambda i: [[st.value, run] for st, run in R.confirmed_state_series(
                     i["closes"], R.moving_average(i["closes"], i["length"]),
                     buffer=i["buffer"], confirm_days=i["confirm_days"])],
                 "states over moving_average(closes, length)")
    case("regime", "moving_average", {"closes": [1.0, 2.0], "length": 0},
         lambda i: R.moving_average(i["closes"], i["length"]), "zero length raises")

    # build_index_signals
    def bis(i: dict[str, Any]) -> Any:
        return R.build_index_signals(i["index_name"], [candle_from(c) for c in i["candles"]],
                                     cfg_from(i["cfg"]), as_of=_od(i["as_of"]),
                                     instrument_token=i["instrument_token"]).as_dict()

    bis_inputs = [
        (candles([100.0 + k for k in range(120)]), None, None),
        (candles([100.0] * 30) + [R.Candle(D(2026, 3, 1), 1.0, 1.0, 1.0, 9999.0, False)],
         None, 256265),
        (candles([100.0] * 5, final=False), None, None),
        (candles([100.0] * 30), D(2026, 3, 1), None),
        (candles([100.0 + (k % 7) for k in range(260)]), None, None),
        (list(reversed(candles(walk(rng, 260, 200.0, 0.0005, 0.015)))), D(2026, 9, 18), 1),
        (candles(walk(rng, 230, 50.0, -0.001, 0.02)), None, None),
        (candles([0.0] * 30), None, None),
    ]
    for cs, as_of, tok in bis_inputs:
        for c in (cfg, dataclasses.replace(cfg, buffer_bps=0, confirm_days=1)):
            case("regime", "build_index_signals",
                 {"index_name": "NIFTY 50", "candles": [candle_doc(x) for x in cs],
                  "cfg": cfg_doc(c), "as_of": as_of, "instrument_token": tok}, bis,
                 "as_dict() of the signals")

    # book weights
    def rbw(i: dict[str, Any]) -> Any:
        t = i["full_risk_target"]
        return R.resolve_book_weights(
            cfg_from(i["cfg"]),
            full_risk_target=None if t is None else [R.TargetPosition(**p) for p in t],
            last_r1_snapshot=book_from(i["last_r1_snapshot"])).as_dict()

    snap = R.BookWeights({"smallcap": 0.4, "midcap": 0.4, "largecap": 0.2},
                         R.WeightSource.FULL_RISK_TARGET, 100.0)
    tp = R.TargetPosition
    for target, last in (
        ([tp("A", "smallcap", 600_000.0), tp("B", "midcap", 300_000.0),
          tp("C", "largecap", 100_000.0)], None),
        ([tp("A", "smallcap", 900_000.0), tp("B", "largecap", 50_000.0),
          tp("C", "largecap", 50_000.0)], None),
        (None, snap), (None, None), ([], None),
        ([tp("A", "smallcap", 100_000.0), tp("B", None, 900_000.0)], None),
        ([tp("A", "smallcap", 100_000.0), tp("B", None, 900_000.0)], snap),
        ([tp("A", "smallcap", 850_000.0), tp("B", "midcap", 100_000.0),
          tp("C", "mystery", 50_000.0)], None),
        ([tp("A", "smallcap", -5.0), tp("B", "midcap", 0.0)], None),
        ([tp("A", "smallcap", 333_333.33), tp("B", "midcap", 123_456.78),
          tp("C", "largecap", 98_765.43), tp("D", None, 1_000.0)], None),
        (None, R.BookWeights({"smallcap": 0.0, "midcap": -1.0}, R.WeightSource.FULL_RISK_TARGET,
                             50.0)),
    ):
        case("regime", "resolve_book_weights",
             {"cfg": cfg_doc(cfg), "full_risk_target":
              None if target is None else [dataclasses.asdict(p) for p in target],
              "last_r1_snapshot": book_doc(last) if last else None}, rbw, "as_dict()")

    # health
    bk = R.BookWeights({"smallcap": 0.5, "midcap": 0.3, "largecap": 0.2},
                       R.WeightSource.CONFIG_DEFAULT, 100.0)
    names = {"largecap": "NIFTY 50", "midcap": "NIFTY MIDCAP 150",
             "smallcap": "NIFTY SMLCAP 250"}
    states = [ABOVE, BELOW, UNKNOWN]

    def rand_indices(r: random.Random) -> dict[str, R.IndexSignals]:
        out = {}
        for cat, n in names.items():
            if r.random() < 0.08:
                continue
            close = round(r.uniform(50, 150), 2)
            mas = {m: round(r.uniform(50, 150), 2) for m in (20, 50, 200)}
            if r.random() < 0.25:
                mas = {20: close * 0.95, 50: close, 200: close * 1.05}
                mas = {k: round(v, 2) for k, v in mas.items()}
            st = {m: r.choice(states) for m in (20, 50, 200)}
            if r.random() < 0.15:
                st[20] = BELOW
                mas = {20: round(close * 1.02, 2), 50: round(close * 1.05, 2),
                       200: round(close * 1.1, 2)}
            out[n] = forced(n, st, close=close, mas=mas, stale=r.random() < 0.08,
                            has_data=r.random() > 0.05)
        return out

    def hcall(fn: Callable[..., float]) -> Callable[[dict[str, Any]], Any]:
        return lambda i: fn(book_from(i["book"]),
                            {k: sig_from(v) for k, v in i["index_signals"].items()},
                            cfg_from(i["cfg"]), i["ma_length"])

    for _ in range(12):
        idx = rand_indices(rng)
        b = R.BookWeights({k: round(rng.random(), 3) for k in names},
                          R.WeightSource.FULL_RISK_TARGET, 100.0)
        ins = {"book": book_doc(b), "index_signals": {k: v.as_dict() for k, v in idx.items()},
               "cfg": cfg_doc(cfg)}
        for m in (20, 50, 200):
            case("regime", "weighted_health", {**ins, "ma_length": m}, hcall(R.weighted_health),
                 "weighted health")
            case("regime", "optimistic_health", {**ins, "ma_length": m},
                 hcall(R.optimistic_health), "optimistic health")
        case("regime", "bearish_stack_weight", ins,
             lambda i: R.bearish_stack_weight(
                 book_from(i["book"]), {k: sig_from(v) for k, v in i["index_signals"].items()},
                 cfg_from(i["cfg"])), "bearish stack weight")

    # classifier pieces
    def sent(s50: R.SignalState = ABOVE, s200: R.SignalState = ABOVE, **kw: Any) -> R.IndexSignals:
        return forced("NIFTY 500 MOMENTUM 50", {20: ABOVE, 50: s50, 200: s200}, **kw)

    def cc(i: dict[str, Any]) -> Any:
        t, codes = R.classify_candidate(
            health=health_from(i["health"]), stack_weight=i["stack_weight"],
            breadth_pct=i["breadth_pct"], breadth_usable=i["breadth_usable"],
            sentinel=sig_from(i["sentinel"]), cfg=cfg_from(i["cfg"]))
        return [t.value, codes]

    grid = [
        ({20: 1.0, 50: 0.8, 200: 0.8}, 0.0, 55.0, True, sent()),
        ({20: 1.0, 50: 0.7999, 200: 0.8}, 0.0, 55.0, True, sent()),
        ({20: 1.0, 50: 0.8, 200: 0.7999}, 0.0, 55.0, True, sent()),
        ({20: 1.0, 50: 0.8, 200: 0.8}, 0.0, 54.99, True, sent()),
        ({20: 1.0, 50: 0.6, 200: 0.5}, 0.0, 60.0, True, sent()),
        ({20: 1.0, 50: 0.6, 200: 0.5001}, 0.0, 60.0, True, sent()),
        ({20: 1.0, 50: 0.6, 200: 0.6}, 0.0, 39.99, True, sent()),
        ({20: 1.0, 50: 0.6, 200: 0.6}, 0.0, 40.0, True, sent()),
        ({20: 0.0, 50: 0.0, 200: 0.4}, 0.6, 29.99, True, sent()),
        ({20: 0.0, 50: 0.0, 200: 0.4}, 0.5999, 29.99, True, sent()),
        ({20: 0.0, 50: 0.0, 200: 0.4}, 0.6, 30.0, True, sent()),
        ({20: 0.0, 50: 0.0, 200: 0.29}, 0.0, 29.0, True, sent(s200=BELOW)),
        ({20: 0.7, 50: 0.7, 200: 0.7}, 0.0, 50.0, True, sent()),
        ({20: 1.0, 50: 1.0, 200: 1.0}, 0.0, 90.0, False, sent()),
        ({20: 0.0, 50: 0.0, 200: 0.6}, 1.0, 5.0, False, sent()),
        ({20: 1.0, 50: 1.0, 200: 1.0}, 0.0, None, False, None),
        ({20: 1.0, 50: 1.0, 200: 1.0}, 0.0, 90.0, True, sent(s50=UNKNOWN)),
        ({}, 0.0, 70.0, True, sent()),
    ]
    for hh, sw, bp, bu, sn in grid:
        for c in (cfg, dataclasses.replace(cfg, breadth_required=False)):
            case("regime", "classify_candidate",
                 {"health": hh, "stack_weight": sw, "breadth_pct": bp, "breadth_usable": bu,
                  "sentinel": sn.as_dict() if sn else None, "cfg": cfg_doc(c)}, cc,
                 "[tier, codes]")

    for tier in R.RegimeTier:
        for sn in (None, sent(), sent(s50=BELOW), sent(s200=BELOW), sent(BELOW, BELOW),
                   sent(has_data=False), sent(UNKNOWN, UNKNOWN)):
            case("regime", "apply_sentinel_floor",
                 {"tier": tier.value, "sentinel": sn.as_dict() if sn else None},
                 lambda i: [x if isinstance(x, list) else x.value for x in R.apply_sentinel_floor(
                     R.RegimeTier(i["tier"]), sig_from(i["sentinel"]))], "[tier, codes]")

    for tier in R.RegimeTier:
        for hh, bp, bu, conf in (({20: 0.8}, 60.0, True, 2), ({20: 1.0}, 99.0, True, 99),
                                 ({20: 0.8}, 60.0, True, 1), ({20: 0.2}, 60.0, True, 5),
                                 ({20: 0.9}, 90.0, False, 5), ({20: 0.9}, None, True, 5),
                                 ({20: 0.9}, 54.99, True, 5), ({}, 70.0, True, 5)):
            case("regime", "apply_recovery_override",
                 {"tier": tier.value, "health": hh, "breadth_pct": bp, "breadth_usable": bu,
                  "confirmations": conf, "cfg": cfg_doc(cfg)},
                 lambda i: (lambda t, a, c: [t.value, a, c])(*R.apply_recovery_override(
                     R.RegimeTier(i["tier"]), health=health_from(i["health"]),
                     breadth_pct=i["breadth_pct"], breadth_usable=i["breadth_usable"],
                     confirmations=i["confirmations"], cfg=cfg_from(i["cfg"]))),
                 "[tier, active, codes]")

    for prev in [None, *R.RegimeTier]:
        for cand in R.RegimeTier:
            case("regime", "apply_transition",
                 {"previous": prev.value if prev else None, "candidate": cand.value},
                 lambda i: (lambda t, lim, c: [t.value, lim, c])(*R.apply_transition(
                     R.RegimeTier(i["previous"]) if i["previous"] else None,
                     R.RegimeTier(i["candidate"]), cfg)), "[tier, limited, codes]")

    for day in range(7):
        for wd in (0, 4, 6):
            c = dataclasses.replace(cfg, weekly_evaluation_weekday=wd)
            case("regime", "next_evaluation_date",
                 {"after": D(2026, 6, 1) + dt.timedelta(days=day), "cfg": cfg_doc(c)},
                 lambda i: R.next_evaluation_date(D.fromisoformat(i["after"]),
                                                  cfg_from(i["cfg"])).isoformat(), "date")

    cfgs = [
        cfg, dataclasses.replace(cfg, confirm_days=5), dataclasses.replace(cfg, r1_breadth_min=60.0),
        dataclasses.replace(cfg, mode=R.RegimeMode.ENFORCE),
        dataclasses.replace(cfg, tier_exposure_pct={"R1": 100.0, "R2": 70.0, "R3": 40.0,
                                                    "R4": 0.0}),
        dataclasses.replace(cfg, bootstrap_policy=R.BootstrapPolicy.ADOPT_CANDIDATE,
                            breadth_required=False, min_breadth_coverage_pct=85.5),
        dataclasses.replace(cfg, momentum_sentinel="NIFTY ALPHA 50 é",
                            structural_indices={"largecap": "NIFTY 100", "smallcap": "S\"Q\\"}),
    ]
    for c in cfgs:
        case("regime", "RegimeConfig.config_hash", {"cfg": cfg_doc(c)},
             lambda i: cfg_from(i["cfg"]).config_hash(), "16 hex")
        for we, ss in ((D(2026, 6, 5), D(2026, 6, 5)), (D(2026, 6, 12), D(2026, 6, 11))):
            case("regime", "make_evaluation_id",
                 {"scheduled_week_end": we, "signal_session_date": ss, "cfg": cfg_doc(c)},
                 lambda i: R.make_evaluation_id(D.fromisoformat(i["scheduled_week_end"]),
                                                D.fromisoformat(i["signal_session_date"]),
                                                cfg_from(i["cfg"])), "24 hex")

    bad = [
        cfg, dataclasses.replace(cfg, ma_lengths=(50, 20, 200)),
        dataclasses.replace(cfg, ma_lengths=(20, 20, 200)), dataclasses.replace(cfg, ma_lengths=()),
        dataclasses.replace(cfg, ma_lengths=(20,)),
        dataclasses.replace(cfg, tier_exposure_pct={"R1": 100.0, "R2": 40.0, "R3": 70.0,
                                                    "R4": 10.0}),
        dataclasses.replace(cfg, tier_exposure_pct={"R1": 120.0, "R2": 70.0, "R3": 40.0,
                                                    "R4": -1.0}),
        dataclasses.replace(cfg, tier_exposure_pct={"R1": 100.0, "R2": 70.0}),
        dataclasses.replace(cfg, confirm_days=0), dataclasses.replace(cfg, buffer_bps=5000),
        dataclasses.replace(cfg, r1_breadth_min=155.0), dataclasses.replace(cfg, r1_h200_min=8.0),
        dataclasses.replace(cfg, default_book_weights={"smallcap": -1.0, "midcap": 0.5,
                                                       "largecap": 0.5}),
        dataclasses.replace(cfg, default_book_weights={"smallcap": 0.0, "midcap": 0.0}),
        dataclasses.replace(cfg, momentum_sentinel="  "),
        dataclasses.replace(cfg, confirm_days=0, buffer_bps=9999, recovery_confirm_evaluations=0,
                            weekly_evaluation_weekday=7, structural_indices={}),
        dataclasses.replace(cfg, structural_indices={"largecap": " ", "midcap": "X",
                                                     "smallcap": "Y"}),
    ]
    for c in bad:
        case("regime", "RegimeConfig.validate", {"cfg": cfg_doc(c)},
             lambda i: cfg_from(i["cfg"]).validate(), "None or ConfigError")

    for tier in R.RegimeTier:
        for sn in (None, sent(), sent(s50=BELOW)):
            for ok in (True, False):
                case("regime", "resolve_new_buys",
                     {"tier": tier.value, "sentinel": sn.as_dict() if sn else None,
                      "data_ok": ok},
                     lambda i: (lambda m, c: [m.value, c])(*R.resolve_new_buys(
                         R.RegimeTier(i["tier"]), sentinel=sig_from(i["sentinel"]),
                         data_ok=i["data_ok"])), "[mode, codes]")
        for gap in (-5.0, 0.0, 1.0, 1.000001, 30.0):
            for sup in (True, False):
                case("regime", "resolve_forced_action",
                     {"tier": tier.value, "gap_pct": gap, "cfg": cfg_doc(cfg),
                      "bootstrap_suppressed": sup},
                     lambda i: (lambda a, c: [a.value, c])(*R.resolve_forced_action(
                         R.RegimeTier(i["tier"]), i["gap_pct"], cfg_from(i["cfg"]),
                         bootstrap_suppressed=i["bootstrap_suppressed"])), "[action, codes]")

    case("regime", "describe", {"codes": [r for r in R.DISPLAY_REASONS] + ["NOT_A_CODE"]},
         lambda i: R.describe(i["codes"]), "display text")

    # evaluate: the spec scenarios
    def all_idx(state_map: dict[str, dict[int, R.SignalState]], **kw: Any) -> dict[str, Any]:
        return {names[c]: forced(names[c], st, **kw) for c, st in state_map.items()}

    up = {20: ABOVE, 50: ABOVE, 200: ABOVE}
    dn = {20: BELOW, 50: BELOW, 200: BELOW}
    healthy = all_idx({c: up for c in names})
    p = R.PreviousRegime
    base = {"indices": healthy, "sentinel": sent(), "brd": breadth(), "bk": bk,
            "exposure": R.ExposureSnapshot(50.0), "previous": None}
    scen: list[tuple[str, R.RegimeConfig, dict[str, Any]]] = [
        ("healthy bootstrap", cfg, {}),
        ("sentinel 50 veto", cfg, {"sentinel": sent(s50=BELOW)}),
        ("sentinel veto from R1 trims", cfg, {
            "sentinel": sent(s50=BELOW), "exposure": R.ExposureSnapshot(100.0),
            "previous": p(R.RegimeTier.R1, D(2026, 5, 29))}),
        ("veto removal no R1", cfg, {
            "indices": all_idx({"largecap": up, "midcap": {20: ABOVE, 50: BELOW, 200: ABOVE},
                                "smallcap": dn}),
            "previous": p(R.RegimeTier.R2, D(2026, 5, 29))}),
        ("recovery under veto", cfg, {
            "indices": all_idx({c: {20: ABOVE, 50: BELOW, 200: BELOW} for c in names}),
            "sentinel": sent(BELOW, BELOW), "brd": breadth(60.0),
            "previous": p(R.RegimeTier.R3, D(2026, 5, 29), recovery_confirmations=5)}),
        ("recovery applied", cfg, {
            "indices": all_idx({c: {20: ABOVE, 50: BELOW, 200: BELOW} for c in names}),
            "brd": breadth(60.0),
            "previous": p(R.RegimeTier.R4, D(2026, 5, 29), recovery_confirmations=2)}),
        ("same week rerun", cfg, {"sentinel": sent(s200=BELOW),
                                  "previous": p(R.RegimeTier.R1, D(2026, 5, 29))}),
        ("holiday week", cfg, {"_week": (D(2026, 6, 5), D(2026, 6, 4))}),
        ("bootstrap never sells", cfg, {
            "indices": all_idx({c: dn for c in names}), "sentinel": sent(BELOW, BELOW),
            "brd": breadth(20.0), "exposure": R.ExposureSnapshot(95.0)}),
        ("bootstrap adopt", dataclasses.replace(
            cfg, bootstrap_policy=R.BootstrapPolicy.ADOPT_CANDIDATE), {
            "indices": all_idx({c: dn for c in names}), "sentinel": sent(BELOW, BELOW),
            "brd": breadth(20.0), "exposure": R.ExposureSnapshot(95.0)}),
        ("stale index holds", cfg, {
            "indices": all_idx({c: up for c in names}, stale=True),
            "exposure": R.ExposureSnapshot(95.0), "previous": p(R.RegimeTier.R1, D(2026, 5, 29))}),
        ("missing index", cfg, {
            "indices": {k: v for k, v in healthy.items() if k != "NIFTY SMLCAP 250"},
            "previous": p(R.RegimeTier.R2, D(2026, 5, 29))}),
        ("stale breadth", cfg, {"brd": breadth(70.0, as_of=D(2026, 1, 1)),
                                "exposure": R.ExposureSnapshot(95.0),
                                "previous": p(R.RegimeTier.R1, D(2026, 5, 29))}),
        ("genuine risk-off through degraded", cfg, {
            "indices": {**all_idx({"largecap": dn, "smallcap": dn}),
                        "NIFTY MIDCAP 150": forced("NIFTY MIDCAP 150", up, stale=True)},
            "brd": breadth(35.0), "exposure": R.ExposureSnapshot(90.0),
            "previous": p(R.RegimeTier.R1, D(2026, 5, 29))}),
        ("degraded never rerisks", cfg, {
            "indices": all_idx({c: up for c in names}, stale=True), "brd": breadth(95.0),
            "previous": p(R.RegimeTier.R3, D(2026, 5, 29))}),
        ("low coverage breadth", cfg, {"brd": breadth(70.0, coverage=50.0),
                                       "previous": p(R.RegimeTier.R2, D(2026, 5, 29))}),
        ("missing breadth", cfg, {"brd": None, "previous": p(R.RegimeTier.R2, D(2026, 5, 29))}),
        ("unknown signal", cfg, {
            "indices": all_idx({"largecap": up, "midcap": up,
                                "smallcap": {20: ABOVE, 50: ABOVE, 200: UNKNOWN}}),
            "brd": breadth(80.0), "previous": p(R.RegimeTier.R2, D(2026, 5, 29))}),
        ("cap not achieved", cfg, {"sentinel": sent(s200=BELOW),
                                   "exposure": R.ExposureSnapshot(90.0),
                                   "previous": p(R.RegimeTier.R1, D(2026, 5, 29))}),
        ("partial retry", cfg, {
            "sentinel": sent(s200=BELOW),
            "exposure": R.ExposureSnapshot(65.0, pending_sell_pct=25.0,
                                           execution_status=R.ExecutionStatus.PARTIAL),
            "previous": p(R.RegimeTier.R3, D(2026, 6, 5), last_transition_date=D(2026, 6, 5)),
            "_week": (D(2026, 6, 12), D(2026, 6, 12))}),
        ("within tolerance", cfg, {"sentinel": sent(s200=BELOW),
                                   "exposure": R.ExposureSnapshot(40.5),
                                   "previous": p(R.RegimeTier.R3, D(2026, 5, 29))}),
        ("R4 crash", cfg, {
            "indices": all_idx({c: dn for c in names}), "sentinel": sent(BELOW, BELOW),
            "brd": breadth(20.0), "exposure": R.ExposureSnapshot(80.0),
            "previous": p(R.RegimeTier.R1, D(2026, 5, 29))}),
        ("R1 full", cfg, {"brd": breadth(80.0), "previous": p(R.RegimeTier.R1, D(2026, 5, 29))}),
        ("R2 half", cfg, {
            "indices": all_idx({"largecap": up, "midcap": {20: ABOVE, 50: BELOW, 200: ABOVE},
                                "smallcap": up}),
            "brd": breadth(50.0), "previous": p(R.RegimeTier.R2, D(2026, 5, 29))}),
        ("breadth not used", dataclasses.replace(cfg, breadth_required=False), {
            "brd": None, "previous": p(R.RegimeTier.R2, D(2026, 5, 29))}),
        ("no sentinel", cfg, {"sentinel": None, "previous": p(R.RegimeTier.R2, D(2026, 5, 29))}),
        ("missing cap", dataclasses.replace(cfg, tier_exposure_pct={"R1": 100.0}), {
            "sentinel": sent(s200=BELOW), "previous": p(R.RegimeTier.R1, D(2026, 5, 29))}),
        ("sentinel also structural", dataclasses.replace(
            cfg, momentum_sentinel="NIFTY 50"), {
            "sentinel": forced("NIFTY 50", {20: BELOW, 50: BELOW, 200: ABOVE}, close=99.0)}),
    ]
    for note, c, over in scen:
        args = {**base, **over}
        week = args.pop("_week", (D(2026, 6, 5), D(2026, 6, 5)))
        eval_case(note, c, week_end=week[0], session=week[1], **args)

    # evaluate: seeded sweep
    tiers = list(R.RegimeTier)
    statuses = list(R.ExecutionStatus)
    for k in range(80):
        c = cfg
        if k % 9 == 4:
            c = dataclasses.replace(cfg, breadth_required=False)
        if k % 11 == 6:
            c = dataclasses.replace(cfg, bootstrap_policy=R.BootstrapPolicy.ADOPT_CANDIDATE)
        sn = None if rng.random() < 0.1 else forced(
            "NIFTY 500 MOMENTUM 50", {m: rng.choice(states) for m in (20, 50, 200)},
            close=100.0, stale=rng.random() < 0.08, has_data=rng.random() > 0.05)
        brd = None if rng.random() < 0.12 else breadth(
            round(rng.uniform(5, 95), 2), as_of=D(2026, 6, 5) - dt.timedelta(days=rng.randint(0, 14)),
            coverage=round(rng.uniform(80, 100), 2))
        b = R.BookWeights({k2: round(rng.random(), 3) for k2 in names},
                          rng.choice(list(R.WeightSource)), round(rng.uniform(0, 100), 2),
                          tuple(rng.sample([R.Reason.BOOK_WEIGHTS_CONFIG_DEFAULT,
                                            R.Reason.BOOK_CLASSIFICATION_COVERAGE_LOW], 1)))
        prev = None if rng.random() < 0.2 else p(
            rng.choice(tiers), D(2026, 5, 29),
            last_transition_date=rng.choice([None, D(2026, 5, 1)]),
            recovery_confirmations=rng.randint(0, 3))
        ex = R.ExposureSnapshot(round(rng.uniform(0, 110), 3), round(rng.uniform(0, 5), 2),
                                round(rng.uniform(0, 5), 2), rng.choice(statuses))
        eval_case(f"seeded sweep {k}", c, indices=rand_indices(rng), sentinel=sn, brd=brd,
                  bk=b, exposure=ex, previous=prev)

    # evaluate: end to end from candles
    for k in range(6):
        idx = {}
        for cat, n in names.items():
            cs = candles(walk(rng, 260, 100.0, rng.uniform(-0.002, 0.002), 0.015),
                         start=D(2025, 9, 19))
            idx[n] = R.build_index_signals(n, cs, cfg, as_of=D(2026, 6, 5))
        sn = R.build_index_signals(
            "NIFTY 500 MOMENTUM 50",
            candles(walk(rng, 260, 100.0, rng.uniform(-0.002, 0.002), 0.02),
                    start=D(2025, 9, 19)), cfg, as_of=D(2026, 6, 5))
        eval_case(f"from candles {k}", cfg, indices=idx, sentinel=sn,
                  brd=breadth(round(rng.uniform(20, 80), 2), as_of=D(2026, 6, 4)), bk=bk,
                  exposure=R.ExposureSnapshot(round(rng.uniform(30, 100), 2)),
                  previous=p(rng.choice(tiers), D(2026, 5, 29)))


def dump_allocation() -> None:  # noqa: PLR0915
    rng = random.Random(7)

    for lo, hi in ((6.0, 15.0), (1.0, 30.0), (10.0, 10.0), (7.5, 12.5), (0.0, 10.0),
                   (5.0, -1.0), (33.3, 50.0)):
        case("allocation", "feasible_position_range", {"min_sleeve_w": lo, "max_sleeve_w": hi},
             lambda i: list(A.feasible_position_range(i["min_sleeve_w"], i["max_sleeve_w"])),
             "[lo, hi]")
    for n, lo, hi, nh in ((10, 6.0, 15.0, 0), (17, 6.0, 15.0, 0), (17, 6.0, 15.0, 2),
                          (3, 6.0, 15.0, 0), (10, 20.0, 10.0, 0), (0, 6.0, 15.0, 0),
                          (7, 6.0, 15.0, 0), (16, 6.0, 15.0, 0)):
        case("allocation", "validate_allocation_request",
             {"n": n, "min_sleeve_w": lo, "max_sleeve_w": hi, "n_half": nh},
             lambda i: A.validate_allocation_request(i["n"], min_sleeve_w=i["min_sleeve_w"],
                                                     max_sleeve_w=i["max_sleeve_w"],
                                                     n_half=i["n_half"]), "None or raise")

    def cands(n: int, start_rank: int = 1, price: float = 100.0, cluster: str | None = None,
              base_score: float = 80.0) -> list[A.Candidate]:
        return [A.Candidate(f"S{i:02d}", start_rank + i, base_score - i, price,
                            cluster or f"c{i % 4}", 1e8 - i) for i in range(n)]

    def solve(i: dict[str, Any]) -> Any:
        return dataclasses.asdict(A.solve_allocation(
            [cand_from(c) for c in i["candidates"]], sleeve_pct=i["sleeve_pct"],
            min_sleeve_w=i["min_sleeve_w"], max_sleeve_w=i["max_sleeve_w"],
            cluster_cap=i["cluster_cap"], half_sized=set(i["half_sized"]),
            half_factor=i["half_factor"]))

    heavy = ([A.Candidate(f"H{i}", i + 1, 80 - i, 100.0, cluster="pharma") for i in range(6)]
             + [A.Candidate(f"R{i}", 10 + i, 60 - i, 100.0, cluster=f"c{i}") for i in range(6)])
    solves = [
        (cands(10), 40.0, 6.0, 15.0, 25.0, [], 0.5),
        (cands(10, base_score=50.0), 40.0, 6.0, 15.0, 25.0, [], 0.5),
        (cands(12), 40.0, 6.0, 15.0, 25.0, [], 0.5),
        (cands(12), 100.0, 6.0, 15.0, 25.0, [], 0.5),
        (cands(17), 100.0, 6.0, 15.0, 25.0, [], 0.5),
        (cands(3), 100.0, 6.0, 15.0, 25.0, [], 0.5),
        ([], 100.0, 6.0, 15.0, 25.0, [], 0.5),
        (cands(14), 70.0, 6.0, 15.0, 25.0, [], 0.5),
        (heavy, 100.0, 1.0, 30.0, 25.0, [], 0.5),
        (cands(10), 100.0, 6.0, 15.0, 25.0, ["S00"], 0.5),
        (cands(12), 70.0, 6.0, 15.0, 25.0, ["S03", "S07", "S11", "ZZ"], 0.5),
        (cands(8, cluster="it"), 100.0, 6.0, 15.0, 25.0, [], 0.5),
        (cands(8, cluster="other"), 100.0, 6.0, 15.0, 25.0, [], 0.5),
        (cands(2), 10.0, 1.0, 50.0, 25.0, [], 0.5),
        ([A.Candidate("A", 1, -5.0, 10.0), A.Candidate("B", 2, 0.0, 10.0)], 10.0, 1.0, 50.0,
         100.0, [], 0.5),
        (cands(12), 100.0, 6.0, 15.0, 25.0, ["S00", "S01"], 0.3),
    ]
    for _ in range(14):
        n = rng.randint(7, 16)
        cs = [A.Candidate(f"X{j:02d}", j + 1, round(rng.uniform(1, 100), 3),
                          round(rng.uniform(10, 3000), 2),
                          rng.choice(["bank", "it", "pharma", "auto", "other", ""]),
                          round(rng.uniform(1e6, 1e9), 0)) for j in range(n)]
        half = [c.symbol for c in cs if rng.random() < 0.2]
        solves.append((cs, round(rng.choice([100.0, 70.0, 40.0, 10.0]), 1), 6.0, 15.0,
                       rng.choice([25.0, 30.0, 40.0]), half, 0.5))
    for cs, sl, lo, hi, cc, half, hf in solves:
        case("allocation", "solve_allocation",
             {"candidates": [dataclasses.asdict(c) for c in cs], "sleeve_pct": sl,
              "min_sleeve_w": lo, "max_sleeve_w": hi, "cluster_cap": cc, "half_sized": half,
              "half_factor": hf}, solve, "dataclasses.asdict(Allocation)")

    P = A.Position

    def osales(i: dict[str, Any]) -> Any:
        return [dataclasses.asdict(x) for x in A.order_sales(
            [pos_from(x) for x in i["positions"]], nearby_band=i["nearby_band"],
            tax_flagged=set(i["tax_flagged"]))]

    sale_sets = [
        ([P("A", 100, 100.0, 90.0, 1, guard_exit=True), P("B", 100, 100.0, 90.0, 2),
          P("C", 100, 100.0, 90.0, 3)], 3, []),
        ([P("A", 100, 100.0, 90.0, 1), P("B", 100, 100.0, 90.0, 5),
          P("C", 100, 100.0, 90.0, 20)], 1, []),
        ([P("WIN", 100, 120.0, 100.0, 10), P("LOSE", 100, 80.0, 100.0, 11)], 5, []),
        ([P("CLEAR", 100, 120.0, 100.0, 10), P("NEARLTCG", 100, 120.0, 100.0, 11)], 5,
         ["NEARLTCG"]),
        ([P("RANKED", 100, 100.0, 90.0, 1), P("GONE", 100, 100.0, 90.0, None)], 100, []),
        ([], 3, []),
    ]
    for _ in range(8):
        ps = [P(f"P{j:02d}", rng.randint(1, 500), round(rng.uniform(10, 500), 2),
                round(rng.uniform(0, 500), 2), rng.choice([None, *range(1, 40)]),
                guard_exit=rng.random() < 0.1) for j in range(rng.randint(3, 14))]
        sale_sets.append((ps, rng.choice([0, 1, 3, 5]),
                          [x.symbol for x in ps if rng.random() < 0.2]))
    for ps, band, flagged in sale_sets:
        case("allocation", "order_sales",
             {"positions": [dataclasses.asdict(x) for x in ps], "nearby_band": band,
              "tax_flagged": flagged}, osales, "positions in sale order")

    def survivors(i: dict[str, Any]) -> Any:
        s, d = A.select_survivors(
            [cand_from(c) for c in i["candidates"]], [pos_from(x) for x in i["positions"]],
            max_positions=i["max_positions"], new_buys=R.NewBuyMode(i["new_buys"]),
            residual_max_names=i["residual_max_names"],
            residual_ordering=tuple(i["residual_ordering"]), nearby_band=i["nearby_band"],
            tax_flagged=set(i["tax_flagged"]))
        return {"survivors": [dataclasses.asdict(c) for c in s], "dropped": d}

    held12 = [P(f"S{j:02d}", 100, 100.0, 90.0, j + 1, cluster="c0") for j in range(12)]
    for cs, ps, mx, nb, res, order in (
        (cands(20), held12, 15, "blocked", None, ["rank", "liquidity"]),
        (cands(30), [], 15, "half", None, ["rank", "liquidity"]),
        (cands(12), held12, 15, "full", 2, ["rank", "liquidity"]),
        (cands(12), held12, 15, "full", 2, ["liquidity", "rank"]),
        (cands(12), [], 15, "blocked", 3, ["liquidity"]),
        (cands(12), held12, 15, "blocked", 0, ["rank"]),
        (cands(12), held12, -3, "full", None, ["rank"]),
        (list(reversed(cands(12))), held12[:5], 8, "full", None, ["rank"]),
    ):
        case("allocation", "select_survivors",
             {"candidates": [dataclasses.asdict(c) for c in cs],
              "positions": [dataclasses.asdict(x) for x in ps], "max_positions": mx,
              "new_buys": nb, "residual_max_names": res, "residual_ordering": order,
              "nearby_band": 3, "tax_flagged": ["S11"]}, survivors, "{survivors, dropped}")

    def orders(i: dict[str, Any]) -> Any:
        lines = A.build_orders(alloc_from(i["allocation"]), [pos_from(x) for x in i["positions"]],
                               [cand_from(c) for c in i["candidates"]], capital=i["capital"],
                               tax_flagged=set(i["tax_flagged"]),
                               guard_exits=set(i["guard_exits"]))
        return {"lines": [dataclasses.asdict(o) for o in lines],
                "projected_equity_pct": A.projected_equity_pct(lines, i["capital"])}

    for k in range(10):
        cs = cands(12, price=round(rng.uniform(50, 2500), 2))
        if k % 3 == 0:
            cs = [dataclasses.replace(c, price=round(rng.uniform(50, 2500), 2)) for c in cs]
        sleeve = rng.choice([100.0, 70.0, 40.0])
        half = {c.symbol for c in cs if rng.random() < 0.15}
        try:
            alloc = A.solve_allocation(cs, sleeve_pct=sleeve, min_sleeve_w=6.0, max_sleeve_w=15.0,
                                       half_sized=half)
        except A.AllocationError:
            alloc = A.Allocation({}, {}, frozenset(), sleeve)
        ps = [P(c.symbol, rng.randint(0, 2000), c.price if rng.random() < 0.8 else 0.0,
                round(c.price * rng.uniform(0.7, 1.3), 2), c.rank,
                guard_exit=rng.random() < 0.1) for c in cs if rng.random() < 0.6]
        ps.append(P("ORPHAN", 50, 321.5, 300.0, None))
        ps.append(P("ZEROPX", 50, 0.0, 300.0, None))
        cap = rng.choice([1_200_000.0, 10_000_000.0, 0.0])
        case("allocation", "build_orders",
             {"allocation": dataclasses.asdict(alloc),
              "positions": [dataclasses.asdict(x) for x in ps],
              "candidates": [dataclasses.asdict(c) for c in cs], "capital": cap,
              "tax_flagged": [p.symbol for p in ps if rng.random() < 0.3],
              "guard_exits": [p.symbol for p in ps if p.guard_exit]}, orders,
             "{lines, projected_equity_pct}")


def main() -> None:
    import shutil

    target = _golden().GOLDEN_DIR / LANE
    if target.exists():
        shutil.rmtree(target)
    dump_regime()
    dump_allocation()
    sys.stdout.write(f"wrote {len(WRITTEN)} goldens under {target}\n")
    for k, v in sorted(COUNTS.items()):
        sys.stdout.write(f"  {k}: {v}\n")


if __name__ == "__main__":
    main()
