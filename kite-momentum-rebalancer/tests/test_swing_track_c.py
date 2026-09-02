"""SW10: the desk's Track-B and Track-C claims as theorems (docs/swing/06 SW10, docs/swing/02).

Each claim below is one the desk half of the swing book makes about itself, restated as a test
that would fail the moment the claim stopped being true:

* **Track B** — "with `BASKFY_SWING_EXECUTION_ENABLED=false` no code path from `/swing/execute`
  reaches a non-dry-run adapter." A spy wraps the REAL swing gateway (built over a broker
  client that explodes on contact) and records the gates every order-shaped call was made
  under, through the route, for a buy, a partial sell, a raised stop and a re-arm — with
  `DRY_RUN` true AND with it false. Every call saw `dry_run=True`; every journal line is a dry
  run; nothing reached the fake. And the spy is shown not to be blind: the one flag pair this
  run never sets makes it see `dry_run=False`, at which point the exploding fake is what stops
  the order.
* **Track C §3** — the monitor's strategy has no place call: over its code with docstrings
  stripped (`ast`), a word-boundary scan, plus `generate_targets` answering `[]` at runtime.
* **Track C §6** — every `sw_` write in the desk carries the sole user id: every `INSERT` and
  `UPDATE` in `swing_desk` / `swing_monitor` / `swing_execute` names `user_id` (statement by
  statement, one whitelisted `UPDATE … WHERE id = ?` with its reason), the id is
  `C.SOLE_USER_ID` / `BASKFY_SOLE_USER_ID` and never a literal, and after a full morning through
  the real module every row in every `sw_` table belongs to that user.
* **Track C §5 and `04` §6.5** — `execute_line`'s guards, as seeded property loops of 500
  cases each: a SELL for more than `quantity_open` is `BLOCKED` and the book is untouched; a
  RAISE at or below the resting stop is `BLOCKED` and the trigger is untouched. (No hypothesis
  in the desk's venv — `docs/02` locks it for the screener — so a `random.Random(SEED)` loop
  with the seed in every message.)
* **Track C §1/§2** — every order the module sends is `CNC`, `LIMIT` or `MARKET`, on `NSE`,
  with no `variety`; the gates never allow intraday or options whatever the weekly desk says.
* **STANDING-ANSWERS A5 / `04` §5.3, §8.4, §9.1 at the click** — the confirm-time gate
  (SW10.4) as a seeded property over 500 sequences of one to eight confirms on random books,
  rungs, sleeves and lines: after every confirm the book is inside the rung's exposure
  ceiling, the position count is inside `min(rung, max_open_positions)`, the session has
  taken at most three entries, and nothing was sent at a size the page did not show.

The fixtures are the neighbouring suites' own — `tests.test_swing_desk.Scenario` (the sqlite
twin of `0028_swing.py`) and `tests.test_swing_execute.MemoryStore` — imported as modules so
their tests are not collected twice.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import datetime as dt
import inspect
import json
import pathlib
import random
import re
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import config as C
from app import main as M
from app import swing_desk, swing_monitor
from app import swing_execute as X
from app.core.risk import RiskManager
from app.strategies import swing_breakout
from tests import test_swing_desk as desk_tests
from tests import test_swing_execute as exec_tests

SEED = 20260902
CASES = 500
D = Decimal
IST = desk_tests.IST
NOW = desk_tests.NOW
USER = desk_tests.USER

#: Every journal event the gateway can write on a path that did NOT reach a broker.
DRY_EVENTS = frozenset({"dry_run", "gtt_dry_run", "gtt_dry_run_delete", "gtt_band_warning",
                        "gtt_risk_note"})
#: The events that mean a broker answered. None of these may ever appear in this file.
BROKER_EVENTS = frozenset({"placed", "gtt_placed", "gtt_deleted", "rejected", "error",
                           "gtt_error", "gtt_delete_error"})


def _why(what: str) -> str:
    return f"{what} (seed {SEED})"


# =======================================================================================
# The spy: the real gateway, every order-shaped call recorded with the gates it ran under
# =======================================================================================
class SpyGateway:
    """Wraps `build_swing_gateway(...)`'s real instance. Records, then delegates."""

    def __init__(self, real) -> None:
        self.real = real
        self.seen: list[tuple[str, bool]] = []

    def _record(self, method: str) -> None:
        self.seen.append((method, self.real._gates().dry_run))

    async def place(self, **kw):
        self._record("place")
        return await self.real.place(**kw)

    async def place_gtt_stop(self, **kw):
        self._record("place_gtt_stop")
        return await self.real.place_gtt_stop(**kw)

    async def delete_gtt(self, **kw):
        self._record("delete_gtt")
        return await self.real.delete_gtt(**kw)

    def events(self) -> list[str]:
        path = pathlib.Path(self.real._journal_path)
        if not path.exists():
            return []
        return [json.loads(row)["event"] for row in path.read_text().splitlines()]


