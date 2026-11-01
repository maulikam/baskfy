"""Goldens for the desk's exec_kite lane (DECISIONS-RS R8.11): `app.kite_client.Kite`'s own
answers — a value, or the exception it raises (`type(exc).__name__`, `str(exc)`) — on broker
replies of every shape, well-formed and not, for `baskfy-desk`'s `tests/exec_kite_goldens.rs`
to reproduce exactly.

The wave-D port skipped a malformed entry where Python raises (ltp, quotes, holdings), and
read every list reply that was not a list as empty; these cases pin what Python actually does.

A fake `kc` answers each method from the case's `replies` (a reply `{"__raise__": [type, msg]}`
raises). The Kite is built the way the desk's tests build one (`Kite.__new__` with `kc` set), the
read limiter is alone and never sleeps, and the token store points at a temporary directory with
a configured key, so nothing is written beside the desk. No network, no `portfolio.db`.

Run:
    PYTHONPATH=<wt>/kite-momentum-rebalancer:<wt>/decile-blueprint/packages/execution/src:\
<wt>/decile-blueprint/packages/providers/src:<wt>/decile-blueprint/packages/core/src \
      <main>/kite-momentum-rebalancer/.venv/bin/python tools/parity/exec_kite_goldens.py \
      rust/crates/baskfy-desk/fixtures/exec_kite/goldens.json
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="exec_kite_goldens_")
os.environ["TOKEN_FILE"] = os.path.join(_TMP, ".kite_token.json")
os.environ["DESK_SHARED_READ_LIMITS"] = "false"
os.environ["DRY_RUN"] = "true"
os.environ["KITE_API_KEY"] = "goldens-key"

from cryptography.fernet import Fernet  # noqa: E402

os.environ["KITE_TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import config as C  # noqa: E402
from app.core.kite_limits import DeskLimits  # noqa: E402
from app.kite_client import Kite  # noqa: E402


class FakeKC:
    GTT_TYPE_SINGLE = "single"
    TRANSACTION_TYPE_SELL = "SELL"
    ORDER_TYPE_LIMIT = "LIMIT"
    PRODUCT_CNC = "CNC"

    def __init__(self, replies: dict) -> None:
        self.replies = replies
        self.calls: list[str] = []

    def _answer(self, name: str):
        self.calls.append(name)
        reply = self.replies.get(name)
        if isinstance(reply, dict) and "__raise__" in reply:
            kind, message = reply["__raise__"]
            raise {"RuntimeError": RuntimeError, "ValueError": ValueError}.get(kind, Exception)(message)
        return reply

    def set_access_token(self, _token):
        return None

    def holdings(self):
        return self._answer("holdings")

    def positions(self):
        return self._answer("positions")

    def ltp(self, _keys):
        return self._answer("ltp")

    def quote(self, _keys):
        return self._answer("quote")

    def trades(self):
        return self._answer("trades")

    def margins(self, *_segment):
        return self._answer("margins")

    def instruments(self, *_exchange):
        return self._answer("instruments")

    def historical_data(self, *_args):
        return self._answer("historical_data")

    def orders(self):
        return self._answer("orders")

    def order_history(self, _order_id):
        return self._answer("order_history")

    def get_gtts(self):
        return self._answer("get_gtts")

    def place_gtt(self, **_kwargs):
        return self._answer("place_gtt")

    def delete_gtt(self, **_kwargs):
        return self._answer("delete_gtt")

    def profile(self):
        return self._answer("profile")


def _kite(replies: dict) -> Kite:
    k = Kite.__new__(Kite)
    k.kc = FakeKC(replies)
    k._limits = DeskLimits(clock=lambda: 0.0, sleep=lambda _s: None, shared={})
    return k


def _call(k: Kite, method: str, args: list):
    if method == "holdings":
        return k.holdings()
    if method == "ltp":
        return k.ltp(args[0], "NSE")
    if method == "quotes":
        return k.quotes(args[0], "NSE")
    if method == "trades":
        return k.trades()
    if method == "available_cash":
        return k.available_cash()
    if method == "margins":
        return k.margins(args[0] if args else None)
    if method == "quote_raw":
        return k.quote_raw(args[0])
    if method == "instruments":
        return k.instruments("NSE")
    if method == "historical":
        return k.historical(408065, "2026-10-01", "2026-10-29", "day")
    if method == "orders":
        return k.orders()
    if method == "order_history":
        return k.order_history("1")
    if method == "get_gtts":
        return k.get_gtts()
    if method == "place_gtt_stop":
        return k.place_gtt_stop(*args)
    if method == "delete_gtt":
        return k.delete_gtt(*args)
    raise SystemExit(f"unknown method {method}")


H = {"tradingsymbol": "INFY", "exchange": "NSE", "quantity": 10, "t1_quantity": 0,
     "collateral_quantity": 0, "average_price": 100.0, "last_price": 101.0}


def _h(**over):
    row = dict(H)
    for k, v in over.items():
        if v is _DROP:
            row.pop(k, None)
        else:
            row[k] = v
    return row


_DROP = object()


def _p(**over):
    row = {"tradingsymbol": "TCS", "exchange": "NSE", "quantity": 4, "product": "CNC",
           "overnight_quantity": 0, "average_price": 200.0, "last_price": 201.0}
    for k, v in over.items():
        if v is _DROP:
            row.pop(k, None)
        else:
            row[k] = v
    return row


NET = {"net": []}

CASES: list[tuple[str, str, list, dict, bool]] = [
    # (name, method, args, replies, live)
    # ---- holdings: the holdings reply -------------------------------------------------------
    ("holdings well-formed", "holdings", [], {"holdings": [H], "positions": NET}, False),
    ("holdings reply None", "holdings", [], {"holdings": None, "positions": NET}, False),
    ("holdings reply empty dict", "holdings", [], {"holdings": {}, "positions": NET}, False),
    ("holdings reply a dict", "holdings", [], {"holdings": {"a": 1}, "positions": NET}, False),
    ("holdings reply a string", "holdings", [], {"holdings": "ab", "positions": NET}, False),
    ("holdings reply an int", "holdings", [], {"holdings": 5, "positions": NET}, False),
    ("holdings row a string", "holdings", [], {"holdings": ["x"], "positions": NET}, False),
    ("holdings row a list", "holdings", [], {"holdings": [[1]], "positions": NET}, False),
    ("holdings row None", "holdings", [], {"holdings": [None], "positions": NET}, False),
    ("holdings row empty", "holdings", [], {"holdings": [{}], "positions": NET}, False),
    ("holdings quantity None", "holdings", [], {"holdings": [_h(quantity=None)], "positions": NET}, False),
    ("holdings quantity a string", "holdings", [], {"holdings": [_h(quantity="5")], "positions": NET}, False),
    ("holdings all strings", "holdings", [],
     {"holdings": [_h(quantity="5", t1_quantity="1", collateral_quantity="2")], "positions": NET}, False),
    ("holdings quantity a list", "holdings", [], {"holdings": [_h(quantity=[1])], "positions": NET}, False),
    ("holdings t1 None", "holdings", [], {"holdings": [_h(t1_quantity=None)], "positions": NET}, False),
    ("holdings t1 and collateral missing", "holdings", [],
     {"holdings": [_h(t1_quantity=_DROP, collateral_quantity=_DROP)], "positions": NET}, False),
    ("holdings collateral a float", "holdings", [],
     {"holdings": [_h(collateral_quantity=2.5)], "positions": NET}, False),
    ("holdings quantity a bool", "holdings", [], {"holdings": [_h(quantity=True)], "positions": NET}, False),
    ("holdings quantity zero is skipped before the keys", "holdings", [],
     {"holdings": [{"quantity": 0}], "positions": NET}, False),
    ("holdings tradingsymbol missing", "holdings", [],
     {"holdings": [_h(tradingsymbol=_DROP)], "positions": NET}, False),
    ("holdings exchange missing", "holdings", [], {"holdings": [_h(exchange=_DROP)], "positions": NET}, False),
    ("holdings average_price missing", "holdings", [],
     {"holdings": [_h(average_price=_DROP)], "positions": NET}, False),
    ("holdings last_price missing", "holdings", [],
     {"holdings": [_h(last_price=_DROP)], "positions": NET}, False),
    ("holdings tradingsymbol a list", "holdings", [],
     {"holdings": [_h(tradingsymbol=["x"])], "positions": NET}, False),
    ("holdings tradingsymbol a dict", "holdings", [],
     {"holdings": [_h(tradingsymbol={"x": 1})], "positions": NET}, False),
    ("holdings tradingsymbol an int", "holdings", [],
     {"holdings": [_h(tradingsymbol=5)], "positions": NET}, False),
    ("holdings tradingsymbol None", "holdings", [],
     {"holdings": [_h(tradingsymbol=None)], "positions": NET}, False),
    ("holdings duplicate symbol: last wins, first position", "holdings", [],
     {"holdings": [_h(quantity=1), _h(tradingsymbol="TCS"), _h(quantity=7)], "positions": NET}, False),
    ("holdings int and float symbols 1 and 1.0 collide", "holdings", [],
     {"holdings": [_h(tradingsymbol=1), _h(tradingsymbol=1.0, quantity=3)], "positions": NET}, False),
    ("holdings negative total skipped", "holdings", [],
     {"holdings": [_h(quantity=-5, t1_quantity=2)], "positions": NET}, False),
    # ---- holdings: the positions reply ------------------------------------------------------
    ("positions raises", "holdings", [],
     {"holdings": [H], "positions": {"__raise__": ["RuntimeError", "network"]}}, False),
    ("positions reply None", "holdings", [], {"holdings": [H], "positions": None}, False),
    ("positions reply a list", "holdings", [], {"holdings": [H], "positions": []}, False),
    ("positions reply a string", "holdings", [], {"holdings": [H], "positions": "x"}, False),
    ("positions net missing", "holdings", [], {"holdings": [H], "positions": {}}, False),
    ("positions net None", "holdings", [], {"holdings": [H], "positions": {"net": None}}, False),
    ("positions net a dict", "holdings", [], {"holdings": [H], "positions": {"net": {"a": 1}}}, False),
    ("positions net an int", "holdings", [], {"holdings": [H], "positions": {"net": 5}}, False),
    ("positions net zero", "holdings", [], {"holdings": [H], "positions": {"net": 0}}, False),
    ("positions net a string", "holdings", [], {"holdings": [H], "positions": {"net": "ab"}}, False),
    ("positions row a string", "holdings", [], {"holdings": [H], "positions": {"net": ["x"]}}, False),
    ("positions row None", "holdings", [], {"holdings": [H], "positions": {"net": [None]}}, False),
    ("positions well-formed new symbol", "holdings", [], {"holdings": [H], "positions": {"net": [_p()]}}, False),
    ("positions product missing", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(product=_DROP)]}}, False),
    ("positions product an int", "holdings", [], {"holdings": [H], "positions": {"net": [_p(product=5)]}}, False),
    ("positions product MIS", "holdings", [], {"holdings": [H], "positions": {"net": [_p(product="MIS")]}}, False),
    ("positions quantity a string", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(quantity="7")]}}, False),
    ("positions quantity padded string", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(quantity=" 12 ")]}}, False),
    ("positions quantity underscored string", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(quantity="1_0")]}}, False),
    ("positions quantity signed string", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(quantity="+3")]}}, False),
    ("positions quantity a decimal string", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(quantity="1.5")]}}, False),
    ("positions quantity garbage string", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(quantity="abc")]}}, False),
    ("positions quantity a double underscore", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(quantity="1__0")]}}, False),
    ("positions quantity a float", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(quantity=2.9)]}}, False),
    ("positions quantity a negative float", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(quantity=-2.9)]}}, False),
    ("positions quantity a list", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(quantity=[1])]}}, False),
    ("positions quantity a bool", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(quantity=True)]}}, False),
    ("positions quantity None", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(quantity=None)]}}, False),
    ("positions overnight a string", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(quantity=10, overnight_quantity="3")]}}, False),
    ("positions overnight a dict", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(overnight_quantity={"a": 1})]}}, False),
    ("positions tradingsymbol missing", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(tradingsymbol=_DROP)]}}, False),
    ("positions tradingsymbol missing on a sell", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(tradingsymbol=_DROP, quantity=-3)]}}, False),
    ("positions tradingsymbol a list", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(tradingsymbol=["x"])]}}, False),
    ("positions tradingsymbol an int", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(tradingsymbol=7)]}}, False),
    ("positions exchange missing and prices None", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(exchange=_DROP, average_price=None, last_price=0)]}}, False),
    ("positions exchange None", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(exchange=None)]}}, False),
    ("positions fold into a float holding", "holdings", [],
     {"holdings": [_h(tradingsymbol="TCS", collateral_quantity=0.5)], "positions": {"net": [_p()]}}, False),
    ("positions fold into an existing row", "holdings", [],
     {"holdings": [H], "positions": {"net": [_p(tradingsymbol="INFY", quantity=3)]}}, False),
    ("positions twice for one new symbol", "holdings", [],
     {"holdings": [], "positions": {"net": [_p(), _p(quantity=2)]}}, False),
    # ---- ltp ----------------------------------------------------------------------------------
    ("ltp well-formed", "ltp", [["INFY", "TCS"]],
     {"ltp": {"NSE:INFY": {"last_price": 1500.5}, "NSE:TCS": {"last_price": 4000}}}, False),
    ("ltp reply None", "ltp", [["INFY"]], {"ltp": None}, False),
    ("ltp reply a list", "ltp", [["INFY"]], {"ltp": []}, False),
    ("ltp entry without last_price", "ltp", [["INFY"]], {"ltp": {"NSE:INFY": {}}}, False),
    ("ltp entry None", "ltp", [["INFY"]], {"ltp": {"NSE:INFY": None}}, False),
    ("ltp entry a string", "ltp", [["INFY"]], {"ltp": {"NSE:INFY": "x"}}, False),
    ("ltp entry a list", "ltp", [["INFY"]], {"ltp": {"NSE:INFY": [1]}}, False),
    ("ltp entry an int", "ltp", [["INFY"]], {"ltp": {"NSE:INFY": 5}}, False),
    ("ltp last_price a string", "ltp", [["INFY"]], {"ltp": {"NSE:INFY": {"last_price": "12.5"}}}, False),
    ("ltp last_price a padded string", "ltp", [["INFY"]],
     {"ltp": {"NSE:INFY": {"last_price": " 1e3 "}}}, False),
    ("ltp last_price garbage", "ltp", [["INFY"]], {"ltp": {"NSE:INFY": {"last_price": "abc"}}}, False),
    ("ltp last_price None", "ltp", [["INFY"]], {"ltp": {"NSE:INFY": {"last_price": None}}}, False),
    ("ltp last_price a list", "ltp", [["INFY"]], {"ltp": {"NSE:INFY": {"last_price": [1]}}}, False),
    ("ltp last_price a bool", "ltp", [["INFY"]], {"ltp": {"NSE:INFY": {"last_price": True}}}, False),
    ("ltp key without an exchange", "ltp", [["INFY"]], {"ltp": {"INFY": {"last_price": 1.0}}}, False),
    ("ltp key with two colons", "ltp", [["INFY"]], {"ltp": {"NSE:A:B": {"last_price": 1.0}}}, False),
    ("ltp a bad entry after a good one", "ltp", [["INFY", "TCS"]],
     {"ltp": {"NSE:INFY": {"last_price": 1.0}, "NSE:TCS": {}}}, False),
    ("ltp nothing asked", "ltp", [["", ""]], {"ltp": None}, False),
    # ---- quotes -------------------------------------------------------------------------------
    ("quotes well-formed", "quotes", [["INFY"]],
     {"quote": {"NSE:INFY": {"last_price": 1500.5, "net_change": 3.5, "volume": 0, "volume_traded": 99,
                             "ohlc": {"open": 1490, "high": 1510, "low": 1480, "close": 1497}}}}, False),
    ("quotes batch raises", "quotes", [["INFY"]], {"quote": {"__raise__": ["RuntimeError", "Too many"]}}, False),
    ("quotes reply None", "quotes", [["INFY"]], {"quote": None}, False),
    ("quotes reply a list", "quotes", [["INFY"]], {"quote": [1]}, False),
    ("quotes entry an int", "quotes", [["INFY"]], {"quote": {"NSE:INFY": 5}}, False),
    ("quotes entry None", "quotes", [["INFY"]], {"quote": {"NSE:INFY": None}}, False),
    ("quotes entry empty", "quotes", [["INFY"]], {"quote": {"NSE:INFY": {}}}, False),
    ("quotes ohlc a list", "quotes", [["INFY"]], {"quote": {"NSE:INFY": {"ohlc": [1]}}}, False),
    ("quotes ohlc an empty list", "quotes", [["INFY"]], {"quote": {"NSE:INFY": {"ohlc": []}}}, False),
    ("quotes ohlc a string", "quotes", [["INFY"]], {"quote": {"NSE:INFY": {"ohlc": "x"}}}, False),
    ("quotes key without an exchange", "quotes", [["INFY"]], {"quote": {"INFY": {"last_price": 1}}}, False),
    ("quotes key without an exchange and a bad ohlc", "quotes", [["INFY"]],
     {"quote": {"INFY": {"ohlc": 5}}}, False),
    ("quotes nothing asked", "quotes", [[""]], {"quote": None}, False),
    # ---- the list readers ---------------------------------------------------------------------
    ("trades None", "trades", [], {"trades": None}, False),
    ("trades a dict", "trades", [], {"trades": {"a": 1, "b": 2}}, False),
    ("trades an int", "trades", [], {"trades": 5}, False),
    ("trades zero", "trades", [], {"trades": 0}, False),
    ("trades a string", "trades", [], {"trades": "ab"}, False),
    ("trades a list", "trades", [], {"trades": [{"trade_id": "1"}]}, False),
    ("trades a bool", "trades", [], {"trades": True}, False),
    ("instruments a dict", "instruments", [], {"instruments": {"a": 1}}, False),
    ("instruments None", "instruments", [], {"instruments": None}, False),
    ("historical a dict", "historical", [], {"historical_data": {"a": 1}}, False),
    ("historical a float", "historical", [], {"historical_data": 1.5}, False),
    ("orders a string", "orders", [], {"orders": "xy"}, False),
    ("order_history None", "order_history", [], {"order_history": None}, False),
    ("get_gtts a dict", "get_gtts", [], {"get_gtts": {"k": 1}}, False),
    ("get_gtts an int", "get_gtts", [], {"get_gtts": 3}, False),
    # ---- available_cash -----------------------------------------------------------------------
    ("cash well-formed", "available_cash", [], {"margins": {"equity": {"available": {"live_balance": 1234.5}}}}, False),
    ("cash an int", "available_cash", [], {"margins": {"equity": {"available": {"live_balance": 12}}}}, False),
    ("cash reply None", "available_cash", [], {"margins": None}, False),
    ("cash reply empty", "available_cash", [], {"margins": {}}, False),
    ("cash reply a list", "available_cash", [], {"margins": []}, False),
    ("cash reply a string", "available_cash", [], {"margins": "x"}, False),
    ("cash equity a list", "available_cash", [], {"margins": {"equity": [1]}}, False),
    ("cash equity None", "available_cash", [], {"margins": {"equity": None}}, False),
    ("cash available missing", "available_cash", [], {"margins": {"equity": {}}}, False),
    ("cash live_balance missing", "available_cash", [], {"margins": {"equity": {"available": {}}}}, False),
    ("cash live_balance a string", "available_cash", [],
     {"margins": {"equity": {"available": {"live_balance": "12.5"}}}}, False),
    ("cash live_balance garbage", "available_cash", [],
     {"margins": {"equity": {"available": {"live_balance": "n/a"}}}}, False),
    ("cash live_balance None", "available_cash", [],
     {"margins": {"equity": {"available": {"live_balance": None}}}}, False),
    ("cash live_balance a dict", "available_cash", [],
     {"margins": {"equity": {"available": {"live_balance": {}}}}}, False),
    ("cash live_balance a bool", "available_cash", [],
     {"margins": {"equity": {"available": {"live_balance": False}}}}, False),
    ("cash live_balance an exponent string", "available_cash", [],
     {"margins": {"equity": {"available": {"live_balance": " 1e3 "}}}}, False),
    ("cash live_balance an underscored string", "available_cash", [],
     {"margins": {"equity": {"available": {"live_balance": "1_000.5"}}}}, False),
    # ---- margins / quote_raw: dict(...) -------------------------------------------------------
    ("margins a dict", "margins", [], {"margins": {"equity": {"net": 1}}}, False),
    ("margins with a segment", "margins", ["equity"], {"margins": {"net": 1}}, False),
    ("margins None", "margins", [], {"margins": None}, False),
    ("margins an empty list", "margins", [], {"margins": []}, False),
    ("margins pairs", "margins", [], {"margins": [["a", 1], ["b", [2]]]}, False),
    ("margins two-char strings", "margins", [], {"margins": ["ab", "cd"]}, False),
    ("margins an int element", "margins", [], {"margins": [1]}, False),
    ("margins a long element", "margins", [], {"margins": ["abc"]}, False),
    ("margins a long pair", "margins", [], {"margins": [["a", 1, 2]]}, False),
    ("margins a later bad element", "margins", [], {"margins": [["a", 1], 5]}, False),
    ("margins a None element", "margins", [], {"margins": [None]}, False),
    ("margins an int", "margins", [], {"margins": 5}, False),
    ("margins a string", "margins", [], {"margins": "ab"}, False),
    ("margins an empty string", "margins", [], {"margins": ""}, False),
    ("margins a bool", "margins", [], {"margins": True}, False),
    ("margins a dict element of two keys", "margins", [], {"margins": [{"k": 1, "v": 2}]}, False),
    ("margins a repeated key", "margins", [], {"margins": [["a", 1], ["b", 2], ["a", 3]]}, False),
    ("quote_raw a dict", "quote_raw", [["NSE:INFY"]], {"quote": {"NSE:INFY": {"last_price": 1}}}, False),
    ("quote_raw None", "quote_raw", [["NSE:INFY"]], {"quote": None}, False),
    ("quote_raw an empty list", "quote_raw", [["NSE:INFY"]], {"quote": []}, False),
    ("quote_raw zero", "quote_raw", [["NSE:INFY"]], {"quote": 0}, False),
    ("quote_raw an int", "quote_raw", [["NSE:INFY"]], {"quote": 5}, False),
    ("quote_raw pairs", "quote_raw", [["NSE:INFY"]], {"quote": [["k", "v"]]}, False),
    # ---- the documented GTT exception, live (a fake kc; never Kite) ---------------------------
    ("gtt live well-formed", "place_gtt_stop", ["INFY", 12, 1380.37, 1500.0],
     {"instruments": [{"tradingsymbol": "INFY", "tick_size": 0.05}], "place_gtt": {"trigger_id": 4242}}, True),
    ("gtt live tick 1", "place_gtt_stop", ["OFSS", 3, 9876.4, 10000.0],
     {"instruments": [{"tradingsymbol": "OFSS", "tick_size": 1}], "place_gtt": {"trigger_id": 1}}, True),
    ("gtt live tick a string", "place_gtt_stop", ["INFY", 2, 1380.37, 1500.0],
     {"instruments": [{"tradingsymbol": "INFY", "tick_size": "0.1"}], "place_gtt": {"trigger_id": 2}}, True),
    ("gtt live tick garbage", "place_gtt_stop", ["INFY", 2, 1380.37, 1500.0],
     {"instruments": [{"tradingsymbol": "INFY", "tick_size": "abc"}], "place_gtt": {"trigger_id": 2}}, True),
    ("gtt live tick None", "place_gtt_stop", ["INFY", 2, 1380.37, 1500.0],
     {"instruments": [{"tradingsymbol": "INFY", "tick_size": None}], "place_gtt": {"trigger_id": 2}}, True),
    ("gtt live tick a list", "place_gtt_stop", ["INFY", 2, 1380.37, 1500.0],
     {"instruments": [{"tradingsymbol": "INFY", "tick_size": [1]}], "place_gtt": {"trigger_id": 2}}, True),
    ("gtt live instrument without a symbol", "place_gtt_stop", ["INFY", 2, 1380.37, 1500.0],
     {"instruments": [{"tick_size": 1}], "place_gtt": {"trigger_id": 2}}, True),
    ("gtt live instrument a string", "place_gtt_stop", ["INFY", 2, 1380.37, 1500.0],
     {"instruments": ["x"], "place_gtt": {"trigger_id": 2}}, True),
    ("gtt live instrument symbol a list", "place_gtt_stop", ["INFY", 2, 1380.37, 1500.0],
     {"instruments": [{"tradingsymbol": ["x"]}], "place_gtt": {"trigger_id": 2}}, True),
    ("gtt live instrument symbol an int", "place_gtt_stop", ["INFY", 2, 1380.37, 1500.0],
     {"instruments": [{"tradingsymbol": 5, "tick_size": 1}], "place_gtt": {"trigger_id": 2}}, True),
    ("gtt live instruments None", "place_gtt_stop", ["INFY", 2, 1380.37, 1500.0],
     {"instruments": None, "place_gtt": {"trigger_id": 2}}, True),
    ("gtt live instruments raise", "place_gtt_stop", ["INFY", 2, 1380.37, 1500.0],
     {"instruments": {"__raise__": ["RuntimeError", "dump failed"]}, "place_gtt": {"trigger_id": 2}}, True),
    ("gtt live reply None", "place_gtt_stop", ["INFY", 2, 1380.37, 1500.0],
     {"instruments": [], "place_gtt": None}, True),
    ("gtt live reply empty", "place_gtt_stop", ["INFY", 2, 1380.37, 1500.0],
     {"instruments": [], "place_gtt": {}}, True),
    ("gtt live reply a list", "place_gtt_stop", ["INFY", 2, 1380.37, 1500.0],
     {"instruments": [], "place_gtt": []}, True),
    ("gtt live reply a string", "place_gtt_stop", ["INFY", 2, 1380.37, 1500.0],
     {"instruments": [], "place_gtt": "x"}, True),
    ("gtt live reply trigger_id a string", "place_gtt_stop", ["INFY", 2, 1380.37, 1500.0],
     {"instruments": [], "place_gtt": {"trigger_id": "77"}}, True),
    ("gtt live refused", "place_gtt_stop", ["INFY", 2, 1380.37, 1500.0],
     {"instruments": [], "place_gtt": {"__raise__": ["ValueError", "Trigger price should be a multiple"]}}, True),
    ("gtt delete live", "delete_gtt", [9, "INFY"], {"delete_gtt": {"trigger_id": 9}}, True),
    ("gtt delete live refused", "delete_gtt", [9, "INFY"],
     {"delete_gtt": {"__raise__": ["RuntimeError", "Invalid trigger"]}}, True),
]


def main(out_path: str) -> None:
    out = []
    names = set()
    for name, method, args, replies, live in CASES:
        assert name not in names, name
        names.add(name)
        k = _kite(replies)
        C.DRY_RUN = not live
        try:
            result = {"ok": _call(k, method, args)}
        except Exception as exc:                                          # noqa: BLE001
            result = {"raise": [type(exc).__name__, str(exc)]}
        out.append({"name": name, "method": method, "args": args, "replies": replies,
                    "live": live, "calls": k.kc.calls, **result})
    C.DRY_RUN = True
    with open(out_path, "w") as fh:
        json.dump({"cases": out}, fh, indent=1, sort_keys=False)
        fh.write("\n")
    print(f"{len(out)} cases -> {out_path}")


if __name__ == "__main__":
    main(sys.argv[1])
