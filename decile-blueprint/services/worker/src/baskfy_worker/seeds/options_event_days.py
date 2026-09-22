"""Event days no options sleeve trades, and the options config rows (``docs/options/04`` §1.3).

**Only dates verified from their primary source are here, each with that source beside it.** A date
that could not be verified is omitted, not guessed, and ``docs/options/QUESTIONS.md`` Q4 says so;
Maulik adds anything else on ``/options/calendar`` (``source = USER``). An expiry that is an event
day is skipped, never shifted.

Verified 22 Sep 2026 (DECISIONS-OP OP2.6):

* **RBI MPC decision days, FY 2026-27** — RBI press release "Meeting Schedule of the Monetary
  Policy Committee for 2026-2027", 23 Mar 2026,
  https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=62422 : meetings on
  Apr 6-8, Jun 3-5, Aug 3-5, Oct 5-7, Dec 2-4 2026 and Feb 3-5 2027. The decision is announced on
  the **last** day of each meeting (the Monetary Policy Statements of 5 Jun 2026 and 5 Aug 2026
  are dated so), so that day is the event day.
* **Union Budget 2027-28** — not seeded: the Finance Ministry has not announced the date as of
  22 Sep 2026 (convention is 1 Feb, but a convention is not a primary source).
* **Election results** — none announced for the FY.

Idempotent (house rule 7): ``ON CONFLICT DO NOTHING`` everywhere, so a re-seed never resets a
number a person chose, and never overwrites a day a person added.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Final

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import OpBookConfig, OpEventDay, OpSleeveConfig
from baskfy_core.options.config import DEFAULT_OPTIONS_CONFIG, OptionsConfig, Sleeve, SleeveGroup

RBI_MPC_2026_27: Final = "https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=62422"


@dataclass(frozen=True, slots=True)
class SeedEventDay:
    date: dt.date
    reason: str
    source_url: str
    note: str


EVENT_DAYS: Final[tuple[SeedEventDay, ...]] = (
    SeedEventDay(dt.date(2026, 4, 8), "RBI_POLICY", RBI_MPC_2026_27, "MPC decision, Apr 6-8"),
    SeedEventDay(dt.date(2026, 6, 5), "RBI_POLICY", RBI_MPC_2026_27, "MPC decision, Jun 3-5"),
    SeedEventDay(dt.date(2026, 8, 5), "RBI_POLICY", RBI_MPC_2026_27, "MPC decision, Aug 3-5"),
    SeedEventDay(dt.date(2026, 10, 7), "RBI_POLICY", RBI_MPC_2026_27, "MPC decision, Oct 5-7"),
    SeedEventDay(dt.date(2026, 12, 4), "RBI_POLICY", RBI_MPC_2026_27, "MPC decision, Dec 2-4"),
    SeedEventDay(dt.date(2027, 2, 5), "RBI_POLICY", RBI_MPC_2026_27, "MPC decision, Feb 3-5"),
)

#: A representative sleeve per config group, to read that group's defaults off the config.
_GROUP_SLEEVE: Final[dict[SleeveGroup, Sleeve]] = {
    SleeveGroup.O1M: Sleeve.O1M,
    SleeveGroup.O1W: Sleeve.O1W,
    SleeveGroup.O2: Sleeve.O2,
    SleeveGroup.O3: Sleeve.O3A,
}


def sleeve_config_rows(
    user_id: int, config: OptionsConfig = DEFAULT_OPTIONS_CONFIG
) -> list[dict[str, object]]:
    """``03`` §7's per-sleeve defaults, read off ``OptionsConfig`` so the two cannot disagree.

    ``sleeve_capital_inr`` is **0** for every sleeve (PACK.6, QUESTIONS Q1) and is left to the
    column default: no agent writes a capital.
    """
    return [
        {
            "user_id": user_id,
            "sleeve": group.value,
            "risk_per_trade_pct": config.risk_per_trade_pct(sleeve),
            "max_lots": config.max_lots(sleeve),
            "hard_exit_time": config.hard_exit_time(sleeve),
            "updated_by": "seed",
        }
        for group, sleeve in _GROUP_SLEEVE.items()
    ]


async def seed_options(session: AsyncSession, user_id: int) -> dict[str, int]:
    """Event days, the book row and the four sleeve rows for ``user_id``. Returns rows written."""
    days = await session.execute(
        insert(OpEventDay)
        .values(
            [
                {
                    "user_id": user_id,
                    "date": d.date,
                    "reason": d.reason,
                    "source": "SEED",
                    "source_url": d.source_url,
                    "note": d.note,
                }
                for d in EVENT_DAYS
            ]
        )
        .on_conflict_do_nothing(index_elements=["user_id", "date"])
    )
    book = await session.execute(
        insert(OpBookConfig)
        .values(user_id=user_id, updated_by="seed")
        .on_conflict_do_nothing(index_elements=["user_id"])
    )
    sleeves = await session.execute(
        insert(OpSleeveConfig)
        .values(sleeve_config_rows(user_id))
        .on_conflict_do_nothing(index_elements=["user_id", "sleeve"])
    )
    await session.flush()
    return {
        "op_event_day": int(getattr(days, "rowcount", 0) or 0),
        "op_book_config": int(getattr(book, "rowcount", 0) or 0),
        "op_sleeve_config": int(getattr(sleeves, "rowcount", 0) or 0),
    }
