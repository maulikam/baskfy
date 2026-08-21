"""NSE public-data adapter — the live index board, and each index's constituent list.

Two NSE surfaces with very different tolerances, and that difference is the whole design
of this module.

  www.nseindia.com/api/allIndices  — the live board. Cookie-gated (a bare request is 403,
      so a browser-shaped session has to visit a page first) but content to be polled.
  nsearchives.nseindia.com/...     — the constituent CSVs. No cookie needed, but the host
      bans a burst outright. Probing 139 index names two ways in ten threads turned a
      working IP into "Access Denied" for EVERY url on that host, including ones that had
      answered a minute earlier, and it stayed that way long after the burst stopped.

So constituents are fetched ONE index at a time, only when someone actually opens that
index, never closer together than MIN_ARCHIVE_GAP, and cached for a month — a constituent
list changes at a semi-annual review, not intraday. On a 403 the module stops asking for
ARCHIVE_BACKOFF and serves whatever it already has, because a ban costs every index at
once rather than the one being fetched. That is also why nothing here pre-warms the cache:
a helpful-looking "fetch them all on startup" is exactly the burst that gets the box
blocked, and the box is the machine holding the broker session.

Nothing in here decides anything about the market. It reads public data and caches it.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
import os
import re
import threading
import time
from typing import Any

import requests

log = logging.getLogger("nse")

BOARD_URL = "https://www.nseindia.com/api/allIndices"
ARCHIVE_BASE = "https://nsearchives.nseindia.com/content/indices"
PRIME_URLS = ("https://www.nseindia.com/",
              "https://www.nseindia.com/market-data/live-equity-market")

BOARD_CACHE = "data/.nse_board.json"
CONSTITUENT_CACHE = "data/.nse_constituents.json"

BOARD_TTL = 90                    # seconds; the board is a quote feed
CONSTITUENT_TTL = 30 * 86400      # a month; membership changes at a semi-annual review
MIN_ARCHIVE_GAP = 3.0             # seconds between two archive requests, process-wide
ARCHIVE_BACKOFF = 3600            # seconds of silence after the host says no
SESSION_TTL = 900                 # re-prime the cookie jar this often

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_lock = threading.Lock()
_session: requests.Session | None = None
_session_at = 0.0
_last_archive_at = 0.0
_blocked_until = 0.0


# --- session ---------------------------------------------------------------------------

def _fresh_session() -> requests.Session:
    """A browser-shaped session with NSE's cookies in it.

    The API refuses a bare client, so the jar has to be filled by visiting real pages
    first. Priming failures are not fatal: the request that follows may still work, and
    raising here would take the page down over a warm-up step.
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    })
    for url in PRIME_URLS:
        try:
            s.get(url, timeout=15, headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none", "Upgrade-Insecure-Requests": "1",
            })
        except Exception as e:                                   # noqa: BLE001
            log.debug("nse prime %s failed: %s", url, e)
    return s


def _session_get() -> requests.Session:
    global _session, _session_at
    now = time.time()
    if _session is None or now - _session_at > SESSION_TTL:
        _session = _fresh_session()
        _session_at = now
    return _session


# --- disk cache ------------------------------------------------------------------------

