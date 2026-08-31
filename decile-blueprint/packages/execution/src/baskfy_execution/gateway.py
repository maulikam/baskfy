"""Async order gateway — the ONLY module allowed to place/modify/cancel orders.
Enforces: instrument guards → risk manager → rate limits → idempotency → journal.
Sync kiteconnect calls run in a thread pool so the event loop never blocks."""
from __future__ import annotations
import asyncio, json, os, time, uuid, logging
from dataclasses import dataclass
from typing import Callable
from .guards import (OvernightOptionError, assert_not_overnight_option,
                     assert_tradeable)
from .gtt import (DEFAULT_STOP_BAND, DRY_RUN_GTT, DRY_RUN_GTT_DELETE, GTT_DELETE_ERROR,
                  GTT_DELETED, GTT_ERROR, GTT_LIMIT_FRACTION, GTT_PLACED, StopBand,
                  TickSizes, band_finding, drop_pct, gtt_params, kill_switch_reason,
                  refuse_stop, to_tick)
from .ratelimit import KiteLimits
from .risk import RiskManager
from .tenancy import TenantIds, refuse_cross_tenant

log = logging.getLogger("gateway")
JOURNAL = "data/outputs/orders_journal.jsonl"


@dataclass(frozen=True)
class ProductGates:
    """The three switches that decide whether a real order can be placed at all.

    FAIL CLOSED, and that is the whole design. Every default here refuses: simulate rather
    than place, cash equity only, no F&O. A caller that forgets to supply gates gets the
    safest configuration rather than the most permissive one, which is the opposite of what
    a default usually does and the only sane choice for the module that talks to a broker.

    Non-negotiable 5 (CLAUDE.md): "MIS needs INTRADAY_ENABLED, NFO/BFO needs OPTIONS_ENABLED;
    both default off, enforced inside the gateway."
    """

    dry_run: bool = True
    intraday_enabled: bool = False
    options_enabled: bool = False


# Statuses that mean the order did NOT reach the exchange, and say why. The circuit
# breaker in /execute counts consecutive identical failures against this set, so a status
# missing from it is a systemic refusal the breaker cannot see: RISK_BLOCKED was absent,
# and a tripped loss cap would have refused all twenty-one orders one at a time — the exact
# shape of the 18 Aug "No IPs configured" batch the breaker was built after.
#
# DUPLICATE and DRY_RUN are deliberately NOT here: neither is a failure, and neither
# carries an error to compare.
FAILED_STATUSES: frozenset[str] = frozenset({"ERROR", "REJECTED", "BLOCKED",
                                             "RISK_BLOCKED"})

# Kite's own exception taxonomy tells you whether the order got anywhere. A refusal it
# names — bad margin, a disallowed IP, an invalid parameter, an expired token — was
# decided BEFORE the exchange saw anything, so the order definitively does not exist. A
# network failure or a timeout is the only case where it might.
#
# Collapsing both into one ERROR made the report warn "an order may still have been
# accepted" after every failure, including six SHILPAMED rejections that each said
# "Insufficient funds" in plain words. The warning is the right one to give when the
# outcome is unknown and the wrong one to give when it is not: it sends you to the order
# book to rule out a double-send that was never possible.
_DEFINITIVE_REFUSALS = ("InputException", "OrderException", "PermissionException",
                        "TokenException")


