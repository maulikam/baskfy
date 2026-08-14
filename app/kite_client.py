"""Kite Connect wrapper. Auth, holdings (pledged-aware), margins, LTP, CNC orders, GTT stops.
Order placement is gated by the caller (main.py) — this module never self-initiates trades."""
from __future__ import annotations
import json
import os
import logging
from kiteconnect import KiteConnect
from . import config as C
from .core.guards import assert_tradeable

log = logging.getLogger("kite")


class Kite:
    def __init__(self):
        if not C.KITE_API_KEY:
            raise RuntimeError("KITE_API_KEY missing — copy .env.example to .env and fill it.")
        self.kc = KiteConnect(api_key=C.KITE_API_KEY)
        self._load_token()

    # ---------- auth ----------
    def login_url(self) -> str:
        return self.kc.login_url()

    def exchange_token(self, request_token: str) -> None:
        data = self.kc.generate_session(request_token, api_secret=C.KITE_API_SECRET)
        self.kc.set_access_token(data["access_token"])
        os.makedirs(os.path.dirname(C.TOKEN_FILE), exist_ok=True)
        with open(C.TOKEN_FILE, "w") as f:
            json.dump({"access_token": data["access_token"]}, f)

    def _load_token(self) -> None:
        if os.path.exists(C.TOKEN_FILE):
            with open(C.TOKEN_FILE) as f:
                self.kc.set_access_token(json.load(f)["access_token"])

    def is_authed(self) -> bool:
        try:
            self.kc.profile()
            return True
        except Exception:
            return False

    # ---------- reads ----------
    def holdings(self) -> list[dict]:
        """Total qty = quantity + t1 + collateral (pledged). Missing collateral once
        caused a 103-share undercount — never simplify this."""
        out = []
        for h in self.kc.holdings():
            total = h["quantity"] + h.get("t1_quantity", 0) + h.get("collateral_quantity", 0)
            if total <= 0:
                continue
            out.append(dict(symbol=h["tradingsymbol"], exchange=h["exchange"],
                            quantity=total, pledged_qty=h.get("collateral_quantity", 0),
                            average_price=h["average_price"], last_price=h["last_price"]))
        return out

    def available_cash(self) -> float:
        m = self.kc.margins()
        return float(m["equity"]["available"]["live_balance"])

    def ltp(self, symbols: list[str], exchange: str = "NSE") -> dict[str, float]:
        keys = [f"{exchange}:{s}" for s in symbols]
        data = self.kc.ltp(keys)
        return {k.split(":", 1)[1]: v["last_price"] for k, v in data.items()}

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

    def place_gtt_stop(self, symbol: str, qty: int, trigger: float, last_price: float,
                       exchange: str = "NSE") -> dict:
        assert_tradeable(symbol)   # SGB/G-sec hard block
        if C.DRY_RUN:
            return {"symbol": symbol, "status": "DRY_RUN_GTT", "trigger": trigger}
        try:
            gid = self.kc.place_gtt(
                trigger_type=self.kc.GTT_TYPE_SINGLE, tradingsymbol=symbol,
                exchange=exchange, trigger_values=[round(trigger, 1)],
                last_price=last_price,
                orders=[dict(exchange=exchange, tradingsymbol=symbol,
                             transaction_type=self.kc.TRANSACTION_TYPE_SELL,
                             quantity=int(qty), order_type=self.kc.ORDER_TYPE_LIMIT,
                             product=self.kc.PRODUCT_CNC,
                             price=round(trigger * 0.995, 1))])
            return {"symbol": symbol, "status": "GTT_PLACED", "gtt_id": gid["trigger_id"]}
        except Exception as exc:
            return {"symbol": symbol, "status": "GTT_ERROR", "error": str(exc)}