def _read_cache(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:                                            # noqa: BLE001
        return {}


def _write_cache(path: str, payload: dict) -> None:
    """Write via a temp file in the same directory, then rename.

    A half-written cache read by the next request is worse than no cache: the page would
    render an index board with three rows in it and look like the market vanished.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


# --- the live board --------------------------------------------------------------------

def _num(v: Any) -> float | None:
    if v in (None, "", "-", "NA"):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _board_row(r: dict) -> dict:
    """One index, reduced to what the desk shows.

    `index` is the full name ("NIFTY FINANCIAL SERVICES"); `indexSymbol` is the ticker
    abbreviation ("NIFTY FIN SERVICE"). Only the full name resolves an archive filename,
    so the full name is what identifies a row everywhere downstream.
    """
    adv, dec, unch = _num(r.get("advances")), _num(r.get("declines")), _num(r.get("unchanged"))
    return {
        "name": (r.get("index") or "").strip(),
        "ticker": (r.get("indexSymbol") or "").strip(),
        "group": (r.get("key") or "").strip(),
        "last": _num(r.get("last")),
        "change": _num(r.get("variation")),
        "pct": _num(r.get("percentChange")),
        "open": _num(r.get("open")),
        "high": _num(r.get("high")),
        "low": _num(r.get("low")),
        "prev_close": _num(r.get("previousClose")),
        "year_high": _num(r.get("yearHigh")),
        "year_low": _num(r.get("yearLow")),
        "pe": _num(r.get("pe")),
        "pb": _num(r.get("pb")),
        "dy": _num(r.get("dy")),
        "ret_1y": _num(r.get("perChange365d")),
        "ret_30d": _num(r.get("perChange30d")),
        "advances": int(adv) if adv is not None else None,
        "declines": int(dec) if dec is not None else None,
        "unchanged": int(unch) if unch is not None else None,
    }


def board(force: bool = False) -> dict:
    """The live index board, or the last one that worked.

    Returns {"rows", "fetched_at", "stale", "error"}. A failure NEVER raises: an index
    board that is four minutes old is a working page, and an exception is a broken one.
    """
    cached = _read_cache(BOARD_CACHE)
    age = time.time() - float(cached.get("fetched_at_epoch") or 0)
    if cached.get("rows") and not force and age < BOARD_TTL:
        return {**cached, "stale": False, "error": None}

    try:
        s = _session_get()
        r = s.get(BOARD_URL, timeout=20, headers={
            "Accept": "*/*",
            "Referer": "https://www.nseindia.com/market-data/live-equity-market",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
        })
        r.raise_for_status()
        raw = r.json().get("data") or []
        rows = [_board_row(x) for x in raw if (x.get("index") or "").strip()]
        if not rows:
            raise ValueError("board returned no rows")
        payload = {
            "rows": rows,
            "fetched_at": dt.datetime.now().strftime("%d %b %Y %H:%M"),
            "fetched_at_epoch": time.time(),
        }
        _write_cache(BOARD_CACHE, payload)
        return {**payload, "stale": False, "error": None}
    except Exception as e:                                       # noqa: BLE001
        log.warning("nse board fetch failed: %s", e)
        global _session
        _session = None                                          # force a re-prime next time
        if cached.get("rows"):
            return {**cached, "stale": True, "error": str(e)}
        return {"rows": [], "fetched_at": None, "stale": True, "error": str(e)}


# --- constituents ----------------------------------------------------------------------

def slug(name: str) -> str:
    """The archive's filename stem for an index name.

    NSE strips every separator: "NIFTY OIL & GAS" -> "niftyoilgas". The suffix is not
    consistent across the archive (`ind_niftymedialist.csv` but
    `ind_niftytotalmarket_list.csv`), so both spellings are tried and the one that
    answered is remembered in the cache.
    """
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _archive_candidates(name: str) -> list[str]:
    s = slug(name)
    return [f"ind_{s}list.csv", f"ind_{s}_list.csv"]


def _parse_constituents(text: str) -> list[dict]:
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        sym = (r.get("Symbol") or "").strip()
        if not sym:
            continue
        rows.append({
            "symbol": sym,
            "company": (r.get("Company Name") or "").strip(),
            "industry": (r.get("Industry") or "").strip(),
            "series": (r.get("Series") or "").strip(),
            "isin": (r.get("ISIN Code") or "").strip(),
        })
    return rows


def _fetch_constituents(name: str) -> list[dict]:
    """One index's list, throttled and rate-limit aware. Raises on failure."""
    global _last_archive_at, _blocked_until

    if time.time() < _blocked_until:
        raise RuntimeError("NSE archive is refusing requests; backing off")

    last_err: Exception | None = None
    for cand in _archive_candidates(name):
        gap = MIN_ARCHIVE_GAP - (time.time() - _last_archive_at)
        if gap > 0:
            time.sleep(gap)
        _last_archive_at = time.time()
        try:
            r = requests.get(f"{ARCHIVE_BASE}/{cand}", timeout=25, headers={
                "User-Agent": UA,
                "Referer": "https://www.nseindia.com/",
                "Accept": "text/csv,application/csv,*/*",
            })
        except Exception as e:                                   # noqa: BLE001
            last_err = e
            continue
        if r.status_code == 403:
            # The host is not saying "no such index", it is saying "not you". Every other
            # index would get the same answer, so stop asking on all of them.
            _blocked_until = time.time() + ARCHIVE_BACKOFF
            raise RuntimeError("NSE archive returned 403 — backing off for an hour")
        if r.status_code == 200 and r.text.lstrip().startswith("Company Name"):
            rows = _parse_constituents(r.text)
            if rows:
                return rows
        last_err = RuntimeError(f"{cand} -> HTTP {r.status_code}")
    raise RuntimeError(f"no constituent list for {name!r} ({last_err})")


def constituents(name: str) -> dict:
    """Constituents of one index, from cache when possible.

    Returns {"rows", "fetched_at", "stale", "error"}. Like board(), never raises — a
    drill-down that cannot load is a message in the panel, not a 500 on the page.
    """
    key = slug(name)
    with _lock:
        cache = _read_cache(CONSTITUENT_CACHE)
        hit = cache.get(key) or {}
        age = time.time() - float(hit.get("fetched_at_epoch") or 0)
        if hit.get("rows") and age < CONSTITUENT_TTL:
            return {**hit, "stale": False, "error": None}

        try:
            rows = _fetch_constituents(name)
        except Exception as e:                                   # noqa: BLE001
            log.info("constituents(%s) failed: %s", name, e)
            if hit.get("rows"):
                return {**hit, "stale": True, "error": str(e)}
            return {"rows": [], "fetched_at": None, "stale": True, "error": str(e)}

        entry = {
            "rows": rows,
            "fetched_at": dt.datetime.now().strftime("%d %b %Y %H:%M"),
            "fetched_at_epoch": time.time(),
        }
        cache[key] = entry
        _write_cache(CONSTITUENT_CACHE, cache)
        return {**entry, "stale": False, "error": None}


def cached_index_names() -> list[str]:
    """Which indices already have a constituent list on disk — no network."""
    return sorted(_read_cache(CONSTITUENT_CACHE).keys())
