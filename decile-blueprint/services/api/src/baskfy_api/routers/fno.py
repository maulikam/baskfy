"""``/fno/*`` — the FO run's read surface for the web app, and its one money-free write (FO5).

    GET    /fno/overnight    F1 per underlying (next entry, the night's scan, the proposed condor),
                             open structures with their settle marks, the journal, the evidence
                             card, F2's candidates, open positions and closed trades, and F3's
                             night per underlying, open and closed spreads and evidence (M.5)
    GET    /fno/info         ``04`` §5's Stock F&O table, the verdict table with the latest
                             re-test beside each row, the spread sample's sessions, the ingest's
                             MISSING days
    GET    /fno/config       settings, ceilings, the audit trail
    PATCH  /fno/config       the settings (capital, risk %, lots, open positions, paper, the book's
                             monthly pause), bounded by ``02``'s ceilings, audited to
                             ``fo_config_audit``

``docs/fno/06`` FO5: "GET-only, plus the settings PATCH (money-free, audited). Every other verb is
a 405, and ``test_fno_readonly.py`` checks both sides."

READ-ONLY WHERE MONEY IS CONCERNED
----------------------------------
There is no execute, confirm, plan or close route here, and there is not going to be one: the
desk's ``/fno`` is the only surface with a Confirm (``05`` §4, ``02`` Track C §4). This module does
not import the execution package, names no broker, and reads the FO execution flags only to
*report* them (PAPER / LIVE per sleeve group). Nothing here reaches Kite.

THE CLOCK
---------
Every number is end of day (root ``CLAUDE.md``'s two-clock table, Overnight and Stock F&O rows):
``scan_date`` is the bhavcopy the scan read, marks are at that session's settle, and ``as_of`` is
the series' latest session. The page labels them ``As of close, Tue 22 Sep`` and
``Marked at settle, 22 Sep``; its only live overlay is the web app's shared ``useLiveMarks`` on
the underlying's level.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from baskfy_api import fno_read as reads
from baskfy_api.auth import AuthenticatedDep, settings_for
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.db import SessionDep
from baskfy_api.fno_settings import (
    FnoBookPatch,
    FnoConfigNotSeeded,
    FnoSettingOutOfBounds,
    FnoSleevePatch,
    apply_book_patch,
    apply_sleeve_patch,
    audit_trail,
    ceilings_from_settings,
    check_book,
    check_sleeve,
    read_book,
    read_sleeve,
)
from baskfy_api.problems import bad_request, not_found
from baskfy_api.settings import Settings
from baskfy_core.fno.config import FoSleeve, FoSleeveGroup
from baskfy_core.market_hours_cb import IST

router = APIRouter(prefix="/fno", tags=["fno"])

JSON_MEDIA_TYPE: Final = "application/json"
F1_SLEEVES: Final[tuple[str, ...]] = (FoSleeve.F1N.value, FoSleeve.F1B.value)
F2_SLEEVES: Final[tuple[str, ...]] = (FoSleeve.F2.value,)
F3_SLEEVES: Final[tuple[str, ...]] = (FoSleeve.F3N.value, FoSleeve.F3B.value)


def _settings(request: Request) -> Settings:
    return settings_for(request)


SettingsDep = Annotated[Settings, Depends(_settings)]


def _now() -> dt.datetime:
    """Seam for tests — production uses wall-clock IST."""
    return dt.datetime.now(tz=IST)


def _json(model: BaseModel) -> Response:
    """Pydantic's own JSON, in which a ``Decimal`` is a quoted string (house rule 9)."""
    return Response(content=model.model_dump_json(), media_type=JSON_MEDIA_TYPE)


def _gates(settings: Settings) -> list[reads.FnoGateOut]:
    """Reported, never branched on: the page says PAPER or LIVE per sleeve group."""
    flags = {
        FoSleeveGroup.F1: settings.fno_f1_execution_enabled,
        FoSleeveGroup.F2: settings.fno_f2_execution_enabled,
        FoSleeveGroup.F3: settings.fno_f3_execution_enabled,
    }
    return [
        reads.FnoGateOut(group=group.value, mode=("PAPER", "LIVE")[int(enabled)])
        for group, enabled in flags.items()
    ]


