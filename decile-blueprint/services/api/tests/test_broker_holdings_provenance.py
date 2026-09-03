"""Tree-5 leaf C1 — holdings carry their provenance, so live is never labelled fixture.

The defect this file exists to pin: ``holdings_for_broker`` used to return a bare
``list[HoldingRow]``, so ``routers/brokers.py`` labelled *every* non-empty response
``"fixture holdings (DRY_RUN or BASKFY_BROKER_HOLDINGS_FIXTURE)"`` — including a genuine
live Kite fetch. A user looking at their real money was told the numbers were fake.

These tests assert the spec (house rule 2), not the old behaviour: every one of
:class:`TestLiveIsNeverLabelledFixture` fails against the pre-C1 code, because the pre-C1
code had no ``source`` to read and said "fixture" about a live fetch.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from baskfy_execution.broker_ports import HoldingRow, normalize_holding
from sqlalchemy.ext.asyncio import AsyncSession

from api_helpers import request_stub

from baskfy_api import broker_holdings
from baskfy_api.broker_holdings import (
    HOLDINGS_SOURCES,
    HoldingsResult,
    HoldingsSource,
    holdings_for_broker,
)
from baskfy_api.broker_oauth import clear_oauth_states, register_oauth_state
from baskfy_api.problems import Problem
from baskfy_api.routers import brokers as brokers_router
from baskfy_api.routers.brokers import (
    OAUTH_CALLBACK_PATH,
    SyncHoldingsOut,
    connect_broker,
    oauth_callback,
    sync_holdings,
)
from baskfy_api.settings import get_settings
from baskfy_providers.errors import AccessTokenExpired

WIRED = "zerodha"
UNWIRED = "upstox"


class _FakeToken:
    """Just enough of ``baskfy_providers.tokens.AccessToken`` for the live path."""

    value = "not-a-real-token"


class _FakeStore:
    """A connected broker session. The tests never let a real one be built."""

    def exists(self) -> bool:
        return True

    def require_fresh(self) -> _FakeToken:
        return _FakeToken()


class _ExpiredStore(_FakeStore):
    def require_fresh(self) -> _FakeToken:
        raise AccessTokenExpired()


def a_row(symbol: str = "INFY", quantity: int = 10) -> HoldingRow:
    return normalize_holding(
        symbol=symbol,
        exchange="NSE",
        quantity=quantity,
        t1_quantity=2,
        collateral_quantity=3,
        average_price="1400.50",
        last_price="1450.00",
        product="CNC",
    )


def write_fixture(tmp_path: Path, symbol: str = "TCS") -> Path:
    path = tmp_path / "holdings.json"
    path.write_text(
        json.dumps(
            [
                {
                    "symbol": symbol,
                    "exchange": "NSE",
                    "quantity": 7,
                    "t1_quantity": 1,
                    "collateral_quantity": 0,
                    "average_price": "3500.00",
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _dry_run_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Safety rail: every test starts in DRY_RUN with no fixture and no broker session."""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("BASKFY_BROKER_HOLDINGS_FIXTURE", raising=False)


@pytest.fixture
def live_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment with DRY_RUN off, an app key, and a fresh stored token."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("BASKFY_KITE_API_KEY", "test-app-key")
    monkeypatch.setattr(broker_holdings, "token_store_for", _FakeStore)


def principal_stub() -> MagicMock:
    principal = MagicMock()
    principal.require_user.return_value = 1
    return principal


