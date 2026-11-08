"""The reconciler: the broker's order book, GTT list and holdings against every sleeve's book (LV2).

WHY THIS EXISTS
---------------
`docs/trading-readiness-review-2026-09-27.md` P0.1, the review's highest-priority finding: a real
TWT or VBT buy was recorded ``SENT`` and returned, and the only code that turned a fill into a
position, a fill row and a GTT was the dry-run branch. ``twt_execute.on_order_update`` existed and
had no caller; VBT had no handler at all and booked its exits at the reference price before the
broker had filled them. **A broker could hold shares the sleeve had not recorded or protected.**
Nothing in the account was in that state when this was written (`docs/live/AUDIT-2026-09-27.md`:
every sleeve book was empty), which is the only reason the gap was a gap and not an incident.

WHAT IT DOES, ONCE PER PASS
---------------------------
1. Reads the broker's order book **once** (``OrderBook.orders``) and hands each sleeve's open
   order — every ``SENT``/``PARTIAL`` row that carries a broker order id — to that sleeve's own
   ``on_order_update``. The handlers are idempotent: a repeated, duplicate or out-of-order report
   changes nothing; more shares filled than recorded grow the position and **re-size** its one
   GTT; a dead order with nothing filled closes the line; a dead order with shares held keeps the
   position for what filled. The dry-run branch and the live path share those handlers, which is
   the only way a rehearsal proves anything.
2. Gives an unfilled remainder back to the account's risk ledger (``RiskManager.release``) when
   an order dies, so a rejected entry does not keep spending the day's caps (LV3).
3. Reads the broker's GTT list and holdings **once each** and judges every open position's
   protection: no stop (``NAKED``), a stop the broker no longer lists (``GTT_MISSING``), a stop
   for more or fewer shares than are held (``GTT_OVERSIZED`` / ``GTT_UNDERSIZED``), a stop that
   fired without the shares leaving the book (``GTT_TRIGGERED_UNFILLED``), shares gone from the
   broker's book that a sleeve still records (``EXTERNAL_EXIT``). Findings are written to
   ``lv_protection_issue``; a finding this pass could observe and did not see is resolved. A read
   that failed resolves nothing of its kind — an unreadable broker is not a clean bill.
4. Every sleeve's buy path asks :func:`protection_unresolved` first and refuses
   ``PROTECTION_UNRESOLVED`` while an issue is open for that sleeve. Protection first, entries
   second (non-negotiable 4 read strictly).

WHAT IT NEVER DOES
------------------
Place a buy. Every broker mutation it causes — a GTT re-sized after a partial fill, a stop armed
for a naked position, a GTT cancelled after an exit — goes through the sleeve's handler and the
gateway (law 2, non-negotiable 6). It holds no ``kc`` of its own: ``OrderBook`` is the desk's
read-only ``Kite`` wrapper (``orders``, ``get_gtts``, ``holdings``), one limiter slot a call.

WHERE IT RUNS
-------------
``python -m app.reconcile`` runs one pass and exits — restart recovery, or a person's hand. The
session supervisor (LV4) runs :func:`reconcile_once` every ten seconds while the session is open
and writes the ``reconciler`` heartbeat from the run it gets back.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, Protocol

log = logging.getLogger("reconcile")

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))

#: Kite's order statuses, as the order book reports them. ``COMPLETE`` is the one fill state;
#: a dead order is one the broker will not fill any further.
ORDER_COMPLETE: Final = "COMPLETE"
ORDER_DEAD_STATUSES: Final[frozenset[str]] = frozenset({"REJECTED", "CANCELLED", "CANCELLED AMO"})

#: Kite's GTT statuses. ``active`` is resting; ``triggered`` fired (a LIMIT order was placed, which
#: is not a fill); the rest are gone.
GTT_ACTIVE: Final = "active"
GTT_TRIGGERED: Final = "triggered"
GTT_GONE_STATUSES: Final[frozenset[str]] = frozenset(
    {"cancelled", "deleted", "expired", "disabled", "rejected"}
)

#: A simulated GTT id, as the gateway mints it. Nothing rests at the exchange under it, so the
#: broker's list is not asked about it.
SIMULATED_GTT_PREFIX: Final = "DRY-"

#: ``lv_protection_issue.kind`` — ``baskfy_core.models.live.LV_ISSUE_KINDS``, restated here because
#: the desk's venv does not carry core.
NAKED: Final = "NAKED"
GTT_MISSING: Final = "GTT_MISSING"
GTT_OVERSIZED: Final = "GTT_OVERSIZED"
GTT_UNDERSIZED: Final = "GTT_UNDERSIZED"
GTT_TRIGGERED_UNFILLED: Final = "GTT_TRIGGERED_UNFILLED"
EXTERNAL_EXIT: Final = "EXTERNAL_EXIT"
STOP_REJECTED: Final = "STOP_REJECTED"
ISSUE_KINDS: Final[frozenset[str]] = frozenset(
    {
        NAKED,
        GTT_MISSING,
        GTT_OVERSIZED,
        GTT_UNDERSIZED,
        GTT_TRIGGERED_UNFILLED,
        EXTERNAL_EXIT,
        STOP_REJECTED,
    }
)
#: The kinds a pass can only judge with the broker's GTT list, and with its holdings.
GTT_KINDS: Final[frozenset[str]] = frozenset(
    {GTT_MISSING, GTT_OVERSIZED, GTT_UNDERSIZED, GTT_TRIGGERED_UNFILLED}
)
HOLDINGS_KINDS: Final[frozenset[str]] = frozenset({EXTERNAL_EXIT})

SLEEVES: Final[tuple[str, ...]] = ("swing", "twt", "vbt")


# --- the ports --------------------------------------------------------------------------------


class OrderBook(Protocol):
    """The broker's three reads. The desk's ``Kite`` wrapper satisfies it; nothing here writes."""

    def orders(self) -> list[dict]: ...

    def get_gtts(self) -> list[dict]: ...

    def holdings(self) -> list[dict]: ...


@dataclass(frozen=True)
class Issue:
    """One thing wrong with one position's protection."""

    sleeve: str
    position_id: int
    symbol: str
    kind: str
    detail: str


