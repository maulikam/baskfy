"""FO8 — the desk's `/fno` page and `POST /fno/execute`, on paper (`docs/fno/06` FO8, `05` §1, §4).

Goal: one click confirms a paper F1 or F2 plan, and the simulated fills, the GTT, the carry, the
rolls and the exits all happen. Each test drives the real route (`app.fno_desk`) through the real
executor (`app.fno_execute`) and the real FO gateway (paper-pinned, the covered-overnight rule
wired) over a real PostgreSQL with the `fo_` schema, with a fake clock, a fixture book of Kite
quotes and depth, and a recording broker that must see **nothing** (FO7's replay harness,
`test_fno_monitor.World`):

* confirm → simulated fills walking the depth (`fo_fill.simulated`), client ids `plan_id:symbol`,
  an `OPEN` position; F2's GTT rests under its paper handle (`TestTheConfirm`);
* an expired plan (410 `PLAN_EXPIRED`), a missing or false confirm (400 `CONFIRM_REQUIRED`, before
  any read), a second post of the same plan (409 `NOT_ISSUED`, nothing sent twice), a refused leg
  (its gateway reason by name), a LIVE sleeve (`LIVE_NOT_BUILT`) (`TestRefusals`);
* the monitor's exit under the confirm, shown on the page as it happens (`TestUnderTheConfirm`);
* the page: `05` §4's confirm sentences verbatim, `Confirm (paper)`, PAPER per sleeve, the
  countdown, the credit drift in amber, the red flag on a future without its GTT, and the nav tab
  with its badge (`TestThePage`);
* structure: one POST, no GET that executes, nothing here reaches `place_order`/`place_gtt`.

Skipped without `BASKFY_TEST_DATABASE_URL` (the structural tests run regardless).
"""
from __future__ import annotations

import contextlib
import inspect
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import config as C
from app import fno_desk
from app import fno_execute as X
from baskfy_core.fno.config import FoSleeve
from baskfy_execution.risk import RiskManager
from test_fno_monitor import (  # noqa: F401 - the FO7 replay harness and its fixtures
    F1_ENTRY,
    F2_ENTRY,
    FUT_NOV,
    HARD_EXIT,
    LOT,
    NEUTRAL,
    STRIKES,
    TYPES,
    World,
    at,
    before,
    book,
    condor_book,
    conn,
    f2_scan,
    opt_symbol,
    requires_db,
    user,
    world,
)

#: `05` §4 verbatim: the minus in "E-1" is U+2212, as the spec prints it.
E_MINUS_1 = "E\u22121"
F1_SENTENCE = ("This confirm also authorises this structure's 50 % profit take, its loss close at "
               f"1.5\u00d7 the credit (shorts first), and its {E_MINUS_1} 15:00 exit. It places "
               "nothing else.")  # fmt: skip - Maulik, DECISIONS-FO M.3
F2_SENTENCE = (f"This confirm also authorises this position's trailing GTT stop, its {E_MINUS_1} "
               "rolls and its 40-session time exit")  # fmt: skip
FO_FLAGS = ("OPTIONS_ENABLED", "FNO_CARRY_ENABLED", "FNO_F1_EXECUTION_ENABLED",
            "FNO_F2_EXECUTION_ENABLED")  # fmt: skip


class Desk:
    """The route's seams pointed at the replay world; `now` is the desk's clock."""

    def __init__(self, w: World, monkeypatch: pytest.MonkeyPatch) -> None:
        from app import main as M  # noqa: PLC0415
        from fastapi.testclient import TestClient  # noqa: PLC0415

        self.w = w
        self.now = at(F1_ENTRY, 9, 25)

        @contextlib.contextmanager
        def store() -> Iterator[X.FoStore]:
            yield w.store

        monkeypatch.setattr(fno_desk, "open_store", store)
        monkeypatch.setattr(fno_desk, "_now", lambda: self.now)
        monkeypatch.setattr(fno_desk, "gateway_for", w.gateway_for)
        monkeypatch.setattr(fno_desk, "quotes", lambda: w.quotes)
        monkeypatch.setattr(fno_desk, "view_sources", lambda: (w.quotes, None, None))
        monkeypatch.setitem(fno_desk._badge_cache, "at", -1e9)
        self.client = TestClient(M.app)

    def post(self, plan_id: str, confirm: str | None = "true") -> Any:  # noqa: ANN401
        data = {"plan_id": plan_id}
        if confirm is not None:
            data["confirm"] = confirm
        return self.client.post("/fno/execute", data=data)

    def page(self) -> str:
        fno_desk._badge_cache["at"] = -1e9
        r = self.client.get("/fno")
        assert r.status_code == 200
        return r.text