def no_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop `sync_holdings` writing, for the tests that are about the response's provenance.

    M75 gave the endpoint a second job: a live read is now written into the broker's holding
    group. These tests predate that and assert what the response SAYS about where its rows came
    from — `source`, `degraded`, `note`, the quantity contract — none of which depends on the
    write. Handing them a database would make seven provenance assertions wait on Postgres to
    check a string. Persistence has its own tests in `test_broker_holdings_sync.py`, and the
    live-write path is exercised end to end there.
    """
    monkeypatch.setattr(brokers_router, "is_persistable", lambda _result: False)


class TestLiveIsNeverLabelledFixture:
    """The money-safety fix. Every test here fails against the pre-C1 code."""

    def test_live_fetch_with_rows_reports_source_live(
        self, monkeypatch: pytest.MonkeyPatch, live_session: None
    ) -> None:
        monkeypatch.setattr(
            broker_holdings, "fetch_kite_holdings", lambda **_kwargs: [a_row("RELIANCE")]
        )
        result = holdings_for_broker(WIRED)
        assert result.source == "live"
        assert result.is_live is True
        assert result.degraded is False
        assert [row.symbol for row in result.rows] == ["RELIANCE"]

    def test_live_fetch_is_live_even_when_a_fixture_is_also_configured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live_session: None
    ) -> None:
        """A fixture lying around must not re-label real money as sample data."""
        monkeypatch.setenv("BASKFY_BROKER_HOLDINGS_FIXTURE", str(write_fixture(tmp_path)))
        monkeypatch.setattr(
            broker_holdings, "fetch_kite_holdings", lambda **_kwargs: [a_row("RELIANCE")]
        )
        result = holdings_for_broker(WIRED)
        assert result.source == "live"
        assert [row.symbol for row in result.rows] == ["RELIANCE"]

    async def test_live_not_fixture_in_the_http_response(
        self, monkeypatch: pytest.MonkeyPatch, live_session: None
    ) -> None:
        """The whole defect, at the surface a user sees: the note must not say 'fixture'."""
        no_persistence(monkeypatch)
        monkeypatch.setattr(
            broker_holdings, "fetch_kite_holdings", lambda **_kwargs: [a_row("RELIANCE")]
        )
        out = await sync_holdings(principal_stub(), AsyncSession(), WIRED)
        assert out.source == "live"
        assert out.degraded is False
        assert "fixture" not in out.note.lower()
        assert "live" in out.note.lower()
        assert len(out.holdings) == 1

    def test_a_live_fetch_that_returns_nothing_is_empty_not_live(
        self, monkeypatch: pytest.MonkeyPatch, live_session: None
    ) -> None:
        """'live' must mean rows a broker reported, so a zero-row fetch cannot claim it."""
        monkeypatch.setattr(broker_holdings, "fetch_kite_holdings", lambda **_kwargs: [])
        result = holdings_for_broker(WIRED)
        assert result.source == "empty"
        assert result.rows == ()
        assert result.degraded is False


class TestUnwiredBroker:
    def test_unwired_broker_reports_unwired(self) -> None:
        result = holdings_for_broker(UNWIRED)
        assert result.source == "unwired"
        assert result.source not in ("fixture", "live")
        assert result.rows == ()
        assert result.degraded is False

    def test_unwired_broker_stays_unwired_even_with_a_fixture_configured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("BASKFY_BROKER_HOLDINGS_FIXTURE", str(write_fixture(tmp_path)))
        result = holdings_for_broker(UNWIRED)
        assert result.source == "unwired"
        assert result.rows == ()

    async def test_unwired_broker_says_so_in_the_response(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("BASKFY_BROKER_HOLDINGS_FIXTURE", str(write_fixture(tmp_path)))
        out = await sync_holdings(principal_stub(), AsyncSession(), UNWIRED)
        assert out.source == "unwired"
        assert out.holdings == []
        assert "unwired" in out.note.lower()

    async def test_an_unknown_broker_is_still_a_404_not_a_provenance_value(self) -> None:
        with pytest.raises(Problem) as caught:
            await sync_holdings(principal_stub(), AsyncSession(), "not-a-broker")
        assert caught.value.status == 404


class TestConfiguredFixture:
    def test_dry_run_with_a_fixture_reports_fixture_and_is_not_degraded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("BASKFY_BROKER_HOLDINGS_FIXTURE", str(write_fixture(tmp_path)))
        result = holdings_for_broker(WIRED)
        assert result.source == "fixture"
        assert result.degraded is False
        assert [row.symbol for row in result.rows] == ["TCS"]

    def test_no_app_key_is_a_configured_stub_not_a_degradation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setenv("BASKFY_KITE_API_KEY", "")
        monkeypatch.setenv("BASKFY_BROKER_HOLDINGS_FIXTURE", str(write_fixture(tmp_path)))
        result = holdings_for_broker(WIRED)
        assert result.source == "fixture"
        assert result.degraded is False

    def test_no_stored_session_is_a_configured_stub_not_a_degradation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        class _NoSession(_FakeStore):
            def exists(self) -> bool:
                return False

        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setenv("BASKFY_KITE_API_KEY", "test-app-key")
        monkeypatch.setenv("BASKFY_BROKER_HOLDINGS_FIXTURE", str(write_fixture(tmp_path)))
        monkeypatch.setattr(broker_holdings, "token_store_for", _NoSession)
        result = holdings_for_broker(WIRED)
        assert result.source == "fixture"
        assert result.degraded is False

    async def test_the_router_still_names_the_fixture_env_var_for_a_real_fixture(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The old note string survives — on the one path where it was ever true."""
        monkeypatch.setenv("BASKFY_BROKER_HOLDINGS_FIXTURE", str(write_fixture(tmp_path)))
        out = await sync_holdings(principal_stub(), AsyncSession(), WIRED)
        assert out.source == "fixture"
        assert out.degraded is False
        # Byte-identical to the pre-C1 note, deliberately: under DRY_RUN the old wording was
        # always true, so nothing about it needed to change. Only the live path was lying.
        assert out.note == "fixture holdings (DRY_RUN or BASKFY_BROKER_HOLDINGS_FIXTURE)"

    async def test_dry_run_without_a_fixture_is_empty_and_never_crashes(self) -> None:
        """DRY_RUN must simulate end to end — safety rail, not just a nicety."""
        out = await sync_holdings(principal_stub(), AsyncSession(), WIRED)
        assert out.source == "empty"
        assert out.holdings == []
        assert out.dry_run is True
        assert out.degraded is False