class IssueStore(Protocol):
    """Where findings live. ``PgIssueStore`` over ``lv_protection_issue``; a dict in tests."""

    def open_issues(self, sleeve: str | None = None) -> list[dict]: ...

    def record(self, issue: Issue, *, now: dt.datetime) -> None: ...

    def resolve_except(
        self,
        sleeve: str,
        kinds: Iterable[str],
        keep: set[tuple[int, str]],
        *,
        now: dt.datetime,
    ) -> int:
        """Resolve every open issue of ``sleeve`` whose kind is in ``kinds`` and whose
        ``(position_id, kind)`` is not in ``keep``. Returns how many."""
        ...


@dataclass
class SleeveHooks:
    """One sleeve's book, as the reconciler needs to see it.

    ``open_orders`` rows carry ``broker_order_id``, ``symbol``, ``quantity``, ``filled_quantity``
    and ``reference_price`` (what the risk layer valued the order at; ``None`` when unknown).
    ``open_positions`` rows carry ``id``, ``symbol``, ``quantity_open``, ``gtt_id`` and
    ``simulated``. ``on_order_update`` is the sleeve's own idempotent handler over a Kite
    order-book row. ``risk`` is the gateway's ``RiskManager`` (``release`` on a dead order).
    """

    name: str
    open_orders: Callable[[], list[dict]]
    on_order_update: Callable[[dict], Awaitable[Any]]
    open_positions: Callable[[], list[dict]]
    risk: Any = None  # noqa: ANN401 - a RiskManager, or None in a test that does not care


@dataclass(frozen=True)
class GttRow:
    gtt_id: str
    status: str
    symbol: str
    quantity: int | None
    trigger: Decimal | None


@dataclass
class ReconcileRun:
    """What one pass saw and did. ``line`` is the one-line summary the heartbeat carries."""

    at: dt.datetime
    seen: int = 0
    applied: int = 0
    dead: int = 0
    released: int = 0
    unseen: int = 0
    issues: list[Issue] = field(default_factory=list)
    resolved: int = 0
    errors: list[str] = field(default_factory=list)

    def line(self) -> str:
        kinds: dict[str, int] = {}
        for issue in self.issues:
            kinds[issue.kind] = kinds.get(issue.kind, 0) + 1
        found = ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) or "none"
        out = (
            f"reconcile {self.at.astimezone(IST):%H:%M:%S}: {self.seen} order(s) seen, "
            f"{self.applied} applied, {self.dead} dead, {self.released} released, "
            f"{self.unseen} not in the book; issues: {found}; {self.resolved} resolved"
        )
        if self.errors:
            out += "; errors: " + " | ".join(self.errors)
        return out

    @property
    def ok(self) -> bool:
        return not self.errors


