"""PM8: the paper book's I/O. Writes only mb_paper_*; no broker call of any kind. Rules: portfolioml/paper.py.

Every night (after the score): record the month's live tranche if it is missing (scoring its first session first
if that night was missed), add the trigger lines that fired, then recompute every mark from the corrected bars.
Tranches and lines are the record and are never rewritten; marks are derived and replaced whole."""

from __future__ import annotations

import datetime as dt
import json

import polars as pl

from portfolioml import paper as PP

LIVE, BACKTEST = "live", "backtest"


def _state(cur, key: str) -> str | None:
    cur.execute("SELECT value FROM mb_paper_state WHERE key = %s", (key,))
    row = cur.fetchone()
    return None if row is None else row[0]


def _month(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def grid_of(bars: pl.DataFrame, terminal: pl.DataFrame) -> PP.Grid:
    off = set(terminal.filter(pl.col("status").is_in(["delisted", "suspended"]))["instrument_id"].to_list())
    return PP.Grid(bars.select("instrument_id", "date", "open", "high", "close"), {int(i) for i in off})


def _investable(cur, d: dt.date) -> list[int]:
    cur.execute("SELECT instrument_id FROM mb_score_daily WHERE date = %s AND investable AND scope = 'universe' ORDER BY rank", (d,))
    return [r[0] for r in cur.fetchall()]


def record_live(conn, grid: PP.Grid, date: dt.date, model, rescore) -> list[dt.date]:
    """The live tranches due by ``date`` and missing, each from its own first session's scores."""
    made = []
    with conn.cursor() as cur:
        live_from = _state(cur, "live_from_month")
        if live_from is None:
            starts_now = PP.month_starts(grid.sessions)
            live_from = _month(date) if date in starts_now else _month(date.replace(day=28) + dt.timedelta(days=4))
            cur.execute("INSERT INTO mb_paper_state VALUES ('live_from_month', %s) ON CONFLICT DO NOTHING", (live_from,))
        for f in PP.month_starts(grid.sessions):
            if _month(f) < live_from or f > date:
                continue
            cur.execute("SELECT 1 FROM mb_paper_tranche WHERE source = %s AND signal_date = %s", (LIVE, f))
            if cur.fetchone():
                continue
            ranked = _investable(cur, f)
            if not ranked:
                rescore(f)  # the night of f was missed: score it now, from bars that end on f
                ranked = _investable(cur, f)
            if not ranked:
                continue
            cur.execute("SELECT t.id FROM mb_paper_tranche t WHERE t.source = %s AND t.signal_date < %s ORDER BY t.signal_date DESC LIMIT %s",
                        (LIVE, f, PP.SLICES))
            ids = [r[0] for r in cur.fetchall()]
            recent = []
            for tid in reversed(ids):
                cur.execute("SELECT instrument_id FROM mb_paper_line WHERE tranche_id = %s AND line = 'book'", (tid,))
                recent.append([r[0] for r in cur.fetchall()])
            book = PP.book_picks(ranked, recent)
            watch = [[i, k + 1] for k, i in enumerate(ranked[:PP.WATCH])]
            pos = {i: k + 1 for k, i in enumerate(ranked)}
            cur.execute("INSERT INTO mb_paper_tranche (source, signal_date, model_id, watch) VALUES (%s, %s, %s, %s) RETURNING id",
                        (LIVE, f, model[0], json.dumps(watch)))
            tid = cur.fetchone()[0]
            cur.executemany("INSERT INTO mb_paper_line (tranche_id, line, instrument_id, rank, signal_date) VALUES (%s, 'book', %s, %s, %s)",
                            [(tid, i, pos[i], f) for i in book])
            made.append(f)
    return made


def record_triggers(conn, grid: PP.Grid, source: str, through: int) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id, signal_date, watch FROM mb_paper_tranche WHERE source = %s", (source,))
        tranches = {grid.sidx[d]: (tid, w) for tid, d, w in cur.fetchall() if d in grid.sidx}
        cur.execute("SELECT t.signal_date, l.instrument_id, l.signal_date, l.rank FROM mb_paper_line l JOIN mb_paper_tranche t ON t.id = l.tranche_id "
                    "WHERE t.source = %s AND l.line = 'hi63'", (source,))
        already = [PP.Fired(grid.sidx[s], i, grid.sidx[e], r) for s, i, e, r in cur.fetchall() if s in grid.sidx and e in grid.sidx]
        new = PP.triggers({s: [tuple(x) for x in w] for s, (_, w) in tranches.items()}, lambda i, t: (i, t) in grid.fired, through, already)
        cur.executemany("INSERT INTO mb_paper_line (tranche_id, line, instrument_id, rank, signal_date) VALUES (%s, 'hi63', %s, %s, %s) ON CONFLICT DO NOTHING",
                        [(tranches[f.sig][0], f.iid, f.rank, grid.sessions[f.e]) for f in new])
    return len(new)


def refresh(conn, grid: PP.Grid, idx: pl.DataFrame, source: str) -> dict:
    """Recompute this source's marks and line state from the bars. Idempotent: the same bars give the same rows."""
    with conn.cursor() as cur:
        cur.execute("SELECT l.tranche_id, l.line, l.instrument_id, l.signal_date, t.signal_date FROM mb_paper_line l "
                    "JOIN mb_paper_tranche t ON t.id = l.tranche_id WHERE t.source = %s", (source,))
        rows = cur.fetchall()
        cur.execute("DELETE FROM mb_paper_mark WHERE source = %s", (source,))
        if not rows:
            return {"lines": 0}
        firsts = [grid.sidx[t] for *_, t in rows if t in grid.sidx]
        n = len(grid.sessions)
        first = min(firsts) + 1
        books = {"book": [], "hi63_20": [], "hi63_16": []}
        updates = []
        for tid, line, iid, sig_date, _ in rows:
            got = grid.path(iid, grid.sidx[sig_date]) if sig_date in grid.sidx else None
            if got is None:
                continue
            start, p, entry = got
            if start >= n:  # entered at the next open, which has not happened yet
                continue
            for b in (["book"] if line == "book" else ["hi63_20", "hi63_16"]):
                books[b].append((start, p, PP.SLOTS[b]))
            updates.append((grid.sessions[start], round(float(entry), 4), grid.sessions[start + len(p) - 1], round(float(p[-1]), 6),
                            round(grid.max_mult(iid, start, entry), 6), len(p), len(p) >= PP.HORIZON, tid, line, iid))
        cur.executemany("UPDATE mb_paper_line SET entry_date = %s, entry_px = %s, last_date = %s, mult = %s, max_mult = %s, sessions_held = %s, closed = %s "
                        "WHERE tranche_id = %s AND line = %s AND instrument_id = %s", updates)
        if first >= n:
            return {"lines": len(updates)}
        days = grid.sessions[first:]
        marks = {}
        for b, lines in books.items():
            marks[b] = PP.pool_marks(lines, n, first)
        sc = pl.DataFrame({"date": days}).join(idx.select("date", "smallcap250"), on="date", how="left").with_columns(
            pl.col("smallcap250").fill_null(strategy="forward").fill_null(strategy="backward"))["smallcap250"].to_numpy()
        marks["book_overlay"] = PP.overlay_marks(marks["book"][0], sc)
        out = [(d, source, b, round(float(v[0][k]), 6), round(float(v[1][k]), 6)) for b, v in marks.items() for k, d in enumerate(days)]
        with cur.copy("COPY mb_paper_mark (date, source, book, equity, invested) FROM STDIN") as cp:
            for r in out:
                cp.write_row(r)
    return {"lines": len(updates), "marks": len(out), "equity": {b: round(float(v[0][-1]), 4) for b, v in marks.items()}}


def nightly(conn, bars, ins, idx, terminal, date: dt.date, model, score_day) -> str:
    grid = grid_of(bars, terminal)
    with conn.transaction():
        made = record_live(conn, grid, date, model, lambda f: score_day(conn, bars, ins, idx, f, model))
        fired = record_triggers(conn, grid, LIVE, grid.sidx[date])
        live = refresh(conn, grid, idx, LIVE)
        back = refresh(conn, grid, idx, BACKTEST)
    return f"mb paper {date}: new live tranches {[str(d) for d in made]}, new trigger lines {fired}, live {live}, backtest lines {back.get('lines')}"


def seed(conn, path: str) -> int:
    """Load out/paper/seed.json as backtest tranches; existing rows are kept. Marks follow on the next refresh."""
    data = json.loads(open(path, encoding="utf8").read())
    n = 0
    with conn.transaction(), conn.cursor() as cur:
        for t in data["tranches"]:
            d = dt.date.fromisoformat(t["signal_date"])
            cur.execute("INSERT INTO mb_paper_tranche (source, signal_date, watch) VALUES (%s, %s, %s) ON CONFLICT (source, signal_date) DO NOTHING RETURNING id",
                        (BACKTEST, d, json.dumps(t["watch"])))
            got = cur.fetchone()
            if got is None:
                continue
            tid = got[0]
            cur.executemany("INSERT INTO mb_paper_line (tranche_id, line, instrument_id, rank, signal_date) VALUES (%s, 'book', %s, %s, %s)",
                            [(tid, i, r, d) for i, r in t["book"]])
            cur.executemany("INSERT INTO mb_paper_line (tranche_id, line, instrument_id, rank, signal_date) VALUES (%s, 'hi63', %s, %s, %s)",
                            [(tid, i, r, dt.date.fromisoformat(e)) for i, r, e in t["hi63"]])
            n += 1
    return n

