"""Webview + API. Flow: /login → upload scan → /analyze (plan) → review → /execute (confirm-gated).
Execution NEVER happens without confirm=true + a live plan_id from this session."""
from __future__ import annotations
import asyncio
import datetime as dt
import io
import json
import logging
import pathlib
import time
from fastapi import FastAPI, UploadFile, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
try:                       # ultra-fast serialization when available
    from fastapi.responses import ORJSONResponse as JSONResponse
except Exception:
    from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import pandas as pd

from . import config as C
from .core.gateway import OrderGateway
from .core.gateway import FAILED_STATUSES as _FAILED_STATUSES
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
app = FastAPI(title="Kite Momentum Rebalancer", default_response_class=JSONResponse,
              # The interactive docs enumerate every route, including the ones that place
              # orders. Off unless asked for.
              docs_url="/docs" if C.DESK_DOCS else None,
              redoc_url="/redoc" if C.DESK_DOCS else None,
              openapi_url="/openapi.json" if C.DESK_DOCS else None)

from .core.websec import DeskSecurity                     # noqa: E402
from .core.websec import check_secret_permissions as _secret_perm_check  # noqa: E402

app.add_middleware(DeskSecurity)

# Credential files are tightened at startup rather than trusted to a deploy step, because
# the deploy step is the thing most likely to be skipped.
for _bad in _secret_perm_check(fix=True):
    logging.warning("tightened %s from %s (%s)", _bad["file"], _bad["mode"], _bad["detail"])
templates = Jinja2Templates(directory="app/templates")

# The same directory ops.py confines its file parameters to, so a retained scan is
# reachable by the operations page and nothing else can be.
UPLOAD_DIR = pathlib.Path("data/uploads")

# The stylesheet is BUILT and VENDORED (app/static/app.css, from src.css via the Tailwind
# CLI) rather than pulled from a CDN. The desk has to render while an order is being
# confirmed, and an unstyled page at that moment is a real failure, not a cosmetic one.
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Cache-bust on content, so a rebuilt stylesheet is picked up without a hard reload and an
# unchanged one still caches.
def _asset_version() -> str:
    import hashlib
    import pathlib as _pl
    try:
        return hashlib.md5(_pl.Path("app/static/app.css").read_bytes()).hexdigest()[:8]
    except OSError:
        return "dev"


ASSET_V = _asset_version()
templates.env.globals["asset_v"] = ASSET_V
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
def home(request: Request, logged_in: str = "", autorun: str = "", note: str = ""):
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
    # What the post-login collection is doing, or did. Read from the ops journal rather
    # than remembered, so a page reload after the job finishes still shows the outcome.
    auto = None
    try:
        from .analytics import db as _db, ops as _ops
        with _db.connect() as conn:
            running = _ops.running_job(conn)
            last = (_ops.last_run(conn) or {}).get("autorun")
            if (logged_in or autorun) or (running and running["name"] == "autorun") or last:
                auto = {"just_logged_in": bool(logged_in), "note": note,
                        "running": running if running and running["name"] == "autorun" else None,
                        "last": last}
    except Exception:                                              # noqa: BLE001
        pass

    return templates.TemplateResponse(request, "index.html",
                                      {"authed": authed, "dry_run": C.DRY_RUN,
                                       "collection": collection, "auto": auto,
                                       "protection": protection})


@app.get("/login")
def login():
    return RedirectResponse(kite().login_url())


@app.get("/callback")
def callback(request_token: str = ""):
    """Exchange the token, then collect whatever the day still owes.

    Logging in is the moment the blocker clears. Both daily jobs need a token that expires
    overnight with no refresh, and on 17-18 Aug 2026 both exited 2 for want of one — two
    sessions of straddle observations and an EOD snapshot lost, none of it recoverable.
    Waiting for the next scheduled fire would have lost another day.

    The run is started, not awaited: a login must not hang on a fifteen-minute collection,
    and the ops page already shows a running job.
    """
    if not request_token:
        raise HTTPException(400, "Missing request_token")
    kite().exchange_token(request_token)

    started, why = "", ""
    try:
        from .analytics import db as _db, ops as _ops
        with _db.connect() as conn:
            _db.migrate(conn)
            res = _ops.start(conn, "autorun")
            started, why = res["name"], "post-login collection"
    except Exception as exc:                                       # noqa: BLE001
        # A failed autostart must never cost the login itself, which is the one thing the
        # user came here to do.
        logging.warning("post-login autorun did not start: %s", exc)
        why = str(exc)[:120]
    return RedirectResponse(f"/?logged_in=1&autorun={started}&note={why}")