# --- the broker's shapes ----------------------------------------------------------------------


def gtt_index(raw: Iterable[dict]) -> dict[str, GttRow]:
    """Kite's GTT rows by id: status, symbol, the sell leg's quantity and the trigger."""
    out: dict[str, GttRow] = {}
    for trigger in raw:
        gtt_id = trigger.get("id")
        if gtt_id is None:
            continue
        condition = trigger.get("condition") or {}
        values = condition.get("trigger_values") or []
        legs = trigger.get("orders") or []
        quantity: int | None = None
        for leg in legs:
            if str(leg.get("transaction_type") or "SELL").upper() == "SELL":
                quantity = int(leg.get("quantity") or 0)
                break
        out[str(gtt_id)] = GttRow(
            gtt_id=str(gtt_id),
            status=str(trigger.get("status") or "").lower(),
            symbol=str(condition.get("tradingsymbol") or ""),
            quantity=quantity,
            trigger=Decimal(str(values[0])) if values else None,
        )
    return out


def holdings_index(raw: Iterable[dict]) -> dict[str, int]:
    """The desk's ``Kite.holdings`` rows (``symbol``, ``quantity`` — already total qty) by symbol."""
    return {str(row["symbol"]): int(row.get("quantity") or 0) for row in raw if row.get("symbol")}


# --- the pass ---------------------------------------------------------------------------------


async def reconcile_once(
    book: OrderBook,
    sleeves: Sequence[SleeveHooks],
    *,
    now: dt.datetime,
    issues: IssueStore,
) -> ReconcileRun:
    """One pass over every sleeve. Never raises for a broker that will not answer: each read is
    tried once, a failure is recorded on the run, and what that read would have judged is left
    as it was."""
    run = ReconcileRun(at=now)
    await _reconcile_orders(book, sleeves, run)
    _judge_protection(book, sleeves, run, now=now, issues=issues)
    log.info(run.line())
    return run


async def _reconcile_orders(book: OrderBook, sleeves: Sequence[SleeveHooks], run: ReconcileRun) -> None:
    try:
        by_id = {str(row.get("order_id")): row for row in book.orders() if row.get("order_id")}
    except Exception as exc:  # noqa: BLE001 - one unreadable book is a recorded miss, not a crash
        run.errors.append(f"orders: {type(exc).__name__}: {exc}")
        return
    for sleeve in sleeves:
        try:
            rows = sleeve.open_orders()
        except Exception as exc:  # noqa: BLE001 - one sleeve's store must not cost the others
            run.errors.append(f"{sleeve.name} open_orders: {type(exc).__name__}: {exc}")
            continue
        for row in rows:
            broker_id = str(row.get("broker_order_id") or "")
            if not broker_id:
                continue
            payload = by_id.get(broker_id)
            if payload is None:
                run.unseen += 1
                continue
            run.seen += 1
            try:
                outcome = await sleeve.on_order_update(payload)
            except Exception as exc:  # noqa: BLE001 - one bad update must not stop the pass
                log.exception("%s: update for %s failed", sleeve.name, broker_id)
                run.errors.append(f"{sleeve.name} {broker_id}: {type(exc).__name__}: {exc}")
                continue
            if outcome is not None:
                run.applied += 1
            status = str(payload.get("status") or "").upper()
            if status in ORDER_DEAD_STATUSES:
                run.dead += 1
                if _release_remainder(sleeve, row, payload):
                    run.released += 1


def _release_remainder(sleeve: SleeveHooks, row: dict, payload: dict) -> bool:
    """Give the unfilled part of a dead order back to the risk ledger. True when something was."""
    if sleeve.risk is None:
        return False
    remainder = int(row.get("quantity") or 0) - int(payload.get("filled_quantity") or 0)
    reference = row.get("reference_price")
    if remainder <= 0 or reference is None:
        return False
    value = float(Decimal(str(reference)) * remainder)
    if value <= 0:
        return False
    sleeve.risk.release(str(row["symbol"]), value)
    return True


