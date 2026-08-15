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
from .core.gateway import OrderGateway
from .core.guards import UntouchableInstrumentError
from .core.risk import RiskManager
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
# Argv is stored as JSON so the exact command can be shown back without re-quoting it.
templates.env.filters["fromjson"] = json.loads

def _apply_stored_settings() -> None:
    """Push DB overrides onto the config module at startup.

    Without this a restart would silently revert every runtime change to whatever .env
    says, which is the opposite of what a persisted setting means.
    """
    try:
        from .analytics import db as _db, settings as _st
        with _db.connect() as conn:
            _db.migrate(conn)
            applied = _st.apply_to_config(conn)
        if applied:
            logging.info("applied %d stored setting override(s): %s",
                         len(applied), ", ".join(sorted(applied)))
    except Exception as exc:
        logging.warning("could not apply stored settings: %s", exc)


_apply_stored_settings()

PLANS: dict[str, dict] = {}          # plan_id -> plan (in-memory, session-scoped)
_kite: Kite | None = None
_gateway: OrderGateway | None = None
_risk: RiskManager | None = None


def kite() -> Kite:
    global _kite
    if _kite is None:
        _kite = Kite()
    return _kite


def _latest_nav() -> float:
    """NAV from the most recent stored EOD snapshot, for deriving risk limits."""
    try:
        from .analytics import db as _db
        with _db.connect() as conn:
            _db.migrate(conn)
            rows = _db.snapshot_series(conn)
            return float(rows[-1]["nav"]) if rows else 0.0
    except Exception:
        return 0.0