@pytest.fixture
def desk(
    world: World,  # noqa: F811 - the imported fixture, requested by name
    monkeypatch: pytest.MonkeyPatch,
) -> Desk:
    for name in FO_FLAGS:
        monkeypatch.setattr(C, name, False)
    return Desk(world, monkeypatch)


def f1_scan(w: World, *, credit: str | None = None) -> None:
    legs = [{"entry_seq": i, "role": r, "strike": str(STRIKES[r]), "option_type": TYPES[r],
             "expiry": "2026-11-24", "qty_sign": 1 if r.startswith("LONG") else -1}
            for i, r in enumerate(("LONG_PUT", "LONG_CALL", "SHORT_PUT", "SHORT_CALL"), 1)]
    detail = {"underlying": "NIFTY", "entry_session": F1_ENTRY.isoformat(),
              "expiry": "2026-11-24", "hard_exit_date": HARD_EXIT.isoformat(), "legs": legs,
              "lot_size": LOT}  # fmt: skip
    if credit is not None:
        detail["credit_points"] = credit
    w.scan("F1N", before(F1_ENTRY, 1), "NIFTY", detail)


def raise_f1(d: Desk, *, credit: str | None = None) -> str:
    f1_scan(d.w, credit=credit)
    d.w.quotes.books.update(NEUTRAL)
    raised = d.w.tick(at(F1_ENTRY, 9, 20)).raised
    assert len(raised) == 1
    return raised[0]


def raise_f2(d: Desk) -> str:
    f2_scan(d.w)
    d.w.quotes.books[FUT_NOV] = book("1399.90", "1400.00", last="1400.00")
    raised = d.w.tick(at(F2_ENTRY, 9, 20)).raised
    assert len(raised) == 1
    d.now = at(F2_ENTRY, 9, 22)
    return raised[0]


def fill_count(w: World) -> int:
    return int(w.rows("SELECT count(*) AS n FROM fo_fill WHERE user_id = ?", w.uid)[0]["n"])


# --- the confirm ---------------------------------------------------------------------------------