def _judge_protection(
    book: OrderBook,
    sleeves: Sequence[SleeveHooks],
    run: ReconcileRun,
    *,
    now: dt.datetime,
    issues: IssueStore,
) -> None:
    gtts: dict[str, GttRow] | None
    held: dict[str, int] | None
    try:
        gtts = gtt_index(book.get_gtts())
    except Exception as exc:  # noqa: BLE001 - recorded; the GTT kinds are left as they were
        run.errors.append(f"gtts: {type(exc).__name__}: {exc}")
        gtts = None
    try:
        held = holdings_index(book.holdings())
    except Exception as exc:  # noqa: BLE001 - recorded; EXTERNAL_EXIT is left as it was
        run.errors.append(f"holdings: {type(exc).__name__}: {exc}")
        held = None

    positions: dict[str, list[dict]] = {}
    for sleeve in sleeves:
        try:
            positions[sleeve.name] = [
                p for p in sleeve.open_positions() if int(p.get("quantity_open") or 0) > 0
            ]
        except Exception as exc:  # noqa: BLE001 - one sleeve's store must not cost the others
            run.errors.append(f"{sleeve.name} open_positions: {type(exc).__name__}: {exc}")
    # Every sleeve's real shares in a name, together: the broker holds one book.
    recorded: dict[str, int] = {}
    for rows in positions.values():
        for p in rows:
            if not p.get("simulated"):
                name = str(p.get("symbol") or f"instrument:{p.get('instrument_id')}")
                recorded[name] = recorded.get(name, 0) + int(p["quantity_open"])

    for sleeve in sleeves:
        if sleeve.name not in positions:
            continue
        found = [
            issue
            for p in positions[sleeve.name]
            for issue in _judge_position(sleeve.name, p, gtts=gtts, held=held, recorded=recorded)
        ]
        observable = set(ISSUE_KINDS)
        if gtts is None:
            observable -= GTT_KINDS
        if held is None:
            observable -= HOLDINGS_KINDS
        for issue in found:
            issues.record(issue, now=now)
        run.resolved += issues.resolve_except(
            sleeve.name, observable, {(i.position_id, i.kind) for i in found}, now=now
        )
        run.issues.extend(found)


def _judge_position(
    sleeve: str,
    position: dict,
    *,
    gtts: dict[str, GttRow] | None,
    held: dict[str, int] | None,
    recorded: dict[str, int],
) -> list[Issue]:
    # A VBT position row carries no symbol of its own in the store protocol; the desk's store
    # joins it in and a double may not, so the instrument id names the row when nothing else does.
    symbol = str(position.get("symbol") or f"instrument:{position.get('instrument_id')}")
    position_id = int(position["id"])
    open_qty = int(position["quantity_open"])
    gtt_id = position.get("gtt_id")
    found: list[Issue] = []

    def issue(kind: str, detail: str) -> None:
        found.append(Issue(sleeve, position_id, symbol, kind, detail))

    if gtt_id is None:
        issue(NAKED, f"{open_qty} share(s) held with no resting stop")
    elif gtts is not None and not str(gtt_id).startswith(SIMULATED_GTT_PREFIX):
        row = gtts.get(str(gtt_id))
        if row is None or row.status in GTT_GONE_STATUSES:
            state = "not in the broker's list" if row is None else row.status
            issue(GTT_MISSING, f"GTT {gtt_id} is {state}; {open_qty} share(s) unprotected")
        elif row.status == GTT_TRIGGERED:
            still = held.get(symbol) if held is not None else None
            where = "" if still is None else f"; the broker still shows {still}"
            issue(
                GTT_TRIGGERED_UNFILLED,
                f"GTT {gtt_id} fired and the position is still open for {open_qty}{where} — "
                f"a trigger is not a fill; check the LIMIT order it placed",
            )
        elif row.quantity is not None and row.quantity > open_qty:
            issue(
                GTT_OVERSIZED,
                f"GTT {gtt_id} sells {row.quantity} against {open_qty} held — a residual stop "
                f"would sell what is not there",
            )
        elif row.quantity is not None and row.quantity < open_qty:
            issue(
                GTT_UNDERSIZED,
                f"GTT {gtt_id} sells {row.quantity} against {open_qty} held — "
                f"{open_qty - row.quantity} share(s) unprotected",
            )
    if held is not None and not position.get("simulated"):
        have = held.get(symbol, 0)
        total = recorded.get(symbol, 0)
        if have < total:
            issue(
                EXTERNAL_EXIT,
                f"the broker holds {have} {symbol} and the sleeves record {total} — "
                f"{total - have} share(s) left the book outside the desk",
            )
    return found


# --- the guard every buy calls ------------------------------------------------------------------


