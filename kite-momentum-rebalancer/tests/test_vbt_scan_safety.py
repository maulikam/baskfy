"""The one non-negotiable behind "Scan now" on the VBT page, as a property.

``PLAN-SCAN-SYNC.md`` rule 3: *"No leaf places an order, and nothing new may reach
``OrderGateway.place``. A scan queues a detector; it is money-free by construction and must stay
that way, with a test that says so."* This is that test, in the shape
``packages/core/tests/test_twt_safety_properties.py`` set.

The property is one sentence:

    **No route that asks for a detection can reach a broker — whatever the flags say.**

The clause that matters is the last one. ``tests/test_vbt_safety.py`` pins
``VBT_EXECUTION_ENABLED=False`` and ``DRY_RUN=True``, so its tests pass for two reasons and
cannot tell you which did the work. Here both switches are set the *other* way — the desk live,
the sleeve enabled — so nothing but the shape of the code is standing between a scan and an
order. If those flags flipped for real tomorrow, this file would still hold.

**Every scan route, discovered rather than listed.** A property over a hard-coded list stops
being a property the moment somebody adds the third route. The routes are parsed out of
``app/vbt_desk.py`` with ``ast``, and :class:`TestTheScanSurfaceIsWhatWeThinkItIs` fails when the
set changes — which makes adding one a deliberate act with a failing test on it.

**Why the gateway is a landmine rather than a mock.** ``ExplodingGateway`` raises on *any*
attribute, not only on ``place``: a path that reached for ``.kite``, ``.risk`` or a journal on
its way to an order would be caught at the first touch rather than at the last. The scan routes
never touch it at all, which is the claim.
"""

from __future__ import annotations

import ast
import contextlib
import datetime as dt
import sqlite3
import sys
import textwrap
from pathlib import Path
from typing import Final

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_vbt_desk import (  # noqa: E402 - the sys.path insert above has to come first
    DDL,
    NOW,
)

from app import config as C  # noqa: E402
from app import vbt_desk as W  # noqa: E402

DESK: Final = Path(__file__).resolve().parents[1]

#: Every route on the desk that asks for a detection. Two names, one request: `/vbt/rescan` is
#: what the page's own form has always posted, `/vbt/scan` is the shape `PLAN-SCAN-SYNC.md` fixed
#: across the sleeves. `GET /vbt/scan/{run_id}` is the poll and is covered here too, because a
#: read that could place would be the quietest hole of the three.
EXPECTED_SCAN_ROUTES: Final[set[tuple[str, str]]] = {
    ("POST", "/vbt/rescan"),
    ("POST", "/vbt/scan"),
    ("GET", "/vbt/scan/{run_id}"),
}

#: What a money path looks like, whatever it is spelled. Checked against the handlers' code with
#: docstrings stripped — the docstrings explain at length what these routes may not do.
ORDER_WORDS: Final[tuple[str, ...]] = (
    "execute_line",
    "gateway",
    "place",
    "place_order",
    "place_gtt",
    "delete_gtt",
    "kc.",
    "kiteconnect",
    "gtt",
    "ordergateway",
)

#: The tables a scan must leave alone. `vb_scan_run` is the one it may write.
MONEY_TABLES: Final[tuple[str, ...]] = (
    "vb_plan",
    "vb_plan_line",
    "vb_order",
    "vb_position",
    "vb_fill",
)


class ExplodingGateway:
    """A landmine where the gateway sits. Any attribute at all is a failure."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"a scan route reached the gateway: .{name}")


class ExplodingKC:
    """A landmine where the broker client sits, for the same reason."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"a scan route reached the broker: kc.{name}")


