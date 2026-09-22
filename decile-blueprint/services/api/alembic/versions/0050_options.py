"""The options run's schema — ``docs/options/03-data-model.md``, module OP2.

Seventeen tables and one enum, one migration, for the reason ``0028_swing``, ``0037_vbt`` and
``0041_twt`` gave: a plan with no legs or a session with no journal is not a half-working book, it
is a schema no code can use. Nothing here alters an existing table.

* **Market data, shared** (no ``user_id``): ``op_contract`` (the NFO master, never deleted),
  ``op_expiry`` (the calendar, rebuilt nightly), ``op_index_minute`` and ``op_chain_snapshot``
  (partitioned by month on ``ts``; partitions Sep 2026 → Dec 2027 created here, later months by
  ``baskfy_worker.options.partitions`` before the collector writes them — DECISIONS-OP OP2.5).
* **Everything else carries ``user_id``**, non-null, cascading from ``app_user`` (P4.1).
* ``op_sleeve`` is a Postgres enum (``O1M``, ``O1W``, ``O2``, ``O3A``, ``O3B``) as ``03`` names it.

It seeds nothing. Event days and the config rows for the sole user are written by
``python -m baskfy_worker.options_cli seed`` (a migration keyed on ``BASKFY_SOLE_USER_ID`` would
embed an environment variable into schema history). ``sleeve_capital_inr`` defaults to **0** and
no agent ever sets it (PACK.6).

The downgrade drops every table, every partition (with its parent) and the enum — tested by
``packages/core/tests/test_options_schema.py`` against the test database.

Revision ID: 0050_options
Revises: 0049_broker_trade
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0050_options"
down_revision: str | None = "0049_broker_trade"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Reverse dependency order, for :func:`downgrade`. Listed once so a table added to
#: :func:`upgrade` and forgotten here fails the round-trip test rather than leaking.
TABLES: tuple[str, ...] = (
    "op_backtest_run",
    "op_journal",
    "op_position",
    "op_fill",
    "op_order",
    "op_leg",
    "op_plan",
    "op_session",
    "op_config_audit",
    "op_sleeve_config",
    "op_book_config",
    "op_scan",
    "op_event_day",
    "op_chain_snapshot",
    "op_index_minute",
    "op_expiry",
    "op_contract",
)

SLEEVES: tuple[str, ...] = ("O1M", "O1W", "O2", "O3A", "O3B")
SLEEVE_GROUPS: tuple[str, ...] = ("O1M", "O1W", "O2", "O3")
SESSION_STATES: tuple[str, ...] = (
    "OBSERVING",
    "SKIPPED",
    "PLANNED",
    "LAPSED",
    "CONFIRMED",
    "OPEN",
    "CLOSED",
)

#: The first and last months partitioned here (``[start, end]``, inclusive).
FIRST_PARTITION: tuple[int, int] = (2026, 9)
LAST_PARTITION: tuple[int, int] = (2027, 12)

PRICE = sa.Numeric(precision=18, scale=2)
INR = sa.Numeric(precision=12, scale=2)
GREEK = sa.Numeric(precision=10, scale=6)

sleeve_enum = postgresql.ENUM(*SLEEVES, name="op_sleeve", create_type=False)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _user_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["user_id"], ["app_user.id"], name=op.f(f"fk_{table}_user_id_app_user"), ondelete="CASCADE"
    )


def _now() -> sa.TextClause:
    return sa.text("now()")


def _months() -> list[tuple[dt.date, dt.date]]:
    out: list[tuple[dt.date, dt.date]] = []
    year, month = FIRST_PARTITION
    while (year, month) <= LAST_PARTITION:
        start = dt.date(year, month, 1)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)  # noqa: PLR2004
        out.append((start, dt.date(year, month, 1)))
    return out


def partition_name(start: dt.date) -> str:
    return f"op_chain_snapshot_{start.year:04d}{start.month:02d}"


def upgrade() -> None:
    sleeve_enum.create(op.get_bind(), checkfirst=False)

    # --- 1. Market data, shared --------------------------------------------------------------
    op.create_table(
        "op_contract",
        sa.Column("instrument_token", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("tradingsymbol", sa.String(length=64), nullable=False),
        sa.Column("underlying", sa.String(length=16), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("strike", PRICE, nullable=False),
        sa.Column("option_type", sa.String(length=2), nullable=False),
        sa.Column("lot_size", sa.SmallInteger(), nullable=False),
        sa.Column("tick_size", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("first_seen", sa.Date(), nullable=False),
        sa.Column("last_seen", sa.Date(), nullable=False),
        sa.Column("expired", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            _in("option_type", ("CE", "PE")), name=op.f("ck_op_contract_option_type_known")
        ),
        sa.CheckConstraint("lot_size > 0", name=op.f("ck_op_contract_lot_size_positive")),
        sa.CheckConstraint("tick_size > 0", name=op.f("ck_op_contract_tick_size_positive")),
        sa.CheckConstraint("strike > 0", name=op.f("ck_op_contract_strike_positive")),
        sa.CheckConstraint("last_seen >= first_seen", name=op.f("ck_op_contract_seen_in_order")),
        sa.PrimaryKeyConstraint("instrument_token", name=op.f("pk_op_contract")),
    )
    op.create_index("ix_op_contract_underlying_expiry", "op_contract", ["underlying", "expiry"])

    op.create_table(
        "op_expiry",
        sa.Column("underlying", sa.String(length=16), nullable=False),
        sa.Column("expiry_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("lot_size", sa.SmallInteger(), nullable=False),
        sa.Column("first_seen", sa.Date(), nullable=False),
        sa.Column("seen_on", sa.Date(), nullable=False),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            _in("kind", ("WEEKLY", "MONTHLY")), name=op.f("ck_op_expiry_kind_known")
        ),
        sa.CheckConstraint("lot_size > 0", name=op.f("ck_op_expiry_lot_size_positive")),
        sa.CheckConstraint("seen_on >= first_seen", name=op.f("ck_op_expiry_seen_in_order")),
        sa.PrimaryKeyConstraint("underlying", "expiry_date", name=op.f("pk_op_expiry")),
    )

    op.create_table(
        "op_index_minute",
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", PRICE, nullable=False),
        sa.Column("high", PRICE, nullable=False),
        sa.Column("low", PRICE, nullable=False),
        sa.Column("close", PRICE, nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            _in("source", ("KITE_HIST", "TICKS")), name=op.f("ck_op_index_minute_source_known")
        ),
        sa.CheckConstraint("high >= low", name=op.f("ck_op_index_minute_high_not_below_low")),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_op_index_minute_instrument_id_instrument"),
        ),
        sa.PrimaryKeyConstraint("instrument_id", "ts", name=op.f("pk_op_index_minute")),
    )

    op.create_table(
        "op_chain_snapshot",
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("instrument_token", sa.BigInteger(), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("strike", PRICE, nullable=False),
        sa.Column("option_type", sa.String(length=2), nullable=False),
        sa.Column("spot", PRICE, nullable=True),
        sa.Column("bid", PRICE, nullable=True),
        sa.Column("ask", PRICE, nullable=True),
        sa.Column("last", PRICE, nullable=True),
        sa.Column("bid_qty", sa.Integer(), nullable=True),
        sa.Column("ask_qty", sa.Integer(), nullable=True),
        sa.Column("depth_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("oi", sa.BigInteger(), nullable=True),
        sa.Column("forward", PRICE, nullable=True),
        sa.Column("iv", GREEK, nullable=True),
        sa.Column("delta", GREEK, nullable=True),
        sa.Column("gamma", GREEK, nullable=True),
        sa.Column("theta", GREEK, nullable=True),
        sa.Column("vega", GREEK, nullable=True),
        sa.Column("greeks_model", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=8), server_default="QUOTE", nullable=False),
        sa.CheckConstraint(
            _in("option_type", ("CE", "PE")), name=op.f("ck_op_chain_snapshot_option_type_known")
        ),
        sa.CheckConstraint(
            _in("source", ("QUOTE", "VENDOR")), name=op.f("ck_op_chain_snapshot_source_known")
        ),
        sa.PrimaryKeyConstraint("ts", "instrument_token", name=op.f("pk_op_chain_snapshot")),
        postgresql_partition_by="RANGE (ts)",
    )
    op.create_index(
        "ix_op_chain_snapshot_ts_brin", "op_chain_snapshot", ["ts"], postgresql_using="brin"
    )
    op.create_index("ix_op_chain_snapshot_expiry_ts", "op_chain_snapshot", ["expiry", "ts"])
    for start, end in _months():
        # Bounds in IST so a month's partition holds that month's sessions exactly.
        op.execute(
            f"CREATE TABLE {partition_name(start)} PARTITION OF op_chain_snapshot "
            f"FOR VALUES FROM ('{start.isoformat()} 00:00:00+05:30') "
            f"TO ('{end.isoformat()} 00:00:00+05:30')"
        )

    # --- 2. User-scoped -------------------------------------------------------------------------
    op.create_table(
        "op_event_day",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=24), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(
            _in("reason", ("RBI_POLICY", "UNION_BUDGET", "ELECTION_RESULT", "MANUAL")),
            name=op.f("ck_op_event_day_reason_known"),
        ),
        sa.CheckConstraint(
            _in("source", ("SEED", "USER")), name=op.f("ck_op_event_day_source_known")
        ),
        _user_fk("op_event_day"),
        sa.PrimaryKeyConstraint("user_id", "date", name=op.f("pk_op_event_day")),
    )

    op.create_table(
        "op_scan",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("sleeve", sleeve_enum, nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column(
            "reasons", postgresql.ARRAY(sa.String(length=48)), server_default="{}", nullable=False
        ),
        sa.Column(
            "numbers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "candidates",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("as_of_minute", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stale", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        _user_fk("op_scan"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_op_scan")),
        sa.UniqueConstraint("user_id", "sleeve", "ts", name="uq_op_scan_user_sleeve_ts"),
    )
    op.create_index("ix_op_scan_user_date_sleeve", "op_scan", ["user_id", "trade_date", "sleeve"])

    op.create_table(
        "op_book_config",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("underlying", sa.String(length=16), server_default="NIFTY", nullable=False),
        sa.Column("account_inr", INR, server_default="0", nullable=False),
        sa.Column("margin_pool_inr", INR, server_default="0", nullable=False),
        sa.Column("daily_loss_limit_inr", INR, server_default="0", nullable=False),
        sa.Column("monthly_pause_inr", INR, server_default="0", nullable=False),
        sa.Column("paused_until", sa.Date(), nullable=True),
        sa.Column("paused_reason", sa.String(length=32), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        # 02 Track C §5: NIFTY only in v1. The settings API refuses anything else with a 422 first.
        sa.CheckConstraint(
            _in("underlying", ("NIFTY",)), name=op.f("ck_op_book_config_underlying_nifty_only")
        ),
        sa.CheckConstraint("account_inr >= 0", name=op.f("ck_op_book_config_account_non_negative")),
        sa.CheckConstraint(
            "margin_pool_inr >= 0", name=op.f("ck_op_book_config_margin_pool_non_negative")
        ),
        sa.CheckConstraint(
            "daily_loss_limit_inr >= 0", name=op.f("ck_op_book_config_daily_limit_non_negative")
        ),
        sa.CheckConstraint(
            "monthly_pause_inr >= 0", name=op.f("ck_op_book_config_monthly_pause_non_negative")
        ),
        _user_fk("op_book_config"),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_op_book_config")),
    )

    op.create_table(
        "op_sleeve_config",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("sleeve", sa.String(length=4), nullable=False),
        # ZERO IS THE POINT (PACK.6): paper plans one lot; live refuses NO_SLEEVE_CAPITAL.
        sa.Column("sleeve_capital_inr", INR, server_default="0", nullable=False),
        sa.Column(
            "risk_per_trade_pct",
            sa.Numeric(precision=5, scale=2),
            server_default="0.50",
            nullable=False,
        ),
        sa.Column("max_lots", sa.SmallInteger(), server_default="2", nullable=False),
        sa.Column("paper_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("hard_exit_time", sa.Time(), server_default="14:30", nullable=False),
        sa.Column("paused_until", sa.Date(), nullable=True),
        sa.Column("paused_reason", sa.String(length=32), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            _in("sleeve", SLEEVE_GROUPS), name=op.f("ck_op_sleeve_config_sleeve_group_known")
        ),
        sa.CheckConstraint(
            "sleeve_capital_inr >= 0", name=op.f("ck_op_sleeve_config_capital_non_negative")
        ),
        sa.CheckConstraint(
            "risk_per_trade_pct > 0", name=op.f("ck_op_sleeve_config_risk_pct_positive")
        ),
        sa.CheckConstraint("max_lots > 0", name=op.f("ck_op_sleeve_config_max_lots_positive")),
        _user_fk("op_sleeve_config"),
        sa.PrimaryKeyConstraint("user_id", "sleeve", name=op.f("pk_op_sleeve_config")),
    )

    op.create_table(
        "op_config_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("scope", sa.String(length=4), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("changed_by", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            _in("scope", ("BOOK", *SLEEVE_GROUPS)), name=op.f("ck_op_config_audit_scope_known")
        ),
        _user_fk("op_config_audit"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_op_config_audit")),
    )
    op.create_index("ix_op_config_audit_user_time", "op_config_audit", ["user_id", "changed_at"])

    op.create_table(
        "op_session",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("sleeve", sleeve_enum, nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("expiry_used", sa.Date(), nullable=True),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="OBSERVING", nullable=False),
        sa.Column(
            "numbers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("verdict", sa.String(length=16), nullable=True),
        sa.Column(
            "skip_reasons",
            postgresql.ARRAY(sa.String(length=48)),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("plan_id", sa.String(length=64), nullable=True),
        sa.Column("closed_reason", sa.String(length=32), nullable=True),
        sa.Column("pnl_inr", INR, nullable=True),
        sa.Column("pnl_r", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("slot_holder", sa.String(length=4), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(_in("mode", ("PAPER", "LIVE")), name=op.f("ck_op_session_mode_known")),
        sa.CheckConstraint(_in("state", SESSION_STATES), name=op.f("ck_op_session_state_known")),
        sa.CheckConstraint(
            "slot_holder IS NULL OR " + _in("slot_holder", SLEEVES),
            name=op.f("ck_op_session_slot_holder_known"),
        ),
        _user_fk("op_session"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_op_session")),
        sa.UniqueConstraint(
            "user_id", "sleeve", "trade_date", name="uq_op_session_user_sleeve_date"
        ),
    )

    op.create_table(
        "op_plan",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("sleeve", sleeve_enum, nullable=False),
        sa.Column("structure", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("sizing_mode", sa.String(length=16), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("credit_points", PRICE, nullable=True),
        sa.Column("debit_points", PRICE, nullable=True),
        sa.Column("width_points", PRICE, nullable=True),
        sa.Column("lots", sa.SmallInteger(), nullable=False),
        sa.Column("lot_size", sa.SmallInteger(), nullable=False),
        sa.Column("risk_per_lot_inr", INR, nullable=True),
        sa.Column("risk_budget_inr", INR, nullable=True),
        sa.Column("max_loss_inr", INR, nullable=True),
        sa.Column("profit_target_inr", INR, nullable=True),
        sa.Column("stop_inr", INR, nullable=True),
        sa.Column("expected_cost_inr", INR, nullable=True),
        sa.Column("cost_share", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("margin_required_inr", INR, nullable=True),
        sa.Column("status", sa.String(length=12), server_default="ISSUED", nullable=False),
        sa.Column("rejected_code", sa.String(length=48), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            _in("structure", ("IRON_CONDOR", "LONG_OPTION", "DEBIT_SPREAD")),
            name=op.f("ck_op_plan_structure_known"),
        ),
        sa.CheckConstraint(_in("kind", ("ENTRY", "EXIT")), name=op.f("ck_op_plan_kind_known")),
        sa.CheckConstraint(
            _in("status", ("ISSUED", "CONFIRMED", "LAPSED", "REJECTED")),
            name=op.f("ck_op_plan_status_known"),
        ),
        sa.CheckConstraint(
            _in("sizing_mode", ("BUDGET", "PAPER_ONE_LOT")),
            name=op.f("ck_op_plan_sizing_mode_known"),
        ),
        sa.CheckConstraint("expires_at > issued_at", name=op.f("ck_op_plan_expires_after_issue")),
        sa.CheckConstraint("lots > 0", name=op.f("ck_op_plan_lots_positive")),
        sa.CheckConstraint("lot_size > 0", name=op.f("ck_op_plan_lot_size_positive")),
        _user_fk("op_plan"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["op_session.id"],
            name=op.f("fk_op_plan_session_id_op_session"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_op_plan")),
        sa.UniqueConstraint("plan_id", name=op.f("uq_op_plan_plan_id")),
    )

    op.create_table(
        "op_leg",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.SmallInteger(), nullable=False),
        sa.Column("role", sa.String(length=12), nullable=False),
        sa.Column("tradingsymbol", sa.String(length=64), nullable=False),
        sa.Column("instrument_token", sa.BigInteger(), nullable=False),
        sa.Column("strike", PRICE, nullable=False),
        sa.Column("option_type", sa.String(length=2), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("limit_price", PRICE, nullable=True),
        sa.Column("iv_at_plan", GREEK, nullable=True),
        sa.Column("delta_at_plan", GREEK, nullable=True),
        sa.Column("bid", PRICE, nullable=True),
        sa.Column("ask", PRICE, nullable=True),
        sa.Column("depth_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=12), server_default="PENDING", nullable=False),
        sa.Column("filled_qty", sa.Integer(), server_default="0", nullable=False),
        sa.Column("avg_price", PRICE, nullable=True),
        sa.Column("simulated", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.CheckConstraint(
            _in("role", ("LONG_CALL", "LONG_PUT", "SHORT_CALL", "SHORT_PUT")),
            name=op.f("ck_op_leg_role_known"),
        ),
        sa.CheckConstraint(_in("side", ("BUY", "SELL")), name=op.f("ck_op_leg_side_known")),
        sa.CheckConstraint(
            _in("option_type", ("CE", "PE")), name=op.f("ck_op_leg_option_type_known")
        ),
        sa.CheckConstraint(
            _in("status", ("PENDING", "SENT", "FILLED", "PARTIAL", "CANCELLED", "REJECTED")),
            name=op.f("ck_op_leg_status_known"),
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_op_leg_quantity_positive")),
        sa.CheckConstraint(
            "filled_qty >= 0 AND filled_qty <= quantity", name=op.f("ck_op_leg_filled_within_qty")
        ),
        sa.CheckConstraint("seq > 0", name=op.f("ck_op_leg_seq_positive")),
        _user_fk("op_leg"),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["op_plan.id"], name=op.f("fk_op_leg_plan_id_op_plan"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["instrument_token"],
            ["op_contract.instrument_token"],
            name=op.f("fk_op_leg_instrument_token_op_contract"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_op_leg")),
        sa.UniqueConstraint("plan_id", "seq", name="uq_op_leg_plan_seq"),
    )

    op.create_table(
        "op_order",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("leg_id", sa.BigInteger(), nullable=False),
        sa.Column("client_id", sa.String(length=96), nullable=False),
        sa.Column("product", sa.String(length=8), server_default="MIS", nullable=False),
        sa.Column("exchange", sa.String(length=8), server_default="NFO", nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("limit_price", PRICE, nullable=True),
        sa.Column("broker_order_id", sa.String(length=64), nullable=True),
        sa.Column("gateway_status", sa.String(length=24), nullable=True),
        sa.Column("gateway_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("simulated", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        # 02 Track C §1: MIS on NFO, nothing else. The gateway refuses first; this says it again.
        sa.CheckConstraint(_in("product", ("MIS",)), name=op.f("ck_op_order_product_mis_only")),
        sa.CheckConstraint(_in("exchange", ("NFO",)), name=op.f("ck_op_order_exchange_nfo_only")),
        sa.CheckConstraint(_in("side", ("BUY", "SELL")), name=op.f("ck_op_order_side_known")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_op_order_quantity_positive")),
        _user_fk("op_order"),
        sa.ForeignKeyConstraint(
            ["leg_id"], ["op_leg.id"], name=op.f("fk_op_order_leg_id_op_leg"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_op_order")),
        sa.UniqueConstraint("client_id", name=op.f("uq_op_order_client_id")),
    )

    op.create_table(
        "op_fill",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("leg_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", PRICE, nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("sim_method", sa.String(length=16), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            _in("sim_method", ("DEPTH_LADDER", "LIVE")), name=op.f("ck_op_fill_sim_method_known")
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_op_fill_quantity_positive")),
        sa.CheckConstraint("price >= 0", name=op.f("ck_op_fill_price_non_negative")),
        _user_fk("op_fill"),
        sa.ForeignKeyConstraint(
            ["leg_id"], ["op_leg.id"], name=op.f("fk_op_fill_leg_id_op_leg"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["op_order.id"],
            name=op.f("fk_op_fill_order_id_op_order"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_op_fill")),
    )

    op.create_table(
        "op_position",
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("leg_ids", postgresql.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("entry_points", PRICE, nullable=False),
        sa.Column("entry_inr", INR, nullable=False),
        sa.Column("lots", sa.SmallInteger(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hard_exit_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("peak_value", PRICE, nullable=True),
        sa.Column("last_mark_points", PRICE, nullable=True),
        sa.Column("last_mark_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_plan_id", sa.String(length=64), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.CheckConstraint("lots > 0", name=op.f("ck_op_position_lots_positive")),
        _user_fk("op_position"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["op_session.id"],
            name=op.f("fk_op_position_session_id_op_session"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_op_position")),
    )

    op.create_table(
        "op_journal",
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("sleeve", sleeve_enum, nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("expiry_used", sa.Date(), nullable=False),
        sa.Column("structure", sa.String(length=16), nullable=False),
        sa.Column("entry_inr", INR, nullable=False),
        sa.Column("exit_inr", INR, nullable=False),
        sa.Column("gross_pnl_inr", INR, nullable=False),
        sa.Column("costs_inr", INR, nullable=False),
        sa.Column("net_pnl_inr", INR, nullable=False),
        sa.Column("risk_budget_inr", INR, nullable=False),
        sa.Column("r_multiple", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("closed_reason", sa.String(length=32), nullable=False),
        sa.Column("minutes_held", sa.Integer(), nullable=False),
        sa.Column("mae_r", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("mfe_r", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("sizing_mode", sa.String(length=16), nullable=False),
        sa.Column("half_size", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            _in("structure", ("IRON_CONDOR", "LONG_OPTION", "DEBIT_SPREAD")),
            name=op.f("ck_op_journal_structure_known"),
        ),
        sa.CheckConstraint(
            _in("sizing_mode", ("BUDGET", "PAPER_ONE_LOT")),
            name=op.f("ck_op_journal_sizing_mode_known"),
        ),
        sa.CheckConstraint("minutes_held >= 0", name=op.f("ck_op_journal_minutes_non_negative")),
        _user_fk("op_journal"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["op_session.id"],
            name=op.f("fk_op_journal_session_id_op_session"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_op_journal")),
    )

    op.create_table(
        "op_backtest_run",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("sleeve", sleeve_enum, nullable=False),
        sa.Column("tier", sa.SmallInteger(), nullable=False),
        sa.Column("params_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("date_from", sa.Date(), nullable=False),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("sessions", sa.Integer(), nullable=False),
        sa.Column("signals", sa.Integer(), nullable=False),
        sa.Column("traded", sa.Integer(), nullable=False),
        sa.Column(
            "skipped_by_reason_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("win_rate", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("expectancy_r", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("net_pnl_inr", INR, nullable=True),
        sa.Column("max_drawdown_r", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("caveats", sa.Text(), nullable=False),
        sa.Column("ran_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("git_sha", sa.String(length=40), nullable=True),
        sa.CheckConstraint("tier IN (1, 2, 3)", name=op.f("ck_op_backtest_run_tier_known")),
        sa.CheckConstraint("date_to >= date_from", name=op.f("ck_op_backtest_run_dates_in_order")),
        sa.CheckConstraint(
            "sessions >= 0 AND signals >= 0 AND traded >= 0",
            name=op.f("ck_op_backtest_run_counts_non_negative"),
        ),
        _user_fk("op_backtest_run"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_op_backtest_run")),
    )


def downgrade() -> None:
    for table in TABLES:
        # CASCADE only matters for op_chain_snapshot, whose partitions go with their parent.
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    sleeve_enum.drop(op.get_bind(), checkfirst=False)
