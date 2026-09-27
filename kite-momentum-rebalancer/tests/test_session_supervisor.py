"""LV4 — the session supervisor over fakes: a token arriving is the event.

`gates/live-4-login-event.md` L1. The clock, the token, the broker and the reconciler are all
injected; every branch of `Supervisor.tick` and of the day's clock is a table here.
"""

from __future__ import annotations

import datetime as dt

from app import session_supervisor as S

IST = S.IST
MONDAY = dt.date(2026, 9, 28)


def at(hhmm: str, day: dt.date = MONDAY) -> dt.datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST)


class World:
    """The fakes behind `Deps`, mutable from a test."""

    def __init__(self) -> None:
        self.authed = False
        self.mtime: float | None = None
        self.holdings_rows: list[dict] = [
            {"symbol": "HELD", "quantity": 10, "last_price": 100.0},
            {"symbol": "ZERO", "quantity": 0, "last_price": 100.0},
        ]
        self.seeded: list[dict[str, float]] = []
        self.passes: list[dt.datetime] = []
        self.beats: list[tuple[str, str, str]] = []
        self.reconcile_raises = False

    def deps(self) -> S.Deps:
        def reconcile(now: dt.datetime) -> object:
            self.passes.append(now)
            if self.reconcile_raises:
                raise RuntimeError("book unavailable")

            class Run:
                ok = True

                def line(self) -> str:
                    return f"reconcile at {now:%H:%M:%S}"

            return Run()

        return S.Deps(
            authed=lambda: self.authed,
            token_mtime=lambda: self.mtime,
            holdings=lambda: list(self.holdings_rows),
            seed_positions=self.seeded.append,
            reconcile=reconcile,
            beat=lambda process, state, detail, now: self.beats.append((process, state, detail)),
        )


class TestTheLiveDrain:
    """LV8: in session, with a Kite session, the supervisor asks twt_auto to drain once a minute.

    The drain is the flag-gated ``twt_auto.drain_now``; here it is a recorder. With no drain
    handed in (the default) nothing is called — the field is optional so every older test and a
    desk without the module keep working.
    """

    def _world(self) -> tuple[World, list[dt.datetime]]:
        world = World()
        calls: list[dt.datetime] = []
        return world, calls

    def _deps(self, world: World, calls: list[dt.datetime], *, ran: bool = False) -> S.Deps:
        def drain(now: dt.datetime) -> object:
            calls.append(now)

            class Report:
                pass

            report = Report()
            report.ran = ran  # type: ignore[attr-defined]
            report.reason = "" if ran else "NO_PLAN: no TWT plan exists"  # type: ignore[attr-defined]
            report.attempts = [("AAA", "BUY_AT_OPEN", "SENT")] if ran else []  # type: ignore[attr-defined]
            return report

        deps = world.deps()
        return S.Deps(**{**deps.__dict__, "drain_twt": drain})

    def test_in_session_it_drains_once_a_minute_not_every_tick(self) -> None:
        world, calls = self._world()
        sup = S.Supervisor(self._deps(world, calls), reconcile_every=10.0, drain_every=60.0)
        world.mtime, world.authed = 1.0, True
        start = at("10:00")
        for seconds in range(0, 130, 10):
            sup.tick(start + dt.timedelta(seconds=seconds))
        assert [int((c - start).total_seconds()) for c in calls] == [0, 60, 120]
        idle = [b for b in world.beats if b[0] == "twt-auto"]
        assert idle and idle[-1][1] == "idle" and "NO_PLAN" in idle[-1][2]

    def test_a_drain_that_ran_beats_with_its_line_count(self) -> None:
        world, calls = self._world()
        sup = S.Supervisor(self._deps(world, calls, ran=True))
        world.mtime, world.authed = 1.0, True
        sup.tick(at("11:40"))
        assert sup.drains == 1
        assert ("twt-auto", "ran", "1 line(s) sent through execute_line") in world.beats

    def test_outside_the_session_or_without_a_token_it_never_drains(self) -> None:
        world, calls = self._world()
        sup = S.Supervisor(self._deps(world, calls))
        sup.tick(at("10:00"))  # no token
        world.mtime, world.authed = 1.0, True
        sup.tick(at("08:50"))  # token, before the open
        sup.tick(at("15:31"))  # token, after the close
        assert calls == []

    def test_no_drain_handed_in_means_nothing_is_called_and_nothing_breaks(self) -> None:
        world = World()
        sup = S.Supervisor(world.deps())
        world.mtime, world.authed = 1.0, True
        assert sup.tick(at("10:00")) == S.STATE_SESSION
        assert sup.drains == 0

    def test_a_drain_that_raises_is_a_heartbeat_not_a_crash(self) -> None:
        world, calls = self._world()

        def boom(now: dt.datetime) -> object:
            raise RuntimeError("desk db away")

        deps = S.Deps(**{**world.deps().__dict__, "drain_twt": boom})
        sup = S.Supervisor(deps)
        world.mtime, world.authed = 1.0, True
        assert sup.tick(at("10:00")) == S.STATE_SESSION
        assert ("twt-auto", "error", "drain raised RuntimeError") in world.beats

    def test_from_desk_wires_twt_autos_drain(self) -> None:
        import inspect

        source = inspect.getsource(S.from_desk)
        assert "drain_twt=twt_auto.drain_now" in source