# --- routes ---------------------------------------------------------------------------------------


async def _f3(
    session: SessionDep,
    user_id: int,
    today: dt.date,
    backtests: list[reads.FnoBacktestOut],
) -> reads.FnoF3Out:
    """F3's block: the night's row per underlying (its ``entry_session`` or ``next_session`` is the
    session the desk may plan at 09:20), the spreads, the journal and the re-test rows."""
    f3_date = await reads.latest_scan_date(session, user_id, F3_SLEEVES)
    rows = (
        {}
        if f3_date is None
        else {r.symbol: r for r in await reads.scan_rows(session, user_id, f3_date, F3_SLEEVES)}
    )
    underlyings: list[reads.FnoUnderlyingOut] = []
    for symbol, sleeve in reads.F3_UNDERLYINGS:
        row = rows.get(symbol)
        scan = None if row is None else reads.scan_out(row)
        entry = None
        if scan is not None:
            raw = scan.detail.get("entry_session") or scan.detail.get("next_session")
            entry = dt.date.fromisoformat(str(raw)) if raw else None
        underlyings.append(
            reads.FnoUnderlyingOut(
                symbol=symbol,
                sleeve=sleeve,
                level=await reads.underlying_level(session, symbol, f3_date),
                scan=scan,
                next_entry_date=entry,
            )
        )
    return reads.FnoF3Out(
        research_line=reads.RESEARCH_F3_LINE,
        not_tested=list(reads.F3_NOT_TESTED),
        scan_date=f3_date,
        underlyings=underlyings,
        open=await reads.open_positions(session, user_id, today=today, sleeves=F3_SLEEVES),
        closed=await reads.journal(session, user_id, F3_SLEEVES),
        backtests=[b for b in backtests if b.family in reads.F3_FAMILIES],
    )


@router.get("/overnight", response_model=reads.FnoOvernightOut, summary="F1-F3, read-only")
async def get_fno_overnight(
    session: SessionDep, principal: AuthenticatedDep, settings: SettingsDep
) -> Response:
    """``05`` §2 in one call: the header, the F1 cards, the evidence card, the F2 section and
    the F3 section (M.5, F3-7)."""
    user_id = await scoped_sole_user_id(session, principal.user_id)
    today = _now().astimezone(IST).date()
    next_session = await reads.next_session_after(session, today)

    f1_date = await reads.latest_scan_date(session, user_id, F1_SLEEVES)
    f1_rows = (
        {}
        if f1_date is None
        else {r.symbol: r for r in await reads.scan_rows(session, user_id, f1_date, F1_SLEEVES)}
    )
    underlyings: list[reads.FnoUnderlyingOut] = []
    for symbol, sleeve in zip(reads.F1_UNDERLYINGS, F1_SLEEVES, strict=True):
        row = f1_rows.get(symbol)
        scan = None if row is None else reads.scan_out(row)
        entry = None
        if scan is not None:
            raw = scan.detail.get("next_entry_date") or scan.detail.get("entry_session")
            entry = dt.date.fromisoformat(str(raw)) if raw else None
        underlyings.append(
            reads.FnoUnderlyingOut(
                symbol=symbol,
                sleeve=sleeve,
                level=await reads.underlying_level(session, symbol, f1_date),
                scan=scan,
                next_entry_date=entry,
            )
        )

    structures = await reads.open_positions(session, user_id, today=today, sleeves=F1_SLEEVES)
    f2_date = await reads.latest_scan_date(session, user_id, F2_SLEEVES)
    f2_rows = (
        [] if f2_date is None else await reads.scan_rows(session, user_id, f2_date, F2_SLEEVES)
    )
    counts: dict[str, int] = {}
    for r in f2_rows:
        counts[r.state] = counts.get(r.state, 0) + 1
    whole = next((r for r in f2_rows if r.symbol == reads.F2_WHOLE_SLEEVE_SYMBOL), None)

    backtests = await reads.latest_backtests(session, user_id)
    f3 = await _f3(session, user_id, today, backtests)

    empty_reason = None
    if f1_date is None and f2_date is None and f3.scan_date is None:
        empty_reason = reads.EMPTY_NEVER if settings.fno_scan_enabled else reads.EMPTY_SCAN_OFF
    view = reads.FnoOvernightOut(
        today=today,
        next_session=next_session,
        scan_date=f1_date,
        empty_reason=empty_reason,
        scan_enabled=settings.fno_scan_enabled,
        monitor_enabled=settings.fno_monitor_enabled,
        gates=_gates(settings),
        underlyings=underlyings,
        hard_exit_tomorrow=[
            p
            for p in structures
            if p.hard_exit_date is not None and p.hard_exit_date == next_session
        ],
        open_structures=structures,
        journal=await reads.journal(session, user_id, F1_SLEEVES),
        evidence=reads.FnoEvidenceOut(
            tier="2E",
            line=reads.RESEARCH_B4_LINE,
            loss_close_line=reads.RESEARCH_B4_LOSS_CLOSE_LINE,
            slippage_note=reads.RESEARCH_B4_SLIPPAGE,
            caveat=reads.TIER_2E_CAVEAT,
            n=reads.RESEARCH_B4_N,
            run_date=reads.RESEARCH_RUN_DATE,
            sample=reads.RESEARCH_SAMPLE,
            why_paper=list(reads.RESEARCH_B4_WHY_PAPER),
            backtests=[b for b in backtests if b.family in {"B4", "B4_LOSS_CLOSE"}],
            tally=await reads.paper_tally(session, user_id),
        ),
        f2=reads.FnoF2Out(
            research_line=reads.RESEARCH_F2_LINE,
            scan_date=f2_date,
            candidates=[
                reads.scan_out(r)
                for r in f2_rows
                if r.state in reads.F2_LISTED_STATES and r.symbol != reads.F2_WHOLE_SLEEVE_SYMBOL
            ],
            sleeve_row=None if whole is None else reads.scan_out(whole),
            state_counts=dict(sorted(counts.items())),
            open=await reads.open_positions(session, user_id, today=today, sleeves=F2_SLEEVES),
            closed=await reads.journal(session, user_id, F2_SLEEVES),
        ),
        f3=f3,
    )
    return _json(view)


