"""The nightly NFO master: ``op_contract`` and ``op_expiry`` (``docs/options/03`` §1-§2, OP2).

Kite's ``instruments("NFO")`` filtered to NIFTY CE/PE is written into ``op_contract`` — **rows are
never deleted**; a contract absent from tonight's dump is marked ``expired`` and stays resolvable
— and ``op_expiry`` is rebuilt from tonight's contracts: the distinct expiries, each one's lot
size, and its kind by ``baskfy_core.options.calendar.kind`` (monthly = the last master expiry of
its calendar month; **no weekday arithmetic**, ``04`` §1.1).

Three changes raise ``OPTIONS_MASTER_CHANGED``, because sizing and every sleeve's day role depend
on them (``03`` §2): an expiry's **lot size changed**, an expiry's **kind changed**, or a listed
expiry that has not yet happened was **withdrawn** (the shape a holiday shift takes: the Tuesday
disappears and a Monday appears). Each is also written into that expiry's ``detail``.

Idempotent (house rule 7): the same dump on the same night changes nothing and raises nothing.
An **empty** dump is refused rather than applied — applying it would mark every live contract
expired on the strength of one failed read.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import OpContract, OpExpiry
from baskfy_core.models.base import JsonObject
from baskfy_core.options.calendar import Contract, kind
from baskfy_core.options.config import OptionType
from baskfy_providers.records import OptionContractRecord
from baskfy_worker.alerts import Alert, AlertName, Severity

log = logging.getLogger("baskfy_worker.options.master")

#: ``02`` Track C §5 / PACK.12: the only underlying in v1.
UNDERLYING = "NIFTY"


class EmptyMaster(RuntimeError):
    """Tonight's dump held no contract for the underlying; nothing was written."""


@dataclass(frozen=True, slots=True)
class MasterChange:
    """One change a person should look at before the open."""

    expiry: dt.date
    what: str  # "LOT_SIZE" | "KIND" | "WITHDRAWN"
    before: str | None
    after: str | None

    def as_dict(self) -> JsonObject:
        return {
            "expiry": self.expiry.isoformat(),
            "what": self.what,
            "before": self.before,
            "after": self.after,
        }


@dataclass(slots=True)
class MasterReport:
    as_of: dt.date
    contracts_seen: int = 0
    contracts_new: int = 0
    contracts_expired: int = 0
    expiries: int = 0
    changes: list[MasterChange] = field(default_factory=list)

    def as_dict(self) -> JsonObject:
        return {
            "as_of": self.as_of.isoformat(),
            "contracts_seen": self.contracts_seen,
            "contracts_new": self.contracts_new,
            "contracts_expired": self.contracts_expired,
            "expiries": self.expiries,
            "changes": [c.as_dict() for c in self.changes],
        }


def to_contract(record: OptionContractRecord | OpContract) -> Contract:
    """A master row as the pure calendar's ``Contract``."""
    return Contract(
        instrument_token=int(record.instrument_token),
        tradingsymbol=record.tradingsymbol,
        underlying=record.underlying,
        expiry=record.expiry,
        strike=record.strike,
        option_type=OptionType(record.option_type),
        lot_size=int(record.lot_size),
        tick_size=record.tick_size,
    )


def expiry_lot_sizes(records: Sequence[OptionContractRecord]) -> dict[dt.date, int]:
    """Each expiry's lot size. Where one expiry states two, the larger is kept and the caller
    reports it — the master is the truth, and a smaller guess would under-count risk per lot."""
    sizes: dict[dt.date, set[int]] = defaultdict(set)
    for r in records:
        sizes[r.expiry].add(r.lot_size)
    return {expiry: max(values) for expiry, values in sizes.items()}


async def load_contracts(session: AsyncSession, underlying: str = UNDERLYING) -> list[Contract]:
    """Every master row for ``underlying`` as calendar ``Contract``s — what the calendar reads.

    Only contracts still listed (``expired = false``): the calendar asks which expiries the
    exchange lists now, and a withdrawn Tuesday must not survive as a phantom monthly.
    """
    rows = (
        await session.execute(
            select(OpContract).where(
                OpContract.underlying == underlying, OpContract.expired.is_(False)
            )
        )
    ).scalars()
    return [to_contract(row) for row in rows]


async def refresh_master(
    session: AsyncSession,
    records: Sequence[OptionContractRecord],
    *,
    as_of: dt.date,
    underlying: str = UNDERLYING,
) -> MasterReport:
    """Apply tonight's dump. Raises :class:`EmptyMaster` (and writes nothing) on an empty one."""
    tonight = [r for r in records if r.underlying == underlying]
    if not tonight:
        raise EmptyMaster(f"the NFO dump held no {underlying} option on {as_of.isoformat()}")
    report = MasterReport(as_of=as_of, contracts_seen=len(tonight))
    await _write_contracts(session, tonight, as_of=as_of, underlying=underlying, report=report)
    await _rebuild_expiries(session, tonight, as_of=as_of, underlying=underlying, report=report)
    await session.flush()
    return report


