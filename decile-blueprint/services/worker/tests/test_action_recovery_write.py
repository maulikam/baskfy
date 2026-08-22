"""Writing recovered corporate actions, and rebuilding the series from them (M28).

`baskfy_core.action_recovery` decides *what* was recovered; this module decides what is allowed
to reach the database, and both halves can be wrong in ways that do not fail loudly. A wrongly
written split silently rewrites a price history that a live strategy ranks on.

Four properties matter enough to be asserted rather than reviewed:

* **only share-count actions are written** — M27 measured that the corpus computes a price
  return, so writing a dividend would adopt a different convention rather than fix data;
* **a recovered row never overwrites a sourced one** — the NSE feed outranks an inference;
* **every row is reversible by one predicate**, which is the whole basis on which writing to a
  table the parity gates read was acceptable at all;
* **the write actually corrects the series** — a stored action that does not change `close` has
  done nothing.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from fractions import Fraction

import pytest
from helpers import add_bar, make_instrument, requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.action_recovery import CASH, SHARE_COUNT, RecoveredAction
from baskfy_core.models import CorporateAction, OhlcvDaily
from baskfy_worker import action_recovery as recovery
from baskfy_worker.tasks.adjustments import reprocess_instrument

EX_DATE = dt.date(2026, 6, 15)


def split(factor: int = 5, *, confirmed: bool = True, at_edge: bool = False) -> RecoveredAction:
    return RecoveredAction(
        EX_DATE, float(factor), SHARE_COUNT, Fraction(factor), confirmed, at_edge
    )


class TestTheProvenancePayload:
    def test_it_carries_the_marker_the_reversal_predicate_matches(self) -> None:
        payload = recovery._payload("NESTLEIND", split(10))
        assert payload["source"] == recovery.SOURCE

    def test_it_records_what_was_measured_and_not_only_what_was_concluded(self) -> None:
        """A row that cannot explain where it came from is worse than no row."""
        payload = recovery._payload("CUPID", split(5))

        assert payload["symbol"] == "CUPID"
        assert payload["measured_factor"] == 5.0
        assert payload["ratio"] == "5:1"
        assert payload["confirmed"] is True

    def test_it_records_the_ambiguity_rather_than_hiding_it(self) -> None:
        """A 5:1 split and a 4:1 bonus are the same price factor. The type is a choice."""
        payload = recovery._payload("CUPID", split(5))
        assert payload["ambiguous_with"] == "bonus 4:1"
        assert "not sourced from a corporate-action feed" in str(payload["note"]).lower()

    def test_a_window_edge_action_says_so(self) -> None:
        payload = recovery._payload("NESTLEIND", split(10, confirmed=False, at_edge=True))
        assert payload["confirmed"] is False
        assert payload["at_edge"] is True


class TestWhatMayBeWritten:
    def test_a_cash_action_has_no_share_count_ratio_to_write(self) -> None:
        """The type system refuses it before the database has to.

        This is the structural half of M27's verdict: a dividend cannot be turned into a split
        row even by mistake, because it has no ratio to put in one.
        """
        dividend = RecoveredAction(EX_DATE, 1.02, CASH, None, True)
        with pytest.raises(ValueError, match="cash-shaped"):
            recovery._payload("TATASTEEL", dividend)


@requires_db
@pytest.mark.db
class TestAgainstTheDatabase:
    async def _instrument_with_a_split(self, session: AsyncSession) -> int:
        """100 for ten days, then 20 — a 5:1 split nobody told the database about."""
        instrument_id = await make_instrument(session, "SPLITCO", token=999001)
        for day in range(20):
            on = dt.date(2026, 6, 1) + dt.timedelta(days=day)
            await add_bar(session, instrument_id, on, "100" if on < EX_DATE else "20")
        await session.flush()
        return instrument_id

    async def test_writing_then_reprocessing_corrects_the_adjusted_series(
        self, session: AsyncSession
    ) -> None:
        """The point of the whole module: a stored action becomes a corrected price."""
        instrument_id = await self._instrument_with_a_split(session)

        before = (
            await session.execute(
                select(OhlcvDaily.close).where(
                    OhlcvDaily.instrument_id == instrument_id,
                    OhlcvDaily.date == dt.date(2026, 6, 2),
                )
            )
        ).scalar_one()
        assert before == Decimal("100.0000"), "the raw print, unadjusted"

        await recovery._write(session, instrument_id, "SPLITCO", [split(5)])
        await reprocess_instrument(session, instrument_id)
        await session.flush()

        after = (
            await session.execute(
                select(OhlcvDaily.close).where(
                    OhlcvDaily.instrument_id == instrument_id,
                    OhlcvDaily.date == dt.date(2026, 6, 2),
                )
            )
        ).scalar_one()
        assert after == Decimal("20.0000"), "adjusted back through the split"

        raw_print = (
            await session.execute(
                select(OhlcvDaily.close_raw).where(
                    OhlcvDaily.instrument_id == instrument_id,
                    OhlcvDaily.date == dt.date(2026, 6, 2),
                )
            )
        ).scalar_one()
        assert raw_print == Decimal("100.0000"), "close_raw is the exchange print and never moves"

    async def test_writing_twice_inserts_once(self, session: AsyncSession) -> None:
        """House rule 7. A re-run of the recovery must not pile up duplicate actions."""
        instrument_id = await self._instrument_with_a_split(session)

        first = await recovery._write(session, instrument_id, "SPLITCO", [split(5)])
        second = await recovery._write(session, instrument_id, "SPLITCO", [split(5)])

        assert (first, second) == (1, 0)
        stored = (
            (
                await session.execute(
                    select(CorporateAction).where(CorporateAction.instrument_id == instrument_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(stored) == 1

    async def test_a_recovered_row_never_overwrites_a_sourced_one(
        self, session: AsyncSession
    ) -> None:
        """NSE's own feed outranks an inference drawn from two price series."""
        instrument_id = await self._instrument_with_a_split(session)
        session.add(
            CorporateAction(
                instrument_id=instrument_id,
                action_type="split",
                ex_date=EX_DATE,
                ratio_from=Decimal(5),
                ratio_to=Decimal(1),
                raw={"source": "nse_file", "subject": "Face Value Split"},
            )
        )
        await session.flush()

        written = await recovery._write(session, instrument_id, "SPLITCO", [split(5)])

        assert written == 0
        stored = (
            (
                await session.execute(
                    select(CorporateAction.raw).where(
                        CorporateAction.instrument_id == instrument_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [r["source"] for r in stored] == ["nse_file"], "the feed's row survived untouched"

    async def test_a_sourced_action_of_a_DIFFERENT_type_still_blocks_the_date(
        self, session: AsyncSession
    ) -> None:
        """The unique constraint does not catch this, and M28's first write proved it.

        NSE recorded CUPID's 2026-03-09 event as `bonus 4:1`; the recovery derived the same event
        as `split 5:1`. Different `action_type`, so different rows, so the constraint let both in
        — and `reprocess_instrument` then applied both, turning a 5x adjustment into 25x.

        The recovered factor measures the WHOLE step at that ex-date, so anything already recorded
        there is a component of it rather than a separate action.
        """
        instrument_id = await self._instrument_with_a_split(session)
        session.add(
            CorporateAction(
                instrument_id=instrument_id,
                action_type="bonus",  # NOT "split" — the constraint will not fire
                ex_date=EX_DATE,
                ratio_from=Decimal(4),
                ratio_to=Decimal(1),
                raw={"source": "nse_file", "subject": "Bonus 4:1"},
            )
        )
        await session.flush()

        written = await recovery._write(session, instrument_id, "SPLITCO", [split(5)])

        assert written == 0, "the date was already known; writing again would double-count it"
        stored = (
            (
                await session.execute(
                    select(CorporateAction).where(CorporateAction.instrument_id == instrument_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(stored) == 1
        assert stored[0].action_type == "bonus"

        # And the series is adjusted once, not twice.
        await reprocess_instrument(session, instrument_id)
        await session.flush()
        close = (
            await session.execute(
                select(OhlcvDaily.close).where(
                    OhlcvDaily.instrument_id == instrument_id,
                    OhlcvDaily.date == dt.date(2026, 6, 2),
                )
            )
        ).scalar_one()
        assert close == Decimal("20.0000"), "5x once, not 25x"

    async def test_every_written_row_is_reversible_by_one_predicate(
        self, session: AsyncSession
    ) -> None:
        """The basis on which writing to a table the parity gates read was acceptable."""
        instrument_id = await self._instrument_with_a_split(session)
        await recovery._write(session, instrument_id, "SPLITCO", [split(5)])
        await reprocess_instrument(session, instrument_id)
        await session.flush()

        stored = (
            (
                await session.execute(
                    select(CorporateAction).where(CorporateAction.instrument_id == instrument_id)
                )
            )
            .scalars()
            .all()
        )
        assert stored and all(row.raw["source"] == recovery.SOURCE for row in stored)

        for row in stored:
            await session.delete(row)
        await session.flush()
        await reprocess_instrument(session, instrument_id)
        await session.flush()

        restored = (
            await session.execute(
                select(OhlcvDaily.close).where(
                    OhlcvDaily.instrument_id == instrument_id,
                    OhlcvDaily.date == dt.date(2026, 6, 2),
                )
            )
        ).scalar_one()
        assert restored == Decimal("100.0000"), "deleting the row puts the series back exactly"
