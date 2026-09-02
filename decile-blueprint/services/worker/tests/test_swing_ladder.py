"""SW8's acceptance: the ladder closes the loop, against a real database.

`docs/swing/06` SW8, in its own words: "five simulated closes with net positive R in a GREEN tape
move the rung 0 → 1 on the next EOD run and the page says so with the five R values; three
simulated losses move it back; RED puts it at 0 with entries disallowed and the next plan shows
`GATE_RED` skips." One test each, through `run_swing_eod`, and then the ones those imply:

* the rung lands in `sw_config.exposure_level` **and** leaves an `sw_config_audit` row that says
  `swing-eod` moved it (`03` §1b) — the plan is built with that same rung;
* the evening is idempotent: run twice, the ladder climbs once (house rule 7);
* the ladder reads the paper book while execution is off (PACK.6), never a close dated after the
  session it is settling (house rule 5), and never a `CLOSED` row whose R was never written.

SW9.5 adds `04` §8.5, the drawdown containment, to the same evening: the sleeve's EOD NAV against
its peak (`sleeve_drawdown`, SW9.5.1) is settled beside the rung — a sleeve 15% under its peak
settles rung 0 with `drawdown_locked` and the plan's skips say `DRAWDOWN_LOCKOUT`; back within
10% it unlocks and starts at rung 0; between the two it stays as it was (hysteresis); the first
evening sets the peak and is never locked; the peak only rises; a re-run moves nothing; and the
night the ladder switches books (PACK.6) the paper peak is not held against the real book.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import (
    AppUser,
    OhlcvDaily,
    SwConfig,
    SwConfigAudit,
    SwFill,
    SwMarketDaily,
    SwPlan,
    SwPlanLine,
    SwPlanSkip,
    SwPosition,
    SwSetupDaily,
)
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.swing_eod import LADDER_CHANGED_BY, run_swing_eod

pytestmark = [requires_db, pytest.mark.db]

AS_OF = dt.date(2026, 8, 18)
YESTERDAY = dt.date(2026, 8, 17)
TIERS = DEFAULT_SWING_CONFIG.market.tiers

#: Five closes, net +4.50R — the module plan's "five simulated closes with net positive R".
FIVE_GOOD = ("2.00", "-1.00", "1.50", "-0.50", "2.50")
#: Two wins and then three losses in a row — `04` §8.4's `step_down_loss_streak`.
THREE_LOSSES = ("2.00", "1.50", "-1.00", "-0.80", "-1.00")


async def _user(
    session: AsyncSession,
    *,
    rung: int = 0,
    capital: str = "1000000",
    peak: str | None = None,
    locked: bool = False,
) -> int:
    user = AppUser(public_id="sw8-user", email="sw8@example.com")
    session.add(user)
    await session.flush()
    session.add(
        SwConfig(
            user_id=user.id,
            sleeve_capital_inr=Decimal(capital),
            exposure_level=rung,
            sleeve_peak_inr=None if peak is None else Decimal(peak),
            drawdown_locked=locked,
            updated_by="test",
        )
    )
    await session.flush()
    return int(user.id)


async def _market(  # noqa: PLR0913 - one keyword per column a test may set
    session: AsyncSession,
    *,
    user_id: int,
    gate: str = "GREEN",
    on: dt.date = AS_OF,
    level: int = 0,
    settled: tuple[int, int] | None = None,
) -> None:
    """A market row as the detection job leaves it, carrying whatever tier it computed.

    ``settled=(from, to)`` adds the record an evening's `settle_ladder` leaves on the row — the
    fixture for "yesterday's evening ran".
    """
    positions, exposure = TIERS[level]
    detail: dict[str, object] = {
        "sectors": [],
        "closed_r_multiples": [],
        "closed_trades_read": "simulated",
    }
    if settled is not None:
        detail["ladder"] = {"from": settled[0], "to": settled[1], "settled_by": "swing-eod"}
    session.add(
        SwMarketDaily(
            user_id=user_id,
            date=on,
            constituent_count=40,
            pct_up_strong_1m=Decimal("8.0000"),
            gate=gate,
            exposure_level=level,
            max_open_positions=positions,
            max_exposure_pct=Decimal(str(exposure)),
            new_entries_allowed=gate != "RED",
            parabolic_count=0,
            detail=detail,
        )
    )
    await session.flush()


async def _closed(  # noqa: PLR0913 - one keyword per column a test may set
    session: AsyncSession,
    *,
    user_id: int,
    symbol: str,
    r: str | None,
    closed_on: dt.date = YESTERDAY,
    simulated: bool = True,
    pnl: str | None = None,
) -> int:
    """A closed simulated trade: entry 100, one R = 4, so `exit_avg` follows from ``r``.
    ``pnl`` overrides the rupees (the drawdown reads `pnl_inr`, the ladder reads R)."""
    instrument_id = await make_instrument(session, symbol)
    r_multiple = None if r is None else Decimal(r)
    exit_avg = None if r_multiple is None else Decimal(100) + r_multiple * 4
    row = SwPosition(
        user_id=user_id,
        instrument_id=instrument_id,
        setup="FLAG",
        entry_date=closed_on - dt.timedelta(days=7),
        entry_avg=Decimal("100.0000"),
        quantity_entered=100,
        initial_stop=Decimal("96.00"),
        stop=Decimal("96.00"),
        gtt_id=None,
        trail="MA20",
        partial_done=False,
        quantity_open=0,
        state="CLOSED",
        closed_on=closed_on,
        exit_avg=exit_avg,
        close_reason="CLOSE_BELOW_TRAIL_MA",
        r_multiple=r_multiple,
        pnl_inr=(
            Decimal(pnl)
            if pnl is not None
            else (None if r_multiple is None else r_multiple * 4 * 100)
        ),
        simulated=simulated,
    )
    session.add(row)
    await session.flush()
    return int(row.id)


async def _closes(
    session: AsyncSession, *, user_id: int, rs: tuple[str, ...], simulated: bool = True
) -> None:
    """``rs`` oldest first, closed on successive days so the order is the order they happened."""
    first = YESTERDAY - dt.timedelta(days=len(rs) - 1)
    for index, r in enumerate(rs):
        await _closed(
            session,
            user_id=user_id,
            symbol=f"DONE{index}",
            r=r,
            closed_on=first + dt.timedelta(days=index),
            simulated=simulated,
        )


async def _watched(session: AsyncSession, *, user_id: int, symbols: tuple[str, ...]) -> None:
    """Flags scoring above the auto-watch floor, so the evening has names to plan."""
    for symbol in symbols:
        instrument_id = await make_instrument(session, symbol)
        session.add(
            SwSetupDaily(
                user_id=user_id,
                date=AS_OF,
                instrument_id=instrument_id,
                setup="FLAG",
                status="SETTING_UP",
                score=Decimal("72.00"),
                close=Decimal("108.00"),
                trigger=Decimal("110.00"),
                stop_ref=Decimal("104.00"),
                adj_factor=Decimal(1),
                # 5.60 since SW9.5: the widest stop is one ADR (`04` §6), and a 104 stop under a 110
                # trigger is 5.45% — inside 5.60, outside the 5.00 the fixture used to carry; under
                # the 6% fast-trail line, so the line still trails the 20-day.
                adr_pct=Decimal("5.60"),
                turnover_avg=100_000_000,
                locked_upper_circuit=False,
                listed_within_2y=False,
            )
        )
    await session.flush()


async def _rung(session: AsyncSession, user_id: int) -> int:
    return int(
        (
            await session.execute(
                sa.select(SwConfig.exposure_level).where(SwConfig.user_id == user_id)
            )
        ).scalar_one()
    )


async def _market_row(session: AsyncSession, user_id: int, on: dt.date = AS_OF) -> SwMarketDaily:
    return (
        await session.execute(
            sa.select(SwMarketDaily).where(
                SwMarketDaily.user_id == user_id, SwMarketDaily.date == on
            )
        )
    ).scalar_one()


async def _audit(session: AsyncSession, user_id: int) -> list[SwConfigAudit]:
    return list(
        (
            await session.execute(
                sa.select(SwConfigAudit)
                .where(SwConfigAudit.user_id == user_id, SwConfigAudit.key == "exposure_level")
                .order_by(SwConfigAudit.id)
            )
        ).scalars()
    )


class TestTheAcceptanceCriteria:
    async def test_five_good_closes_in_a_green_tape_move_the_rung_up_one(
        self, session: AsyncSession
    ) -> None:
        """0 → 1 on the next EOD run, and the row says so with the five R values."""
        user_id = await _user(session)
        await _market(session, user_id=user_id, gate="GREEN")
        await _closes(session, user_id=user_id, rs=FIVE_GOOD)

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert (report.rung_before, report.exposure_level) == (0, 1)
        assert await _rung(session, user_id) == 1
        market = await _market_row(session, user_id)
        positions, exposure = TIERS[1]
        assert (market.exposure_level, market.max_open_positions) == (1, positions)
        assert market.max_exposure_pct == Decimal(str(exposure))
        assert market.new_entries_allowed is True
        assert isinstance(market.detail, dict)
        assert market.detail["closed_r_multiples"] == list(FIVE_GOOD)
        assert market.detail["closed_trades_read"] == "simulated"
        assert report.closed_r == list(FIVE_GOOD)

    async def test_three_losses_in_a_row_move_it_back(self, session: AsyncSession) -> None:
        """From rung 1, a loss streak of three is one rung down — whatever the tape says."""
        user_id = await _user(session, rung=1)
        await _market(session, user_id=user_id, gate="GREEN", on=YESTERDAY, level=1, settled=(0, 1))
        await _market(session, user_id=user_id, gate="GREEN", level=1)
        await _closes(session, user_id=user_id, rs=THREE_LOSSES)

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert (report.rung_before, report.exposure_level) == (1, 0)
        assert await _rung(session, user_id) == 0
        market = await _market_row(session, user_id)
        assert market.exposure_level == 0
        assert market.max_open_positions == TIERS[0][0]
        audit = await _audit(session, user_id)
        assert [(row.old_value, row.new_value) for row in audit] == [("1", "0")]

    async def test_red_puts_it_at_zero_disallows_entries_and_the_plan_skips_gate_red(
        self, session: AsyncSession
    ) -> None:
        """Rung 2 with five good closes behind it — and a RED tape overrides all of it."""
        user_id = await _user(session, rung=2)
        await _market(session, user_id=user_id, gate="GREEN", on=YESTERDAY, level=2, settled=(1, 2))
        await _market(session, user_id=user_id, gate="RED", level=2)
        await _closes(session, user_id=user_id, rs=FIVE_GOOD)
        await _watched(session, user_id=user_id, symbols=("AAA", "BBB", "CCC"))

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert (report.rung_before, report.exposure_level) == (2, 0)
        assert await _rung(session, user_id) == 0
        market = await _market_row(session, user_id)
        assert market.new_entries_allowed is False
        assert market.exposure_level == 0
        assert report.entry_lines == 0
        skips = (await session.execute(sa.select(SwPlanSkip))).scalars().all()
        assert sorted(skip.symbol for skip in skips) == ["AAA", "BBB", "CCC"]
        assert {skip.reason for skip in skips} == {"GATE_RED"}
        plan = (await session.execute(sa.select(SwPlan))).scalar_one()
        assert plan.exposure_level == 0


class TestTheWriteBack:
    async def test_it_writes_back_to_sw_config_with_an_audit_row_by_swing_eod(
        self, session: AsyncSession
    ) -> None:
        """`03` §1b: `exposure_level` is "written by `swing-eod`, never by a form", and the audit
        says who, from what, to what, and — in the note — on which closes."""
        user_id = await _user(session)
        await _market(session, user_id=user_id, gate="GREEN")
        await _closes(session, user_id=user_id, rs=FIVE_GOOD)
        stamp = dt.datetime(2026, 8, 18, 15, 35, tzinfo=dt.UTC)

        await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id, now=stamp)

        config = (
            await session.execute(sa.select(SwConfig).where(SwConfig.user_id == user_id))
        ).scalar_one()
        assert config.exposure_level == 1
        assert config.updated_by == "swing-eod"
        audit = await _audit(session, user_id)
        assert len(audit) == 1
        row = audit[0]
        assert row.changed_by == "swing-eod" == LADDER_CHANGED_BY
        assert (row.old_value, row.new_value) == ("0", "1")
        assert row.changed_at == stamp
        assert row.note is not None
        assert "GREEN" in row.note
        for r in FIVE_GOOD:
            assert r in row.note

    async def test_the_plan_is_built_with_the_rung_the_evening_leaves_behind(
        self, session: AsyncSession
    ) -> None:
        """Rung 0 allows two positions; rung 1 allows four. Three watched names all get a line
        only if the plan was built *after* the ladder settled — and the plan row says rung 1,
        the same number `sw_config` now says."""
        user_id = await _user(session)
        await _market(session, user_id=user_id, gate="GREEN")
        await _closes(session, user_id=user_id, rs=FIVE_GOOD)
        await _watched(session, user_id=user_id, symbols=("AAA", "BBB", "CCC"))

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert report.entry_lines == 3
        assert report.skips == 0
        lines = (await session.execute(sa.select(SwPlanLine))).scalars().all()
        assert len(lines) == 3
        plan = (await session.execute(sa.select(SwPlan))).scalar_one()
        assert plan.exposure_level == 1 == await _rung(session, user_id)

    async def test_the_same_closes_at_rung_zero_would_have_hit_the_tier_cap(
        self, session: AsyncSession
    ) -> None:
        """The control for the test above: with no closes the rung stays 0, and the third name
        is `TIER_FULL` — so the three lines above really were the ladder's doing."""
        user_id = await _user(session)
        await _market(session, user_id=user_id, gate="GREEN")
        await _watched(session, user_id=user_id, symbols=("AAA", "BBB", "CCC"))

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert report.exposure_level == 0
        assert (report.entry_lines, report.skips) == (2, 1)
        skip = (await session.execute(sa.select(SwPlanSkip))).scalar_one()
        assert skip.reason == "TIER_FULL"

    async def test_an_unchanged_rung_leaves_no_audit_row(self, session: AsyncSession) -> None:
        """The audit is a history of changes, not a log of runs."""
        user_id = await _user(session)
        await _market(session, user_id=user_id, gate="AMBER")
        await _closes(session, user_id=user_id, rs=FIVE_GOOD)

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert (report.rung_before, report.exposure_level) == (0, 0)
        assert await _audit(session, user_id) == []

    async def test_running_the_evening_twice_climbs_once(self, session: AsyncSession) -> None:
        """House rule 7. The rung is read from yesterday's row, not from the value this job
        writes, so a re-run of the same evening answers the same rung and adds no audit row."""
        user_id = await _user(session)
        await _market(session, user_id=user_id, gate="GREEN")
        await _closes(session, user_id=user_id, rs=FIVE_GOOD)

        first = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)
        second = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert first.exposure_level == second.exposure_level == 1
        assert second.rung_before == 0
        assert await _rung(session, user_id) == 1
        assert len(await _audit(session, user_id)) == 1

    async def test_the_rung_carries_over_from_the_previous_session(
        self, session: AsyncSession
    ) -> None:
        """Yesterday's evening settled rung 1 for today; tonight, GREEN with the same five good
        closes, is rung 2 — the ladder climbs from where it stood, one rung at a time, and
        the detection job's preview on today's row (0) is not where it stood."""
        user_id = await _user(session, rung=1)
        await _market(session, user_id=user_id, gate="GREEN", on=YESTERDAY, level=1, settled=(0, 1))
        await _market(session, user_id=user_id, gate="GREEN", level=0)
        await _closes(session, user_id=user_id, rs=FIVE_GOOD)

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert (report.rung_before, report.exposure_level) == (1, 2)
        assert await _rung(session, user_id) == 2
        assert (await _market_row(session, user_id)).exposure_level == 2

    async def test_a_previous_row_a_re_detect_rewrote_does_not_move_the_ladder(
        self, session: AsyncSession
    ) -> None:
        """The Saturday re-scan rewrites Friday's row from the rung Friday's evening had already
        written — one rung too high, and without the settlement record. The rung in force is
        the one `sw_config` carries (1), not the column (2): the record is the fact, the column
        is the detection job's preview of it."""
        user_id = await _user(session, rung=1)
        await _market(session, user_id=user_id, gate="GREEN", on=YESTERDAY, level=2)
        await _market(session, user_id=user_id, gate="AMBER", level=2)

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert (report.rung_before, report.exposure_level) == (1, 1)
        assert await _rung(session, user_id) == 1
        assert (await _market_row(session, user_id)).exposure_level == 1

    async def test_a_re_run_starts_from_the_rung_it_recorded(self, session: AsyncSession) -> None:
        """Tonight's row already carries ``ladder.from = 1`` from an earlier run of the same
        evening; a re-run starts there, whatever `sw_config` has been moved to since."""
        user_id = await _user(session, rung=3)
        await _market(session, user_id=user_id, gate="GREEN", level=2, settled=(1, 2))
        await _closes(session, user_id=user_id, rs=FIVE_GOOD)

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert (report.rung_before, report.exposure_level) == (1, 2)
        assert await _rung(session, user_id) == 2

    async def test_a_sleeve_that_was_never_seeded_still_settles_the_market_row(
        self, session: AsyncSession
    ) -> None:
        """No `sw_config` row: nothing to write back to, and the evening does not fail — the
        market row still carries the rung, and the plan is built with it."""
        user = AppUser(public_id="sw8-unseeded", email="sw8-unseeded@example.com")
        session.add(user)
        await session.flush()
        user_id = int(user.id)
        await _market(session, user_id=user_id, gate="GREEN")
        await _closes(session, user_id=user_id, rs=FIVE_GOOD)

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert report.exposure_level == 1
        assert (await _market_row(session, user_id)).exposure_level == 1
        assert await _audit(session, user_id) == []


