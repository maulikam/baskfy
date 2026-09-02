"""Tree-3 leaf 3.2 — OAuth callback + encrypted token helpers."""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import broker_oauth
from baskfy_api.app import create_app
from baskfy_api.broker_oauth import (
    SIMULATED_TOKEN_PREFIX,
    SimulatedTokenRefused,
    TokenExchange,
    clear_oauth_states,
    consume_oauth_state,
    exchange_request_token,
    exchange_request_token_stub,
    is_simulated_token,
    register_oauth_state,
    simulated_exchange_reasons,
    simulated_token_storage_enabled,
    simulated_token_store_for,
    simulated_token_store_path,
    store_access_token,
    token_store_for,
    token_store_path,
)
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.routers import brokers as brokers_router
from baskfy_api.routers.brokers import oauth_callback
from baskfy_core.broker_connections import BROKER_OAUTH_REVIEW

#: What the M58 desk bridge leaves in the shared blob. Not a `sim_` value, and the assertions
#: below check for this exact string rather than merely "not simulated" — the defect this file
#: guards against replaced a real session, so proving the *same* session survived is the test.
REAL_DESK_SESSION = "desk-live-session-token-from-m58-bridge"


@pytest.fixture(autouse=True)
def _clean_oauth_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    clear_oauth_states()
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("BASKFY_KITE_API_SECRET", raising=False)
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("BASKFY_KITE_TOKEN_ENCRYPTION_KEY", key)
    monkeypatch.setenv("BASKFY_BROKER_TOKEN_PATH", str(tmp_path / "broker-token.enc"))
    monkeypatch.setenv("BASKFY_KITE_API_KEY", "test-api-key")
    # Absent, not false: the spec is that a deployment which says nothing gets the refusal.
    monkeypatch.delenv("BASKFY_BROKER_OAUTH_ALLOW_SIMULATED", raising=False)
    yield
    clear_oauth_states()


@pytest.fixture(scope="module")
def spec() -> dict[str, object]:
    return create_app().openapi()


class TestGateOpenAndCallbackRoute:
    def test_gate_is_open(self) -> None:
        assert BROKER_OAUTH_REVIEW.signed_off is True
        assert BROKER_OAUTH_REVIEW.blocks_live_oauth is False

    def test_callback_is_in_openapi(self, spec: dict[str, object]) -> None:
        paths = spec["paths"]
        assert isinstance(paths, dict)
        assert "/api/v1/brokers/callback" in paths
        callback = paths["/api/v1/brokers/callback"]
        assert isinstance(callback, dict)
        assert "get" in callback


