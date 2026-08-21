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
CLOSE_CACHE = "data/.nse_close.json"

BOARD_TTL = 90                    # seconds; the board is a quote feed
CLOSE_BACK_DAYS = 6               # walk back over a weekend plus a holiday or two
# TODAY'S report is still being written — the file published minutes after the close held
# 148 indices where the previous day's finished file held 164. So today's copy is re-read
# until the day is over; a past day's report never changes and is kept for good.
CLOSE_TODAY_TTL = 1800
CONSTITUENT_TTL = 30 * 86400      # a month; membership changes at a semi-annual review
FAILURE_TTL = 6 * 3600            # how long a "no such list" answer is remembered
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


# --- the daily close report --------------------------------------------------------------
# allIndices is a LIVE feed and carries only what NSE quotes intraday — 139 indices. The
# archive's daily close report carries every published index, 164 of them, and is a strict
# superset: every board name appears in it. So the report is the spine of the list and the
# live feed enriches it, rather than the other way round. Twenty-one equity indices —
# Sugar & Ethanol, NBFC, Insurance, Power, Capital Goods, Hospitals, the corporate-group
# indices and others — exist ONLY here, which is why a board-only page silently lost them.
#
# The report also supplies volume and turnover, which the live feed does not publish at all.

def _close_url(d: dt.date) -> str:
    return f"{ARCHIVE_BASE}/ind_close_all_{d.strftime('%d%m%Y')}.csv"