async def _no_wait() -> None:
    """The limiter's clock removed: guards, risk, idempotency and journal stay."""


def _unthrottled(gw):
    gw.limits.order_slot = _no_wait
    gw.limits.api_slot = _no_wait
    return gw


@pytest.fixture()
def spy(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    real = X.build_swing_gateway(desk_tests.ExplodingKC(), RiskManager())
    return SpyGateway(_unthrottled(real))


@pytest.fixture()
def scenario(tmp_path):
    return desk_tests.Scenario(str(tmp_path / "swing.db"))


def _client(scenario, monkeypatch, tmp_path, gateway) -> TestClient:
    monkeypatch.setattr(M, "_kite", None)
    monkeypatch.setattr(swing_desk, "open_store",
                        lambda: contextlib.nullcontext(scenario.store()))
    monkeypatch.setattr(swing_desk, "_now", lambda: NOW)
    monkeypatch.setattr(swing_desk, "last_price", lambda symbol: desk_tests.LAST_PRICES.get(symbol))
    monkeypatch.setattr(C, "TOKEN_FILE", str(tmp_path / "no-such-token.json"))
    monkeypatch.setattr(swing_desk, "_swing_gateway", gateway)
    return TestClient(M.app)


def _confirm(client: TestClient, plan_id: str, line_id: int) -> dict:
    r = client.post("/swing/execute", data={"plan_id": plan_id, "line_id": line_id,
                                            "confirm": "true"})
    assert r.status_code == 200, r.text
    return r.json()


# =======================================================================================
# Track B: with the flag off, nothing from /swing/execute reaches a non-dry-run branch
# =======================================================================================
class TestTheFlagOffNeverReachesANonDryRunAdapter:
    @pytest.mark.parametrize("dry_run", [True, False], ids=["DRY_RUN=true", "DRY_RUN=false"])
    def test_every_gateway_call_through_the_route_ran_dry(self, scenario, monkeypatch, tmp_path,
                                                          spy, dry_run):
        """A buy (LIMIT + GTT), a partial sell (MARKET + cancel + GTT), a raised stop (cancel +
        GTT) and a re-arm — through `POST /swing/execute` and `/swing/rearm`, under the flag
        false — and every one of the gateway's methods saw `dry_run=True`, with `DRY_RUN`
        itself set both ways."""
        monkeypatch.setattr(C, "SWING_EXECUTION_ENABLED", False)
        monkeypatch.setattr(C, "DRY_RUN", dry_run)
        assert X.swing_gates().dry_run is True
        ids = scenario.morning()
        # The scenario's held position rests on "G-44"; an integer id is what a real trigger
        # carries, and only an integer id is handed to the gateway's `delete_gtt` (SW7.1) —
        # so this is what makes the cancel path go through the gateway rather than around it.
        scenario.conn.execute("UPDATE sw_position SET gtt_id = '4242' WHERE id = ?",
                              (ids["held"],))
        client = _client(scenario, monkeypatch, tmp_path, spy)

        buy = _confirm(client, ids["signal_plan"][1], ids["trigger_line"])
        sell = _confirm(client, ids["morning"][1], ids["sell"])
        raise_ = _confirm(client, ids["morning"][1], ids["raise"])
        rearm = client.post("/swing/rearm", data={"position_id": ids["naked"],
                                                   "confirm": "true"}).json()
        for outcome in (buy, sell, raise_, rearm):
            assert outcome["status"] == "SIMULATED", outcome
            assert outcome["simulated"] is True, outcome
        assert buy["order"]["status"] == "DRY_RUN" and buy["gtt"]["status"] == "DRY_RUN_GTT"

        methods = [method for method, _ in spy.seen]
        assert methods.count("place") == 2, methods            # the buy and the sell
        assert methods.count("place_gtt_stop") == 4, methods   # buy, sell remainder, raise, re-arm
        # The sell cancels 4242 through the gateway; by the raise the resting id is the
        # simulated `DRY-…` the sell armed, which is recorded as cancelled without a call.
        assert methods.count("delete_gtt") == 1, methods
        assert all(dry for _, dry in spy.seen), _why(f"a call ran live: {spy.seen}")

        events = spy.events()
        assert events, "the gateway journalled nothing — the calls did not go through it"
        assert set(events) <= DRY_EVENTS, f"non-dry events in the journal: {events}"
        assert not set(events) & BROKER_EVENTS

        s = scenario.store()
        for pid in (buy["position_id"], ids["held"], ids["naked"]):
            assert s.position(pid)["simulated"] is True
        assert all(fill["simulated"] is True for fill in s.fills_for(buy["position_id"]))

    def test_the_gate_truth_table_has_exactly_one_live_cell(self, monkeypatch):
        """`swing_gates().dry_run` is false only when BOTH the desk's DRY_RUN is off AND the
        swing flag is on — the cell this run never sets (`02` §3)."""
        table = {}
        for flag in (False, True):
            for dry in (True, False):
                monkeypatch.setattr(C, "SWING_EXECUTION_ENABLED", flag)
                monkeypatch.setattr(C, "DRY_RUN", dry)
                table[(flag, dry)] = X.swing_gates().dry_run
        assert table == {(False, True): True, (False, False): True, (True, True): True,
                         (True, False): False}

    def test_the_spy_is_not_blind_the_live_cell_is_stopped_by_the_fake(self, scenario, monkeypatch,
                                                                       tmp_path, spy):
        """Under the one live cell the spy records `dry_run=False` and the exploding broker
        client is what refuses the order: `REJECTED`, no position, no stop. This is the proof
        that the dry-run branch is what the flag selects, not that the fake never answers."""
        monkeypatch.setattr(C, "SWING_EXECUTION_ENABLED", True)
        monkeypatch.setattr(C, "DRY_RUN", False)
        ids = scenario.morning()
        client = _client(scenario, monkeypatch, tmp_path, spy)
        out = _confirm(client, ids["signal_plan"][1], ids["trigger_line"])
        assert out["status"] == "REJECTED" and out["simulated"] is False
        assert out["position_id"] is None and out["gtt"] is None
        assert spy.seen == [("place", False)]
        assert "error" in spy.events()          # the fake's AssertionError, journalled
        assert scenario.store().open_position_for(1) is None
        assert scenario.store().line(ids["trigger_line"])["state"] == "REJECTED"

    def test_the_route_builds_no_gateway_at_import_and_resolves_the_flag_per_order(self):
        """The gateway is handed a CALLABLE for its gates (`swing_gates`), read at the moment
        of the order, so a flag flipped mid-session is the next order's, not the next restart's."""
        assert swing_desk._swing_gateway is None
        gw = X.build_swing_gateway(desk_tests.ExplodingKC(), RiskManager())
        assert gw._gates is X.swing_gates


# =======================================================================================
# Track C §3: the monitor's strategy has no place call
# =======================================================================================
def _code_only(source: str) -> str:
    """The module with docstrings removed, so the sentence "SW10 scans this file for `place`"
    in the strategy's own docstring is not what fails the scan."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(
                getattr(body[0], "value", None), ast.Constant
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


#: What a strategy that never places must not name in its code. The runner may name the Kite
#: wrapper — it READS quotes and candles through it — so it gets the shorter list.
PLACING = (r"\bplace\b", r"place_order", r"place_gtt", r"\.place\(", r"delete_gtt",
           r"OrderGateway", r"\border\b")
STRATEGY_ONLY = (r"\bself\.gw\b", r"\bkc\.", r"kiteconnect", r"kite_client")


class TestTheMonitorStrategyHasNoPlaceCall:
    @pytest.mark.parametrize("pattern", PLACING + STRATEGY_ONLY)
    def test_the_strategy_code_never_names(self, pattern):
        code = _code_only(inspect.getsource(swing_breakout))
        assert not re.search(pattern, code), f"swing_breakout.py names {pattern}"

    @pytest.mark.parametrize("pattern", PLACING)
    def test_the_runner_code_never_names(self, pattern):
        code = _code_only(inspect.getsource(swing_monitor))
        assert not re.search(pattern, code), f"swing_monitor.py names {pattern}"

    def test_the_runner_hands_the_strategy_no_gateway_and_builds_nothing_with_the_flag_off(self):
        code = _code_only(inspect.getsource(swing_monitor.main))
        assert "gateway=None" in code
        assert swing_monitor.build_monitor(enabled=False, gateway=None, watchlist=[], store=None,
                                           candles=None, day=dt.date(2026, 9, 2)) is None

    def test_generate_targets_is_empty_at_runtime(self):
        strategy = swing_breakout.SwingBreakout(None, watchlist=[], store=None, candles=None,
                                                day=dt.date(2026, 9, 2))
        assert asyncio.run(strategy.generate_targets({"anything": True})) == []
        assert strategy.gw is None

    def test_the_scan_is_over_code_not_prose(self):
        """The strategy's docstring says the word; the scan must not read it."""
        assert re.search(r"\bplace\b", inspect.getsource(swing_breakout))
        assert not re.search(r"\bplace\b", _code_only(inspect.getsource(swing_breakout)))


# =======================================================================================
# Track C §6: every sw_ write in the desk carries the sole user id
# =======================================================================================
#: `INSERT INTO <table>` / `UPDATE <table>` where the table is a literal or an f-string
#: expression such as `{self.t('sw_position')}` / `{SCHEMA}.sw_signal`.
_SW_WRITE = re.compile(r"\b(INSERT\s+INTO|UPDATE)\s+((?:\{[^}]*\})?[\w.]*)", re.I)

#: The one statement allowed to address a row without `user_id` in it, and why: the row was
#: inserted with the store's own `user_id` four lines earlier, and this UPDATE addresses it by
#: the id that INSERT returned. A `user_id` predicate here would only re-check what the same
#: connection just wrote.
WHITELISTED_UPDATES = {
    "swing_monitor.raise_signal": "UPDATE {SCHEMA}.sw_signal SET plan_line_id = ? WHERE id = ?",
}


def _literal(node: ast.expr) -> str | None:
    """A string constant, or an f-string with its expressions rendered as `{expr}`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            elif isinstance(v, ast.FormattedValue):
                parts.append("{" + ast.unparse(v.value) + "}")
        return "".join(parts)
    return None


def _sql_statements(module) -> list[tuple[str, str, str]]:
    """(function, statement text, function code) for every sw_ INSERT / UPDATE in the module."""
    tree = ast.parse(inspect.getsource(module))
    found: list[tuple[str, str, str]] = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(function):
            if not isinstance(call, ast.Call):
                continue
            # Implicitly concatenated string parts arrive as ONE Constant / JoinedStr, so the
            # whole statement is one argument; a `+`-built statement would not be seen, and
            # none of the three modules builds one (the census test counts what is seen).
            for arg in call.args:
                text = _literal(arg) or ""
                match = _SW_WRITE.search(text) if text else None
                if match and "sw_" in match.group(2):
                    found.append((f"{module.__name__.split('.')[-1]}.{function.name}",
                                  " ".join(text.split()), ast.unparse(function)))
    return found


def _carries_user_id(statement: str, function_code: str) -> bool:
    match = _SW_WRITE.search(statement)
    assert match is not None
    if match.group(1).upper().startswith("INSERT"):
        # The column list sits between the table (and an optional alias) and VALUES.
        columns = statement[match.end():].split("VALUES", 1)[0]
        if "{" in columns:
            # A dynamic column list (`create_position`, `add_fill`): the function must build it
            # with `user_id` first.
            return "'user_id'" in function_code or '"user_id"' in function_code
        return "user_id" in columns
    return re.search(r"WHERE\b.*\buser_id\s*=\s*\?", statement, re.I | re.S) is not None


class TestEverySwWriteCarriesTheSoleUserId:
    def test_every_insert_and_update_names_user_id(self):
        statements = []
        for module in (swing_desk, swing_monitor, X):
            statements.extend(_sql_statements(module))
        assert len(statements) >= 8, f"the scan found only {len(statements)} writes: {statements}"
        offenders = []
        for where, statement, code in statements:
            if WHITELISTED_UPDATES.get(where) == statement:
                continue
            if not _carries_user_id(statement, code):
                offenders.append(f"{where}: {statement}")
        assert offenders == [], f"sw_ writes without the user: {offenders}"

    def test_the_whitelist_is_still_real(self):
        """A stale whitelist is a hole nobody is watching."""
        statements = {(w, s) for w, s, _ in _sql_statements(swing_monitor)}
        for where, statement in WHITELISTED_UPDATES.items():
            assert (where, statement) in statements, f"{where} no longer issues {statement!r}"

    def test_the_scan_sees_the_dynamic_inserts(self):
        """`create_position` and `add_fill` build their column lists at runtime; the census
        must contain them or the scan is reading the wrong shape."""
        where = {w for w, _, _ in _sql_statements(swing_desk)}
        assert {"swing_desk.create_position", "swing_desk.add_fill", "swing_desk.bump_session",
                "swing_desk.set_line", "swing_desk.update_position"} <= where
        assert {"swing_monitor.raise_signal", "swing_monitor._plan_for",
                "swing_monitor.record_monitor_ran"} <= {w for w, _, _ in _sql_statements(swing_monitor)}

    def test_the_user_id_is_the_sole_tenant_and_never_a_literal(self):
        desk_code = _code_only(inspect.getsource(swing_desk))
        assert "user_id=C.SOLE_USER_ID" in desk_code.replace(" ", "")
        monitor_code = _code_only(inspect.getsource(swing_monitor))
        assert "BASKFY_SOLE_USER_ID" in monitor_code
        exec_code = _code_only(inspect.getsource(X))
        assert "C.SOLE_USER_ID" in exec_code and "C.SOLE_BROKER_ACCOUNT_ID" in exec_code
        for module in (swing_desk, swing_monitor, X):
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if isinstance(node, ast.keyword) and node.arg == "user_id":
                    assert not isinstance(node.value, ast.Constant), (
                        f"{module.__name__}:{node.value.lineno} passes a literal user_id")

    def test_after_a_full_morning_every_sw_row_belongs_to_the_sole_user(self, scenario,
                                                                          monkeypatch, tmp_path,
                                                                          spy):
        """Runtime, over the sqlite twin: a buy, a sell, a raise and a re-arm through the real
        module, then every `sw_` table is read for a row that is not the user's."""
        monkeypatch.setattr(C, "SWING_EXECUTION_ENABLED", False)
        monkeypatch.setattr(C, "DRY_RUN", True)
        ids = scenario.morning()
        client = _client(scenario, monkeypatch, tmp_path, spy)
        _confirm(client, ids["signal_plan"][1], ids["trigger_line"])
        _confirm(client, ids["morning"][1], ids["sell"])
        _confirm(client, ids["morning"][1], ids["raise"])
        client.post("/swing/rearm", data={"position_id": ids["naked"], "confirm": "true"})
        conn = desk_tests._connect(scenario.path)
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sw_%'")]
        assert len(tables) >= 9
        for table in tables:
            foreign = conn.execute(f"SELECT count(*) AS n FROM {table} WHERE user_id != ?",  # noqa: S608
                                   (USER,)).fetchone()["n"]
            assert foreign == 0, f"{table} holds {foreign} rows of another user"
        written = {t: conn.execute(f"SELECT count(*) AS n FROM {t} WHERE user_id = ?",  # noqa: S608
                                   (USER,)).fetchone()["n"]
                   for t in ("sw_position", "sw_fill", "sw_session", "sw_plan_line")}
        assert written["sw_position"] == 3 and written["sw_fill"] == 2
        assert written["sw_session"] == 1 and written["sw_plan_line"] >= 4
        # And another tenant's store, over the same file, sees none of it.
        other = swing_desk.PgSwingStore(desk_tests._connect(scenario.path), user_id=USER + 1,
                                        schema="")
        assert other.open_positions() == [] and other.session(desk_tests.TODAY) is None


# =======================================================================================
# Track C §5 and 04 §6.5 at the desk: execute_line's guards, as seeded property loops
# =======================================================================================
def _sell_store(open_qty: int, entered: int, sell_qty: int, *, present: bool):
    store = exec_tests.MemoryStore()
    plan_id = store.add_plan()
    if present:
        store.add_position(quantity_entered=entered, quantity_open=open_qty, gtt_id="DRY-x",
                           state="OPEN" if open_qty == entered else "PARTIAL")
    line_id = store.add_line(plan_id, kind="SELL_AT_OPEN", quantity=sell_qty, trigger=None,
                             stop=None, note="PARTIAL_INTO_STRENGTH")
    return store, plan_id, line_id


@pytest.fixture()
def fast_gw(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "SWING_EXECUTION_ENABLED", False)
    monkeypatch.setattr(C, "DRY_RUN", True)
    monkeypatch.chdir(tmp_path)
    return _unthrottled(X.build_swing_gateway(desk_tests.ExplodingKC(), RiskManager()))


class TestASellNeverExceedsWhatTheSleeveOwns:
    def test_sell_is_blocked_exactly_when_it_exceeds_quantity_open_over_500_cases(self, fast_gw):
        """Random books and random SELL lines, through the real dry-run gateway: `BLOCKED` iff
        the line asks for more than is open (or nothing, or a name the sleeve does not hold);
        otherwise a fill for exactly the line's quantity and a book that never goes negative."""
        rng = random.Random(SEED)
        blocked = filled = 0
        for case in range(CASES):
            entered = rng.randint(1, 5000)
            open_qty = rng.randint(1, entered)
            sell_qty = rng.choice([rng.randint(-5, 0), rng.randint(1, open_qty),
                                   rng.randint(open_qty + 1, 2 * open_qty + 5)])
            present = rng.random() > 0.1
            store, plan_id, line_id = _sell_store(open_qty, entered, sell_qty, present=present)
            out = exec_tests.execute(store, fast_gw, plan_id, line_id, last_price=D("104.00"))
            should_block = (not present) or sell_qty <= 0 or sell_qty > open_qty
            if should_block:
                blocked += 1
                assert out.status == "BLOCKED", _why(f"case {case}: {sell_qty} of {open_qty} open "
                                                     f"(present={present}) → {out}")
                assert store.fills == [], _why(f"case {case}: a blocked sell wrote a fill")
                if present:
                    assert store.positions[1]["quantity_open"] == open_qty
            else:
                filled += 1
                assert out.status == "SIMULATED", _why(f"case {case}: {out}")
                (fill,) = store.fills
                assert fill["quantity"] == sell_qty and fill["side"] == "SELL"
                assert store.positions[1]["quantity_open"] == open_qty - sell_qty >= 0
        assert blocked > 50 and filled > 50, _why(f"the loop was lopsided: {blocked}/{filled}")

    def test_a_sell_never_consults_the_brokers_holdings(self):
        """Track C §5 says the book, not the broker, is the source of truth; the code that
        decides a SELL reads `open_position_for` and names no holdings call."""
        code = _code_only(inspect.getsource(X._sell))
        assert "open_position_for" in code
        for word in ("holdings", "kc.", "positions()"):
            assert word not in code


class TestAStopNeverFallsAtTheDesk:
    def test_stop_never_falls_a_raise_at_or_below_the_resting_stop_is_blocked_over_500_cases(
        self, fast_gw
    ):
        """`04` §6.5 at the desk: a RAISE line to any level at or below the resting stop is
        `BLOCKED` before the cancel, and the trigger is untouched; a level above it and below
        the last price re-arms at exactly that level."""
        rng = random.Random(SEED)
        blocked = raised = 0
        for case in range(CASES):
            entry = D(rng.randint(2000, 100000)) / 20        # ₹100–5000 on the tick
            resting = (entry * (1 - D(rng.randint(5, 1000)) / 10000)).quantize(D("0.05"))
            if resting >= entry:
                resting = entry - D("0.05")
            last = entry * (1 + D(rng.randint(0, 3000)) / 10000)
            new_stop = rng.choice([
                resting - D(rng.randint(0, 2000)) / 20,      # below
                resting,                                     # equal
                resting + D(rng.randint(1, 4000)) / 20,      # above
            ]).quantize(D("0.05"))
            store = exec_tests.MemoryStore()
            plan_id = store.add_plan()
            store.add_position(entry_avg=entry, initial_stop=resting, stop=resting,
                               gtt_trigger=resting, gtt_id="DRY-old")
            line_id = store.add_line(plan_id, kind="RAISE_GTT_STOP", quantity=0, trigger=None,
                                     stop=new_stop, note="BREAKEVEN_AT_R")
            out = exec_tests.execute(store, fast_gw, plan_id, line_id, last_price=last)
            pos = store.positions[1]
            if new_stop <= resting:
                blocked += 1
                assert out.status == "BLOCKED", _why(f"case {case}: {new_stop} vs {resting}: {out}")
                assert "never falls" in out.reason
                assert pos["stop"] == resting and pos["gtt_id"] == "DRY-old", _why(
                    f"case {case}: a blocked raise touched the position")
            elif new_stop >= last:
                assert out.status == "BLOCKED" and pos["stop"] == resting
            else:
                raised += 1
                assert out.status == "SIMULATED", _why(f"case {case}: {out}")
                assert pos["stop"] == new_stop > resting
                assert pos["gtt_trigger"] == new_stop and pos["gtt_id"] != "DRY-old"
        assert blocked > 100 and raised > 50, _why(f"lopsided: {blocked}/{raised}")


# =======================================================================================
# Track C §1/§2: CNC only, no leverage, no intraday, no options
# =======================================================================================
class TestTheBookIsCncOnly:
    def test_every_order_the_module_sends_is_cnc_on_nse_without_a_variety(self):
        tree = ast.parse(inspect.getsource(X))
        places = [node for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "place"]
        assert len(places) == 2, "the buy and the sell"
        for call in places:
            kw = {k.arg: k.value for k in call.keywords}
            assert isinstance(kw["product"], ast.Constant) and kw["product"].value == "CNC"
            assert isinstance(kw["exchange"], ast.Constant) and kw["exchange"].value == "NSE"
            assert isinstance(kw["order_type"], ast.Constant)
            assert kw["order_type"].value in ("LIMIT", "MARKET")
            assert "variety" not in kw
        code = _code_only(inspect.getsource(X))
        for word in ('"MIS"', '"NFO"', '"BFO"', '"co"', '"bo"', "MTF"):
            assert word not in code

    def test_the_gates_never_allow_intraday_or_options_whatever_the_weekly_desk_says(self,
                                                                                      monkeypatch):
        monkeypatch.setattr(C, "INTRADAY_ENABLED", True)
        monkeypatch.setattr(C, "OPTIONS_ENABLED", True)
        gates = X.swing_gates()
        assert gates.intraday_enabled is False and gates.options_enabled is False

    def test_an_mis_order_would_be_blocked_by_the_gateway_itself(self, fast_gw):
        """Belt and braces: even a hand-edited call with `product="MIS"` is refused inside
        the gateway under the swing gates, before any network call."""
        from baskfy_execution.tenancy import TenantIds

        sole = TenantIds(user_id=1, broker_account_id=1)
        out = asyncio.run(fast_gw.place(symbol="ALPHA", qty=1, side="BUY", product="MIS",
                                        order_type="MARKET", exchange="NSE", client_id="x:MIS",
                                        tenant=sole, plan_tenant=sole))
        assert out["status"] == "BLOCKED" and "MIS" in out["error"]


# =======================================================================================
# STANDING-ANSWERS A5 (SW10.4): no sequence of confirms exceeds the ceiling, the count or
# the cap — a seeded property over the real dry-run gateway and the in-memory store
# =======================================================================================
TIERS = exec_tests.TIERS


def _random_book(rng: random.Random, store, n: int) -> None:
    """`n` positions already on the book, at random costs — possibly already over the rung's
    ceiling (a rung stepped down overnight), which is a book the gate must not add to."""
    for i in range(n):
        entry = D(rng.randint(50, 900))
        qty = rng.randint(10, 400)
        store.add_position(symbol=f"HELD{i}", instrument_id=100 + i, entry_avg=entry,
                           quantity_entered=qty, quantity_open=qty,
                           initial_stop=entry - D(2), stop=entry - D(2))


def _random_line(rng: random.Random, store, plan_id: str, i: int) -> int:
    """A BUY line the way a SIGNAL plan might have written it — sized by *some* earlier
    reading, so its quantity may be anything from a sliver to far too much."""
    trigger = D(rng.randint(2000, 90000)) / 100
    adr = D(rng.randint(400, 1000)) / 100                      # 4-10 % ADR names
    distance_pct = D(rng.randint(50, int(adr * 100))) / 100    # a stop inside one ADR
    stop = (trigger * (1 - distance_pct / 100)).quantize(D("0.05"))
    symbol = f"LINE{i}"
    store.detected[symbol] = (adr, D(rng.choice([50, 100, 500]) * 1_000_000), D(rng.randint(40, 95)))
    quantity = rng.choice([rng.randint(1, 50), rng.randint(50, 3000), rng.randint(3000, 20000)])
    return store.add_line(plan_id, symbol=symbol, instrument_id=200 + i, quantity=quantity,
                          trigger=trigger, stop=stop, risk_inr=(trigger - stop) * quantity,
                          position_value=trigger * quantity)


def _book(store) -> tuple[Decimal, int]:
    held = [p for p in store.positions.values() if p["state"] != "CLOSED" and p["quantity_open"] > 0]
    return (sum((D(str(p["entry_avg"])) * p["quantity_open"] for p in held), D(0)), len(held))


class TestNoSequenceOfConfirmsExceedsTheCeilingTheCountOrTheCap:
    def test_a_sequence_of_confirms_never_exceeds_ceiling_count_or_cap_over_500_sequences(
        self, monkeypatch, tmp_path
    ):
        """500 sequences of 1–8 confirms, each on a fresh sleeve (₹2–50 lakh), a random rung,
        a random cap, 0–3 positions already held, and lines sized by nobody in particular —
        through `execute_line` and the real dry-run gateway (unthrottled). After every confirm
        that went through: the book at cost is inside the rung's ceiling, the count is inside
        `min(rung, max_open_positions)`, the sequence has taken at most three entries, and
        the size sent is at most the line's own. After every refusal: the book is exactly what
        it was, the reason leads with a skip code, and nothing was journalled."""
        monkeypatch.setattr(C, "SWING_EXECUTION_ENABLED", False)
        monkeypatch.setattr(C, "DRY_RUN", True)
        monkeypatch.chdir(tmp_path)
        rng = random.Random(SEED)
        codes = {"EXPOSURE_FULL", "TIER_FULL", "SESSION_CAP", "SIZE_REFUSED", "ALREADY_HELD",
                 "GATE_RED", "DRAWDOWN_LOCKOUT", "NOT_TRADEABLE_SETUP"}
        seen: dict[str, int] = {"sized": 0, "re-sized": 0, **{c: 0 for c in codes}}
        journalled = 0   # every case's gateway writes the same journal file; read the tail
        for case in range(CASES):
            rung = rng.randint(0, 3)
            cap = rng.randint(1, 10)
            capital = D(rng.randint(2, 50)) * 100_000
            store = exec_tests.MemoryStore(rung=rung, capital=capital)
            store._config["max_open_positions"] = cap
            store._config["risk_per_trade_pct"] = D(rng.choice(["0.250", "0.500", "1.000"]))
            _random_book(rng, store, rng.randint(0, 3))
            gw = _unthrottled(X.build_swing_gateway(desk_tests.ExplodingKC(), RiskManager()))
            plan_id = store.add_plan()
            lines = [_random_line(rng, store, plan_id, i) for i in range(rng.randint(1, 8))]
            count_limit, ceiling_pct = TIERS[rung]
            ceiling = capital * D(str(ceiling_pct)) / 100
            allowed_count = min(count_limit, cap)
            entries = 0
            why = f"seed {SEED} case {case}: rung {rung} cap {cap} sleeve {capital}"
            for line_id in lines:
                before, count_before = _book(store)
                planned = store.lines[line_id]["quantity"]
                out = exec_tests.execute(store, gw, plan_id, line_id)
                after, count_after = _book(store)
                if out.status == "SIMULATED":
                    entries += 1
                    sent = store.lines[line_id]["quantity"]
                    assert after <= ceiling, f"{why}: book {after} over the ceiling {ceiling}"
                    assert count_after <= allowed_count, f"{why}: {count_after} > {allowed_count}"
                    assert entries <= 3, f"{why}: a fourth entry went out"
                    assert sent <= planned and sent > 0, f"{why}: sent {sent} of {planned}"
                    assert store.positions[out.position_id]["quantity_open"] == sent
                    assert store.fills[-1]["quantity"] == sent and out.gtt["qty"] == sent
                    seen["re-sized" if sent < planned else "sized"] += 1
                else:
                    assert out.status == "BLOCKED", f"{why}: {out}"
                    code = out.reason.split(":", 1)[0]
                    assert code in codes, f"{why}: {out.reason}"
                    seen[code] += 1
                    assert (after, count_after) == (before, count_before), f"{why}: refused, moved"
                    assert store.lines[line_id]["state"] == "REJECTED"
                    assert store.lines[line_id]["quantity"] == planned
            assert entries <= 3, why
            assert store.locks == [NOW.date()] * len(lines), f"{why}: a confirm skipped the lock"
            events = exec_tests.journal_events(gw)[journalled:]
            journalled += len(events)
            assert set(events) <= DRY_EVENTS and not set(events) & BROKER_EVENTS, f"{why}: {events}"
            # a stop closer than the band's 0.5 % floor is a warning beside the GTT, not an order
            sent_events = [e for e in events if e != "gtt_band_warning"]
            assert sent_events == ["dry_run", "gtt_dry_run"] * entries, f"{why}: {events}"
        # The property is only worth what it exercised: every refusal the gate can give and
        # both sizing outcomes must have occurred across the 500 sequences.
        print(f"confirm sequences over {CASES} cases: {seen}")
        assert seen["sized"] > 0 and seen["re-sized"] > 0, seen
        for code in ("EXPOSURE_FULL", "TIER_FULL", "SESSION_CAP", "SIZE_REFUSED"):
            assert seen[code] > 0, f"{code} never occurred: {seen}"

    def test_two_tabs_confirming_the_same_line_send_it_once(self, fast_gw):
        """The lock's race, in miniature: both requests validated the line as PROPOSED; the
        one that holds the lock second re-reads it and answers 409 — one order, one position."""
        from fastapi import HTTPException

        store = exec_tests.MemoryStore(rung=3)
        plan_id = store.add_plan()
        line_id = store.add_line(plan_id, quantity=100)
        first = exec_tests.execute(store, fast_gw, plan_id, line_id)
        assert first.status == "SIMULATED"
        with pytest.raises(HTTPException) as second:
            exec_tests.execute(store, fast_gw, plan_id, line_id)
        assert second.value.status_code == 409
        assert exec_tests.sent_quantities(fast_gw) == [100] and len(store.positions) == 1
