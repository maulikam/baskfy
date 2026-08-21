"""API keys, screen alerts and outbound webhooks — PROMPTS.md Prompt 20.

Six tables, none of them in ``docs/04-data-model.md``, because Prompt 20 builds three features the
data model was written before: the public read API (deliverable 1 and 2), screen alerts
(deliverable 3) and webhooks (deliverable 4). Each is recorded in ``docs/DECISIONS.md`` §20 —
the overnight build may append there and nowhere else under ``docs/`` — and
``packages/core/tests/test_schema_matches_docs.py`` asserts that every one of them is.

``api_key`` / ``api_key_usage_daily``
    "Keys are hashed at rest and shown once", with "per-key rate limits, and a usage dashboard".
    The key row holds a digest and a clear-text *prefix*; the usage row is the dashboard's source.

``screen_alert`` / ``screen_alert_delivery``
    "A user subscribes a screen to a schedule (daily/weekly after publish)". The delivery row is
    what makes a re-run of a night's dispatch idempotent (CLAUDE.md house rule 7): it is keyed by
    ``(alert, as_of)``, so the second attempt at the same trade date sends nothing.

``webhook_endpoint`` / ``webhook_delivery``
    "Webhooks for entries/exits on a screen, with HMAC signing and retry with backoff." The
    endpoint holds no secret — see :class:`WebhookEndpoint` — and the delivery row carries the
    attempt count and the next attempt time that the backoff schedule is computed from.

Why the signing secret is not a column
--------------------------------------
An HMAC signature needs the secret in the clear at signing time, so "hash it at rest" — the
answer for an API key — is not available. The alternatives are to store it encrypted (a key to
manage, a decryption on every delivery) or to *derive* it. This schema derives:
``secret = HMAC(master, endpoint.public_id || secret_version)``. Rotation is
``secret_version += 1``; nothing secret is ever written to a row; and a database dump on its own
yields no signing key. See ``decile_api.webhooks``.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from decile_core.models.base import Base, BigIntPk, CreatedAt, JsonObject, UpdatedAt

#: docs/07 §Conventions rate-limits by caller; Prompt 20 §1 asks for "per-key rate limits". NULL
#: means "the deployment's API-key tier", which docs/07 fixes at 600/min.
#: A number means this key alone is metered differently.
API_KEY_DEFAULT_RATE_LIMIT: int | None = None

#: PROMPTS.md Prompt 20 §3: "a schedule (daily/weekly after publish)".
ALERT_FREQUENCIES: tuple[str, ...] = ("daily", "weekly")

#: What one dispatch attempt concluded. ``skipped`` is a first-class outcome, not an absence: a
#: night where the screen did not change must be distinguishable from a night the job never ran.
ALERT_DELIVERY_STATUSES: tuple[str, ...] = ("sent", "skipped", "failed")

#: PROMPTS.md Prompt 20 §4: "Webhooks for **entries/exits** on a screen."
WEBHOOK_EVENTS: tuple[str, ...] = ("screen.entries", "screen.exits")

WEBHOOK_DELIVERY_STATUSES: tuple[str, ...] = ("pending", "delivered", "failed")


class ApiKey(Base):
    """One credential for docs/07's ``X-API-Key`` header.

    ``token_hash`` is SHA-256 of the secret half and the secret is never stored, so the plaintext
    exists exactly once — in the response to the request that created it. ``prefix`` is the
    clear-text half the lookup indexes on and the UI displays.

    ``revoked_at`` is checked on **every** request rather than cached, which is what Prompt 20's
    first acceptance criterion — "a revoked key is rejected within one second (no cached-auth
    window)" — actually requires. See ``decile_api.api_keys.authenticate``.
    """

    __tablename__ = "api_key"
    __table_args__ = (
        Index("ix_api_key_user_id", "user_id"),
        Index("uq_api_key_prefix", "prefix", unique=True),
    )

    id: Mapped[BigIntPk]
    public_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    #: What the owner called it. Required: an unlabelled key is one nobody dares revoke.
    name: Mapped[str] = mapped_column(String, nullable=False)
    prefix: Mapped[str] = mapped_column(String, nullable=False)
    token_hash: Mapped[str] = mapped_column(String, nullable=False)
    #: ``decile_core.api_keys.Scope`` values. Every one of them is a read.
    scopes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    #: NULL = the deployment's API-key tier (docs/07: 600/min).
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[CreatedAt]
    #: Advanced on use. Approximate by design — see ``decile_api.api_keys`` for the write policy.
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String)
    #: Set on the *new* key produced by a rotation, pointing at the one it replaced, so an
    #: operator can see that a key was rotated rather than that two unrelated keys exist.
    rotated_from_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("api_key.id"))

    def is_active(self, *, now: dt.datetime | None = None) -> bool:
        moment = now or dt.datetime.now(tz=dt.UTC)
        if self.revoked_at is not None:
            return False
        if self.expires_at is None:
            return True
        expiry = self.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=dt.UTC)
        return expiry > moment


class ApiKeyUsageDaily(Base):
    """One row per key per UTC day — the "usage dashboard" of Prompt 20 §1.

    Daily granularity rather than per-request rows: a usage dashboard answers "how much did this
    key do, and when did it stop working", and a table with one row per request answers that no
    better while growing without bound. Counters are incremented with an upsert, so a concurrent
    burst cannot lose a count.
    """

    __tablename__ = "api_key_usage_daily"
    __table_args__ = (PrimaryKeyConstraint("api_key_id", "date"),)

    api_key_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("api_key.id", ondelete="CASCADE")
    )
    #: UTC, not IST. A usage quota is not a trading day, and picking the market's calendar for it
    #: would make a key's midnight depend on which country the caller is in.
    date: Mapped[dt.date] = mapped_column(Date)
    requests: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    #: Requests refused by the rate limiter. Kept apart from ``requests`` so a dashboard can show
    #: "you are being throttled" rather than only "you are busy".
    throttled: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    updated_at: Mapped[UpdatedAt]


class ScreenAlert(Base):
    """A subscription of one account to one screen's changes.

    ``unsubscribe_token_hash`` follows the same rule as every other token in this codebase: the
    plaintext goes in the email, SHA-256 goes in the row, and the endpoint looks up by digest
    (``decile_api.security``). That is what lets one-click unsubscribe work without a session.

    ``digest`` is the "digest preference" of Prompt 20 §3. True means *this screen's* changes are
    folded into one combined email covering every digest alert the account holds; False means it
    gets its own message. Per alert rather than per account so a user can have one screen shout
    and five whisper — and because a per-account preference would need a column on ``app_user``,
    which docs/04 defines.
    """

    __tablename__ = "screen_alert"
    __table_args__ = (
        CheckConstraint("frequency IN ('daily', 'weekly')", name="screen_alert_frequency"),
        CheckConstraint("min_move >= 1", name="screen_alert_min_move"),
        UniqueConstraint("user_id", "screen_id", name="uq_screen_alert_user_id_screen_id"),
        Index("uq_screen_alert_unsubscribe_token_hash", "unsubscribe_token_hash", unique=True),
    )

    id: Mapped[BigIntPk]
    public_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    screen_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("screen.id", ondelete="CASCADE"), nullable=False
    )
    frequency: Mapped[str] = mapped_column(String, nullable=False)
    #: For ``weekly``: 0 = Monday .. 4 = Friday, matching ``date.weekday()``. NULL on a daily
    #: alert. A weekly alert with no weekday defaults to Friday at dispatch time.
    weekday: Mapped[int | None] = mapped_column(SmallInteger)
    #: Diff only the first N ranks. NULL diffs the whole result set — see
    #: ``decile_core.screen_diff.diff_runs``.
    top_n: Mapped[int | None] = mapped_column(Integer)
    min_move: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    digest: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    unsubscribe_token_hash: Mapped[str] = mapped_column(String, nullable=False)
    #: The ``screen_run`` this alert last reported *up to*. The next dispatch diffs against it.
    last_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("screen_run.id", ondelete="SET NULL")
    )
    last_sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class ScreenAlertDelivery(Base):
    """One night's outcome for one alert. Unique on ``(alert, as_of)``, which is the idempotency.

    CLAUDE.md house rule 7: "Re-running any day's job produces identical rows." A dispatch that
    has already recorded a row for a trade date sends nothing the second time.
    """

    __tablename__ = "screen_alert_delivery"
    __table_args__ = (
        CheckConstraint(
            "status IN ('sent', 'skipped', 'failed')", name="screen_alert_delivery_status"
        ),
        UniqueConstraint("alert_id", "as_of", name="uq_screen_alert_delivery_alert_id_as_of"),
    )

    id: Mapped[BigIntPk]
    alert_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("screen_alert.id", ondelete="CASCADE"), nullable=False
    )
    as_of: Mapped[dt.date] = mapped_column(Date, nullable=False)
    previous_as_of: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String, nullable=False)
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    exit_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    change_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: Why it was skipped, or what failed. Never the email body.
    detail: Mapped[JsonObject | None] = mapped_column(JSONB)
    created_at: Mapped[CreatedAt]


class WebhookEndpoint(Base):
    """Where to POST a screen's entries and exits. **Holds no secret** — see the module docstring.

    ``consecutive_failures`` and ``disabled_at`` are the circuit breaker: an endpoint that has
    failed every attempt for long enough is switched off and its owner told, rather than retried
    forever. An integration whose URL has been repointed at someone else's server is exactly the
    case a permanent retry loop turns into an incident.
    """

    __tablename__ = "webhook_endpoint"
    __table_args__ = (
        Index("ix_webhook_endpoint_user_id", "user_id"),
        UniqueConstraint("user_id", "screen_id", "url", name="uq_webhook_endpoint_target"),
    )

    id: Mapped[BigIntPk]
    public_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    screen_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("screen.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(String, nullable=False)
    #: Bumped by a rotation. The signing secret is derived from ``(public_id, secret_version)``.
    secret_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    events: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    disabled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_reason: Mapped[str | None] = mapped_column(String)
    last_delivery_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class WebhookDelivery(Base):
    """One attempt-bearing outbound message.

    ``idempotency_key`` is unique per endpoint and is built from the screen, the trade date and
    the event, so a dispatch that runs twice for one night enqueues one delivery. ``attempts``
    and ``next_attempt_at`` are what the backoff schedule in ``decile_api.webhooks`` computes
    from; the sweeper picks up rows whose ``next_attempt_at`` has passed.
    """

    __tablename__ = "webhook_delivery"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'delivered', 'failed')", name="webhook_delivery_status"
        ),
        UniqueConstraint(
            "endpoint_id", "idempotency_key", name="uq_webhook_delivery_endpoint_id_key"
        ),
        Index("ix_webhook_delivery_next_attempt_at", "status", "next_attempt_at"),
    )

    id: Mapped[BigIntPk]
    endpoint_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("webhook_endpoint.id", ondelete="CASCADE"), nullable=False
    )
    event: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: The transport-level outcome of the last attempt. A status code, or NULL if nothing answered.
    response_status: Mapped[int | None] = mapped_column(Integer)
    #: Truncated. Never the response body in full: a failing endpoint can return anything.
    last_error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[CreatedAt]
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