def gateway() -> OrderGateway:
    """The sole order path. Holds the risk manager so the daily loss cap, the kill switch
    and the order counter persist across requests rather than resetting per plan.

    Limits are derived from live NAV so they cannot contradict the strategy's own sizing:
    a fixed rupee cap silently forbids a position MAX_SINGLE_WEIGHT explicitly permits.
    """
    global _gateway, _risk
    if _gateway is None:
        nav = _latest_nav()
        cfg = C.risk_config(nav)
        for problem in C.risk_coherence(cfg, nav):
            logging.warning("RISK LIMIT INCOHERENT: %s", problem)
        logging.info("risk limits: position<=Rs %,.0f gross<=Rs %,.0f dayloss<=Rs %,.0f"
                     .replace("%,", "%") % (cfg.max_position_value, cfg.max_gross_exposure,
                                            cfg.max_daily_loss))
        _risk = RiskManager(cfg)
        _gateway = OrderGateway(kite().kc, _risk)
    return _gateway


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    authed = False
    try:
        authed = kite().is_authed()
    except Exception:
        pass
    # Collection health belongs on the desk: a silently broken daily job costs history
    # that cannot be backfilled, and this is the page actually opened before trading.
    collection = None
    try:
        from .analytics import daily_runs as _dr, db as _db
        with _db.connect() as conn:
            _db.migrate(conn)
            collection = _dr.status(conn)
    except Exception:
        pass
    # Stop coverage, checked against the broker rather than assumed from the code path
    # that placed them. Needs a live session, so it degrades to None rather than blocking.
    protection = None
    if authed:
        try:
            from .analytics import protection as _prot
            protection = _prot.from_kite(kite())
        except Exception as exc:
            logging.warning("could not check stop coverage: %s", exc)

    # Starlette >=0.29 requires the request-first signature (the old
    # (name, {"request": ...}) form was removed in Starlette 1.x).
    return templates.TemplateResponse(request, "index.html",
                                      {"authed": authed, "dry_run": C.DRY_RUN,
                                       "collection": collection,
                                       "protection": protection})


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

    # Live prices for EVERY name the plan could touch, not just the ones already held.
    # A candidate priced from the scan CSV is sized and limit-priced one session stale at
    # best, and quantity is capital*weight/price — a wrong price is a wrong position size.
    # Kite takes up to 500 instruments per call; a scan plus a book is far inside that.
    candidates = [str(s) for s in scored.loc[scored["reject"] == "", "symbol"]]
    wanted = sorted({*candidates, *(h["symbol"] for h in holdings)})
    live: dict[str, float] = {}
    try:
        live = k.ltp(wanted)
    except Exception as exc:
        logging.warning("LTP fetch failed for the plan universe: %s", exc)
    for h in holdings:
        h["last_price"] = live.get(h["symbol"], h["last_price"])

    plan = build_plan(scored, holdings, cash, live_prices=live)
    plan["audit"] = scan_audit
    plan["created_at"] = time.time()
    PLANS[plan["plan_id"]] = plan

    # Persist the plan and stamp it with the regime decision it was built under, so the
    # book's actual movement can later be checked against the policy that asked for it.
    # A failure to record must not block the desk: the in-memory plan is still executable.
    try:
        from .analytics import db as _db, plan_store as _ps, regime_store as _rs
        with _db.connect() as conn:
            _db.migrate(conn)
            # The evaluation that was CURRENT when this plan was built, preview or
            # committed. In observe mode nothing is ever committed, so linking only to
            # committed decisions would leave every plan unstamped and the /regime
            # reconciliation panel permanently empty.
            ev = _rs.latest_evaluation(conn) if C.REGIME_ENABLED else None
            plan["evaluation_id"] = ev["evaluation_id"] if ev else None
            _ps.save_plan(conn, plan, evaluation_id=plan["evaluation_id"],
                          note=f"scan {scan.filename}")
    except Exception as exc:
        logging.warning("could not persist plan %s: %s", plan["plan_id"], exc)

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
    gw = gateway()

    # The daily-loss cap was inert: on_pnl() existed but nothing ever called it, so
    # day_pnl stayed 0 and the kill switch was manual-only. Measured here BEFORE any
    # order, so the change since the last EOD snapshot is pure mark-to-market rather
    # than the effect of today's own trading.
    nav_prev = _latest_nav()
    if nav_prev > 0 and _risk is not None:
        try:
            from .analytics import db as _db
            with _db.connect() as conn:
                rows = _db.snapshot_series(conn)
            prev_invested = float(rows[-1]["invested"]) if rows else 0.0
            if prev_invested > 0:
                _risk.on_pnl(float(plan.get("book_value") or 0.0) - prev_invested)
        except Exception as exc:
            logging.warning("could not evaluate the daily loss cap: %s", exc)

    results = []
    # Sells first (frees cash), then buys, then GTT stops. Unchanged sequence — only the
    # route changed: every order now goes through core/gateway.py, so guards -> risk ->
    # idempotency -> rate limits -> journal all apply. Previously this called
    # kite_client directly and skipped everything after the guards.
    ordered = sorted([o for o in plan["orders"] if o["delta"] != 0],
                     key=lambda o: (o["delta"] > 0, -abs(o["delta"] * o["ref_price"])))
    gross = float(plan.get("book_value") or 0.0)
    for o in ordered:
        side = "SELL" if o["delta"] < 0 else "BUY"
        try:
            # Pledged shares sell directly on Zerodha (instant-sale feature); collateral
            # margin reduces automatically — no unpledge gate needed.
            res = await gw.place(
                symbol=o["symbol"], qty=abs(o["delta"]), side=side, product="CNC",
                order_type="LIMIT", price=o["ref_price"], exchange="NSE",
                # Deterministic per plan+symbol, so re-posting a plan cannot double-send.
                client_id=f"{plan_id}:{o['symbol']}", gross_exposure=gross)
        except UntouchableInstrumentError as exc:
            # Caught per order: one protected instrument must not abort a batch that has
            # already placed real orders, leaving the book half-rebalanced.
            res = {"symbol": o["symbol"], "status": "BLOCKED", "error": str(exc)}
        res["action"] = o["action"]
        results.append(res)
        # No time.sleep here: it blocked the event loop, and the gateway's token buckets
        # already pace to Kite's caps and under SEBI's 10-OPS threshold.

    stops = []
    if place_stops == "true":
        for o in plan["orders"]:
            if o["qty_final"] > 0 and o.get("stop"):
                await gw.limits.api_slot()          # same limiter, no blocking sleep
                try:
                    stops.append(k.place_gtt_stop(o["symbol"], o["qty_final"], o["stop"],
                                                  o["ref_price"]))
                except UntouchableInstrumentError as exc:
                    stops.append({"symbol": o["symbol"], "status": "BLOCKED",
                                  "error": str(exc)})

    log_path = f"data/outputs/execution_{plan_id}.json"
    with open(log_path, "w") as f:
        json.dump({"plan": plan, "orders": results, "gtt": stops}, f, indent=2, default=str)

    # Fold the outcome back onto the stored plan. A JSON file records what happened; the
    # database is what lets /regime ask whether the policy was actually implemented, and
    # what lets slippage be measured against the price the plan assumed.
    recon = {}
    try:
        from .analytics import db as _db, plan_store as _ps
        with _db.connect() as conn:
            _db.migrate(conn)
            _ps.record_execution(conn, plan_id, results)
            recon = _ps.reconciliation(conn, plan_id)
    except Exception as exc:
        logging.warning("could not record execution for %s: %s", plan_id, exc)

    return JSONResponse({"dry_run": C.DRY_RUN, "orders": results, "gtt": stops,
                         "log": log_path, "reconciliation": recon})


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
    from .analytics import backtest_view as _bv
    return templates.TemplateResponse(request, "regime_backtest.html",
                                      {"b": data, "path": path,
                                       "charts": _bv.build(data)})