def protection_unresolved(store: Any, *, naked: Iterable[dict]) -> list[str]:  # noqa: ANN401
    """Why this sleeve may not buy right now: its naked positions, and every open issue the
    reconciler has recorded for it. Empty means clear.

    ``store.open_protection_issues()`` is the sleeve store's read of ``lv_protection_issue`` for
    its own sleeve. Protection first, entries second: a book with an unresolved stop does not
    add a position (the review's P0.1 "block additional entries when protection is unresolved").
    """
    reasons = [
        f"{row['symbol']}: position {row['id']} holds {row.get('quantity_open')} with no "
        f"resting stop"
        for row in naked
    ]
    for issue in store.open_protection_issues():
        reasons.append(f"{issue['symbol']}: {issue['kind']} — {issue['detail']}")
    return reasons


def refusal(reasons: Sequence[str]) -> str:
    return "PROTECTION_UNRESOLVED: " + "; ".join(reasons)


# --- the Postgres issue store ---------------------------------------------------------------


class PgIssueStore:
    """``lv_protection_issue`` through the desk's sqlite-shaped connection.

    One open row per ``(user, sleeve, position, kind)`` — the partial unique index in migration
    0055 — so a finding seen on every pass is one row whose ``seen_at`` moves, not a row a pass.
    """

    def __init__(self, conn: Any, user_id: int, schema: str = "public") -> None:  # noqa: ANN401
        self.conn = conn
        self.user_id = int(user_id)
        self.schema = schema

    def t(self, table: str) -> str:
        return f"{self.schema}.{table}" if self.schema else table

    def open_issues(self, sleeve: str | None = None) -> list[dict]:
        sql = (
            f"SELECT id, sleeve, position_id, symbol, kind, detail, seen_at FROM "
            f"{self.t('lv_protection_issue')} WHERE user_id = ? AND resolved_at IS NULL"
        )
        params: tuple[Any, ...] = (self.user_id,)
        if sleeve is not None:
            sql += " AND sleeve = ?"
            params = (*params, sleeve)
        rows = self.conn.execute(sql + " ORDER BY seen_at, id", params).fetchall()
        return [
            {
                "id": int(r["id"]),
                "sleeve": str(r["sleeve"]),
                "position_id": int(r["position_id"]),
                "symbol": str(r["symbol"]),
                "kind": str(r["kind"]),
                "detail": str(r["detail"]),
                "seen_at": r["seen_at"],
            }
            for r in rows
        ]

    def record(self, issue: Issue, *, now: dt.datetime) -> None:
        self.conn.execute(
            f"INSERT INTO {self.t('lv_protection_issue')} "
            "(user_id, sleeve, position_id, symbol, kind, detail, seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, sleeve, position_id, kind) WHERE resolved_at IS NULL "
            "DO UPDATE SET detail = EXCLUDED.detail, seen_at = EXCLUDED.seen_at",
            (
                self.user_id,
                issue.sleeve,
                issue.position_id,
                issue.symbol,
                issue.kind,
                issue.detail,
                now,
            ),
        )

    def resolve_except(
        self,
        sleeve: str,
        kinds: Iterable[str],
        keep: set[tuple[int, str]],
        *,
        now: dt.datetime,
    ) -> int:
        resolved = 0
        for issue in self.open_issues(sleeve):
            if issue["kind"] not in kinds or (issue["position_id"], issue["kind"]) in keep:
                continue
            self.conn.execute(
                f"UPDATE {self.t('lv_protection_issue')} SET resolved_at = ? WHERE id = ?",
                (now, int(issue["id"])),
            )
            resolved += 1
        return resolved


