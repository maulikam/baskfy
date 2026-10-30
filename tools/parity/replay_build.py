"""Build a replay bundle for one trading day from the bed (DECISIONS-RS R7.3).

    cd /Volumes/T7-Work/Dev/projects/baskfy/decile-blueprint
    PYTHONPATH=..:packages/core/src:packages/providers/src:services/api/src:services/worker/src \\
        .venv/bin/python ../tools/parity/replay_build.py --date 2026-10-29

writes ``rust/target/replay/<date>/<variant>/`` for every variant (``base``, ``gate-refuse``,
``provider-failure-midway``) and prints each bundle's hash.

What the providers *would have returned* that day, reconstructed from the bed's own tables — read
from the ``baskfy`` database in ``baskfy-rs-pg`` inside a read-only transaction, never written:

=====================  ===================================================================
call                   reconstructed from
=====================  ===================================================================
list_instruments       ``instrument`` rows with a ``kite_token`` not delisted by the date, as
                       Kite's dump carries them (no series except Emerge's SM/ST, no ISIN)
listings               ``instrument`` EQ rows with a main-board series, not delisted
sme_listings           ``instrument`` rows with series SM/ST, not delisted
daily_bars             every Kite token's raw bar (one per token: a token shared by a
                       legacy ``X-SM`` row and its bare Emerge row is the bare row's) for the date (``open_raw`` .. ``close_raw``,
                       ``volume_raw``; ``open``/``high``/``low`` when the raw column is null);
                       a token with no bar that day answers an empty frame, as Kite does
bhavcopy               every ``ohlcv_daily`` row of the date with its raw prices, the previous
                       bar's ``close_raw`` as ``prev_close``, turnover and circuit bands
                       (``trades`` is not stored: null)
corporate_actions      ``corporate_action`` rows with ``ex_date >= date - 7`` (the chain's since)
index_constituents     ``index_member_daily`` rows of the date with ``source = 'nse_file'``
etf_symbols            the ``etf`` universe's members that day
fno_underlyings        the ``nifty-fno`` universe's members that day
index_snapshots        ``index_snapshot_daily`` rows of the date
equity_fundamentals    each instrument's latest ``fundamental_daily`` row in the 14 days up to
                       the date, stamped with the date, ``last_price`` = that day's ``close_raw``
                       (the real 2026-10-29 fetch failed upstream; this exercises the path)
=====================  ===================================================================

Fidelity to the real day is a nice-to-have; **identical inputs to both sides** is the requirement,
and every bundle is deterministic: rebuilding from an unchanged bed gives the same hash.

Variant ``session`` (wave 6, R8.3) is ``base`` plus an ``upstream/`` directory for the tasks that
build their own Kite and NSE clients: real F&O raw files from the AWS-exit archive, a rebuilt NFO
dump, synthesized session quotes, a synthetic read-only book (``replay_build_session.py`` says what
each is and how faithful). ``replay_upstream.py`` serves it to both sides; ``base`` is unchanged.

Variants: ``gate-refuse`` keeps the bars (Kite and bhavcopy) of two symbols in five, so the
quality gate's bar count fails on a fresh day; ``provider-failure-midway`` refuses
``index_snapshots(on=<date>)`` with ``ProviderUnavailable``, failing step 6 after five steps have
written.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from baskfy_providers.records import (
    CorporateAction,
    EquityFundamental,
    IndexSnapshot,
    InstrumentRecord,
    ListingRecord,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "rust" / "target" / "replay"
CONTAINER = "baskfy-rs-pg"
BED_DB = "baskfy"
CORPORATE_ACTION_LOOKBACK_DAYS = 7
SME_SERIES = ("SM", "ST")
VARIANTS = ("base", "gate-refuse", "provider-failure-midway", "index-edges", "fundamentals-refused", "session")


def bed_rows(sql: str) -> list[dict[str, Any]]:
    """Rows of ``sql`` from the bed's ``baskfy`` database, in a read-only transaction."""
    out = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", "-U", "baskfy", "-d", BED_DB, "-At", "-q",
         "-v", "ON_ERROR_STOP=1"],
        input=(
            "BEGIN TRANSACTION READ ONLY;\n"
            f"SELECT coalesce(json_agg(q), '[]'::json) FROM ({sql}) q;\n"
            "ROLLBACK;\n"
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise SystemExit(f"bed query failed: {out.stderr.strip()}")
    return json.loads(out.stdout, parse_float=Decimal)


def text(value: Decimal | int | None) -> str | None:
    return None if value is None else str(value)


def dump(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def records(models: list[Any]) -> list[Any]:
    return [m.model_dump(mode="json") for m in models]


def build_base(day: dt.date) -> dict[str, Any]:
    """Every call's answer, keyed by the bundle path it is written to."""
    d = day.isoformat()
    since = (day - dt.timedelta(days=CORPORATE_ACTION_LOOKBACK_DAYS)).isoformat()
    files: dict[str, Any] = {}

    inst = bed_rows(
        "SELECT i.symbol, i.name, i.instrument_type, i.series, i.isin, e.code AS exchange, "
        "i.kite_token, i.lot_size, i.face_value, i.listed_on, i.delisted_on "
        "FROM instrument i JOIN exchange e ON e.id = i.exchange_id "
        f"WHERE i.delisted_on IS NULL OR i.delisted_on > DATE '{d}' ORDER BY i.symbol, i.id"
    )
    # Kite's dump carries one row per token. 462 tokens are shared on the bed by a legacy
    # ``X-SM``/``X-SZ`` row and the bare Emerge row the register now uses; Kite's own row is the
    # bare symbol (``_split_sme_symbol``), so each token keeps the instrument without a suffix
    # (then the lowest id) — for its record and for its one candle.
    owner: dict[int, str] = {}
    for r in sorted((r for r in inst if r["kite_token"] is not None),
                    key=lambda r: ("-" in r["symbol"], r["symbol"])):
        owner.setdefault(r["kite_token"], r["symbol"])
    files["list_instruments.json"] = records(
        [
            InstrumentRecord(
                symbol=r["symbol"],
                name=r["name"] or r["symbol"],
                instrument_type=r["instrument_type"],
                series=r["series"] if r["series"] in SME_SERIES else None,
                kite_token=r["kite_token"],
                lot_size=r["lot_size"],
                exchange=r["exchange"],
            )
            for r in inst
            if r["kite_token"] is not None and owner[r["kite_token"]] == r["symbol"]
        ]
    )

    def listing(r: dict[str, Any]) -> ListingRecord:
        return ListingRecord(
            symbol=r["symbol"],
            name=r["name"] or r["symbol"],
            series=r["series"],
            isin=r["isin"],
            listed_on=r["listed_on"],
            face_value=r["face_value"],
        )

    files["listings.json"] = records(
        [listing(r) for r in inst
         if r["instrument_type"] == "EQ" and r["series"] and r["series"] not in SME_SERIES]
    )
    files["sme_listings.json"] = records([listing(r) for r in inst if r["series"] in SME_SERIES])

    bars = bed_rows(
        "SELECT i.kite_token, i.symbol, o.date, coalesce(o.open_raw, o.open) AS open, "
        "coalesce(o.high_raw, o.high) AS high, coalesce(o.low_raw, o.low) AS low, "
        "o.close_raw AS close, o.volume_raw AS volume "
        "FROM instrument i LEFT JOIN ohlcv_daily o ON o.instrument_id = i.id "
        f"AND o.date = DATE '{d}' WHERE i.kite_token IS NOT NULL ORDER BY i.kite_token, i.id"
    )
    by_token: dict[str, list[dict[str, Any]]] = {}
    for r in bars:
        rows = by_token.setdefault(str(r["kite_token"]), [])
        if r["date"] is not None and owner.get(r["kite_token"]) == r["symbol"]:
            rows.append(
                {
                    "symbol": r["symbol"],
                    "date": r["date"],
                    "open": text(r["open"]),
                    "high": text(r["high"]),
                    "low": text(r["low"]),
                    "close": text(r["close"]),
                    "volume": r["volume"],
                }
            )
    files["daily_bars.json"] = {"start": d, "end": d, "by_token": by_token}

    bhav = bed_rows(
        "SELECT i.symbol, i.series, o.date, coalesce(o.open_raw, o.open) AS open, "
        "coalesce(o.high_raw, o.high) AS high, coalesce(o.low_raw, o.low) AS low, "
        "o.close_raw AS close, prev.close_raw AS prev_close, o.volume_raw AS volume, o.turnover, "
        "NULL::bigint AS trades, o.upper_circuit, o.lower_circuit "
        "FROM ohlcv_daily o JOIN instrument i ON i.id = o.instrument_id "
        "LEFT JOIN LATERAL (SELECT p.close_raw FROM ohlcv_daily p WHERE p.instrument_id = "
        f"o.instrument_id AND p.date < DATE '{d}' ORDER BY p.date DESC LIMIT 1) prev ON true "
        f"WHERE o.date = DATE '{d}' ORDER BY i.symbol, i.id"
    )
    files[f"bhavcopy/{d}.json"] = [
        {k: (text(v) if isinstance(v, Decimal) else v) for k, v in r.items()} for r in bhav
    ]

    actions = bed_rows(
        "SELECT i.symbol, ca.action_type, ca.ex_date, ca.ratio_from, ca.ratio_to, ca.amount, "
        "ca.raw::text AS raw_text FROM corporate_action ca JOIN instrument i ON i.id = "
        f"ca.instrument_id WHERE ca.ex_date >= DATE '{since}' ORDER BY ca.ex_date, i.symbol, ca.id"
    )
    files[f"corporate_actions/{since}.json"] = records(
        [
            CorporateAction(
                symbol=r["symbol"],
                action_type=r["action_type"],
                ex_date=r["ex_date"],
                ratio_from=r["ratio_from"],
                ratio_to=r["ratio_to"],
                amount=r["amount"],
                raw=json.loads(r["raw_text"]),
            )
            for r in actions
        ]
    )

    members = bed_rows(
        "SELECT d.slug, i.symbol, m.source FROM index_member_daily m "
        "JOIN index_def d ON d.id = m.index_id JOIN instrument i ON i.id = m.instrument_id "
        f"WHERE m.date = DATE '{d}' ORDER BY d.slug, i.symbol"
    )
    by_slug: dict[str, list[str]] = {}
    for r in members:
        if r["source"] == "nse_file":
            by_slug.setdefault(r["slug"], []).append(r["symbol"])
    for slug, symbols in by_slug.items():
        files[f"index_constituents/{slug}/{d}.json"] = symbols
    files["etf_symbols.json"] = [r["symbol"] for r in members if r["slug"] == "etf"]
    files["fno_underlyings.json"] = [r["symbol"] for r in members if r["slug"] == "nifty-fno"]

    snaps = bed_rows(
        "SELECT d.slug, s.date, s.level, s.change_abs, s.change_pct, s.pe, s.pb, s.div_yield "
        "FROM index_snapshot_daily s JOIN index_def d ON d.id = s.index_id "
        f"WHERE s.date = DATE '{d}' ORDER BY d.sort_order, d.slug"
    )
    files[f"index_snapshots/{d}.json"] = records(
        [
            IndexSnapshot(
                index_slug=r["slug"],
                date=r["date"],
                level=r["level"],
                change_abs=r["change_abs"],
                change_pct=r["change_pct"],
                pe=r["pe"],
                pb=r["pb"],
                div_yield=r["div_yield"],
            )
            for r in snaps
        ]
    )

    funds = bed_rows(
        "SELECT * FROM (SELECT DISTINCT ON (f.instrument_id) i.symbol, f.shares_outstanding, "
        "f.marketcap_cr, f.pe, f.pb, f.div_yield, o.close_raw AS last_price "
        "FROM fundamental_daily f JOIN instrument i ON i.id = f.instrument_id "
        "LEFT JOIN ohlcv_daily o ON o.instrument_id = f.instrument_id "
        f"AND o.date = DATE '{d}' WHERE f.date <= DATE '{d}' AND f.date > DATE '{d}' - 14 "
        "ORDER BY f.instrument_id, f.date DESC) x ORDER BY symbol"
    )
    files[f"equity_fundamentals/{d}.json"] = records(
        [
            EquityFundamental(
                symbol=r["symbol"],
                date=day,
                shares_outstanding=r["shares_outstanding"],
                last_price=r["last_price"],
                marketcap_cr=r["marketcap_cr"],
                pe=r["pe"],
                pb=r["pb"],
                div_yield=r["div_yield"],
            )
            for r in funds
        ]
    )
    return files


def variant_files(base: dict[str, Any], variant: str, day: dt.date) -> tuple[dict[str, Any], list[Any]]:
    files = json.loads(json.dumps(base))  # deep copy
    refuse: list[Any] = []
    d = day.isoformat()
    if variant == "gate-refuse":
        bhav = files[f"bhavcopy/{d}.json"]
        keep = {r["symbol"] for i, r in enumerate(sorted(bhav, key=lambda r: r["symbol"])) if i % 5 < 2}
        files[f"bhavcopy/{d}.json"] = [r for r in bhav if r["symbol"] in keep]
        for token, rows in files["daily_bars.json"]["by_token"].items():
            files["daily_bars.json"]["by_token"][token] = [r for r in rows if r["symbol"] in keep]
    elif variant == "provider-failure-midway":
        refuse.append(
            {
                "method": "index_snapshots",
                "args": {"on": d},
                "kind": "ProviderUnavailable",
                "message": "replay refusal: NSE's index snapshot is down for this scenario",
            }
        )
    elif variant == "index-edges":
        index_edges(files, refuse, d)
    elif variant == "fundamentals-refused":
        refuse.append(
            {
                "method": "equity_fundamentals",
                "args": {"on": d},
                "kind": "UpstreamUnavailable",
                "message": "replay refusal: NSE's quote endpoint timed out for this scenario",
            }
        )
    elif variant not in ("base", "session"):  # `session` adds `upstream/` after the calls
        raise SystemExit(f"unknown variant {variant!r}")
    return files, refuse


def index_edges(files: dict[str, Any], refuse: list[Any], d: str) -> None:
    """Steps 5 and 6's thin branches on one day (the nightly-index review, RS5).

    Membership: ``nifty-next-50`` has no recorded file (ProviderUnavailable, routed into
    CapabilityNotAvailable: a recorded failure); ``nifty-100`` answers UnexpectedPayload (empty,
    so reconstruction runs and finds no later file); ``nifty-200`` is UpstreamUnavailable (a
    transient failure, recorded); ``nifty-500`` changes constituents (its first symbol dropped,
    an unlisted symbol added and skipped); ``etf`` and ``nifty-fno`` refuse (empty, not failed).
    Snapshots: two levels move past SW16's 40 % (one down, one up, refused and named), a
    published name we have never seen (registered as a dashboard index, its name stripped of
    Python's whitespace set), and a name with no slug (unnamed). Fundamentals: the quote's last
    price differs from the day's print, so the P/E is re-priced and rounded; one record has no
    shares (market cap falls back to the quote's), one a zero P/E, one no last price.
    """
    files.pop(f"index_constituents/nifty-next-50/{d}.json", None)
    refuse.append({"method": "index_constituents", "args": {"index_slug": "nifty-100", "on": d},
                   "kind": "UnexpectedPayload", "message": "replay refusal: no file for nifty-100"})
    refuse.append({"method": "index_constituents", "args": {"index_slug": "nifty-200", "on": d},
                   "kind": "UpstreamUnavailable", "message": "replay refusal: nifty-200 timed out"})
    n500 = files[f"index_constituents/nifty-500/{d}.json"]
    files[f"index_constituents/nifty-500/{d}.json"] = [*n500[1:], "ZZRSNOTLISTED"]
    refuse.append({"method": "etf_symbols", "args": {}, "kind": "ProviderUnavailable",
                   "message": "replay refusal: the ETF list is down"})
    refuse.append({"method": "fno_underlyings", "args": {}, "kind": "ProviderError",
                   "message": "replay refusal: the instrument dump is down"})

    snaps = files[f"index_snapshots/{d}.json"]
    for row in snaps:
        if row["index_slug"] == "nifty-50":
            row["level"] = str((Decimal(row["level"]) / 2).quantize(Decimal("0.00001")))
        elif row["index_slug"] == "nifty-500":
            row["level"] = str(Decimal(row["level"]) * Decimal("1.4125"))
    template = dict(snaps[0])
    snaps.append({**template, "index_slug": "\u001c NIFTY RS Review Index (TRI)  ", "level": "1234.5600",
                  "pe": None, "pb": None, "div_yield": None})
    snaps.append({**template, "index_slug": "  ***  ", "level": "99.0000"})

    funds = files[f"equity_fundamentals/{d}.json"]
    for i, row in enumerate(funds):
        if row.get("last_price") is not None:
            row["last_price"] = str((Decimal(row["last_price"]) * Decimal("1.0737")).quantize(Decimal("0.01")))
        if i == 0:
            row["shares_outstanding"] = None
        elif i == 1:
            row["pe"] = "0"
        elif i == 2:
            row["last_price"] = None


def bundle_hash(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(directory)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def write_bundle(out: Path, day: dt.date, variant: str, files: dict[str, Any], refuse: list[Any]) -> str:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for relative, obj in files.items():
        dump(out / "calls" / relative, obj)
    dump(
        out / "manifest.json",
        {
            "format": 1,
            "trade_date": day.isoformat(),
            "variant": variant,
            "built_from": f"the bed ({CONTAINER}, database {BED_DB}), read-only",
            "refuse": refuse,
        },
    )
    return bundle_hash(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--variant", choices=(*VARIANTS, "all"), default="all")
    args = parser.parse_args(argv)
    day = dt.date.fromisoformat(args.date)
    base = build_base(day)
    variants = VARIANTS if args.variant == "all" else (args.variant,)
    for variant in variants:
        files, refuse = variant_files(base, variant, day)
        out = args.out / day.isoformat() / variant
        digest = write_bundle(out, day, variant, files, refuse)
        if variant == "session":
            # Wave 6 (R8.3): the base calls plus `upstream/` — recorded raw NSE files and Kite REST
            # answers the tasks' own clients read (`replay_build_session.py`, `replay_upstream.py`).
            from replay_build_session import write_upstream  # noqa: PLC0415

            write_upstream(out, day, bed_rows)
            digest = bundle_hash(out)
        print(f"{variant}: {args.out / day.isoformat() / variant} sha256={digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
