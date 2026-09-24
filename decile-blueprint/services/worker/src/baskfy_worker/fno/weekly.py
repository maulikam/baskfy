"""The FO book's weekly summary: ``FNO_WEEKLY`` (``docs/fno/06`` FO10; the options pack's OP11).

Every Friday after the close, per FO tenant: the week's closed structures from ``fo_journal``,
one line per ``(sleeve, simulated)`` — never a pooled number (``03`` §6) — with the month-to-date
book per mode from the ledger and the ``04`` §7 pauses in force for each mode, in one alert
through the machinery every other alert uses. Read-only: it reads the journal, the marks and the
audit trail, writes nothing and reaches no broker. Dark unless ``BASKFY_FNO_MONITOR_ENABLED``
(with the monitor off there is no book to summarise).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.fno.journal import PoolKey
from baskfy_core.fno.ledger import ClosedTrade, FoPause, Ledger
from baskfy_worker.alerts import Alert, AlertName, Severity
from baskfy_worker.fno.ledger import load_trades, read_ledger, read_pauses
from baskfy_worker.ops import RUNBOOKS

RUNBOOK: Final = RUNBOOKS[AlertName.FNO_WEEKLY]
WINDOW_DAYS: Final = 7


def _mode(simulated: bool) -> str:
    return "paper" if simulated else "LIVE"


def week_lines(trades: Sequence[ClosedTrade], today: dt.date) -> list[str]:
    """One line per ``(sleeve, simulated)`` closed in the week to ``today``."""
    since = today - dt.timedelta(days=WINDOW_DAYS - 1)
    pools: dict[PoolKey, list[ClosedTrade]] = defaultdict(list)
    for t in trades:
        if since <= t.closed_on <= today:
            pools[PoolKey(t.sleeve, t.simulated)].append(t)
    lines: list[str] = []
    for key in sorted(pools, key=lambda k: (not k.simulated, k.sleeve.value)):
        rows = pools[key]
        net = sum((t.net_pnl_inr for t in rows), Decimal(0))
        r = sum((t.r_multiple for t in rows), Decimal(0))
        worst = min(t.r_multiple for t in rows)
        lines.append(
            f"{key.sleeve.value} {_mode(key.simulated)}: {len(rows)} closed, ₹{net}, {r}R "
            f"(worst {worst}R)"
        )
    return lines


def weekly_alert(
    trades: Sequence[ClosedTrade],
    ledger: Ledger,
    pauses: dict[bool, tuple[FoPause, ...]],
    today: dt.date,
) -> Alert:
    """One alert: the week per pool, the month per book, the pauses per mode."""
    lines = week_lines(trades, today) or ["no FO structure closed this week"]
    for simulated, book in ledger.books:
        lines.append(
            f"{_mode(simulated)} book month to date: {book.mtd_closed} closed, "
            f"₹{book.mtd_realised_inr}, {book.open_positions} open marked ₹{book.open_marked_inr}"
        )
    for simulated in (True, False):
        for p in pauses.get(simulated, ()):
            lines.append(f"{_mode(simulated)} pause {p.scope}: {p.message}")
    return Alert(
        name=AlertName.FNO_WEEKLY,
        severity=Severity.WARNING,
        summary=f"FO week to {today.isoformat()} — " + "; ".join(lines) + ".",
        labels={"week_to": today.isoformat()},
        detail={
            "books": [
                {
                    "simulated": simulated,
                    "mtd_closed": book.mtd_closed,
                    "mtd_realised_inr": str(book.mtd_realised_inr),
                    "open_positions": book.open_positions,
                    "open_marked_inr": str(book.open_marked_inr),
                    "max_drawdown_r": str(book.max_drawdown_r),
                }
                for simulated, book in ledger.books
            ],
            "pauses": [
                {"simulated": s, "scope": p.scope, "code": p.code.value}
                for s in (True, False)
                for p in pauses.get(s, ())
            ],
        },
        runbook=RUNBOOK,
    )


async def build_weekly(session: AsyncSession, user_id: int, today: dt.date) -> Alert:
    """The week's alert for one tenant, from the database (read-only)."""
    return weekly_alert(
        await load_trades(session, user_id),
        await read_ledger(session, user_id, today),
        {s: await read_pauses(session, user_id, today, simulated=s) for s in (True, False)},
        today,
    )


__all__ = ["WINDOW_DAYS", "build_weekly", "week_lines", "weekly_alert"]