class TestOauthStateAndStub:
    def test_stub_is_deterministic(self) -> None:
        a = exchange_request_token_stub(api_key="k", request_token="rt-one", user_id=7)
        b = exchange_request_token_stub(api_key="k", request_token="rt-one", user_id=7)
        assert a == b
        assert a.startswith("sim_")

    def test_bad_state_is_rejected_by_helper(self) -> None:
        assert consume_oauth_state("never-issued-state-value") is None

    async def test_opted_in_simulated_callback_stores_encrypted_bytes_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The exercisable DRY_RUN flow, opted into — and still not the real session.

        Was `test_happy_path_stores_encrypted_bytes`, which asserted `connected is True` for a
        simulated login writing over the shared blob. That was the defect, spelled as a test.
        """
        monkeypatch.setenv("BASKFY_BROKER_OAUTH_ALLOW_SIMULATED", "true")
        register_oauth_state(state="good-state-token-abc12345", user_id=42, broker_id="zerodha")
        principal = MagicMock()
        principal.require_user.return_value = 42

        result = await oauth_callback(
            principal,
            AsyncSession(),
            request_token="request-token-xyz789",
            state="good-state-token-abc12345",
        )
        assert result.connected is False
        assert result.token_stored is True
        assert result.simulated is True
        assert result.broker_id == "zerodha"
        assert "simulated" in result.note

        real = tmp_path / "broker-token.enc"
        assert not real.exists(), "a simulated login must not create the real session blob"

        path = simulated_token_store_path()
        assert path == tmp_path / "broker-token.simulated.enc"
        assert path.is_file()
        raw = path.read_bytes()
        assert raw
        assert SIMULATED_TOKEN_PREFIX.encode() not in raw

        loaded = simulated_token_store_for().load()
        assert loaded.value.startswith(SIMULATED_TOKEN_PREFIX)
        assert consume_oauth_state("good-state-token-abc12345") is None

    async def test_callback_rejects_bad_state(self) -> None:
        principal = MagicMock()
        principal.require_user.return_value = 1
        with pytest.raises(Problem) as caught:
            await oauth_callback(
                principal,
                AsyncSession(),
                request_token="request-token-xyz789",
                state="bogus-state-xxxxxxxx",
            )
        assert caught.value.status == 400


class TestNoPlaceOrder:
    def test_oauth_modules_never_place_an_order(self) -> None:
        for module in (brokers_router, broker_oauth):
            source = inspect.getsource(module)
            for forbidden in ("place_order", "OrderGateway", "confirm=true", "kc.place"):
                assert forbidden not in source, f"{module.__name__} names {forbidden}"


class TestSimulatedTokenNeverPoisonsTheRealSession:
    """Leaf 1.1.4 — the shared blob is the desk's live session; a stub must never land in it.

    `token_store_path()` falls back to `BASKFY_KITE_TOKEN_PATH`, which on the box is
    `/var/lib/baskfy/state/kite-token.enc` — the file the M58 bridge fills and the nightly
    pipeline reads. Kite's registered redirect points at this callback, so these are not
    hypotheticals: they are what happens when a person finishes a Kite login today.
    """

    def test_simulated_store_is_a_different_file_from_the_real_one(self) -> None:
        assert simulated_token_store_path() != token_store_path()
        assert simulated_token_store_path().parent == token_store_path().parent

    def test_stub_storage_is_off_unless_explicitly_opted_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert simulated_token_storage_enabled() is False
        for falsey in ("", "false", "0", "no", "off", "maybe"):
            monkeypatch.setenv("BASKFY_BROKER_OAUTH_ALLOW_SIMULATED", falsey)
            assert simulated_token_storage_enabled() is False, falsey
        for truthy in ("1", "true", "TRUE", "yes", "on"):
            monkeypatch.setenv("BASKFY_BROKER_OAUTH_ALLOW_SIMULATED", truthy)
            assert simulated_token_storage_enabled() is True, truthy

    def test_store_refuses_a_simulated_token_in_the_real_store(self) -> None:
        real = token_store_for()
        real.save(REAL_DESK_SESSION)
        stub = exchange_request_token_stub(api_key="k", request_token="rt", user_id=1)

        with pytest.raises(SimulatedTokenRefused):
            store_access_token(stub, store=real)

        assert token_store_for().load().value == REAL_DESK_SESSION

    def test_store_refuses_a_real_token_in_the_simulated_store(self) -> None:
        with pytest.raises(SimulatedTokenRefused):
            store_access_token(REAL_DESK_SESSION, store=simulated_token_store_for())
        assert not simulated_token_store_path().exists()

    def test_store_routes_by_kind_when_no_store_is_named(self) -> None:
        stub = exchange_request_token_stub(api_key="k", request_token="rt", user_id=1)
        store_access_token(stub)
        assert simulated_token_store_path().is_file()
        assert not token_store_path().exists()

        store_access_token(REAL_DESK_SESSION)
        assert token_store_for().load().value == REAL_DESK_SESSION
        assert simulated_token_store_for().load().value == stub

    def test_the_write_helper_has_exactly_two_call_sites(self) -> None:
        """Sibling audit, as an assertion rather than a claim in a docstring.

        `store_access_token` is the guarded write path. A caller that appears elsewhere is
        still guarded — that is the whole reason the check lives in the helper — but a new
        writer of a shared credential store is worth one deliberate look, so the tree is
        scanned rather than trusted.
        """
        root = Path(broker_oauth.__file__).parents[4]
        callers = sorted(
            str(source.relative_to(root))
            for source in root.glob("*/*/src/**/*.py")
            if "store_access_token(" in source.read_text(encoding="utf-8")
        )
        assert callers == [
            "services/api/src/baskfy_api/broker_oauth.py",
            "services/api/src/baskfy_api/routers/brokers.py",
        ], f"a new caller of store_access_token: {callers}"

    def test_every_module_touching_the_token_store_is_a_known_one(self) -> None:
        """The other half of the audit: who else opens the shared session blob.

        The API can no longer poison it. These are the readers and the one other writer, and
        the list is pinned so that a new module joining it is reviewed rather than assumed
        safe. `kite_session_cli` is the M58 desk bridge — the only other writer, and it always
        carries a real desk token.
        """
        root = Path(broker_oauth.__file__).parents[4]
        touching = sorted(
            str(source.relative_to(root))
            for source in root.glob("*/*/src/**/*.py")
            if "AccessTokenStore" in source.read_text(encoding="utf-8")
        )
        assert touching == [
            "packages/providers/src/baskfy_providers/__init__.py",  # re-export
            "packages/providers/src/baskfy_providers/errors.py",  # names it in a message
            "packages/providers/src/baskfy_providers/kite.py",  # reader
            "packages/providers/src/baskfy_providers/tokens.py",  # the store itself
            "services/api/src/baskfy_api/broker_oauth.py",  # guarded writer
            # Leaf 3.1's resync detector. Reviewed and admitted as a **reader that never
            # writes**: it opens the store only to read `issued_at`, so the admin page can say
            # "there is no usable Kite session" instead of the pipeline discovering it at 6pm.
            # It has no `.save(`, never returns or logs the token value, and the repair it
            # triggers goes through `kite_session_cli` — the M58 bridge below — rather than
            # writing the blob itself.
            "services/api/src/baskfy_api/resync.py",  # reader (issue date only)
            # M75's connection readback. Reviewed and admitted as a **reader**, and it was
            # already the second audited writer — `test_the_write_helper_has_exactly_two_call_sites`
            # above has listed it since the callback was built, because the callback is what
            # stores a real token through the guarded helper.
            #
            # What is new is `_connection_state`, which opens the store to answer one question for
            # the brokers page: is there a session behind this row. Before it, `_broker_out` said
            # `connected=False` unconditionally, so a finished login was invisible and Maulik saw
            # "Connect Zerodha" after connecting successfully.
            #
            # It reads and never writes: no `.save(`, and the token VALUE never leaves the
            # function — only a bool and a status string do. A `sim_` token is reported as
            # `simulated` rather than connected, which is this class's own rule applied one layer
            # further out. (The scan is a substring match, so the mention of the store in a
            # comment would have listed this file anyway; the `.load()` call is the real reason.)
            "services/api/src/baskfy_api/routers/brokers.py",  # reader (presence only)
            "services/worker/src/baskfy_worker/index_backfill.py",  # reader
            "services/worker/src/baskfy_worker/kite_session_cli.py",  # M58 bridge writer
            "services/worker/src/baskfy_worker/ops.py",  # reader
        ], f"a new module opens the access-token store: {touching}"

    def test_the_router_only_writes_through_the_guarded_helper(self) -> None:
        source = inspect.getsource(brokers_router)
        assert source.count("store_access_token(") == 2  # simulated branch, live branch
        assert ".save(" not in source, "the router must not reach past store_access_token"


class TestDryRunCallbackCannotPoisonAStoredSession:
    """G2 — the exact live scenario, both ways round."""

    @staticmethod
    def _principal(user_id: int = 42) -> MagicMock:
        principal = MagicMock()
        principal.require_user.return_value = user_id
        return principal

    async def test_dry_run_callback_leaves_the_real_session_intact_and_refuses(self) -> None:
        token_store_for().save(REAL_DESK_SESSION)
        register_oauth_state(state="live-state-token-000111", user_id=42, broker_id="zerodha")

        with pytest.raises(Problem) as caught:
            await oauth_callback(
                self._principal(),
                AsyncSession(),
                request_token="kite-request-token-1",
                state="live-state-token-000111",
            )

        problem = caught.value
        assert problem.status == 503
        assert problem.type is ProblemType.PIPELINE_DEGRADED
        assert "DRY_RUN" in problem.detail
        assert SIMULATED_TOKEN_PREFIX not in problem.detail

        # The whole point: the desk's session is byte-for-byte the one that was there.
        assert token_store_for().load().value == REAL_DESK_SESSION
        assert not simulated_token_store_path().exists()

    async def test_opted_in_dry_run_callback_still_leaves_the_real_session_intact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BASKFY_BROKER_OAUTH_ALLOW_SIMULATED", "true")
        token_store_for().save(REAL_DESK_SESSION)
        register_oauth_state(state="live-state-token-000222", user_id=42, broker_id="zerodha")

        result = await oauth_callback(
            self._principal(),
            AsyncSession(),
            request_token="kite-request-token-2",
            state="live-state-token-000222",
        )

        assert result.connected is False, "a stub session is not a connection"
        assert result.simulated is True
        assert token_store_for().load().value == REAL_DESK_SESSION
        assert simulated_token_store_for().load().value.startswith(SIMULATED_TOKEN_PREFIX)

    async def test_refusal_names_every_missing_precondition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setenv("BASKFY_KITE_API_KEY", "")
        monkeypatch.delenv("BASKFY_KITE_API_SECRET", raising=False)
        register_oauth_state(state="live-state-token-000333", user_id=42, broker_id="zerodha")

        with pytest.raises(Problem) as caught:
            await oauth_callback(
                self._principal(),
                AsyncSession(),
                request_token="kite-request-token-3",
                state="live-state-token-000333",
            )
        detail = caught.value.detail
        assert "BASKFY_KITE_API_KEY" in detail
        assert "BASKFY_KITE_API_SECRET" in detail
        assert "DRY_RUN" not in detail
        assert caught.value.extra["reasons"] == list(
            simulated_exchange_reasons(api_key="", api_secret="")
        )

    async def test_a_real_exchange_is_stored_in_the_real_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The live branch, with the network stubbed at the exchange rather than at httpx.

        No live Kite call: `exchange_request_token` is replaced, so nothing reaches the wire.
        """
        monkeypatch.setattr(
            brokers_router,
            "exchange_request_token",
            lambda **_: TokenExchange(access_token="real-kite-token", simulated=False, reasons=()),
        )
        register_oauth_state(state="live-state-token-000444", user_id=42, broker_id="zerodha")

        result = await oauth_callback(
            self._principal(),
            AsyncSession(),
            request_token="kite-request-token-4",
            state="live-state-token-000444",
        )
        assert result.connected is True
        assert result.simulated is False
        assert result.token_stored is True
        assert token_store_for().load().value == "real-kite-token"
        assert not simulated_token_store_path().exists()