class TestWhatTheLadderReads:
    async def test_it_reads_the_paper_book_while_execution_is_off(
        self, session: AsyncSession
    ) -> None:
        """PACK.6. Five good *real* closes do not move the paper rung, and the row says which
        book it read."""
        user_id = await _user(session)
        await _market(session, user_id=user_id, gate="GREEN")
        await _closes(session, user_id=user_id, rs=FIVE_GOOD, simulated=False)

        report = await run_swing_eod(
            session, StepOutcome(), AS_OF, user_id=user_id, execution_enabled=False
        )

        assert report.exposure_level == 0
        assert report.closed_r == []
        market = await _market_row(session, user_id)
        assert isinstance(market.detail, dict)
        assert market.detail["closed_trades_read"] == "simulated"

    async def test_it_reads_the_real_book_once_execution_is_on(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _market(session, user_id=user_id, gate="GREEN")
        await _closes(session, user_id=user_id, rs=FIVE_GOOD, simulated=False)

        report = await run_swing_eod(
            session, StepOutcome(), AS_OF, user_id=user_id, execution_enabled=True
        )

        assert report.exposure_level == 1
        market = await _market_row(session, user_id)
        assert isinstance(market.detail, dict)
        assert market.detail["closed_trades_read"] == "real"

    async def test_a_close_dated_after_the_session_is_not_read(self, session: AsyncSession) -> None:
        """House rule 5. Re-settling the 18th must not see a trade closed on the 19th."""
        user_id = await _user(session)
        await _market(session, user_id=user_id, gate="GREEN")
        await _closes(session, user_id=user_id, rs=FIVE_GOOD[:4])
        await _closed(
            session,
            user_id=user_id,
            symbol="LATER",
            r="3.00",
            closed_on=AS_OF + dt.timedelta(days=1),
        )

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert report.closed_r == list(FIVE_GOOD[:4])
        assert report.exposure_level == 0, "four closes are not five"

    async def test_only_the_last_five_count_and_in_the_order_they_closed(
        self, session: AsyncSession
    ) -> None:
        """Seven closes: the two oldest are outside `lookback_trades` and do not count."""
        user_id = await _user(session)
        await _market(session, user_id=user_id, gate="GREEN")
        await _closes(session, user_id=user_id, rs=("-3.00", "-3.00", *FIVE_GOOD))

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert report.closed_r == list(FIVE_GOOD)
        assert report.exposure_level == 1

    async def test_a_closed_row_without_an_r_multiple_is_not_a_trade(
        self, session: AsyncSession
    ) -> None:
        """Four good closes and one `CLOSED` row whose close-out never wrote its R: that is four
        trades, and four is not five."""
        user_id = await _user(session)
        await _market(session, user_id=user_id, gate="GREEN")
        await _closes(session, user_id=user_id, rs=FIVE_GOOD[:4])
        await _closed(session, user_id=user_id, symbol="HALFDONE", r=None)

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert report.closed_r == list(FIVE_GOOD[:4])
        assert report.exposure_level == 0

    async def test_the_step_payload_explains_the_move(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _market(session, user_id=user_id, gate="GREEN")
        await _closes(session, user_id=user_id, rs=FIVE_GOOD)
        outcome = StepOutcome()

        await run_swing_eod(session, outcome, AS_OF, user_id=user_id)

        # SW9.5: the drawdown rides on the same payload. The five closes are +₹1,800 on a ₹10
        # lakh sleeve and this is the first evening, so the peak is tonight's NAV and the
        # sleeve is 0.00% under it, unlocked.
        assert outcome.detail["ladder"] == {
            "from": 0,
            "to": 1,
            "closed_r_multiples": list(FIVE_GOOD),
            "drawdown_pct": "0.00",
            "drawdown_locked": False,
        }


# --- SW9.5: `04` §8.5, the drawdown containment ----------------------------------------------

#: `MarketConfig.max_drawdown_pct` [15] and `resume_drawdown_pct` [10], read off the config the
#: evening uses rather than retyped.
LOCK_AT = DEFAULT_SWING_CONFIG.market.max_drawdown_pct
RELEASE_INSIDE = DEFAULT_SWING_CONFIG.market.resume_drawdown_pct


async def _config(session: AsyncSession, user_id: int) -> SwConfig:
    return (
        await session.execute(sa.select(SwConfig).where(SwConfig.user_id == user_id))
    ).scalar_one()


async def _lock_audit(session: AsyncSession, user_id: int) -> list[tuple[str | None, str]]:
    rows = (
        await session.execute(
            sa.select(SwConfigAudit)
            .where(SwConfigAudit.user_id == user_id, SwConfigAudit.key == "drawdown_locked")
            .order_by(SwConfigAudit.id)
        )
    ).scalars()
    return [(row.old_value, row.new_value) for row in rows]


class TestTheDrawdownContainment:
    async def test_fifteen_percent_under_its_peak_settles_rung_zero_locked_and_the_plan_says_so(
        self, session: AsyncSession
    ) -> None:
        """A ₹10 lakh sleeve whose peak is ₹10 lakh and whose closed paper trades have lost
        ₹1,50,000: NAV ₹8,50,000, 15.00% under — `max_drawdown_pct` exactly — so the evening
        settles rung 0 with `drawdown_locked`, whatever the rung and the tape said (rung 2, GREEN,
        five good closes behind it), writes it to `sw_config` (audited, `swing-eod`) and to the
        market row, and every watched flag is skipped `DRAWDOWN_LOCKOUT`, not `GATE_RED`."""
        user_id = await _user(session, rung=2, peak="1000000")
        await _market(session, user_id=user_id, gate="GREEN", on=YESTERDAY, level=2, settled=(1, 2))
        await _market(session, user_id=user_id, gate="GREEN", level=2)
        await _closes(session, user_id=user_id, rs=FIVE_GOOD)
        await _closed(session, user_id=user_id, symbol="BIGLOSS", r="-1.00", pnl="-151800")
        await _watched(session, user_id=user_id, symbols=("AAA", "BBB", "CCC"))
        assert LOCK_AT == 15.0

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert (report.rung_before, report.exposure_level) == (2, 0)
        assert (report.drawdown_pct, report.drawdown_locked) == ("15.00", True)
        config = await _config(session, user_id)
        assert config.exposure_level == 0
        assert config.sleeve_peak_inr == Decimal("1000000.00")
        assert config.drawdown_pct == Decimal("15.00")
        assert config.drawdown_locked is True
        assert config.updated_by == LADDER_CHANGED_BY
        market = await _market_row(session, user_id)
        assert (market.exposure_level, market.new_entries_allowed) == (0, False)
        assert (market.drawdown_pct, market.drawdown_locked) == (Decimal("15.00"), True)
        assert isinstance(market.detail, dict)
        assert market.detail["drawdown"] == {
            "nav": "850000.00",
            "peak": "1000000.00",
            "pct": "15.00",
            "was_locked": False,
            "locked": True,
        }
        assert await _lock_audit(session, user_id) == [("False", "True")]
        assert report.entry_lines == 0
        skips = (await session.execute(sa.select(SwPlanSkip))).scalars().all()
        assert sorted(skip.symbol for skip in skips) == ["AAA", "BBB", "CCC"]
        assert {skip.reason for skip in skips} == {"DRAWDOWN_LOCKOUT"}

    async def test_back_within_ten_percent_it_unlocks_and_starts_again_at_rung_zero(
        self, session: AsyncSession
    ) -> None:
        """Locked, and tonight 9.90% under the peak (₹99,000 lost of ₹10 lakh): inside
        `resume_drawdown_pct`, so the lock is lifted — audited True → False — the rung is 0 (the
        lock-out settled it there; nothing has climbed since) and the watched flags are lined."""
        user_id = await _user(session, rung=0, peak="1000000", locked=True)
        await _market(session, user_id=user_id, gate="GREEN", on=YESTERDAY, level=0, settled=(0, 0))
        await _market(session, user_id=user_id, gate="GREEN", level=0)
        await _closed(session, user_id=user_id, symbol="LOSS", r="-1.00", pnl="-99000")
        await _watched(session, user_id=user_id, symbols=("AAA", "BBB", "CCC"))
        assert RELEASE_INSIDE == 10.0

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert (report.drawdown_pct, report.drawdown_locked) == ("9.90", False)
        assert report.exposure_level == 0
        config = await _config(session, user_id)
        assert (config.drawdown_pct, config.drawdown_locked) == (Decimal("9.90"), False)
        assert await _lock_audit(session, user_id) == [("True", "False")]
        market = await _market_row(session, user_id)
        assert (market.drawdown_locked, market.new_entries_allowed) == (False, True)
        assert report.entry_lines == TIERS[0][0] == 2, "rung 0's two, best score first"
        skips = (await session.execute(sa.select(SwPlanSkip))).scalars().all()
        assert [skip.reason for skip in skips] == ["TIER_FULL"]

    async def test_between_ten_and_fifteen_a_locked_sleeve_stays_locked(
        self, session: AsyncSession
    ) -> None:
        """Hysteresis: 12% under is not enough to lock a sleeve, and not enough to release one.
        Locked coming in, it stays locked; no audit row, because nothing changed."""
        user_id = await _user(session, peak="1000000", locked=True)
        await _market(session, user_id=user_id, gate="GREEN")
        await _closed(session, user_id=user_id, symbol="LOSS", r="-1.00", pnl="-120000")
        await _watched(session, user_id=user_id, symbols=("AAA",))

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert (report.drawdown_pct, report.drawdown_locked) == ("12.00", True)
        assert await _lock_audit(session, user_id) == []
        assert report.entry_lines == 0

    async def test_between_ten_and_fifteen_an_unlocked_sleeve_is_not_locked(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session, peak="1000000", locked=False)
        await _market(session, user_id=user_id, gate="GREEN")
        await _closed(session, user_id=user_id, symbol="LOSS", r="-1.00", pnl="-120000")
        await _watched(session, user_id=user_id, symbols=("AAA",))

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert (report.drawdown_pct, report.drawdown_locked) == ("12.00", False)
        assert report.entry_lines == 1

    async def test_the_first_evening_sets_the_peak_and_is_never_locked(
        self, session: AsyncSession
    ) -> None:
        """No peak yet (the column is null until the first evening runs): the peak is tonight's
        NAV, the drawdown 0.00, nothing locked — even though the book already lost 20% of the
        sleeve before anything measured it. A ₹0 peak (a sleeve with no capital and no trades)
        divides nothing."""
        user_id = await _user(session)
        await _market(session, user_id=user_id, gate="GREEN")
        await _closed(session, user_id=user_id, symbol="EARLY", r="-1.00", pnl="-200000")

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        config = await _config(session, user_id)
        assert config.sleeve_peak_inr == Decimal("800000.00")
        assert (config.drawdown_pct, config.drawdown_locked) == (Decimal("0.00"), False)
        assert (report.drawdown_pct, report.drawdown_locked) == ("0.00", False)

    async def test_a_sleeve_with_no_capital_and_a_loss_has_no_drawdown(
        self, session: AsyncSession
    ) -> None:
        """Capital ₹0 (the seed) and a paper loss: NAV is negative, the peak is that NAV, and
        the percentage is 0 rather than a division by a non-positive peak."""
        user_id = await _user(session, capital="0")
        await _market(session, user_id=user_id, gate="GREEN")
        await _closed(session, user_id=user_id, symbol="EARLY", r="-1.00", pnl="-400")

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        config = await _config(session, user_id)
        assert config.sleeve_peak_inr == Decimal("-400.00")
        assert (report.drawdown_pct, report.drawdown_locked) == ("0.00", False)

    async def test_the_nav_marks_open_positions_at_the_close_and_counts_a_partials_realised_half(
        self, session: AsyncSession
    ) -> None:
        """`03` §1 (SW9.5.1): NAV = capital + closed P&L + (mark - entry) x open + the SELL fills
        of a still-open position. ₹10 lakh capital, a closed -₹10,000; 100 open at 100 marked
        90 (-₹1,000); a PARTIAL: 300 entered at 100, 200 sold at 110 (+₹2,000), 100 still open
        marked 105 (+₹500). NAV ₹9,91,500; peak ₹10 lakh; 0.85% under."""
        user_id = await _user(session, peak="1000000")
        await _market(session, user_id=user_id, gate="GREEN")
        await _closed(session, user_id=user_id, symbol="DONE", r="-1.00", pnl="-10000")
        for symbol, close, entered, open_, sold in (
            ("OPENCO", 90.0, 100, 100, None),
            ("PARTCO", 105.0, 300, 100, (200, "110.0000")),
        ):
            instrument_id = await make_instrument(session, symbol)
            session.add(
                OhlcvDaily(
                    instrument_id=instrument_id,
                    date=AS_OF,
                    open=Decimal(str(close)),
                    high=Decimal(str(close)),
                    low=Decimal(str(close)),
                    close=Decimal(str(close)),
                    volume=1_000_000,
                    close_raw=Decimal(str(close)),
                    volume_raw=1_000_000,
                    adj_factor=Decimal(1),
                    source="nse",
                )
            )
            position = SwPosition(
                user_id=user_id,
                instrument_id=instrument_id,
                setup="FLAG",
                entry_date=YESTERDAY,
                entry_avg=Decimal("100.0000"),
                quantity_entered=entered,
                initial_stop=Decimal("96.00"),
                stop=Decimal("96.00"),
                gtt_id="DRY-x",
                trail="MA20",
                partial_done=sold is not None,
                quantity_open=open_,
                state="OPEN" if sold is None else "PARTIAL",
                simulated=True,
            )
            session.add(position)
            await session.flush()
            if sold is not None:
                session.add(
                    SwFill(
                        user_id=user_id,
                        position_id=position.id,
                        side="SELL",
                        quantity=sold[0],
                        price=Decimal(sold[1]),
                        filled_at=dt.datetime(2026, 8, 18, 9, 20, tzinfo=dt.UTC),
                        simulated=True,
                    )
                )
        await session.flush()

        report = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        market = await _market_row(session, user_id)
        assert isinstance(market.detail, dict)
        drawdown = market.detail["drawdown"]
        assert isinstance(drawdown, dict)
        assert drawdown["nav"] == "991500.00"
        assert (report.drawdown_pct, report.drawdown_locked) == ("0.85", False)

    async def test_the_peak_only_rises(self, session: AsyncSession) -> None:
        """A stored peak below tonight's NAV is raised to it; a stored peak above it stays."""
        user_id = await _user(session, peak="900000")
        await _market(session, user_id=user_id, gate="GREEN")
        await _closes(session, user_id=user_id, rs=FIVE_GOOD)

        await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        config = await _config(session, user_id)
        assert config.sleeve_peak_inr == Decimal("1001800.00")
        assert config.drawdown_pct == Decimal("0.00")

    async def test_running_the_evening_twice_moves_neither_the_peak_nor_the_lock(
        self, session: AsyncSession
    ) -> None:
        """House rule 7. The re-run reads the lock it wrote as "was locked" and answers the same
        (15% is over the line either way); the peak is a maximum; one audit row, not two."""
        user_id = await _user(session, peak="1000000")
        await _market(session, user_id=user_id, gate="GREEN")
        await _closed(session, user_id=user_id, symbol="BIGLOSS", r="-1.00", pnl="-150000")

        first = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)
        second = await run_swing_eod(session, StepOutcome(), AS_OF, user_id=user_id)

        assert (first.drawdown_pct, first.drawdown_locked) == ("15.00", True)
        assert (second.drawdown_pct, second.drawdown_locked) == ("15.00", True)
        config = await _config(session, user_id)
        assert config.sleeve_peak_inr == Decimal("1000000.00")
        assert await _lock_audit(session, user_id) == [("False", "True")]

    async def test_the_night_the_ladder_switches_books_the_paper_peak_is_not_held_against_it(
        self, session: AsyncSession
    ) -> None:
        """PACK.6: paper closes until the flag is on, real ones after. The paper book peaked at
        ₹12 lakh; the real book starts at the ₹10 lakh sleeve. With the flag on tonight and
        yesterday's settlement recorded as read from the simulated book, the peak starts over
        at tonight's real NAV — 0.00% under, unlocked — instead of reading a 16.67% drawdown
        the real book never had. The paper closes are not the real book's either."""
        user_id = await _user(session, peak="1200000", locked=True)
        await _market(session, user_id=user_id, gate="GREEN", on=YESTERDAY, settled=(0, 0))
        await _market(session, user_id=user_id, gate="GREEN")
        await _closed(session, user_id=user_id, symbol="PAPER", r="5.00", pnl="200000")

        report = await run_swing_eod(
            session, StepOutcome(), AS_OF, user_id=user_id, execution_enabled=True
        )

        config = await _config(session, user_id)
        assert config.sleeve_peak_inr == Decimal("1000000.00")
        assert (report.drawdown_pct, report.drawdown_locked) == ("0.00", False)
        assert report.closed_r == []
        assert await _lock_audit(session, user_id) == [("True", "False")]