@requires_db
class TestTheConfirm:
    def test_f1_confirm_is_four_simulated_fills_longs_first_and_open(self, desk: Desk) -> None:
        plan_id = raise_f1(desk)
        r = desk.post(plan_id)
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["outcome"], body["simulated"], body["plan_id"]) == ("OPEN", True, plan_id)
        fills = desk.w.fills(plan_id)
        assert [f["role"] for f in fills] == ["LONG_PUT", "LONG_CALL", "SHORT_PUT", "SHORT_CALL"]
        assert all(f["simulated"] for f in fills)
        assert [f["client_id"] for f in fills] == [
            f"{plan_id}:{opt_symbol(r)}" for r in ("LONG_PUT", "LONG_CALL", "SHORT_PUT",
                                                    "SHORT_CALL")]  # fmt: skip
        pos = desk.w.position(int(body["position_id"]))
        assert pos["closed_at"] is None and pos["simulated"] is True
        plan = desk.w.plan_row(plan_id)
        assert plan["status"] == "OPEN" and plan["confirmed_at"] is not None
        assert desk.w.kc.calls == []

    def test_f2_confirm_fills_the_future_and_rests_its_gtt_under_a_paper_handle(
        self, desk: Desk
    ) -> None:
        plan_id = raise_f2(desk)
        r = desk.post(plan_id)
        assert r.status_code == 200, r.text
        body = r.json()
        pid = int(body["position_id"])
        assert body["outcome"] == "OPEN" and body["detail"]["gtt"] == f"PAPER-{pid}"
        pos = desk.w.position(pid)
        assert pos["gtt_id"] == f"PAPER-{pid}" and pos["stop_price"] is not None
        fills = desk.w.fills(plan_id)
        assert fills and all(f["simulated"] for f in fills)
        assert fills[0]["client_id"] == f"{plan_id}:{FUT_NOV}"
        assert desk.w.kc.calls == []

    def test_every_flag_false_is_simulated_even_with_dry_run_false(
        self, desk: Desk, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(C, "DRY_RUN", False)
        plan_id = raise_f1(desk)
        assert desk.post(plan_id).json()["outcome"] == "OPEN"
        assert {f["simulated"] for f in desk.w.fills(plan_id)} == {True}
        assert desk.w.kc.calls == []


# --- the refusals --------------------------------------------------------------------------------


@requires_db
class TestRefusals:
    def test_an_expired_plan_is_refused_410_and_nothing_is_sent(self, desk: Desk) -> None:
        plan_id = raise_f1(desk)
        desk.now = at(F1_ENTRY, 9, 50)  # expires_at, exactly
        r = desk.post(plan_id)
        assert r.status_code == 410 and r.json()["code"] == "PLAN_EXPIRED"
        assert desk.w.fills(plan_id) == [] and desk.w.plan_row(plan_id)["status"] == "ISSUED"

    @pytest.mark.parametrize("confirm", [None, "false", "", "True", "1"])
    def test_a_missing_or_false_confirm_is_400_before_any_read(
        self, desk: Desk, monkeypatch: pytest.MonkeyPatch, confirm: str | None
    ) -> None:
        def boom() -> None:
            raise AssertionError("the store was opened")

        monkeypatch.setattr(fno_desk, "open_store", boom)
        r = desk.post("F1N-20261103-NIFTY-u1", confirm)
        assert r.status_code == 400 and r.json()["code"] == "CONFIRM_REQUIRED"

    def test_an_unknown_plan_is_404(self, desk: Desk) -> None:
        r = desk.post("F1N-19990101-NOPE-u0")
        assert r.status_code == 404 and r.json()["code"] == "UNKNOWN_PLAN"

    def test_a_second_post_of_the_same_plan_sends_nothing(self, desk: Desk) -> None:
        plan_id = raise_f1(desk)
        assert desk.post(plan_id).json()["outcome"] == "OPEN"
        sent = fill_count(desk.w)
        again = desk.post(plan_id)
        assert again.status_code == 409 and again.json()["code"] == "NOT_ISSUED"
        assert fill_count(desk.w) == sent == 4
        n = desk.w.rows("SELECT count(*) AS n FROM fo_position WHERE user_id = ?", desk.w.uid)
        assert n[0]["n"] == 1
        assert desk.w.kc.calls == []

    def test_a_refused_leg_is_shown_by_its_name(self, desk: Desk, tmp_path: Path) -> None:
        plan_id = raise_f2(desk)
        halted = RiskManager()
        halted.state.killed = True
        halted.state.reasons = ["FO8 drill halt"]
        desk.w.gateways[FoSleeve.F2] = X.build_fo_gateway(
            FoSleeve.F2, desk.w.kc, halted, str(tmp_path / "fo-halt.jsonl"))
        body = desk.post(plan_id).json()
        assert body["outcome"] == "ABANDONED_PARTIAL" and body["code"] == "ABANDONED_PARTIAL"
        assert "RISK_BLOCKED" in body["detail"] and "KILL SWITCH" in body["detail"]
        plan = desk.w.plan_row(plan_id)
        assert plan["status"] == "ABANDONED_PARTIAL" and "RISK_BLOCKED" in plan["reason"]
        page = desk.page()
        assert "ABANDONED_PARTIAL" in page and "KILL SWITCH: FO8 drill halt" in page
        assert desk.w.fills(plan_id) == [] and desk.w.kc.calls == []

    def test_a_live_sleeve_is_refused_before_any_order(
        self, desk: Desk, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan_id = raise_f1(desk)
        for name in ("OPTIONS_ENABLED", "FNO_CARRY_ENABLED", "FNO_F1_EXECUTION_ENABLED"):
            monkeypatch.setattr(C, name, True)
        monkeypatch.setattr(C, "DRY_RUN", False)
        r = desk.post(plan_id)
        assert r.status_code == 409 and r.json()["code"] == "LIVE_NOT_BUILT"
        assert desk.w.fills(plan_id) == [] and desk.w.plan_row(plan_id)["status"] == "ISSUED"
        assert desk.w.kc.calls == []


# --- under the confirm ---------------------------------------------------------------------------


@requires_db
class TestUnderTheConfirm:
    def test_the_monitors_profit_take_runs_under_the_click_and_the_page_shows_it(
        self, desk: Desk
    ) -> None:
        plan_id = raise_f1(desk)
        pid = int(desk.post(plan_id).json()["position_id"])
        day = before(HARD_EXIT, 5)
        desk.w.quotes.books.update(condor_book("20", "18", "3", "2"))  # cost to close 33 <= 46
        assert desk.w.tick(at(day, 11, 1)).actions == [f"{plan_id}-X"]
        assert desk.w.position(pid)["closed_reason"] == "PROFIT_TAKE"
        desk.now = at(day, 11, 2)
        page = desk.page()
        assert f"{plan_id}-X" in page and "PROFIT_TAKE" in page
        assert desk.w.kc.calls == []


# --- the page ------------------------------------------------------------------------------------


@requires_db
class TestThePage:
    def test_the_plan_card_the_sentence_the_button_and_paper(self, desk: Desk) -> None:
        plan_id = raise_f1(desk, credit="70.00")  # 93.50 on live mids: a 33.57 % drift
        page = desk.page()
        assert plan_id in page
        assert '<button type="submit" class="btn-saffron">Confirm (paper)</button>' in page
        assert F1_SENTENCE.replace("'", "&#39;") in page
        for sleeve in ("F1N", "F1B", "F2"):
            assert f'id="fnoMode{sleeve}">{sleeve} PAPER<' in page
        assert 'class="muted fno-countdown"' in page and "25 min left" in page
        assert f'<span class="pill warn" id="fnoDrift-{plan_id}">drift 33.57 %</span>' in page
        assert "long put → long call → short put → short call (longs first)" in page
        assert "within free" in page  # the basket margin against free margin
        view = desk.client.get("/fno/data").json()
        card = view["plans"][0]
        assert card["credit_drift_amber"] is True
        assert card["hard_exit_date"] == HARD_EXIT.isoformat()
        assert card["legs"][0]["oi"] == 1000000 and card["legs"][0]["spread_pct"] is not None

    def test_an_expired_plan_has_no_button(self, desk: Desk) -> None:
        raise_f1(desk)
        desk.now = at(F1_ENTRY, 9, 50)
        page = desk.page()
        assert '<button type="submit" class="btn-saffron">Confirm (paper)</button>' not in page
        assert 'class="fno-confirm"' not in page

    def test_f2_sentence_the_gtt_and_the_red_flag(self, desk: Desk) -> None:
        plan_id = raise_f2(desk)
        page = desk.page()
        assert F2_SENTENCE.replace("'", "&#39;") in page
        pid = int(desk.post(plan_id).json()["position_id"])
        page = desk.page()
        assert f"<code>PAPER-{pid}</code>" in page and f"fnoNaked-{pid}" not in page
        desk.w.store.update_position(pid, gtt_id=None)
        assert f'id="fnoNaked-{pid}"' in desk.page()

    def test_the_nav_tab_and_its_badge(self, desk: Desk) -> None:
        plan_id = raise_f1(desk)
        page = desk.page()
        assert 'href="/fno"' in page and "F&amp;O Overnight" in page
        assert 'href="/nifty-options"' in page
        assert 'id="navFnoBadge"' not in page  # nothing open yet
        desk.post(plan_id)
        page = desk.page()
        assert f'id="navFnoBadge">1 open · exit {HARD_EXIT.isoformat()}</span>' in page
        assert 'title="F1N PAPER · F1B PAPER · F2 PAPER"' in page

    def test_the_view_says_why_it_has_nothing(
        self, desk: Desk, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken() -> Iterator[X.FoStore]:
            raise RuntimeError("no database")

        monkeypatch.setattr(fno_desk, "open_store", broken)
        r = desk.client.get("/fno")
        assert r.status_code == 200 and "Unavailable: RuntimeError: no database" in r.text


# --- structure -----------------------------------------------------------------------------------


class TestStructure:
    def test_one_post_and_no_get_executes(self) -> None:
        source = inspect.getsource(fno_desk)
        assert re.findall(r'@router\.post\("([^"]+)"', source) == ["/fno/execute"]
        assert not re.search(r'@router\.get\("[^"]*execute', source)

    def test_nothing_here_reaches_the_broker_around_the_executor(self) -> None:
        source = inspect.getsource(fno_desk)
        code = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", source)
        assert re.search(r"\bplace_gtt|\.place_order\(|\.place\(", code) is None
        assert "X.execute_entry(" in code and "confirm=True" in code

    def test_the_sentences_are_05s_verbatim(self) -> None:
        assert fno_desk.sentence_for("F1N") == F1_SENTENCE
        assert fno_desk.sentence_for("F1B") == F1_SENTENCE
        assert fno_desk.sentence_for("F2") == F2_SENTENCE

    def test_the_page_offers_one_form_and_it_is_the_confirm(self) -> None:
        html = (Path(__file__).resolve().parent.parent / "app/templates/fno.html").read_text()
        assert re.findall(r'<form[^>]*action="([^"]+)"', html) == ["/fno/execute"]
        assert len(re.findall(r"<input(?![^>]*hidden)", html)) == 0  # no field that moves money
