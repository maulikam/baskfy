"""The three-weeks-tight sleeve's schema — ``docs/twt/03-data-model.md``, module TW3.

Thirteen tables, one migration. They are one migration rather than thirteen because they are one
*thing*: a state table with no plan, or a plan with no positions, is not a half-working sleeve —
it is a schema no code can use, and alembic history is linear, so splitting them would only
serialise the modules that build on them. ``0028_swing`` and ``0037_vbt`` made the same call for
the same reason.

Nothing here alters an existing table. This sleeve joins ``instrument``, ``ohlcv_daily``,
``trading_day``, ``pipeline_run`` and the desk's order journal, and owns none of them.

FIVE THINGS TO KNOW BEFORE READING THE DDL
------------------------------------------
**Every table carries ``user_id``.** ``docs/twt/02-scope-and-gating.md`` Track C §6, so that the
day multi-tenancy arrives nothing needs a migration. The three tables ``03`` sketches with a
market-wide key — ``tw_state_daily``, ``tw_signal_daily`` and ``tw_breadth_daily`` — therefore
have it in their primary key: all three are snapshots of what one user's system saw, and it is
that user's settings that decide what may be acted on.

**The state and the signal are two tables, not one.** TWT-1's scan is a *state* — "this name has
been tight for eleven sessions" — and its signal is the **first day** of one. Storing only the
signals would make "how long has this been tight" and "which names left the state today"
unanswerable, and both are on the page (``docs/twt/05`` §2). ``tw_signal_daily`` keeps the rows
the liquidity floor *rejected* too (``state = 'SCAN_ONLY'``), for the reason VBT-1 keeps its
rejects: a system that stores only what it accepted cannot show a person what it passed over.

**The stop can only ever rise.** ``ck_tw_position_stop_never_below_initial`` is the floor a bug
cannot get under. The rule is enforced where the update happens (``04`` §7.2's ratchet takes a
maximum; the desk refuses a raise at or below the resting trigger), and this constraint is the
database saying so a third time, because that rule's violation is **silent**: a stop that quietly
fell is a stop nobody notices until it does not fire. On a book whose only exit is that stop,
that is the difference between a 20 % loss and an open-ended one.

**There is no ``EMA_EXIT`` and no ``TIME_EXIT`` in ``ck_tw_position_close_reason_known``.**
``docs/twt/01`` §5 measured a 50-SMA exit and it is a *different strategy* with the same signal
(543 trades, 15.6 % CAGR, -38.3 % drawdown). A reason that exists is a reason somebody writes
code for, so the five that are here are the five the strategy can produce.

**``tw_position`` and ``tw_order`` reference each other**, so one of the two foreign keys is
added after both tables exist (``fk_tw_position_order_id_tw_order``, below) and dropped before
either is dropped. This is written out rather than left to ``use_alter=True`` inside
``op.create_table``, because SQLAlchemy's ``CREATE TABLE`` silently *omits* a constraint marked
``use_alter`` and Alembic does not emit the follow-up ``ALTER`` — the foreign key would simply
not exist, and nothing would say so.

WHAT THIS MIGRATION DELIBERATELY DOES NOT DO
--------------------------------------------
It seeds nothing. ``tw_config`` for the sole user is written by ``baskfy_api.seed``, because a
migration that inserted a row keyed on ``BASKFY_SOLE_USER_ID`` would embed an environment
variable into schema history. The seeded row has ``sleeve_capital_inr = 0``, which is the state
in which the sleeve plans nothing at all (``docs/twt/04`` §9.3 skips every signal
``NO_SLEEVE_CAPITAL``), and **no agent ever sets that number** — Maulik enters it himself on the
first live morning (``02`` §3).

It also writes **no threshold from ``docs/twt/04``** into a CHECK. ``week_range_pct <= 3.01``,
``month_low_ratio >= 1.3``, ``sessions_out_before >= 5`` and the plan's 30-minute TTL are all
``baskfy_core.twt.config`` fields; pinning one here would make a recalibration a migration. The
constraints assert the *shapes* (a range is non-negative, a stop is below its entry, an expiry
is after its build) and leave the values to the config.

Revision ID: 0041_twt
Revises: 0040_vbt_scan_run
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041_twt"
down_revision: str | None = "0040_vbt_scan_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Dropped in this order by :func:`downgrade` — reverse dependency order, so every table goes
#: after the tables that reference it. Listed once, so a table added to :func:`upgrade` and
#: forgotten here fails ``make downgrade`` rather than leaking into the next developer's
#: database. ``tw_order`` before ``tw_signal_daily`` because an order names the stored signal
#: that produced it; ``tw_position`` after ``tw_order`` and ``tw_fill`` for the same reason.
TABLES: tuple[str, ...] = (
    "tw_backtest_run",
    "tw_session",
    "tw_plan_skip",
    "tw_plan_line",
    "tw_plan",
    "tw_fill",
    "tw_order",
    "tw_position",
    "tw_breadth_daily",
    "tw_signal_daily",
    "tw_state_daily",
    "tw_config_audit",
    "tw_config",
)

#: The one foreign key that cannot be created with its table, because the table it points at
#: does not exist yet and points back. Named here so :func:`downgrade` drops exactly what
#: :func:`upgrade` added.
POSITION_ORDER_FK: str = "fk_tw_position_order_id_tw_order"


def upgrade() -> None:
    # --- 1. The settings, and the history of every change to them --------------------------
    op.create_table(
        "tw_config",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        # ZERO IS THE POINT, NOT A PLACEHOLDER. `04` §9.3: a sleeve at ₹0 plans nothing —
        # every signal is skipped NO_SLEEVE_CAPITAL. Maulik enters this number himself.
        sa.Column(
            "sleeve_capital_inr",
            sa.Numeric(precision=12, scale=2),
            server_default="0",
            nullable=False,
        ),
        sa.Column("max_open_positions", sa.SmallInteger(), server_default="10", nullable=False),
        sa.Column(
            "max_position_pct",
            sa.Numeric(precision=5, scale=2),
            server_default="12.50",
            nullable=False,
        ),
        sa.Column(
            "stop_pct", sa.Numeric(precision=5, scale=2), server_default="20.00", nullable=False
        ),
        # Bounded BELOW by BASKFY_TWT_TRAIL_PCT_MIN [18.00], not above: the trail is TWT-1's only
        # exit and the measured cliff is in the tightening direction (DECISIONS-TW TW0.5).
        sa.Column(
            "trail_pct", sa.Numeric(precision=5, scale=2), server_default="20.00", nullable=False
        ),
        sa.Column(
            "first_live_entries_left", sa.SmallInteger(), server_default="10", nullable=False
        ),
        sa.Column("dry_run_sessions", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "sleeve_capital_inr >= 0", name=op.f("ck_tw_config_capital_non_negative")
        ),
        sa.CheckConstraint(
            "dry_run_sessions >= 0", name=op.f("ck_tw_config_dry_run_sessions_non_negative")
        ),
        sa.CheckConstraint(
            "first_live_entries_left >= 0", name=op.f("ck_tw_config_first_live_non_negative")
        ),
        sa.CheckConstraint(
            "max_open_positions > 0", name=op.f("ck_tw_config_max_positions_positive")
        ),
        sa.CheckConstraint("max_position_pct > 0", name=op.f("ck_tw_config_position_pct_positive")),
        sa.CheckConstraint("stop_pct > 0", name=op.f("ck_tw_config_stop_pct_positive")),
        sa.CheckConstraint("trail_pct > 0", name=op.f("ck_tw_config_trail_pct_positive")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_tw_config_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_tw_config")),
    )

    op.create_table(
        "tw_config_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("changed_by", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_tw_config_audit_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tw_config_audit")),
    )
    op.create_index(
        "ix_tw_config_audit_user_time", "tw_config_audit", ["user_id", "changed_at"], unique=False
    )

    # --- 2. What the scan saw: the state, the entry events, and the gate --------------------
    op.create_table(
        "tw_state_daily",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("open", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("high", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("low", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("close", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("close_raw", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "adj_factor", sa.Numeric(precision=18, scale=10), server_default="1", nullable=False
        ),
        # `04` §3.2's three weekly closes, stored because the one rule anybody will dispute is
        # this one: w0 is TODAY's close read as the current week's partial candle.
        sa.Column("week_close_0", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("week_close_1", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("week_close_2", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("week_range_pct", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("month_low_3", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("month_low_ratio", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("vol_sma_50", sa.BigInteger(), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("turnover_inr", sa.BigInteger(), nullable=False),
        sa.Column("turnover_avg_20", sa.BigInteger(), nullable=True),
        sa.Column("sma_dma", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("sessions_in_state", sa.SmallInteger(), nullable=False),
        sa.Column("bars_in_window", sa.SmallInteger(), nullable=True),
        sa.Column("locked_upper_circuit", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("pipeline_run_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "bars_in_window IS NULL OR bars_in_window >= 0",
            name=op.f("ck_tw_state_daily_bars_non_negative"),
        ),
        sa.CheckConstraint(
            "close > 0 AND close_raw > 0", name=op.f("ck_tw_state_daily_close_positive")
        ),
        sa.CheckConstraint(
            "month_low_3 > 0 AND month_low_ratio > 0",
            name=op.f("ck_tw_state_daily_month_low_positive"),
        ),
        sa.CheckConstraint(
            "sessions_in_state > 0", name=op.f("ck_tw_state_daily_sessions_in_state_positive")
        ),
        sa.CheckConstraint(
            "volume >= 0 AND vol_sma_50 >= 0", name=op.f("ck_tw_state_daily_volume_non_negative")
        ),
        sa.CheckConstraint(
            "week_range_pct >= 0", name=op.f("ck_tw_state_daily_week_range_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_tw_state_daily_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"],
            ["pipeline_run.id"],
            name=op.f("fk_tw_state_daily_pipeline_run_id_pipeline_run"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_tw_state_daily_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "date", "instrument_id", name="pk_tw_state_daily"),
    )
    op.create_index(
        "ix_tw_state_daily_session", "tw_state_daily", ["date", "instrument_id"], unique=False
    )
    op.create_index(
        "ix_tw_state_daily_instrument", "tw_state_daily", ["instrument_id", "date"], unique=False
    )

    op.create_table(
        "tw_signal_daily",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column(
            "failed_filters",
            postgresql.ARRAY(sa.String(length=16)),
            server_default="{}",
            nullable=False,
        ),
        # NOT an entry level: TWT-1 buys the next open at market. This is the close that produced
        # the signal, so the morning's fill can be read against it (`03` §3).
        sa.Column("entry_reference_close", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("stop_preview", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("sessions_out_before", sa.SmallInteger(), nullable=False),
        # The signal session's OWN turnover, not the 20-day average the research note's prose
        # names: the code that produced every number in `01` §6 ranks this way (TW0.2).
        sa.Column("rank_key", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("turnover_avg_20", sa.BigInteger(), nullable=True),
        sa.Column("pipeline_run_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('SIGNAL', 'SCAN_ONLY')", name=op.f("ck_tw_signal_daily_state_known")
        ),
        sa.CheckConstraint(
            "entry_reference_close > 0", name=op.f("ck_tw_signal_daily_entry_reference_positive")
        ),
        sa.CheckConstraint(
            "sessions_out_before >= 0", name=op.f("ck_tw_signal_daily_sessions_out_non_negative")
        ),
        # `04` §7.1's STOP_NOT_BELOW_ENTRY, in the database. It cannot arise at a 20 % stop; the
        # check is here because a ceiling edit could make it arise.
        sa.CheckConstraint(
            "stop_preview > 0 AND stop_preview < entry_reference_close",
            name=op.f("ck_tw_signal_daily_stop_below_entry"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_tw_signal_daily_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"],
            ["pipeline_run.id"],
            name=op.f("fk_tw_signal_daily_pipeline_run_id_pipeline_run"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_tw_signal_daily_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "date", "instrument_id", name="pk_tw_signal_daily"),
    )
    # The plan's one query: today's signals, best turnover first (`03` §3). DESC in the index,
    # so the ordering the plan asks for is the ordering the index already holds.
    op.create_index(
        "ix_tw_signal_daily_rank",
        "tw_signal_daily",
        ["date", "state", sa.text("rank_key DESC")],
        unique=False,
    )
    op.create_index(
        "ix_tw_signal_daily_instrument", "tw_signal_daily", ["instrument_id", "date"], unique=False
    )

    op.create_table(
        "tw_breadth_daily",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("universe_count", sa.Integer(), nullable=False),
        sa.Column("measured_count", sa.Integer(), nullable=False),
        sa.Column("above_count", sa.Integer(), nullable=False),
        sa.Column("pct_above_dma", sa.Numeric(precision=7, scale=4), nullable=True),
        sa.Column("gate", sa.String(length=8), nullable=False),
        sa.Column("dma_bars", sa.SmallInteger(), server_default="200", nullable=False),
        sa.Column("thin_session", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("pipeline_run_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("gate IN ('OPEN', 'SHUT')", name=op.f("ck_tw_breadth_daily_gate_known")),
        sa.CheckConstraint(
            "above_count <= measured_count", name=op.f("ck_tw_breadth_daily_above_within_measured")
        ),
        sa.CheckConstraint(
            "measured_count <= universe_count",
            name=op.f("ck_tw_breadth_daily_measured_within_universe"),
        ),
        # A session dropped from the calendar (`04` §2.1) is not a session this book enters on.
        sa.CheckConstraint(
            "NOT thin_session OR gate = 'SHUT'",
            name=op.f("ck_tw_breadth_daily_thin_session_is_shut"),
        ),
        # And a session that was measured must carry its percentage: `03` §4 allows the null
        # only on the thin-session row written for the record.
        sa.CheckConstraint(
            "thin_session OR pct_above_dma IS NOT NULL",
            name=op.f("ck_tw_breadth_daily_measured_session_has_a_percentage"),
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"],
            ["pipeline_run.id"],
            name=op.f("fk_tw_breadth_daily_pipeline_run_id_pipeline_run"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_tw_breadth_daily_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "date", name="pk_tw_breadth_daily"),
    )

    # --- 3. The book, its orders and its fills ----------------------------------------------
    #
    # `tw_position` is created WITHOUT its `order_id` foreign key: `tw_order` does not exist
    # yet and points back at this table. The constraint is added below, once both exist.
    op.create_table(
        "tw_position",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("broker_account_id", sa.BigInteger(), nullable=True),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("signal_date", sa.Date(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("entry_avg", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("quantity_entered", sa.Integer(), nullable=False),
        # `04` §7.3 / TW0.7: the evening job compares this with the as-of row's factor to notice
        # a split under a hold. `03` §5's column table does not list it and §7.3 reads it by
        # name; a rule that needs a column the schema lacks needs a migration on the morning of
        # a split. DECISIONS-TW TW3.2.
        sa.Column(
            "entry_adj_factor",
            sa.Numeric(precision=18, scale=10),
            server_default="1",
            nullable=False,
        ),
        sa.Column("initial_stop", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("stop_price", sa.Numeric(precision=18, scale=2), nullable=False),
        # The highest high since entry, as an EXCHANGE print — the ratchet's only input besides
        # trail_pct. Stored rather than recomputed because the stop resting at the exchange was
        # derived from a specific number on a specific evening (`03` §5).
        sa.Column("high_since", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("high_since_date", sa.Date(), nullable=True),
        sa.Column("gtt_id", sa.String(length=64), nullable=True),
        sa.Column("gtt_trigger", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("gtt_armed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_trigger", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("next_trigger_for", sa.Date(), nullable=True),
        sa.Column("quantity_open", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="OPEN", nullable=False),
        sa.Column("closed_on", sa.Date(), nullable=True),
        sa.Column("exit_avg", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("close_reason", sa.String(length=24), nullable=True),
        sa.Column("pnl_inr", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("return_pct", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("r_multiple", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("hold_sessions", sa.SmallInteger(), nullable=True),
        sa.Column("simulated", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("half_size", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Five reasons. No EMA_EXIT, no TIME_EXIT — see the module docstring.
        sa.CheckConstraint(
            "close_reason IN ('STOP_HIT', 'STOP_GAP', 'STOP_DAY0', 'NO_BAR', 'MANUAL')",
            name=op.f("ck_tw_position_close_reason_known"),
        ),
        sa.CheckConstraint("state IN ('OPEN', 'CLOSED')", name=op.f("ck_tw_position_state_known")),
        sa.CheckConstraint(
            "entry_avg > 0 AND initial_stop > 0",
            name=op.f("ck_tw_position_entry_and_stop_positive"),
        ),
        sa.CheckConstraint("high_since > 0", name=op.f("ck_tw_position_high_since_positive")),
        sa.CheckConstraint(
            "initial_stop < entry_avg", name=op.f("ck_tw_position_initial_stop_below_entry")
        ),
        # `04` §11.3: a plan never reads a trigger computed for another session, and a trigger
        # that cannot say which session it is for cannot be checked against that rule.
        sa.CheckConstraint(
            "next_trigger IS NULL OR next_trigger_for IS NOT NULL",
            name=op.f("ck_tw_position_next_trigger_has_a_session"),
        ),
        sa.CheckConstraint("quantity_entered > 0", name=op.f("ck_tw_position_quantity_positive")),
        sa.CheckConstraint("quantity_open >= 0", name=op.f("ck_tw_position_open_non_negative")),
        sa.CheckConstraint(
            "quantity_open <= quantity_entered", name=op.f("ck_tw_position_open_within_entered")
        ),
        # THE ONE WHOSE VIOLATION IS SILENT.
        sa.CheckConstraint(
            "stop_price >= initial_stop", name=op.f("ck_tw_position_stop_never_below_initial")
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_tw_position_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_tw_position_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tw_position")),
    )
    op.create_index(
        "ix_tw_position_book", "tw_position", ["user_id", "state", "instrument_id"], unique=False
    )

    op.create_table(
        "tw_order",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("broker_account_id", sa.BigInteger(), nullable=True),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("signal_date", sa.Date(), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("stop_price", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="PROPOSED", nullable=False),
        sa.Column("broker_order_id", sa.String(length=64), nullable=True),
        sa.Column("client_id", sa.String(length=128), nullable=True),
        sa.Column("filled_quantity", sa.Integer(), server_default="0", nullable=False),
        sa.Column("avg_fill_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("position_id", sa.BigInteger(), nullable=True),
        sa.Column("simulated", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name=op.f("ck_tw_order_side_known")),
        # Seven states, and none of them is EXPIRED: TWT-1 buys at the next open at market, so
        # there is no working order and no expiry sweep (`03` §6).
        sa.CheckConstraint(
            "state IN ('PROPOSED', 'CONFIRMED', 'SENT', 'FILLED', 'PARTIAL', 'CANCELLED', "
            "'REJECTED')",
            name=op.f("ck_tw_order_state_known"),
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_tw_order_quantity_positive")),
        sa.CheckConstraint("stop_price > 0", name=op.f("ck_tw_order_stop_positive")),
        sa.CheckConstraint("filled_quantity >= 0", name=op.f("ck_tw_order_filled_non_negative")),
        sa.CheckConstraint(
            "filled_quantity <= quantity", name=op.f("ck_tw_order_filled_within_quantity")
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_tw_order_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["tw_position.id"],
            name=op.f("fk_tw_order_position_id_tw_position"),
            ondelete="SET NULL",
        ),
        # Every order names the stored signal that produced it (`03` §6). NO ACTION rather than
        # CASCADE: deleting a signal snapshot must not delete the order it produced, and the
        # detection is an upsert (house rule 7) so it never needs to. The DPDP erasure path is
        # unaffected — a NO ACTION check is made at the end of the statement, by which time the
        # cascade from `app_user` has removed both sides.
        sa.ForeignKeyConstraint(
            ["user_id", "signal_date", "instrument_id"],
            ["tw_signal_daily.user_id", "tw_signal_daily.date", "tw_signal_daily.instrument_id"],
            name="fk_tw_order_signal",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_tw_order_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tw_order")),
        # What makes the evening idempotent: a re-run cannot double-place.
        sa.UniqueConstraint(
            "user_id",
            "signal_date",
            "instrument_id",
            "side",
            name="uq_tw_order_one_per_signal",
        ),
    )
    op.create_index("ix_tw_order_state", "tw_order", ["user_id", "state"], unique=False)

    # The back half of the cycle, now that both tables exist.
    op.create_foreign_key(
        POSITION_ORDER_FK,
        "tw_position",
        "tw_order",
        ["order_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "tw_fill",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("position_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("journal_ref", sa.String(length=64), nullable=True),
        sa.Column("simulated", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name=op.f("ck_tw_fill_side_known")),
        sa.CheckConstraint("price > 0", name=op.f("ck_tw_fill_price_positive")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_tw_fill_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["tw_order.id"],
            name=op.f("fk_tw_fill_order_id_tw_order"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["tw_position.id"],
            name=op.f("fk_tw_fill_position_id_tw_position"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_tw_fill_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tw_fill")),
    )
    op.create_index("ix_tw_fill_position", "tw_fill", ["position_id", "filled_at"], unique=False)

    # --- 4. The plan a person confirms ------------------------------------------------------
    op.create_table(
        "tw_plan",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("gate", sa.String(length=8), nullable=False),
        sa.Column("sleeve_equity_inr", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "total_new_exposure_inr",
            sa.Numeric(precision=12, scale=2),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("gate IN ('OPEN', 'SHUT')", name=op.f("ck_tw_plan_gate_known")),
        sa.CheckConstraint(
            "source IN ('EVENING', 'MORNING', 'MANUAL')", name=op.f("ck_tw_plan_source_known")
        ),
        # The 30 minutes are config; what the database refuses is a plan that expired before it
        # was built, which is a clock bug rather than a setting.
        sa.CheckConstraint("expires_at > built_at", name=op.f("ck_tw_plan_expiry_after_build")),
        sa.CheckConstraint("sleeve_equity_inr >= 0", name=op.f("ck_tw_plan_equity_non_negative")),
        sa.CheckConstraint(
            "total_new_exposure_inr >= 0", name=op.f("ck_tw_plan_exposure_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_tw_plan_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tw_plan")),
        sa.UniqueConstraint("plan_id", name=op.f("uq_tw_plan_plan_id")),
    )
    op.create_index(
        "ix_tw_plan_session", "tw_plan", ["user_id", "session_date", "built_at"], unique=False
    )

    op.create_table(
        "tw_plan_line",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="PROPOSED", nullable=False),
        sa.Column("quantity", sa.Integer(), server_default="0", nullable=False),
        sa.Column("stop_price", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column(
            "value_inr", sa.Numeric(precision=12, scale=2), server_default="0", nullable=False
        ),
        # The ratchet's two explanatory numbers: the high the new trigger was derived from, and
        # the trigger resting at the exchange before it (`03` §7).
        sa.Column("high_since", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("previous_trigger", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("client_id", sa.String(length=128), nullable=True),
        sa.Column("journal_ref", sa.String(length=64), nullable=True),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("position_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # SELL_AT_OPEN is in the vocabulary and is produced by NOTHING in TWT-1: the strategy has
        # no end-of-day sell rule and the GTT is the exit (`01` §5). TW10 asserts that no rule
        # emits one. The kind exists so a MANUAL exit needs no migration.
        sa.CheckConstraint(
            "kind IN ('BUY_AT_OPEN', 'ARM_GTT', 'RAISE_GTT_STOP', 'SELL_AT_OPEN')",
            name=op.f("ck_tw_plan_line_kind_known"),
        ),
        sa.CheckConstraint(
            "state IN ('PROPOSED', 'CONFIRMED', 'SENT', 'FILLED', 'REJECTED', 'EXPIRED', "
            "'SKIPPED')",
            name=op.f("ck_tw_plan_line_state_known"),
        ),
        sa.CheckConstraint("quantity >= 0", name=op.f("ck_tw_plan_line_quantity_non_negative")),
        sa.CheckConstraint("value_inr >= 0", name=op.f("ck_tw_plan_line_value_non_negative")),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_tw_plan_line_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["tw_order.id"],
            name=op.f("fk_tw_plan_line_order_id_tw_order"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["tw_plan.id"],
            name=op.f("fk_tw_plan_line_plan_id_tw_plan"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["tw_position.id"],
            name=op.f("fk_tw_plan_line_position_id_tw_position"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_tw_plan_line_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tw_plan_line")),
        sa.UniqueConstraint("plan_id", "instrument_id", "kind", name="uq_tw_plan_line_once"),
    )
    op.create_index("ix_tw_plan_line_plan", "tw_plan_line", ["plan_id", "kind"], unique=False)

    op.create_table(
        "tw_plan_skip",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # `04` §10.1's twelve SkipReason values. A plan is not honest without them.
        sa.CheckConstraint(
            "reason IN ('GATE_SHUT', 'ALREADY_HELD', 'SESSION_CAP', 'SLOTS_FULL', "
            "'EXPOSURE_FULL', 'BELOW_MIN_TRADE_VALUE', 'TURNOVER_CAP', 'LOCKED_UPPER_CIRCUIT', "
            "'NO_SLEEVE_CAPITAL', 'NO_BAR', 'BELOW_LIQUIDITY_FLOOR', 'STOP_NOT_BELOW_ENTRY')",
            name=op.f("ck_tw_plan_skip_reason_known"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_tw_plan_skip_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["tw_plan.id"],
            name=op.f("fk_tw_plan_skip_plan_id_tw_plan"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_tw_plan_skip_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tw_plan_skip")),
    )
    op.create_index("ix_tw_plan_skip_plan", "tw_plan_skip", ["plan_id"], unique=False)

    # --- 5. The session log and the backtest ------------------------------------------------
    op.create_table(
        "tw_session",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("mode", sa.String(length=8), server_default="DRY_RUN", nullable=False),
        sa.Column("gate", sa.String(length=8), server_default="SHUT", nullable=False),
        sa.Column("states", sa.Integer(), server_default="0", nullable=False),
        sa.Column("signals", sa.Integer(), server_default="0", nullable=False),
        sa.Column("orders_placed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("confirms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("fills", sa.Integer(), server_default="0", nullable=False),
        # Its own column because it is the number that says whether the sleeve's one new
        # mechanism actually ran. A week of zeroes with the market up is a bug, not a quiet
        # spell (`03` §8).
        sa.Column("ratchets", sa.Integer(), server_default="0", nullable=False),
        sa.Column("exits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("naked_at_1515", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "first_live_entries_counted", sa.SmallInteger(), server_default="0", nullable=False
        ),
        sa.Column("counted_for_dry_run", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("plan_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("gate IN ('OPEN', 'SHUT')", name=op.f("ck_tw_session_gate_known")),
        sa.CheckConstraint("mode IN ('DRY_RUN', 'LIVE')", name=op.f("ck_tw_session_mode_known")),
        sa.CheckConstraint(
            "states >= 0 AND signals >= 0 AND orders_placed >= 0 AND confirms >= 0 "
            "AND fills >= 0 AND ratchets >= 0 AND exits >= 0 AND naked_at_1515 >= 0 "
            "AND first_live_entries_counted >= 0",
            name=op.f("ck_tw_session_counters_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_tw_session_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "session_date", name="pk_tw_session"),
    )

    op.create_table(
        "tw_backtest_run",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("drift", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('PLANT', 'RESEARCH_EXPORT')", name=op.f("ck_tw_backtest_run_source_known")
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_tw_backtest_run_finished_after_started"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_tw_backtest_run_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tw_backtest_run")),
    )
    # The page's one query: this user's latest FINISHED run per source — the latest finished,
    # not the latest started, so a run in flight never displaces the last good number.
    op.create_index(
        "ix_tw_backtest_run_latest",
        "tw_backtest_run",
        ["user_id", "source", "finished_at"],
        unique=False,
    )


def downgrade() -> None:
    """Undo all of it, for real.

    A downgrade that does not actually undo is worse than none, because it is the path somebody
    takes at 3am. The cycle's foreign key goes first — ``tw_order`` cannot be dropped while
    ``tw_position.order_id`` still points at it — and then the tables in reverse dependency
    order. Postgres drops a table's own indexes with it, so they are not listed separately.
    """
    op.drop_constraint(POSITION_ORDER_FK, "tw_position", type_="foreignkey")
    for table in TABLES:
        op.drop_table(table)
