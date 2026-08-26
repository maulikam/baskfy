"""``app_user.session_epoch`` — the first thing that can end a web session.

Until now nothing could. The web session is an Auth.js JWT cookie with a thirty-day life, and the
``jwt`` strategy is forced: Auth.js v5 cannot use database sessions with the Credentials provider
(``docs/08a`` §3). A self-contained JWT is valid until it expires regardless of what this database
thinks, so sign-out deleted the browser's copy of a credential that stayed good, and
``revoke_all_for_user`` revoked ``auth_session`` rows that the web app has never read — it mints
its own access tokens from the shared secret and ``current_principal`` checks only that ``sub``
names a row in ``app_user``.

The visible consequence was that **changing a password did not evict whoever prompted the change**,
which is the one thing a password change exists to do. ``NEEDS-MAULIK.md`` §22 is the finding.

This column is the fix: a generation number stamped into the session at sign-in and compared on
every gated render against ``GET /me``. Bumping it invalidates every cookie issued before the bump.

WHY A COUNTER AND NOT A TIMESTAMP
----------------------------------
``sessions_valid_after TIMESTAMPTZ`` is the other common shape and it is worse here for one
reason: it makes correctness depend on two clocks agreeing. A token minted a second before a
revocation, by a web container whose clock runs slightly fast, compares as still-valid. An integer
has no such failure mode — the token either carries the current number or it does not — and the
comparison is exact on any clock. The cost is that "revoke everything issued before 4pm" is not
expressible, and nothing needs it.

WHY NOT NULL WITH A SERVER DEFAULT
-----------------------------------
A nullable epoch would make "no epoch" a third state that every comparison has to decide about,
and the safe reading of "I do not know this session's generation" is "refuse it" — which would
sign out every existing session the moment this deploys. ``DEFAULT 0`` backfills every existing
row to the same generation as the sessions they already hold, so this migration is invisible to
anybody signed in when it runs. That is deliberate: a security fix that logs the whole userbase
out on deploy teaches people that being logged out is normal.

Revision ID: 0025_session_epoch
Revises: 0024_portfolio_kind_default
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_session_epoch"
down_revision: str | None = "0024_portfolio_kind_default"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column(
            "session_epoch",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    # Dropping this re-opens the hole rather than corrupting anything: sessions stop being
    # checkable, they do not become invalid. Safe to run, and worth nobody's confidence.
    op.drop_column("app_user", "session_epoch")