class TestDegradedFallbackIsDistinguishable:
    """A fallback fixture is still a fixture, and the caller is told it is a fallback."""

    def test_a_network_error_falls_back_to_fixture_and_marks_it_degraded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live_session: None
    ) -> None:
        def _boom(**_kwargs: object) -> list[HoldingRow]:
            raise httpx.ConnectError("broker unreachable")

        monkeypatch.setenv("BASKFY_BROKER_HOLDINGS_FIXTURE", str(write_fixture(tmp_path)))
        monkeypatch.setattr(broker_holdings, "fetch_kite_holdings", _boom)
        result = holdings_for_broker(WIRED)
        assert result.source == "fixture"
        assert result.degraded is True
        assert [row.symbol for row in result.rows] == ["TCS"]

    def test_an_http_status_error_is_degraded_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live_session: None
    ) -> None:
        def _boom(**_kwargs: object) -> list[HoldingRow]:
            request = httpx.Request("GET", "https://api.kite.trade/portfolio/holdings")
            raise httpx.HTTPStatusError(
                "403", request=request, response=httpx.Response(403, request=request)
            )

        monkeypatch.setenv("BASKFY_BROKER_HOLDINGS_FIXTURE", str(write_fixture(tmp_path)))
        monkeypatch.setattr(broker_holdings, "fetch_kite_holdings", _boom)
        assert holdings_for_broker(WIRED).degraded is True

    def test_an_expired_token_is_degraded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A session that should have worked and did not is a degradation, not a stub."""
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setenv("BASKFY_KITE_API_KEY", "test-app-key")
        monkeypatch.setenv("BASKFY_BROKER_HOLDINGS_FIXTURE", str(write_fixture(tmp_path)))
        monkeypatch.setattr(broker_holdings, "token_store_for", _ExpiredStore)
        result = holdings_for_broker(WIRED)
        assert result.source == "fixture"
        assert result.degraded is True

    def test_a_degraded_fallback_with_no_fixture_is_a_degraded_empty(
        self, monkeypatch: pytest.MonkeyPatch, live_session: None
    ) -> None:
        def _boom(**_kwargs: object) -> list[HoldingRow]:
            raise httpx.ConnectError("broker unreachable")

        monkeypatch.setattr(broker_holdings, "fetch_kite_holdings", _boom)
        result = holdings_for_broker(WIRED)
        assert result.source == "empty"
        assert result.rows == ()
        assert result.degraded is True

    def test_a_degraded_fixture_is_distinguishable_from_a_configured_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Same rows, same source — and still tellable apart. That is the requirement."""
        fixture = write_fixture(tmp_path)
        monkeypatch.setenv("BASKFY_BROKER_HOLDINGS_FIXTURE", str(fixture))
        configured = holdings_for_broker(WIRED)

        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setenv("BASKFY_KITE_API_KEY", "test-app-key")
        monkeypatch.setattr(broker_holdings, "token_store_for", _ExpiredStore)
        degraded = holdings_for_broker(WIRED)

        assert configured.source == degraded.source == "fixture"
        assert configured.rows == degraded.rows
        assert configured.degraded is False
        assert degraded.degraded is True
        assert configured != degraded

    def test_a_body_that_is_not_json_is_degraded_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live_session: None
    ) -> None:
        def _boom(**_kwargs: object) -> list[HoldingRow]:
            raise json.JSONDecodeError("not json", "<<<", 0)

        monkeypatch.setenv("BASKFY_BROKER_HOLDINGS_FIXTURE", str(write_fixture(tmp_path)))
        monkeypatch.setattr(broker_holdings, "fetch_kite_holdings", _boom)
        result = holdings_for_broker(WIRED)
        assert result.source == "fixture"
        assert result.degraded is True

    def test_a_bug_in_our_own_code_is_not_reported_as_a_broker_outage(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live_session: None
    ) -> None:
        """The resilience path is for the broker's failures, not ours.

        A ``TypeError`` from our own row mapping is a defect. Swallowing it into a fixture
        would show a user sample data and tell them their broker was unreachable, which is
        both false and unfixable — so it escapes.
        """

        def _our_bug(**_kwargs: object) -> list[HoldingRow]:
            raise TypeError("we passed the wrong thing")

        monkeypatch.setenv("BASKFY_BROKER_HOLDINGS_FIXTURE", str(write_fixture(tmp_path)))
        monkeypatch.setattr(broker_holdings, "fetch_kite_holdings", _our_bug)
        with pytest.raises(TypeError, match="wrong thing"):
            holdings_for_broker(WIRED)

    async def test_the_response_says_degraded_in_the_field_and_the_note(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live_session: None
    ) -> None:
        def _boom(**_kwargs: object) -> list[HoldingRow]:
            raise httpx.ConnectError("broker unreachable")

        monkeypatch.setenv("BASKFY_BROKER_HOLDINGS_FIXTURE", str(write_fixture(tmp_path)))
        monkeypatch.setattr(broker_holdings, "fetch_kite_holdings", _boom)
        out = await sync_holdings(principal_stub(), AsyncSession(), WIRED)
        assert out.source == "fixture"
        assert out.degraded is True
        assert "degraded" in out.note.lower()

    def test_the_degradation_is_reported_not_swallowed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        live_session: None,
    ) -> None:
        """House rule 3: the resilience path logs, and logs no credential."""

        def _boom(**_kwargs: object) -> list[HoldingRow]:
            raise httpx.ConnectError("broker unreachable")

        monkeypatch.setattr(broker_holdings, "fetch_kite_holdings", _boom)
        with caplog.at_level("WARNING", logger=broker_holdings.__name__):
            holdings_for_broker(WIRED)
        assert any("ConnectError" in record.getMessage() for record in caplog.records)
        assert not any("not-a-real-token" in record.getMessage() for record in caplog.records)


