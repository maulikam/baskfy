"""Goldens for lane fno_desk (wave D2): the rows the **real Python monitor and executor** write in
the FO7 replay world (``kite-momentum-rebalancer/tests/test_fno_monitor.World``), so the Rust
desk's DB-backed tests seed exactly what Python raises and compare what they write with what
Python wrote.

    <desk venv>/python tools/parity/fno_desk_goldens.py --db rsd_<name> \\
        [--out rust/crates/baskfy-desk/fixtures/fno/goldens.json]
    python3 tools/parity/fno_desk_goldens.py --emit-cases   # desk_diff's cases, from the goldens

``--db`` must be a bed clone named ``rsd_*`` (never ``baskfy``); every row written hangs off a
throw-away ``app_user`` per scenario, deleted at the end (``ON DELETE CASCADE``). No broker is
reached: the world's ``RecordingKC`` must stay empty, and the script fails if it is not.

Each scenario records, per ``fo_`` table, the user's rows as ``to_jsonb(row)::text`` (numerics
keep their digits) and the world's uid, so a reader can remap ids and the ``-u<uid>`` suffix of
plan ids. Scenarios:

* ``f1_raised`` / ``f2_raised`` — the 09:20 plans (FO7's ``f1_scan``/``f2_scan``), raised, not
  sent; with the quote books they were priced from, as Kite ``quote`` payloads;
* ``f1_open`` / ``f2_open`` — the same, confirmed at 09:25 / 09:22 (the executor's outcome too);
* the closes of ``test_fno_ledger.py`` — ``profit_take``, ``loss_close``, ``hard_exit``,
  ``late_exit``, ``abandoned``, ``roll_time_exit``, ``stop``, ``naked_future`` — each world's
  end state, ``fo_journal`` included (what Python's ``write_journal`` wrote).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DESK = REPO / "kite-momentum-rebalancer"
BED = "postgresql://baskfy:rs-test-only@127.0.0.1:55432/{db}"
TABLES = ("fo_scan", "fo_sleeve_config", "fo_book_config", "fo_plan", "fo_leg", "fo_position",
          "fo_fill", "fo_mark", "fo_journal", "fo_config_audit")  # fmt: skip


def book_payload(q: Any) -> dict[str, Any]:  # noqa: ANN401 - an X.FoQuote
    """An ``X.FoQuote`` as the Kite ``quote`` entry ``fno_monitor.kite_quotes`` reads back."""
    return {
        "last_price": str(q.last) if q.last is not None else None,
        "oi": q.oi,
        "depth": {
            "buy": [{"price": str(lv.price), "quantity": lv.quantity} for lv in q.bids],
            "sell": [{"price": str(lv.price), "quantity": lv.quantity} for lv in q.asks],
        },
    }


ORDER = {"fo_sleeve_config": "sleeve", "fo_book_config": "user_id", "fo_mark": "position_id, trade_date",
         "fo_journal": "position_id"}  # fmt: skip


def dump(w: Any) -> dict[str, list[str]]:  # noqa: ANN401 - a World
    out: dict[str, list[str]] = {}
    for table in TABLES:
        rows = w.conn.execute(
            f"SELECT to_jsonb(t)::text AS j FROM {table} t WHERE user_id = ? "
            f"ORDER BY {ORDER.get(table, 'id')}",
            (w.uid,),
        ).fetchall()
        out[table] = [str(r["j"]) for r in rows]
    return out


# --- desk_diff: the same rows, seeded for the sole user on both desks ----------------------------

SOLE_UID = 1
FIXTURE_OUT = HERE / "desk_fixtures" / "fno_world"
CASES_OUT = HERE / "desk_cases" / "fno_world.json"
PLANS_OUT = HERE / "desk_cases" / "plans_fno.json"
ID_TABLES = ("fo_scan", "fo_plan", "fo_leg", "fo_position", "fo_fill", "fo_config_audit")
SEED_TABLES = ("fo_scan", "fo_position", "fo_plan", "fo_leg", "fo_fill", "fo_mark", "fo_journal")


def remap_uid(text: str, old: int, new: int) -> str:
    return re.sub(rf"-u{old}(?![0-9])", f"-u{new}", text)


def seed_sql(scn: dict[str, Any], base: int, renames: list[tuple[str, str]],
             scans: dict[tuple[Any, ...], int] | None = None) -> list[str]:
    """One scenario's rows as INSERTs for the sole user, ids explicit from ``base`` (far above
    any the bed's sequences reach), foreign keys and ``detail.scan_id`` following, and
    ``renames`` (text, replacement) applied so two scenarios' plan ids and contracts never meet."""
    ids: dict[tuple[str, int], int] = {}
    nxt = base
    out: list[str] = []
    for table in SEED_TABLES:
        for raw in scn["rows"][table]:
            text = remap_uid(raw, scn["uid"], SOLE_UID)
            for a, b in renames:
                text = text.replace(a, b)
            row = json.loads(text)
            row["user_id"] = SOLE_UID
            key = (row.get("sleeve"), row.get("trade_date"), row.get("symbol"))
            if table == "fo_scan" and scans is not None and key in scans:
                ids[(table, row["id"])] = scans[key]
                continue
            if table == "fo_scan" and scans is not None:
                scans[key] = nxt
            if table in ID_TABLES:
                ids[(table, row["id"])] = nxt
                row["id"] = nxt
                nxt += 1
            detail = row.get("detail") if table == "fo_plan" else None
            if isinstance(detail, dict):
                for key, ref in (("scan_id", "fo_scan"), ("position_id", "fo_position")):
                    if isinstance(detail.get(key), int):
                        detail[key] = ids.get((ref, detail[key]), detail[key])
            if table == "fo_leg":
                row["plan_id"] = ids[("fo_plan", row["plan_id"])]
            if table == "fo_fill":
                row["leg_id"] = ids[("fo_leg", row["leg_id"])]
            if table in ("fo_fill", "fo_mark", "fo_journal") and row.get("position_id") is not None:
                row["position_id"] = ids[("fo_position", row["position_id"])]
            lit = json.dumps(row, ensure_ascii=False).replace("'", "''")
            override = " OVERRIDING SYSTEM VALUE" if table in ID_TABLES else ""
            out.append(f"INSERT INTO public.{table}{override} SELECT * FROM "
                       f"jsonb_populate_record(NULL::public.{table}, '{lit}'::jsonb);")  # fmt: skip
    return out


