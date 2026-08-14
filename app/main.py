"""Webview + API. Flow: /login → upload scan → /analyze (plan) → review → /execute (confirm-gated).
Execution NEVER happens without confirm=true + a live plan_id from this session."""
from __future__ import annotations
import io
import json
import logging
import time
from fastapi import FastAPI, UploadFile, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
try:                       # ultra-fast serialization when available
    from fastapi.responses import ORJSONResponse as JSONResponse
except Exception:
    from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
import pandas as pd

from . import config as C
from .scoring import load_scan, score, audit
from .rebalance import build_plan
from .kite_client import Kite

logging.basicConfig(level=logging.INFO)
try:
    import uvloop; uvloop.install()      # faster asyncio event loop (Linux/macOS)
except Exception:
    pass
app = FastAPI(title="Kite Momentum Rebalancer", default_response_class=JSONResponse)
templates = Jinja2Templates(directory="app/templates")

PLANS: dict[str, dict] = {}          # plan_id -> plan (in-memory, session-scoped)
_kite: Kite | None = None


def kite() -> Kite:
    global _kite
    if _kite is None:
        _kite = Kite()
    return _kite


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    authed = False
    try:
        authed = kite().is_authed()
    except Exception:
        pass
    # Starlette >=0.29 requires the request-first signature (the old
    # (name, {"request": ...}) form was removed in Starlette 1.x).
    return templates.TemplateResponse(request, "index.html",
                                      {"authed": authed, "dry_run": C.DRY_RUN})


@app.get("/login")
def login():
    return RedirectResponse(kite().login_url())


@app.get("/callback")
def callback(request_token: str = ""):
    if not request_token:
        raise HTTPException(400, "Missing request_token")
    kite().exchange_token(request_token)
    return RedirectResponse("/")


@app.post("/analyze")
async def analyze(scan: UploadFile):
    """Score the uploaded scan, pull live holdings + cash, return the full plan."""
    raw = await scan.read()
    df = load_scan(io.BytesIO(raw))
    scan_audit = audit(df)
    scored = score(df)

    k = kite()
    if not k.is_authed():
        raise HTTPException(401, "Kite session expired — click Login first.")
    holdings = [h for h in k.holdings() if h["symbol"] not in C.EXCLUDED_SYMBOLS
                and not h["symbol"].upper().startswith("SGB")]
    cash = k.available_cash()

    # refresh ref prices with live LTP where possible
    try:
        live = k.ltp([h["symbol"] for h in holdings])
        for h in holdings:
            h["last_price"] = live.get(h["symbol"], h["last_price"])
    except Exception:
        pass

    plan = build_plan(scored, holdings, cash)
    plan["audit"] = scan_audit
    plan["created_at"] = time.time()
    PLANS[plan["plan_id"]] = plan
    return JSONResponse(plan)


@app.post("/execute")
async def execute(plan_id: str = Form(...), confirm: str = Form(...),
                  place_stops: str = Form("true")):
    """Execute a reviewed plan. Hard gates: confirm=='true', plan exists, plan < 30 min old."""
    if confirm != "true":
        raise HTTPException(400, "Execution requires explicit confirmation.")
    plan = PLANS.get(plan_id)
    if not plan:
        raise HTTPException(404, "Unknown or expired plan_id — re-run Analyze.")
    if time.time() - plan["created_at"] > 1800:
        raise HTTPException(410, "Plan older than 30 minutes — prices stale, re-run Analyze.")

    k = kite()
    results = []
    # sells first (frees cash), then buys, then GTT stops
    ordered = sorted([o for o in plan["orders"] if o["delta"] != 0],
                     key=lambda o: (o["delta"] > 0, -abs(o["delta"] * o["ref_price"])))
    for o in ordered:
        side = "SELL" if o["delta"] < 0 else "BUY"
        # Pledged shares sell directly on Zerodha (instant-sale feature); collateral
        # margin reduces automatically — no unpledge gate needed.
        results.append(k.place_cnc_order(o["symbol"], abs(o["delta"]), side, o["ref_price"]))
        time.sleep(0.35)  # rate-limit courtesy

    stops = []
    if place_stops == "true":
        for o in plan["orders"]:
            if o["qty_final"] > 0 and o.get("stop"):
                stops.append(k.place_gtt_stop(o["symbol"], o["qty_final"], o["stop"],
                                              o["ref_price"]))
                time.sleep(0.35)

    log_path = f"data/outputs/execution_{plan_id}.json"
    with open(log_path, "w") as f:
        json.dump({"plan": plan, "orders": results, "gtt": stops}, f, indent=2, default=str)
    return JSONResponse({"dry_run": C.DRY_RUN, "orders": results, "gtt": stops,
                         "log": log_path})


# --- regime overlay (read-only) --------------------------------------------------------
# Both routes read PRECOMPUTED rows from SQLite. No Kite calls, no signal recomputation,
# no writes: a status page must never be able to change the state it reports.
@app.get("/regime", response_class=HTMLResponse)
def regime_page(request: Request):
    from .analytics import db as _db, regime_view as _rv
    with _db.connect() as conn:
        _db.migrate(conn)
        view = _rv.build(conn)
    return templates.TemplateResponse(request, "regime.html", {"v": view})


@app.get("/regime/backtest", response_class=HTMLResponse)
def regime_backtest(request: Request):
    """Detailed backtest results, kept off the live status page on purpose."""
    import os
    path = "data/outputs/regime_backtest.json"
    data = None
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
    return templates.TemplateResponse(request, "regime_backtest.html",
                                      {"b": data, "path": path})


@app.get("/regime/data")
def regime_data():
    from .analytics import db as _db, regime_view as _rv
    with _db.connect() as conn:
        _db.migrate(conn)
        return JSONResponse(_rv.build(conn))


@app.get("/status")
def status():
    try:
        k = kite()
        return {"authed": k.is_authed(), "dry_run": C.DRY_RUN,
                "cash": k.available_cash() if k.is_authed() else None}
    except Exception as exc:
        return {"authed": False, "dry_run": C.DRY_RUN, "error": str(exc)}