class TestEmptyNeverCarriesRowsInvariant:
    def test_constructing_empty_with_rows_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must not carry rows"):
            HoldingsResult(rows=(a_row(),), source="empty")

    def test_constructing_unwired_with_rows_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must not carry rows"):
            HoldingsResult(rows=(a_row(),), source="unwired")

    def test_the_empty_constructor_accepts_no_rows_at_all(self) -> None:
        """Structural, not conventional: there is no argument through which rows could arrive."""
        parameters = inspect.signature(HoldingsResult.empty).parameters
        assert "rows" not in parameters

    def test_a_row_bearing_source_with_no_rows_is_refused(self) -> None:
        """The enum is truthful in both directions — 'live' cannot describe an empty list."""
        with pytest.raises(ValueError, match="requires at least one row"):
            HoldingsResult(rows=(), source="live")
        with pytest.raises(ValueError, match="requires at least one row"):
            HoldingsResult(rows=(), source="fixture")

    def test_the_result_is_frozen_so_rows_cannot_be_added_later(self) -> None:
        """``setattr`` rather than an assignment: mypy rejects the assignment outright, which
        is half the point — the other half is that the *runtime* rejects it too."""
        result = HoldingsResult.empty(detail="nothing here")
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(result, "rows", (a_row(),))  # noqa: B010 - the assignment is the test

    def test_rows_are_an_immutable_tuple_not_a_list(self) -> None:
        """A list would let a caller append to an 'empty' result after construction."""
        assert isinstance(HoldingsResult.empty().rows, tuple)
        assert isinstance(HoldingsResult.live([a_row()]).rows, tuple)

    def test_an_unknown_source_is_refused(self) -> None:
        """The Literal is a compile-time guard; this is the runtime one behind it.

        The value is cast rather than suppressed — house rule 3 forbids a suppression
        comment, and the point of the test is that the *runtime* refuses a value the
        annotation already rejects.
        """
        with pytest.raises(ValueError, match="unknown holdings source"):
            HoldingsResult(rows=(), source=cast(HoldingsSource, "made-up"))

    def test_only_a_fixture_or_an_empty_result_can_be_degraded(self) -> None:
        with pytest.raises(ValueError, match="cannot be degraded"):
            HoldingsResult(rows=(a_row(),), source="live", degraded=True)
        with pytest.raises(ValueError, match="cannot be degraded"):
            HoldingsResult(rows=(), source="unwired", degraded=True)