def renamed_books(scn: dict[str, Any], renames: list[tuple[str, str]]) -> dict[str, Any]:
    out = {}
    for sym, q in scn["books"].items():
        for a, b in renames:
            sym = sym.replace(a, b)
        out[f"NFO:{sym}"] = q
    return out


def closed_sql(pid: int, sleeve: str, r: str, inr: str, on: str) -> list[str]:
    """``test_fno_ledger.closed()``: one closed paper position and its journal row."""
    symbol = {"F1N": "NIFTY", "F1B": "BANKNIFTY"}.get(sleeve, "RELIANCE")
    structure = "FUTURE" if sleeve == "F2" else "IRON_CONDOR"
    at_close = f"{on}T15:00:00+05:30"
    return [
        "INSERT INTO public.fo_position (id, user_id, sleeve, symbol, structure, entry_plan_id, "
        "legs, lots, lot_size, max_loss_inr, opened_at, closed_at, closed_reason, simulated) "
        f"OVERRIDING SYSTEM VALUE VALUES ({pid}, {SOLE_UID}, '{sleeve}', '{symbol}', '{structure}', "
        f"'seed-{sleeve}-{r}-{on}', '{{}}', 1, 65, 10000, '{at_close}', '{at_close}', 'LOSS_CLOSE', true);",
        "INSERT INTO public.fo_journal (position_id, user_id, sleeve, symbol, structure, opened_on, "
        "closed_on, entry_inr, exit_inr, gross_pnl_inr, costs_inr, net_pnl_inr, risk_budget_inr, "
        "r_multiple, closed_reason, sessions_held, simulated, sizing_mode) VALUES "
        f"({pid}, {SOLE_UID}, '{sleeve}', '{symbol}', '{structure}', '{on}', '{on}', 0, 0, {inr}, 0, "
        f"{inr}, 10000, {r}, 'LOSS_CLOSE', 1, true, 'BUDGET');",
    ]  # fmt: skip