@router.get("/info", response_model=reads.FnoInfoOut, summary="The Stock F&O information table")
async def get_fno_info(session: SessionDep, principal: AuthenticatedDep) -> Response:
    """``04`` §5 and ``05`` §3: facts per F&O underlying, never a candidate (PACK.8)."""
    user_id = await scoped_sole_user_id(session, principal.user_id)
    today = _now().astimezone(IST).date()
    as_of = await reads.latest_series_date(session)
    return _json(
        reads.FnoInfoOut(
            as_of=as_of,
            rows=[] if as_of is None else await reads.info_rows(session, as_of),
            families=reads.families(await reads.latest_backtests(session, user_id)),
            research_run_date=reads.RESEARCH_RUN_DATE,
            research_sample=reads.RESEARCH_SAMPLE,
            spread_sample=await reads.spread_summary(session),
            ingest=await reads.ingest_summary(session, today),
        )
    )


# --- settings -------------------------------------------------------------------------------------


class FnoSleeveConfigOut(BaseModel):
    sleeve: str
    capital_inr: Decimal
    risk_per_trade_pct: Decimal
    max_lots: int
    max_open_positions: int
    paper_enabled: bool
    paused_until: dt.date | None
    paused_reason: str | None


class FnoBookOut(BaseModel):
    monthly_pause_inr: Decimal
    paused_until: dt.date | None
    paused_reason: str | None


class FnoCeilingsOut(BaseModel):
    risk_per_trade_inr_max: Decimal
    risk_pct_max: Decimal
    max_open_positions_max: int
    max_per_underlying_max: int
    book_monthly_loss_inr_max: Decimal


class FnoAuditOut(BaseModel):
    scope: str
    key: str
    old_value: str | None
    new_value: str | None
    changed_at: dt.datetime
    changed_by: str


class FnoConfigOut(BaseModel):
    seeded: bool
    book: FnoBookOut | None
    sleeves: list[FnoSleeveConfigOut]
    ceilings: FnoCeilingsOut
    gates: list[reads.FnoGateOut]
    audit: list[FnoAuditOut]