@app.get("/regime/data")
def regime_data():
    from .analytics import db as _db, regime_view as _rv
    with _db.connect() as conn:
        _db.migrate(conn)
        return JSONResponse(_rv.build(conn))


# --- operations ---------------------------------------------------------------------------
@app.get("/ops", response_class=HTMLResponse)
def ops_page(request: Request, started: str = "", error: str = ""):
    from .analytics import daily_runs as _dr, db as _db, ops as _ops
    with _db.connect() as conn:
        _db.migrate(conn)
        ctx = _ops.view(conn)
        ctx["collection"] = _dr.status(conn)
        ctx["daily_history"] = _dr.history(conn, limit=10)
    ctx.update({"started": started, "error": error})
    return templates.TemplateResponse(request, "ops.html", ctx)


@app.post("/ops/run")
async def ops_run(request: Request):
    """Start one allowlisted operation. Never a free-form command."""
    from .analytics import db as _db, ops as _ops
    form = await request.form()
    name = str(form.get("op") or "")
    values = {k: v for k, v in form.items() if k != "op"}
    try:
        with _db.connect() as conn:
            _db.migrate(conn)
            res = _ops.start(conn, name, values)
        return RedirectResponse(f"/ops?started={res['name']}", status_code=303)
    except _ops.OpsError as exc:
        return RedirectResponse(f"/ops?error={exc}", status_code=303)


@app.get("/ops/data")
def ops_data():
    from .analytics import db as _db, ops as _ops
    with _db.connect() as conn:
        _db.migrate(conn)
        return JSONResponse({"running": _ops.running_job(conn),
                             "history": _ops.history(conn),
                             "operations": [{"name": o.name, "label": o.label,
                                             "group": o.group, "cli": o.cli()}
                                            for o in _ops.OPERATIONS]})


@app.get("/ops/job/{job_id}", response_class=HTMLResponse)
def ops_job(request: Request, job_id: int):
    from .analytics import db as _db
    with _db.connect() as conn:
        _db.migrate(conn)
        row = conn.execute("SELECT * FROM ops_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Unknown job")
    return templates.TemplateResponse(request, "ops_job.html", {"j": dict(row)})


# --- performance -------------------------------------------------------------------------
@app.get("/performance", response_class=HTMLResponse)
def performance_page(request: Request):
    from .analytics import db as _db, performance_view as _pv
    with _db.connect() as conn:
        _db.migrate(conn)
        view = _pv.build(conn)
    return templates.TemplateResponse(request, "performance.html", {"p": view})


@app.get("/performance/data")
def performance_data():
    from .analytics import db as _db, performance_view as _pv
    with _db.connect() as conn:
        _db.migrate(conn)
        return JSONResponse(_pv.build(conn))


# --- tradebook ---------------------------------------------------------------------------
def _snapshot_positions() -> list[dict]:
    """Strategy positions from the latest stored snapshot. No broker call."""
    try:
        from .analytics import db as _db
        with _db.connect() as conn:
            _db.migrate(conn)
            rows = _db.snapshot_series(conn)
            if not rows:
                return []
            snap = _db.get_snapshot(conn, rows[-1]["date"])
        return [p for p in json.loads(snap["holdings_json"]).get("positions", [])
                if not p.get("excluded")]
    except Exception:
        return []


@app.get("/tradebook", response_class=HTMLResponse)
def tradebook_page(request: Request, imported: str = "", error: str = ""):
    from .analytics import db as _db, tradebook as _tb
    positions = _snapshot_positions()
    with _db.connect() as conn:
        _db.migrate(conn)
        st = _tb.status(conn, positions)
    return templates.TemplateResponse(request, "tradebook.html",
                                      {"s": st, "f": st["fills"],
                                       "imported": imported, "error": error,
                                       "have_snapshot": bool(positions)})


