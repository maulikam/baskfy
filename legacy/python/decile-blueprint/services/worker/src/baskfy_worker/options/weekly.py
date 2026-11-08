"""The options book's weekly summary: `OPTIONS_WEEKLY` (`docs/options/06` OP11, `05` §4).

Every Friday after the close: the week's journal rows and skipped sessions, summarised by
`baskfy_core.options.journal.summarize` — one line per `(sleeve, simulated, sizing_mode)`, never a
pooled number (`04` §12) — in one alert through the machinery every other alert uses. Read-only:
it reads `op_journal` and `op_session`, writes nothing and reaches no broker.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import OpJournal, OpSession, OpSleeveConfig
from baskfy_core.options.config import SizingMode, Sleeve, group_of
from baskfy_core.options.journal import JournalRow, Summary, summarize
from baskfy_worker.alerts import Alert, AlertName, Severity
from baskfy_worker.ops import RUNBOOKS

RUNBOOK: Final = RUNBOOKS[AlertName.OPTIONS_WEEKLY]
WINDOW_DAYS: Final = 7


async def week_rows(session: AsyncSession, user_id: int, today: dt.date) -> list[JournalRow]:
    """The week's traded rows (`op_journal`) and its skipped sessions, each in its own pool."""
    since = today - dt.timedelta(days=WINDOW_DAYS - 1)
    traded = (
        await session.execute(
            select(OpJournal).where(
                OpJournal.user_id == user_id, OpJournal.trade_date.between(since, today)
            )
        )
    ).scalars()
    rows = [
        JournalRow(
            sleeve=Sleeve(j.sleeve),
            trade_date=j.trade_date,
            simulated=j.simulated,
            sizing_mode=SizingMode(j.sizing_mode),
            traded=True,
            net_pnl_inr=Decimal(j.net_pnl_inr),
            r_inr=Decimal(j.risk_budget_inr),
            closed_reason=j.closed_reason,
            minutes_held=j.minutes_held,
            mae_r=Decimal(j.mae_r or 0),
            mfe_r=Decimal(j.mfe_r or 0),
        )
        for j in traded
    ]
    capital = {
        c.sleeve: Decimal(c.sleeve_capital_inr)
        for c in (
            await session.execute(select(OpSleeveConfig).where(OpSleeveConfig.user_id == user_id))
        ).scalars()
    }
    skipped = (
        await session.execute(
            select(OpSession).where(
                OpSession.user_id == user_id,
                OpSession.trade_date.between(since, today),
                OpSession.state == "SKIPPED",
            )
        )
    ).scalars()
    for s in skipped:
        sleeve = Sleeve(s.sleeve)
        funded = capital.get(group_of(sleeve).value, Decimal(0)) > 0
        rows.append(
            JournalRow(
                sleeve=sleeve,
                trade_date=s.trade_date,
                simulated=s.mode == "PAPER",
                sizing_mode=SizingMode.BUDGET if funded else SizingMode.PAPER_ONE_LOT,
                traded=False,
                skip_reason=(s.skip_reasons or ["UNKNOWN"])[0],
            )
        )
    return rows


def _line(summary: Summary) -> str:
    key = summary.key
    kind = "paper" if key.simulated else "LIVE"
    head = f"{key.sleeve.value} {kind} {key.sizing_mode.value}: {summary.traded} traded"
    skipped = sum(n for _, n in summary.skipped_by_reason)
    if skipped:
        head += f", {skipped} skipped"
    if summary.traded:
        head += (
            f"; win {summary.win_rate:.0%}, mean {summary.mean_r:.2f}R, "
            f"worst {summary.worst_r:.2f}R, drawdown {summary.max_drawdown_r:.2f}R"
        )
    return head


def weekly_alert(rows: list[JournalRow], today: dt.date) -> Alert:
    """One alert, one line per pool. A week with nothing says so."""
    summaries = summarize(rows)
    lines = [_line(s) for s in summaries] or ["no options session was decided this week"]
    return Alert(
        name=AlertName.OPTIONS_WEEKLY,
        severity=Severity.WARNING,
        summary=f"Options week to {today.isoformat()} — " + "; ".join(lines) + ".",
        labels={"week_to": today.isoformat(), "pools": str(len(summaries))},
        detail={
            "pools": [
                {
                    "sleeve": s.key.sleeve.value,
                    "simulated": s.key.simulated,
                    "sizing_mode": s.key.sizing_mode.value,
                    "traded": s.traded,
                    "skipped": dict(s.skipped_by_reason),
                    "mean_r": None if s.mean_r is None else str(s.mean_r),
                    "worst_r": None if s.worst_r is None else str(s.worst_r),
                }
                for s in summaries
            ]
        },
        runbook=RUNBOOK,
    )


__all__ = ["WINDOW_DAYS", "week_rows", "weekly_alert"]
