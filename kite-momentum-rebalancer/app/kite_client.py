"""Kite Connect wrapper. Auth, holdings (pledged-aware), margins, LTP, CNC orders, GTT stops.
Order placement is gated by the caller (main.py) — this module never self-initiates trades."""
from __future__ import annotations
import json
import os
import time
import logging
from kiteconnect import KiteConnect
from . import config as C
from .core.guards import assert_not_overnight_option, assert_tradeable
from .core.kite_limits import DeskLimits
from .core.net import force_ipv4

#: How long a last price answered by `ltp` is reused. Five seconds is the desk page's own
#: refresh interval and `OpeningRangeConfig.quote_poll_min_seconds`, so a page poll costs no
#: Kite call at all and the monitor's fallback stays inside its own bound.
LTP_CACHE_SECONDS = 5.0

log = logging.getLogger("kite")


class Kite:
    def __init__(self):
        if not C.KITE_API_KEY:
            raise RuntimeError("KITE_API_KEY missing — copy .env.example to .env and fill it.")
        # Before the session exists, so every call it makes leaves on the family the
        # broker's allowlist can actually name.
        if C.FORCE_IPV4:
            force_ipv4()
        self.kc = KiteConnect(api_key=C.KITE_API_KEY)
        # SW20: every READ below takes a slot from this before it touches `self.kc`. Orders keep
        # the gateway's own async limiter — see app/core/kite_limits.py for the caps and for why
        # there are two.
        self._limits = DeskLimits()
        #: `ltp` answers for a few seconds, so a page that refreshes every five does not need a
        #: fresh call each time. Quote is Kite's 1 req/s endpoint; this is what keeps a polling
        #: page off it entirely.
        self._ltp_cache: dict[str, tuple[float, float]] = {}
        self._ltp_ttl = LTP_CACHE_SECONDS
        self._load_token()

    @property
    def limits(self) -> DeskLimits:
        """The read limiter, created on first use.

        A property rather than a plain attribute because the desk's tests build a `Kite` without
        running `__init__` (they set `kc` on a bare object to keep the broker out of a unit test),
        and a read path that raised `AttributeError` in that shape would push everyone back to
        calling `kc` directly — which is the thing SW20 exists to stop.
        """
        limiter = getattr(self, "_limits", None)
        if limiter is None:
            limiter = DeskLimits()
            self._limits = limiter
        return limiter

    # ---------- auth ----------
    def login_url(self) -> str:
        return self.kc.login_url()

    def exchange_token(self, request_token: str) -> None:
        data = self.kc.generate_session(request_token, api_secret=C.KITE_API_SECRET)
        self.kc.set_access_token(data["access_token"])
        # ENCRYPTED AT REST since M16. It used to be plain JSON at 0600 -- the right mode, and
        # still a readable secret in any backup, sync or snapshot of this directory. The store
        # is the screener's, so the merged system keeps a Kite token exactly one way.
        # See app/token_store.py for what the key fallback does and does not protect against.
        from .token_store import store_for

        store_for(C.TOKEN_FILE, getattr(C, "KITE_TOKEN_ENCRYPTION_KEY", "")).save(
            data["access_token"]
        )

    def _load_token(self) -> None:
        from .token_store import store_for

        store = store_for(C.TOKEN_FILE, getattr(C, "KITE_TOKEN_ENCRYPTION_KEY", ""))
        if not store.exists():
            self._token_mtime = None
            return
        try:
            self.kc.set_access_token(store.load().value)
        except Exception as exc:                                          # noqa: BLE001
            # A token that cannot be read is the same situation as no token: the operator
            # logs in again. Failing to start the desk over it would be worse.
            log.warning("could not read the stored access token (%s); log in again", exc)
            self._token_mtime = None
        else:
            self._token_mtime = self._blob_mtime(store)

    @staticmethod
    def _blob_mtime(store) -> float | None:                               # noqa: ANN001
        """The token blob's mtime, or None when it is gone or unreadable."""
        try:
            return store.path.stat().st_mtime
        except OSError:
            return None

    def refresh_token_if_changed(self) -> bool:
        """Re-read the blob when somebody else has written a newer token. Returns True if it did.

        The desk holds ONE `Kite` for the life of the process (`app.main.kite()`), and
        `__init__` loads the token once. So a token written at 09:10 — by the morning login
        the nudge asks for (SW18), by `/callback`, or by the desk bridge — was invisible to the
        already-running web process until somebody restarted the container. That is a restart
        of an order-capable process, every morning, for a file that changed underneath us.

        Cheap: one `stat` per call, and `set_access_token` only when the mtime moved. Called at
        the top of every read and before the order path's own checks, so the first request after
        a login carries the new session and no request carries a stale one.
        """
        from .token_store import store_for

        store = store_for(C.TOKEN_FILE, getattr(C, "KITE_TOKEN_ENCRYPTION_KEY", ""))
        seen = self._blob_mtime(store)
        if seen is None or seen == getattr(self, "_token_mtime", None):
            return False
        log.info("the stored Kite token changed on disk; re-reading it")
        self._load_token()
        return True

    def is_authed(self) -> bool:
        self.refresh_token_if_changed()
        try:
            self.limits.slot("general")
            self.kc.profile()
            return True
        except Exception:
            return False

    # ---------- reads ----------
    def holdings(self) -> list[dict]:
        """What you actually own right now, including shares bought TODAY.

        Total qty = quantity + t1 + collateral (pledged). Missing collateral once caused a
        103-share undercount — never simplify this.

        AND TODAY'S CNC BUYS, WHICH ARE NOT IN holdings() AT ALL. Kite moves a same-day
        purchase into holdings only at T+1; until then it exists solely as a net position.
        Reading holdings alone therefore reports 0 for a stock bought an hour ago, and the
        rebalancer treats it as a fresh buy — on 18 Aug 2026 a re-run plan proposed buying
        CUPID, HSCL and SANSERA a second time, about Rs 16 lakh, having already filled all
        three that morning.

        Same-day SELLS need no such handling and must not get it: Kite reduces the holdings
        quantity immediately, so SAILIFE showed 435 after selling 112 from 547 while its
        position showed -112. Adding that negative would subtract the sale twice. Only
        POSITIVE day positions are folded in.

        The asymmetry is Kite's, not ours, and it is why this cannot be a single call.
        """
        self.refresh_token_if_changed()
        out: dict[str, dict] = {}
        self.limits.slot("general")
        for h in self.kc.holdings():
            total = h["quantity"] + h.get("t1_quantity", 0) + h.get("collateral_quantity", 0)
            if total <= 0:
                continue
            out[h["tradingsymbol"]] = dict(
                symbol=h["tradingsymbol"], exchange=h["exchange"], quantity=total,
                pledged_qty=h.get("collateral_quantity", 0),
                average_price=h["average_price"], last_price=h["last_price"])

        try:
            self.limits.slot("general")
            positions = self.kc.positions().get("net", []) or []
        except Exception as exc:                                   # noqa: BLE001
            # Refuse rather than under-report. A holdings figure that silently omits
            # today's buys is the input to a plan that would buy them again.
            raise RuntimeError(
                f"could not read positions, so today's purchases cannot be counted: {exc}"
            ) from exc

        for p in positions:
            if str(p.get("product") or "") != "CNC":
                continue
            # THE DAY'S EXPOSURE, NOT THE POSITION. `quantity` includes any overnight
            # carry, and a carried position is one Kite has already settled into
            # holdings() — so folding the whole quantity counts those shares twice, the
            # same Rs 16 lakh error as before with the sign reversed.
            #
            # Checked against the live book at 01:50 on 19 Aug 2026, before Kite had
            # rolled its day: all 20 CNC rows still reported overnight_quantity 0 with
            # yesterday's fills as day buys, so quantity and day-net agreed exactly. That
            # is the timing being kind, not the rule being right — the two diverge the
            # moment the roll happens, which is somewhere inside the next session.
            qty = int(p.get("quantity") or 0) - int(p.get("overnight_quantity") or 0)
            if qty <= 0:                    # sells are already reflected in holdings
                continue
            sym = p["tradingsymbol"]
            row = out.get(sym)
            if row:
                row["quantity"] += qty
            else:
                out[sym] = dict(symbol=sym, exchange=p.get("exchange", "NSE"),
                                quantity=qty, pledged_qty=0,
                                average_price=p.get("average_price") or 0.0,
                                last_price=p.get("last_price") or 0.0)
        return list(out.values())

    def trades(self) -> list[dict]:
        """Today's executed trades. THE BOOK IS SAME-DAY ONLY.

        /trades takes no date parameter and Zerodha flushes it nightly, so this is the
        only chance to record a fill through the API — miss the session and it is gone.
        Historical fills exist solely in a Console export.
        """
        self.limits.slot("general")
        return list(self.kc.trades() or [])

    def available_cash(self) -> float:
        self.limits.slot("general")
        m = self.kc.margins()
        return float(m["equity"]["available"]["live_balance"])

    def ltp(self, symbols: list[str], exchange: str = "NSE") -> dict[str, float]:
        """Last prices, served from a `LTP_CACHE_SECONDS` cache before Kite is asked.

        `quote`/`ltp`/`ohlc` share Kite's tightest limit — **1 req/s** — and the desk page
        refreshes every five seconds during the session while the monitor runs beside it. So a
        price this process fetched within the TTL is reused, and only the names it has no fresh
        answer for cost a call. `refresh_token_if_changed` still runs first: a cached price must
        not keep a stale session alive (SW19).
        """
        self.refresh_token_if_changed()
        cache = getattr(self, "_ltp_cache", None)
        if cache is None:
            cache = self._ltp_cache = {}
        ttl = getattr(self, "_ltp_ttl", LTP_CACHE_SECONDS)
        wanted = [s for s in dict.fromkeys(symbols) if s]
        now = time.monotonic()
        out = {}
        missing = []
        for symbol in wanted:
            hit = cache.get(f"{exchange}:{symbol}")
            if hit is not None and now - hit[0] < ttl:
                out[symbol] = hit[1]
            else:
                missing.append(symbol)
        if not missing:
            return out
        keys = [f"{exchange}:{s}" for s in missing]
        self.limits.slot("quote")
        data = self.kc.ltp(keys)
        fetched = time.monotonic()
        for key, value in data.items():
            price = float(value["last_price"])
            cache[key] = (fetched, price)
            out[key.split(":", 1)[1]] = price
        return out

    def quotes(self, symbols: list[str], exchange: str = "NSE") -> dict[str, dict]:
        """Full quote — last price, the day's OHLC, previous close and volume.

        Kite caps a quote call at 500 instruments, so this chunks below that rather than
        letting a Nifty 500 drill-down fail as one oversized request. An unknown symbol is
        simply absent from Kite's reply, so callers get a short dict rather than an error:
        a delisted or renamed constituent must not take the whole panel down.

        Read-only. A quote is never a step towards an order — those go through
        core/gateway.py, which is the only path allowed to place one.
        """
        self.refresh_token_if_changed()
        out: dict[str, dict] = {}
        clean = [s for s in dict.fromkeys(symbols) if s]
        for i in range(0, len(clean), 400):
            keys = [f"{exchange}:{s}" for s in clean[i:i + 400]]
            try:
                self.limits.slot("quote")
                data = self.kc.quote(keys)
            except Exception as e:                              # noqa: BLE001
                log.warning("quote batch failed (%d symbols): %s", len(keys), e)
                continue
            for k, v in data.items():
                ohlc = v.get("ohlc") or {}
                out[k.split(":", 1)[1]] = {
                    "last_price": v.get("last_price"),
                    "net_change": v.get("net_change"),
                    "open": ohlc.get("open"),
                    "high": ohlc.get("high"),
                    "low": ohlc.get("low"),
                    "prev_close": ohlc.get("close"),
                    "volume": v.get("volume") or v.get("volume_traded"),
                }
        return out

    # ---------- limited wrappers for everything outside this file ----------
    #
    # SW20: `app/analytics/` used to call `k.kc.<anything>` directly, which meant twenty-five
    # reads a second was as legal as one. These exist so nothing outside this module has to hold
    # the raw handle, and `tests/test_kite_limits.py` fails if a new direct call appears.

    def _instruments(self, exchange: str = "NSE") -> list[dict]:
        self.limits.slot("general")
        return list(self.kc.instruments(exchange) or [])

    def instruments(self, exchange: str = "NSE") -> list[dict]:
        """The exchange's instrument dump. One general slot; a big response, not a fast one."""
        return self._instruments(exchange)

    def historical(self, token: int, start, end, interval: str = "day") -> list[dict]:
        """Daily (or finer) candles for one instrument — Kite's 3 req/s family."""
        self.refresh_token_if_changed()
        self.limits.slot("historical")
        return list(self.kc.historical_data(token, start, end, interval) or [])

    def margins(self, segment: str | None = None) -> dict:
        """The raw margins payload. `available_cash` is the number most callers want."""
        self.refresh_token_if_changed()
        self.limits.slot("general")
        return dict(self.kc.margins(segment) if segment else self.kc.margins())

    def orders(self) -> list[dict]:
        self.refresh_token_if_changed()
        self.limits.slot("general")
        return list(self.kc.orders() or [])

    def order_history(self, order_id: str) -> list[dict]:
        self.refresh_token_if_changed()
        self.limits.slot("general")
        return list(self.kc.order_history(order_id) or [])

    def quote_raw(self, keys: list[str]) -> dict:
        """Kite's own `quote` shape for pre-keyed instruments (`NSE:INFY`), one quote slot.

        `quotes()` above is the desk's shape — chunked, symbol-keyed, tolerant of a missing
        name. `analytics/snapshot.py` wants the raw payload and does its own chunking, so it
        gets this rather than the client handle it used to reach through.
        """
        self.refresh_token_if_changed()
        self.limits.slot("quote")
        return dict(self.kc.quote(keys) or {})

    def get_gtts(self) -> list[dict]:
        """Every resting GTT. A read: creating one is `place_gtt_stop`, deleting `delete_gtt`."""
        self.refresh_token_if_changed()
        self.limits.slot("general")
        return list(self.kc.get_gtts() or [])

    # ---------- writes (caller must gate with user confirmation) ----------
    def place_cnc_order(self, symbol: str, qty: int, side: str, limit_price: float | None,
                        exchange: str = "NSE") -> dict:
        assert_tradeable(symbol)   # SGB/G-sec hard block, cannot be bypassed
        if C.DRY_RUN:
            log.info("DRY_RUN order: %s %s x%d @ %s", side, symbol, qty, limit_price)
            return {"symbol": symbol, "status": "DRY_RUN", "order_id": None}
        params = dict(variety=self.kc.VARIETY_REGULAR, exchange=exchange,
                      tradingsymbol=symbol, quantity=abs(int(qty)),
                      transaction_type=self.kc.TRANSACTION_TYPE_BUY if side == "BUY"
                      else self.kc.TRANSACTION_TYPE_SELL,
                      product=self.kc.PRODUCT_CNC,   # delivery only — never MIS/NRML
                      order_type=self.kc.ORDER_TYPE_LIMIT if limit_price
                      else self.kc.ORDER_TYPE_MARKET,
                      validity=self.kc.VALIDITY_DAY)
        if limit_price:
            params["price"] = round(float(limit_price), 1)
        try:
            oid = self.kc.place_order(**params)
            return {"symbol": symbol, "status": "PLACED", "order_id": oid}
        except Exception as exc:
            return {"symbol": symbol, "status": "ERROR", "error": str(exc)}

    def tick_size(self, symbol: str, exchange: str = "NSE") -> float:
        """Tick size for an instrument, cached for the process.

        NSE ticks are not uniform: most names are 0.05 or 0.10, but a high-priced scrip
        like OFSS is 1.00. A price that is not a multiple of its tick is rejected outright
        — "Trigger price should be a multiple of tick size 1.00" is what cost OFSS its
        stop on the first live arming run.
        """
        cache = getattr(self, "_ticks", None)
        if cache is None or exchange not in cache:
            cache = cache or {}
            cache[exchange] = {i["tradingsymbol"]: float(i.get("tick_size") or 0.05)
                               for i in self._instruments(exchange)}
            self._ticks = cache
        return cache[exchange].get(symbol, 0.05)

    @staticmethod
    def to_tick(price: float, tick: float) -> float:
        """Snap to the nearest valid tick. Sub-tick precision is not a price."""
        if tick <= 0:
            return round(price, 2)
        steps = round(price / tick)
        return round(steps * tick, 2)

    def delete_gtt(self, gtt_id: int, symbol: str = "") -> dict:
        """Cancel one GTT trigger.

        Cancelling only ever REDUCES exposure — it removes a resting sell order — so it
        carries the untouchable guard for consistency but cannot itself create a position.
        It exists because an over-covered stop cannot be fixed by adding another: a trigger
        for more shares than are held sells what you do not own when it fires.
        """
        if symbol:
            assert_tradeable(symbol)
        if C.DRY_RUN:
            return {"symbol": symbol, "gtt_id": gtt_id, "status": "DRY_RUN_GTT_DELETE"}
        try:
            self.kc.delete_gtt(trigger_id=int(gtt_id))
            return {"symbol": symbol, "gtt_id": gtt_id, "status": "GTT_DELETED"}
        except Exception as exc:
            return {"symbol": symbol, "gtt_id": gtt_id, "status": "GTT_DELETE_ERROR",
                    "error": str(exc)}

    def place_gtt_stop(self, symbol: str, qty: int, trigger: float, last_price: float,
                       exchange: str = "NSE") -> dict:
        assert_tradeable(symbol)   # SGB/G-sec hard block
        # A GTT rests at the exchange for up to a year, so an option GTT is an overnight
        # option position by construction — it can only fire on a session this system
        # never intended to be holding one. Checked against the order's OWN product, which
        # is CNC below, rather than against the caller's intent.
        assert_not_overnight_option(symbol, exchange, "CNC")
        if C.DRY_RUN:
            return {"symbol": symbol, "status": "DRY_RUN_GTT", "trigger": trigger,
                    "qty": int(qty)}
        try:
            tick = self.tick_size(symbol, exchange)
            trig = self.to_tick(trigger, tick)
            # The GTT's own limit sits just under the trigger so it fills on the way
            # down; it needs snapping to the same tick or the whole trigger is rejected.
            limit = self.to_tick(trig * 0.995, tick)
            gid = self.kc.place_gtt(
                trigger_type=self.kc.GTT_TYPE_SINGLE, tradingsymbol=symbol,
                exchange=exchange, trigger_values=[trig],
                last_price=last_price,
                orders=[dict(exchange=exchange, tradingsymbol=symbol,
                             transaction_type=self.kc.TRANSACTION_TYPE_SELL,
                             quantity=int(qty), order_type=self.kc.ORDER_TYPE_LIMIT,
                             product=self.kc.PRODUCT_CNC,
                             price=limit)])
            return {"symbol": symbol, "status": "GTT_PLACED", "gtt_id": gid["trigger_id"],
                    "trigger": trig, "limit": limit}
        except Exception as exc:
            return {"symbol": symbol, "status": "GTT_ERROR", "error": str(exc)}