async def _write_contracts(
    session: AsyncSession,
    tonight: Sequence[OptionContractRecord],
    *,
    as_of: dt.date,
    underlying: str,
    report: MasterReport,
) -> None:
    known = set(
        (
            await session.execute(
                select(OpContract.instrument_token).where(OpContract.underlying == underlying)
            )
        ).scalars()
    )
    tokens = {r.instrument_token for r in tonight}
    report.contracts_new = len(tokens - known)
    for start in range(0, len(tonight), 1000):
        batch = tonight[start : start + 1000]
        stmt = insert(OpContract).values(
            [
                {
                    "instrument_token": r.instrument_token,
                    "tradingsymbol": r.tradingsymbol,
                    "underlying": r.underlying,
                    "expiry": r.expiry,
                    "strike": r.strike,
                    "option_type": r.option_type,
                    "lot_size": r.lot_size,
                    "tick_size": r.tick_size,
                    "first_seen": as_of,
                    "last_seen": as_of,
                    "expired": False,
                }
                for r in batch
            ]
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["instrument_token"],
                set_={
                    "tradingsymbol": stmt.excluded.tradingsymbol,
                    "lot_size": stmt.excluded.lot_size,
                    "tick_size": stmt.excluded.tick_size,
                    # Never moved backwards by re-running an older night.
                    "last_seen": func.greatest(OpContract.last_seen, stmt.excluded.last_seen),
                    "expired": False,
                },
            )
        )
    gone = known - tokens
    if gone:
        result = await session.execute(
            update(OpContract)
            .where(
                OpContract.underlying == underlying,
                OpContract.instrument_token.in_(gone),
                OpContract.expired.is_(False),
            )
            .values(expired=True)
        )
        report.contracts_expired = int(getattr(result, "rowcount", 0) or 0)


async def _rebuild_expiries(
    session: AsyncSession,
    tonight: Sequence[OptionContractRecord],
    *,
    as_of: dt.date,
    underlying: str,
    report: MasterReport,
) -> None:
    contracts = [to_contract(r) for r in tonight]
    lots = expiry_lot_sizes(tonight)
    ambiguous = {
        expiry for expiry in lots if len({r.lot_size for r in tonight if r.expiry == expiry}) > 1
    }
    existing = {
        row.expiry_date: row
        for row in (
            await session.execute(select(OpExpiry).where(OpExpiry.underlying == underlying))
        ).scalars()
    }
    report.expiries = len(lots)
    for expiry in sorted(lots):
        kind_now = kind(contracts, expiry, underlying).value
        lot_now = lots[expiry]
        row = existing.get(expiry)
        if row is None:
            detail: JsonObject = {}
            if expiry in ambiguous:
                detail["ambiguous_lot_size"] = as_of.isoformat()
            session.add(
                OpExpiry(
                    underlying=underlying,
                    expiry_date=expiry,
                    kind=kind_now,
                    lot_size=lot_now,
                    first_seen=as_of,
                    seen_on=as_of,
                    detail=detail,
                )
            )
            continue
        changes: list[MasterChange] = []
        if row.lot_size != lot_now:
            changes.append(MasterChange(expiry, "LOT_SIZE", str(row.lot_size), str(lot_now)))
        if row.kind != kind_now:
            changes.append(MasterChange(expiry, "KIND", row.kind, kind_now))
        if changes or "withdrawn_on" in row.detail:
            restored = "withdrawn_on" in row.detail
            row.detail = _with_history(row.detail, changes, as_of, restored=restored)
            row.lot_size = lot_now
            row.kind = kind_now
            report.changes.extend(changes)
        row.seen_on = max(row.seen_on, as_of)
    for expiry, row in existing.items():
        if expiry in lots or expiry < as_of or "withdrawn_on" in row.detail:
            continue
        change = MasterChange(expiry, "WITHDRAWN", row.kind, None)
        history = _with_history(row.detail, [change], as_of)
        row.detail = {**history, "withdrawn_on": as_of.isoformat()}
        report.changes.append(change)


def _with_history(
    detail: JsonObject, changes: Sequence[MasterChange], as_of: dt.date, *, restored: bool = False
) -> JsonObject:
    history = detail.get("history")
    entries: list[object] = list(history) if isinstance(history, list) else []
    entries.extend({**c.as_dict(), "on": as_of.isoformat()} for c in changes)
    out: JsonObject = {k: v for k, v in detail.items() if not (restored and k == "withdrawn_on")}
    out["history"] = entries
    return out


def master_alert(report: MasterReport) -> Alert | None:
    """``OPTIONS_MASTER_CHANGED`` when anything changed; ``None`` on an ordinary night."""
    if not report.changes:
        return None
    lines = ", ".join(
        f"{c.expiry.isoformat()} {c.what} {c.before}->{c.after}" for c in report.changes
    )
    return Alert(
        name=AlertName.OPTIONS_MASTER_CHANGED,
        severity=Severity.WARNING,
        summary=(
            f"The NIFTY options master changed on {report.as_of.isoformat()}: {lines}. "
            f"Sizing and each sleeve's day role read op_expiry; check it before the open."
        ),
        labels={"trade_date": report.as_of.isoformat()},
        detail=report.as_dict(),
    )