@app.post("/tradebook")
async def tradebook_upload(file: UploadFile):
    """Import a Console tradebook export. Rebuilds lots for the symbols it covers."""
    import os as _os
    from .analytics import db as _db, tradebook as _tb
    _os.makedirs("data/uploads", exist_ok=True)
    dest = f"data/uploads/tradebook_{int(time.time())}.csv"
    with open(dest, "wb") as f:
        f.write(await file.read())
    try:
        with _db.connect() as conn:
            _db.migrate(conn)
            res = _tb.import_tradebook(conn, dest)
        msg = (f"{res['fills']} fills, {res['symbols']} symbols, "
               f"{res['open_lots']} open lots, {res['closed_trades']} closed "
               f"({res['first_trade']} to {res['last_trade']})")
        if res["unmatched_sells"]:
            msg += f" — {len(res['unmatched_sells'])} unmatched sells"
        return RedirectResponse(f"/tradebook?imported={msg}", status_code=303)
    except _tb.TradebookError as exc:
        return RedirectResponse(f"/tradebook?error={exc}", status_code=303)


@app.get("/tradebook/data")
def tradebook_data():
    from .analytics import db as _db, tradebook as _tb
    with _db.connect() as conn:
        _db.migrate(conn)
        return JSONResponse(_tb.status(conn, _snapshot_positions()))


# --- settings ---------------------------------------------------------------------------
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = "", error: str = ""):
    from .analytics import db as _db, settings as _st
    with _db.connect() as conn:
        _db.migrate(conn)
        ctx = {"eff": _st.effective(conn), "groups": _st.GROUPS,
               "locked": _st.locked_view(), "history": _st.history(conn, 25),
               "saved": saved, "error": error, "dry_run": C.DRY_RUN,
               "risk": _st.risk_preview(_latest_nav())}
    return templates.TemplateResponse(request, "settings.html", ctx)


@app.post("/settings")
async def settings_save(request: Request):
    """Validate, persist, audit and apply. Nothing is written unless everything passes."""
    from .analytics import db as _db, settings as _st
    form = await request.form()
    note = str(form.get("note") or "").strip()
    updates = {k: v for k, v in form.items()
               if k in _st.BY_KEY and k not in _st.SECRET_KEYS}
    # An unchecked box is absent from a form post, so absence means false — but ONLY for
    # the checkboxes this form actually rendered. Without the marker a partial API post
    # would silently switch every boolean off.
    rendered = str(form.get("_form_bools") or "")
    for key in (k.strip() for k in rendered.split(",") if k.strip()):
        spec = _st.BY_KEY.get(key)
        if spec is not None and spec.kind == "bool" and key not in updates:
            updates[key] = "false"
    try:
        with _db.connect() as conn:
            _db.migrate(conn)
            res = _st.save(conn, updates, note=note)
        msg = (f"{len(res['changed'])} changed: {', '.join(res['changed'])}"
               if res["changed"] else "no changes")
        return RedirectResponse(f"/settings?saved={msg}", status_code=303)
    except _st.SettingsError as exc:
        return RedirectResponse(f"/settings?error={exc}", status_code=303)


@app.post("/settings/reset")
async def settings_reset(request: Request):
    from .analytics import db as _db, settings as _st
    form = await request.form()
    keys = [k for k in form.getlist("key")] or None
    with _db.connect() as conn:
        _db.migrate(conn)
        res = _st.reset(conn, keys)
    return RedirectResponse(
        f"/settings?saved=reset {len(res['reset'])} override(s)", status_code=303)


@app.get("/settings/data")
def settings_data():
    from .analytics import db as _db, settings as _st
    with _db.connect() as conn:
        _db.migrate(conn)
        eff = _st.effective(conn)
        return JSONResponse({
            "settings": {k: {"value": v["text"], "source": v["source"],
                             "group": v["spec"].group, "label": v["spec"].label}
                         for k, v in eff.items()},
            "locked": [{"key": r["key"], "value": str(r["value"])}
                       for r in _st.locked_view()],
            "history": _st.history(conn, 25)})


@app.get("/status")
def status():
    try:
        k = kite()
        return {"authed": k.is_authed(), "dry_run": C.DRY_RUN,
                "cash": k.available_cash() if k.is_authed() else None}
    except Exception as exc:
        return {"authed": False, "dry_run": C.DRY_RUN, "error": str(exc)}