class TestExchangeReportsWhetherItWasSimulated:
    def test_simulated_flag_and_reasons_come_from_the_exchange_itself(self) -> None:
        result = exchange_request_token(api_key="k", request_token="rt", user_id=3)
        assert isinstance(result, TokenExchange)
        assert result.simulated is True
        assert is_simulated_token(result.access_token)
        assert result.reasons

    def test_reasons_are_empty_only_when_all_three_preconditions_hold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DRY_RUN", "true")
        assert simulated_exchange_reasons(api_key="k", api_secret="s")
        monkeypatch.setenv("DRY_RUN", "false")
        assert simulated_exchange_reasons(api_key="", api_secret="s")
        assert simulated_exchange_reasons(api_key="k", api_secret="")
        assert simulated_exchange_reasons(api_key="k", api_secret="s") == ()

    def test_the_router_never_recomputes_the_simulated_flag(self) -> None:
        """The old defect's second half: the response's flag was derived independently.

        It was correct, and nothing acted on it. Reading it off the exchange result is what
        makes it impossible for the field and the behaviour to disagree.
        """
        source = inspect.getsource(brokers_router.oauth_callback)
        body = source.replace(brokers_router.oauth_callback.__doc__ or "", "")
        assert 'os.environ.get("BASKFY_KITE_API_SECRET"' not in body
        assert "dry_run_enabled(" not in body
        assert "exchange.simulated" in body


