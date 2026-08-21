"""Query tuning and the Timescale continuous aggregates (Prompt 16, deliverables 2 and 4).

Indexes
-------
1. ``ix_factor_daily_date`` is **dropped** and ``ix_factor_daily_date_marketcap_cr`` is rebuilt
   with ``INCLUDE (instrument_id)``. CLAUDE.md §"Open items" named this as Prompt 16's job:

       "`ix_factor_daily_date_marketcap_cr` is dead weight today. `ix_factor_daily_date` is a
        cheaper prefix index for the same predicate and PostgreSQL always picks it; neither can go
        index-only because a screen needs `instrument_id`. Prompt 16 should drop one or rebuild
        the other with `INCLUDE (instrument_id)`."

   Both, in fact: ``(date)`` is a strict prefix of ``(date, marketcap_cr)``, so anything the
   narrow index served the wide one serves too, and keeping both costs a second index write on
   every one of the ~2,300 rows the nightly ``compute_factors`` step inserts. With
   ``instrument_id`` in the payload, docs/06 §step 3's decile bucketing — "rank by marketcap
   within the universe, keep the top tenth" — reads ``(instrument_id, marketcap_cr)`` for one date
   and can now do it index-only.

2. ``ix_index_member_daily_date_instrument_id``. The factsheet asks the opposite question from
   the screener: not "who is in this index today" (which the existing ``(date, index_id)`` index
   answers) but "which indices is *this instrument* in today" (docs/10a §4, the percentile
   denominator). Neither the primary key ``(index_id, date, instrument_id)`` nor the existing
   index has ``instrument_id`` in a usable position, so that lookup scans every membership row
   for the date — ~2,300 x 145 at full size, on a page docs/11 budgets at 300 ms TTFB.

3. ``ix_instrument_listings_page``. docs/07's listings register orders by
   ``coalesce(listed_on, '0001-01-01') DESC, symbol ASC`` over active instruments and pages with a
   keyset cursor (Prompt 11 acceptance criterion 3). That is an *expression* sort key, so an index
   on ``listed_on`` alone cannot serve it; with ``enable_seqscan = off`` the planner still chose a
   sequential scan, which is the definition of "no index applies".

Continuous aggregates
---------------------
docs/03 §"Scaling plan" step 3: "Timescale continuous aggregates for market-health and index
history." Both source tables are plain tables in docs/04, and a continuous aggregate can only be
defined over a hypertable — so they are converted first, on the same one-year chunk interval
docs/04 uses for every other hypertable. ``date`` is already the second column of both primary
keys, which is what makes the conversion legal.

The aggregates bucket **monthly**. The daily rows stay the source of truth and every endpoint
still reads them; the aggregate exists for the long-range history charts, where a five-year daily
series is 1,250 points rendered into a few hundred pixels. Nothing reads them yet — docs/03 says
the scaling plan is followed "only when measured", and at one seeded date there is nothing to
measure. See docs/DECISIONS.md §16.

Revision ID: 0008_performance_indexes
Revises: 0007_portfolio_rebalance
Create Date: 2026-08-21 09:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_performance_indexes"
down_revision: str | None = "0007_portfolio_rebalance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: ``baskfy_api.market_data.NO_LISTING_DATE`` — the sentinel an instrument with no listing date
#: sorts under. Repeated here as a literal because a migration must not import application code
#: that may change under it.
NO_LISTING_DATE = "0001-01-01"

MARKET_HEALTH_SOURCE = "market_health_daily"
INDEX_SNAPSHOT_SOURCE = "index_snapshot_daily"
MARKET_HEALTH_MONTHLY = "market_health_monthly"
INDEX_SNAPSHOT_MONTHLY = "index_snapshot_monthly"


def upgrade() -> None:
    # --- 1. factor_daily: one index instead of two, and it covers ------------
    op.execute("DROP INDEX IF EXISTS ix_factor_daily_date_marketcap_cr")
    op.execute("DROP INDEX IF EXISTS ix_factor_daily_date")
    op.execute(
        "CREATE INDEX ix_factor_daily_date_marketcap_cr ON factor_daily "
        "(date, marketcap_cr) INCLUDE (instrument_id)"
    )

    # --- 2. index_member_daily: the factsheet's direction --------------------
    op.execute(
        "CREATE INDEX ix_index_member_daily_date_instrument_id ON index_member_daily "
        "(date, instrument_id)"
    )

    # --- 3. instrument: the listings register's exact sort key ---------------
    op.execute(
        "CREATE INDEX ix_instrument_listings_page ON instrument "
        f"((coalesce(listed_on, DATE '{NO_LISTING_DATE}')) DESC, symbol ASC) "
        # `is_active IS TRUE`, not `is_active`: the handler writes the predicate that way
        # (`Instrument.is_active.is_(True)`) and PostgreSQL's partial-index prover does not
        # derive one form from the other. A predicate that does not match to the letter gives an
        # index nothing will ever use.
        "WHERE is_active IS TRUE"
    )

    # --- 4. Hypertables + continuous aggregates (docs/03 §Scaling plan 3) ----
    for table in (INDEX_SNAPSHOT_SOURCE, MARKET_HEALTH_SOURCE):
        op.execute(
            f"SELECT create_hypertable('{table}', 'date', "
            "chunk_time_interval => INTERVAL '1 year', migrate_data => true, "
            "if_not_exists => true)"
        )

    op.execute(
        f"""
        CREATE MATERIALIZED VIEW {MARKET_HEALTH_MONTHLY}
        WITH (timescaledb.continuous) AS
        SELECT
            index_id,
            time_bucket(INTERVAL '1 month', date) AS bucket,
            avg(pct_above_200dma)     AS pct_above_200dma,
            avg(pct_above_50dma)      AS pct_above_50dma,
            avg(pct_within_10pct_ath) AS pct_within_10pct_ath,
            avg(pct_ret_1y_positive)  AS pct_ret_1y_positive,
            max(constituent_count)    AS constituent_count,
            count(*)                  AS trading_days
        FROM {MARKET_HEALTH_SOURCE}
        GROUP BY index_id, bucket
        WITH NO DATA
        """
    )
    op.execute(
        f"""
        CREATE MATERIALIZED VIEW {INDEX_SNAPSHOT_MONTHLY}
        WITH (timescaledb.continuous) AS
        SELECT
            index_id,
            time_bucket(INTERVAL '1 month', date) AS bucket,
            first(level, date) AS open_level,
            last(level, date)  AS close_level,
            min(level)         AS low_level,
            max(level)         AS high_level,
            count(*)           AS trading_days
        FROM {INDEX_SNAPSHOT_SOURCE}
        GROUP BY index_id, bucket
        WITH NO DATA
        """
    )

    # A refresh policy, so the aggregate is maintained by Timescale rather than by remembering to
    # call `refresh_continuous_aggregate`. `end_offset` of one day keeps the *current* bucket out
    # of the materialisation — today's row is still being written by the nightly pipeline, and a
    # materialised average of a half-written month is exactly the "half-written day" docs/06
    # §step 1 rules out.
    for view in (MARKET_HEALTH_MONTHLY, INDEX_SNAPSHOT_MONTHLY):
        op.execute(
            f"SELECT add_continuous_aggregate_policy('{view}', "
            "start_offset => INTERVAL '3 months', "
            "end_offset => INTERVAL '1 day', "
            "schedule_interval => INTERVAL '1 day')"
        )


def downgrade() -> None:
    for view in (MARKET_HEALTH_MONTHLY, INDEX_SNAPSHOT_MONTHLY):
        op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {view}")
    # The hypertable conversion is deliberately *not* reversed. `create_hypertable` has no
    # inverse; undoing it means copying every row into a fresh plain table, and a downgrade that
    # rewrites two tables to restore a storage detail no query depends on is more dangerous than
    # the thing it undoes. `downgrade base` drops both tables outright (migration 0001), which is
    # the path `make downgrade` actually takes.

    op.execute("DROP INDEX IF EXISTS ix_instrument_listings_page")
    op.execute("DROP INDEX IF EXISTS ix_index_member_daily_date_instrument_id")
    op.execute("DROP INDEX IF EXISTS ix_factor_daily_date_marketcap_cr")
    op.execute("CREATE INDEX ix_factor_daily_date ON factor_daily (date)")
    op.execute(
        "CREATE INDEX ix_factor_daily_date_marketcap_cr ON factor_daily (date, marketcap_cr)"
    )