class TestTheLoginEvent:
    def test_no_token_is_waiting_for_login_and_nothing_else_runs(self) -> None:
        world = World()
        sup = S.Supervisor(world.deps())
        assert sup.tick(at("09:20")) == S.STATE_WAITING
        assert world.passes == [] and world.seeded == []
        assert ("supervisor", S.STATE_WAITING) in {(p, s) for p, s, _ in world.beats}

    def test_a_token_arriving_seeds_exposure_and_reconciles_at_once_whatever_the_hour(self) -> None:
        world = World()
        sup = S.Supervisor(world.deps())
        sup.tick(at("08:40"))
        world.mtime, world.authed = 1.0, True
        state = sup.tick(at("08:41"))  # before the open: still the event
        assert sup.logins == 1
        assert world.seeded == [{"HELD": 1000.0}], "quantity x last price; zero quantities dropped"
        assert world.passes == [at("08:41")], "one recovery pass, even before the open"
        assert state == S.STATE_IDLE
        assert ("supervisor", S.STATE_LOGIN) in {(p, s) for p, s, _ in world.beats}

    def test_a_late_login_at_eleven_starts_the_session_work(self) -> None:
        world = World()
        sup = S.Supervisor(world.deps())
        sup.tick(at("10:59"))
        world.mtime, world.authed = 5.0, True
        assert sup.tick(at("11:00")) == S.STATE_SESSION
        assert world.passes == [at("11:00")]

    def test_a_rewritten_token_that_does_not_authenticate_is_not_a_login(self) -> None:
        world = World()
        sup = S.Supervisor(world.deps())
        world.mtime = 2.0  # written, but Kite refuses it (expired at 06:00)
        assert sup.tick(at("09:20")) == S.STATE_WAITING
        assert sup.logins == 0 and world.seeded == []

    def test_the_same_token_is_one_login_not_one_a_tick(self) -> None:
        world = World()
        sup = S.Supervisor(world.deps())
        world.mtime, world.authed = 1.0, True
        sup.tick(at("09:20"))
        sup.tick(at("09:20", MONDAY) + dt.timedelta(seconds=10))
        assert sup.logins == 1 and len(world.seeded) == 1

    def test_a_second_token_the_same_day_is_a_second_login(self) -> None:
        world = World()
        sup = S.Supervisor(world.deps())
        world.mtime, world.authed = 1.0, True
        sup.tick(at("09:20"))
        world.mtime = 2.0
        sup.tick(at("12:00"))
        assert sup.logins == 2