class TestTheEnumIsClosedAndTruthful:
    def test_the_four_values_are_the_contract(self) -> None:
        contract = {"live", "fixture", "empty", "unwired"}
        assert contract == HOLDINGS_SOURCES

    @pytest.mark.parametrize(
        "scenario",
        ["unwired", "dry_run_empty", "dry_run_fixture", "live", "degraded"],
    )
    def test_every_branch_returns_a_value_from_the_enum_and_never_lies_about_rows(
        self, scenario: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        broker = WIRED
        if scenario == "unwired":
            broker = UNWIRED
        elif scenario == "dry_run_fixture":
            monkeypatch.setenv("BASKFY_BROKER_HOLDINGS_FIXTURE", str(write_fixture(tmp_path)))
        elif scenario in ("live", "degraded"):
            monkeypatch.setenv("DRY_RUN", "false")
            monkeypatch.setenv("BASKFY_KITE_API_KEY", "test-app-key")
            monkeypatch.setattr(broker_holdings, "token_store_for", _FakeStore)
            if scenario == "live":
                monkeypatch.setattr(
                    broker_holdings, "fetch_kite_holdings", lambda **_kwargs: [a_row()]
                )
            else:

                def _boom(**_kwargs: object) -> list[HoldingRow]:
                    raise httpx.ConnectError("broker unreachable")

                monkeypatch.setenv("BASKFY_BROKER_HOLDINGS_FIXTURE", str(write_fixture(tmp_path)))
                monkeypatch.setattr(broker_holdings, "fetch_kite_holdings", _boom)

        result = holdings_for_broker(broker)
        assert result.source in HOLDINGS_SOURCES
        if result.source in ("empty", "unwired"):
            assert result.rows == ()
        else:
            assert result.rows


class TestNoteAgreesWithSource:
    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            (HoldingsResult.live([a_row()]), "live"),
            (HoldingsResult.fixture([a_row()]), "fixture"),
            (HoldingsResult.fixture([a_row()], degraded=True), "fixture"),
            (HoldingsResult.empty(), "empty"),
            (HoldingsResult.empty(degraded=True), "empty"),
            (HoldingsResult.unwired("upstox"), "unwired"),
        ],
    )
    def test_the_note_names_the_source_it_describes(
        self, result: HoldingsResult, expected: str
    ) -> None:
        note = brokers_router._holdings_note(result, broker_name="Zerodha", dry_run=False)
        assert expected in note.lower()

    def test_no_note_repeats_its_own_detail(self) -> None:
        """A detail that duplicates the prose reads as a stutter to whoever has to read it."""
        for result in (
            holdings_for_broker(UNWIRED),
            HoldingsResult.empty(),
        ):
            note = brokers_router._holdings_note(result, broker_name="Upstox", dry_run=True)
            assert note.count("adapter") <= 1
            assert "()" not in note

    def test_a_degraded_note_says_degraded_and_a_configured_one_does_not(self) -> None:
        rows = [a_row()]
        configured = brokers_router._holdings_note(
            HoldingsResult.fixture(rows), broker_name="Zerodha", dry_run=True
        )
        degraded = brokers_router._holdings_note(
            HoldingsResult.fixture(rows, degraded=True), broker_name="Zerodha", dry_run=False
        )
        assert "degraded" not in configured.lower()
        assert "degraded" in degraded.lower()

    def test_source_is_a_response_field_not_only_prose(self) -> None:
        """A client must be able to switch on it without parsing English."""
        assert "source" in SyncHoldingsOut.model_fields
        assert "degraded" in SyncHoldingsOut.model_fields


