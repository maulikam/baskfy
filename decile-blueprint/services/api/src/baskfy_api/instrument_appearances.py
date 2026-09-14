"""Where one stock appears: every screen and strategy scan, from what was stored — never re-run.

Maulik, 14 Sep 2026: "when I open any stock, if it is in any screens or strategy came in results
or scan, do mention that" — and then, "instead of computing it again, persist it and reuse it,
based on the last date's result". So this module reads and does not compute:

* **Screens** — ``screen_run.results``, the newest stored run of each screen the person can see
  (their own and the templates). The nightly publish records every screen's run
  (``screener.warm_screen_cache``), so the newest row is the last published session. A screen
  edited since that run is reported with ``definition_changed`` rather than re-run here.
* **Swing, Volume breakout, Three weeks tight** — their own per-session tables, at the session each
  page itself treats as latest (the detector's newest breadth/market row). A name absent from
  that session is not listed, even if it appeared the day before.

Sole-tenant strategies are read for the sole tenant only; anyone else sees their screens alone.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.swing import latest_detected_date
from baskfy_core.models import (
    Screen,
    ScreenRun,
    SwSetupDaily,
    TwBreadthDaily,
    TwSignalDaily,
    TwStateDaily,
    VbBreadthDaily,
    VbSignalDaily,
)
from baskfy_core.screen_definition import ScreenDefinition

#: ``vb_signal_daily.state`` in the page's words. SCAN_ONLY rows are stored on purpose (PACK.6).
_VBT_STATE = {"SIGNAL": "signal", "SCAN_ONLY": "scanned, did not pass the filters"}


class AppearanceKind(StrEnum):
    SCREEN = "screen"
    TEMPLATE = "template"
    SWING = "swing"
    VOLUME_BREAKOUT = "volume_breakout"
    THREE_WEEKS_TIGHT = "three_weeks_tight"


@dataclass(frozen=True, slots=True)
class Appearance:
    kind: AppearanceKind
    #: What the reader sees: the screen's name, or the strategy's.
    name: str
    #: Where it links: a screen's public id, or the strategy's page path.
    ref: str
    as_of: dt.date
    rank: int | None = None
    of: int | None = None
    #: A short fact in the strategy's own words — "EP · TRIGGERED", "signal", "tight 4 sessions".
    detail: str | None = None
    #: The screen was edited after this run was stored; the rank is the earlier version's.
    definition_changed: bool = False


@dataclass(frozen=True, slots=True)
class AppearancesView:
    appearances: tuple[Appearance, ...]
    screens_checked: int
    #: Screens with no stored run at all, by name — never run since they were created.
    screens_never_run: tuple[str, ...]
    strategies_checked: tuple[str, ...]


async def _screens(
    session: AsyncSession, *, user_id: int, instrument_id: int
) -> tuple[list[Appearance], int, list[str]]:
    screens = (
        await session.scalars(
            select(Screen)
            .where((Screen.user_id == user_id) | Screen.is_example.is_(True))
            .order_by(Screen.is_example, Screen.name)
        )
    ).all()
    if not screens:
        return [], 0, []
    newest = (
        await session.scalars(
            select(ScreenRun)
            .where(ScreenRun.screen_id.in_([screen.id for screen in screens]))
            .distinct(ScreenRun.screen_id)
            .order_by(ScreenRun.screen_id, ScreenRun.as_of.desc(), ScreenRun.created_at.desc())
        )
    ).all()
    runs = {run.screen_id: run for run in newest}

    found: list[Appearance] = []
    never: list[str] = []
    for screen in screens:
        run = runs.get(screen.id)
        if run is None:
            never.append(screen.name)
            continue
        hit = next((row for row in run.results if row.get("instrument_id") == instrument_id), None)
        if hit is None:
            continue
        rank = hit.get("rank")
        found.append(
            Appearance(
                kind=AppearanceKind.TEMPLATE if screen.is_example else AppearanceKind.SCREEN,
                name=screen.name,
                ref=screen.public_id,
                as_of=run.as_of,
                rank=rank if isinstance(rank, int) else None,
                of=run.result_count,
                definition_changed=_definition_hash(screen) != run.definition_hash,
            )
        )
    return found, len(screens), never


def _definition_hash(screen: Screen) -> str | None:
    """The saved definition's hash, or ``None`` when it no longer parses (then it has changed)."""
    try:
        return ScreenDefinition.model_validate(screen.definition).definition_hash()
    except ValueError:
        return None


