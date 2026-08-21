"""Screen alerts — Prompt 20 deliverable 3, and the fan-out half of deliverable 4.

    "Screen alerts: a user subscribes a screen to a schedule (daily/weekly after publish) and
     receives an email with entries, exits, and rank changes since the last run — computed by
     diffing screen_run rows. Include an unsubscribe link and a digest preference."

The arithmetic is ``baskfy_core.screen_diff`` and the wording is
``baskfy_api.email.templates.screen_alert``. This module is what sits between them: the
subscription lifecycle, the two ``screen_run`` rows to diff, the symbols to name them by, and the
one function the nightly job calls (:func:`dispatch`).

It lives in ``services/api`` rather than in the worker because the worker already depends on
``baskfy-api`` (``baskfy_worker.tasks.celery_tasks`` imports the screener from it) and the
dependency cannot go the other way. That also means the whole dispatch is drivable from the API
test harness against a real database, with no broker and no Celery — the same property Prompt 3
gave the pipeline steps.

The unsubscribe link
--------------------
The token in the link is **derived**, not random: ``HMAC(master, alert.public_id)``. That is what
makes it reproducible at send time — the plaintext of a random token would exist only in the
response to the request that created the alert, and every email after that would have nothing to
put in the link. Its SHA-256 is stored anyway (``screen_alert.unsubscribe_token_hash``) so the
endpoint resolves a bare token to an alert in one indexed probe rather than scanning.

Why a delivery row exists
-------------------------
CLAUDE.md house rule 7: "Re-running any day's job produces identical rows." ``screen_alert_
delivery`` is unique on ``(alert_id, as_of)``, so a dispatch that runs twice for one trade date —
a retried Celery task, an operator re-running the night — sends one email. A *skip* is recorded
too, with its reason, so "no email arrived" has an answer that is not "the job never ran".
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.email import AlertRow, AlertSection, Mailer
from baskfy_api.email import templates as email_templates
from baskfy_api.screener import current_data_version
from baskfy_api.security import digest
from baskfy_api.settings import Settings
from baskfy_core.models import (
    Instrument,
    Screen,
    ScreenAlert,
    ScreenAlertDelivery,
    ScreenRun,
    WebhookEndpoint,
)
from baskfy_core.models.base import JsonObject
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_core.screen_diff import RankedRow, ScreenDiff, diff_runs, rows_from_results

log = logging.getLogger(__name__)

__all__ = [
    "DispatchReport",
    "SkipReason",
    "create_alert",
    "delete_alert",
    "dispatch",
    "is_due",
    "list_alerts",
    "load_alert",
    "unsubscribe",
    "unsubscribe_token",
    "update_alert",
]

PUBLIC_ID_BYTES: Final = 12

#: Weekly alerts land on Friday unless the subscriber picked a day. Friday because a weekly
#: momentum screen is read against the week that closed, not the one in progress.
DEFAULT_WEEKLY_WEEKDAY: Final = 4

#: Hex characters of the derived unsubscribe token. 32 = 128 bits, unguessable, and short enough
#: that the link survives a mail client's line wrapping.
UNSUBSCRIBE_TOKEN_HEX: Final = 32


class SkipReason:
    """Why one alert sent nothing tonight. Stored in ``screen_alert_delivery.detail``."""

    NO_RUN = "no_run_for_date"
    NO_PREVIOUS_RUN = "no_previous_run_in_window"
    DEFINITION_CHANGED = "definition_changed"
    NO_CHANGE = "no_change"
    NOT_DUE = "not_due"


def unsubscribe_token(settings: Settings, public_id: str) -> str:
    """Deterministic, unguessable, and reproducible at send time. See the module docstring."""
    master = settings.webhook_signing_secret or settings.jwt_secret
    if not master:
        raise RuntimeError(
            "No BASKFY_WEBHOOK_SIGNING_SECRET or BASKFY_JWT_SECRET, so an unsubscribe link "
            "could not be signed. Alerts are refused rather than sent without one."
        )
    mac = hmac.new(master.encode("utf-8"), f"alert:{public_id}".encode(), hashlib.sha256)
    return mac.hexdigest()[:UNSUBSCRIBE_TOKEN_HEX]


def unsubscribe_url(settings: Settings, public_id: str) -> str:
    token = unsubscribe_token(settings, public_id)
    return f"{settings.web_origin}/alerts/unsubscribe?token={token}"


def screen_url(settings: Settings, screen_public_id: str) -> str:
    return f"{settings.web_origin}/screens/{screen_public_id}"


def manage_url(settings: Settings) -> str:
    return f"{settings.web_origin}/alerts"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def create_alert(  # noqa: PLR0913 - every subscription option is a separate input
    session: AsyncSession,
    *,
    user_id: int,
    screen: Screen,
    settings: Settings,
    frequency: str = "daily",
    weekday: int | None = None,
    top_n: int | None = None,
    min_move: int | None = None,
    digest_preference: bool = False,
) -> ScreenAlert:
    """Subscribe one account to one screen. One alert per (account, screen) — a unique constraint.

    A second subscription to the same screen is not a new row: the honest reading of "subscribe
    the screen I already subscribed" is "change the settings", and two alerts on one screen would
    send two emails about the same diff.
    """
    import secrets  # noqa: PLC0415 - only needed here; keeps the module's import surface small

    if frequency not in {"daily", "weekly"}:
        raise ValueError("frequency must be 'daily' or 'weekly' (PROMPTS.md Prompt 20 §3)")
    public_id = secrets.token_hex(PUBLIC_ID_BYTES)
    alert = ScreenAlert(
        public_id=public_id,
        user_id=user_id,
        screen_id=screen.id,
        frequency=frequency,
        weekday=weekday if frequency == "weekly" else None,
        top_n=top_n,
        min_move=min_move if min_move is not None else settings.alert_min_move,
        digest=digest_preference,
        is_active=True,
        unsubscribe_token_hash=digest(unsubscribe_token(settings, public_id)),
    )
    session.add(alert)
    await session.flush()
    return alert


async def list_alerts(session: AsyncSession, user_id: int) -> Sequence[ScreenAlert]:
    return (
        (
            await session.execute(
                select(ScreenAlert)
                .where(ScreenAlert.user_id == user_id)
                .order_by(ScreenAlert.created_at.desc(), ScreenAlert.id.desc())
            )
        )
        .scalars()
        .all()
    )


async def load_alert(session: AsyncSession, user_id: int, public_id: str) -> ScreenAlert | None:
    return (
        await session.execute(
            select(ScreenAlert).where(
                ScreenAlert.user_id == user_id, ScreenAlert.public_id == public_id
            )
        )
    ).scalar_one_or_none()


async def update_alert(  # noqa: PLR0913 - a PATCH with five optional members
    session: AsyncSession,
    alert: ScreenAlert,
    *,
    frequency: str | None = None,
    weekday: int | None = None,
    top_n: int | None = None,
    min_move: int | None = None,
    digest_preference: bool | None = None,
    is_active: bool | None = None,
) -> ScreenAlert:
    if frequency is not None:
        if frequency not in {"daily", "weekly"}:
            raise ValueError("frequency must be 'daily' or 'weekly'")
        alert.frequency = frequency
        if frequency == "daily":
            alert.weekday = None
    if weekday is not None and alert.frequency == "weekly":
        alert.weekday = weekday
    if top_n is not None:
        alert.top_n = top_n
    if min_move is not None:
        alert.min_move = min_move
    if digest_preference is not None:
        alert.digest = digest_preference
    if is_active is not None:
        alert.is_active = is_active
    await session.flush()
    return alert


async def delete_alert(session: AsyncSession, alert: ScreenAlert) -> None:
    await session.delete(alert)
    await session.flush()


async def unsubscribe(session: AsyncSession, token: str) -> ScreenAlert | None:
    """One-click unsubscribe, with no session. Idempotent — a second click is still a 200.

    Deactivates rather than deletes: the subscriber may want it back, and the delivery history is
    the audit trail for "why did I get that email".
    """
    alert = (
        await session.execute(
            select(ScreenAlert).where(ScreenAlert.unsubscribe_token_hash == digest(token))
        )
    ).scalar_one_or_none()
    if alert is None:
        return None
    alert.is_active = False
    await session.flush()
    return alert


def is_due(alert: ScreenAlert, day: dt.date) -> bool:
    """docs' schedule: "daily/weekly after publish". ``day`` is the trade date being published."""
    if not alert.is_active:
        return False
    if alert.frequency == "daily":
        return True
    wanted = alert.weekday if alert.weekday is not None else DEFAULT_WEEKLY_WEEKDAY
    return day.weekday() == wanted


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunPair:
    """The two ``screen_run`` rows a diff is taken over, or the reason there is no pair."""

    current: ScreenRun | None
    previous: ScreenRun | None
    skip_reason: str | None


