"""Staff-only tables (PROMPTS.md Prompt 17 deliverable 4).

    "/admin (staff-only): pipeline run history with per-step detail and a re-run button,
     data_version history, provider health, user lookup, entitlement override, and a
     reprocess-instrument action."

Three of those seven need storage that ``docs/04`` does not define, so all three are additions,
recorded in ``docs/DECISIONS.md`` §17 alongside ``docs/04c``'s precedent for the auth tables:

``app_user.is_staff``
    docs/09 §Observability says ``/admin/pipeline`` is "behind staff auth" and nothing in the
    bundle says what makes a user staff. A boolean column on the account is the smallest thing
    that can answer it, and it is a *server* fact — the web app reads it from ``GET /me`` and
    renders accordingly, exactly as it does for entitlements. Defined on ``AppUser`` itself
    (``baskfy_core.models.accounts``) rather than here, because a second table joined on every
    request to answer one bit would be a worse shape, not a better one.

:class:`EntitlementOverride`
    "entitlement override" implies a grant that is not derived from a subscription. It cannot be
    a write to ``plan.features`` (that would change what *every* account on the plan gets) and it
    cannot be a fake ``subscription`` row (that would make the billing history lie). So it is its
    own row, with an author, a reason and an expiry, and ``baskfy_api.entitlements`` consults it
    after resolving the plan.

:class:`AdminAction`
    Nothing in the bundle asks for an audit log. Every action on the admin surface is either
    privileged (granting entitlements) or expensive (re-running a pipeline, reprocessing an
    instrument), and an undo-less privileged action with no record of who took it is the kind of
    hole that is only noticed after it matters. One row per action, written in the same
    transaction as the action itself.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.models.base import Base, BigIntPk, CreatedAt, JsonObject

#: What an override may do to one feature. ``grant`` and ``revoke`` rather than a boolean so the
#: absence of a row and an explicit "no" are distinguishable — a support grant that is later
#: withdrawn should read as withdrawn, not as never made.
OVERRIDE_EFFECTS: tuple[str, ...] = ("grant", "revoke")

#: The admin actions worth recording. Open-ended in the column (a text field, not an enum type),
#: closed here so a reader can see the whole surface at once.
ADMIN_ACTIONS: tuple[str, ...] = (
    "pipeline_rerun",
    "instrument_reprocess",
    "entitlement_override_set",
    "entitlement_override_cleared",
)


class EntitlementOverride(Base):
    """A staff grant or revocation of one feature for one account.

    ``feature`` is a :class:`baskfy_core.entitlements.Feature` value, or
    :data:`baskfy_core.entitlements.MAX_SCREENS_KEY` — in which case ``value`` carries the number.
    Keeping both in one table is what lets ``baskfy_api.entitlements`` apply overrides in a single
    pass rather than in one pass per kind of thing that can be overridden.

    ``expires_at`` is NULL for "until someone clears it". A support grant with no expiry is how a
    trial quietly becomes permanent, so the admin UI defaults to a date — but the column allows
    NULL because a genuine comp account is a real case and faking it with the year 2999 is worse.
    """

    __tablename__ = "entitlement_override"
    __table_args__ = (
        CheckConstraint("effect IN ('grant', 'revoke')", name="entitlement_override_effect"),
        Index(
            "uq_entitlement_override_user_feature",
            "user_id",
            "feature",
            unique=True,
        ),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    feature: Mapped[str] = mapped_column(String, nullable=False)
    effect: Mapped[str] = mapped_column(String, nullable=False)
    #: Only meaningful for ``max_screens``; NULL for the boolean features.
    value: Mapped[int | None] = mapped_column(BigInteger)
    #: Who granted it. NOT NULL: an override with no author is exactly the row this table exists
    #: to make impossible.
    granted_by_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[CreatedAt]

    def is_active(self, *, now: dt.datetime) -> bool:
        """``now`` is required — Law 1 forbids core from reading the wall clock (AF 3.10)."""
        moment = now
        if self.expires_at is None:
            return True
        expiry = self.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=dt.UTC)
        return expiry > moment


class AdminAction(Base):
    """One privileged action, and who took it.

    Append-only by convention — nothing in the codebase updates or deletes a row. ``target`` is a
    free-text identifier of whatever the action acted on (a trade date, a symbol, a user's
    ``public_id``) because the three actions do not share a key space and inventing a polymorphic
    foreign key for an audit log would buy referential integrity at the cost of ever being able to
    record an action against something that has since been deleted — which is precisely when an
    audit log earns its keep.
    """

    __tablename__ = "admin_action"
    __table_args__ = (Index("ix_admin_action_created_at", "created_at"),)

    id: Mapped[BigIntPk]
    actor_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String, nullable=False)
    target: Mapped[str] = mapped_column(String, nullable=False)
    #: Everything else worth keeping: the request body, the task id an enqueue returned, the
    #: previous value an override replaced.
    detail: Mapped[JsonObject | None] = mapped_column(JSONB)
    created_at: Mapped[CreatedAt]


#: Re-exported so a migration can reference the type without importing SQLAlchemy's `Boolean`
#: from three places. ``app_user.is_staff`` is declared on :class:`baskfy_core.models.AppUser`.
STAFF_FLAG_TYPE = Boolean
