"""SC4 Beat entry for curated-basket dividend derivation (leaf 3.4)."""

from __future__ import annotations

import datetime as dt
import inspect
from decimal import Decimal
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from celery.schedules import crontab

from baskfy_core.curated_dividends import DIVIDEND_SOURCE, DividendRow
from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_COMPUTE, TASK_ROUTES, build_celery
from baskfy_worker.settings import WorkerSettings
from baskfy_worker.tasks import curated_dividends
from baskfy_worker.tasks.curated_dividends import run_curated_dividends


def _settings() -> WorkerSettings:
    return WorkerSettings(
        _env_file=None,
        redis_url="redis://localhost:6380/0",
        celery_broker_url="",
        task_max_retries=3,
        task_retry_backoff_seconds=60,
    )


def test_cb_dividends_beat_entry_exists() -> None:
    entry = BEAT_SCHEDULE["cb-dividends"]
    assert entry["task"] == "baskfy.cb.derive_dividends"
    schedule = cast(crontab, entry["schedule"])
    assert schedule.hour == {20}
    assert schedule.minute == {25}
    # `BEAT_SCHEDULE` is `dict[str, dict[str, object]]`, so `entry["options"]` is `object`.
    # Narrowing it is the assertion that the entry has an options mapping at all — the
    # alternative was a `cast`, which would assert the shape instead of checking it.
    options = entry["options"]
    assert isinstance(options, dict), "the beat entry must carry an options mapping"
    assert options["queue"] == QUEUE_COMPUTE
    # Sibling SC2 / SC7 entries must survive the merge.
    assert BEAT_SCHEDULE["cb-eod-metrics"]["task"] == "baskfy.cb.compute_metrics"
    assert BEAT_SCHEDULE["cb-sip-reminders"]["task"] == "baskfy.cb.sip_reminders"


def test_cb_dividends_task_is_registered_and_routed() -> None:
    app = build_celery(_settings())
    assert "baskfy.cb.derive_dividends" in app.tasks
    assert TASK_ROUTES["baskfy.cb.*"]["queue"] == QUEUE_COMPUTE


def test_dividends_task_source_has_no_broker_path() -> None:
    source = inspect.getsource(curated_dividends)
    for forbidden in ("OrderGateway", "place_order", "kc.place_order"):
        assert forbidden not in source


@pytest.mark.asyncio
async def test_run_derives_and_persists_idempotently() -> None:
    as_of = dt.date(2026, 8, 21)

    investment = MagicMock()
    investment.id = 3
    investment.status = "ACTIVE"
    investment.created_at = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)

    holding = MagicMock()
    holding.investment_id = 3
    holding.instrument_id = 101
    holding.qty = Decimal("10")

    action = MagicMock()
    action.instrument_id = 101
    action.ex_date = dt.date(2025, 6, 15)
    action.amount = Decimal("2.50")
    action.action_type = "dividend"

    inv_scalars = MagicMock()
    inv_scalars.all = MagicMock(return_value=[investment])
    hold_scalars = MagicMock()
    hold_scalars.all = MagicMock(return_value=[holding])
    action_scalars = MagicMock()
    action_scalars.all = MagicMock(return_value=[action])
    existing_scalars = MagicMock()
    existing_scalars.all = MagicMock(return_value=[])

    session = AsyncMock()
    session.scalars = AsyncMock(
        side_effect=[inv_scalars, hold_scalars, action_scalars, existing_scalars]
    )
    session.add = MagicMock()
    session.flush = AsyncMock()

    row = DividendRow(
        instrument_id=101,
        ex_date=dt.date(2025, 6, 15),
        amount_per_share=Decimal("2.50"),
        qty_held=Decimal("10"),
        total=Decimal("25.00"),
        source=DIVIDEND_SOURCE,
    )

    with patch.object(curated_dividends, "derive_dividends", return_value=(row,)) as derive:
        first = await run_curated_dividends(session, as_of)

    derive.assert_called_once()
    assert first["inserted"] == 1
    assert first["derived"] == 1
    session.add.assert_called_once()
    pending = session.add.call_args.args[0]
    assert pending.investment_id == 3
    assert pending.instrument_id == 101
    assert pending.ex_date == dt.date(2025, 6, 15)
    assert pending.source == DIVIDEND_SOURCE

    # Second pass: existing key → skip insert.
    existing = MagicMock()
    existing.investment_id = 3
    existing.instrument_id = 101
    existing.ex_date = dt.date(2025, 6, 15)
    existing_scalars.all = MagicMock(return_value=[existing])
    session.add.reset_mock()
    session.scalars = AsyncMock(
        side_effect=[inv_scalars, hold_scalars, action_scalars, existing_scalars]
    )

    with patch.object(curated_dividends, "derive_dividends", return_value=(row,)):
        second = await run_curated_dividends(session, as_of)

    assert second["inserted"] == 0
    assert second["skipped_existing"] == 1
    session.add.assert_not_called()
