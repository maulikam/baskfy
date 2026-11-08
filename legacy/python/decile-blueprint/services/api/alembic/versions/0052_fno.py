"""The FO run's schema — ``docs/fno/03-data-model.md``, module FO2.

Fifteen tables and one enum, one migration, for the reason ``0050_options`` gave: a plan with no
legs or a position with no marks is not a half-working book, it is a schema no code can use.

* **Market data, shared** (no ``user_id``): ``fo_contract_daily`` (the F&O bhavcopy, partitioned
  by month on ``trade_date``; partitions Jan 2022 → Dec 2027 created here, later months by
  ``baskfy_worker.fno.partitions`` before the nightly ingest writes them), ``fo_underlying_daily``
  (derived from it, plus the ban list), ``fo_ingest_day`` (one row per session: INGESTED,
  PENDING or MISSING — what the status page reads, ``04`` §4) and ``fo_spread_sample`` (FO3).
* **Everything else carries ``user_id``**, non-null, cascading from ``app_user`` (P4.1).
* ``fo_sleeve`` is a Postgres enum (``F1N``, ``F1B``, ``F2``) as ``01`` §4 names it. Config is per
  sleeve *group* (``F1``, ``F2``): F1's capital is one number for both underlyings (M.1).

It also widens ``op_contract.lot_size`` and ``op_expiry.lot_size`` from ``smallint`` to
``integer``: FO2 widens the nightly NFO master from NIFTY to every F&O underlying, and IDEA's lot
(71,475 on 22 Sep 2026) does not fit in a smallint. Nothing else in the ``op_`` schema changes.

It seeds nothing. The config rows for the sole user are written by
``python -m baskfy_worker.fno_cli seed``.

The downgrade drops every table, every partition (with its parent) and the enum, and narrows the
two lot-size columns back (it fails loudly if a lot above 32,767 is stored, rather than truncating
one) — tested by ``packages/core/tests/test_fno_schema.py`` against the test database.

Revision ID: 0052_fno
Revises: 0051_op_position_extremes
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0052_fno"
down_revision: str | None = "0051_op_position_extremes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Reverse dependency order, for :func:`downgrade`. Listed once so a table added to
#: :func:`upgrade` and forgotten here fails the round-trip test rather than leaking.
TABLES: tuple[str, ...] = (
    "fo_backtest_run",
    "fo_journal",
    "fo_mark",
    "fo_fill",
    "fo_position",
    "fo_leg",
    "fo_plan",
    "fo_scan",
    "fo_config_audit",
    "fo_sleeve_config",
    "fo_book_config",
    "fo_spread_sample",
    "fo_ingest_day",
    "fo_underlying_daily",
    "fo_contract_daily",
)

SLEEVES: tuple[str, ...] = ("F1N", "F1B", "F2")
SLEEVE_GROUPS: tuple[str, ...] = ("F1", "F2")
INSTRUMENTS: tuple[str, ...] = ("FUTSTK", "FUTIDX", "OPTSTK", "OPTIDX")
CONTRACT_TYPES: tuple[str, ...] = ("CE", "PE", "XX")

#: The first and last months partitioned here (``[start, end]``, inclusive).
FIRST_PARTITION: tuple[int, int] = (2022, 1)
LAST_PARTITION: tuple[int, int] = (2027, 12)

PRICE = sa.Numeric(precision=18, scale=2)
INR = sa.Numeric(precision=12, scale=2)
MONEY = sa.Numeric(precision=20, scale=2)
VOL = sa.Numeric(precision=10, scale=6)
R = sa.Numeric(precision=8, scale=2)

sleeve_enum = postgresql.ENUM(*SLEEVES, name="fo_sleeve", create_type=False)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _user_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["user_id"], ["app_user.id"], name=op.f(f"fk_{table}_user_id_app_user"), ondelete="CASCADE"
    )


def _now() -> sa.TextClause:
    return sa.text("now()")


def _jsonb(name: str, default: str = "{}") -> sa.Column[object]:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text(f"'{default}'::jsonb"),
        nullable=False,
    )


def _months() -> list[tuple[dt.date, dt.date]]:
    out: list[tuple[dt.date, dt.date]] = []
    year, month = FIRST_PARTITION
    while (year, month) <= LAST_PARTITION:
        start = dt.date(year, month, 1)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)  # noqa: PLR2004
        out.append((start, dt.date(year, month, 1)))
    return out


def partition_name(start: dt.date) -> str:
    return f"fo_contract_daily_{start.year:04d}{start.month:02d}"


def upgrade() -> None:
    sleeve_enum.create(op.get_bind(), checkfirst=False)

    # FO2 widens the NFO master to every underlying; a stock's lot can exceed 32,767 (IDEA).
    op.alter_column("op_contract", "lot_size", type_=sa.Integer(), existing_nullable=False)
    op.alter_column("op_expiry", "lot_size", type_=sa.Integer(), existing_nullable=False)

    # --- 1. Market data, shared --------------------------------------------------------------
    op.create_table(
        "fo_contract_daily",
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("instrument", sa.String(length=6), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("strike", PRICE, nullable=False),
        sa.Column("option_type", sa.String(length=2), nullable=False),
        sa.Column("open", PRICE, nullable=True),
        sa.Column("high", PRICE, nullable=True),
        sa.Column("low", PRICE, nullable=True),
        sa.Column("close", PRICE, nullable=False),
        sa.Column("settle", PRICE, nullable=False),
        sa.Column("underlying", PRICE, nullable=True),
        sa.Column("open_interest", sa.BigInteger(), nullable=True),
        sa.Column("oi_change", sa.BigInteger(), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("turnover", MONEY, nullable=True),
        sa.Column("lot_size", sa.Integer(), nullable=True),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.CheckConstraint(
            _in("instrument", INSTRUMENTS), name=op.f("ck_fo_contract_daily_instrument_known")
        ),
        sa.CheckConstraint(
            _in("option_type", CONTRACT_TYPES),
            name=op.f("ck_fo_contract_daily_option_type_known"),
        ),
        sa.CheckConstraint(
            "(instrument IN ('FUTSTK', 'FUTIDX')) = (option_type = 'XX')",
            name=op.f("ck_fo_contract_daily_future_iff_xx"),
        ),
        sa.CheckConstraint("strike >= 0", name=op.f("ck_fo_contract_daily_strike_non_negative")),
        sa.PrimaryKeyConstraint(
            "trade_date",
            "symbol",
            "expiry",
            "strike",
            "option_type",
            name=op.f("pk_fo_contract_daily"),
        ),
        postgresql_partition_by="RANGE (trade_date)",
    )
    op.create_index(
        "ix_fo_contract_daily_symbol_date", "fo_contract_daily", ["symbol", "trade_date"]
    )
    for start, end in _months():
        op.execute(
            f"CREATE TABLE {partition_name(start)} PARTITION OF fo_contract_daily "
            f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
        )

    op.create_table(
        "fo_underlying_daily",
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("held_expiry", sa.Date(), nullable=True),
        sa.Column("level_o", PRICE, nullable=True),
        sa.Column("level_h", PRICE, nullable=True),
        sa.Column("level_l", PRICE, nullable=True),
        sa.Column("level_c", PRICE, nullable=True),
        sa.Column("ret", sa.Numeric(precision=14, scale=10), nullable=True),
        sa.Column("atr14", PRICE, nullable=True),
        sa.Column("oi_total", sa.BigInteger(), nullable=True),
        sa.Column("fut_turnover_20d", MONEY, nullable=True),
        sa.Column("iv_atm", VOL, nullable=True),
        sa.Column("rv20", VOL, nullable=True),
        sa.Column("basis_ann", VOL, nullable=True),
        sa.Column("in_ban", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("ca_flag", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("trade_date", "symbol", name=op.f("pk_fo_underlying_daily")),
    )
    op.create_index(
        "ix_fo_underlying_daily_symbol_date", "fo_underlying_daily", ["symbol", "trade_date"]
    )

    op.create_table(
        "fo_ingest_day",
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("rows_in_file", sa.Integer(), nullable=True),
        sa.Column("rows_kept", sa.Integer(), nullable=True),
        sa.Column("source_key", sa.Text(), nullable=True),
        sa.Column("attempts", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("ban_for_session", sa.Date(), nullable=True),
        sa.Column("ban_symbols", postgresql.ARRAY(sa.String(length=32)), nullable=True),
        sa.Column("ban_source_key", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(
            _in("status", ("PENDING", "INGESTED", "MISSING")),
            name=op.f("ck_fo_ingest_day_status_known"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_fo_ingest_day_attempts_non_negative")),
        sa.PrimaryKeyConstraint("trade_date", name=op.f("pk_fo_ingest_day")),
    )

    op.create_table(
        "fo_spread_sample",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("strike", PRICE, nullable=False),
        sa.Column("option_type", sa.String(length=2), nullable=False),
        sa.Column("bid", PRICE, nullable=True),
        sa.Column("ask", PRICE, nullable=True),
        sa.Column("mid", PRICE, nullable=True),
        sa.Column("oi", sa.BigInteger(), nullable=True),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            _in("option_type", ("CE", "PE")), name=op.f("ck_fo_spread_sample_option_type_known")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fo_spread_sample")),
        sa.UniqueConstraint(
            "taken_at",
            "symbol",
            "expiry",
            "strike",
            "option_type",
            name="uq_fo_spread_sample_contract_moment",
        ),
    )
    op.create_index("ix_fo_spread_sample_symbol_date", "fo_spread_sample", ["symbol", "trade_date"])

    # --- 2. User-scoped -------------------------------------------------------------------------
    op.create_table(
        "fo_book_config",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("monthly_pause_inr", INR, server_default="0", nullable=False),
        sa.Column("paused_until", sa.Date(), nullable=True),
        sa.Column("paused_reason", sa.String(length=32), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "monthly_pause_inr >= 0", name=op.f("ck_fo_book_config_monthly_pause_non_negative")
        ),
        _user_fk("fo_book_config"),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_fo_book_config")),
    )

    op.create_table(
        "fo_sleeve_config",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("sleeve", sa.String(length=4), nullable=False),
        sa.Column("capital_inr", INR, server_default="0", nullable=False),
        sa.Column(
            "risk_per_trade_pct",
            sa.Numeric(precision=5, scale=2),
            server_default="1.00",
            nullable=False,
        ),
        sa.Column("max_lots", sa.SmallInteger(), server_default="2", nullable=False),
        sa.Column("max_open_positions", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("paper_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("paused_until", sa.Date(), nullable=True),
        sa.Column("paused_reason", sa.String(length=32), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            _in("sleeve", SLEEVE_GROUPS), name=op.f("ck_fo_sleeve_config_sleeve_group_known")
        ),
        sa.CheckConstraint(
            "capital_inr >= 0", name=op.f("ck_fo_sleeve_config_capital_non_negative")
        ),
        sa.CheckConstraint(
            "risk_per_trade_pct > 0", name=op.f("ck_fo_sleeve_config_risk_pct_positive")
        ),
        sa.CheckConstraint("max_lots > 0", name=op.f("ck_fo_sleeve_config_max_lots_positive")),
        sa.CheckConstraint(
            "max_open_positions > 0", name=op.f("ck_fo_sleeve_config_max_open_positive")
        ),
        _user_fk("fo_sleeve_config"),
        sa.PrimaryKeyConstraint("user_id", "sleeve", name=op.f("pk_fo_sleeve_config")),
    )

    op.create_table(
        "fo_config_audit",
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
            _in("scope", ("BOOK", *SLEEVE_GROUPS)), name=op.f("ck_fo_config_audit_scope_known")
        ),
        _user_fk("fo_config_audit"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fo_config_audit")),
    )
    op.create_index("ix_fo_config_audit_user_time", "fo_config_audit", ["user_id", "changed_at"])

    op.create_table(
        "fo_scan",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("sleeve", sleeve_enum, nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("reasons", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        _jsonb("detail"),
        sa.Column("credit", PRICE, nullable=True),
        sa.Column("max_loss_per_lot", INR, nullable=True),
        sa.Column("cost_share", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("iv", VOL, nullable=True),
        sa.Column("rv20", VOL, nullable=True),
        sa.Column("iv_rv", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        _user_fk("fo_scan"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fo_scan")),
        sa.UniqueConstraint(
            "user_id", "sleeve", "trade_date", "symbol", name="uq_fo_scan_user_sleeve_date_symbol"
        ),
    )

    op.create_table(
        "fo_plan",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("sleeve", sleeve_enum, nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("structure", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("sizing_mode", sa.String(length=16), nullable=False),
        sa.Column("parent_plan_id", sa.String(length=64), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hard_exit_date", sa.Date(), nullable=True),
        sa.Column("credit_points", PRICE, nullable=True),
        sa.Column("debit_points", PRICE, nullable=True),
        sa.Column("width_points", PRICE, nullable=True),
        sa.Column("lots", sa.SmallInteger(), nullable=False),
        sa.Column("lot_size", sa.Integer(), nullable=False),
        sa.Column("risk_per_lot_inr", INR, nullable=True),
        sa.Column("risk_budget_inr", INR, nullable=True),
        sa.Column("max_loss_inr", INR, nullable=True),
        sa.Column("expected_cost_inr", INR, nullable=True),
        sa.Column("cost_share", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("margin_required_inr", INR, nullable=True),
        sa.Column("iv_rv", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="ISSUED", nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            _in("structure", ("IRON_CONDOR", "FUTURE")), name=op.f("ck_fo_plan_structure_known")
        ),
        sa.CheckConstraint(
            _in("kind", ("ENTRY", "EXIT", "ROLL")), name=op.f("ck_fo_plan_kind_known")
        ),
        sa.CheckConstraint(
            _in("sizing_mode", ("BUDGET", "PAPER_ONE_LOT")),
            name=op.f("ck_fo_plan_sizing_mode_known"),
        ),
        sa.CheckConstraint("expires_at > issued_at", name=op.f("ck_fo_plan_expires_after_issue")),
        sa.CheckConstraint("lots > 0", name=op.f("ck_fo_plan_lots_positive")),
        sa.CheckConstraint("lot_size > 0", name=op.f("ck_fo_plan_lot_size_positive")),
        _user_fk("fo_plan"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fo_plan")),
        sa.UniqueConstraint("plan_id", name=op.f("uq_fo_plan_plan_id")),
    )
    op.create_index("ix_fo_plan_user_date_sleeve", "fo_plan", ["user_id", "trade_date", "sleeve"])

    op.create_table(
        "fo_leg",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("entry_seq", sa.SmallInteger(), nullable=False),
        sa.Column("role", sa.String(12), nullable=False),
        sa.Column("tradingsymbol", sa.String(length=64), nullable=False),
        sa.Column("instrument_token", sa.BigInteger(), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("strike", PRICE, nullable=True),
        sa.Column("option_type", sa.String(length=2), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("limit_price", PRICE, nullable=True),
        sa.Column("reference_price", PRICE, nullable=True),
        sa.Column("bid", PRICE, nullable=True),
        sa.Column("ask", PRICE, nullable=True),
        sa.Column("status", sa.String(length=12), server_default="PENDING", nullable=False),
        sa.Column("filled_qty", sa.Integer(), server_default="0", nullable=False),
        sa.Column("avg_price", PRICE, nullable=True),
        sa.Column("simulated", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.CheckConstraint(
            _in("role", ("LONG_CALL", "LONG_PUT", "SHORT_CALL", "SHORT_PUT", "LONG_FUTURE")),
            name=op.f("ck_fo_leg_role_known"),
        ),
        sa.CheckConstraint(_in("side", ("BUY", "SELL")), name=op.f("ck_fo_leg_side_known")),
        sa.CheckConstraint(
            _in("option_type", CONTRACT_TYPES), name=op.f("ck_fo_leg_option_type_known")
        ),
        sa.CheckConstraint(
            _in("status", ("PENDING", "SENT", "FILLED", "PARTIAL", "CANCELLED", "REJECTED")),
            name=op.f("ck_fo_leg_status_known"),
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_fo_leg_quantity_positive")),
        sa.CheckConstraint(
            "filled_qty >= 0 AND filled_qty <= quantity", name=op.f("ck_fo_leg_filled_within_qty")
        ),
        sa.CheckConstraint("entry_seq > 0", name=op.f("ck_fo_leg_entry_seq_positive")),
        _user_fk("fo_leg"),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["fo_plan.id"], name=op.f("fk_fo_leg_plan_id_fo_plan"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fo_leg")),
        sa.UniqueConstraint("plan_id", "entry_seq", name="uq_fo_leg_plan_seq"),
    )

    op.create_table(
        "fo_position",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("sleeve", sleeve_enum, nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("structure", sa.String(length=16), nullable=False),
        sa.Column("entry_plan_id", sa.String(length=64), nullable=False),
        _jsonb("legs", "[]"),
        sa.Column("lots", sa.SmallInteger(), nullable=False),
        sa.Column("lot_size", sa.Integer(), nullable=False),
        sa.Column("entry_price", PRICE, nullable=True),
        sa.Column("entry_credit", PRICE, nullable=True),
        sa.Column("max_loss_inr", INR, nullable=True),
        sa.Column("profit_take_points", PRICE, nullable=True),
        sa.Column("loss_close_points", PRICE, nullable=True),
        sa.Column("stop_price", PRICE, nullable=True),
        sa.Column("gtt_id", sa.String(length=32), nullable=True),
        sa.Column("hard_exit_date", sa.Date(), nullable=True),
        sa.Column("next_roll_date", sa.Date(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_reason", sa.String(length=32), nullable=True),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            _in("structure", ("IRON_CONDOR", "FUTURE")), name=op.f("ck_fo_position_structure_known")
        ),
        sa.CheckConstraint("lots > 0", name=op.f("ck_fo_position_lots_positive")),
        sa.CheckConstraint("lot_size > 0", name=op.f("ck_fo_position_lot_size_positive")),
        _user_fk("fo_position"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fo_position")),
    )
    op.create_index("ix_fo_position_user_open", "fo_position", ["user_id", "closed_at"])

    op.create_table(
        "fo_fill",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("leg_id", sa.BigInteger(), nullable=False),
        sa.Column("position_id", sa.BigInteger(), nullable=True),
        sa.Column("client_id", sa.String(length=96), nullable=True),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", PRICE, nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("sim_method", sa.String(length=16), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(_in("side", ("BUY", "SELL")), name=op.f("ck_fo_fill_side_known")),
        sa.CheckConstraint(
            _in("sim_method", ("DEPTH_LADDER", "LIVE")), name=op.f("ck_fo_fill_sim_method_known")
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_fo_fill_quantity_positive")),
        sa.CheckConstraint("price >= 0", name=op.f("ck_fo_fill_price_non_negative")),
        _user_fk("fo_fill"),
        sa.ForeignKeyConstraint(
            ["leg_id"], ["fo_leg.id"], name=op.f("fk_fo_fill_leg_id_fo_leg"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["fo_position.id"],
            name=op.f("fk_fo_fill_position_id_fo_position"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fo_fill")),
    )

    op.create_table(
        "fo_mark",
        sa.Column("position_id", sa.BigInteger(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("mark_points", PRICE, nullable=False),
        sa.Column("pnl_inr", INR, nullable=False),
        sa.Column("stop_price", PRICE, nullable=True),
        _jsonb("detail"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        _user_fk("fo_mark"),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["fo_position.id"],
            name=op.f("fk_fo_mark_position_id_fo_position"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("position_id", "trade_date", name=op.f("pk_fo_mark")),
    )

    op.create_table(
        "fo_journal",
        sa.Column("position_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("sleeve", sleeve_enum, nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("structure", sa.String(length=16), nullable=False),
        sa.Column("opened_on", sa.Date(), nullable=False),
        sa.Column("closed_on", sa.Date(), nullable=False),
        sa.Column("entry_inr", INR, nullable=False),
        sa.Column("exit_inr", INR, nullable=False),
        sa.Column("gross_pnl_inr", INR, nullable=False),
        sa.Column("costs_inr", INR, nullable=False),
        sa.Column("net_pnl_inr", INR, nullable=False),
        sa.Column("risk_budget_inr", INR, nullable=False),
        sa.Column("r_multiple", R, nullable=False),
        sa.Column("closed_reason", sa.String(length=32), nullable=False),
        sa.Column("sessions_held", sa.Integer(), nullable=False),
        sa.Column("rolls", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("mae_r", R, nullable=True),
        sa.Column("mfe_r", R, nullable=True),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("sizing_mode", sa.String(length=16), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            _in("structure", ("IRON_CONDOR", "FUTURE")), name=op.f("ck_fo_journal_structure_known")
        ),
        sa.CheckConstraint(
            _in("sizing_mode", ("BUDGET", "PAPER_ONE_LOT")),
            name=op.f("ck_fo_journal_sizing_mode_known"),
        ),
        sa.CheckConstraint("sessions_held >= 0", name=op.f("ck_fo_journal_sessions_non_negative")),
        sa.CheckConstraint("rolls >= 0", name=op.f("ck_fo_journal_rolls_non_negative")),
        sa.CheckConstraint("closed_on >= opened_on", name=op.f("ck_fo_journal_dates_in_order")),
        _user_fk("fo_journal"),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["fo_position.id"],
            name=op.f("fk_fo_journal_position_id_fo_position"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("position_id", name=op.f("pk_fo_journal")),
    )

    op.create_table(
        "fo_backtest_run",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("family", sa.String(length=32), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tier", sa.String(length=4), server_default="2E", nullable=False),
        sa.Column("caveat", sa.Text(), nullable=False),
        sa.Column("sample_from", sa.Date(), nullable=False),
        sa.Column("sample_to", sa.Date(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("net_r", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("gross_r", sa.Numeric(precision=10, scale=4), nullable=True),
        _jsonb("per_year"),
        sa.Column("slippage_source", sa.String(length=8), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("git_sha", sa.String(length=40), nullable=True),
        sa.CheckConstraint(
            _in("slippage_source", ("ASSUMED", "MEASURED")),
            name=op.f("ck_fo_backtest_run_slippage_source_known"),
        ),
        sa.CheckConstraint(
            "sample_to >= sample_from", name=op.f("ck_fo_backtest_run_dates_in_order")
        ),
        sa.CheckConstraint("n >= 0", name=op.f("ck_fo_backtest_run_n_non_negative")),
        _user_fk("fo_backtest_run"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fo_backtest_run")),
    )
    op.create_index(
        "ix_fo_backtest_run_user_family_time", "fo_backtest_run", ["user_id", "family", "run_at"]
    )


def downgrade() -> None:
    for table in TABLES:
        # CASCADE only matters for fo_contract_daily, whose partitions go with their parent.
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    sleeve_enum.drop(op.get_bind(), checkfirst=False)
    # Fails loudly (numeric out of range) if a lot above 32,767 is stored, rather than truncating.
    op.alter_column("op_expiry", "lot_size", type_=sa.SmallInteger(), existing_nullable=False)
    op.alter_column("op_contract", "lot_size", type_=sa.SmallInteger(), existing_nullable=False)