async def _funding_check(k, orders) -> dict:
    """What the whole plan needs in margin, against what is available.

    A basket margin query — the same POST an order uses, so it exercises the same
    permissions — and the number that predicts a rejection before one happens. Run at
    ANALYSE time as well as at execute: knowing the plan is short after the batch has
    half-filled is knowing it too late. On 19 Aug it was measured only at execute, shown
    nowhere, and SHILPAMED was refused six times for the shortfall it had already found.
    """
    basket = [{"exchange": "NSE", "tradingsymbol": o["symbol"],
               "transaction_type": "SELL" if o["delta"] < 0 else "BUY",
               "variety": "regular", "product": "CNC", "order_type": "MARKET",
               "quantity": abs(int(o["delta"]))}
              for o in orders if o["delta"] != 0]
    if not basket:
        return {"checked": False, "reason": "nothing to fund"}
    try:
        resp = await asyncio.to_thread(k.kc.basket_order_margins, basket)
        need = float(((resp or {}).get("final") or {}).get("total") or 0.0)
        have = float(k.available_cash() or 0.0)
        return {"checked": True, "required": round(need), "available": round(have),
                "shortfall": round(max(0.0, need - have))}
    except Exception as exc:                                       # noqa: BLE001
        # Reported, never fatal: a margin endpoint that is down must not block a plan.
        return {"checked": False, "error": str(exc)[:200]}


@app.post("/analyze")
async def analyze(scan: UploadFile):
    """Score the uploaded scan, pull live holdings + cash, return the full plan."""
    raw = await scan.read()
    df = load_scan(io.BytesIO(raw))
    # Kept, not discarded. The plan already recorded WHICH scan built it; without the file
    # itself that name cannot be resolved back to the numbers, and the holdings page has
    # no current market-cap or beta context — 91% of the book came back "unclassified"
    # because the only stored scan was the bundled sample.
    try:
        import re as _re
        safe = _re.sub(r"[^A-Za-z0-9._-]", "_", scan.filename or "scan.csv")
        dest = UPLOAD_DIR / f"scan_{int(time.time())}_{safe}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
    except Exception as exc:                                       # noqa: BLE001
        logging.warning("could not retain the uploaded scan: %s", exc)
    scan_audit = audit(df)
    scored = score(df)

    k = kite()
    if not k.is_authed():
        raise HTTPException(401, "Kite session expired — click Login first.")
    # No filtering here any more. build_plan applies core/guards.assert_tradeable itself,
    # so every caller gets the same protection rather than only this route. The SGB* test
    # that used to live on this line is what kept /analyze safe while the planner was not.
    holdings = k.holdings()
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
    # Before you confirm, not after the batch is half sent.
    plan["funding"] = await _funding_check(k, plan["orders"])
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

    # --- preflight ---------------------------------------------------------------------
    # A basket margin query before anything is sent. It is a POST to an order-adjacent
    # endpoint, so it exercises the same permissions the orders will need, and it returns
    # the margin the whole plan requires — which is the check that would have caught
    # SAILIFE failing for Rs 802.67 after twelve other orders had already gone out.
    # Re-checked here because cash moves between analysis and confirmation, and because
    # this is the last moment it can still stop something.
    preflight = await _funding_check(k, plan["orders"])

    results = []
    # --- circuit breaker -----------------------------------------------------------------
    # On 18 Aug 2026 all twenty-one orders were fired into the same rejection — "No IPs
    # configured for this app" — because nothing noticed that the first three had failed
    # identically. A systemic refusal does not become less systemic on the fourth attempt,
    # and each one still consumes a rate-limit slot and a journal line.
    breaker_trip = 3
    recent_errors: list[str] = []
    aborted_for = ""
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

        # From the gateway's own vocabulary, not a hand-written pair. RISK_BLOCKED was
        # missing, so a tripped loss cap or an exposure limit would refuse every order
        # individually and the breaker would never see it.
        err = (str(res.get("error") or "")[:80]
               if res.get("status") in _FAILED_STATUSES else "")
        recent_errors = (recent_errors + [err])[-breaker_trip:] if err else []
        if len(recent_errors) == breaker_trip and len(set(recent_errors)) == 1:
            aborted_for = recent_errors[0]
            for rest in ordered[ordered.index(o) + 1:]:
                results.append({"symbol": rest["symbol"], "status": "ABORTED",
                                "action": rest["action"],
                                "error": f"batch stopped after {breaker_trip} identical "
                                         f"failures: {aborted_for}"})
            break
        # No time.sleep here: it blocked the event loop, and the gateway's token buckets
        # already pace to Kite's caps and under SEBI's 10-OPS threshold.

    # STOPS ARE NOT ARMED HERE ANY MORE, and that is the fix for a real loss of protection.
    #
    # This used to place a GTT for each order's qty_final immediately after submitting the
    # batch — before any fill was known. So every trigger was sized to the PLANNED position
    # rather than the actual one. On 18 Aug 2026 that left the book carrying 10,383 shares
    # of GTT against 9,478 held: SONACOMS covered 2.5x what was owned, RADICO 3.1x, and a
    # 438-share stop sat on PARAS before a single share of it had filled. An over-covered
    # trigger sells shares you do not own when it fires, which is short delivery.
    #
    # Fills are not knowable at this instant — these are LIMIT orders and some will rest.
    # So arming moves to /stops, which sizes every trigger from the broker's own holdings
    # and can also cancel the wrong ones. Rule 4 still holds: every buy gets a stop the
    # same session. It just happens after the fills, which is the only time it can be right.
    stops: list[dict] = []
    stops_note = ("stops are armed from /stops once fills are known; sizing them here "
                  "would use planned quantities, not filled ones")

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
                         "stops_note": stops_note, "stops_pending": True,
                         "preflight": preflight, "aborted_for": aborted_for,
                         "log": log_path, "reconciliation": recon})


