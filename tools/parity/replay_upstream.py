"""The replay upstream: recorded Kite REST and NSE answers on a loopback port, for both sides at once.

The port-level replay (``replay_provider.py`` / ``replay.rs``) answers the nightly's provider calls.
Wave 6's tasks also build their own clients — a Kite client for quotes during a session, the
read-only holdings sync, the NFO instrument dump and option-chain quotes; the NSE adapter for the
F&O bhavcopy and ban list — so the harness replays those **at the wire**, where both production
stacks (Python's ``kiteconnect`` + ``KiteProvider``; Rust's ``KiteConnect`` + ``KiteProvider``) parse
the same bytes. Nothing is ever fetched live (DECISIONS-RS R8.3).

Two halves, both under a bundle's ``upstream/`` directory (written by ``replay_build.py``):

``upstream/archive/``   a raw-file archive tree (``nse/fo-bhavcopy/<date>.zip``,
                        ``nse/fo-ban-list/<date>.csv``, ...). The NSE adapter is archive-first
                        (``fetch_and_archive``: an archived key is never re-fetched), so the
                        harness copies this tree into each side's ``BASKFY_RAW_ARCHIVE_DIR`` and
                        the adapter parses the recorded raw file without a request. Files a side
                        writes there are compared afterwards.
``upstream/kite/``      Kite REST answers this server serves:
                        ``quote/<HHMM>.json`` ``{"NSE:SYM": <raw quote>, ...}`` — ``/quote``,
                        ``/quote/ohlc`` and ``/quote/ltp`` answer the asked keys from the latest
                        recorded minute at or before the case's clock (Kite omits an unknown key);
                        ``holdings.json``, ``margins.json``, ``trades.json``, ``profile.json``;
                        ``instruments/<EXCHANGE>.csv``; ``historical/<token>/<interval>.json``
                        (candles, filtered to ``from``/``to``).
``upstream/http/``      any other recorded GET, answered verbatim by path (NSE fallbacks).

Anything else answers 404 (a provider error on both sides, never data). **Any write verb** is
refused with 405 and recorded as a law-2 violation — the harness fails the case — except Kite's
two margin *calculators* (``POST /margins/basket``, ``/margins/orders``), which place nothing and
answer only if recorded. Every request is logged per side and compared.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

#: Kite calculators that are POSTs but place nothing.
CALCULATORS = ("/margins/basket", "/margins/orders")


class UpstreamServer:
    """A threaded loopback HTTP server over one bundle's ``upstream/`` directory."""

    def __init__(self, bundle: Path, port: int) -> None:
        self.root = bundle / "upstream"
        self.port = port
        self.minute = "1530"
        self.requests: list[dict[str, Any]] = []
        self.violations: list[str] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def set_clock(self, now_ist: str) -> None:
        """The case's IST moment picks which recorded quote minute answers."""
        moment = dt.datetime.fromisoformat(now_ist)
        self.minute = moment.strftime("%H%M")

    def mark(self) -> int:
        with self._lock:
            return len(self.requests)

    def since(self, mark: int) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.requests[mark:])

    def start(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                return

            def _record(self) -> tuple[str, dict[str, list[str]]]:
                split = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(split.query, keep_blank_values=True)
                with owner._lock:
                    owner.requests.append({
                        "method": self.command,
                        "path": split.path,
                        "query": {k: sorted(v) for k, v in sorted(query.items())},
                    })
                return split.path, query

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, payload: Any) -> None:
                self._send(status, json.dumps(payload).encode(), "application/json")

            def _missing(self, path: str) -> None:
                self._json(404, {"status": "error", "error_type": "GeneralException",
                                 "message": f"[replay upstream] no recorded answer for {path}"})

            def do_GET(self) -> None:  # noqa: N802
                path, query = self._record()
                body = owner.answer(path, query)
                if body is None:
                    self._missing(path)
                    return
                status, data, ctype = body
                self._send(status, data, ctype)

            def _write_verb(self) -> None:
                path, _ = self._record()
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                if self.command == "POST" and path in CALCULATORS:
                    recorded = owner.root / "kite" / f"{path.strip('/').replace('/', '_')}.json"
                    if recorded.is_file():
                        self._send(200, recorded.read_bytes(), "application/json")
                    else:
                        self._missing(path)
                    return
                with owner._lock:
                    owner.violations.append(f"{self.command} {path}")
                self._json(405, {"status": "error", "error_type": "InputException",
                                 "message": "[replay upstream] write verbs are refused (law 2)"})

            do_POST = do_PUT = do_DELETE = do_PATCH = _write_verb  # noqa: N815

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # --- answers ---------------------------------------------------------------------------------

    def _quotes(self) -> dict[str, Any]:
        directory = self.root / "kite" / "quote"
        if not directory.is_dir():
            return {}
        minutes = sorted(p.stem for p in directory.glob("*.json"))
        if not minutes:
            return {}
        eligible = [m for m in minutes if m <= self.minute] or minutes[:1]
        return json.loads((directory / f"{eligible[-1]}.json").read_text())

    def answer(self, path: str, query: dict[str, list[str]]) -> tuple[int, bytes, str] | None:
        kite = self.root / "kite"
        ok = lambda data: (200, json.dumps({"status": "success", "data": data}).encode(),  # noqa: E731
                           "application/json")
        if path in ("/quote", "/quote/ohlc", "/quote/ltp"):
            quotes = self._quotes()
            out: dict[str, Any] = {}
            for key in query.get("i", []):
                q = quotes.get(key)
                if q is None:
                    continue
                if path == "/quote/ltp":
                    out[key] = {"instrument_token": q["instrument_token"], "last_price": q["last_price"]}
                elif path == "/quote/ohlc":
                    out[key] = {"instrument_token": q["instrument_token"], "last_price": q["last_price"],
                                "ohlc": q["ohlc"]}
                else:
                    out[key] = q
            return ok(out)
        simple = {
            "/portfolio/holdings": "holdings.json",
            "/user/margins": "margins.json",
            "/trades": "trades.json",
            "/user/profile": "profile.json",
        }
        if path in simple:
            f = kite / simple[path]
            return ok(json.loads(f.read_text())) if f.is_file() else None
        if path.startswith("/user/margins/"):
            f = kite / "margins.json"
            if not f.is_file():
                return None
            segment = path.rsplit("/", 1)[1]
            data = json.loads(f.read_text())
            return ok(data[segment]) if segment in data else None
        if path.startswith("/instruments/historical/"):
            parts = path.split("/")
            if len(parts) != 5:
                return None
            token, interval = parts[3], parts[4]
            f = kite / "historical" / token / f"{interval}.json"
            if not f.is_file():
                return None
            candles = json.loads(f.read_text())
            lo = (query.get("from") or [""])[0]
            hi = (query.get("to") or ["~"])[0]
            kept = [c for c in candles if lo <= c[0][:19].replace("T", " ") <= hi]
            return ok({"candles": kept})
        if path.startswith("/instruments/"):
            f = kite / "instruments" / f"{path.rsplit('/', 1)[1]}.csv"
            return (200, f.read_bytes(), "text/csv") if f.is_file() else None
        base = (self.root / "http").resolve()
        recorded = (base / path.lstrip("/")).resolve()
        if recorded.is_file() and base in recorded.parents:
            return (200, recorded.read_bytes(), "application/octet-stream")
        return None
