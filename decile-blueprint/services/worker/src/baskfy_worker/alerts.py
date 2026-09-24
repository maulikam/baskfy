"""Alerts (PROMPTS.md Prompt 17 deliverable 3).

    "Alerting rules: pipeline failure, Kite token expiry, gate failure, publish later than
     20:15 IST, error rate > 1%, queue backlog."

Six rules, two mechanisms
-------------------------
Four of them are facts this codebase already knows and Prometheus does not: a pipeline run
failed, a gate assertion failed, a Kite token is about to expire, a run was abandoned. Those are
raised **here**, at the moment the code learns them, so the alert carries the run id, the failing
assertion and the error text rather than a threshold crossing.

Two of them — the API error rate and the queue backlog — are rates over a window, which is what a
time-series database is for. They live in ``infra/prometheus/alerts.yml`` alongside recording rules
for the same metrics ``baskfy_api.metrics`` exposes. The publish deadline is in **both**: the
worker checks it at 20:15 IST because that is a specific instant docs/11 names, and Prometheus
carries the same rule so a worker that is itself down does not take the alert down with it.

Sinks
-----
An alert always increments :data:`baskfy_api.metrics.ALERTS` and always logs a structured record
at ERROR with ``alert=`` set, which is what a Loki alert rule matches on. Beyond that it goes to
whichever sinks are configured: Sentry when a DSN is set, email when ``BASKFY_OPS_ALERT_EMAIL`` is,
and a JSON webhook when ``BASKFY_OPS_ALERT_WEBHOOK_URL`` is. **None configured is a valid
deployment and is not an error** — it is a laptop — but it is also the state in which nobody is
woken up, so ``dispatch`` says so once per process.

A sink that fails never propagates. An alert dispatcher that raises turns "the pipeline failed"
into "the pipeline failed *and* the run record was lost", which is strictly worse.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum
from html import escape
from typing import Final

from baskfy_api import sentry
from baskfy_api.email import Mailer, Message, build_transport
from baskfy_api.metrics import ALERTS
from baskfy_api.settings import Settings, get_settings
from baskfy_api.telemetry import annotate_current_span
from baskfy_core.models.base import JsonObject

log = logging.getLogger(__name__)

__all__ = ["Alert", "AlertName", "Severity", "dispatch", "webhook_payload"]


class Severity(StrEnum):
    #: Wake someone. Data is wrong, or will be missing when the market opens.
    CRITICAL = "critical"
    #: Look at it in the morning. Degraded, not broken.
    WARNING = "warning"


class AlertName(StrEnum):
    """The six of PROMPTS.md Prompt 17 §3, by the name every sink and rule file uses.

    Stable strings: they are the ``alertname`` label in ``infra/prometheus/alerts.yml``, the
    ``alert`` field in the JSON logs, and the tag on the Sentry issue. Renaming one silently
    orphans a routing rule.
    """

    PIPELINE_FAILED = "pipeline_failed"
    #: The shape a worker killed mid-run leaves behind: a run stuck in `running` with nobody in it.
    PIPELINE_ABANDONED = "pipeline_abandoned"
    GATE_FAILED = "data_quality_gate_failed"
    KITE_TOKEN_EXPIRING = "kite_token_expiring"
    PUBLISH_LATE = "publish_late"
    ERROR_RATE_HIGH = "api_error_rate_high"
    QUEUE_BACKLOG = "queue_backlog"
    # SW11 (docs/swing/STANDING-ANSWERS B8): the swing book's five. Upper-case because that is
    # the name every swing document uses; they are the `alert:` names in alerts.yml verbatim.
    SWING_POSITION_NAKED = "SWING_POSITION_NAKED"
    SWING_MONITOR_DID_NOT_START = "SWING_MONITOR_DID_NOT_START"
    SWING_DETECT_STALE = "SWING_DETECT_STALE"
    SWING_ORDER_OPEN_AFTER_CUTOFF = "SWING_ORDER_OPEN_AFTER_CUTOFF"
    SWING_GTT_MISSING_AT_1515 = "SWING_GTT_MISSING_AT_1515"
    # VB4/VB7/VB8 (docs/vbt/05 §4): the volume-breakout sleeve's four. Same convention as the
    # swing book's above — upper-case, verbatim in `alerts.yml`, and each one a condition the
    # worker can raise itself rather than one that waits for Prometheus to be deployed.
    VBT_POSITION_NAKED = "VBT_POSITION_NAKED"
    VBT_DETECT_STALE = "VBT_DETECT_STALE"
    VBT_ORDER_PAST_EXPIRY = "VBT_ORDER_PAST_EXPIRY"
    VBT_POSITION_NO_BAR = "VBT_POSITION_NO_BAR"
    # TW6/TW7 (docs/twt/FIRST-LIVE-MORNING §11): the three-weeks-tight sleeve's four — the four
    # names that runbook tells a person to react to, written down before the code that raises
    # them. Same convention again: upper-case, verbatim, each raised in-process.
    #
    # TWT_EVENING is the odd one and it is deliberate. It is not a failure: it is the evening's
    # plan, delivered by the one mechanism this repository already has for "tell Maulik
    # something at 21:05" (`dispatch` writes the log line, the Sentry breadcrumb and the email).
    # A second mailer for one sleeve's digest would be a second place for an address, a
    # transport and a failure to hide; `docs/twt/05` specifies no email template, so this is the
    # summary and the page is the list. DECISIONS-TW TW6.2.
    TWT_EVENING = "TWT_EVENING"
    TWT_POSITION_NAKED = "TWT_POSITION_NAKED"
    TWT_GTT_MISSING_AT_1515 = "TWT_GTT_MISSING_AT_1515"
    TWT_ADJUSTMENT_RESET = "TWT_ADJUSTMENT_RESET"
    # OP2 (docs/options/03 §2): the NFO master changed under the options calendar — a lot size
    # moved, an expiry changed kind, or a listed future expiry was withdrawn (a holiday shift).
    # Sizing and each sleeve's day role both depend on it, so a person looks before the open.
    OPTIONS_MASTER_CHANGED = "OPTIONS_MASTER_CHANGED"
    # OP6 (docs/options/06 OP6): an O1 sleeve's morning — the verdict, or the four-leg plan with
    # its credit, lots, costs, margin and expiry. Like TWT_EVENING it is not a failure: it is the
    # plan, delivered by the one mechanism that already tells Maulik something (DECISIONS-OP OP6.7).
    OPTIONS_PLAN = "OPTIONS_PLAN"
    # OP11: the options book's week, one line per (sleeve, simulated, sizing mode) — never pooled
    # (docs/options/04 §12). Read-only; like OPTIONS_PLAN it is information, not a failure.
    OPTIONS_WEEKLY = "OPTIONS_WEEKLY"
    # FO10 (docs/fno/06 FO10): the FO book's week, one line per (sleeve, simulated), the month
    # per book and the 04 §7 pauses per mode — never pooled. Information, not a failure.
    FNO_WEEKLY = "FNO_WEEKLY"
    # FO12 (docs/fno/06 FO12): the FO book's events — a plan to confirm, an exit done, a hard exit
    # tomorrow, a LATE_EXIT the desk recorded, and a night's F&O bhavcopy MISSING at 23:30. The
    # desk writes the rows; the worker reads them and raises each once (baskfy_worker.fno.alerts).
    FNO_PLAN = "FNO_PLAN"
    FNO_EXIT = "FNO_EXIT"
    FNO_HARD_EXIT_TOMORROW = "FNO_HARD_EXIT_TOMORROW"
    FNO_LATE_EXIT = "FNO_LATE_EXIT"
    FNO_BHAVCOPY_MISSING = "FNO_BHAVCOPY_MISSING"
    # OP14 (docs/options/05 §4): the options book's five Prometheus rules, verbatim, and the
    # worker's in-process checks (09:20, 10:20, each hard exit + 3 min, 15:35), which raise the
    # failure the moment it is about rather than waiting for a scrape.
    OPTIONS_OPEN_AFTER_HARD_EXIT = "OPTIONS_OPEN_AFTER_HARD_EXIT"
    OPTIONS_COLLECTOR_GAP = "OPTIONS_COLLECTOR_GAP"
    OPTIONS_SCAN_STALE = "OPTIONS_SCAN_STALE"
    OPTIONS_LIMITER_SHARE_HIGH = "OPTIONS_LIMITER_SHARE_HIGH"
    OPTIONS_NO_SESSION_ON_TRADING_DAY = "OPTIONS_NO_SESSION_ON_TRADING_DAY"
    OPTIONS_CHECK_FAILED = "OPTIONS_CHECK_FAILED"


#: How long the webhook is given before it is abandoned. An alert that blocks the reaper for
#: thirty seconds delays the next alert behind it.
WEBHOOK_TIMEOUT_SECONDS: Final = 5.0

_warned_no_sink = False


@dataclass(frozen=True, slots=True)
class Alert:
    """One thing an operator needs to know about, with everything they need to act on it."""

    name: AlertName
    severity: Severity
    summary: str
    #: Low-cardinality identifiers: ``trade_date``, ``run_id``, ``step``, ``queue``. These become
    #: Prometheus labels and Sentry tags, so they are strings and there are few of them.
    labels: dict[str, str] = field(default_factory=dict)
    #: Anything else worth having in the body of the page — an error payload, a failing assertion
    #: list, a row count. Never a label.
    detail: JsonObject = field(default_factory=dict)
    #: The runbook that tells whoever is paged what to do next. Prompt 17 deliverable 5 writes
    #: them; naming the file on the alert itself is what makes them get read.
    runbook: str = ""

    def as_dict(self) -> JsonObject:
        return {
            "alert": self.name.value,
            "severity": self.severity.value,
            "summary": self.summary,
            "labels": dict(self.labels),
            "detail": dict(self.detail),
            "runbook": self.runbook,
            "raised_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        }


class AlertRaised(Exception):
    """Carries an :class:`Alert` to Sentry.

    Sentry groups by exception type and message, so an alert that arrives as a bare
    ``capture_message`` lands in whatever issue the SDK decides. A real exception type with the
    alert name in its message groups one issue per rule, which is what an operator wants.
    """

    def __init__(self, alert: Alert) -> None:
        self.alert = alert
        super().__init__(f"{alert.name.value}: {alert.summary}")


async def dispatch(
    alert: Alert,
    settings: Settings | None = None,
    *,
    mailer: Mailer | None = None,
    suppressed: str | None = None,
) -> JsonObject:
    """Raise ``alert`` to every configured sink. Never raises. Returns what was sent.

    ``suppressed`` is the one way to record an alert *without* delivering it: the counter and the
    ``alert`` log line still happen — they are the evaluation record, and a Loki rule may count
    them — but Sentry, email and the webhook are skipped, and the reason is logged and returned.
    It exists for a check that re-evaluates a condition someone has already been told about
    (``baskfy_worker.ops.run_kite_token_check``, "once per token per day"); a check that wants
    silence should return no alert at all.

    Async because the mail transport is (``baskfy_api.email.Mailer.deliver``), and every caller —
    the orchestrator, the ops tasks — is already inside a coroutine. Wrapping an ``asyncio.run``
    around a synchronous version would deadlock the first time an alert was raised from inside the
    pipeline's own event loop.

    The return value is what the Celery task echoes back, so ``celery inspect`` and the admin
    page can both show the alert that fired without reading the logs.
    """
    resolved = settings or get_settings()
    payload = alert.as_dict()

    ALERTS.labels(alert.name.value, alert.severity.value).inc()
    annotate_current_span({"baskfy.alert": alert.name.value})
    log.error(
        "alert",
        extra={
            "alert": alert.name.value,
            "severity": alert.severity.value,
            "summary": alert.summary,
            "runbook": alert.runbook,
            **({"suppressed": suppressed} if suppressed is not None else {}),
            **{f"alert_{key}": value for key, value in alert.labels.items()},
        },
    )

    delivered: list[str] = ["log"]
    if suppressed is not None:
        payload["delivered_to"] = delivered
        payload["suppressed"] = suppressed
        return payload
    if sentry.sentry_is_active():
        sentry.capture(AlertRaised(alert), alert=alert.name.value, severity=alert.severity.value)
        delivered.append("sentry")
    if resolved.ops_alert_email and await _send_email(alert, resolved, mailer):
        delivered.append("email")
    if resolved.ops_alert_webhook_url:
        # `to_thread` because `_post_webhook` is a blocking `urllib` call and this runs inside the
        # pipeline's event loop; a five-second webhook timeout must not stall the run that raised
        # the alert.
        posted: bool = await asyncio.to_thread(_post_webhook, alert, resolved)
        if posted:
            delivered.append("webhook")

    _warn_if_unheard(delivered)
    payload["delivered_to"] = delivered
    return payload


def webhook_payload(alert: Alert) -> JsonObject:
    """The body posted to ``BASKFY_OPS_ALERT_WEBHOOK_URL``.

    Shaped like an Alertmanager v2 webhook entry so the same receiver can take alerts from here
    and from Prometheus without a second parser: ``labels`` (with ``alertname`` and ``severity``)
    and ``annotations`` (with ``summary`` and ``runbook_url``).
    """
    return {
        "status": "firing",
        "labels": {
            "alertname": alert.name.value,
            "severity": alert.severity.value,
            "service": "decile",
            **alert.labels,
        },
        "annotations": {"summary": alert.summary, "runbook_url": alert.runbook},
        "startsAt": dt.datetime.now(tz=dt.UTC).isoformat(),
        "detail": dict(alert.detail),
    }


async def _send_email(alert: Alert, settings: Settings, mailer: Mailer | None) -> bool:
    """Email the on-call address, through the same transport every other mail goes through.

    Deliberately not one of the rendered templates in ``baskfy_api.email.templates``: those are
    written for customers, and an operational page wants the whole payload verbatim, including
    the fields a template would have dropped.
    """
    body = json.dumps(alert.as_dict(), indent=2, default=str)
    message = Message(
        to=settings.ops_alert_email,
        subject=f"[decile/{settings.environment}] {alert.severity.value}: {alert.summary}",
        text=f"{alert.summary}\n\nRunbook: {alert.runbook or '(none)'}\n\n{body}\n",
        html=(
            f"<p><strong>{escape(alert.summary)}</strong></p>"
            f"<p>Runbook: {escape(alert.runbook or '(none)')}</p>"
            f"<pre>{escape(body)}</pre>"
        ),
    )
    try:
        transport = mailer or Mailer(build_transport(settings))
        return await transport.deliver(message)
    except Exception as exc:  # see the module docstring: a sink never propagates
        log.error("alert email failed", extra={"alert": alert.name.value, "error": str(exc)})
        return False


def _post_webhook(alert: Alert, settings: Settings) -> bool:
    """POST the alert as JSON. Synchronous and short — this runs inside a Celery task."""
    body = json.dumps(webhook_payload(alert), default=str).encode("utf-8")
    request = urllib.request.Request(  # the URL is operator configuration, not input
        settings.ops_alert_webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS):
            return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.error("alert webhook failed", extra={"alert": alert.name.value, "error": str(exc)})
        return False


def _warn_if_unheard(delivered: list[str]) -> None:
    """Say once, loudly, that alerts are going nowhere but the log."""
    global _warned_no_sink  # noqa: PLW0603 - one warning per process, not one per alert
    if delivered != ["log"] or _warned_no_sink:
        return
    _warned_no_sink = True
    log.warning(
        "alerts have no delivery sink configured; set BASKFY_SENTRY_DSN, "
        "BASKFY_OPS_ALERT_EMAIL or BASKFY_OPS_ALERT_WEBHOOK_URL"
    )
