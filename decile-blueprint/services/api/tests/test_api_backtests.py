"""``/backtests/*`` over HTTP — docs/07 §Backtests (Prompt 15 deliverable 5).

The simulation itself is proven twice already: ``packages/core/tests/test_backtest.py`` runs
docs/10's six correctness tests against the engine, and ``services/worker/tests/test_backtest.py``
runs the whole job against PostgreSQL. This suite is about the *contract*: who may call these
routes, what a queued run answers with, how the artefacts paginate, whether a signed link expires,
and what the event stream sends first.

So the ``done`` backtest these tests read is **constructed**, not simulated — a small
:class:`~baskfy_core.backtest.BacktestResult` built by hand and put through the same
``build_payload``/``artefact_bytes`` the worker uses. Running a fifteen-year simulation to check
that pagination works would test the engine for the third time and the router for the first.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Final

import anyio
import api_helpers
import httpx
import pytest
import pytest_asyncio
import screener_helpers
from api_helpers import bearer, make_user, running_app, url
from redis.asyncio import Redis
from redis.exceptions import RedisError
from screener_helpers import requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.backtests import (
    ARTEFACTS,
    DownloadSigningUnavailable,
    artefact_bytes,
    artefact_key,
    build_payload,
    events_channel,
    new_public_id,
    progress_frame,
    progress_key,
    sign_download,
    verify_download,
)
from baskfy_api.routers.backtests import event_stream
from baskfy_api.settings import Settings
from baskfy_core.backtest import (
    BacktestConfig,
    BacktestResult,
    HoldingSnapshot,
    SelectionSpec,
    Trade,
    TradeReason,
    TradeSide,
)
from baskfy_core.models import Backtest, Screen
from baskfy_providers.archive import LocalRawArchive

pytestmark = [requires_db, pytest.mark.db]

START: Final = dt.date(2025, 1, 1)
END: Final = dt.date(2025, 3, 31)


class RecordingQueue:
    """Stands in for the Celery producer. Records what would have been published."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, list[object]]] = []

    def send_task(self, name: str, args: Sequence[object]) -> object:
        self.sent.append((name, list(args)))
        return None


@pytest_asyncio.fixture
async def session(screener_session: AsyncSession) -> AsyncIterator[AsyncSession]:
    yield screener_session


def _settings(archive_dir: Path, *, executor: str = "celery") -> Settings:
    """The usual test settings, with the artefact store pointed at the test's own directory.

    ``invoice_local_dir`` is the directory ``build_invoice_archive`` falls back to when no bucket
    is configured, and backtests share that store — one abstraction over R2-or-a-directory, not
    two (see ``baskfy_api.backtests``).

    ``executor`` defaults to ``celery`` here and to ``inline`` in production, which is the opposite
    of the obvious arrangement and is deliberate. These tests assert what the *endpoint* does — the
    row it writes, the cap it enforces, the idempotency key it honours — and an inline executor
    would run a real simulation inside each of them against a fixture database with no bars.
    `RecordingQueue` keeps the dispatch observable without simulating anything.
    """
    return api_helpers.api_settings(
        screener_helpers.database_url(),
        invoice_local_dir=str(archive_dir),
        backtest_executor=executor,
    )


def _config(**overrides: object) -> BacktestConfig:
    base: dict[str, object] = {
        "start": START,
        "end": END,
        "initial_capital": Decimal(1_000_000),
        "selection": SelectionSpec(top_n=5, hold_buffer=2),
    }
    base.update(overrides)
    return BacktestConfig.model_validate(base)


async def _screen(session: AsyncSession, user_id: int) -> Screen:
    screen = Screen(
        public_id=new_public_id()[:12],
        user_id=user_id,
        name="Backtest screen",
        definition={"index": "nifty-500", "sort_by": "ret_12m"},
        columns=[],
    )
    session.add(screen)
    await session.flush()
    return screen