# --- arming stops ----------------------------------------------------------------------
# Same shape as /analyze -> /execute, and for the same reason: proposing and doing are
# separate acts. A status check found the gap; nothing arms a stop without being told to.
STOP_PLANS: dict[str, dict] = {}
# The statuses kite_client.place_gtt_stop returns on success, named here so the two
# cannot drift apart silently. Anything else is a failure and is shown with its reason.
STOP_OK = frozenset({"GTT_PLACED", "DRY_RUN_GTT"})


@app.get("/options", response_class=HTMLResponse)
def options_page(request: Request, started: str = "", error: str = ""):
    """The options lab: paper-only, read-only, no session required.

    Deliberately needs no Kite login. Its whole job is to say what the program is and is
    not doing, and a page that could only answer that while authenticated would be
    unavailable at exactly the moment someone wondered.
    """
    from .analytics import db as _db, options_view as _ov
    with _db.connect() as conn:
        _db.migrate(conn)
        p = _ov.page(conn)
    return templates.TemplateResponse(request, "options.html",
                                      {"p": p, "started": started, "error": error})


@app.post("/options/run")
async def options_run(request: Request):
    """Start one allowlisted options operation and come back to this page.

    The same fixed allowlist as /ops — the browser sends an operation NAME, never a
    command — and restricted further to the options group, so this route cannot start the
    equity jobs even if the form is edited.
    """
    from .analytics import db as _db, ops as _ops, options_view as _ov
    form = await request.form()
    name = str(form.get("op") or "")
    if name not in _ov.STRANGLE_OPS:
        return RedirectResponse(f"/options?error={name or 'no operation'} is not an "
                                "options operation", status_code=303)
    values = {k: v for k, v in form.items() if k != "op"}
    try:
        with _db.connect() as conn:
            _db.migrate(conn)
            res = _ops.start(conn, name, values)
        return RedirectResponse(f"/options?started={res['name']}", status_code=303)
    except _ops.OpsError as exc:
        return RedirectResponse(f"/options?error={exc}", status_code=303)


@app.get("/options/data")
def options_data():
    from .analytics import db as _db, options_view as _ov
    with _db.connect() as conn:
        _db.migrate(conn)
        return _ov.page(conn)