def _close_num(v: Any) -> float | None:
    """The report writes an absent value as "-" and a bare decimal as ".42"."""
    s = str(v or "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_close(text: str) -> list[dict]:
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        name = (r.get("Index Name") or "").strip()
        if not name:
            continue
        rows.append({
            "name": name,
            "date": (r.get("Index Date") or "").strip(),
            "open": _close_num(r.get("Open Index Value")),
            "high": _close_num(r.get("High Index Value")),
            "low": _close_num(r.get("Low Index Value")),
            "last": _close_num(r.get("Closing Index Value")),
            "change": _close_num(r.get("Points Change")),
            "pct": _close_num(r.get("Change(%)")),
            "volume": _close_num(r.get("Volume")),
            "turnover": _close_num(r.get("Turnover (Rs. Cr.)")),
            "pe": _close_num(r.get("P/E")),
            "pb": _close_num(r.get("P/B")),
            "dy": _close_num(r.get("Div Yield")),
        })
    return rows


def close_report(today: dt.date | None = None) -> dict:
    """The most recent daily index close report, walking back over non-trading days.

    A published report never changes, so a date that succeeded is cached forever and only
    the search for a NEWER one costs a request. Today's file does not exist until after the
    close, so during the session this legitimately returns yesterday's — every row carries
    its own date and the page labels it rather than implying it is live.
    """
    today = today or dt.date.today()
    with _lock:
        cache = _read_cache(CLOSE_CACHE)

        for back in range(CLOSE_BACK_DAYS + 1):
            d = today - dt.timedelta(days=back)
            key = d.isoformat()
            hit = cache.get(key)
            if hit:
                settled = d != today or (
                    time.time() - float(hit.get("fetched_at_epoch") or 0) < CLOSE_TODAY_TTL)
                if settled:
                    return {**hit, "stale": back > 0, "error": None}
            elif d.weekday() >= 5:                  # NSE publishes nothing for a weekend
                continue
            try:
                rows = _fetch_close(d)
            except Exception as e:                  # noqa: BLE001
                log.info("close report %s failed: %s", key, e)
                if hit:                             # a stale copy beats no list at all
                    return {**hit, "stale": True, "error": str(e)}
                break                               # blocked or offline: stop asking
            if rows:
                entry = {"rows": rows, "as_of": key,
                         "fetched_at": dt.datetime.now().strftime("%d %b %Y %H:%M"),
                         "fetched_at_epoch": time.time()}
                cache[key] = entry
                _write_cache(CLOSE_CACHE, cache)
                return {**entry, "stale": back > 0, "error": None}
            if hit:                                 # today's file vanished? keep what we had
                return {**hit, "stale": True, "error": None}

        # Nothing fetched. Fall back to the newest report already on disk — an index list
        # from last Thursday is a working page; an empty one is not.
        if cache:
            newest = max(cache)
            return {**cache[newest], "stale": True, "error": "could not reach NSE's archive"}
        return {"rows": [], "as_of": None, "fetched_at": None, "stale": True,
                "error": "no close report available"}


def _fetch_close(d: dt.date) -> list[dict]:
    """One day's report, through the same throttle and ban-backoff as constituent lists."""
    global _last_archive_at, _blocked_until

    if time.time() < _blocked_until:
        raise RuntimeError("NSE archive is refusing requests; backing off")

    gap = MIN_ARCHIVE_GAP - (time.time() - _last_archive_at)
    if gap > 0:
        time.sleep(gap)
    _last_archive_at = time.time()

    r = requests.get(_close_url(d), timeout=25, headers={
        "User-Agent": UA, "Referer": "https://www.nseindia.com/",
        "Accept": "text/csv,application/csv,*/*",
    })
    if r.status_code == 403:
        _blocked_until = time.time() + ARCHIVE_BACKOFF
        raise RuntimeError("NSE archive returned 403 — backing off for an hour")
    if r.status_code == 404:
        return []                                   # a holiday, or today before the close
    if r.status_code != 200 or not r.text.lstrip().startswith("Index Name"):
        raise RuntimeError(f"close report {d} -> HTTP {r.status_code}")
    return _parse_close(r.text)


def full_board() -> dict:
    """Every published index: the close report as the spine, the live feed layered on top.

    Where an index appears in both, the live values win for level and change — they are
    today's, the report's may be yesterday's — while volume, turnover and the report's
    properly-cased name are kept from the report. Rows the live feed does not carry keep
    their report values and are marked source="eod" so the page can say so.
    """
    rep = close_report()
    live = board()
    by_live = {slug(r["name"]): r for r in live["rows"]}

    rows: list[dict] = []
    seen: set[str] = set()
    for r in rep["rows"]:
        k = slug(r["name"])
        seen.add(k)
        lv = by_live.get(k)
        if lv:
            rows.append({**lv,
                         "name": r["name"],                    # the report's nicer casing
                         "volume": r["volume"], "turnover": r["turnover"],
                         "as_of": None, "source": "live"})
        else:
            rows.append({**r, "ticker": "", "group": "",
                         "prev_close": None, "year_high": None, "year_low": None,
                         "ret_1y": None, "ret_30d": None,
                         "advances": None, "declines": None, "unchanged": None,
                         "as_of": r["date"] or rep["as_of"], "source": "eod"})

    # A live index the report somehow lacks must still appear. Today the report is a strict
    # superset, but that is NSE's choice to change, not an invariant to depend on.
    for k, lv in by_live.items():
        if k not in seen:
            rows.append({**lv, "volume": None, "turnover": None,
                         "as_of": None, "source": "live"})

    return {
        "rows": rows,
        "fetched_at": live.get("fetched_at") or rep.get("fetched_at"),
        "live_count": len(by_live),
        "eod_only": sum(1 for r in rows if r["source"] == "eod"),
        "close_as_of": rep.get("as_of"),
        "close_stale": rep.get("stale"),
        "stale": live.get("stale") and rep.get("stale"),
        "error": live.get("error") or rep.get("error"),
    }


# --- constituents ----------------------------------------------------------------------

def slug(name: str) -> str:
    """The archive's filename stem for an index name.

    NSE strips every separator: "NIFTY OIL & GAS" -> "niftyoilgas". The suffix is not
    consistent across the archive (`ind_niftymedialist.csv` but
    `ind_niftytotalmarket_list.csv`), so both spellings are tried and the one that
    answered is remembered in the cache.
    """
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


# An acronym is part of the filename as written — ind_niftyNBFC_list.csv, not ...Nbfc... —
# so it has to survive intact. Length cannot make this call: NBFC and BANK are both four
# letters and only one is an acronym. This only matters for a name arriving in full caps
# from the live feed; the close report supplies almost every row already title-cased.
ACRONYMS = {"IT", "NBFC", "FMCG", "PSU", "PSE", "CPSE", "ESG", "MNC", "EV", "IPO", "SME",
            "FPI", "USD", "TR", "PR", "MAATR", "REIT", "REITS", "INVIT", "INVITS", "GS"}


def camel(name: str) -> str:
    """"Nifty Sugar & Ethanol" -> "niftySugarEthanol"."""
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", name or "") if p]
    out = []
    for i, p in enumerate(parts):
        if i == 0:
            out.append(p.lower())
        elif p.isupper() and p not in ACRONYMS:
            out.append(p.title())          # PRIVATE -> Private, but NBFC stays NBFC
        else:
            out.append(p[0].upper() + p[1:])
    return "".join(out)


# NSE spells these filenames four different ways across two hosts, with no rule that
# predicts which. All four are real, and were confirmed by fetching them:
#   ind_niftymedialist.csv          nsearchives   Media, Bank, IT, 500, Private Bank
#   ind_niftytotalmarket_list.csv   nsearchives   Total Market
#   ind_niftyHospitals_list.csv     niftyindices  Hospitals, NBFC, Power, Sugar & Ethanol
#   ind_niftyIndiaDefence_list.csv  niftyindices  India Defence
# A wrong guess on niftyindices returns 200 with an HTML page rather than a 404, which is
# why every response is validated on the CSV header instead of the status code. The winner
# is cached with the constituent rows, so this search runs once a month per index.
NIFTYINDICES = "https://www.niftyindices.com/IndexConstituent"


def _archive_candidates(name: str) -> list[str]:
    s, c = slug(name), camel(name)
    return [
        f"{ARCHIVE_BASE}/ind_{s}list.csv",
        f"{NIFTYINDICES}/ind_{c}_list.csv",
        f"{ARCHIVE_BASE}/ind_{s}_list.csv",
        f"{NIFTYINDICES}/ind_{c}list.csv",
    ]


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
    for url in _archive_candidates(name):
        gap = MIN_ARCHIVE_GAP - (time.time() - _last_archive_at)
        if gap > 0:
            time.sleep(gap)
        _last_archive_at = time.time()
        try:
            r = requests.get(url, timeout=25, headers={
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
        # Validated on the header, never the status: a wrong name on niftyindices.com
        # answers 200 with an HTML page, which would otherwise parse to zero constituents
        # and be cached for a month as "this index is empty".
        if r.status_code == 200 and r.text.lstrip().startswith("Company Name"):
            rows = _parse_constituents(r.text)
            if rows:
                return rows
        last_err = RuntimeError(f"{url.rsplit('/', 1)[-1]} -> HTTP {r.status_code}")
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

        # An index whose filename none of the four spellings resolves would otherwise cost
        # four requests EVERY time it is opened. Remember the miss for a few hours — long
        # enough to stop the repetition, short enough that a newly published list appears
        # the same day.
        failed_age = time.time() - float(hit.get("failed_at_epoch") or 0)
        if not hit.get("rows") and hit.get("failed_at_epoch") and failed_age < FAILURE_TTL:
            return {"rows": [], "fetched_at": None, "stale": True,
                    "error": hit.get("error") or "no constituent list published"}

        try:
            rows = _fetch_constituents(name)
        except Exception as e:                                   # noqa: BLE001
            log.info("constituents(%s) failed: %s", name, e)
            if hit.get("rows"):
                return {**hit, "stale": True, "error": str(e)}
            # Only a genuine "not found" is remembered. Being rate-limited says nothing
            # about whether the index has a list, and must not be cached as if it did.
            if "403" not in str(e) and "backing off" not in str(e):
                cache[key] = {"rows": [], "failed_at_epoch": time.time(), "error": str(e)}
                _write_cache(CONSTITUENT_CACHE, cache)
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
