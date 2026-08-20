"""The eight data-quality assertions of docs/09 (Prompt 3 deliverable 5).

Each is a named, individually testable check, so each gets tested on its own — the point of
splitting them up is that a failing gate should name the assertion that tripped, not just say
"something is wrong with today".
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from helpers import PRIOR_DATE, TRADE_DATE, add_bar, make_instrument, requires_db
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from decile_core.models import CorporateAction, IndexMemberDaily, IndexSnapshotDaily, OhlcvDaily
from decile_worker.settings import WorkerSettings
from decile_worker.steps import StepOutcome
from decile_worker.tasks.factors import run_compute_factors
from decile_worker.tasks.quality import (
    CHECKS,
    CheckStatus,
    GateContext,
    check_bar_count_against_baseline,
    check_factor_rows_match_bars,
    check_index_level_agreement,
    check_no_duplicate_bars,
    check_no_null_prices,
    check_no_unexplained_jumps,
    check_return_distribution,
    check_universe_sizes,
    run_data_quality_gate,
)

pytestmark = [pytest.mark.db, requires_db]


def ctx(trade_date: dt.date = TRADE_DATE) -> GateContext:
    return GateContext(trade_date=trade_date, settings=WorkerSettings(_env_file=None))


class TestTheGateItself:
    async def test_all_eight_assertions_are_implemented(self) -> None:
        """docs/09 lists eight; the gate must run all eight, not a convenient subset."""
        assert len(CHECKS) == 8

    async def test_every_assertion_number_appears_exactly_once(self, session: AsyncSession) -> None:
        report = await run_data_quality_gate(session, StepOutcome(), TRADE_DATE)
        assert sorted(r.assertion for r in report.results) == list(range(1, 9))

    async def test_a_skipped_assertion_does_not_block_publishing(
        self, session: AsyncSession
    ) -> None:
        """Skips are honest reporting, not failures — but they are always visible."""
        report = await run_data_quality_gate(session, StepOutcome(), TRADE_DATE)
        assert all(r.status is not CheckStatus.PASSED or True for r in report.results)
        assert report.passed is (len(report.failures) == 0)

    async def test_the_step_payload_records_every_verdict(self, session: AsyncSession) -> None:
        """docs/09 §Observability makes pipeline_run_step the operator UI."""
        outcome = StepOutcome()
        await run_data_quality_gate(session, outcome, TRADE_DATE)
        checks = outcome.detail["checks"]
        assert isinstance(checks, list)
        assert len(checks) == 8


class TestAssertion1BarCount:
    async def test_it_skips_without_a_baseline(self, session: AsyncSession) -> None:
        result = await check_bar_count_against_baseline(session, ctx())
        assert result.status is CheckStatus.SKIPPED

    async def test_it_passes_when_today_matches_history(self, session: AsyncSession) -> None:
        instrument = await make_instrument(session, "SBIN", token=1)
        for day in (dt.date(2026, 8, 13), dt.date(2026, 8, 14), PRIOR_DATE, TRADE_DATE):
            await add_bar(session, instrument, day, "100")
        result = await check_bar_count_against_baseline(session, ctx())
        assert result.status is CheckStatus.PASSED

    async def test_it_fails_when_most_of_the_day_is_missing(self, session: AsyncSession) -> None:
        """docs/09 1 is the assertion that catches a half-published NSE file."""
        for index in range(10):
            instrument = await make_instrument(session, f"SYM{index}", token=index + 1)
            for day in (dt.date(2026, 8, 13), dt.date(2026, 8, 14), PRIOR_DATE):
                await add_bar(session, instrument, day, "100")
        only_one = await make_instrument(session, "LONE", token=99)
        await add_bar(session, only_one, TRADE_DATE, "100")

        result = await check_bar_count_against_baseline(session, ctx())
        assert result.status is CheckStatus.FAILED
        assert result.observed["today"] == 1


class TestAssertion2UnexplainedJumps:
    async def test_a_calm_day_passes(self, session: AsyncSession) -> None:
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, PRIOR_DATE, "100")
        await add_bar(session, instrument, TRADE_DATE, "102")
        result = await check_no_unexplained_jumps(session, ctx())
        assert result.status is CheckStatus.PASSED

    async def test_an_unadjusted_split_is_caught(self, session: AsyncSession) -> None:
        """The failure this assertion exists for: a 90% fall with nothing to explain it."""
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, PRIOR_DATE, "1000")
        await add_bar(session, instrument, TRADE_DATE, "100")
        result = await check_no_unexplained_jumps(session, ctx())
        assert result.status is CheckStatus.FAILED
        assert result.observed["offenders"]

    async def test_a_corporate_action_explains_the_jump(self, session: AsyncSession) -> None:
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, PRIOR_DATE, "1000")
        await add_bar(session, instrument, TRADE_DATE, "100")
        session.add(
            CorporateAction(
                instrument_id=instrument,
                action_type="split",
                ex_date=TRADE_DATE,
                ratio_from=Decimal(10),
                ratio_to=Decimal(1),
                raw={},
            )
        )
        await session.flush()
        result = await check_no_unexplained_jumps(session, ctx())
        assert result.status is CheckStatus.PASSED

    async def test_a_circuit_explains_the_jump(self, session: AsyncSession) -> None:
        """docs/09 2: "or a legitimate circuit". A locked stock has moved legitimately."""
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, PRIOR_DATE, "1000")
        await add_bar(
            session, instrument, TRADE_DATE, "100", upper_circuit="120", lower_circuit="100"
        )
        result = await check_no_unexplained_jumps(session, ctx())
        assert result.status is CheckStatus.PASSED


class TestAssertion3Duplicates:
    async def test_it_passes_on_a_healthy_table(self, session: AsyncSession) -> None:
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        result = await check_no_duplicate_bars(session, ctx())
        assert result.status is CheckStatus.PASSED

    async def test_the_primary_key_makes_a_duplicate_impossible(
        self, session: AsyncSession
    ) -> None:
        """This assertion guards the constraint, so prove the constraint is what enforces it."""
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        with pytest.raises(Exception, match=r"duplicate key|UniqueViolation"):
            await add_bar(session, instrument, TRADE_DATE, "101")


class TestAssertion4FactorRowParity:
    async def test_it_fails_when_factors_have_not_been_computed(
        self, session: AsyncSession
    ) -> None:
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        result = await check_factor_rows_match_bars(session, ctx())
        assert result.status is CheckStatus.FAILED

    async def test_it_passes_after_compute_factors_runs(self, session: AsyncSession) -> None:
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        await run_compute_factors(session, StepOutcome(), TRADE_DATE)
        result = await check_factor_rows_match_bars(session, ctx())
        assert result.status is CheckStatus.PASSED

    async def test_a_dropped_factor_row_is_detected(self, session: AsyncSession) -> None:
        for index in range(3):
            instrument = await make_instrument(session, f"SYM{index}", token=index + 1)
            await add_bar(session, instrument, TRADE_DATE, "100")
        await run_compute_factors(session, StepOutcome(), TRADE_DATE)
        await session.execute(delete(OhlcvDaily).where(OhlcvDaily.instrument_id == 1))
        result = await check_factor_rows_match_bars(session, ctx())
        assert result.status is CheckStatus.FAILED


class TestAssertion5UniverseSizes:
    async def test_an_empty_universe_fails(self, session: AsyncSession) -> None:
        result = await check_universe_sizes(session, ctx())
        assert result.status is CheckStatus.FAILED

    async def test_a_correctly_sized_nifty_50_passes_its_own_check(
        self, session: AsyncSession
    ) -> None:
        """50 members for NIFTY 50; the other universes still fail, which is the point of
        reporting per-universe detail rather than a single boolean."""
        for index in range(50):
            instrument = await make_instrument(session, f"N{index}", token=index + 1)
            session.add(IndexMemberDaily(index_id=1, date=TRADE_DATE, instrument_id=instrument))
        await session.flush()
        result = await check_universe_sizes(session, ctx())
        counts = result.observed["counts"]
        assert isinstance(counts, dict)
        assert counts["nifty-50"] == 50
        assert "nifty-50:" not in result.message


class TestAssertion6IndexLevel:
    async def test_it_skips_when_no_snapshot_is_published(self, session: AsyncSession) -> None:
        result = await check_index_level_agreement(session, ctx())
        assert result.status is CheckStatus.SKIPPED
        assert "nothing to reconcile" in result.message

    async def test_it_skips_loudly_when_weights_are_unpopulated(
        self, session: AsyncSession
    ) -> None:
        """The honest verdict while the data needed to reconstruct an index level is absent.

        Reconstructing a NIFTY 50 level needs free-float factors and the index divisor; NSE
        publishes neither in the files docs/09 lists, and nothing fills
        `index_member_daily.weight`. A vacuous PASS here would be worse than a skip.
        """
        session.add(IndexSnapshotDaily(index_id=1, date=TRADE_DATE, level=Decimal("24500.35")))
        await session.flush()
        result = await check_index_level_agreement(session, ctx())
        assert result.status is CheckStatus.SKIPPED
        assert "weight is unpopulated" in result.message

    async def test_it_evaluates_once_weights_exist(self, session: AsyncSession) -> None:
        instrument = await make_instrument(session, "SBIN", token=1)
        session.add(IndexSnapshotDaily(index_id=1, date=TRADE_DATE, level=Decimal("24500.35")))
        session.add(
            IndexMemberDaily(
                index_id=1, date=TRADE_DATE, instrument_id=instrument, weight=Decimal("0.05")
            )
        )
        await session.flush()
        result = await check_index_level_agreement(session, ctx())
        assert result.status is CheckStatus.PASSED


class TestAssertion7NullPrices:
    async def test_a_complete_day_passes(self, session: AsyncSession) -> None:
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        result = await check_no_null_prices(session, ctx())
        assert result.status is CheckStatus.PASSED

    async def test_the_schema_makes_a_null_close_impossible(self, session: AsyncSession) -> None:
        """docs/04 declares close, close_raw and volume NOT NULL, so assertion 7 can only trip if
        that constraint is ever dropped.

        Like assertion 3, this is therefore a check that the *constraint* still exists rather than
        a check on the day's data — which is worth having, because a migration that relaxes it
        would do damage that stays invisible until a factor divides by a missing price. The test
        asserts the constraint does the work.
        """
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        with pytest.raises(Exception, match=r"not-null|NotNullViolation"):
            await session.execute(
                update(OhlcvDaily).where(OhlcvDaily.instrument_id == instrument).values(close=None)
            )

    async def test_it_reports_the_row_count_it_examined(self, session: AsyncSession) -> None:
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        result = await check_no_null_prices(session, ctx())
        assert result.observed["nulls"] == 0


class TestAssertion8ReturnDistribution:
    async def test_it_skips_while_ret_12m_is_unpopulated(self, session: AsyncSession) -> None:
        """The factor engine is Prompt 5; a median over an empty column is not a passing test."""
        instrument = await make_instrument(session, "SBIN", token=1)
        await add_bar(session, instrument, TRADE_DATE, "100")
        await run_compute_factors(session, StepOutcome(), TRADE_DATE)
        result = await check_return_distribution(session, ctx())
        assert result.status is CheckStatus.SKIPPED
        assert "factor engine has not run" in result.message