@app.get("/reconcile", response_class=HTMLResponse)
def reconcile_page(request: Request, plan_id: str = ""):
    """Plan against broker. The broker is the authority; this reports disagreement.

    Also RECONCILES fills as a side effect of loading, because a comparison that shows
    stale local state and does nothing about it is a worse version of the problem.
    """
    from .analytics import db as _db, plan_store as _ps, reconcile_view as _rv

    view = {"available": False, "reason": "not authenticated"}
    try:
        k = kite()
        if k.is_authed():
            orders = k.kc.orders() or []
            gtts = k.kc.get_gtts() or []
            holdings = k.holdings()
            with _db.connect() as conn:
                _db.migrate(conn)
                pid = plan_id or (_ps.latest(conn) or {}).get("version_id")
                settled = _ps.reconcile_fills(conn, pid, orders) if pid else {}
                plan = PLANS.get(pid)
                view = _rv.build(conn, plan_id=pid, plan=plan, broker_orders=orders,
                                 gtts=gtts, holdings=holdings)
                view["available"] = True
                view["settled"] = settled
    except Exception as exc:                                       # noqa: BLE001
        logging.warning("reconcile failed: %s", exc)
        view = {"available": False, "reason": str(exc)[:200]}
    return templates.TemplateResponse(request, "reconcile.html", {"v": view})


@app.get("/reconcile/data")
def reconcile_data(plan_id: str = ""):
    from .analytics import db as _db, plan_store as _ps, reconcile_view as _rv
    k = kite()
    if not k.is_authed():
        return {"available": False, "reason": "not authenticated"}
    orders, gtts, holdings = k.kc.orders() or [], k.kc.get_gtts() or [], k.holdings()
    with _db.connect() as conn:
        _db.migrate(conn)
        pid = plan_id or (_ps.latest(conn) or {}).get("version_id")
        if pid:
            _ps.reconcile_fills(conn, pid, orders)
        return {**_rv.build(conn, plan_id=pid, plan=PLANS.get(pid),
                            broker_orders=orders, gtts=gtts, holdings=holdings),
                "available": True}


@app.get("/stops", response_class=HTMLResponse)
def stops_page(request: Request, armed: str = "", error: str = ""):
    """Review the stops that would be armed. Builds a plan; places nothing."""
    from .analytics import protection as _prot

    plan, review, problem = None, None, ""
    try:
        k = kite()
        if not k.is_authed():
            problem = "Kite session expired — log in first. Stops are sized from live prices."
        else:
            plan = _prot.build_stop_plan(k.holdings(), k.kc.get_gtts() or [])
            plan["created_at"] = time.time()
            STOP_PLANS[plan["plan_id"]] = plan
            review = plan["review"]
    except Exception as exc:
        problem = f"Could not read holdings or triggers: {exc}"

    return templates.TemplateResponse(
        request, "stops.html",
        {"plan": plan, "review": review, "problem": problem, "dry_run": C.DRY_RUN,
         "armed": armed, "error": error,
         "band": (C.STOP_MIN * 100, C.STOP_MAX * 100),
         "daily_loss_cap": C.risk_config(_latest_nav()).max_daily_loss})


