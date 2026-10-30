"""The replay Kite for the PYTHON desk — harness-only (DECISIONS-RS R8.3).

``ReplayKiteConnect`` is ``kiteconnect.KiteConnect`` with ``_request`` replaced: every SDK method
(holdings, ltp, place_order, place_gtt, ...) still shapes its request and parses its answer
exactly as kiteconnect 5.2.1 does, but the request never leaves the process. It is answered from
a recorded fixture and **recorded** in a call log. The desk's own code is not touched;
``desk_harness.py`` swaps the class in before the desk imports it.

Its twin is ``rust/crates/baskfy-desk/src/fake_kite.rs``; the fixture format, the answering rules
and the log line are documented there and must stay identical (``desk_diff.py`` compares the two
desks' logs). In short:

* ``user.profile``: TokenException (403) when ``session.authed`` is false, else ``routes``.
* ``market.quote`` / ``.ltp`` / ``.ohlc``: requested keys present in ``quotes``, request order.
* ``market.historical``: ``historical[token][interval]`` candles within ``from[:10]..=to[:10]``.
* ``market.instruments``: ``instruments/<EXCHANGE>.csv`` bytes (the SDK parses them).
* ``api.token``: a fixed replay session unless recorded.
* writes: an ``error`` recorded for the route raises it; otherwise REPLAY order ids counting from
  1 and trigger ids from 900001; modify/cancel/delete echo the id.
* anything unrecorded: ``DataException("replay kite has no recording for route '<route>'")``.
* log line: ``json.dumps({"seq", "route", "method", "url_args", "params", "is_json",
  "query_params"})`` with ``api_key``/``checksum``/``access_token``/``request_token`` redacted.
"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

from kiteconnect import KiteConnect
from kiteconnect import exceptions as ex

REDACTED = ("api_key", "checksum", "access_token", "request_token")
WRITE_ROUTES = ("order.place", "order.modify", "order.cancel", "gtt.place", "gtt.modify", "gtt.delete")


class ReplayState:
    """The fixture, the counters and the call log: one per process, shared by every client."""

    def __init__(self, fixture_dir: str, log_path: str | None) -> None:
        self.dir = Path(fixture_dir)
        self.fixture: dict[str, Any] = json.loads((self.dir / "kite.json").read_text())
        self.log_path = log_path
        self.calls: list[dict[str, Any]] = []
        self.orders_placed = 0
        self.gtts_placed = 0
        self.lock = threading.Lock()

    def record(self, line: dict[str, Any]) -> None:
        with self.lock:
            self.calls.append(line)
            line = {"seq": len(self.calls), **line}
            self.calls[-1] = line
            if self.log_path:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(line, default=str) + "\n")


STATE: ReplayState | None = None


def install(fixture_dir: str, log_path: str | None) -> ReplayState:
    global STATE  # noqa: PLW0603 - one replay per harness process, by design
    STATE = ReplayState(fixture_dir, log_path)
    return STATE


def _redact(params: Any) -> Any:  # noqa: ANN401
    if isinstance(params, dict):
        return {k: ("<redacted>" if k in REDACTED else v) for k, v in params.items()}
    return params


def _raise(error: dict[str, Any]) -> None:
    cls = getattr(ex, str(error.get("type") or ""), ex.GeneralException)
    kwargs = {"code": int(error["code"])} if error.get("code") is not None else {}
    raise cls(str(error.get("message") or ""), **kwargs)


class ReplayKiteConnect(KiteConnect):
    """``KiteConnect`` that answers from the replay fixture and never opens a socket."""

    def _request(self, route, method, url_args=None, params=None, is_json=False,  # noqa: ANN001, ANN202, PLR0913
                 query_params=None):
        state = STATE
        if state is None:
            raise ex.DataException("replay kite is not installed")
        state.record({"route": route, "method": method,
                      "url_args": copy.deepcopy(url_args), "params": _redact(copy.deepcopy(params)),
                      "is_json": bool(is_json), "query_params": copy.deepcopy(query_params)})
        routes = state.fixture.get("routes") or {}
        entry = routes.get(route)

        if route in WRITE_ROUTES:
            if isinstance(entry, dict) and isinstance(entry.get("error"), dict):
                _raise(entry["error"])
            with state.lock:
                if route == "order.place":
                    state.orders_placed += 1
                    return {"order_id": f"REPLAY{state.orders_placed:06d}"}
                if route == "gtt.place":
                    state.gtts_placed += 1
                    return {"trigger_id": 900000 + state.gtts_placed}
            if route in ("order.modify", "order.cancel"):
                return {"order_id": (url_args or {}).get("order_id")}
            return {"trigger_id": (url_args or {}).get("trigger_id")}

        if route == "user.profile":
            if (state.fixture.get("session") or {}).get("authed") is False:
                raise ex.TokenException("Incorrect `api_key` or `access_token`.", code=403)
        if route in ("market.quote", "market.quote.ltp", "market.quote.ohlc"):
            if isinstance(entry, dict) and isinstance(entry.get("error"), dict):
                _raise(entry["error"])
            table = state.fixture.get("quotes") or {}
            out: dict[str, Any] = {}
            for key in (params or {}).get("i") or []:
                if key in out or not isinstance(table.get(key), dict):
                    continue
                q = table[key]
                keep = {"market.quote.ltp": ("instrument_token", "last_price"),
                        "market.quote.ohlc": ("instrument_token", "last_price", "ohlc")}.get(route)
                out[key] = copy.deepcopy({k: q[k] for k in keep if k in q} if keep else q)
            return out
        if route in ("market.instruments", "market.instruments.all"):
            name = f"{(url_args or {}).get('exchange')}.csv" if route == "market.instruments" else "ALL.csv"
            path = state.dir / "instruments" / name
            if not path.is_file():
                raise ex.DataException(f"replay kite has no recording for route '{route}'")
            return path.read_bytes()
        if route == "market.historical":
            token = str((url_args or {}).get("instrument_token"))
            interval = (url_args or {}).get("interval")
            series = ((state.fixture.get("historical") or {}).get(token) or {}).get(interval)
            if not isinstance(series, list):
                raise ex.DataException(
                    "replay kite has no recording for route 'market.historical' with "
                    f"instrument_token={token} interval={interval}")
            lo, hi = str(params["from"])[:10], str(params["to"])[:10]
            return {"candles": [copy.deepcopy(c) for c in series
                                if isinstance(c, list) and c and isinstance(c[0], str)
                                and lo <= c[0][:10] <= hi]}
        if route == "user.margins.segment":
            whole = (routes.get("user.margins") or {}).get("data")
            segment = (url_args or {}).get("segment")
            if not isinstance(whole, dict) or segment not in whole:
                raise ex.DataException(f"replay kite has no margins for segment '{segment}'")
            return copy.deepcopy(whole[segment])
        if route == "api.token" and not isinstance(entry, dict):
            return {"access_token": "replay-access-token", "user_id": "REPLAY", "login_time": None}

        if not isinstance(entry, dict):
            raise ex.DataException(f"replay kite has no recording for route '{route}'")
        if isinstance(entry.get("error"), dict):
            _raise(entry["error"])
        if isinstance(entry.get("by_url_arg"), str):
            arg = entry["by_url_arg"]
            key = str((url_args or {}).get(arg))
            answers = entry.get("answers") or {}
            if key not in answers:
                raise ex.DataException(
                    f"replay kite has no recording for route '{route}' with {arg}={key}")
            return copy.deepcopy(answers[key])
        if "data" not in entry:
            raise ex.DataException(f"replay kite: route '{route}' has no data")
        return copy.deepcopy(entry["data"])