async def ensure_run(session: AsyncSession, screen: Screen, as_of: dt.date) -> ScreenRun | None:
    """Make sure a ``screen_run`` exists for ``as_of``, running the screen if it does not.

    Without this the feature would almost never fire. ``screen_run`` is written when *a user*
    runs a screen (``baskfy_api.routers.screens``); the nightly publish warms the Redis cache but
    records no run, so a subscriber who did not happen to open the page that evening would get
    "no run for that date" every night. "Daily after publish" (Prompt 20 §3) has to mean the
    screen is evaluated for the newly published date, and that is what this does.

    It writes through the same upsert the API route uses (``baskfy_api.screener.record_run``), so
    the row an alert diffs is byte-for-byte the row a user's own run would have produced — and a
    user who opens the page afterwards gets a cache-consistent audit trail rather than a second,
    subtly different row.
    """
    from baskfy_api.screener import execute_screen, record_run  # noqa: PLC0415 - avoids a cycle

    try:
        definition = ScreenDefinition.model_validate(screen.definition)
    except ValidationError:
        log.warning(
            "screen has an invalid definition; no alert run written",
            extra={"screen_public_id": screen.public_id},
        )
        return None
    data_version = await current_data_version(session)
    result = await execute_screen(
        session,
        definition,
        as_of=as_of,
        data_version=data_version,
        columns=list(screen.columns),
    )
    await record_run(session, screen.id, result, definition.definition_hash())
    await session.flush()
    return (
        await session.execute(
            select(ScreenRun).where(
                ScreenRun.screen_id == screen.id,
                ScreenRun.as_of == as_of,
                ScreenRun.definition_hash == definition.definition_hash(),
            )
        )
    ).scalar_one_or_none()