async def _swing(session: AsyncSession, *, user_id: int, instrument_id: int) -> list[Appearance]:
    day = await latest_detected_date(session, user_id)
    if day is None:
        return []
    rows = (
        await session.scalars(
            select(SwSetupDaily).where(
                SwSetupDaily.user_id == user_id,
                SwSetupDaily.date == day,
                SwSetupDaily.instrument_id == instrument_id,
            )
        )
    ).all()
    return [
        Appearance(
            kind=AppearanceKind.SWING,
            name="Swing",
            ref="/swing",
            as_of=day,
            detail=f"{row.setup} · {row.status} · score {row.score}",
        )
        for row in rows
    ]


async def _volume_breakout(
    session: AsyncSession, *, user_id: int, instrument_id: int
) -> list[Appearance]:
    day = (
        await session.execute(
            select(VbBreadthDaily.date)
            .where(VbBreadthDaily.user_id == user_id)
            .order_by(VbBreadthDaily.date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if day is None:
        return []
    row = (
        await session.scalars(
            select(VbSignalDaily).where(
                VbSignalDaily.user_id == user_id,
                VbSignalDaily.date == day,
                VbSignalDaily.instrument_id == instrument_id,
            )
        )
    ).one_or_none()
    if row is None:
        return []
    return [
        Appearance(
            kind=AppearanceKind.VOLUME_BREAKOUT,
            name="Volume breakout",
            ref="/vbt",
            as_of=day,
            detail=_VBT_STATE.get(row.state, row.state.lower().replace("_", " ")),
        )
    ]


async def _three_weeks_tight(
    session: AsyncSession, *, user_id: int, instrument_id: int
) -> list[Appearance]:
    day = (
        await session.execute(
            select(TwBreadthDaily.date)
            .where(TwBreadthDaily.user_id == user_id)
            .order_by(TwBreadthDaily.date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if day is None:
        return []
    found = (
        await session.execute(
            select(TwStateDaily.sessions_in_state, TwSignalDaily.state)
            .outerjoin(
                TwSignalDaily,
                (TwSignalDaily.user_id == TwStateDaily.user_id)
                & (TwSignalDaily.date == TwStateDaily.date)
                & (TwSignalDaily.instrument_id == TwStateDaily.instrument_id),
            )
            .where(
                TwStateDaily.user_id == user_id,
                TwStateDaily.date == day,
                TwStateDaily.instrument_id == instrument_id,
            )
        )
    ).one_or_none()
    if found is None:
        return []
    sessions, signal = found
    detail = f"tight {sessions} session{'' if sessions == 1 else 's'}"
    if signal:
        detail += f" · {str(signal).lower().replace('_', ' ')}"
    return [
        Appearance(
            kind=AppearanceKind.THREE_WEEKS_TIGHT,
            name="Three weeks tight",
            ref="/twt",
            as_of=day,
            detail=detail,
        )
    ]


async def appearances(
    session: AsyncSession, *, user_id: int, instrument_id: int, strategies_user_id: int | None
) -> AppearancesView:
    """Everything stored that names this instrument. ``strategies_user_id`` is the sole tenant's
    id when the caller is that tenant, else ``None`` and the strategy scans are not read."""
    found, checked, never = await _screens(session, user_id=user_id, instrument_id=instrument_id)
    strategies: tuple[str, ...] = ()
    if strategies_user_id is not None:
        strategies = ("Swing", "Volume breakout", "Three weeks tight")
        for reader in (_swing, _volume_breakout, _three_weeks_tight):
            found.extend(
                await reader(session, user_id=strategies_user_id, instrument_id=instrument_id)
            )
    return AppearancesView(
        appearances=tuple(found),
        screens_checked=checked,
        screens_never_run=tuple(never),
        strategies_checked=strategies,
    )