class MemoryIssueStore:
    """The same contract over a list — for tests, and for a sleeve store double."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._next = 0

    def open_issues(self, sleeve: str | None = None) -> list[dict]:
        return [
            dict(r)
            for r in self.rows
            if r["resolved_at"] is None and (sleeve is None or r["sleeve"] == sleeve)
        ]

    def record(self, issue: Issue, *, now: dt.datetime) -> None:
        for r in self.rows:
            if (
                r["resolved_at"] is None
                and (r["sleeve"], r["position_id"], r["kind"])
                == (issue.sleeve, issue.position_id, issue.kind)
            ):
                r["detail"], r["seen_at"] = issue.detail, now
                return
        self._next += 1
        self.rows.append(
            {
                "id": self._next,
                "sleeve": issue.sleeve,
                "position_id": issue.position_id,
                "symbol": issue.symbol,
                "kind": issue.kind,
                "detail": issue.detail,
                "seen_at": now,
                "resolved_at": None,
            }
        )

    def resolve_except(
        self,
        sleeve: str,
        kinds: Iterable[str],
        keep: set[tuple[int, str]],
        *,
        now: dt.datetime,
    ) -> int:
        kinds = set(kinds)
        resolved = 0
        for r in self.rows:
            if r["resolved_at"] is not None or r["sleeve"] != sleeve or r["kind"] not in kinds:
                continue
            if (r["position_id"], r["kind"]) in keep:
                continue
            r["resolved_at"] = now
            resolved += 1
        return resolved


# --- the sleeves' hooks -------------------------------------------------------------------------


def twt_hooks(store: Any, gateway: Any, *, now: Callable[[], dt.datetime]) -> SleeveHooks:  # noqa: ANN401
    from . import twt_execute  # noqa: PLC0415 - the broker-side module, at call time

    async def update(payload: dict) -> Any:  # noqa: ANN401 - an ExecOutcome or None
        return await twt_execute.on_order_update(store, gateway, payload, now=now())

    return SleeveHooks(
        "twt",
        open_orders=store.open_orders,
        on_order_update=update,
        open_positions=store.open_positions,
        risk=getattr(gateway, "risk", None),
    )


def vbt_hooks(store: Any, gateway: Any, *, now: Callable[[], dt.datetime]) -> SleeveHooks:  # noqa: ANN401
    from . import vbt_execute  # noqa: PLC0415 - the broker-side module, at call time

    async def update(payload: dict) -> Any:  # noqa: ANN401 - an ExecOutcome or None
        return await vbt_execute.on_order_update(store, gateway, payload, now=now())

    return SleeveHooks(
        "vbt",
        open_orders=store.open_orders,
        on_order_update=update,
        open_positions=store.open_positions,
        risk=getattr(gateway, "risk", None),
    )


def swing_hooks(store: Any, gateway: Any, *, now: Callable[[], dt.datetime]) -> SleeveHooks:  # noqa: ANN401
    from . import swing_execute  # noqa: PLC0415 - the broker-side module, at call time

    def open_orders() -> list[dict]:
        day = swing_execute._session_day(now())  # noqa: SLF001 - the module's own day rule
        return [
            {
                "broker_order_id": line.get("journal_ref"),
                "symbol": line["symbol"],
                "quantity": int(line.get("quantity") or 0),
                "filled_quantity": 0,
                "reference_price": line.get("trigger"),
            }
            for line in store.sent_buy_lines(day)
            if line.get("journal_ref")
        ]

    async def update(payload: dict) -> Any:  # noqa: ANN401 - an ExecOutcome or None
        return await swing_execute.on_order_update(store, gateway, payload, now=now())

    return SleeveHooks(
        "swing",
        open_orders=open_orders,
        on_order_update=update,
        open_positions=store.open_positions,
        risk=getattr(gateway, "risk", None),
    )


# --- one pass from the command line -----------------------------------------------------------


def run_pass_from_desk(now: dt.datetime | None = None) -> ReconcileRun:
    """Build the three sleeves' hooks over the desk's own stores and gateways and run one pass.

    The desk's ``Kite`` is the order book (three reads). Stores are the sole user's, over one
    connection each; the gateways are the ones the pages confirm through, so a re-sized GTT here
    carries the same guards, journal and idempotency map as one re-sized by a click.
    """
    from . import config as C  # noqa: PLC0415
    from . import main as _main  # noqa: PLC0415 - the desk's one Kite and risk manager
    from . import swing_desk, twt_desk, vbt_desk  # noqa: PLC0415
    from .analytics import db as _db  # noqa: PLC0415

    stamp = now or dt.datetime.now(tz=IST)
    clock = lambda: stamp  # noqa: E731 - one instant for the whole pass
    kite = _main.kite()
    with (
        swing_desk.open_store() as swing_store,
        twt_desk.open_store() as twt_store,
        vbt_desk.open_store() as vbt_store,
        _db.connect() as conn,
    ):
        schema = "public" if _db.DB_BACKEND == "postgres" else ""
        issues = PgIssueStore(conn, user_id=C.SOLE_USER_ID, schema=schema)
        sleeves = [
            swing_hooks(swing_store, swing_desk.swing_gateway(), now=clock),
            twt_hooks(twt_store, twt_desk.twt_gateway(), now=clock),
            vbt_hooks(vbt_store, vbt_desk.vbt_gateway(), now=clock),
        ]
        return asyncio.run(reconcile_once(kite, sleeves, now=stamp, issues=issues))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    run = run_pass_from_desk()
    print(run.line(), flush=True)
    return 0 if run.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