@app.post("/stops/arm")
async def stops_arm(plan_id: str = Form(...), confirm: str = Form(...)):
    """Arm the reviewed stops. Same gates as /execute: explicit confirm, a plan issued by
    the review page, and a freshness limit because a trigger is priced off a live quote."""
    if confirm != "true":
        raise HTTPException(400, "Arming stops requires explicit confirmation.")
    plan = STOP_PLANS.get(plan_id)
    if not plan:
        raise HTTPException(404, "Unknown or expired plan — reload the review page.")
    if time.time() - plan["created_at"] > 1800:
        raise HTTPException(410, "Plan older than 30 minutes — prices stale, reload it.")

    k = kite()
    gw = gateway()

    # Cancels FIRST. An over-covered symbol has every trigger removed before the correct
    # one is armed, so the two can never both be live — and cancelling is the half that
    # reduces risk, so it is the half that must not be skipped if something fails later.
    cancelled = []
    for c in plan.get("cancels", []):
        await gw.limits.api_slot()
        try:
            res = k.delete_gtt(c["gtt_id"], c["symbol"])
        except Exception as exc:                                  # noqa: BLE001
            res = {"symbol": c["symbol"], "gtt_id": c["gtt_id"],
                   "status": "GTT_DELETE_ERROR", "error": str(exc)}
        cancelled.append({**res, "reason": c["reason"]})

    placed = []
    for r in plan["rows"]:
        # The gateway has no GTT method, so this uses kite_client.place_gtt_stop, which
        # carries its own untouchable-instrument guard. The gateway's limiter still paces
        # the calls so a batch of stops cannot outrun Kite's caps.
        await gw.limits.api_slot()
        try:
            res = k.place_gtt_stop(r["symbol"], r["qty"], r["trigger"], r["last_price"])
        except UntouchableInstrumentError as exc:
            res = {"symbol": r["symbol"], "status": "BLOCKED", "error": str(exc)}
        except Exception as exc:
            # One rejection must not abandon the rest of the book unprotected.
            res = {"symbol": r["symbol"], "status": "FAILED", "error": str(exc)}
        placed.append({**res, "qty": r["qty"], "trigger": r["trigger"]})

    STOP_PLANS.pop(plan_id, None)          # single use: re-arming needs a fresh review
    log_path = f"data/outputs/stops_{plan_id}.json"
    with open(log_path, "w") as f:
        json.dump({"plan": plan["rows"], "cancels": plan.get("cancels", []),
                   "cancelled": cancelled, "results": placed}, f, indent=2, default=str)

    # Success is whatever kite_client actually returns, matched from ITS constants rather
    # than a guessed list. The first live run placed 16 triggers and reported "0 armed,
    # 17 failed" because GTT_PLACED was not in a hand-written set — and a false failure
    # invites a re-arm, which is how a position ends up with two stops.
    ok = [p for p in placed if str(p.get("status", "")) in STOP_OK]
    failed = [p for p in placed if p not in ok]
    msg = f"{len(ok)} of {len(placed)} stops {'simulated' if C.DRY_RUN else 'armed'}"
    if cancelled:
        gone = [c for c in cancelled
                if str(c.get("status")) in ("GTT_DELETED", "DRY_RUN_GTT_DELETE")]
        msg = f"{len(gone)} of {len(cancelled)} wrong triggers cancelled · " + msg
    if failed:
        msg += (f" · {len(failed)} failed: "
                + ", ".join(f"{p['symbol']} ({p.get('error', 'unknown')[:60]})"
                            for p in failed[:3]))
    return RedirectResponse(f"/stops?armed={msg}", status_code=303)


# --- index board (read-only) -------------------------------------------------------------
# The market side of the screener: every NSE index, and what the desk's own scan thinks of
# the names inside one. Read-only in the strongest sense — it reads NSE's public data and
# Kite quotes, and has no path to an order. Failures degrade to a stale or empty panel
# rather than a 500, because this page is also where you look when something else is down.
@app.get("/indices", response_class=HTMLResponse)
def indices_page(request: Request, index: str = ""):
    from .analytics import index_view as _iv
    return templates.TemplateResponse(request, "indices.html", {"v": _iv.build(index)})


@app.get("/indices/data")
def indices_data(index: str = ""):
    from .analytics import index_view as _iv
    return JSONResponse(_iv.build(index))


@app.get("/indices/constituents")
def indices_constituents(index: str):
    """Who is in this index, with a live quote and the screen's score for each name.

    The Kite client is built best-effort: an expired token (which is every morning, since
    Kite issues no refresh) must cost the price columns, not the panel.
    """
    from .analytics import index_view as _iv
    k = None
    try:
        k = kite()
    except Exception as e:                                       # noqa: BLE001
        logging.getLogger("main").info(
            "indices/constituents without a broker session: %s", e)
    return JSONResponse(_iv.drilldown(index, k))


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
def _holdings_block() -> dict:
    """The live holdings table for the portfolio page.

    Best-effort by design: the performance record is stored and always renders, while this
    needs a broker session, the GTT book and an uploaded scan. A page that 500s because
    the token expired would take the whole track record down with it.
    """
    from .analytics import holdings_view as _hv
    out: dict = {"available": False, "gaps": _hv.GAPS}
    try:
        k = kite()
        if not k.is_authed():
            return {**out, "reason": "not logged in"}
        held, raw = k.holdings(), k.kc.holdings()
        cash = k.available_cash()
    except Exception as exc:                                       # noqa: BLE001
        return {**out, "reason": str(exc)[:160]}

    # Stop cover, named per symbol, from the same review the /stops page runs.
    protected: dict[str, str] = {}
    try:
        from .analytics import protection as _p
        rev = _p.review(held, k.kc.get_gtts())
        protected = {f["symbol"]: f["kind"] for f in rev["findings"]}
        for h in held:
            protected.setdefault(h["symbol"], "ok")
    except Exception as exc:                                       # noqa: BLE001
        logging.warning("stop cover unavailable for the holdings table: %s", exc)

    scan: dict = {}
    try:
        import glob
        import os

        from .scoring import load_scan
        files = [f for f in glob.glob("data/uploads/*.csv") if "tradebook" not in f.lower()]
        if files:
            df = load_scan(sorted(files, key=os.path.getmtime)[-1])
            scan = {r["symbol"]: r for r in df.to_dict("records")}
    except Exception as exc:                                       # noqa: BLE001
        logging.warning("scan context unavailable for the holdings table: %s", exc)

    fills: list = []
    try:
        from .analytics import db as _db
        with _db.connect() as conn:
            fills = [dict(r) for r in conn.execute(
                "SELECT symbol, side, when_ts FROM fills")]
    except Exception as exc:                                       # noqa: BLE001
        logging.warning("fill history unavailable for the holdings table: %s", exc)

    rows = _hv.rows(held, raw=raw, scan=scan, fills=fills, protected=protected)
    summary = _hv.summary(rows, cash=cash)
    return {"available": True, "rows": rows, "summary": summary,
            "allocation": _hv.allocation(rows, cash=cash),
            "warnings": _hv.warnings(rows, summary),
            "scan_context": bool(scan), "gaps": _hv.GAPS,
            "as_of": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}


