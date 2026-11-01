"""Dump the weekly book's answer key for `baskfy-desk`'s `lanes::weekly` (wave D, lane weekly).

    PYTHONPATH=<wt>/kite-momentum-rebalancer:<wt>/decile-blueprint/packages/core/src:\\
    <wt>/decile-blueprint/packages/execution/src \\
    <main>/kite-momentum-rebalancer/.venv/bin/python tools/parity/desk_weekly_goldens.py

Run from the worktree's ``kite-momentum-rebalancer`` (``app.config`` reads the environment at
import, so the environment is cleared of every desk variable first). Writes
``rust/crates/baskfy-desk/fixtures/weekly/*.json``:

* ``primitives.json`` — numpy's quicksort argsort, pandas' ``rank(pct=True)``,
  ``Series.quantile`` (linear), numpy ``round(1)`` and Python ``round(x, n)`` over seeded arrays.
* ``scans.json`` — synthetic scan CSV texts (the corpus's synthetic ``sample_scan.csv``, read only,
  and variants of it: missing cells, ``NA`` strings, ties, a ``listed_on`` column, CRLF/BOM/quotes,
  duplicated symbols, an all-rejected scan, a missing column) → what ``load_scan`` parsed (dtype
  and values per column), ``audit``, ``score`` (every scored column), or the error.
* ``corpus.json`` — the same for the real scan in the regression corpus, by SHA-256 only (the
  file is read from the corpus at test time and never copied).
* ``plans.json`` — ``build_plan`` over those scans with assorted holdings, cash, live prices,
  breadth overrides, clusters and ``FULLY_INVESTED``, as orjson bytes (types kept).
* ``costs.json`` — ``order_cost``, ``cost_pct``, ``plan_cost``.
* ``plan_store.json`` — ``save_plan`` → ``record_execution`` → ``reconciliation`` on SQLite.

Synthetic data only. Never ``portfolio.db``; the corpus is opened read-only.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import sqlite3
import sys
import tempfile
import types
import uuid
from pathlib import Path

for key in list(os.environ):
    if key.startswith(("DESK_", "BASKFY_", "KITE_", "REGIME_", "RISK_", "OPTION", "SWING_", "VBT_",
                       "TWT_", "FNO_")) or key in ("DRY_RUN", "FULLY_INVESTED", "SCAN_SOURCE_DEFAULT",
                                                   "SCREENER_DATABASE_URL", "DB_PATH"):
        del os.environ[key]
os.environ["DRY_RUN"] = "true"

import numpy as np  # noqa: E402
import orjson  # noqa: E402
import pandas as pd  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = REPO / "rust" / "crates" / "baskfy-desk" / "fixtures" / "weekly"
CORPUS = Path("/Volumes/T7-Work/Dev/projects/baskfy/kite-momentum-rebalancer/data/uploads")
FIXTURE = HERE / "desk_fixtures" / "default" / "kite.json"

os.chdir(REPO / "kite-momentum-rebalancer")
sys.path.insert(0, str(REPO / "kite-momentum-rebalancer"))

from app import config as C  # noqa: E402
from app import rebalance  # noqa: E402
from app import scoring  # noqa: E402
from baskfy_core import basket  # noqa: E402
from baskfy_core import costs  # noqa: E402

rng = random.Random(20261030)


def frepr(x: float) -> str:
    return repr(float(x))


def dumps(value: object) -> str:
    return orjson.dumps(value, option=orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY).decode()


# ---------------------------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------------------------


def primitives() -> dict:
    cases = []
    for i in range(150):
        n = rng.randint(0, 140)
        kind = i % 4
        vals = []
        for _ in range(n):
            if kind == 0:
                v = round(rng.uniform(-50, 120), rng.choice([0, 1, 1, 2]))
            elif kind == 1:
                v = float(rng.randint(0, 6))
            elif kind == 2:
                v = rng.uniform(-1e6, 1e9)
            else:
                v = round(rng.gauss(50, 20), 1)
            if rng.random() < 0.05:
                v = float("nan")
            vals.append(v)
        s = pd.Series(vals, dtype="float64")
        desc = pd.DataFrame({"v": s}).sort_values("v", ascending=False).index.tolist()
        asc = pd.DataFrame({"v": s}).sort_values("v").index.tolist()
        rank = (s.rank(pct=True) * 100).tolist()
        q = s.quantile([0.01, 0.99]).tolist() if n else [float("nan"), float("nan")]
        q85 = float(s.quantile(0.85)) if n else float("nan")
        r1 = s.round(1).tolist()
        clipped = s.clip(q[0], q[1]).tolist() if n else []
        cases.append({
            "values": [frepr(v) for v in vals],
            "desc": desc, "asc": asc,
            "rank_pct": [frepr(v) for v in rank],
            "q01_q99": [frepr(v) for v in q], "q85": frepr(q85),
            "np_round1": [frepr(v) for v in r1],
            "clipped": [frepr(v) for v in clipped],
            "py_round2": [frepr(round(v, 2)) for v in vals],
            "py_round3": [frepr(round(v, 3)) for v in vals],
            "py_round1": [frepr(round(v, 1)) for v in vals],
            "py_round0": [None if v != v else round(v) for v in vals],
        })
    # half-way cases for the roundings
    halves = [0.125, 0.375, 2.675, 1.005, 0.5, 1.5, 2.5, -0.5, -2.5, 1234.5, 0.05, 0.15, 0.25, 0.35,
              1e-7, 123456789.125, 4.35, 4.45, 8.25, 99.95, 79.85, 52.65]
    return {"cases": cases, "halves": [{"v": frepr(v), "np_round1": frepr(np.round(v, 1)),
                                        "py_round1": frepr(round(v, 1)), "py_round2": frepr(round(v, 2)),
                                        "py_round3": frepr(round(v, 3)), "py_round0": round(v)}
                                       for v in halves]}


# ---------------------------------------------------------------------------------------------
# frames
# ---------------------------------------------------------------------------------------------


def column_dump(s: pd.Series) -> dict:
    dt = str(s.dtype)
    if dt.startswith("int"):
        return {"dtype": "int64", "values": [int(v) for v in s]}
    if dt.startswith("float"):
        return {"dtype": "float64", "values": [frepr(v) for v in s]}
    if dt == "bool":
        return {"dtype": "bool", "values": [bool(v) for v in s]}
    return {"dtype": "object", "values": [None if (isinstance(v, float) and v != v) else str(v) for v in s]}


def frame_dump(df: pd.DataFrame) -> dict:
    return {"columns": list(df.columns), "data": {c: column_dump(df[c]) for c in df.columns}}


SCORED_COLS = ["SCORE", "rank", "ext_over_20dma", "A_trend", "B_momentum", "C_sharpe", "D_consistency",
               "E_liquidity", "F_penalty"]


def scan_result(text: bytes) -> dict:
    try:
        u = scoring.load_scan(io.BytesIO(text))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    out: dict = {"loaded": frame_dump(u)}
    try:
        out["audit"] = dumps(scoring.audit(u))
    except Exception as exc:  # noqa: BLE001
        out["audit_error"] = f"{type(exc).__name__}: {exc}"
    try:
        sc = scoring.score(u)
    except Exception as exc:  # noqa: BLE001
        out["score_error"] = f"{type(exc).__name__}: {exc}"
        return out
    out["scored_columns"] = list(sc.columns)
    out["reject"] = sc["reject"].tolist()
    out["scored"] = {c: column_dump(sc[c]) for c in SCORED_COLS if c in sc.columns}
    return out


def variants(sample: bytes) -> list[tuple[str, bytes]]:
    text = sample.decode("utf-8")
    lines = text.splitlines()
    header, rows = lines[0], lines[1:]
    cols = header.split(",")
    out = [("sample", sample)]

    def with_rows(new_rows: list[str], new_header: str | None = None) -> bytes:
        return ("\n".join([new_header or header, *new_rows]) + "\n").encode()

    # missing cells and NA strings
    r = [row.split(",") for row in rows]
    for i, row in enumerate(r):
        if i % 7 == 3:
            row[cols.index("median_volume_one_year")] = ""
        if i % 11 == 5:
            row[cols.index("ma_50")] = "NA"
        if i % 13 == 2:
            row[cols.index("rsi_one_month")] = "nan"
        if i % 17 == 4:
            row[cols.index("away_from_high_one_year")] = "null"
    out.append(("missing_cells", with_rows([",".join(x) for x in r])))
    # ties: duplicate the scoring numbers of the top rows under new symbols, and round metrics
    r = [row.split(",") for row in rows]
    extra = []
    for k, row in enumerate(r[:20]):
        clone = list(row)
        clone[0] = f"TIE{k:02d}"
        extra.append(clone)
        clone2 = list(row)
        clone2[0] = f"TWIN{k:02d}"
        extra.append(clone2)
    for row in r + extra:
        for c in ("absolute_return_one_month", "sharpe_return_three_months", "positive_days_percent_three_months"):
            j = cols.index(c)
            try:
                row[j] = str(round(float(row[j])))
            except ValueError:
                pass
    out.append(("ties", with_rows([",".join(x) for x in r + extra])))
    # listed_on (half-size rule)
    r = [row.split(",") for row in rows]
    listed = []
    for i, row in enumerate(r):
        listed.append(row + [["2025-06-30", "2010-01-01", "", "2026-01-15", "2025-02-14", "2025-02-13"][i % 6]])
    out.append(("listed_on", with_rows([",".join(x) for x in listed], header + ",listed_on")))
    # CRLF, BOM, quoted names with commas and doubled quotes
    r = [row.split(",") for row in rows]
    quoted = []
    for i, row in enumerate(r):
        row = list(row)
        row[1] = '"' + row[1].replace('"', '""') + (', Ltd "the" one' if i % 5 == 0 else "") + '"'
        quoted.append(",".join(row))
    out.append(("crlf_bom_quotes", ("﻿" + "\r\n".join([header, *quoted]) + "\r\n").encode()))
    # duplicated symbols (load_scan keeps the first)
    out.append(("duplicates", with_rows(rows + rows[:5])))
    # all rejected
    r = [row.split(",") for row in rows]
    for row in r:
        row[cols.index("series")] = "BE"
    out.append(("all_rejected", with_rows([",".join(x) for x in r])))
    # a required column missing
    j = cols.index("beta")
    out.append(("missing_beta", with_rows([",".join(x for k, x in enumerate(row.split(",")) if k != j)
                                            for row in rows],
                                           ",".join(c for k, c in enumerate(cols) if k != j))))
    # ints where floats were (is_nifty_fno as True/False, integral closes)
    r = [row.split(",") for row in rows]
    for i, row in enumerate(r):
        row[cols.index("is_nifty_fno")] = "True" if row[cols.index("is_nifty_fno")] == "1" else "False"
        if i % 3 == 0:
            row[cols.index("marketcap")] = str(int(float(row[cols.index("marketcap")])))
    out.append(("bools_and_ints", with_rows([",".join(x) for x in r])))
    # high RSI / parabolic, untouchables, T2T, circuits, excluded instrument
    r = [row.split(",") for row in rows]
    r[0][cols.index("rsi_one_month")] = "85.4"
    r[1][cols.index("rsi_one_month")] = "82.0"
    r[2][cols.index("series")] = "BZ"
    r[3][cols.index("circuits_three_months")] = "9"
    r[4][0] = "SGBDE31III"
    r[5][cols.index("volatility_one_year")] = "0.61"
    r[6][cols.index("beta")] = "1.75"
    r[7][cols.index("circuits_one_year")] = "12"
    out.append(("edges", with_rows([",".join(x) for x in r])))
    return out


# ---------------------------------------------------------------------------------------------
# plans
# ---------------------------------------------------------------------------------------------


class FakeKC:
    def __init__(self, fixture: dict) -> None:
        self.routes = fixture["routes"]

    def holdings(self) -> list:
        return self.routes["portfolio.holdings"]["data"]

    def positions(self) -> dict:
        return self.routes["portfolio.positions"]["data"]


def fixture_holdings() -> list[dict]:
    from app.kite_client import Kite

    fx = json.loads(FIXTURE.read_text())
    k = Kite.__new__(Kite)
    k.kc = FakeKC(fx)
    k._token_mtime = None
    k.refresh_token_if_changed = lambda: False  # type: ignore[method-assign]
    return k.holdings()


def h(sym: str, qty: int, px: float | int, pledged: int = 0) -> dict:
    return {"symbol": sym, "exchange": "NSE", "quantity": qty, "pledged_qty": pledged,
            "average_price": px, "last_price": px}


def cfg_with(**over: object) -> types.SimpleNamespace:
    names = [n for n in dir(C) if n.isupper()]
    ns = types.SimpleNamespace(**{n: getattr(C, n) for n in names})
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def plan_case(name: str, scan: str, scored: pd.DataFrame, holdings: list[dict], cash: float,
              live: dict[str, float], breadth: float | None, clusters: dict[str, str] | None,
              cfg_over: dict) -> dict:
    pid = uuid.UUID(int=rng.getrandbits(128), version=4)
    cfg = cfg_with(**cfg_over)
    orig = basket.uuid.uuid4
    basket.uuid.uuid4 = lambda: pid  # type: ignore[assignment]
    try:
        plan = basket.build_plan(scored, [dict(x) for x in holdings], cash, cfg=cfg,
                                 tradeable=rebalance._tradeable, clusters=clusters,
                                 live_prices=dict(live), breadth_override=breadth)
        result = {"plan": dumps(plan)}
    except Exception as exc:  # noqa: BLE001
        result = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        basket.uuid.uuid4 = orig  # type: ignore[assignment]
    return {"name": name, "scan": scan, "holdings": dumps(holdings), "cash": dumps(cash),
            "live": dumps(live), "breadth": breadth, "clusters": clusters, "cfg": cfg_over,
            "plan_id_uuid": pid.hex, **result}


def plans(scans: dict[str, bytes]) -> list[dict]:
    out = []
    fixture_h = fixture_holdings()
    fx = json.loads(FIXTURE.read_text())
    quotes = {k.split(":", 1)[1]: float(v["last_price"]) for k, v in fx["quotes"].items()}
    for scan_name, text in scans.items():
        try:
            u = scoring.load_scan(io.BytesIO(text))
            sc = scoring.score(u)
        except Exception:  # noqa: BLE001
            continue
        closes = {str(s): float(c) for s, c in zip(sc.symbol, sc.close)}
        elig = sc[sc.reject == ""].sort_values("rank")["symbol"].tolist()
        rejected = sc[sc.reject != ""]["symbol"].tolist()
        drift = {s: round(p * rng.uniform(0.9, 1.1), 2) for s, p in closes.items()}
        some_missing = {s: p for i, (s, p) in enumerate(drift.items()) if i % 9 != 4}
        if elig:
            some_missing[elig[0]] = 0.0
        hs = []
        if len(elig) > 3:
            hs.append(h(elig[0], 50, closes[elig[0]]))
            hs.append(h(elig[1], 7, int(closes[elig[1]])))
            hs.append(h(elig[-1], 120, closes[elig[-1]], pledged=40))
        if rejected:
            hs.append(h(rejected[0], 10_000, closes[rejected[0]]))
            hs.append(h(rejected[-1], 3, closes[rejected[-1]]))
        hs.append(h("SGBDE31III-GB", 392, 15430.0))
        hs.append(h("NOTINSCAN", 30, 512))
        hs.append(h("NOTINSCAN2", 3, 50.0, pledged=3))
        cl = {s: ("metals" if i % 3 == 0 else "it" if i % 3 == 1 else "pharma") for i, s in enumerate(elig[:12])}
        cases = [
            ("empty book", [], 5_000_000.0, closes, None, None, {}),
            ("empty book, drifted live", [], 2_500_000.0, drift, None, None, {}),
            ("unpriced and zero prices", [], 5_000_000.0, some_missing, None, None, {}),
            ("synthetic book", hs, 1_200_000.0, drift, None, None, {}),
            ("synthetic book, breadth 50", hs, 1_200_000.0, drift, 50.0, None, {}),
            ("synthetic book, breadth 30, clusters", hs, 900_000.0, drift, 30.0, cl, {}),
            ("synthetic book, fully invested", hs, 900_000.0, drift, 30.0, None, {"FULLY_INVESTED": True}),
            ("fixture book", fixture_h, 750_000.0, {**closes, **quotes}, None, None, {}),
            ("fixture book, int cash", fixture_h, 300_000, {**quotes}, 68.6347, None, {}),
            ("tiny cash", hs[:2], 1000.0, drift, None, None, {}),
        ]
        if len(elig) > 1:
            # one share short of the target (the no-trade band)
            sym = elig[0]
            tmp = basket.build_plan(sc, [], 5_000_000.0, cfg=C, tradeable=rebalance._tradeable,
                                    live_prices=closes)
            tgt = next(o for o in tmp["orders"] if o["symbol"] == sym)["qty_final"]
            px = closes[sym]
            cases.append(("one share short", [h(sym, tgt - 1, px)], 5_000_000.0 - (tgt - 1) * px, closes,
                          None, None, {}))
            # a keeper far down the ranks and a challenger (replacement edge)
            low = elig[-1]
            cases.append(("weak keeper", [h(low, 25, closes[low]), h(elig[1], 9, closes[elig[1]])], 3_000_000.0,
                          closes, None, None, {}))
            cases.append(("weak keeper, small edge", [h(low, 25, closes[low])], 3_000_000.0, closes, None, None,
                          {"REPLACEMENT_EDGE": 200.0}))
            cases.append(("narrow band", [h(s, 5, closes[s]) for s in elig[:20]], 400_000.0, closes, None, None,
                          {"RETENTION_BUFFER": 0, "TARGET_POSITIONS": (3, 5)}))
        for name, hold, cash, live, br, clusters, over in cases:
            out.append(plan_case(name, scan_name, sc, hold, cash, live, br, clusters, over))
    return out


# ---------------------------------------------------------------------------------------------
# costs and the plan store
# ---------------------------------------------------------------------------------------------


def cost_cases() -> dict:
    vals = [0.0, 1.0, 999.99, 2000.0, 15_930.0, 50_000.0, 123_456.78, 7_349_552.0, -5000.0, 1e9]
    single = []
    for v in vals:
        for side in ("BUY", "SELL", "sell", "buy"):
            c = costs.order_cost(v, side)
            single.append({"value": frepr(v), "side": side, "as_dict": dumps(c.as_dict()), "total": frepr(c.total),
                           "cost_pct": frepr(costs.cost_pct(v, side))})
    plans_ = []
    for _ in range(40):
        orders = []
        for _ in range(rng.randint(0, 12)):
            d = rng.choice([0, rng.randint(-500, 500)])
            orders.append({"delta": d, "ref_price": rng.choice([round(rng.uniform(10, 5000), 2), None, 0.0])})
        plans_.append({"orders": dumps(orders), "plan_cost": dumps(costs.plan_cost(orders))})
    return {"single": single, "plans": plans_}


def plan_store_cases() -> list[dict]:
    from app.analytics import db, plan_store as ps

    out = []
    statuses = ["PLACED", "OK", "DRY_RUN", "dry_run", "DUPLICATE", "REJECTED", "BLOCKED", "ABORTED", "ERROR",
                "RISK_BLOCKED", "", None]
    for i in range(12):
        with tempfile.TemporaryDirectory() as tmp, db.connect(str(Path(tmp) / "p.db")) as conn:
            db.migrate(conn)
            orders = []
            for k in range(rng.randint(0, 8)):
                d = rng.choice([0, rng.randint(-300, 300)])
                orders.append({"symbol": f"S{k:02d}", "delta": d, "weight": rng.choice([0, 1.25, 6.0]),
                               "ref_price": rng.choice([round(rng.uniform(10, 900), 2), 100, None])})
            plan = {"plan_id": f"p{i:02d}abcdef", "orders": orders,
                    "created_at": rng.choice([1793349960.0 + i, None])}
            ps.save_plan(conn, plan, evaluation_id=rng.choice([None, "ev-1"]), note=f"scan x [upload:{i}]")
            results = []
            for o in orders:
                if rng.random() < 0.85:
                    st = rng.choice(statuses)
                    r = {"symbol": o["symbol"], "status": st}
                    if rng.random() < 0.5:
                        r["order_id"] = f"OID{i}{o['symbol']}"
                    if rng.random() < 0.5:
                        r["price"] = o["ref_price"]
                    if rng.random() < 0.2:
                        r["avg_price"] = 101.5
                    results.append(r)
            results.append({"symbol": "UNPLANNED", "status": "PLACED"})
            rec = ps.record_execution(conn, plan["plan_id"], results)
            recon = ps.reconciliation(conn, plan["plan_id"])
            versions = [dict(r) for r in conn.execute("SELECT * FROM rebalance_versions")]
            rows = [dict(r) for r in conn.execute("SELECT * FROM rebalance_orders ORDER BY id")]
            out.append({"plan": dumps(plan), "results": dumps(results), "record": dumps(rec),
                        "reconciliation": dumps(recon), "versions": dumps(versions), "orders": dumps(rows)})
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    sample = (CORPUS / "sample_scan.csv").read_bytes()  # read only (synthetic: tests/make_sample_scan.py)
    scans = dict(variants(sample))
    (OUT / "primitives.json").write_text(json.dumps(primitives()))
    (OUT / "scans.json").write_text(json.dumps(
        {"scans": [{"name": n, "csv": t.decode("utf-8"), **scan_result(t)} for n, t in scans.items()]}))
    corpus = []
    for path in sorted(CORPUS.glob("*.csv")):
        raw = path.read_bytes()
        result = scan_result(raw)
        result.pop("loaded", None)  # the corpus's numbers stay in the corpus
        corpus.append({"file": path.name, "sha256": hashlib.sha256(raw).hexdigest(), **result})
    (OUT / "corpus.json").write_text(json.dumps({"files": corpus}))
    real = {p.name: p.read_bytes() for p in sorted(CORPUS.glob("scan_*.csv"))}
    plan_inputs = {k: v for k, v in scans.items() if k in ("sample", "ties", "listed_on", "edges", "missing_cells",
                                                           "all_rejected", "bools_and_ints")}
    plan_inputs.update(real)
    (OUT / "plans.json").write_text(json.dumps({"cases": plans(plan_inputs)}))
    (OUT / "costs.json").write_text(json.dumps(cost_cases()))
    (OUT / "plan_store.json").write_text(json.dumps({"cases": plan_store_cases()}))
    print("wrote", sorted(p.name for p in OUT.glob("*.json")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