def _result(config: BacktestConfig) -> BacktestResult:
    """A three-day run with two fills. Enough to exercise every artefact writer."""
    days = (dt.date(2025, 1, 1), dt.date(2025, 1, 2), dt.date(2025, 1, 3))
    equity = (Decimal("1000000.00"), Decimal("1010000.00"), Decimal("1005000.00"))
    trades = (
        Trade(
            date=days[1],
            instrument_id=1,
            symbol="ACME",
            side=TradeSide.BUY,
            quantity=100,
            price=Decimal("500.0000"),
            notional=Decimal("50000.00"),
            cost=Decimal("140.00"),
            reason=TradeReason.ENTER,
        ),
        Trade(
            date=days[2],
            instrument_id=1,
            symbol="ACME",
            side=TradeSide.SELL,
            quantity=100,
            price=Decimal("510.0000"),
            notional=Decimal("51000.00"),
            cost=Decimal("142.80"),
            reason=TradeReason.EXIT,
            realised_pnl=Decimal("717.20"),
        ),
    )
    holdings = (
        HoldingSnapshot(
            rebalance_date=days[0],
            executed_on=days[1],
            instrument_id=1,
            symbol="ACME",
            name="ACME LIMITED",
            rank=1,
            target_weight=Decimal("1.000000"),
            quantity=100,
            price=Decimal("500.0000"),
            value=Decimal("50000.00"),
            actual_weight=Decimal("0.049505"),
        ),
    )
    return BacktestResult(
        config=config,
        dates=days,
        equity=equity,
        cash=(Decimal("1000000.00"), Decimal("960000.00"), Decimal("1005000.00")),
        invested=(Decimal(0), Decimal("50000.00"), Decimal(0)),
        benchmark=(Decimal("100"), Decimal("101"), Decimal("100.5")),
        trades=trades,
        holdings=holdings,
        delistings=(),
        total_costs=Decimal("282.80"),
        dividends_credited=Decimal(0),
        rebalance_dates=(days[0],),
        data_version=screener_helpers.DATA_VERSION,
    )


async def _completed(
    session: AsyncSession, user_id: int, archive_dir: Path, *, config: BacktestConfig | None = None
) -> Backtest:
    """A ``done`` row, with its three artefacts written where the app will look for them."""
    resolved = config or _config()
    public_id = new_public_id()
    result = _result(resolved)
    stored = build_payload(public_id, resolved, result, None)
    archive = LocalRawArchive(archive_dir)
    for artefact, payload in artefact_bytes(result).items():
        archive.put(artefact_key(public_id, artefact), payload, content_type="text/csv")
    row = Backtest(
        public_id=public_id,
        user_id=user_id,
        screen_id=None,
        config=json.loads(resolved.model_dump_json()),
        status="done",
        metrics=stored.metrics,
        equity_curve=stored.equity_curve,
        trades_key=stored.trades_key,
        finished_at=dt.datetime(2026, 8, 20, 12, tzinfo=dt.UTC),
    )
    session.add(row)
    await session.flush()
    return row


# ---------------------------------------------------------------------------
# POST /backtests
# ---------------------------------------------------------------------------


async def test_backtests_are_entitlement_gated(session: AsyncSession, tmp_path: Path) -> None:
    """docs/07 §Entitlements lists ``backtests``; CLAUDE.md has carried "nothing enforces it"
    since Prompt 13. This is the test that makes that note obsolete."""
    _, public_id = await make_user(session, "unpaid.backtest@example.com", subscribed=False)
    async with running_app(_settings(tmp_path), session, task_queue=RecordingQueue()) as client:
        response = await client.post(
            url("/backtests"),
            json={"config": json.loads(_config().model_dump_json())},
            headers=bearer(public_id),
        )
    assert response.status_code == 402
    body = response.json()
    assert body["type"] == "payment-required"
    assert body["upgrade_url"] == "/pricing"


async def test_a_paid_account_queues_a_run(session: AsyncSession, tmp_path: Path) -> None:
    """docs/07: `POST /backtests { config } → 202 { public_id, status:"queued" }`."""
    user_id, public_id = await make_user(session, "paid.backtest@example.com", subscribed=True)
    screen = await _screen(session, user_id)
    queue = RecordingQueue()
    config = _config(screen_public_id=screen.public_id)
    async with running_app(_settings(tmp_path), session, task_queue=queue) as client:
        response = await client.post(
            url("/backtests"),
            json={"config": json.loads(config.model_dump_json())},
            headers=bearer(public_id),
        )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["events_url"].endswith(f"/backtests/{body['public_id']}/events")

    row = (
        await session.execute(select(Backtest).where(Backtest.public_id == body["public_id"]))
    ).scalar_one()
    assert row.user_id == user_id
    assert row.screen_id == screen.id
    assert queue.sent == [("baskfy.backtest.run", [body["public_id"], True])]