def _strip_prose(source: str) -> str:
    """The handler's code without its docstring. Dedented, because a method's source is not."""
    tree = ast.parse(textwrap.dedent(source))
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    if (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        node.body = node.body[1:]
    return ast.unparse(node)


@pytest.fixture
def store(tmp_path: Path) -> W.PgVbtStore:
    """A sqlite twin, reachable from FastAPI's worker thread (Postgres has no such rule)."""
    conn = sqlite3.connect(tmp_path / "vbt.db", isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    return W.PgVbtStore(conn, user_id=1, schema="", broker_account_id=1)


@pytest.fixture
def live_desk_client(store: W.PgVbtStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The desk with **both** switches set the dangerous way, and a landmine for a gateway.

    ``DRY_RUN=False`` and ``VBT_EXECUTION_ENABLED=True`` is the configuration this sleeve will
    one day run in. Every assertion below holds in it, so none of them is passing because a flag
    happened to be off.
    """
    from app import main as M

    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "VBT_EXECUTION_ENABLED", True)
    monkeypatch.setattr(W, "_vbt_gateway", ExplodingGateway())
    monkeypatch.setattr(M, "_kite", ExplodingKC())
    monkeypatch.setattr(W, "open_store", lambda: contextlib.nullcontext(store))
    monkeypatch.setattr(W, "_now", lambda: NOW)
    return TestClient(M.app)


class TestTheScanSurfaceIsWhatWeThinkItIs:
    """The enumeration below is what "every scan route" means in this file."""

    def _scan_routes(self) -> set[tuple[str, str]]:
        found: set[tuple[str, str]] = set()
        tree = ast.parse((DESK / "app" / "vbt_desk.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call) or not isinstance(deco.func, ast.Attribute):
                    continue
                value = deco.func.value
                if not isinstance(value, ast.Name) or value.id != "router":
                    continue
                if not deco.args or not isinstance(deco.args[0], ast.Constant):
                    continue
                path = str(deco.args[0].value)
                if "scan" in path or "rescan" in path:
                    found.add((deco.func.attr.upper(), path))
        return found

    def test_the_desk_mounts_exactly_the_scan_routes_this_file_covers(self) -> None:
        assert self._scan_routes() == EXPECTED_SCAN_ROUTES

    def test_the_route_scan_would_notice_a_new_one(self) -> None:
        """Non-vacuity: the parser finds a route in source shaped like a real one."""
        tree = ast.parse('@router.post("/vbt/scan/confirm")\ndef go() -> None:\n    ...\n')
        node = tree.body[0]
        assert isinstance(node, ast.FunctionDef)
        deco = node.decorator_list[0]
        assert isinstance(deco, ast.Call)
        assert isinstance(deco.func, ast.Attribute)
        assert deco.func.attr == "post"
        assert isinstance(deco.args[0], ast.Constant)
        assert "scan" in str(deco.args[0].value)

    def test_exactly_one_desk_route_can_still_reach_an_order(self) -> None:
        """The whole point of counting: `/vbt/execute` is the one doorway to the gateway, and
        the scan routes are not a second one. `02` Track C §3."""
        source = (DESK / "app" / "vbt_desk.py").read_text(encoding="utf-8")
        assert source.count("_execute.execute_line(") == 1
        assert source.count('@router.post("/vbt/execute"') == 1


class TestNoScanRouteReachesABrokerWithTheFlagsLive:
    """The property, exercised. Each route is called for real against a landmine gateway."""

    def test_the_contract_scan_places_nothing(
        self, live_desk_client: TestClient, store: W.PgVbtStore
    ) -> None:
        response = live_desk_client.post("/vbt/scan")
        assert response.status_code == 202, response.text
        self._nothing_but_the_scan_row(store)

    def test_the_older_rescan_name_places_nothing(
        self, live_desk_client: TestClient, store: W.PgVbtStore
    ) -> None:
        response = live_desk_client.post("/vbt/rescan")
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] is True
        self._nothing_but_the_scan_row(store)

    def test_the_poll_places_nothing(
        self, live_desk_client: TestClient, store: W.PgVbtStore
    ) -> None:
        run_id = store.request_scan(now=NOW - dt.timedelta(minutes=5))
        assert live_desk_client.get(f"/vbt/scan/{run_id}").status_code == 200
        self._nothing_but_the_scan_row(store)

    def test_both_refusals_place_nothing_either(
        self, live_desk_client: TestClient, store: W.PgVbtStore
    ) -> None:
        """The refusal paths are paths too, and a 409 or a 429 is still a code path that ran."""
        assert live_desk_client.post("/vbt/scan").status_code == 202
        assert live_desk_client.post("/vbt/scan").status_code == 409
        store.conn.execute("UPDATE vb_scan_run SET status = 'DONE'")
        assert live_desk_client.post("/vbt/scan").status_code == 429
        self._nothing_but_the_scan_row(store)

    def test_the_landmine_is_real(self, live_desk_client: TestClient) -> None:
        """Non-vacuity: the gateway this suite hands the desk does explode when touched."""
        with pytest.raises(AssertionError, match="reached the gateway"):
            W._vbt_gateway.place()  # noqa: SLF001 - reaching for it is the point

    def _nothing_but_the_scan_row(self, store: W.PgVbtStore) -> None:
        for table in MONEY_TABLES:
            count = store.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            assert count == 0, f"a scan wrote to {table}"


class TestNoScanRouteEvenNamesAnOrder:
    """Structural, so a path that could place is caught before anybody runs it."""

    @pytest.mark.parametrize(
        "handler",
        [W.vbt_scan_now, W.vbt_scan_run, W.vbt_rescan],
        ids=["post_scan", "get_scan", "post_rescan"],
    )
    def test_the_handler_names_no_order_verb(self, handler: object) -> None:
        import inspect

        code = _strip_prose(inspect.getsource(handler)).lower()
        for word in ORDER_WORDS:
            assert word not in code, f"{handler.__name__} names {word}"

    def test_the_store_method_the_scan_calls_writes_one_table(self) -> None:
        """`request_scan` is the one write behind all three routes. If it could write a plan
        line or an order, the routes' own innocence would be worth nothing."""
        import inspect

        code = _strip_prose(inspect.getsource(W.PgVbtStore.request_scan))
        assert code.count("INSERT INTO") == 1
        assert "vb_scan_run" in code
        for table in MONEY_TABLES:
            assert table not in code, f"request_scan names {table}"

    def test_the_poll_reads_and_does_not_write(self) -> None:
        import inspect

        code = _strip_prose(inspect.getsource(W.PgVbtStore.scan_run)).upper()
        for verb in ("INSERT", "UPDATE", "DELETE"):
            assert verb not in code, f"scan_run performs an {verb}"

    def test_there_is_no_auto_execute_flag_anywhere_near_the_scan(self) -> None:
        """Non-negotiable #1's named exception is the swing sleeve's. A scan is not a confirm,
        and adding a flag that made one into the other would have to pass here first."""
        source = (DESK / "app" / "vbt_desk.py").read_text(encoding="utf-8").upper()
        assert "AUTO_EXECUTE" not in source
