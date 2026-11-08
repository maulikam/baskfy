"""The instrument master is upserted in batches, because PostgreSQL has a bind-parameter ceiling.

Until 22 Aug 2026 `run_refresh_instruments` sent every row in one statement. That worked for as
long as the step had only ever been run against the 40-instrument fixture; the first run against
the real Kite dump — which lists every NSE instrument, not the ~2,300 the screener keeps — died on

    asyncpg.exceptions._base.InterfaceError: the number of query arguments cannot exceed 32767

These assert the spec that replaced it: batching changes how many statements are sent and nothing
else. No row is dropped, none is duplicated, and the upsert stays idempotent across the seam
between two batches — which is the only place batching can plausibly break it.
"""

from __future__ import annotations

import pytest
from helpers import requires_db
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import Instrument
from baskfy_providers.records import InstrumentRecord
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks import instruments as step


class _Provider:
    """Serves a list of instruments and nothing else, like Kite's own capability set."""

    def __init__(self, records: list[InstrumentRecord]) -> None:
        self._records = records

    def list_instruments(self) -> list[InstrumentRecord]:
        return self._records


def _records(count: int) -> list[InstrumentRecord]:
    return [
        InstrumentRecord(
            symbol=f"BATCH{index:05d}",
            name=f"BATCH {index}",
            series="EQ",
            instrument_type="EQ",
            kite_token=900000 + index,
        )
        for index in range(count)
    ]


def test_the_batch_size_stays_under_postgresqls_ceiling() -> None:
    """The guard against a future column addition walking this back over the limit."""
    assert step.UPSERT_BATCH_ROWS * step.UPSERT_COLUMNS_PER_ROW < step.MAX_BIND_PARAMETERS


def test_batched_covers_every_row_exactly_once_and_in_order() -> None:
    rows: list[dict[str, object]] = [{"n": n} for n in range(10)]
    batches = list(step._batched(rows, 3))

    assert [len(batch) for batch in batches] == [3, 3, 3, 1]
    assert [row for batch in batches for row in batch] == rows


def test_batched_on_an_empty_list_yields_nothing() -> None:
    assert list(step._batched([], 3)) == []


def test_batched_refuses_a_size_that_would_never_terminate() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        list(step._batched([{"n": 1}], 0))


@requires_db
@pytest.mark.db
class TestAgainstTheDatabase:
    """A batch size small enough that the seam is crossed many times over few rows."""

    async def test_every_row_lands_when_the_upsert_spans_many_batches(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(step, "UPSERT_BATCH_ROWS", 7)
        provider = _Provider(_records(50))

        written = await step.run_refresh_instruments(session, provider, StepOutcome())
        await session.flush()

        assert written == 50
        stored = await session.execute(
            select(func.count()).select_from(Instrument).where(Instrument.symbol.like("BATCH%"))
        )
        assert stored.scalar_one() == 50

    async def test_a_re_run_across_batches_is_still_idempotent(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """House rule 7: re-running any day's job produces identical rows."""
        monkeypatch.setattr(step, "UPSERT_BATCH_ROWS", 7)
        provider = _Provider(_records(50))

        await step.run_refresh_instruments(session, provider, StepOutcome())
        await session.flush()
        first = (
            await session.execute(
                select(Instrument.symbol, Instrument.kite_token, Instrument.updated_at)
                .where(Instrument.symbol.like("BATCH%"))
                .order_by(Instrument.symbol)
            )
        ).all()

        await step.run_refresh_instruments(session, provider, StepOutcome())
        await session.flush()
        second = (
            await session.execute(
                select(Instrument.symbol, Instrument.kite_token, Instrument.updated_at)
                .where(Instrument.symbol.like("BATCH%"))
                .order_by(Instrument.symbol)
            )
        ).all()

        # `updated_at` included deliberately: the DO UPDATE ... WHERE means an unchanged row is
        # not rewritten, and batching must not turn one no-op statement into fifty real ones.
        assert first == second