@app.get("/performance", response_class=HTMLResponse)
def performance_page(request: Request):
    from .analytics import db as _db, performance_view as _pv
    with _db.connect() as conn:
        _db.migrate(conn)
        view = _pv.build(conn)
    return templates.TemplateResponse(request, "performance.html",
                                      {"p": view, "h": _holdings_block()})


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
        from .analytics import corporate_actions as _ca
        actions = _ca.listing(conn)
    return templates.TemplateResponse(request, "tradebook.html",
                                      {"s": st, "f": st["fills"], "actions": actions,
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


@app.post("/tradebook/corporate-action")
async def record_corporate_action(symbol: str = Form(...), kind: str = Form(...),
                                  ex_date: str = Form(...), ratio_new: str = Form(...),
                                  ratio_old: str = Form(...), note: str = Form("")):
    """Record a bonus or split, then rebuild that symbol's lots.

    Lots are derived, so recording the action and rebuilding is the whole operation —
    there is no adjusted quantity stored anywhere that would need migrating.
    """
    from .analytics import corporate_actions as _ca, db as _db, tradebook as _tb
    try:
        action = _ca.parse(symbol, kind, ex_date, ratio_new, ratio_old, note)
        with _db.connect() as conn:
            _db.migrate(conn)
            _ca.record(conn, action)
            res = _tb.rebuild_symbols(conn, [action.symbol])
        msg = (f"{action.symbol}: {action.describe()} · rebuilt to "
               f"{res['open_lots']} open lots")
        return RedirectResponse(f"/tradebook?imported={msg}", status_code=303)
    except _ca.CorporateActionError as exc:
        return RedirectResponse(f"/tradebook?error={exc}", status_code=303)


@app.post("/tradebook/corporate-action/delete")
async def delete_corporate_action(action_id: str = Form(...), symbol: str = Form(...)):
    from .analytics import corporate_actions as _ca, db as _db, tradebook as _tb
    with _db.connect() as conn:
        _db.migrate(conn)
        _ca.remove(conn, int(action_id))
        _tb.rebuild_symbols(conn, [symbol])       # derived, so removal self-heals
    return RedirectResponse(f"/tradebook?imported=removed, {symbol} lots rebuilt",
                            status_code=303)


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
def status(ip: str = ""):
    """Session state. `?ip=1` also reports the address the broker will see.

    That is the one thing a rejected batch turns on and the one thing you cannot read off
    this machine: Kite authorises orders against an allowlist, and an order refused for a
    disallowed IP names an address you have to go and look up. Behind a flag because it
    costs an outbound call, and /status is polled.
    """
    out = {"dry_run": C.DRY_RUN, "force_ipv4": C.FORCE_IPV4}
    if ip:
        from .core.net import outbound_ip
        out["outbound_ip"] = outbound_ip()
    try:
        k = kite()
        out |= {"authed": k.is_authed(),
                "cash": k.available_cash() if k.is_authed() else None}
    except Exception as exc:
        out |= {"authed": False, "error": str(exc)}
    return out