class TestQuantityContractSurvives:
    async def test_total_quantity_is_quantity_plus_t1_plus_collateral_on_a_live_row(
        self, monkeypatch: pytest.MonkeyPatch, live_session: None
    ) -> None:
        """Desk non-negotiable #2, asserted on the path that used to be mislabelled."""
        no_persistence(monkeypatch)
        monkeypatch.setattr(
            broker_holdings, "fetch_kite_holdings", lambda **_kwargs: [a_row(quantity=10)]
        )
        out = await sync_holdings(principal_stub(), AsyncSession(), WIRED)
        row = out.holdings[0]
        assert out.source == "live"
        assert row.quantity == Decimal("10")
        assert row.t1_quantity == Decimal("2")
        assert row.collateral_quantity == Decimal("3")
        assert row.total_quantity == Decimal("15")
        assert row.total_quantity == row.quantity + row.t1_quantity + row.collateral_quantity

    def test_quantities_are_decimal_never_float(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """House rule 9, through the fixture reader as well as the live one.

        ``Decimal`` and ``float`` are disjoint types, so asserting the first *is* asserting
        the second — a separate ``not isinstance(value, float)`` line would be unreachable.
        """
        monkeypatch.setenv("BASKFY_BROKER_HOLDINGS_FIXTURE", str(write_fixture(tmp_path)))
        row = holdings_for_broker(WIRED).rows[0]
        for value in (row.quantity, row.t1_quantity, row.collateral_quantity, row.average_price):
            assert isinstance(value, Decimal)
            assert type(value) is Decimal


class TestStillReadOnly:
    def test_neither_module_names_an_order_path(self) -> None:
        """Law 2 and non-negotiable #1: holdings is a read, and stays a read."""
        for module in (broker_holdings, brokers_router):
            source = inspect.getsource(module)
            for forbidden in ("place_order", "place_gtt", "OrderGateway", "confirm=true"):
                assert forbidden not in source, f"{module.__name__} names {forbidden}"

    def test_the_only_broker_url_is_the_read_only_holdings_endpoint(self) -> None:
        assert broker_holdings._KITE_HOLDINGS_URL.endswith("/portfolio/holdings")

    def test_the_wired_set_still_exists_under_its_pinned_name(self) -> None:
        """C2's honesty test imports this name; C1 may change its contents, never its name."""
        assert isinstance(broker_holdings._HOLDINGS_WIRED, frozenset)
        assert WIRED in broker_holdings._HOLDINGS_WIRED


class TestConnectDoesNotOverClaimEither:
    """Handed to C1 by leaf C2: the same over-claim, one dimension over.

    ``holdings_sync`` was not the only capability the catalog advertised beyond what is
    built. ``POST /brokers/{id}/connect`` used to hand back a redirect for any broker with a
    known authorize URL — five of them — while the app key it embedded and the token
    exchange behind the callback are Zerodha's alone. Clicking "Connect" on Upstox therefore
    put Baskfy's Zerodha app key in Upstox's URL and then failed at the callback, and the
    only refusal the route could produce named Zerodha whatever the caller had clicked.

    C1 owns ``routers/brokers.py``, so the fix and its tests live in this file even though it
    is named for holdings provenance.
    """

    @pytest.fixture(autouse=True)
    def _a_configured_zerodha_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # BOTH. The route needs a key *and* a secret, because the login it starts ends at
        # `session/token`, whose checksum is SHA256(api_key + request_token + api_secret). This
        # fixture set only the key, so "configured" here meant something the real flow would have
        # rejected halfway through — see `test_a_publisher_key_alone_is_not_a_connect_app`.
        monkeypatch.setenv("BASKFY_KITE_API_KEY", "zerodha-app-key")
        monkeypatch.setenv("BASKFY_KITE_API_SECRET", "zerodha-app-secret")
        clear_oauth_states()

    async def test_a_publisher_key_alone_is_not_a_connect_app(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kite sells two products and only one of them can finish this login.

        **Publisher** is free and embeds a basket the user confirms inside Kite; it issues no API
        secret. **Connect** is the paid REST API and the only one that can redeem a request_token.
        A Publisher key is a perfectly valid credential that can never satisfy this route.

        The refusal used to read "The Zerodha app key is not configured on this deployment",
        which says *nobody pasted a key* — and on 27 Aug 2026 it was shown to someone who had
        pasted two, both correct, neither of the kind this needs. So the message is asserted, not
        just the refusal: an error that does not say what to do next costs more than the outage.
        """
        monkeypatch.setenv("BASKFY_KITE_API_KEY", "a-publisher-key")
        monkeypatch.delenv("BASKFY_KITE_API_SECRET", raising=False)

        out = await connect_broker(principal_stub(), "zerodha")
        assert out.oauth_available is False
        assert out.redirect_url is None
        assert "BASKFY_KITE_API_SECRET" in out.reason, out.reason
        assert "Connect" in out.reason and "Publisher" in out.reason, out.reason
        # One variable missing, so singular. "X and Y is not set" is the kind of sentence that
        # makes a reader wonder what else here was not read carefully.
        assert "BASKFY_KITE_API_SECRET is not set" in out.reason, out.reason

    async def test_the_refusal_agrees_in_number_when_both_are_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BASKFY_KITE_API_KEY", raising=False)
        monkeypatch.delenv("BASKFY_KITE_API_SECRET", raising=False)

        out = await connect_broker(principal_stub(), "zerodha")
        assert "BASKFY_KITE_API_KEY and BASKFY_KITE_API_SECRET are not set" in out.reason

    @pytest.mark.parametrize("broker_id", ["upstox", "angelone", "fyers", "dhan"])
    async def test_a_login_that_cannot_finish_is_refused_not_redirected(
        self, broker_id: str
    ) -> None:
        out = await connect_broker(principal_stub(), broker_id)
        assert out.oauth_available is False
        assert out.redirect_url is None
        assert out.state is None

    async def test_the_refusal_names_the_broker_the_caller_actually_clicked(self) -> None:
        out = await connect_broker(principal_stub(), "upstox")
        assert "Upstox" in out.reason
        assert "Zerodha app key is not configured" not in out.reason

    @pytest.mark.parametrize(
        "broker_id", ["upstox", "angelone", "fyers", "dhan", "kotak", "icici", "hdfc"]
    )
    async def test_no_other_brokers_url_ever_carries_the_zerodha_app_key(
        self, broker_id: str
    ) -> None:
        """A credential leak, not merely a misleading label: the key is ours, not theirs."""
        out = await connect_broker(principal_stub(), broker_id)
        assert out.redirect_url is None or "zerodha-app-key" not in out.redirect_url

    async def test_zerodha_still_gets_its_redirect(self) -> None:
        out = await connect_broker(principal_stub(), "zerodha")
        assert out.oauth_available is True
        assert out.redirect_url is not None
        assert out.redirect_url.startswith("https://kite.zerodha.com/connect/login?")
        assert out.state

    def test_kite_credentials_alone_do_not_switch_on_the_connect_button(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The trap that making the *data* bridge work would otherwise spring.

        Baskfy uses the momentum desk's Kite app so its backfill and `fetch_daily_bars` can run on
        the desk's daily session (`baskfy_worker.kite_session_cli`). One app means one registered
        redirect, and it points at `desk.modelbasket.in/callback`. So the key and the secret being
        present says nothing about whether a person clicking "Connect" lands back on Baskfy — they
        would land on the desk.

        `connect_configured` therefore asks three questions, not two. Without the third, wiring
        Kite for data would have reinstated exactly the dead button that field exists to prevent.
        """
        from baskfy_api.routers.brokers import _connect_configured  # noqa: PLC0415

        monkeypatch.setenv("BASKFY_KITE_API_KEY", "desk-app-key")
        monkeypatch.setenv("BASKFY_KITE_API_SECRET", "desk-app-secret")
        monkeypatch.setenv("BASKFY_BROKER_OAUTH_REDIRECT", "https://desk.modelbasket.in/callback")
        assert _connect_configured() is False

    def test_a_redirect_that_does_come_back_to_us_does_switch_it_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other direction, so this is a gate rather than an off switch.

        A Baskfy-owned Connect app would set a redirect on our own origin, and the button must
        return without a code change.
        """
        from baskfy_api.routers.brokers import _connect_configured  # noqa: PLC0415
        from baskfy_api.settings import get_settings  # noqa: PLC0415 - local to this test

        monkeypatch.setenv("BASKFY_KITE_API_KEY", "our-app-key")
        monkeypatch.setenv("BASKFY_KITE_API_SECRET", "our-app-secret")
        monkeypatch.delenv("BASKFY_BROKER_OAUTH_REDIRECT", raising=False)
        get_settings.cache_clear()
        assert _connect_configured() is True

    async def test_the_redirect_uri_points_at_a_route_that_exists(self) -> None:
        """The half of an OAuth flow that fails *after* the user has committed to it.

        The default used to be `https://baskfy.com/brokers/callback`, wrong twice over: the apex
        has no DNS record (only `staging.baskfy.com` resolves), and `/brokers/callback` is not a
        route — `/brokers` is the page, and the callback this service serves is
        `/api/v1/brokers/callback`. Neither shows up until Kite sends the browser back, by which
        point the user has already signed in at Zerodha and authorised the app.

        The login URL no longer carries `redirect_uri` at all: Kite uses the redirect REGISTERED
        against the app and ignores one supplied at login time, so asserting on it was asserting
        on a value Kite never read. What still has to be right is the redirect
        `_connect_configured()` compares against `web_origin`, so that is what this checks.

        It also pins the mechanism that actually broke. `state` must travel inside
        `redirect_params` — Kite echoes back only what that carries and silently drops unknown
        top-level keys, so sending `{"state": ...}` at the top level meant every login came home
        stateless and the callback refused it as one it had not started.
        """
        out = await connect_broker(principal_stub(), "zerodha")
        assert out.redirect_url is not None
        query = parse_qs(urlparse(out.redirect_url).query)

        assert "redirect_uri" not in query, "Kite ignores it; sending it only looks like it works"
        assert "state" not in query, "a top-level `state` is dropped by Kite — see redirect_params"

        nested = parse_qs(query["redirect_params"][0])
        assert nested["state"][0], "the callback requires a state Kite will echo back"

        # The redirect the operator must have registered, and the one `_connect_configured()`
        # measures against — asserted via the path constant the route is registered under rather
        # than a string typed out a second time.
        settings = get_settings()
        expected = os.environ.get("BASKFY_BROKER_OAUTH_REDIRECT", "").strip() or (
            f"{settings.web_origin.rstrip('/')}{OAUTH_CALLBACK_PATH}"
        )
        assert expected.endswith(OAUTH_CALLBACK_PATH), expected
        # ...and it is absolute against a host we actually serve, not the unresolvable apex.
        assert expected.startswith(settings.web_origin.rstrip("/")), expected
        assert "//brokers/callback" not in expected

    async def test_a_callback_for_a_broker_we_cannot_exchange_for_is_refused(self) -> None:
        """The token store is one shared blob; honouring this would file it under a lie."""
        state = "state-for-a-broker-we-cannot-exchange-for"
        register_oauth_state(state=state, user_id=1, broker_id="upstox")
        with pytest.raises(Problem) as caught:
            await oauth_callback(
                principal_stub(), AsyncSession(), request_stub(), request_token="req-token-1234", state=state
            )
        assert caught.value.status == 400