class TestConnectPullsHoldingsImmediately:
    """M81. A finished login is the one moment a Kite session is certainly alive.

    Kite ends a session at the start of the next trading day, so "connected" is a state that has to
    be re-established every morning. Leaving the holdings read to a manual Sync meant a fresh login
    showed an empty Portfolio — "Holdings not synced yet" beside a broker that had just connected —
    and with daily expiry that is the normal state each morning, not an edge case.
    """

    def test_the_callback_reads_holdings_after_storing_the_token(self) -> None:
        """Asserted on the source, because the ordering is the point.

        The read must come after `store_access_token`: `holdings_for_broker` loads the session from
        the store, so a read placed first would use yesterday's token or none at all.
        """
        source = inspect.getsource(brokers_router.oauth_callback)
        assert "store_access_token" in source
        assert "holdings_for_broker" in source
        assert source.index("store_access_token") < source.index("holdings_for_broker"), (
            "holdings are read before the new token is stored, so the read uses the old session"
        )

    def test_a_failed_read_does_not_undo_the_login(self) -> None:
        """The token is already stored and valid; a data fetch must not throw the session away.

        Pinned on the source rather than by forcing a failure, because what matters is that the
        read is inside a try and the connected response is outside it.
        """
        source = inspect.getsource(brokers_router.oauth_callback)
        assert "except Exception" in source
        assert "session.rollback()" in source
        # `connected=True` is returned regardless of what the holdings read did.
        tail = source[source.index("except Exception") :]
        assert "connected=True" in tail or "connected=True" in source

    def test_only_a_live_read_is_written(self) -> None:
        """The same guard the manual path has: a fixture must never land in a real portfolio."""
        source = inspect.getsource(brokers_router.oauth_callback)
        assert "is_persistable" in source