def emit_cases(goldens: Path) -> None:  # noqa: PLR0915 - one scenario script, in clock order
    """``desk_cases/fno_world.json`` + ``plans_fno.json`` and ``desk_fixtures/fno_world``."""
    sc = json.loads(goldens.read_text())["scenarios"]
    books: dict[str, Any] = {}
    raised = lambda name, renames: remap_uid(  # noqa: E731
        _apply(sc[name]["raised"][0], renames), sc[name]["uid"], SOLE_UID)

    def add_books(name: str, renames: list[tuple[str, str]]) -> None:
        books.update(renamed_books(sc[name], renames))

    def post(name: str, plan: str, clock: str | None = None, **extra: Any) -> dict[str, Any]:  # noqa: ANN401
        c: dict[str, Any] = {"name": f"fno_world: {name}", "method": "POST", "path": "/fno/execute",
                             "form": {"plan_id": plan, "confirm": "true"}, **extra}  # fmt: skip
        if clock:
            c["clock"] = clock
        return c

    def get(name: str, path: str, clock: str | None = None, **extra: Any) -> dict[str, Any]:  # noqa: ANN401
        c: dict[str, Any] = {"name": f"fno_world: {name}", "method": "GET", "path": path, **extra}
        if clock:
            c["clock"] = clock
        return c

    cases: list[dict[str, Any]] = []
    scans: dict[tuple[Any, ...], int] = {}
    # 2 Nov: F2's 09:20 plan, confirmed at 09:22 with its paper GTT.
    add_books("f2_raised", [])
    f2 = raised("f2_raised", [])
    cases += [
        get("/fno/data shows F2's 09:20 plan, live", "/fno/data", "2026-11-02T09:21:00+05:30",
            sql=seed_sql(sc["f2_raised"], 910_000, [], scans)),
        get("/fno shows its card, sentence and button", "/fno"),
        post("F2 confirmed: the future fills and its GTT rests under a paper handle", f2,
             "2026-11-02T09:22:00+05:30"),
        post("F2 posted again: NOT_ISSUED, nothing sent twice", f2),
        get("/fno/data: the open future, its stop and handle", "/fno/data"),
    ]  # fmt: skip
    # 3 Nov: F1's 09:20 plan, confirmed longs first; a thin twin abandons; a third expires.
    x, w = [("NIFTY26NOV", "NIFTX26NOV"), ("-NIFTY-u", "-NIFTX-u")], [
        ("NIFTY26NOV", "NIFTW26NOV"), ("-NIFTY-u", "-NIFTW-u")]
    add_books("f1_raised", [])
    add_books("abandoned", x)
    f1 = raised("f1_raised", [])
    cases += [
        get("/fno at 09:25: three F1 cards with their countdowns", "/fno", "2026-11-03T09:25:00+05:30",
            sql=seed_sql(sc["f1_raised"], 920_000, [], scans) + seed_sql(sc["f1_raised"], 921_000, x, scans)
            + seed_sql(sc["f1_raised"], 922_000, w, scans)),
        post("F1 confirmed: four simulated fills, longs first, OPEN", f1),
        post("F1 posted again: NOT_ISSUED, nothing sent twice", f1),
        post("a thin long call: ABANDONED_PARTIAL, journalled (FO10.4)",
             raised("f1_raised", x), "2026-11-03T09:26:00+05:30"),
        post("09:50, expires_at exactly: PLAN_EXPIRED, nothing sent", raised("f1_raised", w),
             "2026-11-03T09:50:00+05:30"),
        get("/fno at 09:50: no button on an expired plan", "/fno"),
        get("/fno/data at 11:00: the open condor, its marks and next rule", "/fno/data",
            "2026-11-03T11:00:00+05:30"),
    ]  # fmt: skip
    # 6 Nov: F3's entry, wing first.
    f = [("NIFTY26NOV", "NIFTF26NOV")]
    add_books("f3_raised", f)
    cases += [
        post("F3 confirmed: the wing, then the short, OPEN with its level", raised("f3_raised", f),
             "2026-11-06T09:25:00+05:30", sql=seed_sql(sc["f3_raised"], 930_000, f, scans)),
        get("/fno/data: the open spread", "/fno/data"),
    ]  # fmt: skip
    # 9 Nov: an F3 add the monitor raised, confirmed by the click.
    a = [("NIFTY26NOV", "NIFTA26NOV"), ("-NIFTY-u", "-NIFTA-u")]
    add_books("f3_add_issued", a)
    add_at = sc["f3_add_issued"]["at"]
    cases += [
        get("/fno shows the add awaiting its click", "/fno", add_at,
            sql=seed_sql(sc["f3_add_issued"], 940_000, a, scans)),
        post("the add confirmed: two lots, one position", raised("f3_add_issued", a)),
    ]  # fmt: skip
    # 17 Nov: an F3 exit held for the click (M.5, the flag off); clicked; journalled; the
    # paper book's pauses recorded at the close (F2 -6R, F1N's -0.6R run, the book's 75,000).
    e = [("NIFTY26NOV", "NIFTE26NOV"), ("-NIFTY-u", "-NIFTE-u")]
    add_books("f3_exit_issued", e)
    closes: list[str] = []
    for i, (sleeve, r, inr, on) in enumerate([
        ("F2", "-2", "-20000", "2026-11-02"), ("F2", "-2", "-20000", "2026-11-03"),
        ("F2", "-2", "-20000", "2026-11-04"), ("F1N", "-0.60", "-6000", "2026-11-12"),
        ("F1N", "-0.73", "-7300", "2026-11-13"), ("F1N", "-0.60", "-6000", "2026-11-16"),
    ]):  # fmt: skip
        closes += closed_sql(990_001 + i, sleeve, r, inr, on)
    exit_id = raised("f3_exit_issued", e)
    exit_at = sc["f3_exit_issued"]["at"]
    cases += [
        get("/fno at 11:02: the exit held for the click, its sentence", "/fno",
            "2026-11-17T11:02:00+05:30", sql=seed_sql(sc["f3_exit_issued"], 950_000, e, scans) + closes),
        post("the click on the held exit: CLOSED short first; journal and the paper pauses", exit_id,
             exit_at),
        post("the exit clicked again: NOT_ISSUED", exit_id),
        get("/fno/data after the click: actions, violations, the badge", "/fno/data",
            "2026-11-17T11:04:00+05:30"),
        get("/fno after the click: the nav badge read again", "/fno", "2026-11-17T11:05:00+05:30"),
    ]  # fmt: skip
    masks = {"public.risk_ledger": ["updated_at"], "public.fo_sleeve_config": ["updated_at"],
             "public.fo_book_config": ["updated_at"], "public.fo_config_audit": ["changed_at"]}  # fmt: skip
    note = ("Lane fno_desk (wave D2): the ISSUED path through POST /fno/execute on both desks, over "
            "rows the real Python monitor raised (tools/parity/fno_desk_goldens.py, FO7's world) "
            "seeded for the sole user with explicit ids from 910000 and, per scenario, renamed "
            "contracts/plan ids so books never meet. Run with --fixture "
            "tools/parity/desk_fixtures/fno_world (the default fixture plus these NFO books); under "
            "the default fixture every confirm refuses NO_QUOTE on both sides. Graded: outcomes, "
            "pages, every fo_ row, the FO order journal under data/, and the replay Kite's order/GTT "
            "log, which must stay EMPTY (FO7.1: paper-pinned).")  # fmt: skip
    CASES_OUT.write_text(json.dumps({"note": note, "db_mask": masks, "cases": cases}, indent=1,
                                    ensure_ascii=False) + "\n")  # fmt: skip
    # --plans: the "fno exits" kind — the exit the monitor raised, as the page and its JSON show it,
    # byte for byte (the desk builds no plan of its own; the monitor raises, the desk shows).
    p = [("NIFTY26NOV", "NIFTP26NOV"), ("-NIFTY-u", "-NIFTP-u")]
    plans = [
        get("plans fno exits: the held F3 exit on /fno/data", "/fno/data", "2026-11-17T11:02:00+05:30",
            sql=seed_sql(sc["f3_exit_issued"], 960_000, p), plan=True),
        get("plans fno exits: the held F3 exit on /fno", "/fno", plan=True),
    ]  # fmt: skip
    for c in plans:
        c["name"] = c["name"].replace("fno_world: ", "")
    PLANS_OUT.write_text(json.dumps({"note": "Lane fno_desk: the 'fno exits' plan kind — the exit "
                                     "plan the Python monitor raised (fno_desk_goldens.py), seeded, "
                                     "shown by both desks, graded byte for byte (plan sha256).",
                                     "db_mask": masks, "cases": plans}, indent=1,
                                    ensure_ascii=False) + "\n")  # fmt: skip
    default = json.loads((HERE / "desk_fixtures" / "default" / "kite.json").read_text())
    default["quotes"].update(books)
    FIXTURE_OUT.mkdir(parents=True, exist_ok=True)
    (FIXTURE_OUT / "kite.json").write_text(json.dumps(default, indent=1) + "\n")
    inst = FIXTURE_OUT / "instruments"
    inst.mkdir(exist_ok=True)
    for f in (HERE / "desk_fixtures" / "default" / "instruments").iterdir():
        (inst / f.name).write_bytes(f.read_bytes())
    print(f"wrote {CASES_OUT.name} ({len(cases)} cases), {PLANS_OUT.name}, {FIXTURE_OUT}")