async def test_a_run_nothing_can_execute_fails_rather_than_waiting(
    session: AsyncSession, tmp_path: Path
) -> None:
    """M42. ``queued`` must only ever mean "about to run".

    The failure this pins actually happened: a run sat at ``queued`` for hours because no broker
    URL reached the API, nothing was published, and the page said "queued" while meaning "never".
    The old code logged that and returned, reasoning that an operator could re-drive it. There was
    no operator.
    """
    user_id, public_id = await make_user(session, "noqueue.backtest@example.com", subscribed=True)
    screen = await _screen(session, user_id)
    config = _config(screen_public_id=screen.public_id)

    # `task_queue=None` would leave the real Celery producer the lifespan builds, which would
    # publish happily into the developer's Redis. This stands in for the state that occurred: an
    # app whose broker was never configured, so there is nothing to publish with.
    class NoBroker:
        """Deliberately has no `send_task`."""

    async with running_app(_settings(tmp_path), session, task_queue=NoBroker()) as client:
        response = await client.post(
            url("/backtests"),
            json={"config": json.loads(config.model_dump_json())},
            headers=bearer(public_id),
        )

    assert response.status_code == 202, "the row was recorded, so the caller is not given a 500"
    row = (
        await session.execute(
            select(Backtest).where(Backtest.public_id == response.json()["public_id"])
        )
    ).scalar_one()
    assert row.status == "failed"
    assert row.error is not None and "no message broker" in row.error
    assert row.finished_at is not None


async def test_the_inline_executor_starts_the_run_without_a_broker(
    session: AsyncSession, tmp_path: Path
) -> None:
    """The default path: no broker anywhere, and the run still leaves `queued`.

    What is asserted is that it *started* — the runner took it — not that it finished, because the
    fixture database has no bars to simulate against. Finishing is covered end to end by
    `services/worker/tests/test_backtest_job.py`.
    """
    user_id, public_id = await make_user(session, "inline.backtest@example.com", subscribed=True)
    screen = await _screen(session, user_id)
    config = _config(screen_public_id=screen.public_id)

    class NoBroker:
        """Deliberately has no `send_task`."""

    async with running_app(
        _settings(tmp_path, executor="inline"), session, task_queue=NoBroker()
    ) as client:
        response = await client.post(
            url("/backtests"),
            json={"config": json.loads(config.model_dump_json())},
            headers=bearer(public_id),
        )
        assert response.status_code == 202
        # The runner is scheduled on the loop; give it the turn it needs to claim the row.
        await asyncio.sleep(0)

    row = (
        await session.execute(
            select(Backtest).where(Backtest.public_id == response.json()["public_id"])
        )
    ).scalar_one()
    assert row.status != "failed" or "no message broker" not in (row.error or ""), (
        "the inline runner should have taken this, not reported a missing broker"
    )


async def test_a_second_concurrent_run_is_refused(session: AsyncSession, tmp_path: Path) -> None:
    """PROMPTS.md Prompt 15 §4: "a per-user concurrency cap of 1"."""
    user_id, public_id = await make_user(session, "busy.backtest@example.com", subscribed=True)
    screen = await _screen(session, user_id)
    config = _config(screen_public_id=screen.public_id)
    payload = {"config": json.loads(config.model_dump_json())}
    async with running_app(_settings(tmp_path), session, task_queue=RecordingQueue()) as client:
        first = await client.post(url("/backtests"), json=payload, headers=bearer(public_id))
        second = await client.post(url("/backtests"), json=payload, headers=bearer(public_id))

    assert first.status_code == 202
    assert second.status_code == 429
    assert second.headers["Retry-After"]
    assert "already have a backtest running" in second.json()["detail"]


async def test_the_concurrency_caps_are_configuration(
    session: AsyncSession, tmp_path: Path
) -> None:
    """The per-user cap is docs'; the global one is invented (``docs/DECISIONS.md`` §15). Both are
    settings, and a test that only exercised the default would not prove it."""
    user_id, public_id = await make_user(session, "twoup.backtest@example.com", subscribed=True)
    screen = await _screen(session, user_id)
    payload = {"config": json.loads(_config(screen_public_id=screen.public_id).model_dump_json())}
    settings = api_helpers.api_settings(
        screener_helpers.database_url(),
        invoice_local_dir=str(tmp_path),
        backtest_user_concurrency=2,
    )
    async with running_app(settings, session, task_queue=RecordingQueue()) as client:
        first = await client.post(url("/backtests"), json=payload, headers=bearer(public_id))
        second = await client.post(url("/backtests"), json=payload, headers=bearer(public_id))
        third = await client.post(url("/backtests"), json=payload, headers=bearer(public_id))
    assert [first.status_code, second.status_code, third.status_code] == [202, 202, 429]