class FnoConfigPatch(BaseModel):
    """The whole settings patch: the book, and any of the two sleeve groups."""

    model_config = ConfigDict(extra="forbid")

    book: FnoBookPatch | None = None
    sleeves: dict[FoSleeveGroup, FnoSleevePatch] = Field(default_factory=dict)


async def _config_out(session: SessionDep, user_id: int, settings: Settings) -> FnoConfigOut:
    ceilings = ceilings_from_settings(settings)
    book: FnoBookOut | None = None
    sleeves: list[FnoSleeveConfigOut] = []
    try:
        row = await read_book(session, user_id)
        book = FnoBookOut(
            monthly_pause_inr=row.monthly_pause_inr,
            paused_until=row.paused_until,
            paused_reason=row.paused_reason,
        )
        for group in FoSleeveGroup:
            s = await read_sleeve(session, user_id, group)
            sleeves.append(
                FnoSleeveConfigOut(
                    sleeve=s.sleeve,
                    capital_inr=s.capital_inr,
                    risk_per_trade_pct=s.risk_per_trade_pct,
                    max_lots=s.max_lots,
                    max_open_positions=s.max_open_positions,
                    paper_enabled=s.paper_enabled,
                    paused_until=s.paused_until,
                    paused_reason=s.paused_reason,
                )
            )
    except FnoConfigNotSeeded:
        book, sleeves = None, []
    return FnoConfigOut(
        seeded=book is not None,
        book=book,
        sleeves=sleeves,
        ceilings=FnoCeilingsOut(
            risk_per_trade_inr_max=ceilings.risk_per_trade_inr_max,
            risk_pct_max=ceilings.risk_pct_max,
            max_open_positions_max=ceilings.max_open_positions_max,
            max_per_underlying_max=ceilings.max_per_underlying_max,
            book_monthly_loss_inr_max=ceilings.book_monthly_loss_inr_max,
        ),
        gates=_gates(settings),
        audit=[
            FnoAuditOut(
                scope=a.scope,
                key=a.key,
                old_value=a.old_value,
                new_value=a.new_value,
                changed_at=a.changed_at,
                changed_by=a.changed_by,
            )
            for a in await audit_trail(session, user_id=user_id)
        ],
    )


@router.get("/config", response_model=FnoConfigOut, summary="FO settings, ceilings, audit")
async def get_fno_config(
    session: SessionDep, principal: AuthenticatedDep, settings: SettingsDep
) -> Response:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    return _json(await _config_out(session, user_id, settings))


@router.patch("/config", response_model=FnoConfigOut, summary="Change the FO settings")
async def patch_fno_config(
    session: SessionDep,
    principal: AuthenticatedDep,
    settings: SettingsDep,
    patch: FnoConfigPatch,
) -> Response:
    """The one mutation. A value above its ceiling is a 422 naming the ceiling and its env var;
    ``paused_*`` and the execution flags are not fields. **Atomic across the request**: every
    part's bounds are checked before any row changes."""
    user_id = await scoped_sole_user_id(session, principal.user_id)
    ceilings = ceilings_from_settings(settings)
    now = dt.datetime.now(tz=dt.UTC)
    changed_by = f"user:{user_id}"
    try:
        if patch.book is not None:
            await read_book(session, user_id)
            check_book(patch.book.changes(), ceilings)
        for group, sleeve_patch in patch.sleeves.items():
            current = await read_sleeve(session, user_id, group)
            check_sleeve(group, sleeve_patch.changes(), ceilings, current=current)
        if patch.book is not None:
            await apply_book_patch(
                session, user_id=user_id, patch=patch.book, changed_by=changed_by, now=now
            )
        for group, sleeve_patch in patch.sleeves.items():
            await apply_sleeve_patch(
                session,
                user_id=user_id,
                group=group,
                patch=sleeve_patch,
                changed_by=changed_by,
                now=now,
            )
    except FnoConfigNotSeeded as exc:
        raise not_found("fno config", str(user_id)) from exc
    except FnoSettingOutOfBounds as exc:
        raise bad_request(str(exc)) from exc
    return _json(await _config_out(session, user_id, settings))
