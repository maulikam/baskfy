"""The plan, rebuilt against the session in progress — so a live setup is buyable today.

WHY THIS EXISTS
---------------
Maulik, 4 Sep 2026: "whatever the scans we are doing, they are not the live data, they would be
the past data."

Half of that was already fixed and half of it was not, and the half that was not is the half he
could feel. M85 made the *detection* live: a broker login inside the cash session publishes
`baskfy.swing.scan_after_login`, which builds a provisional bar per liquid name from Kite quotes
and runs the real detectors over today-so-far. That works. On the box this afternoon, scan #7 at
14:08 wrote **thirteen** provisional setups for 2026-09-04.

Nothing downstream of the scan ran. The watchlist is filled by the 21:05 evening job; the plan is
built at 09:16 from the previous close, or by the evening for tomorrow. So at 14:08 the desk had
thirteen fresh setups on the Setups page and exactly **one** executable line — a `BUY_ON_TRIGGER`
in a MORNING plan built at 09:16 against yesterday's numbers. Visible, and not buyable. That is
the whole of "I am not able to buy any stock which is in swing".

WHAT THIS DOES, AND WHAT IT DELIBERATELY DOES NOT
-------------------------------------------------
It re-runs the evening's own sequence against the provisional session:

    auto_watch(on=today) → manage_open_positions(on=today) → watch_items(on=today)
                        → build_entries(...) → assemble → store_plan(source="INTRADAY")

Every one of those is imported, not reimplemented. The sizing, the gate, the exposure tier, the
per-session entry cap, the stop rules and the skip reasons are the same functions the 21:05 job
and the 09:16 job call, so an INTRADAY line cannot be sized by rules the other two do not share.
`04` §5 and §9 are unchanged; only *when* a build may happen is new.

**It does not settle the ladder and it does not count a session.** `settle_ladder` and
`_record_session` are the evening's, once per day, on real closes (STANDING-ANSWERS A10). A plan
rebuilt four times an afternoon must not move the rung four times, and a session that has not
closed has not happened. The rung this plan sizes against is whatever the last evening left in
`sw_config`.

**It does not invent a gate.** The gate and the tier come from the day's `sw_market_daily` row,
which the scan writes alongside the setups. If the scan has not run there is no row, and this
refuses rather than planning against a tape nobody measured — the same rule
`build_morning_plan` follows.

HONEST ABOUT WHAT A PROVISIONAL PLAN IS
---------------------------------------
The bar underneath it is a day in progress. A base that looks tight at 13:42 can widen by 15:30,
a volume dry-up is measured on four hours of volume, and the breadth behind the gate is half a
day's breadth. The plan is stamped ``INTRADAY`` for exactly that reason: the page can say so, the
journal can tell it apart from a plan built on a close, and the evening's own build replaces it.
Confirming one is still a deliberate act — non-negotiable 1's `confirm=true` and the 30-minute
expiry are untouched, and `execute_line` re-derives the book under the session lock before any
order is sent (A5).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.swing_watch import auto_watch
from baskfy_core.models.swing import SwMarketDaily
from baskfy_core.swing.config import SwingConfig
from baskfy_core.swing.market import ExposureTier, MarketGate
from baskfy_core.swing.plan import (
    LineKind,
    assemble,
    build_entries,
    first_live_multiplier,
)
from baskfy_worker.tasks.swing import load_swing_config
from baskfy_worker.tasks.swing_eod import (
    held_instrument_ids,
    manage_open_positions,
    risk_pct_in_force,
    sleeve_account,
    store_plan,
    watch_items,
)
from baskfy_worker.tasks.swing_premarket import load_config_row

log = logging.getLogger(__name__)

#: The source stamped on every plan this module writes (migration 0034).
INTRADAY: str = "INTRADAY"


@dataclass(slots=True)
class IntradayPlanReport:
    """What the rebuild did, for the task's return value and the run row."""

    plan_id: str | None = None
    as_of: dt.date | None = None
    watch_added: int = 0
    watch_expired: int = 0
    watching: int = 0
    entry_lines: int = 0
    pending_lines: int = 0
    exit_lines: int = 0
    skips: int = 0
    skipped_reason: str | None = None
    detail: dict[str, object] = field(default_factory=dict)

    def as_detail(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "as_of": None if self.as_of is None else self.as_of.isoformat(),
            "source": INTRADAY,
            "watch_added": self.watch_added,
            "watch_expired": self.watch_expired,
            "watching": self.watching,
            "entry_lines": self.entry_lines,
            "pending_lines": self.pending_lines,
            "exit_lines": self.exit_lines,
            "skips": self.skips,
            "skipped_reason": self.skipped_reason,
            **self.detail,
        }


