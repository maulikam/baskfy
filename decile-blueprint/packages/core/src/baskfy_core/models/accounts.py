"""Accounts, billing, portfolios, backtests and pipeline bookkeeping — docs/04-data-model.md."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.allocation_ledger import PortfolioKind, PortfolioSource
from baskfy_core.models.base import (
    INR,
    MONEY,
    PRICE_RAW,
    QUANTITY,
    Base,
    BigIntPk,
    CreatedAt,
    JsonObject,
    SmallIntPk,
)

SUBSCRIPTION_STATUSES: tuple[str, ...] = ("active", "past_due", "cancelled", "expired")
BACKTEST_STATUSES: tuple[str, ...] = ("queued", "running", "done", "failed")
PIPELINE_STATUSES: tuple[str, ...] = ("running", "succeeded", "failed", "aborted")
PLAN_INTERVALS: tuple[str, ...] = ("month", "year")
#: docs/04 does not enumerate ``payment.status``. Razorpay's own vocabulary for a payment, minus
#: the states we never persist (``authorized`` is transient; we record a charge once captured).
PAYMENT_STATUSES: tuple[str, ...] = ("created", "captured", "failed", "refunded")

#: ``portfolio_sleeve.kind``. ``basket`` joins ``screen`` and ``manual`` in migration 0019 so the
#: curated-basket half of the product can be one slice of a portfolio rather than a parallel
#: universe. Each kind names exactly one source column — see :class:`PortfolioSleeve`.
SLEEVE_KINDS: tuple[str, ...] = ("screen", "manual", "basket")

#: The deepest a ``portfolio`` chain may run, counting the root as depth 1. The database enforces
#: only "a portfolio is not its own parent"; the cap and cycle-freedom are graph invariants that
#: ``baskfy_core.portfolio_graph`` asserts before a write, because neither is expressible as a
#: row-local ``CHECK``.
MAX_PORTFOLIO_DEPTH: int = 6


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[BigIntPk]
    public_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    #: citext so e-mail uniqueness is case-insensitive without a functional index (docs/04).
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    #: argon2id; NULL for OTP-only users.
    password_hash: Mapped[str | None] = mapped_column(String)
    name: Mapped[str | None] = mapped_column(String)
    email_verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[CreatedAt]
    #: Set when erasure is requested (Prompt 12 §5, DPDP). The account stops authenticating
    #: immediately; `account_deletion.purge_after` is when the rows actually go. NOT in docs/04 —
    #: see `docs/04c`. Deleting on the spot would be irreversible on a misclick.
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: docs/09 §Observability: `/admin/pipeline` is "behind staff auth", and nothing in the bundle
    #: says what makes an account staff. This is that answer — a server fact, set by hand or by a
    #: migration, never by a self-service form. NOT in docs/04; see `baskfy_core.models.admin`
    #: and `docs/DECISIONS.md` §17.
    is_staff: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    #: The generation number of this account's *web* sessions, and the only way one can be killed.
    #:
    #: The web session is an Auth.js JWT cookie with a thirty-day life, and `jwt` strategy is
    #: forced — Auth.js v5 cannot use database sessions with the Credentials provider
    #: (`docs/08a` §3). A self-contained JWT is valid until it expires no matter what this
    #: database thinks, so before this column existed there was nothing a server could do to end
    #: a session: sign-out deleted the browser's copy, and `revoke_all_for_user` revoked
    #: refresh-session rows that the web app never reads. The consequence was that changing a
    #: password did not evict whoever prompted the change, which is the one thing a password
    #: change is for. `NEEDS-MAULIK.md` §22 is the finding; this is the fix.
    #:
    #: The number is stamped into the session at sign-in and re-checked on every gated render
    #: against `GET /me`. Bumping it makes every cookie issued before the bump fail that
    #: comparison, so "sign out everywhere" costs one `UPDATE` and takes effect on the next
    #: navigation rather than in thirty days.
    #:
    #: Monotonic and never reset — it is a generation counter, not a count of anything a user
    #: would recognise, and reuse of a number would resurrect a session that was meant to die.
    session_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )


class AuthIdentity(Base):
    """One external identity — today always Google — bound to one ``app_user``.

    WHY THIS IS NOT JUST A COLUMN ON ``app_user``
    ---------------------------------------------
    A ``google_subject`` column would work exactly until the second provider, and it would make
    "signed in with Google" a property of the account rather than a fact with its own history.
    A row per identity means adding Apple or a broker SSO later is an insert, not a migration of
    every user row.

    WHY IT IS KEYED ON ``subject`` AND NOT ON THE EMAIL
    ---------------------------------------------------
    Google's ``sub`` is stable for the life of the account and is never reissued. The email is
    neither: a Workspace administrator can rename a user, and a released consumer address can be
    registered by a *different person* months later. Keying on the address would hand that person
    the original account. The email is still stored on ``app_user`` because the product shows it
    and mails to it — but it is a display fact, not an identity.
    """

    __tablename__ = "auth_identity"
    __table_args__ = (
        # The pair is what a sign-in looks up, and it must be unique or one Google account could
        # be bound to two users and the lookup would be a coin toss.
        UniqueConstraint("provider", "subject", name="uq_auth_identity_provider_subject"),
        # One account per provider per user: signing in with Google twice must not accumulate
        # rows, and it is what makes the upsert on sign-in idempotent (house rule 7).
        UniqueConstraint("user_id", "provider", name="uq_auth_identity_user_provider"),
        Index("ix_auth_identity_user_id", "user_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id"), nullable=False)
    #: ``google`` today. A short catalog string, not an enum, so a second provider needs no
    #: migration of this column's type.
    provider: Mapped[str] = mapped_column(String, nullable=False)
    #: The provider's stable subject identifier (Google's ``sub``).
    subject: Mapped[str] = mapped_column(String, nullable=False)
    #: What the provider last told us this identity's address was, for support and for noticing
    #: a rename. Never used to *find* the account.
    email_at_provider: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[CreatedAt]
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class BrokerAccount(Base):
    """One broker login belonging to one ``app_user`` (docs/05 P4.1 / P4.2 schema).

    The row is the tenant's ``broker_account_id``. Encrypted tokens are P4.2 and are not
    stored here yet — this table exists so order-shaped rows can name an account without
    inventing a second identifier. One Zerodha row per user is the founder backfill;
    a second broker is another row, not a column.
    """

    __tablename__ = "broker_account"
    __table_args__ = (
        UniqueConstraint("user_id", "broker_id", name="uq_broker_account_user_broker"),
        Index("ix_broker_account_user_id", "user_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id"), nullable=False)
    #: Catalog id from ``baskfy_core.broker_connections`` (``zerodha``, ``upstox``, …).
    broker_id: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'primary'"))
    #: The broker's own user/client id, when known. NULL until the first OAuth sync.
    kite_user_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[CreatedAt]


class Plan(Base):
    __tablename__ = "plan"
    __table_args__ = (
        CheckConstraint("interval IS NULL OR interval IN ('month', 'year')", name="plan_interval"),
    )

    id: Mapped[SmallIntPk]
    code: Mapped[str] = mapped_column(String, unique=True)
    price_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    #: 'month' | 'year' | NULL (NULL = the one-time "Forever" plan).
    interval: Mapped[str | None] = mapped_column(String)
    features: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)


class Subscription(Base):
    __tablename__ = "subscription"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'past_due', 'cancelled', 'expired')", name="subscription_status"
        ),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id"), nullable=False)
    plan_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("plan.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_end: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    razorpay_subscription_id: Mapped[str | None] = mapped_column(String)
    razorpay_customer_id: Mapped[str | None] = mapped_column(String)


class Payment(Base):
    """One charge, and the tax invoice raised for it — docs/04, plus the GST columns docs/11 needs.

    docs/04 gives this table ``gst_inr`` and nothing else about tax. docs/11 §"Compliance & legal
    (India)" requires "GST-compliant invoices with GSTIN, HSN/SAC, place of supply", and Rule 46
    of the CGST Rules requires the taxable value and each tax head to appear on the document.
    Those figures are **stored**, not recomputed at render time: an invoice is a document of
    record, and one whose numbers move when a rate setting changes is not one. The added columns
    are listed in ``docs/DECISIONS.md`` (Prompt 13) alongside ``baskfy_core.models.billing``.

    ``amount_inr`` is the gross the customer was charged, so
    ``taxable_inr + cgst_inr + sgst_inr + igst_inr == amount_inr`` exactly (docs/01 §1's prices
    are tax-inclusive — see ``baskfy_core.gst``).
    """

    __tablename__ = "payment"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created', 'captured', 'failed', 'refunded')", name="payment_status"
        ),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id"), nullable=False)
    subscription_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("subscription.id"))
    amount_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    gst_inr: Mapped[Decimal | None] = mapped_column(INR)
    status: Mapped[str] = mapped_column(String, nullable=False)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String, unique=True)
    invoice_number: Mapped[str | None] = mapped_column(String, unique=True)
    invoice_pdf_key: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[CreatedAt]

    # --- not in docs/04; required by docs/11 §Compliance (see the class docstring) ---------
    #: The order the charge belongs to, for reconciliation against the gateway.
    razorpay_order_id: Mapped[str | None] = mapped_column(String)
    #: Rule 46(a): the date of issue. Distinct from ``created_at``, which is a timestamp in UTC.
    invoice_date: Mapped[dt.date | None] = mapped_column(Date)
    taxable_inr: Mapped[Decimal | None] = mapped_column(INR)
    cgst_inr: Mapped[Decimal | None] = mapped_column(INR)
    sgst_inr: Mapped[Decimal | None] = mapped_column(INR)
    igst_inr: Mapped[Decimal | None] = mapped_column(INR)
    #: The rate applied, stored so a later rate change cannot rewrite an issued invoice.
    gst_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    #: docs/11: "place of supply", spelled as the return expects it: ``Karnataka (29)``.
    place_of_supply: Mapped[str | None] = mapped_column(String)
    #: docs/11: "GSTIN" — the *recipient's*, when they are registered. The supplier's is a setting.
    customer_gstin: Mapped[str | None] = mapped_column(String)
    #: docs/11: "HSN/SAC". A service, so SAC.
    sac_code: Mapped[str | None] = mapped_column(String)


def _in_values(column: str, values: type[PortfolioKind] | type[PortfolioSource]) -> str:
    """``kind IN ('CAPITAL', 'MONITORING')`` built from the enum, never typed twice."""
    allowed = ", ".join(f"'{member.value}'" for member in values)
    return f"{column} IN ({allowed})"


class Portfolio(Base):
    """A user's portfolio — since migration 0019, a node in that user's portfolio forest.

    **Nesting.** ``parent_id`` is nullable: NULL is a root, non-NULL points at another portfolio.
    Three invariants govern the forest, and only the first is expressible as a row-local
    constraint, so the other two live in ``baskfy_core.portfolio_graph`` and are checked before a
    write:

    1. *no self-parent* — ``ck_portfolio_portfolio_parent_not_self``, enforced by the database;
    2. *no cycles* — a chain of ``parent_id`` hops must terminate. A ``CHECK`` cannot walk rows;
    3. *depth ≤* :data:`MAX_PORTFOLIO_DEPTH` — likewise multi-row.

    A parent belongs to the same ``user_id`` as its child. That, too, is a cross-row fact: it is
    enforced in the API, which answers a foreign parent with ``NOT_FOUND`` rather than
    ``FORBIDDEN`` so the existence of another tenant's row never leaks.

    ``ON DELETE SET NULL`` on ``parent_id`` is deliberate and follows the reasoning
    :class:`PortfolioSleeve` already records for ``screen_id``: deleting a grouping node must not
    silently delete the money underneath it. The children are promoted to roots, visibly
    detached, rather than cascaded away with their holdings.

    **Broker attribution.** ``broker_account_id`` is nullable, and the two states mean different
    things — this is the column's whole point, so neither may be collapsed into the other:

    * ``NULL`` — a roll-up node that spans brokers. Its figures are sums over its subtree.
    * ``NOT NULL`` — everything under this portfolio is attributable to exactly one broker
      account, so a per-broker view can name it without a join through every holding.
    """

    __tablename__ = "portfolio"
    __table_args__ = (
        # (2) and (3) above are not expressible here; (1) is, and is the one a single bad write
        # can introduce, so the database owns it.
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="portfolio_parent_not_self"),
        # 0021. The values come from the domain enums rather than from strings repeated here, so
        # the database and `baskfy_core.allocation_ledger` cannot drift into disagreeing about
        # what a kind or a source is.
        CheckConstraint(_in_values("kind", PortfolioKind), name="portfolio_kind_known"),
        CheckConstraint(_in_values("source", PortfolioSource), name="portfolio_source_known"),
        # Redundant against the primary key; it exists so `portfolio_holding` can reference
        # `(id, kind)` and have the database keep its copy of `kind` honest.
        UniqueConstraint("id", "kind", name="uq_portfolio_id_kind"),
        Index("ix_portfolio_parent_id", "parent_id"),
        Index("ix_portfolio_broker_account_id", "broker_account_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id"), nullable=False)
    #: NULL = a root. See the class docstring for why ``ON DELETE SET NULL``.
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("portfolio.id", ondelete="SET NULL")
    )
    #: NULL = a roll-up spanning brokers; NOT NULL = attributable to one broker account.
    broker_account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("broker_account.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    #: PORTFOLIO_REDESIGN.md §4.1. CAPITAL sums into consolidated net worth and holds each of its
    #: holdings exclusively; MONITORING is an overlapping lens that never enters a total. The
    #: distinction is arithmetic, not styling, which is why it is a column and not a tag.
    kind: Mapped[str] = mapped_column(String, nullable=False)
    #: §3. Decides the headline return metric (§5.2) and the badge shown on every surface.
    source: Mapped[str] = mapped_column(String, nullable=False)
    #: 0022. The date every §5.2 metric is measured from, and the date the "since grouped" mark
    #: is taken at for a holding group. Backfilled from ``created_at`` and NOT NULL thereafter:
    #: acceptance criterion 3 ties every displayed return to a start date, and a metric with an
    #: implicit start is a number nobody can check.
    started_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    #: 0022. §6.3's benchmark overlay, which §6.5 lets a portfolio override. NULL = no override,
    #: so the surface falls back to the product default rather than to no comparison at all.
    benchmark_index_id: Mapped[int | None] = mapped_column(
        SmallInteger, ForeignKey("index_def.id", ondelete="SET NULL")
    )
    created_at: Mapped[CreatedAt]


class PortfolioHolding(Base):
    """One instrument held in one portfolio **at one broker account** (0019).

    Before 0019 the primary key was ``(portfolio_id, instrument_id)``, which asserted that a
    portfolio holds a name in exactly one place. That is false the moment a user holds INFY at
    two brokers: the second row could not be written, and the first silently stood for both.
    ``broker_account_id`` joins the key so the pair of positions is two rows that a per-broker
    roll-up can add up, instead of one row that is wrong.

    The column is ``NOT NULL`` because it is in the key, and because a share is always held
    *somewhere* — "held, broker unknown" is not a position, it is a missing fact. Rows that
    predate 0019 are attributed by the migration, which reuses the rule 0018 already established
    for ``cb_investment``: the owner's default broker account. The same rule is kept live by
    ``portfolio_holding_attribute_broker_account()``, a ``BEFORE INSERT`` trigger installed by
    0019, so a writer that names no account still produces an attributed row rather than an
    integrity error. A writer that *does* name one is left untouched by the trigger.
    """

    __tablename__ = "portfolio_holding"
    #: The composite primary key leads on ``portfolio_id``, so it cannot serve a predicate on
    #: ``instrument_id`` alone — which is the direction the rebalance join reads.
    __table_args__ = (
        PrimaryKeyConstraint("portfolio_id", "instrument_id", "broker_account_id"),
        Index("ix_portfolio_holding_instrument_id", "instrument_id"),
        Index("ix_portfolio_holding_broker_account_id", "broker_account_id"),
        # 0021. The copy of ``portfolio.kind`` is kept honest by the database rather than by a
        # writer: this key makes disagreement unrepresentable, and ON UPDATE CASCADE carries a
        # portfolio's holdings with it when its kind changes.
        ForeignKeyConstraint(
            ["portfolio_id", "portfolio_kind"],
            ["portfolio.id", "portfolio.kind"],
            name="fk_portfolio_holding_portfolio_kind",
            onupdate="CASCADE",
        ),
        # Acceptance criterion 2, enforced by Postgres: at most one CAPITAL row per physical
        # holding. Monitoring rows are absent from the index, so lenses overlap freely (§4.1).
        Index(
            "uq_portfolio_holding_one_capital_portfolio",
            "instrument_id",
            "broker_account_id",
            unique=True,
            postgresql_where=text("portfolio_kind = 'CAPITAL'"),
        ),
    )

    portfolio_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("portfolio.id", ondelete="CASCADE")
    )
    instrument_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instrument.id"))
    #: No ``ON DELETE`` action: a broker account with holdings against it cannot be deleted out
    #: from under them. ``SET NULL`` is impossible (the column is in the key) and ``CASCADE``
    #: would destroy positions to tidy up a login, so the database refuses instead.
    #: ``FetchedValue`` records that the value may arrive from the trigger described above, so
    #: an INSERT that omits it is legal and the ORM reads the result back.
    broker_account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("broker_account.id"), nullable=False, server_default=FetchedValue()
    )
    #: 0021. A copy of the owning portfolio's ``kind``, and the reason the partial unique index
    #: above can be written at all — an index cannot read another table, so the fact is brought
    #: here and the composite foreign key stops it from ever disagreeing.
    portfolio_kind: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(QUANTITY)
    avg_price: Mapped[Decimal | None] = mapped_column(PRICE_RAW)
    added_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    #: 0022 / §5.3. When these shares were first bought, once something knows — NULL until a CAS
    #: import or a broker that publishes buy history says so. NULL is not "today": §5.2 forbids
    #: since-purchase P&L for a holding whose purchase date is unknown, and a default would
    #: silently unlock exactly the number it forbids.
    first_bought_on: Mapped[dt.date | None] = mapped_column(Date)
    #: 0022 / §5.3. Where ``first_bought_on`` and ``avg_price`` came from: ``NONE``, ``CAS``,
    #: ``BROKER`` or ``MANUAL``. It is provenance, not decoration — "since grouped" upgrades to a
    #: true since-purchase figure only for rows that can say where their history came from.
    history_source: Mapped[str] = mapped_column(String, nullable=False, server_default="NONE")


class PortfolioRebalance(Base):
    """One computed rebalance, kept so the user can see what they were told and when.

    **NOT IN docs/04.** PROMPTS.md Prompt 14 §4 requires it in as many words — "Rebalance history:
    persist each computed rebalance so a user can see what they were told and when" — and docs/04
    stops at ``portfolio`` and ``portfolio_holding``. Same footing as the auth tables of
    ``docs/04c`` and the billing tables of ``baskfy_core.models.billing``: an addition a numbered
    deliverable asks for, recorded in ``docs/DECISIONS.md`` §14.

    ``payload`` is the response body **verbatim**, not a set of columns to re-render from. The
    point of the history is that it is a record of advice given on a date: the screen can be
    edited, the buffer changed, a name delisted, and this row must still say what the user saw.
    Recomputing it from ``screen_id`` + ``as_of`` would produce today's answer under yesterday's
    timestamp, which is the one thing an audit trail may not do.

    ``screen_id`` is ``ON DELETE SET NULL`` rather than ``CASCADE`` for the same reason: deleting
    a screen must not erase the history of what it once told someone. The screen's name and public
    id are inside ``payload``.
    """

    __tablename__ = "portfolio_rebalance"
    __table_args__ = (Index("ix_portfolio_rebalance_portfolio_id", "portfolio_id"),)

    id: Mapped[BigIntPk]
    portfolio_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("portfolio.id", ondelete="CASCADE"), nullable=False
    )
    screen_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("screen.id", ondelete="SET NULL")
    )
    #: The trading day the screen was run for (docs/06 §step 1's resolved as-of).
    as_of: Mapped[dt.date] = mapped_column(Date, nullable=False)
    #: The published data version behind it, so a stale answer is identifiable as stale.
    data_version: Mapped[int | None] = mapped_column(BigInteger)
    top_n: Mapped[int] = mapped_column(Integer, nullable=False)
    hold_buffer: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[CreatedAt]


class Backtest(Base):
    __tablename__ = "backtest"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')", name="backtest_status"
        ),
    )

    id: Mapped[BigIntPk]
    public_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id"), nullable=False)
    screen_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("screen.id"))
    config: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    metrics: Mapped[JsonObject | None] = mapped_column(JSONB)
    equity_curve: Mapped[JsonObject | None] = mapped_column(JSONB)
    #: Large artefacts (per-trade blotter) live in R2; this is the object key.
    trades_key: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[CreatedAt]
    #: When a runner actually claimed it, committed in its own transaction (M45.2).
    #:
    #: `created_at` records the POST, so it cannot separate a run abandoned weeks ago from one
    #: waiting legitimately behind the concurrency cap. Without this column, started-then-died was
    #: byte-identical to never-started and no safe reaper could be written.
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class PipelineRun(Base):
    __tablename__ = "pipeline_run"

    id: Mapped[BigIntPk]
    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Bumped only by step 10 "publish", and only if the quality gate passed (docs/03).
    data_version: Mapped[int | None] = mapped_column(BigInteger)


class PipelineRunStep(Base):
    """One step of one run.

    ``UNIQUE (run_id, step)`` is an addition to docs/04's DDL. PROMPTS.md Prompt 3 deliverable 2
    requires every task to be "an idempotent unit that upserts (never blind-inserts) and writes a
    pipeline_run_step row"; without the constraint, re-running a step appends a second,
    contradictory record and `/admin/pipeline` (docs/09 §Observability) shows two answers for the
    same question. See docs/04b-ingest-cursor-addendum.md.
    """

    __tablename__ = "pipeline_run_step"
    __table_args__ = (UniqueConstraint("run_id", "step"),)

    id: Mapped[BigIntPk]
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("pipeline_run.id", ondelete="CASCADE"), nullable=False
    )
    step: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    rows_in: Mapped[int | None] = mapped_column(BigInteger)
    rows_out: Mapped[int | None] = mapped_column(BigInteger)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[JsonObject | None] = mapped_column(JSONB)


class PortfolioSleeve(Base):
    """One slice of a portfolio, with its own capital and its own source (M34).

    The Rebalance Tracker is a symbol diff and knows nothing about money. A portfolio run as
    several screens needs the other question answered — how much goes where — and that is what a
    sleeve is.

    ``kind`` is one of :data:`SLEEVE_KINDS`: ``screen`` (names come from ``screen_id``),
    ``manual`` (capital the owner runs themselves, reported so the totals are honest and never
    allocated), or — since 0019 — ``basket`` (names come from a curated basket's live version,
    ``basket_id``). The check constraints make the pairing structural rather than conventional:
    each kind names exactly one source column and leaves the other NULL, so "where does this
    sleeve's list come from" is answerable from the row without guessing.

    Deleting a screen sets ``screen_id`` to NULL rather than cascading: capital allocated against a
    screen that no longer exists should become visibly unsourced, not silently vanish.

    .. warning::

       That last sentence describes an intention the schema does not currently deliver, and 0019
       does not repeat the mistake for ``basket_id``. ``ON DELETE SET NULL`` blanks ``screen_id``
       while ``kind`` stays ``'screen'``, which the pairing constraint forbids — so deleting a
       screen that a sleeve references raises a check violation instead of unsourcing the sleeve.
       Making the sleeve genuinely unsourced needs a trigger that also moves ``kind`` to
       ``'manual'``; until that exists, ``basket_id`` takes no ``ON DELETE`` action, so the
       database refuses the delete cleanly rather than half-applying it.
    """

    __tablename__ = "portfolio_sleeve"
    __table_args__ = (
        CheckConstraint("kind IN ('screen', 'manual', 'basket')", name="portfolio_sleeve_kind"),
        CheckConstraint("capital >= 0", name="portfolio_sleeve_capital_non_negative"),
        CheckConstraint("top_n BETWEEN 1 AND 100", name="portfolio_sleeve_top_n"),
        CheckConstraint(
            "(kind = 'screen' AND screen_id IS NOT NULL AND basket_id IS NULL) OR "
            "(kind = 'manual' AND screen_id IS NULL AND basket_id IS NULL) OR "
            "(kind = 'basket' AND basket_id IS NOT NULL AND screen_id IS NULL)",
            name="portfolio_sleeve_source",
        ),
        UniqueConstraint("portfolio_id", "name", name="uq_portfolio_sleeve_name"),
        Index("ix_portfolio_sleeve_basket_id", "basket_id"),
    )

    id: Mapped[BigIntPk]
    portfolio_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("portfolio.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    screen_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("screen.id", ondelete="SET NULL")
    )
    #: Set for a ``basket`` sleeve, NULL otherwise. No ``ON DELETE`` action — see the class
    #: docstring's warning: ``SET NULL`` here would contradict the pairing constraint.
    basket_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("cb_basket.id"))
    capital: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, server_default="0")
    #: How many of the screen's names this sleeve takes, in rank order.
    top_n: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="15")
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    created_at: Mapped[CreatedAt]


# ---------------------------------------------------------------------------
# The portfolio redesign's three data layers — PORTFOLIO_REDESIGN.md §4.6
#
# §4.6 asks for the layers to be kept separate *in the schema*, and the reason is that they have
# different owners and different truths. The broker ledger is what a broker says is there. Market
# data is what the exchange printed. The Baskfy portfolio ledger is the only one this product
# authors — which holding belongs to which portfolio, which cash moved where, and what each day
# was worth. Collapsing them would make it impossible to say, later, which number came from whom.
# ---------------------------------------------------------------------------


class BrokerCash(Base):
    """Layer 1 — the Unallocated cash bucket of §4.4, one per broker account.

    Derived from ``portfolio_cash_flow`` and materialised anyway: §6.2 shows cash in the hero
    row and §6.6 shows it in the Unallocated section, and summing a lifetime of flows to render
    one number on every page load is the cheap version of this.
    """

    __tablename__ = "broker_cash"

    broker_account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("broker_account.id", ondelete="CASCADE"), primary_key=True
    )
    balance: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    #: Which day this balance is true for. §6.1 shows the sync timestamp separately from the
    #: price timestamp precisely so the two are never confused for each other.
    as_of: Mapped[dt.date] = mapped_column(Date, nullable=False)


class PortfolioCashFlow(Base):
    """Layer 3 — every movement of money, and the only input to per-portfolio XIRR (§4.4).

    The distinction this table exists to preserve, in §4.4's own terms: an external deposit lands
    in Unallocated and is not a portfolio event; **assigning** cash to a portfolio is an internal
    inflow and *is* the XIRR cash-flow event; buying a stock inside a portfolio is a cash-to-stock
    transfer and is *not* an XIRR event. A balance column can express none of that after the fact,
    which is why the flows are rows and the balance is derived from them.

    ``portfolio_id`` is NULL exactly when the flow is external, enforced by a check constraint —
    the boundary is data, not convention.
    """

    __tablename__ = "portfolio_cash_flow"
    __table_args__ = (
        Index("ix_portfolio_cash_flow_portfolio_occurred", "portfolio_id", "occurred_on"),
        Index("ix_portfolio_cash_flow_broker_occurred", "broker_account_id", "occurred_on"),
    )

    id: Mapped[BigIntPk]
    broker_account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("broker_account.id", ondelete="CASCADE"), nullable=False
    )
    portfolio_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("portfolio.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    occurred_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    #: Set for BUY/SELL/DIVIDEND, so the activity tab (§7) can name the stock a flow concerned.
    instrument_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("instrument.id"))
    quantity: Mapped[Decimal | None] = mapped_column(QUANTITY)
    note: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[CreatedAt]


class PortfolioNavDaily(Base):
    """Layer 3 — the official end-of-day value, §5.1's "like a fund NAV".

    One row per portfolio per day, and ``portfolio_id IS NULL`` for the consolidated series, so
    §6.3's combined chart and a single portfolio's chart read the same table rather than two that
    can disagree.

    Stored rather than recomputed on read. §5.1 makes this series the source for the chart, per-day
    P&L, contribution and drawdown; recomputing a year of it per page load would be slow, and worse
    would silently rewrite history whenever an allocation changed. "Valued at close of {date}" is a
    claim about that day, and a claim needs a record.
    """

    __tablename__ = "portfolio_nav_daily"
    #: 0023 replaced the primary key with a unique index. A PK makes every one of its columns
    #: NOT NULL in Postgres, which silently made ``portfolio_id`` non-nullable — and a NULL
    #: ``portfolio_id`` *is* the consolidated series, the row criterion 1 is computed from. The
    #: table documented it as nullable in two places and the database disagreed; nobody noticed
    #: until the nightly job tried to write the row. ``NULLS NOT DISTINCT`` (PG 15+) gives the
    #: same uniqueness and still serves as an ``ON CONFLICT`` arbiter.
    __table_args__ = (Index("ix_portfolio_nav_daily_portfolio_date", "portfolio_id", "date"),)

    #: NULL = the consolidated series for this user. Nullable in the database *because* 0023
    #: removed the primary key; see the note above.
    portfolio_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("portfolio.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    date: Mapped[dt.date] = mapped_column(Date, nullable=False, primary_key=True)
    market_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cash: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    #: The day's net internal flow, which is what makes a time-weighted return computable from
    #: this series alone: TWR has to divide the day at each flow, and cannot if the flow is lost.
    net_flow: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    #: §4.3's freeze, made visible. A day whose value could not be trusted says so; omitting the
    #: row instead would leave a gap that a chart draws as zero.
    pending_reconciliation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class ReconciliationItem(Base):
    """Layer 3 — a question sync could not answer on its own (§4.3).

    "We detected a sell of 100 HDFC Bank — which portfolio?" Its existence is what stops a guess:
    while it is OPEN the affected holding's contribution to performance is frozen rather than
    attributed to whichever portfolio looked likely.

    A RESOLVED row must name ``resolved_portfolio_id``, enforced by a check constraint. Without
    that, "resolved" could mean "somebody clicked something" and a return series would move on a
    decision nobody recorded.
    """

    __tablename__ = "reconciliation_item"
    __table_args__ = (Index("ix_reconciliation_item_user_state", "user_id", "state"),)

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.id"), nullable=False
    )
    broker_account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("broker_account.id", ondelete="CASCADE"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, server_default="OPEN")
    #: The portfolio the UI pre-selects. A suggestion, never an attribution (§4.3).
    suggested_portfolio_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("portfolio.id", ondelete="SET NULL")
    )
    resolved_portfolio_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("portfolio.id", ondelete="SET NULL")
    )
    detected_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
