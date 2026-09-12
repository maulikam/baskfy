"""The schema asserts docs/04-data-model.md, read as a specification rather than as our output.

The table and primary-key expectations below are transcribed from the DDL blocks in docs/04. If
the models drift from the document, these fail — which is the point.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import DefaultClause, Float, Numeric

from baskfy_core.models import Base

REPO_ROOT = Path(__file__).resolve().parents[3]
MONOREPO_ROOT = Path(__file__).resolve().parents[4]

#: Every table in docs/04, with the primary key its DDL declares.
#: Two tables are additions that docs/04 does not define, each with its own addendum:
#:   ``trading_day``   — Prompt 1 deliverable 3; see docs/04a-trading-day-addendum.md
#:   ``ingest_cursor`` — Prompt 3 deliverable 6; see docs/04b-pipeline-tables-addendum.md
#:   ``basket_snapshot`` — M30's nightly basket cache; see docs/04b-pipeline-tables-addendum.md
#:   the four portfolio-redesign tables — see docs/04d-portfolio-redesign-addendum.md
DOCUMENTED_TABLES: dict[str, tuple[str, ...]] = {
    # Reference & instrument data
    "exchange": ("id",),
    "instrument": ("id",),
    "symbol_alias": ("id",),
    "trading_day": ("exchange_id", "date"),
    "ingest_cursor": ("kind", "instrument_id", "window_start"),
    # M30: the nightly chain's basket, stored so `/baskets` reads a row instead of loading nine
    # years of bars per request. docs/04b addendum.
    "basket_snapshot": ("id",),
    # M34: a portfolio divided across screens plus a slice run by hand. docs/04b addendum.
    "portfolio_sleeve": ("id",),
    # Price history
    "ohlcv_daily": ("instrument_id", "date"),
    # Corporate actions
    "corporate_action": ("id",),
    # Index universes
    "index_def": ("id",),
    "index_member_daily": ("index_id", "date", "instrument_id"),
    "index_snapshot_daily": ("index_id", "date"),
    # Fundamentals
    "fundamental_daily": ("instrument_id", "date"),
    # The fact table
    "factor_daily": ("instrument_id", "date"),
    # Screens
    "screen": ("id",),
    "screen_run": ("id",),
    # Market health
    "market_health_daily": ("index_id", "date"),
    # Public API, screen alerts and outbound webhooks — not in docs/04's DDL. Prompt 20 builds
    # three features the data model predates; each table is argued for in docs/DECISIONS.md §20
    # and on `baskfy_core.models.integrations`.
    "api_key": ("id",),
    "api_key_usage_daily": ("api_key_id", "date"),
    "screen_alert": ("id",),
    "screen_alert_delivery": ("id",),
    "webhook_endpoint": ("id",),
    "webhook_delivery": ("id",),
    # Accounts, billing, portfolios, backtests
    "app_user": ("id",),
    "broker_account": ("id",),
    "plan": ("id",),
    "subscription": ("id",),
    "payment": ("id",),
    "portfolio": ("id",),
    # M35 / migration 0019: broker_account_id joins the key so the same instrument held at two
    # brokers is two rows a per-broker roll-up can add up. docs/04b addendum.
    "portfolio_holding": ("portfolio_id", "instrument_id", "broker_account_id"),
    # PORTFOLIO_REDESIGN.md's three data layers — docs/04d-portfolio-redesign-addendum.md
    "broker_cash": ("broker_account_id",),
    "portfolio_cash_flow": ("id",),
    "portfolio_nav_daily": ("portfolio_id", "user_id", "date"),
    "reconciliation_item": ("id",),
    "backtest": ("id",),
    "pipeline_run": ("id",),
    "pipeline_run_step": ("id",),
    # Authentication, consent and erasure — not in docs/04's DDL. Each is required by a numbered
    # line of docs/11 §Security or §"Compliance & legal (India)"; the reasoning per table is in
    # docs/04c-auth-tables-addendum.md and on the models themselves.
    "auth_verification_token": ("identifier", "token"),
    "auth_token": ("id",),
    "refresh_token": ("id",),
    "auth_lockout": ("identifier",),
    "account_deletion": ("user_id",),
    "consent_record": ("id",),
    # M46: Google sign-in replaced the email/password funnel, so identity is federated and an
    # account is bound to a provider subject rather than to a password hash.
    # docs/04c-auth-tables-addendum.md.
    "auth_identity": ("id",),
    # Billing additions — not in docs/04's DDL either. docs/11 §Security requires webhook
    # deduplication by event id and PROMPTS.md Prompt 13 requires gapless invoice numbers;
    # neither is possible with `plan`/`subscription`/`payment` alone. See
    # `baskfy_core.models.billing` and docs/DECISIONS.md.
    "webhook_event": ("id",),
    "invoice_counter": ("series",),
    # Prompt 14 §4: "persist each computed rebalance so a user can see what they were told and
    # when". docs/04 defines `portfolio` and `portfolio_holding` and stops there. See
    # `baskfy_core.models.accounts.PortfolioRebalance` and docs/DECISIONS.md §14.
    "portfolio_rebalance": ("id",),
    # Curated-basket product layer — docs/smallcase/03-data-model.md (SC1).
    "cb_manager": ("id",),
    # Tree-3 managers / migration 0020: what a manager is owed, as an agreement with a
    # start and an end. Dark Track-B; no default rate, because D7 amounts are human-track.
    # docs/04b addendum.
    "cb_manager_revenue_share": ("id",),
    "cb_basket": ("id",),
    "cb_basket_version": ("id",),
    "cb_constituent": ("id",),
    "cb_metrics": ("basket_id", "as_of_date"),
    "cb_collection": ("id",),
    "cb_watchlist_item": ("id",),
    "cb_investment": ("id",),
    "cb_investment_holding": ("id",),
    "cb_order_batch": ("id",),
    "cb_fee_ledger": ("id",),
    "cb_dividend": ("id",),
    "cb_sip_plan": ("id",),
    "cb_pending_action": ("id",),
    "cb_update_post": ("id",),
    "cb_user_rebalance_state": ("user_id", "version_id"),
    "cb_plan": ("id",),
    "cb_subscription": ("id",),
    # Prompt 17 §4: "/admin (staff-only): ... entitlement override, and a reprocess-instrument
    # action." docs/09 §Observability puts the admin surface behind "staff auth" and docs/04
    # defines neither the grant nor the audit trail. See `baskfy_core.models.admin` and
    # docs/DECISIONS.md §17.
    "entitlement_override": ("id",),
    "admin_action": ("id",),
    # The swing book — docs/swing/03-data-model.md (SW2). Twelve tables, every one keyed with
    # `user_id` (docs/swing/02 Track C §6). `sw_setup_daily` and `sw_market_daily` carry it in
    # the primary key rather than beside it; docs/swing/DECISIONS-SW.md SW2.1 says why.
    # `sw_backtest_run` is SW9's (docs/swing/03 §10, migration 0029): one append-only row per run.
    "sw_config": ("user_id",),
    "sw_config_audit": ("id",),
    "sw_setup_daily": ("user_id", "date", "instrument_id", "setup"),
    "sw_market_daily": ("user_id", "date"),
    "sw_watch": ("id",),
    "sw_signal": ("id",),
    "sw_plan": ("id",),
    "sw_plan_line": ("id",),
    "sw_plan_skip": ("id",),
    "sw_position": ("id",),
    "sw_fill": ("id",),
    "sw_session": ("user_id", "session_date"),
    "sw_backtest_run": ("id",),
    # SW11B (docs/swing/03 §11, migration 0032): a headline, a stamp and a link per watched name.
    "sw_catalyst": ("id",),
    # SW15 (docs/swing/03 §12, migration 0033): one row per press of "Scan now".
    "sw_scan_run": ("id",),
    # The volume-breakout sleeve — docs/vbt/03-data-model.md (VB3, migration 0037). Twelve
    # tables, every one keyed with `user_id` (docs/vbt/02 Track C §6), the same shape the swing
    # book uses above and for the same reason: a sleeve is one person's book. `vb_signal_daily`
    # and `vb_breadth_daily` carry the user in the primary key rather than beside it
    # (DECISIONS-VB VB3.1); `vb_backtest_run` is append-only (docs/vbt/03 §11).
    "vb_config": ("user_id",),
    "vb_config_audit": ("id",),
    "vb_signal_daily": ("user_id", "date", "instrument_id"),
    "vb_breadth_daily": ("user_id", "date"),
    "vb_order": ("id",),
    "vb_position": ("id",),
    "vb_fill": ("id",),
    "vb_plan": ("id",),
    "vb_plan_line": ("id",),
    "vb_plan_skip": ("id",),
    "vb_session": ("user_id", "session_date"),
    "vb_backtest_run": ("id",),
    # VB12 (docs/vbt/03 §13, migration 0040): one row per press of the desk's Re-detect button.
    "vb_scan_run": ("id",),
    # The three-weeks-tight sleeve — docs/twt/03-data-model.md (TW3, migration 0041, plus
    # TW12's `tw_scan_run` from 0042). Fourteen tables, every one keyed with `user_id`
    # (docs/twt/02 Track C §6), the same shape the swing
    # book and VBT-1 use above and for the same reason: a sleeve is one person's book. Three
    # carry the user in the primary key rather than beside it — `tw_state_daily`,
    # `tw_signal_daily` and `tw_breadth_daily` — because each is a snapshot of what one user's
    # system saw. `tw_state_daily` is the one with no sibling in the other two sleeves: TWT-1's
    # scan is a *state* and its signal is the first day of one, so the state is stored too
    # (docs/twt/03 §2). `tw_backtest_run` is append-only (docs/twt/03 §9).
    "tw_config": ("user_id",),
    "tw_config_audit": ("id",),
    "tw_state_daily": ("user_id", "date", "instrument_id"),
    "tw_signal_daily": ("user_id", "date", "instrument_id"),
    "tw_breadth_daily": ("user_id", "date"),
    "tw_position": ("id",),
    "tw_order": ("id",),
    "tw_fill": ("id",),
    "tw_plan": ("id",),
    "tw_plan_line": ("id",),
    "tw_plan_skip": ("id",),
    "tw_session": ("user_id", "session_date"),
    "tw_backtest_run": ("id",),
    # TW12: one press of "Scan now" (docs/twt/03 §11, migration 0042). Append-only like
    # `tw_backtest_run`, and the only `tw_` table that records a *request* rather than a
    # measurement. There is deliberately no `provisional` column — DECISIONS-TW TW12.2.
    "tw_scan_run": ("id",),
    # AF lane I (migration 0045): the Stocks watchlist and the goal composer's saved answers.
    # Neither is a curated basket, so neither belongs with the `cb_` tables — a watched
    # *instrument* is a bookmark on a symbol, and the preferences row is what `/discover`
    # reads when there is no query string. docs/04e-stocks-watchlist-addendum.md.
    "instrument_watch_item": ("id",),
    "user_discover_preferences": ("user_id",),
}

#: docs/04 opening paragraph: "Money in numeric, never float."
#: docs/02 rule: prices, quantities and money are exact. Every column whose name matches one of
#: these fragments is checked, so a new float column cannot slip in later either.
MONETARY_NAME_FRAGMENTS: tuple[str, ...] = (
    "price",
    "close",
    "open",
    "high",
    "low",
    "amount",
    "inr",
    "marketcap",
    "ma_",
    "level",
    "turnover",
    "volume",
    "vol_",
    "quantity",
    "weight",
    "beta",
    "sharpe",
    "ret_",
    "rsi",
    "pe",
    "pb",
    "yield",
    "face_value",
    "adj_factor",
    "median_vol",
)


@pytest.mark.parametrize("table_name", sorted(DOCUMENTED_TABLES))
def test_table_exists(table_name: str) -> None:
    assert table_name in Base.metadata.tables


@pytest.mark.parametrize(
    ("table_name", "expected_pk"), sorted((t, k) for t, k in DOCUMENTED_TABLES.items())
)
def test_primary_key_matches_docs(table_name: str, expected_pk: tuple[str, ...]) -> None:
    table = Base.metadata.tables[table_name]
    assert tuple(c.name for c in table.primary_key.columns) == expected_pk


def test_no_undocumented_tables() -> None:
    """A table nobody wrote down is a table nobody maintains.

    A new table must be added to this list *and* to an addendum in docs/, so the data model stays
    the thing docs/ describes rather than whatever the code happens to contain.
    """
    assert set(Base.metadata.tables) == set(DOCUMENTED_TABLES)


def test_every_added_table_has_an_addendum() -> None:
    """The two tables not in docs/04 must each be written down somewhere in docs/."""
    docs = REPO_ROOT / "docs"
    combined = "\n".join(path.read_text(encoding="utf-8") for path in docs.glob("04*addendum*.md"))
    for table in ("trading_day", "ingest_cursor", "basket_snapshot", "portfolio_sleeve"):
        assert table in combined, f"{table} is not described in any docs/04 addendum"


def test_smallcase_tables_are_recorded_in_docs() -> None:
    """SC1 tables live in docs/smallcase/03, not docs/04."""
    smallcase_model = (MONOREPO_ROOT / "docs" / "smallcase" / "03-data-model.md").read_text(
        encoding="utf-8"
    )
    for table in (
        "cb_manager",
        "cb_basket",
        "cb_basket_version",
        "cb_constituent",
        "cb_metrics",
        "cb_collection",
        "cb_watchlist_item",
        "cb_investment",
        "cb_order_batch",
        "broker_account",
        "cb_plan",
        "cb_subscription",
    ):
        assert table in smallcase_model, f"{table} is not described in docs/smallcase/03"


def test_swing_tables_are_recorded_in_docs() -> None:
    """SW2 tables live in docs/swing/03, not docs/04 — the same rule as the smallcase ones.

    The list is written out rather than derived from `Base.metadata` on purpose: deriving it
    would make the test "every sw_ table this file already knows about is documented", which is
    true by construction. Typing the names is what makes adding another a deliberate act in
    two places.
    """
    swing_model = (MONOREPO_ROOT / "docs" / "swing" / "03-data-model.md").read_text(
        encoding="utf-8"
    )
    for table in (
        "sw_config",
        "sw_config_audit",
        "sw_setup_daily",
        "sw_market_daily",
        "sw_watch",
        "sw_signal",
        "sw_plan",
        "sw_plan_line",
        "sw_plan_skip",
        "sw_position",
        "sw_fill",
        "sw_session",
        "sw_backtest_run",
        "sw_catalyst",
        "sw_scan_run",
    ):
        assert table in swing_model, f"{table} is not described in docs/swing/03"


def test_vbt_tables_are_recorded_in_docs() -> None:
    """VB3 tables live in docs/vbt/03, by the same rule as the swing book's above.

    Written out rather than derived, for the reason the swing version gives: a derived list
    asserts only that this file agrees with itself.
    """
    vbt_model = (MONOREPO_ROOT / "docs" / "vbt" / "03-data-model.md").read_text(encoding="utf-8")
    for table in (
        "vb_config",
        "vb_config_audit",
        "vb_signal_daily",
        "vb_breadth_daily",
        "vb_order",
        "vb_position",
        "vb_fill",
        "vb_plan",
        "vb_plan_line",
        "vb_plan_skip",
        "vb_session",
        "vb_backtest_run",
        "vb_scan_run",
    ):
        assert table in vbt_model, f"{table} is not described in docs/vbt/03"


def test_twt_tables_are_recorded_in_docs() -> None:
    """TW3 tables live in docs/twt/03, by the same rule as the swing book's and VBT-1's above.

    Written out rather than derived, for the reason the swing version gives: a derived list
    asserts only that this file agrees with itself.
    """
    twt_model = (MONOREPO_ROOT / "docs" / "twt" / "03-data-model.md").read_text(encoding="utf-8")
    for table in (
        "tw_config",
        "tw_config_audit",
        "tw_state_daily",
        "tw_signal_daily",
        "tw_breadth_daily",
        "tw_position",
        "tw_order",
        "tw_fill",
        "tw_plan",
        "tw_plan_line",
        "tw_plan_skip",
        "tw_session",
        "tw_backtest_run",
        "tw_scan_run",
    ):
        assert table in twt_model, f"{table} is not described in docs/twt/03"


#: Every column `docs/twt/03` names in prose, table by table. The point is not coverage for its
#: own sake: each of these is a column some later module reads by name, and a schema that drifts
#: from the document by one column is a module that needs a migration on the morning it runs.
TWT_DOCUMENTED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("tw_config", "sleeve_capital_inr"),
    ("tw_config", "max_open_positions"),
    ("tw_config", "max_position_pct"),
    ("tw_config", "stop_pct"),
    ("tw_config", "trail_pct"),
    ("tw_config", "first_live_entries_left"),
    ("tw_config", "dry_run_sessions"),
    ("tw_state_daily", "close_raw"),
    ("tw_state_daily", "adj_factor"),
    ("tw_state_daily", "week_close_0"),
    ("tw_state_daily", "week_close_1"),
    ("tw_state_daily", "week_close_2"),
    ("tw_state_daily", "week_range_pct"),
    ("tw_state_daily", "month_low_3"),
    ("tw_state_daily", "month_low_ratio"),
    ("tw_state_daily", "vol_sma_50"),
    ("tw_state_daily", "turnover_inr"),
    ("tw_state_daily", "turnover_avg_20"),
    ("tw_state_daily", "sma_dma"),
    ("tw_state_daily", "sessions_in_state"),
    ("tw_state_daily", "bars_in_window"),
    ("tw_state_daily", "locked_upper_circuit"),
    ("tw_state_daily", "pipeline_run_id"),
    ("tw_signal_daily", "state"),
    ("tw_signal_daily", "failed_filters"),
    ("tw_signal_daily", "entry_reference_close"),
    ("tw_signal_daily", "stop_preview"),
    ("tw_signal_daily", "sessions_out_before"),
    ("tw_signal_daily", "rank_key"),
    ("tw_signal_daily", "turnover_avg_20"),
    ("tw_breadth_daily", "universe_count"),
    ("tw_breadth_daily", "measured_count"),
    ("tw_breadth_daily", "above_count"),
    ("tw_breadth_daily", "pct_above_dma"),
    ("tw_breadth_daily", "gate"),
    ("tw_breadth_daily", "dma_bars"),
    ("tw_breadth_daily", "thin_session"),
    ("tw_breadth_daily", "detail"),
    ("tw_position", "order_id"),
    ("tw_position", "signal_date"),
    ("tw_position", "entry_date"),
    ("tw_position", "entry_avg"),
    ("tw_position", "quantity_entered"),
    ("tw_position", "initial_stop"),
    ("tw_position", "stop_price"),
    ("tw_position", "high_since"),
    ("tw_position", "high_since_date"),
    ("tw_position", "gtt_id"),
    ("tw_position", "gtt_trigger"),
    ("tw_position", "gtt_armed_at"),
    ("tw_position", "next_trigger"),
    ("tw_position", "next_trigger_for"),
    ("tw_position", "quantity_open"),
    ("tw_position", "closed_on"),
    ("tw_position", "exit_avg"),
    ("tw_position", "close_reason"),
    ("tw_position", "pnl_inr"),
    ("tw_position", "return_pct"),
    ("tw_position", "r_multiple"),
    ("tw_position", "hold_sessions"),
    ("tw_position", "simulated"),
    ("tw_position", "half_size"),
    ("tw_fill", "journal_ref"),
    ("tw_order", "signal_date"),
    ("tw_order", "side"),
    ("tw_order", "stop_price"),
    ("tw_order", "broker_order_id"),
    ("tw_order", "client_id"),
    ("tw_order", "filled_quantity"),
    ("tw_order", "avg_fill_price"),
    ("tw_order", "position_id"),
    ("tw_plan", "plan_id"),
    ("tw_plan", "built_at"),
    ("tw_plan", "expires_at"),
    ("tw_plan", "plan_hash"),
    ("tw_plan", "gate"),
    ("tw_plan", "sleeve_equity_inr"),
    ("tw_plan", "total_new_exposure_inr"),
    ("tw_plan_line", "kind"),
    ("tw_plan_line", "value_inr"),
    ("tw_plan_line", "stop_price"),
    ("tw_plan_line", "high_since"),
    ("tw_plan_line", "journal_ref"),
    ("tw_plan_skip", "reason"),
    ("tw_session", "mode"),
    ("tw_session", "plan_ids"),
    ("tw_session", "ratchets"),
    ("tw_session", "naked_at_1515"),
    ("tw_session", "first_live_entries_counted"),
    ("tw_session", "counted_for_dry_run"),
    ("tw_backtest_run", "params"),
    ("tw_backtest_run", "started_at"),
    ("tw_backtest_run", "finished_at"),
    ("tw_backtest_run", "source"),
    ("tw_backtest_run", "stats"),
    ("tw_backtest_run", "drift"),
    ("tw_backtest_run", "error"),
)


#: Four columns `docs/twt/03` names in a shorthand rather than one backticked name each. The
#: literal the document uses is given here instead of loosening the check for every column: a
#: test that accepted a bare substring would pass on `date` appearing in a sentence.
TWT_DOC_SPELLING: dict[str, str] = {
    # §2 writes the three weekly closes as one row of its table.
    "week_close_0": "`week_close_0/1/2`",
    "week_close_1": "`week_close_0/1/2`",
    "week_close_2": "`week_close_0/1/2`",
    # §7 gives the plan's expiry as the arithmetic that produces it.
    "expires_at": "`expires_at = built_at\n+ 30 min`",
}


@pytest.mark.parametrize(("table", "column"), TWT_DOCUMENTED_COLUMNS)
def test_twt_columns_are_modelled_and_recorded_in_docs(table: str, column: str) -> None:
    """TW3: the model has each column `docs/twt/03` names, and the document still names it."""
    twt_model = (MONOREPO_ROOT / "docs" / "twt" / "03-data-model.md").read_text(encoding="utf-8")
    assert column in Base.metadata.tables[table].c, f"{table}.{column} is not modelled"
    spelled = TWT_DOC_SPELLING.get(column, f"`{column}`")
    assert spelled in twt_model, f"{table}.{column} is not described in docs/twt/03"


def test_twt_position_carries_the_factor_its_split_rule_reads() -> None:
    """`tw_position.entry_adj_factor` is the one TW3 column `docs/twt/03` §5 does not list.

    `docs/twt/04` §7.3 and DECISIONS-TW TW0.7 both read it by name — the evening job notices a
    corporate action by comparing the as-of row's `adj_factor` with *the position's* — and §5's
    column table simply omits it. A rule that needs a column the schema does not have is a rule
    that needs a migration on the morning of a split, so the column is here and this test says
    which document asked for it. DECISIONS-TW TW3.2.
    """
    assert "entry_adj_factor" in Base.metadata.tables["tw_position"].c
    rules = (MONOREPO_ROOT / "docs" / "twt" / "04-business-rules.md").read_text(encoding="utf-8")
    assert "entry_adj_factor" in rules


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("sw_config", "sleeve_peak_inr"),
        ("sw_config", "drawdown_pct"),
        ("sw_config", "drawdown_locked"),
        ("sw_market_daily", "drawdown_pct"),
        ("sw_market_daily", "drawdown_locked"),
    ],
)
def test_swing_drawdown_columns_are_modelled_and_recorded_in_docs(table: str, column: str) -> None:
    """SW9.5 (docs/swing/07): the drawdown containment of `04` §8.5 needs the sleeve's peak, its
    drawdown and the lock-out on `sw_config` (§1) and the two measurements on `sw_market_daily`
    (§3). Migration `0030_swing_primary_sources.py` creates them; this is the check that the
    model has each one and `docs/swing/03` names it, so the column and its meaning cannot drift
    apart. The two `sw_config` defaults the same migration moves are asserted beside them."""
    swing_model = (MONOREPO_ROOT / "docs" / "swing" / "03-data-model.md").read_text(
        encoding="utf-8"
    )
    assert column in Base.metadata.tables[table].c, f"{table}.{column} is not modelled"
    assert f"`{column}`" in swing_model, f"{table}.{column} is not described in docs/swing/03"
    config = Base.metadata.tables["sw_config"].c
    defaults = {name: config[name].server_default for name in ("max_open_positions", "adr_min_pct")}
    assert all(isinstance(default, DefaultClause) for default in defaults.values())
    assert {name: str(getattr(default, "arg", None)) for name, default in defaults.items()} == {
        "max_open_positions": "10",
        "adr_min_pct": "4.00",
    }


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("sw_watch", "score"),
        ("sw_watch", "adr_pct"),
        ("sw_watch", "focus"),
        ("sw_watch", "reconfirmed_on"),
        ("sw_position", "half_risk"),
        ("sw_session", "first_live_counted"),
        # SW11B (A3, migration 0032): the earnings flag and the catalyst row's columns.
        ("sw_watch", "earnings_date"),
        ("sw_catalyst", "headline"),
        ("sw_catalyst", "published_at"),
        ("sw_catalyst", "url"),
        ("sw_catalyst", "source"),
        ("sw_catalyst", "earnings_date"),
        # SW15 (migration 0033): the provisional flag on both detection tables and the run log.
        ("sw_setup_daily", "provisional"),
        ("sw_market_daily", "provisional"),
        ("sw_scan_run", "requested_at"),
        ("sw_scan_run", "session_date"),
        ("sw_scan_run", "provisional"),
        ("sw_scan_run", "status"),
        ("sw_scan_run", "detail"),
        ("sw_scan_run", "error"),
        ("sw_scan_run", "task_id"),
    ],
)
def test_swing_review_columns_are_modelled_and_recorded_in_docs(table: str, column: str) -> None:
    """SW10.5 (docs/swing/STANDING-ANSWERS A7, A9, A14): migration
    `0031_swing_review_corrections.py` adds the watch funnel's score / ADR / focus / re-confirm
    columns (§4), the journal's half-risk tag (§7) and the first-live bookkeeping (§8). The
    model has each one and `docs/swing/03` names it, so column and meaning cannot drift."""
    swing_model = (MONOREPO_ROOT / "docs" / "swing" / "03-data-model.md").read_text(
        encoding="utf-8"
    )
    assert column in Base.metadata.tables[table].c, f"{table}.{column} is not modelled"
    assert f"`{column}`" in swing_model, f"{table}.{column} is not described in docs/swing/03"


def test_billing_tables_are_recorded_in_decisions() -> None:
    """The Prompt 13, 14 and 17 additions are written down too, in docs/DECISIONS.md.

    Same rule as the addendum test above: a table nobody wrote down is a table nobody maintains.
    These are recorded in DECISIONS.md rather than a `04x` addendum because the overnight build
    was permitted to append there and nowhere else under docs/.
    """
    decisions = (REPO_ROOT / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
    for table in (
        "webhook_event",
        "invoice_counter",
        "portfolio_rebalance",
        "entitlement_override",
        "admin_action",
        "api_key",
        "api_key_usage_daily",
        "screen_alert",
        "screen_alert_delivery",
        "webhook_endpoint",
        "webhook_delivery",
    ):
        assert table in decisions, f"{table} is not described in docs/DECISIONS.md"


def test_no_float_columns_anywhere() -> None:
    """docs/04: money in numeric, never float. Checked across every column, not a sample."""
    offenders = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, Float)
    ]
    assert offenders == []


def test_monetary_columns_are_exact_numerics_or_integers() -> None:
    """Prices, quantities and money must be Numeric or an integer type — never approximate."""
    offenders: list[str] = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if not any(f in column.name for f in MONETARY_NAME_FRAGMENTS):
                continue
            python_type = getattr(column.type, "python_type", None)
            resolved = python_type if python_type is not None else object
            if resolved is float:
                offenders.append(f"{table.name}.{column.name} -> {column.type}")
    assert offenders == []


class TestPrecisionMatchesTheReferenceExport:
    """docs/13 §4 pins storage precision; docs/04's "Precision matters" note defers to it."""

    @staticmethod
    def _numeric(table: str, column: str) -> Numeric[Decimal]:
        column_type = Base.metadata.tables[table].columns[column].type
        assert isinstance(column_type, Numeric)
        return column_type

    @pytest.mark.parametrize("column", ["vol_1m", "vol_3m", "vol_6m", "vol_9m", "vol_12m"])
    def test_volatility_keeps_ten_decimals(self, column: str) -> None:
        """docs/13 §4: volatility is a decimal fraction at 8-10 dp and feeds a division.

        At the numeric(12,4) of docs/04's DDL sketch, CUPID's 0.5793179400 becomes 0.5793 and
        sharpe = ret / (vol x 100) stops reproducing the export.
        """
        assert self._numeric("factor_daily", column).scale == 10

    def test_beta_keeps_ten_decimals(self) -> None:
        assert self._numeric("factor_daily", "beta_12m").scale == 10

    @pytest.mark.parametrize(
        "column",
        ["close", "close_raw", "ma_20", "ma_50", "ma_100", "ma_200", "high_1y", "high_ath"],
    )
    def test_prices_and_moving_averages_are_two_decimals(self, column: str) -> None:
        assert self._numeric("factor_daily", column).scale == 2

    @pytest.mark.parametrize("column", ["rsi_1m", "rsi_3m", "rsi_6m", "rsi_9m", "rsi_12m"])
    def test_rsi_is_four_decimals(self, column: str) -> None:
        assert self._numeric("factor_daily", column).scale == 4

    @pytest.mark.parametrize(
        "column", ["ret_1m", "ret_12m", "sharpe_1m", "sharpe_12m", "away_high_1y", "pos_days_12m"]
    )
    def test_percentages_are_two_decimals(self, column: str) -> None:
        assert self._numeric("factor_daily", column).scale == 2

    @pytest.mark.parametrize(
        "column", ["marketcap_cr", "median_vol_12m", "vol_day_val", "vol_avg_1w", "vol_avg_12m"]
    )
    def test_marketcap_and_turnover_are_integers(self, column: str) -> None:
        """docs/13 §4: marketcap is an integer in ₹ crore; volumes are bigint rupees."""
        column_type = Base.metadata.tables["factor_daily"].columns[column].type
        assert column_type.python_type is int