class TestTheReconcilerCadence:
    def test_in_session_the_reconciler_runs_every_ten_seconds_and_beats(self) -> None:
        world = World()
        sup = S.Supervisor(world.deps(), reconcile_every=10.0)
        world.mtime, world.authed = 1.0, True
        start = at("10:00")
        for seconds in (0, 5, 10, 15, 20):
            sup.tick(start + dt.timedelta(seconds=seconds))
        # the login pass at 0, then 10 and 20 — never at 5 or 15
        assert [int((p - start).total_seconds()) for p in world.passes] == [0, 10, 20]
        reconciler = [b for b in world.beats if b[0] == "reconciler"]
        assert reconciler and reconciler[-1][1] == "ok" and "reconcile at 10:00:20" in reconciler[-1][2]

    def test_outside_the_session_with_a_token_it_idles_and_does_not_reconcile(self) -> None:
        world = World()
        sup = S.Supervisor(world.deps())
        world.mtime, world.authed = 1.0, True
        sup.tick(at("16:00"))  # the login pass
        sup.tick(at("16:00") + dt.timedelta(seconds=10))
        assert len(world.passes) == 1
        assert world.beats[-1][:2] == ("supervisor", S.STATE_IDLE)

    def test_a_failing_pass_is_a_heartbeat_not_a_crash(self) -> None:
        world = World()
        world.reconcile_raises = True
        sup = S.Supervisor(world.deps())
        world.mtime, world.authed = 1.0, True
        assert sup.tick(at("10:00")) == S.STATE_SESSION
        assert any(b[0] == "reconciler" and b[1] == "error" for b in world.beats)


class TestTheClock:
    def test_next_wake_is_now_inside_a_weekday_and_the_next_weekday_morning_outside(self) -> None:
        assert S.next_wake(at("10:00")) == at("10:00")
        assert S.next_wake(at("08:29")) == at("08:30")
        assert S.next_wake(at("16:00")) == at("08:30", MONDAY + dt.timedelta(days=1))
        friday = MONDAY + dt.timedelta(days=4)
        assert S.next_wake(at("16:00", friday)) == at("08:30", MONDAY + dt.timedelta(days=7))
        saturday = MONDAY + dt.timedelta(days=5)
        assert S.next_wake(at("10:00", saturday)) == at("08:30", MONDAY + dt.timedelta(days=7))

    def test_run_forever_sleeps_to_the_wake_then_ticks_every_ten_seconds(self) -> None:
        world = World()
        sup = S.Supervisor(world.deps())
        moment = {"now": at("08:00")}
        slept: list[float] = []

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            moment["now"] = moment["now"] + dt.timedelta(seconds=seconds)

        ticks = S.run_forever(sup, now_fn=lambda: moment["now"], sleep_fn=sleep, limit=3)
        assert ticks == 3
        assert slept[0] == 1800.0  # 08:00 → 08:30
        assert slept[1:] == [S.TICK_SECONDS] * 3


class TestTheHeartbeatTable:
    def test_one_row_a_process_upserted(self, tmp_path) -> None:  # noqa: ANN001
        import functools

        from app.analytics import db as _db

        path = str(tmp_path / "desk.db")
        with _db.connect(path) as conn:
            conn.execute(
                "CREATE TABLE lv_heartbeat (user_id INTEGER, process TEXT, state TEXT, detail TEXT, "
                "at TEXT, PRIMARY KEY (user_id, process))"
            )
        beats = S.PgHeartbeats(functools.partial(_db.connect, path), user_id=1, schema="")
        beats.beat("supervisor", "waiting_for_login", "no token", at("09:00"))
        beats.beat("supervisor", "session", "open", at("09:20"))
        beats.beat("reconciler", "ok", "x", at("09:20"))
        with _db.connect(path) as conn:
            rows = conn.execute("SELECT process, state FROM lv_heartbeat ORDER BY process").fetchall()
        assert [tuple(r) for r in rows] == [("reconciler", "ok"), ("supervisor", "session")]