async def market_row(session: AsyncSession, *, user_id: int, on: dt.date) -> SwMarketDaily | None:
    return (
        await session.execute(
            select(SwMarketDaily).where(SwMarketDaily.user_id == user_id, SwMarketDaily.date == on)
        )
    ).scalar_one_or_none()


async def build_intraday_plan(  # noqa: PLR0913 - one keyword per input the plan depends on
    session: AsyncSession,
    *,
    user_id: int,
    on: dt.date,
    now: dt.datetime,
    config: SwingConfig | None = None,
    execution_enabled: bool = False,
) -> IntradayPlanReport:
    """Rebuild the day's plan from the session in progress. Returns what it wrote.

    ``on`` is the provisional session — today. Everything is read as of that date, so the
    detectors' provisional rows are what the watchlist and the plan see.
    """
    report = IntradayPlanReport(as_of=on)
    resolved = config if config is not None else await load_swing_config(session, user_id)

    market = await market_row(session, user_id=user_id, on=on)
    if market is None:
        # The same refusal `build_morning_plan` makes, for the same reason: a plan needs a gate,
        # and a gate is a measurement. No scan today means nothing measured today.
        report.skipped_reason = (
            f"no sw_market_daily row for {on.isoformat()}; run the scan before the plan"
        )
        return report

    watched = await auto_watch(session, user_id=user_id, on=on, config=resolved)
    report.watch_added = watched.added
    report.watch_expired = watched.expired

    gate = MarketGate(market.gate)
    tier = ExposureTier(
        level=market.exposure_level,
        max_open_positions=market.max_open_positions,
        max_exposure_pct=float(market.max_exposure_pct),
        new_entries_allowed=market.new_entries_allowed,
        drawdown_locked=bool(market.drawdown_locked),
    )
    # Idempotent (`04` §6.5: a stop never falls), so re-running it inside the session cannot
    # loosen a stop the evening already tightened.
    exits, _, _ = await manage_open_positions(session, user_id=user_id, on=on, config=resolved)

    items, instrument_ids = await watch_items(session, user_id=user_id, on=on)
    report.watching = len(items)
    config_row = await load_config_row(session, user_id)
    account = await sleeve_account(session, user_id=user_id, config_row=config_row)
    left = int(config_row.first_live_sessions_left) if config_row is not None else 0
    multiplier = first_live_multiplier(
        sessions_left=left, execution_enabled=execution_enabled, config=resolved.sizing
    )
    entries, skipped = build_entries(
        as_of=on,
        watch=items,
        account=account,
        gate=gate,
        tier=tier,
        config=resolved,
        risk_multiplier=multiplier,
    )
    plan = assemble(as_of=on, gate=gate, tier=tier, entries=entries, exits=exits, skipped=skipped)
    instrument_ids.update(await held_instrument_ids(session, user_id=user_id))

    report.entry_lines = sum(1 for line in entries if line.kind is LineKind.BUY_ON_TRIGGER)
    report.pending_lines = sum(1 for line in entries if line.kind is LineKind.PENDING_RANGE)
    report.exit_lines = len(exits)
    report.skips = len(skipped)
    report.detail = {
        "risk_multiplier": str(multiplier),
        "risk_pct_in_force": risk_pct_in_force(resolved, multiplier),
        "first_live_sessions_left": left,
        "gate": gate.value,
        "exposure_level": tier.level,
    }
    report.plan_id = await store_plan(
        session,
        plan,
        user_id=user_id,
        source=INTRADAY,
        instrument_ids=instrument_ids,
        now=now,
    )
    log.info(
        "intraday plan %s for %s: %d entries, %d exits, %d skips",
        report.plan_id,
        on.isoformat(),
        report.entry_lines,
        report.exit_lines,
        report.skips,
    )
    return report


__all__ = ["INTRADAY", "IntradayPlanReport", "build_intraday_plan", "market_row"]