def _apply(text: str, renames: list[tuple[str, str]]) -> str:
    for a, b in renames:
        text = text.replace(a, b)
    return text


def main() -> int:  # noqa: C901, PLR0915 - one scenario after another, in order
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db")
    parser.add_argument("--out", default=str(REPO / "rust/crates/baskfy-desk/fixtures/fno/goldens.json"))
    parser.add_argument("--emit-cases", action="store_true",
                        help="build desk_cases/fno_world.json, plans_fno.json and "
                             "desk_fixtures/fno_world from --out (no database)")  # fmt: skip
    args = parser.parse_args()
    if args.emit_cases:
        emit_cases(Path(args.out))
        return 0
    if args.db is None:
        raise SystemExit("--db is required to generate the goldens")
    if not re.fullmatch(r"rsd_[a-z0-9_]{1,50}", args.db):
        raise SystemExit("--db must be a bed clone named rsd_*")
    sys.path.insert(0, str(DESK))
    from app import fno_desk  # noqa: PLC0415
    from app import fno_execute as X  # noqa: PLC0415
    from app.analytics.pg import Connection  # noqa: PLC0415
    from baskfy_core.fno.config import FoSleeve  # noqa: PLC0415
    from baskfy_core.options.chain import Level  # noqa: PLC0415
    from tests import test_fno_monitor as fo7  # noqa: PLC0415

    conn = Connection(BED.format(db=args.db))
    scenarios: dict[str, Any] = {}
    tmp = Path(tempfile.mkdtemp(prefix="fno_goldens_"))

    def world() -> Any:  # noqa: ANN401
        tag = uuid.uuid4().hex[:8]
        uid = int(conn.execute(
            "INSERT INTO app_user (public_id, email) VALUES (?, ?) RETURNING id",
            (f"fo7-{tag}", f"fo7-{tag}@x.test"),
        ).fetchone()["id"])  # fmt: skip
        conn.execute(
            "INSERT INTO fo_sleeve_config (user_id, sleeve, capital_inr) VALUES (?, 'F1', 2500000), "
            "(?, 'F2', 0)", (uid, uid),
        )  # fmt: skip
        (tmp / tag).mkdir()
        return fo7.World(conn, uid, tmp / tag)

    def record(name: str, w: Any, *, keep: bool = False, **extra: Any) -> None:  # noqa: ANN401
        if w.kc.calls:
            raise SystemExit(f"{name}: the broker was reached: {w.kc.calls}")
        scenarios[name] = {"uid": w.uid, "rows": dump(w), "alerts": list(w.alerts),
                           "books": {s: book_payload(q) for s, q in sorted(w.quotes.books.items())},
                           **extra}  # fmt: skip
        if not keep:
            conn.execute("DELETE FROM app_user WHERE id = ?", (w.uid,))

    def outcome(o: Any) -> Any:  # noqa: ANN401
        return json.loads(json.dumps(fno_desk.outcome_json(o), default=str))

    at, before = fo7.at, fo7.before
    F1_ENTRY, F2_ENTRY, HARD_EXIT, NOV = fo7.F1_ENTRY, fo7.F2_ENTRY, fo7.HARD_EXIT, fo7.NOV  # noqa: N806

    # --- the raised plans, and their confirms -------------------------------------------------
    w = world()
    fo7.f1_scan(w)
    w.quotes.books.update(fo7.NEUTRAL)
    raised = w.tick(at(F1_ENTRY, 9, 20)).raised
    record("f1_raised", w, raised=raised)

    w = world()
    fo7.f2_scan(w)
    w.quotes.books[fo7.FUT_NOV] = fo7.book("1399.90", "1400.00", last="1400.00")
    raised = w.tick(at(F2_ENTRY, 9, 20)).raised
    record("f2_raised", w, raised=raised)

    w = world()
    fo7.f1_scan(w)
    w.quotes.books.update(fo7.NEUTRAL)
    plan_id = w.tick(at(F1_ENTRY, 9, 20)).raised[0]
    record("f1_open", w, raised=[plan_id], outcome=outcome(w.confirm(plan_id, at(F1_ENTRY, 9, 25))))

    w = world()
    fo7.f2_scan(w)
    w.quotes.books[fo7.FUT_NOV] = fo7.book("1399.90", "1400.00", last="1400.00")
    plan_id = w.tick(at(F2_ENTRY, 9, 20)).raised[0]
    record("f2_open", w, raised=[plan_id], outcome=outcome(w.confirm(plan_id, at(F2_ENTRY, 9, 22))))

    # --- test_fno_ledger.py's closes ----------------------------------------------------------
    w = world()
    _p, _pid = fo7.open_f1(w)
    w.quotes.books.update(fo7.condor_book("20", "18", "3", "2"))
    w.tick(at(before(HARD_EXIT, 5), 11, 0))
    record("profit_take", w)

    w = world()
    fo7.open_f1(w)
    w.quotes.books.update(fo7.condor_book("250", "20", "30", "5"))
    w.tick(at(before(HARD_EXIT, 8), 13, 1))
    record("loss_close", w)

    w = world()
    fo7.open_f1(w)
    w.tick(at(HARD_EXIT, 15, 0))
    record("hard_exit", w)

    w = world()
    fo7.open_f1(w)
    w.tick(at(NOV, 9, 15))
    record("late_exit", w)

    w = world()
    fo7.f1_scan(w)
    w.quotes.books.update(fo7.condor_book("60.5", "55.5", "12.25", "10.25"))
    plan_id = w.tick(at(F1_ENTRY, 9, 20)).raised[0]
    thin = w.quotes.books[fo7.opt_symbol("LONG_CALL")]
    w.quotes.books[fo7.opt_symbol("LONG_CALL")] = X.FoQuote(
        thin.bid, thin.ask, thin.bids, (Level(thin.ask or Decimal(0), 20),), thin.last, thin.oi)
    o = w.confirm(plan_id, at(F1_ENTRY, 9, 21))
    record("abandoned", w, raised=[plan_id], outcome=outcome(o), at=at(F1_ENTRY, 9, 21).isoformat())

    w = world()
    _p, pid = fo7.open_f2(w)
    w.quotes.books[fo7.FUT_NOV] = fo7.book("1430.00", "1430.10", last="1430.05")
    w.quotes.books[fo7.FUT_DEC] = fo7.book("1438.00", "1438.10", last="1438.05")
    w.tick(at(HARD_EXIT, 15, 0))
    w.tick(at(fo7.SESSIONS[fo7.SESSIONS.index(F2_ENTRY) + 39], 15, 0))
    record("roll_time_exit", w)

    w = world()
    fo7.open_f2(w)
    w.quotes.books[fo7.FUT_NOV] = fo7.book("1338", "1338.10", last="1339.95")
    w.tick(at(before(HARD_EXIT, 10), 10, 1))
    record("stop", w)

    w = world()
    fo7.f2_scan(w)
    w.quotes.books[fo7.FUT_NOV] = fo7.book("1399.90", "1400.00", last="1400.00")
    plan_id = w.tick(at(F2_ENTRY, 9, 20)).raised[0]
    gw = w.gateway_for(FoSleeve.F2)

    async def refuse(**_: object) -> dict[str, object]:
        return {"status": "GTT_ERROR", "error": "fixture: refused"}

    gw.place_gtt_stop = refuse
    w.confirm(plan_id, at(F2_ENTRY, 9, 22))
    w.tick(at(F2_ENTRY, 9, 23))
    record("naked_future", w)

    # --- F3 (test_fno_f3.py's world): the entry, the exit held for the click, the add ---------
    from tests import test_fno_f3 as f3  # noqa: PLC0415

    def f3_world() -> Any:  # noqa: ANN401
        base = world()
        return f3.F3World(conn, base.uid, base.tmp)

    w = f3_world()
    f3.f3_scan(w)
    w.quotes.books.update(f3.spread_book("30", "6"))
    plan_id = w.tick(at(f3.ENTRY, 9, 20)).raised[0]
    record("f3_raised", w, raised=[plan_id], keep=True)
    record("f3_open", w, raised=[plan_id], outcome=outcome(w.confirm(plan_id, at(f3.ENTRY, 9, 25))))

    w = f3_world()
    plan_id, _pid = f3.open_f3(w)
    exit_day = before(NOV, 5)
    w.index = f3.M.IndexQuote(Decimal("23970"), Decimal("24200"))
    w.tick(at(exit_day, 11, 1))
    record("f3_exit_issued", w, raised=[f"{plan_id}-X"], keep=True,
           at=at(exit_day, 11, 3).isoformat())  # fmt: skip
    record("f3_exit_clicked", w, raised=[f"{plan_id}-X"],
           outcome=outcome(w.click_exit(f"{plan_id}-X", at(exit_day, 11, 3))))  # fmt: skip

    w = f3_world()
    plan_id, _pid = f3.open_f3(w)
    nxt = before(NOV, 11)
    f3.f3_scan(w, state="OPEN_POSITION", trade_date=f3.ENTRY)
    w.quotes.books.update(f3.spread_book("20", "3"))
    w.tick(at(f3.ENTRY, 9, 30))
    add_id = w.tick(at(nxt, 9, 20)).raised[0]
    record("f3_add_issued", w, raised=[add_id], keep=True, at=at(nxt, 9, 22).isoformat())
    record("f3_add_confirmed", w, raised=[add_id], outcome=outcome(w.confirm(add_id, at(nxt, 9, 22))))

    conn.close()
    doc = {
        "note": "Generated by tools/parity/fno_desk_goldens.py from the FO7 replay world "
                "(tests/test_fno_monitor.World): the real Python monitor, executor and ledger "
                "over a bed clone. Rows are to_jsonb(row)::text per fo_ table; ids and the "
                "plan ids' -u<uid> suffix are the generating run's. Never edit by hand.",
        "scenarios": scenarios,
    }  # fmt: skip
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {out}: {', '.join(scenarios)}")
    _ = asyncio  # the world drives its own loop
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