async def resolve_runs(
    session: AsyncSession, screen_id: int, as_of: dt.date, *, lookback_days: int
) -> RunPair:
    """The run for ``as_of`` and the most recent earlier run of the *same definition*.

    Same definition matters. ``screen_run`` is keyed by ``(screen, as_of, definition_hash)``
    (docs/04), so a user who edited their screen yesterday has two runs that are not comparable —
    diffing them would report the whole result set as entries and exits and blame the market for
    an edit. That case is skipped with ``definition_changed`` rather than sent.
    """
    current = (
        await session.execute(
            select(ScreenRun)
            .where(ScreenRun.screen_id == screen_id, ScreenRun.as_of == as_of)
            .order_by(ScreenRun.created_at.desc(), ScreenRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if current is None:
        return RunPair(None, None, SkipReason.NO_RUN)

    earliest = as_of - dt.timedelta(days=lookback_days)
    previous = (
        await session.execute(
            select(ScreenRun)
            .where(
                ScreenRun.screen_id == screen_id,
                ScreenRun.as_of < as_of,
                ScreenRun.as_of >= earliest,
            )
            .order_by(ScreenRun.as_of.desc(), ScreenRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if previous is None:
        return RunPair(current, None, SkipReason.NO_PREVIOUS_RUN)
    if previous.definition_hash != current.definition_hash:
        return RunPair(current, previous, SkipReason.DEFINITION_CHANGED)
    return RunPair(current, previous, None)


async def _symbols(session: AsyncSession, instrument_ids: Sequence[int]) -> dict[int, Instrument]:
    if not instrument_ids:
        return {}
    rows = (
        await session.execute(select(Instrument).where(Instrument.id.in_(list(instrument_ids))))
    ).scalars()
    return {row.id: row for row in rows}


def _row(
    names: dict[int, Instrument], instrument_id: int, rank: int, previous: int | None
) -> AlertRow:
    instrument = names.get(instrument_id)
    return AlertRow(
        symbol=instrument.symbol if instrument is not None else f"#{instrument_id}",
        name=instrument.name if instrument is not None else "",
        rank=rank,
        previous_rank=previous,
    )


async def build_section(
    session: AsyncSession,
    *,
    screen: Screen,
    alert: ScreenAlert,
    diff: ScreenDiff,
    settings: Settings,
) -> AlertSection:
    """Turn a diff into the presentation record the template renders."""
    names = await _symbols(session, diff.instrument_ids)
    cap = settings.alert_max_rows
    return AlertSection(
        screen_name=screen.name,
        screen_url=screen_url(settings, screen.public_id),
        unsubscribe_url=unsubscribe_url(settings, alert.public_id),
        as_of=diff.current_as_of,
        previous_as_of=diff.previous_as_of,
        entries=tuple(_row(names, row.instrument_id, row.rank, None) for row in diff.entries[:cap]),
        exits=tuple(_row(names, row.instrument_id, row.rank, None) for row in diff.exits[:cap]),
        movers=tuple(
            _row(names, change.instrument_id, change.current_rank, change.previous_rank)
            for change in diff.rank_changes[:cap]
        ),
        entry_total=diff.entry_count,
        exit_total=diff.exit_count,
        mover_total=len(diff.rank_changes),
        held_count=diff.held_count,
    )


@dataclass(slots=True)
class DispatchReport:
    """What one night's dispatch did. Returned to the Celery task, and logged."""

    as_of: dt.date
    alerts_considered: int = 0
    emails_sent: int = 0
    alerts_sent: int = 0
    alerts_skipped: int = 0
    alerts_failed: int = 0
    webhooks_enqueued: int = 0
    skips: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.alerts_skipped += 1
        self.skips[reason] = self.skips.get(reason, 0) + 1

    def as_dict(self) -> JsonObject:
        return {
            "as_of": self.as_of.isoformat(),
            "alerts_considered": self.alerts_considered,
            "emails_sent": self.emails_sent,
            "alerts_sent": self.alerts_sent,
            "alerts_skipped": self.alerts_skipped,
            "alerts_failed": self.alerts_failed,
            "webhooks_enqueued": self.webhooks_enqueued,
            "skips": dict(self.skips),
        }


async def _already_delivered(session: AsyncSession, alert_id: int, as_of: dt.date) -> bool:
    return (
        await session.execute(
            select(ScreenAlertDelivery.id).where(
                ScreenAlertDelivery.alert_id == alert_id, ScreenAlertDelivery.as_of == as_of
            )
        )
    ).scalar_one_or_none() is not None


async def _record(  # noqa: PLR0913 - one column per member of the row being written
    session: AsyncSession,
    *,
    alert_id: int,
    as_of: dt.date,
    previous_as_of: dt.date | None,
    status: str,
    diff: ScreenDiff | None = None,
    detail: JsonObject | None = None,
) -> None:
    statement = (
        insert(ScreenAlertDelivery)
        .values(
            alert_id=alert_id,
            as_of=as_of,
            previous_as_of=previous_as_of,
            status=status,
            entry_count=diff.entry_count if diff else 0,
            exit_count=diff.exit_count if diff else 0,
            change_count=len(diff.rank_changes) if diff else 0,
            detail=detail,
        )
        .on_conflict_do_nothing(
            index_elements=[ScreenAlertDelivery.alert_id, ScreenAlertDelivery.as_of]
        )
    )
    await session.execute(statement)


def webhook_payload(
    screen: Screen, diff: ScreenDiff, names: dict[int, Instrument], event: str
) -> JsonObject:
    """The body of one outbound webhook. Symbols and ranks only — no factor values, no prices.

    docs/11 §Compliance's data-licensing line applies to a webhook exactly as it applies to the
    public API: this leaves our servers and lands on someone else's. Entries and exits are the
    facts Prompt 20 §4 names, and they are membership facts, not market data.
    """
    rows = diff.entries if event == "screen.entries" else diff.exits
    return {
        "event": event,
        "screen": {"public_id": screen.public_id, "name": screen.name},
        "as_of": diff.current_as_of.isoformat(),
        "previous_as_of": diff.previous_as_of.isoformat(),
        "instruments": [
            {
                "symbol": names[row.instrument_id].symbol
                if row.instrument_id in names
                else f"#{row.instrument_id}",
                "rank": row.rank,
            }
            for row in rows
        ],
    }


async def dispatch(  # noqa: PLR0912, PLR0915 - one linear pass over the night's work
    session: AsyncSession,
    *,
    as_of: dt.date,
    mailer: Mailer,
    settings: Settings,
    now: dt.datetime | None = None,
) -> DispatchReport:
    """Send every alert due for ``as_of`` and enqueue every webhook for it.

    One pass, screen by screen, because the two ``screen_run`` rows are loaded per screen and
    several alerts may point at the same one. Emails are grouped per recipient at the end so the
    digest preference produces one message rather than N.
    """
    from baskfy_api import webhooks  # noqa: PLC0415 - avoids a cycle at import time

    moment = now or dt.datetime.now(tz=dt.UTC)
    report = DispatchReport(as_of=as_of)

    alerts = (
        await session.execute(
            select(ScreenAlert, Screen)
            .join(Screen, Screen.id == ScreenAlert.screen_id)
            .where(ScreenAlert.is_active.is_(True))
            .order_by(ScreenAlert.user_id, ScreenAlert.id)
        )
    ).all()
    endpoints = (
        await session.execute(
            select(WebhookEndpoint, Screen)
            .join(Screen, Screen.id == WebhookEndpoint.screen_id)
            .where(WebhookEndpoint.is_active.is_(True))
            .order_by(WebhookEndpoint.id)
        )
    ).all()

    screen_ids = {row[0].screen_id for row in alerts} | {row[0].screen_id for row in endpoints}
    pairs: dict[int, RunPair] = {}
    rows_cache: dict[int, tuple[tuple[RankedRow, ...], tuple[RankedRow, ...]]] = {}
    screens_by_id: dict[int, Screen] = {}
    for _, alert_screen in alerts:
        screens_by_id[alert_screen.id] = alert_screen
    for _, endpoint_screen in endpoints:
        screens_by_id[endpoint_screen.id] = endpoint_screen
    for screen_id in sorted(screen_ids):
        pair = await resolve_runs(
            session, screen_id, as_of, lookback_days=settings.alert_lookback_days
        )
        if pair.skip_reason == SkipReason.NO_RUN:
            # "after publish" means the screen is evaluated for the published date — see
            # :func:`ensure_run`.
            screen_row = screens_by_id.get(screen_id)
            if screen_row is not None and await ensure_run(session, screen_row, as_of) is not None:
                pair = await resolve_runs(
                    session, screen_id, as_of, lookback_days=settings.alert_lookback_days
                )
        pairs[screen_id] = pair
        if pair.skip_reason is None and pair.current is not None and pair.previous is not None:
            rows_cache[screen_id] = (
                rows_from_results(pair.previous.results),
                rows_from_results(pair.current.results),
            )

    # --- emails -----------------------------------------------------------
    per_user: dict[int, list[tuple[ScreenAlert, AlertSection]]] = {}
    singles: list[tuple[ScreenAlert, AlertSection]] = []

    for alert, screen in alerts:
        report.alerts_considered += 1
        if not is_due(alert, as_of):
            continue
        if await _already_delivered(session, alert.id, as_of):
            continue
        pair = pairs[alert.screen_id]
        if pair.skip_reason is not None:
            await _record(
                session,
                alert_id=alert.id,
                as_of=as_of,
                previous_as_of=pair.previous.as_of if pair.previous else None,
                status="skipped",
                detail={"reason": pair.skip_reason},
            )
            report.skip(pair.skip_reason)
            continue
        current_run, previous_run = pair.current, pair.previous
        if current_run is None or previous_run is None:  # pragma: no cover - narrowing only
            continue
        before, after = rows_cache[alert.screen_id]
        diff = diff_runs(
            before,
            after,
            previous_as_of=previous_run.as_of,
            current_as_of=current_run.as_of,
            min_move=max(1, alert.min_move),
            top_n=alert.top_n,
        )
        if diff.is_empty:
            await _record(
                session,
                alert_id=alert.id,
                as_of=as_of,
                previous_as_of=previous_run.as_of,
                status="skipped",
                diff=diff,
                detail={"reason": SkipReason.NO_CHANGE},
            )
            report.skip(SkipReason.NO_CHANGE)
            continue

        section = await build_section(
            session, screen=screen, alert=alert, diff=diff, settings=settings
        )
        if alert.digest:
            per_user.setdefault(alert.user_id, []).append((alert, section))
        else:
            singles.append((alert, section))
        alert.last_run_id = current_run.id
        alert.last_sent_at = moment

    recipients = await _recipient_addresses(
        session, [alert.user_id for alert, _ in (*singles, *_flatten(per_user))]
    )

    for alert, section in singles:
        address = recipients.get(alert.user_id)
        if address is None:
            report.skip("no_email_address")
            continue
        sent = await _send(
            mailer,
            email_templates.screen_alert(address, [section], manage_url=manage_url(settings)),
        )
        await _record(
            session,
            alert_id=alert.id,
            as_of=as_of,
            previous_as_of=section.previous_as_of,
            status="sent" if sent else "failed",
            detail=None if sent else {"reason": "email_not_sent"},
        )
        if sent:
            report.emails_sent += 1
            report.alerts_sent += 1
        else:
            report.alerts_failed += 1

    for user_id, bundle in per_user.items():
        address = recipients.get(user_id)
        if address is None:
            report.skip("no_email_address")
            continue
        sent = await _send(
            mailer,
            email_templates.screen_alert(
                address, [section for _, section in bundle], manage_url=manage_url(settings)
            ),
        )
        if sent:
            report.emails_sent += 1
        for alert, section in bundle:
            await _record(
                session,
                alert_id=alert.id,
                as_of=as_of,
                previous_as_of=section.previous_as_of,
                status="sent" if sent else "failed",
                detail=None if sent else {"reason": "email_not_sent"},
            )
            if sent:
                report.alerts_sent += 1
            else:
                report.alerts_failed += 1

    # --- webhooks ---------------------------------------------------------
    for endpoint, screen in endpoints:
        pair = pairs[endpoint.screen_id]
        if pair.skip_reason is not None or pair.current is None or pair.previous is None:
            continue
        before, after = rows_cache[endpoint.screen_id]
        diff = diff_runs(
            before,
            after,
            previous_as_of=pair.previous.as_of,
            current_as_of=pair.current.as_of,
        )
        names = await _symbols(session, diff.instrument_ids)
        for event in endpoint.events:
            rows = diff.entries if event == "screen.entries" else diff.exits
            if not rows:
                continue
            await webhooks.enqueue(
                session,
                endpoint,
                event=event,
                payload=webhook_payload(screen, diff, names, event),
                idempotency_key=f"{screen.public_id}:{as_of.isoformat()}:{event}",
                now=moment,
            )
            report.webhooks_enqueued += 1

    await session.flush()
    log.info("screen alerts dispatched", extra=report.as_dict())
    return report


def _flatten(
    per_user: dict[int, list[tuple[ScreenAlert, AlertSection]]],
) -> list[tuple[ScreenAlert, AlertSection]]:
    return [pair for bundle in per_user.values() for pair in bundle]


async def _recipient_addresses(session: AsyncSession, user_ids: Sequence[int]) -> dict[int, str]:
    """One query for every recipient on the night, rather than one per alert."""
    from baskfy_core.models import AppUser  # noqa: PLC0415 - local to keep the import list short

    if not user_ids:
        return {}
    rows = (
        await session.execute(
            select(AppUser.id, AppUser.email).where(AppUser.id.in_(sorted(set(user_ids))))
        )
    ).all()
    return {int(row[0]): str(row[1]) for row in rows}


async def _send(mailer: Mailer, message: email_templates.Message) -> bool:
    """``Mailer.deliver`` already reports failure as ``False`` and logs it (see its docstring).

    The wrapper exists so the dispatch reads the same for both branches and so there is one place
    to change if delivery ever grows a retry.
    """
    return await mailer.deliver(message)