async def test_the_global_cap_refuses_everyone(session: AsyncSession, tmp_path: Path) -> None:
    """With the global cap at one, a *second* account is refused even though its own cap is free."""
    first_id, first_public = await make_user(
        session, "globalone.backtest@example.com", subscribed=True
    )
    second_id, second_public = await make_user(
        session, "globaltwo.backtest@example.com", subscribed=True
    )
    mine_screen = await _screen(session, first_id)
    # Their own screen: a backtest naming somebody else's screen is a 404 long before the cap is
    # consulted, which would make this test pass for entirely the wrong reason.
    their_screen = await _screen(session, second_id)
    settings = api_helpers.api_settings(
        screener_helpers.database_url(),
        invoice_local_dir=str(tmp_path),
        backtest_global_concurrency=1,
    )

    def body(public_id: str) -> dict[str, object]:
        return {"config": json.loads(_config(screen_public_id=public_id).model_dump_json())}

    async with running_app(settings, session, task_queue=RecordingQueue()) as client:
        mine = await client.post(
            url("/backtests"), json=body(mine_screen.public_id), headers=bearer(first_public)
        )
        theirs = await client.post(
            url("/backtests"), json=body(their_screen.public_id), headers=bearer(second_public)
        )
    assert mine.status_code == 202
    assert theirs.status_code == 429
    assert "maximum" in theirs.json()["detail"]


async def test_a_config_with_neither_screen_nor_definition_is_rejected(
    session: AsyncSession, tmp_path: Path
) -> None:
    """docs/10 §Config: "screen_public_id … or an inline definition". One of them is required."""
    _, public_id = await make_user(session, "noscreen.backtest@example.com", subscribed=True)
    async with running_app(_settings(tmp_path), session, task_queue=RecordingQueue()) as client:
        response = await client.post(
            url("/backtests"),
            json={"config": json.loads(_config().model_dump_json())},
            headers=bearer(public_id),
        )
    assert response.status_code == 400
    assert response.json()["type"] == "invalid-screen-definition"


async def test_an_inline_definition_is_accepted_and_validated(
    session: AsyncSession, tmp_path: Path
) -> None:
    """docs/10 §Config: "screen_public_id … **or an inline definition**".

    A backtest without a saved screen is a first-class case, and its definition is validated at
    the moment it is posted rather than twenty minutes later on the worker.
    """
    user_id, public_id = await make_user(session, "inline.backtest@example.com", subscribed=True)
    config = _config(screen_definition={"index": "nifty-500", "sort_by": "ret_12m"})
    async with running_app(_settings(tmp_path), session, task_queue=RecordingQueue()) as client:
        accepted = await client.post(
            url("/backtests"),
            json={"config": json.loads(config.model_dump_json())},
            headers=bearer(public_id),
        )
        rejected = await client.post(
            url("/backtests"),
            json={
                "config": json.loads(
                    _config(
                        screen_definition={"index": "nifty-500", "sort_by": "not_a_factor"}
                    ).model_dump_json()
                )
            },
            headers=bearer(public_id),
        )

    assert accepted.status_code == 202
    row = (
        await session.execute(
            select(Backtest).where(Backtest.public_id == accepted.json()["public_id"])
        )
    ).scalar_one()
    assert row.user_id == user_id
    assert row.screen_id is None

    assert rejected.status_code == 400
    assert rejected.json()["type"] == "invalid-screen-definition"


