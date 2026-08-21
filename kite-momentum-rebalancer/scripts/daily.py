#!/usr/bin/env python
"""One command for the daily data run. Idempotent, safe to repeat, safe to schedule.

WHY THIS EXISTS
History only accumulates from the day you start collecting it. A missed session is not
recoverable: kc.margins() has no history, so a backfilled snapshot cannot know that day's
cash, and breadth cannot be reconstructed from a scan you no longer have. Every skipped
day is a permanent hole in the track record and in any future regime backtest.

WHAT IT DOES, IN ORDER
    1. index history      structural indices + the momentum sentinel
    2. benchmark PRI      NIFTY 500 and the momentum index
    3. EOD snapshot       NAV, holdings, cash -> the TWR index chain
    4. trade capture      today's fills -> FIFO tax lots
    5. breadth            only when a scan CSV is supplied; never invented
    6. regime preview     an observe-mode evaluation, stored as a preview

Trade capture has the same one-shot property as the snapshot, for the same reason:
Kite's /trades endpoint is same-day only, so a fill not recorded on its own session
survives nowhere but a Console export.

Every step is independently idempotent, so re-running changes nothing. A step that fails
does not stop the others: partial collection beats none, and the summary says exactly what
was missed.

THE ONE THING IT CANNOT DO FOR YOU
Kite access tokens expire daily, around 6am IST, and the standard login flow has no
refresh token. A scheduled run will fail until you have logged in that day. Schedule it
after your usual login, and read the exit code: 0 means everything landed.

Usage:
    python -m scripts.daily
    python -m scripts.daily --scan data/uploads/scan_2026-08-15.csv
    python -m scripts.daily --check          # report state, change nothing
    python -m scripts.daily --only "index history"   # one step, same idempotence
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
import traceback

sys.path.insert(0, ".")

from app import config as C                                       # noqa: E402
from app.analytics import breadth as B                            # noqa: E402
from app.analytics import daily_runs as DR                        # noqa: E402
from app.analytics import db, index_cache as IC                   # noqa: E402
from app.analytics import regime_store as RS, snapshot as SNAP    # noqa: E402
from app.core import regime as R                                  # noqa: E402

OK, SKIP, FAIL = "ok", "skipped", "FAILED"

MARKET_CLOSE = dt.time(15, 30)          # IST


def _before_close(now: dt.datetime) -> bool:
    """True on a weekday before the close. Weekends and holidays hold a settled book, so
    a snapshot taken then is the previous session's close and is safe."""
    return now.weekday() < 5 and now.time() < MARKET_CLOSE


STEPS = ("index history", "benchmark PRI", "EOD snapshot", "trade capture",
         "stale orders", "breadth",
         "regime preview")


class Runner:
    """Collects step outcomes so one failure never hides the rest."""

    def __init__(self, verbose: bool = True, only: set[str] | None = None):
        self.rows: list[tuple[str, str, str]] = []
        self.verbose = verbose
        self.only = only          # None runs every step

    def step(self, name: str, fn, *, skip_reason: str | None = None):
        if self.only is not None and name not in self.only:
            self.rows.append((name, SKIP, "not selected by --only"))
            return None
        if skip_reason:
            self.rows.append((name, SKIP, skip_reason))
            return None
        try:
            detail = fn()
            self.rows.append((name, OK, detail or ""))
            return detail
        except Exception as exc:
            self.rows.append((name, FAIL, str(exc).splitlines()[0][:110]))
            if self.verbose:
                traceback.print_exc(limit=2)
            return None

    @property
    def failed(self) -> bool:
        return any(s == FAIL for _, s, _ in self.rows)

    def report(self) -> None:
        print()
        print("=" * 78)
        print(f"{'step':<22}{'result':<10}detail")
        print("=" * 78)
        for name, status, detail in self.rows:
            print(f"{name:<22}{status:<10}{detail}")
        print("=" * 78)
        print("every step is idempotent — re-running this changes nothing"
              if not self.failed else
              "some steps failed; re-run once the cause is fixed, nothing is duplicated")


def _safe_record(conn, *, steps, trigger, session_date, started, outcome=None) -> None:
    """Recording is bookkeeping: a failure here must never turn a collection that
    otherwise landed into a crash, nor mask the real exit code."""
    try:
        DR.record(conn, steps=[tuple(s) for s in steps], trigger=trigger,
                  session_date=session_date, outcome=outcome,
                  duration_s=round((dt.datetime.now() - started).total_seconds(), 2))
    except Exception as exc:
        print(f"  (could not record this run: {exc})")