class OrderGateway:
    def __init__(self, kc, risk: RiskManager, *,
                 gates: Callable[[], ProductGates] = ProductGates,
                 journal_path: str = JOURNAL,
                 stop_band: StopBand | None = None):
        self.kc, self.risk, self.limits = kc, risk, KiteLimits()
        self._sent: dict[str, str] = {}   # client_id -> broker order_id (idempotency)
        # A SEPARATE idempotency map for GTTs, and it must stay separate. The desk builds
        # client ids as `plan_id:symbol` for orders; a stop armed for the same plan and
        # symbol would collide with the buy that created it and be reported DUPLICATE —
        # a silently skipped stop, which is the one failure this whole path exists to
        # prevent.
        self._gtt_sent: dict[str, object] = {}   # client_id -> broker trigger_id
        self._ticks = TickSizes()
        self._stop_band = stop_band or DEFAULT_STOP_BAND
        # A CALLABLE, not a value, and deliberately: the desk reads DRY_RUN off its config
        # module at the moment of the order, so a setting changed mid-session takes effect on
        # the next order rather than the next restart. Capturing the gates at construction
        # would have quietly changed that.
        self._gates = gates
        self._journal_path = journal_path
        os.makedirs(os.path.dirname(journal_path), exist_ok=True)

    def _journal(self, rec: dict):
        rec["ts"] = time.time()
        with open(self._journal_path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    async def place(self, *, symbol: str, qty: int, side: str, product: str = "CNC",
                    order_type: str = "LIMIT", price: float | None = None,
                    exchange: str = "NSE", variety: str = "regular",
                    client_id: str | None = None, gross_exposure: float = 0.0,
                    series: str | None = None, tick_size: float | None = None,
                    tenant: TenantIds, plan_tenant: TenantIds) -> dict:
        # Law 2 multi-tenant clause (P4.3): refuse a plan built for someone else before
        # any guard, risk check, or network call. BLOCKED, not an exception — a 500 would
        # be the wrong answer to a cross-tenant post.
        mismatch = refuse_cross_tenant(tenant, plan_tenant)
        if mismatch:
            return {"symbol": symbol, "status": "BLOCKED", "error": mismatch}
        gates = self._gates()
        assert_tradeable(symbol, series)                       # layer 1: untouchables
        # Same layer: an option under a carry product would still be open tomorrow morning.
        # Returned as BLOCKED rather than raised so one refused leg cannot abort a batch
        # that has already placed real orders.
        try:
            assert_not_overnight_option(symbol, exchange, product)
        except OvernightOptionError as exc:
            self._journal({"event": "overnight_option_block", "symbol": symbol,
                           "product": product, "side": side, "exchange": exchange})
            return {"symbol": symbol, "status": "BLOCKED", "error": str(exc)}
        # MIS is an intraday product on every segment. The old NSE/BSE-only check let an
        # NFO MIS order through with INTRADAY_ENABLED=false as soon as OPTIONS_ENABLED was
        # set, so enabling options silently enabled an intraday engine as well.
        if product == "MIS" and not gates.intraday_enabled:
            return {"symbol": symbol, "status": "BLOCKED",
                    "error": "MIS/intraday disabled (config.INTRADAY_ENABLED)"}
        if exchange in ("NFO", "BFO") and not gates.options_enabled:
            return {"symbol": symbol, "status": "BLOCKED",
                    "error": "F&O disabled (config.OPTIONS_ENABLED)"}
        value = abs(qty) * float(price or 0)
        ok, why = self.risk.pre_order(symbol, value, gross_exposure)  # layer 2: risk
        if not ok:
            self._journal({"event": "risk_block", "symbol": symbol, "why": why})
            return {"symbol": symbol, "status": "RISK_BLOCKED", "error": why}
        cid = client_id or uuid.uuid4().hex[:10]
        if cid in self._sent:                                   # layer 3: idempotency
            return {"symbol": symbol, "status": "DUPLICATE", "order_id": self._sent[cid]}
        await self.limits.order_slot()                          # layer 4: rate limits
        if gates.dry_run:
            self._sent[cid] = f"DRY-{cid}"
            self._journal({"event": "dry_run", "symbol": symbol, "side": side,
                           "qty": qty, "price": price, "product": product})
            return {"symbol": symbol, "status": "DRY_RUN", "order_id": self._sent[cid]}
        params = dict(variety=variety, exchange=exchange, tradingsymbol=symbol,
                      quantity=abs(int(qty)), transaction_type=side, product=product,
                      order_type=order_type, validity="DAY")
        if price and order_type == "LIMIT":
            # Option ticks are currently Rs 0.05; rounding every limit to one decimal can
            # move a price away from the selected quote. Live contract metadata remains
            # authoritative because exchange tick sizes can change.
            px = float(price)
            params["price"] = (round(round(px / tick_size) * tick_size, 2)
                               if tick_size and tick_size > 0 else round(px, 1))
        try:
            oid = await asyncio.to_thread(self.kc.place_order, **params)
            self._sent[cid] = oid
            self._journal({"event": "placed", "order_id": oid, **params})
            return {"symbol": symbol, "status": "PLACED", "order_id": oid}
        except Exception as exc:
            definitive = type(exc).__name__ in _DEFINITIVE_REFUSALS
            status = "REJECTED" if definitive else "ERROR"
            self._journal({"event": "rejected" if definitive else "error",
                           "symbol": symbol, "error": str(exc),
                           "exception": type(exc).__name__})
            return {"symbol": symbol, "status": status, "error": str(exc),
                    "exception": type(exc).__name__,
                    # The one thing the operator needs after a failed batch.
                    "reached_exchange": False if definitive else None}

    # --- GTT stops: non-negotiable 4, and the closing of non-negotiable 6's exception -----
    #
    # Ported from kite-momentum-rebalancer/app/kite_client.py:225-273 (`delete_gtt`,
    # `place_gtt_stop`, `tick_size`, `to_tick`), which are byte-identical in both desk copies
    # — see baskfy_execution/gtt.py for the full provenance note and the reconciliation
    # reference. What the desk had was a guard bolted to the broker wrapper; what these two
    # methods add is the SAME four layers an order traverses, in the same order, so a stop
    # cannot be armed or cancelled around them.
    #
    # BOTH HALVES ARE GUARDED. Creating a stop and destroying one are the same hole facing in
    # opposite directions: a trigger that can be cancelled without passing guards leaves a
    # position naked just as surely as one that was never armed.

    async def _tick_size(self, symbol: str, exchange: str) -> float:
        """The instrument's tick, fetched once per exchange and cached.

        `kite_client.py:201-215`. This is a NETWORK CALL, so it takes its own rate-limit slot
        and it is reached only after the untouchable guard has already passed — non-negotiable
        7 is "refused before any network call", and the instrument dump is a network call.

        AN EMPTY DUMP IS AN ERROR, NOT A CACHE ENTRY. The desk cached whatever came back; an
        exchange that momentarily answered with nothing would have been remembered as "every
        symbol ticks at 5 paise" for the life of the process, and every stop armed afterwards
        would have been snapped to the wrong grid — silently, and for the whole session.
        """
        if not self._ticks.cached(exchange):
            await self.limits.api_slot()
            dump = await asyncio.to_thread(self.kc.instruments, exchange)
            if not dump:
                raise RuntimeError(
                    f"{exchange}: the instrument dump came back empty, so no tick size can be "
                    f"trusted; refusing to snap a trigger against a guess"
                )
            self._ticks.remember(exchange, dump)
        return self._ticks.get(symbol, exchange)

    async def place_gtt_stop(self, *, symbol: str, qty: int, trigger: float,
                             last_price: float, exchange: str = "NSE",
                             series: str | None = None, client_id: str | None = None,
                             tenant: TenantIds, plan_tenant: TenantIds) -> dict:
        """Rest a vol-scaled stop-loss at the exchange. The ONLY way to create a GTT.

        `trigger` is `stop_from_vol(price, vol, cfg)` — computed at plan time by
        `protection.build_stop_plan`, clamped to 8-12% below the price by `STOP_MIN`/`STOP_MAX`.
        The gateway does not recompute it: `packages/execution` holds no prices, no
        volatilities and no config, and a second implementation of the stop level is the one
        thing that could make two parts of the system disagree about where a stop belongs.
        What the gateway does instead is refuse a trigger that is not a stop at all, and
        journal one that sits outside the band.
        """
        mismatch = refuse_cross_tenant(tenant, plan_tenant)
        if mismatch:
            return {"symbol": symbol, "status": "BLOCKED", "error": mismatch}
        gates = self._gates()
        assert_tradeable(symbol, series)                       # GTT layer 1: untouchables
        # A GTT rests at the exchange for up to a year, so an option trigger is an overnight
        # option position by construction — it can only fire on a session this system never
        # intended to be holding one. Checked against the leg's OWN product, which is CNC.
        # (`kite_client.py:247-251`.) Returned as BLOCKED rather than raised, matching
        # `place()`, so one refused leg cannot abandon the rest of the book unprotected.
        try:
            assert_not_overnight_option(symbol, exchange, "CNC")
        except OvernightOptionError as exc:
            self._journal({"event": "gtt_overnight_option_block", "symbol": symbol,
                           "exchange": exchange, "qty": int(qty), "trigger": trigger})
            return {"symbol": symbol, "status": "BLOCKED", "error": str(exc)}
        if exchange in ("NFO", "BFO") and not gates.options_enabled:
            return {"symbol": symbol, "status": "BLOCKED",
                    "error": "F&O disabled (config.OPTIONS_ENABLED)"}
        # GTT layer 1b: is this actually a stop? The desk never asked, because its own planner
        # cannot produce a bad one. Nothing enforced that, and the gateway is where it belongs.
        refusal = refuse_stop(symbol=symbol, qty=qty, trigger=trigger, last_price=last_price)
        if refusal:
            self._journal({"event": "gtt_block", "symbol": symbol, "qty": int(qty),
                           "trigger": trigger, "last_price": last_price, "why": refusal})
            return {"symbol": symbol, "status": "BLOCKED", "error": refusal}
        # GTT layer 2: risk — CONSULTED, RECORDED, AND DELIBERATELY NOT OBEYED HERE.
        #
        # `risk.pre_order` is wrong for a protective stop in both of its effects. It refuses
        # everything once the kill switch has fired — and the day the daily-loss cap trips is
        # exactly the day an unstopped book is most dangerous, so refusing to arm would turn a
        # risk control into an unprotected holding. It also increments the day's order count,
        # and a GTT is not an order until it fires: charging the order budget for stops would
        # let a morning of arming refuse a real afternoon sell.
        #
        # So the kill switch is read and journalled, and the stop goes on. Cancelling is the
        # other way round — see `delete_gtt`, where risk does refuse.
        killed = kill_switch_reason(self.risk)
        if killed:
            self._journal({"event": "gtt_risk_note", "symbol": symbol, "qty": int(qty),
                           "trigger": trigger, "why": killed,
                           "note": "kill switch is live; a protective stop is still armed"})
        finding = band_finding(trigger=trigger, last_price=last_price, band=self._stop_band)
        if finding:
            self._journal({"event": "gtt_band_warning", "symbol": symbol, "finding": finding,
                           "drop_pct": drop_pct(trigger=trigger, last_price=last_price),
                           "band": [self._stop_band.min_pct, self._stop_band.max_pct]})
        cid = client_id or uuid.uuid4().hex[:10]
        if cid in self._gtt_sent:                              # GTT layer 3: idempotency
            # A re-armed stop is not a no-op: two triggers on one position sell twice what is
            # held when they fire, which is short delivery and an auction penalty (the 18 Aug
            # 2026 finding behind `protection.EXCESS`).
            return {"symbol": symbol, "status": "DUPLICATE",
                    "gtt_id": self._gtt_sent[cid]}
        await self.limits.api_slot()                           # GTT layer 4: rate limits
        if gates.dry_run:
            # Shape preserved from `kite_client.py:252-254`: the desk reports the UNSNAPPED
            # trigger here because snapping needs the instrument dump, which is a network call
            # a dry run must not make.
            self._journal({"event": "gtt_dry_run", "symbol": symbol, "qty": int(qty),
                           "trigger": trigger, "last_price": last_price,
                           "exchange": exchange})
            self._gtt_sent[cid] = f"DRY-{cid}"
            return {"symbol": symbol, "status": DRY_RUN_GTT, "trigger": trigger,
                    "qty": int(qty)}
        try:
            tick = await self._tick_size(symbol, exchange)
            trig = to_tick(trigger, tick)
            # The GTT's own limit sits just under the trigger so it fills on the way down; it
            # needs snapping to the same tick or the whole trigger is rejected.
            limit = to_tick(trig * GTT_LIMIT_FRACTION, tick)
        except Exception as exc:
            self._journal({"event": "gtt_error", "symbol": symbol, "stage": "tick_size",
                           "error": str(exc), "exception": type(exc).__name__})
            return {"symbol": symbol, "status": GTT_ERROR, "error": str(exc)}
        # RE-CHECKED AFTER SNAPPING, and this is not belt-and-braces. Snapping rounds to the
        # NEAREST tick, so on a coarse-tick scrip it can round a trigger UP through the last
        # price — 99.9 against a Rs 1.00 tick becomes 100.0 — and a sell trigger at or above
        # the last price fires on the next tick and liquidates the position. The desk snapped
        # and placed; nothing looked again.
        snapped_refusal = refuse_stop(symbol=symbol, qty=qty, trigger=trig,
                                      last_price=last_price)
        if snapped_refusal:
            self._journal({"event": "gtt_block", "symbol": symbol, "qty": int(qty),
                           "trigger": trig, "requested_trigger": trigger, "tick": tick,
                           "last_price": last_price, "stage": "after_tick_snap",
                           "why": snapped_refusal})
            return {"symbol": symbol, "status": "BLOCKED", "error": snapped_refusal}
        params = gtt_params(self.kc, symbol=symbol, exchange=exchange, qty=int(qty),
                            trigger=trig, limit=limit, last_price=last_price)
        # ONLY the broker call is inside the try. Reading `trigger_id` out of the response used
        # to sit here too, and that is the shape of a genuinely dangerous bug: a call that
        # SUCCEEDED but answered in an unexpected shape would have been reported GTT_ERROR, the
        # operator would have re-armed, and the position would carry two triggers. That is
        # `protection.EXCESS` — it sells shares that are not held when it fires — and it is the
        # same false-failure pattern that produced "0 armed, 17 failed" with sixteen triggers
        # live at the exchange. Once this call returns, the trigger exists; everything after it
        # is bookkeeping and must not be able to un-report it.
        try:
            gid = await asyncio.to_thread(self.kc.place_gtt, **params)
        except Exception as exc:
            self._journal({"event": "gtt_error", "symbol": symbol, "stage": "place_gtt",
                           "error": str(exc), "exception": type(exc).__name__,
                           "trigger": trig, "qty": int(qty)})
            return {"symbol": symbol, "status": GTT_ERROR, "error": str(exc),
                    "exception": type(exc).__name__,
                    # Unlike an order, a GTT refusal carries no taxonomy to read the outcome
                    # from, so the honest answer is "unknown": check the GTT book before
                    # re-arming, because a blind retry is how a position gets two stops.
                    "reached_exchange": None}
        trigger_id = gid.get("trigger_id") if isinstance(gid, dict) else None
        self._gtt_sent[cid] = trigger_id
        self._journal({"event": "gtt_placed", "symbol": symbol, "gtt_id": trigger_id,
                       "qty": int(qty), "trigger": trig, "limit": limit,
                       "last_price": last_price, "exchange": exchange,
                       "drop_pct": drop_pct(trigger=trig, last_price=last_price),
                       **({} if trigger_id is not None
                          else {"warning": "the broker named no trigger_id; the GTT exists "
                                           "but cannot be addressed from this record"})})
        return {"symbol": symbol, "status": GTT_PLACED, "gtt_id": trigger_id,
                "trigger": trig, "limit": limit}

    async def delete_gtt(self, *, gtt_id: int, symbol: str, exchange: str = "NSE",
                         series: str | None = None, tenant: TenantIds,
                         plan_tenant: TenantIds) -> dict:
        """Cancel one GTT trigger. The ONLY way to destroy a GTT.

        Ported from `kite_client.py:225-242`. The desk's own reasoning is kept — cancelling
        removes a resting sell, so it cannot itself create a position, and it carries the
        untouchable guard for consistency. It exists because an over-covered stop cannot be
        fixed by adding another: a trigger for more shares than are held sells what you do not
        own when it fires.

        THREE THINGS ARE TIGHTER HERE THAN ON THE DESK, and each closes a way protection could
        be removed without a guard:

        * `symbol` is REQUIRED. `kite_client.delete_gtt(gtt_id, symbol="")` ran no guard at all
          when the caller omitted the symbol — the guard was opt-in, which is not a guard. Every
          real caller already passes it (`protection.build_stop_plan` puts the symbol on every
          cancel row), so nothing legitimate is refused.
        * The kill switch REFUSES. Removing protection while the daily-loss cap is tripped is
          precisely the act a kill switch exists to stop, and refusing leaves the stop in
          place — the safe direction. Arming is the opposite case; see `place_gtt_stop`.
        * It is journalled. The desk logged an armed stop and left a cancellation to a redirect
          message, so the one action that removes protection was the one with no durable record.

        The overnight-option guard is deliberately NOT applied to a cancel. `guards.py` already
        makes the argument: "a guard that traps a position is worse than the risk it was written
        to prevent". Refusing to remove an option trigger would strand it at the exchange.
        """
        mismatch = refuse_cross_tenant(tenant, plan_tenant)
        if mismatch:
            return {"symbol": symbol, "gtt_id": gtt_id, "status": "BLOCKED",
                    "error": mismatch}
        gates = self._gates()
        if not (symbol or "").strip():
            why = ("a GTT cannot be cancelled without naming its instrument: the untouchable "
                   "guard has nothing to check")
            self._journal({"event": "gtt_delete_block", "gtt_id": gtt_id, "why": why})
            return {"symbol": symbol, "gtt_id": gtt_id, "status": "BLOCKED", "error": why}
        assert_tradeable(symbol, series)                       # GTT layer 1: untouchables
        # A trigger id is an exchange handle, not a hint. Refusing a malformed one here rather
        # than letting `int()` decide means a bug cannot spend a rate-limit slot, and — worse —
        # cannot truncate into some OTHER live trigger's id and cancel the wrong stop.
        if not isinstance(gtt_id, int) or isinstance(gtt_id, bool) or gtt_id <= 0:
            why = f"{symbol}: {gtt_id!r} is not a GTT trigger id"
            self._journal({"event": "gtt_delete_block", "symbol": symbol,
                           "gtt_id": gtt_id, "why": why})
            return {"symbol": symbol, "gtt_id": gtt_id, "status": "BLOCKED", "error": why}
        killed = kill_switch_reason(self.risk)                 # GTT layer 2: risk
        if killed:
            why = f"{killed} — a stop is not removed while the kill switch is live"
            self._journal({"event": "gtt_delete_risk_block", "symbol": symbol,
                           "gtt_id": gtt_id, "why": why})
            return {"symbol": symbol, "gtt_id": gtt_id, "status": "RISK_BLOCKED",
                    "error": why}
        await self.limits.api_slot()                           # GTT layer 4: rate limits
        if gates.dry_run:
            self._journal({"event": "gtt_dry_run_delete", "symbol": symbol,
                           "gtt_id": gtt_id, "exchange": exchange})
            return {"symbol": symbol, "gtt_id": gtt_id, "status": DRY_RUN_GTT_DELETE}
        try:
            await asyncio.to_thread(self.kc.delete_gtt, trigger_id=int(gtt_id))
            self._journal({"event": "gtt_deleted", "symbol": symbol, "gtt_id": gtt_id,
                           "exchange": exchange})
            return {"symbol": symbol, "gtt_id": gtt_id, "status": GTT_DELETED}
        except Exception as exc:
            self._journal({"event": "gtt_delete_error", "symbol": symbol, "gtt_id": gtt_id,
                           "error": str(exc), "exception": type(exc).__name__})
            return {"symbol": symbol, "gtt_id": gtt_id, "status": GTT_DELETE_ERROR,
                    "error": str(exc)}