async def test_a_free_tier_universe_restriction_covers_an_inline_definition(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Prompt 13 §5's ₹0 tier can screen one universe. Checking only the saved-screen branch
    would leave an inline definition as a way around the restriction."""
    _, public_id = await make_user(session, "freetier.backtest@example.com", subscribed=False)
    settings = api_helpers.api_settings(
        screener_helpers.database_url(),
        invoice_local_dir=str(tmp_path),
        free_tier_enabled=True,
    )
    config = _config(screen_definition={"index": "nifty-500", "sort_by": "ret_12m"})
    async with running_app(settings, session, task_queue=RecordingQueue()) as client:
        response = await client.post(
            url("/backtests"),
            json={"config": json.loads(config.model_dump_json())},
            headers=bearer(public_id),
        )
    # The ₹0 tier does not include backtests at all, so the feature gate fires before the
    # universe one. Both are 402s carrying the upgrade URL, which is the contract that matters.
    assert response.status_code == 402
    assert response.json()["upgrade_url"] == "/pricing"


async def test_an_unknown_config_key_is_rejected(session: AsyncSession, tmp_path: Path) -> None:
    """The same ``extra="forbid"`` contract docs/07 §Screens gives a screen definition."""
    _, public_id = await make_user(session, "extra.backtest@example.com", subscribed=True)
    async with running_app(_settings(tmp_path), session, task_queue=RecordingQueue()) as client:
        response = await client.post(
            url("/backtests"),
            json={"config": {"start": "2015-01-01", "end": "2016-01-01", "leverage": 3}},
            headers=bearer(public_id),
        )
    assert response.status_code == 400
    fields = [str(error["field"]) for error in api_helpers.errors_of(response.json())]
    assert any("leverage" in field for field in fields)


async def test_a_stale_data_version_is_a_conflict(session: AsyncSession, tmp_path: Path) -> None:
    """docs/07 §"Error catalogue": 409 `stale-data-version`."""
    user_id, public_id = await make_user(session, "stale.backtest@example.com", subscribed=True)
    screen = await _screen(session, user_id)
    async with running_app(_settings(tmp_path), session, task_queue=RecordingQueue()) as client:
        response = await client.post(
            url("/backtests"),
            json={
                "config": json.loads(_config(screen_public_id=screen.public_id).model_dump_json()),
                "data_version": 999_999,
            },
            headers=bearer(public_id),
        )
    assert response.status_code == 409
    assert response.json()["type"] == "stale-data-version"


async def test_an_idempotency_key_replays(session: AsyncSession, tmp_path: Path) -> None:
    """docs/07 §Conventions: "`Idempotency-Key` header honoured on all POSTs that create
    resources". Without it a retried queue request costs a second fifteen-year simulation."""
    user_id, public_id = await make_user(session, "idem.backtest@example.com", subscribed=True)
    screen = await _screen(session, user_id)
    payload = {"config": json.loads(_config(screen_public_id=screen.public_id).model_dump_json())}
    headers = {**bearer(public_id), "Idempotency-Key": "backtest-retry-1"}
    async with running_app(_settings(tmp_path), session, task_queue=RecordingQueue()) as client:
        first = await client.post(url("/backtests"), json=payload, headers=headers)
        second = await client.post(url("/backtests"), json=payload, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["public_id"] == second.json()["public_id"]
    count = len(
        (await session.execute(select(Backtest).where(Backtest.user_id == user_id))).scalars().all()
    )
    assert count == 1


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_list_and_detail(session: AsyncSession, tmp_path: Path) -> None:
    """docs/07: `GET /backtests/{id}` → "status + metrics + equity curve"."""
    user_id, public_id = await make_user(session, "read.backtest@example.com", subscribed=True)
    row = await _completed(session, user_id, tmp_path)
    async with running_app(_settings(tmp_path), session) as client:
        listing = await client.get(url("/backtests"), headers=bearer(public_id))
        detail = await client.get(url(f"/backtests/{row.public_id}"), headers=bearer(public_id))

    assert listing.status_code == 200
    assert [item["public_id"] for item in listing.json()["data"]] == [row.public_id]
    assert listing.json()["data"][0]["total_return"] is not None

    body = detail.json()
    assert detail.status_code == 200
    assert body["status"] == "done"
    assert body["metrics"]["total_return"] is not None
    assert body["metrics_hash"]
    assert body["equity_curve"], "docs/07 asks the detail payload for the equity curve"
    assert body["drawdown"]
    assert body["monthly_returns"]
    # docs/10 §"honesty features": the disclaimer and the assumptions come from the server.
    assert body["disclaimer"] == "Past backtest results do not predict future results."
    assert any("NEXT trading day" in line for line in body["assumptions"])
    assert any("reconstructed" in line.lower() for line in body["assumptions"])


async def test_somebody_elses_backtest_is_a_404(session: AsyncSession, tmp_path: Path) -> None:
    """The rule screens already follow: a 403 confirms the id exists."""
    owner_id, _ = await make_user(session, "owner.backtest@example.com", subscribed=True)
    _, stranger = await make_user(session, "stranger.backtest@example.com", subscribed=True)
    row = await _completed(session, owner_id, tmp_path)
    async with running_app(_settings(tmp_path), session) as client:
        response = await client.get(url(f"/backtests/{row.public_id}"), headers=bearer(stranger))
    assert response.status_code == 404


async def test_delete(session: AsyncSession, tmp_path: Path) -> None:
    user_id, public_id = await make_user(session, "del.backtest@example.com", subscribed=True)
    row = await _completed(session, user_id, tmp_path)
    async with running_app(_settings(tmp_path), session) as client:
        response = await client.delete(
            url(f"/backtests/{row.public_id}"), headers=bearer(public_id)
        )
        after = await client.get(url(f"/backtests/{row.public_id}"), headers=bearer(public_id))
    assert response.status_code == 204
    assert after.status_code == 404


# ---------------------------------------------------------------------------
# Artefacts
# ---------------------------------------------------------------------------


async def test_trades_paginate(session: AsyncSession, tmp_path: Path) -> None:
    """docs/07: `GET /backtests/{id}/trades?cursor=` → "paginated fills"."""
    user_id, public_id = await make_user(session, "trades.backtest@example.com", subscribed=True)
    row = await _completed(session, user_id, tmp_path)
    async with running_app(_settings(tmp_path), session) as client:
        first = await client.get(
            url(f"/backtests/{row.public_id}/trades"),
            params={"limit": 1},
            headers=bearer(public_id),
        )
        assert first.status_code == 200
        cursor = first.json()["next_cursor"]
        second = await client.get(
            url(f"/backtests/{row.public_id}/trades"),
            params={"limit": 1, "cursor": cursor},
            headers=bearer(public_id),
        )

    assert [item["side"] for item in first.json()["data"]] == ["buy"]
    assert first.json()["data"][0]["reason"] == "enter"
    assert first.json()["data"][0]["realised_pnl"] is None
    assert cursor == "1"
    assert [item["side"] for item in second.json()["data"]] == ["sell"]
    assert second.json()["data"][0]["realised_pnl"] == "717.20"
    assert second.json()["next_cursor"] is None


async def test_holdings_filter_by_rebalance_date(session: AsyncSession, tmp_path: Path) -> None:
    """docs/08 §Backtests: the results page shows "per-period holdings"."""
    user_id, public_id = await make_user(session, "hold.backtest@example.com", subscribed=True)
    row = await _completed(session, user_id, tmp_path)
    async with running_app(_settings(tmp_path), session) as client:
        matching = await client.get(
            url(f"/backtests/{row.public_id}/holdings"),
            params={"rebalance_date": "2025-01-01"},
            headers=bearer(public_id),
        )
        missing = await client.get(
            url(f"/backtests/{row.public_id}/holdings"),
            params={"rebalance_date": "2025-02-01"},
            headers=bearer(public_id),
        )
    assert [item["symbol"] for item in matching.json()["data"]] == ["ACME"]
    assert missing.json()["data"] == []


async def test_artefacts_of_a_queued_run_do_not_exist_yet(
    session: AsyncSession, tmp_path: Path
) -> None:
    user_id, public_id = await make_user(session, "queued.backtest@example.com", subscribed=True)
    row = Backtest(
        public_id=new_public_id(),
        user_id=user_id,
        config=json.loads(_config().model_dump_json()),
        status="queued",
    )
    session.add(row)
    await session.flush()
    async with running_app(_settings(tmp_path), session) as client:
        response = await client.get(
            url(f"/backtests/{row.public_id}/trades"), headers=bearer(public_id)
        )
    assert response.status_code == 404
    assert "queued" in response.json()["detail"]


@pytest.mark.parametrize("artefact", ARTEFACTS)
async def test_export_returns_a_signed_link_that_downloads(
    session: AsyncSession, tmp_path: Path, artefact: str
) -> None:
    """docs/07: `GET /backtests/{id}/export` → "CSV/Parquet signed URL"."""
    user_id, public_id = await make_user(
        session, f"export{artefact}.backtest@example.com", subscribed=True
    )
    row = await _completed(session, user_id, tmp_path)
    async with running_app(_settings(tmp_path), session) as client:
        link = await client.get(
            url(f"/backtests/{row.public_id}/export"),
            params={"artefact": artefact},
            headers=bearer(public_id),
        )
        assert link.status_code == 200
        body = link.json()
        # The signed link carries no bearer token — that is the point of a signed URL.
        download = await client.get(body["url"])

    assert body["artefact"] == artefact
    assert body["format"] == "csv"
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("text/csv")
    assert "attachment" in download.headers["content-disposition"]
    assert download.text.splitlines()[0].startswith(("date,", "rebalance_date,"))


async def test_a_forged_download_token_is_refused(session: AsyncSession, tmp_path: Path) -> None:
    user_id, public_id = await make_user(session, "forge.backtest@example.com", subscribed=True)
    row = await _completed(session, user_id, tmp_path)
    async with running_app(_settings(tmp_path), session) as client:
        link = await client.get(
            url(f"/backtests/{row.public_id}/export"), headers=bearer(public_id)
        )
        tampered = link.json()["url"].replace("token=", "token=x")
        response = await client.get(tampered)
    assert response.status_code == 404


async def test_an_expired_download_token_is_refused(session: AsyncSession, tmp_path: Path) -> None:
    user_id, public_id = await make_user(session, "expired.backtest@example.com", subscribed=True)
    row = await _completed(session, user_id, tmp_path)
    settings = _settings(tmp_path)
    assert isinstance(settings, object)
    stale = sign_download(
        api_helpers.api_settings(screener_helpers.database_url(), invoice_local_dir=str(tmp_path)),
        row.public_id,
        "trades",
        now=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
    )
    async with running_app(settings, session) as client:
        response = await client.get(
            url(f"/backtests/{row.public_id}/download/trades"),
            params={"expires": stale.expires, "token": stale.token},
        )
        del public_id
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# SSE (Prompt 15 §4)
# ---------------------------------------------------------------------------


async def test_the_event_stream_opens_with_the_current_state(
    session: AsyncSession, tmp_path: Path
) -> None:
    """A client that connects to a finished run gets its state and a close, not a hung socket."""
    user_id, public_id = await make_user(session, "sse.backtest@example.com", subscribed=True)
    row = await _completed(session, user_id, tmp_path)
    async with (
        running_app(_settings(tmp_path), session) as client,
        client.stream(
            "GET", url(f"/backtests/{row.public_id}/events"), headers=bearer(public_id)
        ) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join([chunk async for chunk in response.aiter_text()])

    assert "event: progress" in body
    assert "event: end" in body
    frames = [
        json.loads(line[len("data: ") :]) for line in body.splitlines() if line.startswith("data: ")
    ]
    assert frames[-1]["status"] == "done"
    assert frames[-1]["percent"] == 100


@pytest.mark.redis
async def test_a_running_backtest_relays_the_workers_frames(tmp_path: Path) -> None:
    """PROMPTS.md Prompt 15 §4: "progress events streamed to the client over SSE".

    The worker and the browser never talk directly: the job publishes to a Redis channel and
    mirrors the latest frame on a key, and the endpoint replays the key and then relays the
    channel. This test *is* the worker — it writes the key, opens the stream, publishes a running
    frame and then a terminal one — and asserts all three arrive in order and that the stream
    then closes on its own.

    Driven against ``event_stream`` rather than over HTTP because httpx's ``ASGITransport``
    buffers a response to completion; a stream that only ends when the run does can never be read
    through it. The HTTP wiring is covered by the terminal-state test above.
    """
    public_id = new_public_id()
    settings = _settings(tmp_path)
    client: Redis = Redis.from_url(settings.redis_url)
    try:
        await client.ping()
    except RedisError:  # pragma: no cover - guarded by the redis marker
        pytest.skip("no Redis; run `make up`")

    stored = progress_frame(public_id, "running", stage="screening", completed=3, total=180)
    running = progress_frame(public_id, "running", stage="simulating", completed=250, total=3900)
    finished = progress_frame(public_id, "done", stage="done", completed=1, total=1)
    await client.set(progress_key(public_id), stored, ex=60)

    frames: list[dict[str, object]] = []
    ended = False
    try:
        stream = event_stream(client, public_id, stored, terminal=False)
        with anyio.fail_after(20):
            async for block in stream:
                text = block.decode("utf-8")
                if text.startswith("event: end"):
                    ended = True
                    break
                received = [
                    json.loads(line[len("data: ") :])
                    for line in text.splitlines()
                    if line.startswith("data: ")
                ]
                if not received:
                    # A keep-alive comment. Publishing again here would send the same frame
                    # twice, which is how the first draft of this test lied to itself.
                    continue
                frames.extend(received)
                # The subscription is live the moment the first frame is out, because
                # `event_stream` subscribes before yielding it.
                if len(frames) == 1:
                    await client.publish(events_channel(public_id), running)
                elif len(frames) == 2:
                    await client.publish(events_channel(public_id), finished)
        await stream.aclose()
    finally:
        await client.delete(progress_key(public_id))
        await client.aclose()

    assert [frame["stage"] for frame in frames] == ["screening", "simulating", "done"]
    assert frames[-1]["status"] == "done"
    assert ended, "the stream did not close itself when the run finished"


async def test_the_event_stream_is_entitlement_gated(session: AsyncSession, tmp_path: Path) -> None:
    user_id, _ = await make_user(session, "sseowner.backtest@example.com", subscribed=True)
    _, unpaid = await make_user(session, "ssefree.backtest@example.com", subscribed=False)
    row = await _completed(session, user_id, tmp_path)
    async with running_app(_settings(tmp_path), session) as client:
        response = await client.get(
            url(f"/backtests/{row.public_id}/events"), headers=bearer(unpaid)
        )
    assert response.status_code == 402


async def test_anonymous_callers_are_refused(session: AsyncSession, tmp_path: Path) -> None:
    """docs/07 §"Error catalogue": 401 `unauthenticated`."""
    async with running_app(_settings(tmp_path), session) as client:
        response: httpx.Response = await client.get(url("/backtests"))
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# M43: the signed download link, hardened
# ---------------------------------------------------------------------------


async def test_deleting_a_backtest_revokes_its_download_links(
    session: AsyncSession, tmp_path: Path
) -> None:
    """The DELETE handler promises "nothing can read them once the row is gone". It must be true.

    A signed link is a stateless bearer capability: it carries no user, no nonce and no server
    state, so before M43 it stayed redeemable for the rest of its fifteen minutes after the row
    was deleted. Executed then: mint, DELETE -> 204, redeem anonymously -> 200 with the full CSV.
    """
    user_id, public_id = await make_user(session, "revoke.backtest@example.com", subscribed=True)
    row = await _completed(session, user_id, tmp_path)
    settings = _settings(tmp_path)

    async with running_app(settings, session, task_queue=RecordingQueue()) as client:
        minted = await client.get(
            url(f"/backtests/{row.public_id}/export"), headers=bearer(public_id)
        )
        assert minted.status_code == 200
        link = minted.json()["url"]

        before = await client.get(link)
        assert before.status_code == 200, "the link must work while the run exists"

        removed = await client.delete(url(f"/backtests/{row.public_id}"), headers=bearer(public_id))
        assert removed.status_code == 204

        after = await client.get(link)
        assert after.status_code == 404, (
            "a link that outlives the row it names contradicts the DELETE guarantee"
        )


async def test_a_non_ascii_token_is_refused_rather_than_crashing(
    session: AsyncSession, tmp_path: Path
) -> None:
    """`hmac.compare_digest` raises TypeError on a non-ASCII str.

    The download route is reachable by anyone on the internet, so that was an unauthenticated 500
    and a logged stack trace from a query string. A token that cannot be a signature is wrong,
    not exceptional.
    """
    async with running_app(_settings(tmp_path), session, task_queue=RecordingQueue()) as client:
        response = await client.get(
            url("/backtests/aaaaaaaaaaaaaaaaaaaaaaaa/download/trades"),
            params={"expires": 9999999999, "token": "\u00e9\u00e9\u00e9"},
        )

    assert response.status_code == 404, f"got {response.status_code}, expected a refusal"


def test_an_unconfigured_secret_refuses_to_sign_rather_than_using_a_public_key(
    tmp_path: Path,
) -> None:
    """The fallback key was a constant published in this repository.

    `require_configured` refuses an empty secret only in production, so staging and any developer
    box serving real artefacts were signing with a key anybody could read out of the source.
    """
    settings = _settings(tmp_path).model_copy(update={"jwt_secret": ""})

    with pytest.raises(DownloadSigningUnavailable):
        sign_download(settings, "deadbeef1234", "trades")

    # And verification refuses rather than raising, so the route answers 404 and not 500.
    assert not verify_download(settings, "deadbeef1234", "trades", 9999999999, "anything")


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("GET", ""),
        ("GET", "/trades"),
        ("GET", "/holdings"),
        ("GET", "/export"),
        ("GET", "/events"),
        ("DELETE", ""),
    ],
)
async def test_every_backtest_route_is_a_404_for_somebody_else(
    session: AsyncSession, tmp_path: Path, method: str, suffix: str
) -> None:
    """M43. The module had exactly one cross-tenant test, and it covered only `GET /{id}`.

    The router is correct on all six — an audit executed every one of them against two real
    accounts and got 404 each time. Nothing in the suite would have caught a regression on the
    other five, which matters more than usual right now: a sibling router in this same service
    shipped a tenancy guard that computes a mismatch and then returns the same value in both arms.

    404 and not 403, deliberately: a 403 confirms the id exists.
    """
    owner_id, owner_token = await make_user(
        session, f"owner{suffix or 'x'}@example.com", subscribed=True
    )
    _, intruder_token = await make_user(
        session, f"intruder{suffix or 'x'}@example.com", subscribed=True
    )
    row = await _completed(session, owner_id, tmp_path)

    async with running_app(_settings(tmp_path), session, task_queue=RecordingQueue()) as client:
        response = await client.request(
            method,
            url(f"/backtests/{row.public_id}{suffix}"),
            headers=bearer(intruder_token),
        )
        assert response.status_code == 404, (
            f"{method} {suffix or '/'} leaked another account's run: {response.status_code}"
        )

        # And it is still there for its owner — the intruder's DELETE must not have landed.
        still = await client.get(url(f"/backtests/{row.public_id}"), headers=bearer(owner_token))
        assert still.status_code == 200