def _state(conn) -> dict:
    counts = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
              for t in ("snapshots", "breadth_readings", "trades", "index_series",
                        "regime_evaluations", "benchmark")}
    rows = db.snapshot_series(conn)
    counts["first_snapshot"] = rows[0]["date"] if rows else None
    counts["last_snapshot"] = rows[-1]["date"] if rows else None
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="daily data collection")
    ap.add_argument("--scan", help="weekly momentum scan CSV, for the breadth reading")
    ap.add_argument("--db", default=None)
    ap.add_argument("--check", action="store_true", help="report state, change nothing")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--force-snapshot", action="store_true",
                    help="take the EOD snapshot before the close. It will record an "
                         "intraday NAV as the day's final value; only useful for a "
                         "deliberate backfill.")
    ap.add_argument("--only", action="append", choices=STEPS, metavar="STEP",
                    help=f"run only these steps ({', '.join(STEPS)}); repeatable. "
                         "The steps stay independently idempotent either way.")
    ap.add_argument("--source", choices=("schedule", "cli", "page"), default="cli",
                    help="who launched this run; recorded so the interface can tell an "
                         "unattended failure from one you were watching")
    a = ap.parse_args()

    today = dt.date.today()
    cfg = C.regime_config()
    run = Runner(verbose=not a.quiet, only=set(a.only) if a.only else None)
    started = dt.datetime.now()

    # A --only run collects part of a session by design, so it must not be recorded as
    # the day's collection: doing so would mark the day complete when it is not.
    record_run = not a.only

    with db.connect(a.db) as conn:
        db.migrate(conn)

        if a.check:
            st = _state(conn)
            print(f"as of {today}")
            for k, v in st.items():
                print(f"  {k:<22}{v}")
            gap = None
            if st["last_snapshot"]:
                gap = (today - dt.date.fromisoformat(st["last_snapshot"])).days
                print(f"\n  last snapshot was {gap} day(s) ago")
                if gap > 1:
                    print("  those sessions cannot be recovered: margins() has no history")
            return 0

        # --- auth ----------------------------------------------------------------------
        from app.kite_client import Kite
        try:
            kite = Kite()
            authed = kite.is_authed()
        except Exception as exc:
            print(f"cannot construct the Kite client: {exc}")
            return 2
        if not authed:
            print("Kite session expired. Tokens last one trading day and the standard "
                  "login flow has no refresh token.")
            print(f"  log in:  {kite.login_url()}")
            print("  then re-run this command.")
            # Recorded, not just printed: this is THE failure mode of the scheduled job,
            # and unrecorded it goes nowhere but a log nobody opens.
            if record_run:
                _safe_record(conn, steps=[(s, DR.FAIL, "not reached: no Kite session")
                                          for s in STEPS],
                             trigger=a.source, session_date=today, outcome=DR.AUTH,
                             started=started)
            return 2

        # --- 1. index history ------------------------------------------------------------
        def _indices():
            res = asyncio.run(IC.update_all(kite, conn, cfg))
            return ", ".join(f"{r['index_name'].split()[-1]}+{r['written']}" for r in res)
        run.step("index history", _indices)

        # --- 2. benchmark PRI ------------------------------------------------------------
        def _benchmarks():
            from app.analytics import benchmark as BM
            out = []
            for name in ("NIFTY 500", cfg.momentum_sentinel):
                s = asyncio.run(BM.kite_index_series(name, "2020-01-01", kite=kite))
                r = BM.upsert_benchmark(conn, s.attrs["index_name"], s, kind=BM.PRI)
                out.append(f"{r['index_name']}+{r['rows']}")
            return ", ".join(out) + "  (PRI — TRI is authoritative)"
        run.step("benchmark PRI", _benchmarks)

        # --- 3. EOD snapshot --------------------------------------------------------------
        # REFUSED BEFORE THE CLOSE, because the damage is silent and permanent. A snapshot
        # is insert-if-absent: a row written at 09:30 holds an intraday NAV, and the 18:30
        # run then finds the day already present and leaves it exactly as it was. The
        # track record keeps a number that was never the close and nothing ever says so.
        #
        # This is not hypothetical — a verification kickstart at 01:44 wrote exactly such a
        # row for 2026-08-17 and it had to be deleted by hand. The plist comment warned
        # about it; a comment is not a guard.
        def _snapshot():
            snap = asyncio.run(SNAP.run(kite=kite, db_path=a.db))
            return (f"{snap['date']} nav Rs {snap['nav']:,.0f} "
                    f"index {snap['index_value']:.4f} [{snap['outcome']}]")

        _now = dt.datetime.now()
        too_early = _before_close(_now) and not a.force_snapshot
        run.step("EOD snapshot", _snapshot,
                 skip_reason=(f"it is {_now:%H:%M}, before the 15:30 close — a snapshot now "
                              "would store an intraday NAV as the day's final value and the "
                              "evening run would not correct it. Pass --force-snapshot to "
                              "override." if too_early else None))

        # --- 3b. trade capture ---------------------------------------------------------
        def _trades():
            from app.analytics import tradebook as TB
            r = TB.capture_live_trades(conn, kite)
            if not r["captured"]:
                return "no fills today"
            return (f"{r['new']} new of {r['captured']} fills, "
                    f"{', '.join(r['symbols'])}")
        run.step("trade capture", _trades)

        # --- 3b. orders left working by a previous session -----------------------------
        def _stale_orders():
            from app.analytics import plan_store as _ps
            out = _ps.lapse_stale_orders(conn)
            if not out["n"]:
                return "nothing left working from an earlier session"
            return ", ".join(f"{r['symbol']} {r['was']}->{r['now']} "
                             f"({r['filled']}/{r['planned']})" for r in out["lapsed"])
        run.step("stale orders", _stale_orders)

        # --- 4. breadth --------------------------------------------------------------------
        def _breadth():
            from app.scoring import audit, load_scan
            scan = load_scan(a.scan)
            reading = B.reading_from_audit(audit(scan), scan)
            outcome = B.save_reading(conn, reading)
            return (f"{reading.pct_above_20dma:.1f}% above 20-DMA, "
                    f"{reading.coverage_pct:.0f}% coverage [{outcome}]")
        run.step("breadth", _breadth,
                 skip_reason=None if a.scan else
                 "no --scan given; breadth is never invented from a stale reading")

        # --- 5. regime preview --------------------------------------------------------------
        def _regime():
            names = IC.required_index_names(cfg)
            repo = IC.IndexRepository(conn)
            session = repo.signal_session_for_week(names, today)
            if session is None:
                raise RuntimeError("no session where every required index has a final candle")
            sigs = IC.build_all_signals(conn, cfg, as_of=session)
            sentinel = sigs.pop(cfg.momentum_sentinel)
            reading = B.latest_reading(conn, on_or_before=session)
            rows = db.snapshot_series(conn)
            nav = float(rows[-1]["nav"]) if rows else 0.0
            invested = float(rows[-1]["invested"]) if rows else 0.0
            actual = round(invested / nav * 100, 2) if nav else 0.0

            state = R.evaluate(
                as_of_date=session, scheduled_week_end=session,
                signal_session_date=session, index_signals=sigs, sentinel=sentinel,
                breadth=reading,
                book=R.resolve_book_weights(cfg,
                                            last_r1_snapshot=RS.last_r1_snapshot(conn)),
                exposure=R.ExposureSnapshot(actual_equity_pct=actual),
                cfg=cfg, previous=RS.previous_regime(conn))
            RS.save_preview(conn, state, mode=cfg.mode, dry_run=C.DRY_RUN)
            RS.record_exposure(conn, evaluation_id=state.evaluation_id,
                               actual_equity_pct=actual,
                               target_equity_cap_pct=state.target_equity_cap_pct)
            return (f"{state.policy_tier.value} cap {state.target_equity_cap_pct:.0f}% "
                    f"actual {actual:.1f}% buys={state.new_buys.value}")
        run.step("regime preview", _regime)

        run.report()
        if record_run:
            _safe_record(conn, steps=run.rows, trigger=a.source, session_date=today,
                         started=started)
        st = _state(conn)
        print(f"\nsnapshots {st['snapshots']} ({st['first_snapshot']} -> "
              f"{st['last_snapshot']}) · breadth {st['breadth_readings']} · "
              f"trades {st['trades']}")
        if st["breadth_readings"] < 26:
            print("  breadth history is still too thin for the tiered regime backtest; "
                  "pass --scan on the day you upload one")
        if st["trades"] == 0:
            print("  no tax lots: import a Console tradebook at /tradebook")

    return 1 if run.failed else 0


if __name__ == "__main__":
    sys.exit(main())
