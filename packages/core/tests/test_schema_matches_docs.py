"""The schema asserts docs/04-data-model.md, read as a specification rather than as our output.

The table and primary-key expectations below are transcribed from the DDL blocks in docs/04. If
the models drift from the document, these fail — which is the point.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Float, Numeric

from decile_core.models import Base

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Every table in docs/04, with the primary key its DDL declares.
#: Two tables are additions that docs/04 does not define, each with its own addendum:
#:   ``trading_day``   — Prompt 1 deliverable 3; see docs/04a-trading-day-addendum.md
#:   ``ingest_cursor`` — Prompt 3 deliverable 6; see docs/04b-pipeline-tables-addendum.md
DOCUMENTED_TABLES: dict[str, tuple[str, ...]] = {
    # Reference & instrument data
    "exchange": ("id",),
    "instrument": ("id",),
    "symbol_alias": ("id",),
    "trading_day": ("exchange_id", "date"),
    "ingest_cursor": ("kind", "instrument_id", "window_start"),
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
    # Accounts, billing, portfolios, backtests
    "app_user": ("id",),
    "plan": ("id",),
    "subscription": ("id",),
    "payment": ("id",),
    "portfolio": ("id",),
    "portfolio_holding": ("portfolio_id", "instrument_id"),
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
    # Billing additions — not in docs/04's DDL either. docs/11 §Security requires webhook
    # deduplication by event id and PROMPTS.md Prompt 13 requires gapless invoice numbers;
    # neither is possible with `plan`/`subscription`/`payment` alone. See
    # `decile_core.models.billing` and docs/DECISIONS.md.
    "webhook_event": ("id",),
    "invoice_counter": ("series",),
    # Prompt 14 §4: "persist each computed rebalance so a user can see what they were told and
    # when". docs/04 defines `portfolio` and `portfolio_holding` and stops there. See
    # `decile_core.models.accounts.PortfolioRebalance` and docs/DECISIONS.md §14.
    "portfolio_rebalance": ("id",),
    # Prompt 17 §4: "/admin (staff-only): ... entitlement override, and a reprocess-instrument
    # action." docs/09 §Observability puts the admin surface behind "staff auth" and docs/04
    # defines neither the grant nor the audit trail. See `decile_core.models.admin` and
    # docs/DECISIONS.md §17.
    "entitlement_override": ("id",),
    "admin_action": ("id",),
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
    for table in ("trading_day", "ingest_cursor"):
        assert table in combined, f"{table} is not described in any docs/04 addendum"


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
