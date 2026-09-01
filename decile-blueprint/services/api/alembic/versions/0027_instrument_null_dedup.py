"""Stop `instrument` accumulating a duplicate row per night, and merge the ones already there.

`uq_instrument_exchange_id_symbol_series` is UNIQUE (exchange_id, symbol, series), and Postgres
treats NULLs as distinct in a unique constraint. 38,542 instruments arrive from Kite's dump with no
series, so for every one of them the constraint enforced nothing: `refresh_instruments`'
`ON CONFLICT` never fired and each nightly run INSERTED another copy.

Measured on staging 2 Sep 2026 before this ran:

* 7,937 symbols had more than one live row, and **every one of them had `series IS NULL`** — not a
  single duplicate among the 3,191 rows that carry a series;
* `SGBDE31III-GB` had six rows, all sharing `kite_token` 5753857;
* the nightly fetched 10,145 rows for only 4,023 distinct Kite tokens, so 6,122 calls re-fetched a
  security it had already asked about;
* and the published day was inflated to match — 1 Sep held 8,537 bars for 3,998 real securities.

The last one is why this is a correctness fix and not a tidy-up: a screener ranking a universe
where 1,387 names appear up to five times is ranking a distorted universe, and the data-quality
gate read the inflation as health ("8537 bars against a 10-day median of 2993") because the
baseline had been inflated for three days too.

`NULLS NOT DISTINCT` is Postgres 15+ and the box runs 16, so the constraint can simply be made to
mean what it was always intended to mean.
"""

from __future__ import annotations

from alembic import op

revision = "0027_instrument_null_dedup"
down_revision = "0026_auth_identity"
branch_labels = None
depends_on = None

#: Every table with a foreign key to `instrument`, and the column that holds it. Enumerated from
#: `information_schema` rather than from memory: a table missed here would be orphaned by the
#: delete below, and `portfolio_holding` carries a real person's shares.
#:
#: The value is the OTHER columns of that table's narrowest unique key containing `instrument_id`,
#: read from `pg_index`. A duplicate's row can only be repointed at the survivor when the survivor
#: has nothing under the same key; where it already does, the two rows describe the same security
#: — same `kite_token` — and the duplicate is redundant. An empty tuple means the table is keyed by
#: `id` alone, so nothing can collide and every row simply moves.
REFERENCING: dict[str, tuple[str, tuple[str, ...]]] = {
    "cb_basket": ("benchmark_instrument_id", ()),
    "cb_constituent": ("instrument_id", ()),
    "cb_dividend": ("instrument_id", ()),
    "cb_investment_holding": ("instrument_id", ()),
    "corporate_action": ("instrument_id", ("action_type", "ex_date")),
    "factor_daily": ("instrument_id", ("date",)),
    "fundamental_daily": ("instrument_id", ("date",)),
    "index_member_daily": ("instrument_id", ("index_id", "date")),
    "ingest_cursor": ("instrument_id", ("kind", "window_start")),
    "ohlcv_daily": ("instrument_id", ("date",)),
    "portfolio_cash_flow": ("instrument_id", ()),
    # Two unique keys; the narrower one governs, so it is the one that decides a collision.
    "portfolio_holding": ("instrument_id", ("broker_account_id",)),
    "reconciliation_item": ("instrument_id", ()),
    "symbol_alias": ("instrument_id", ()),
}


def upgrade() -> None:
    # TimescaleDB caps how many compressed tuples one DML transaction may decompress
    # (`max_tuples_decompressed_per_dml_transaction`, default 100,000). Four of the tables below —
    # `ohlcv_daily`, `factor_daily`, `fundamental_daily`, `index_member_daily` — are hypertables,
    # and touching 12,396 duplicate bars means decompressing every chunk that holds one: 3,406,972
    # tuples on staging, which is what the third attempt died on.
    #
    # The row count actually changed is small. Measured before lifting this: of the 12,396 bars
    # pointing at a duplicate, **zero** need moving, because the survivor already holds a bar for
    # every one of those dates — they are the same security fetched twice a night. So this is the
    # cost of reaching the rows, not of rewriting history, and the limit is the wrong guard for a
    # one-off repair. Session-scoped: it ends with the migration.
    op.execute("SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0")

    # The survivor is the lowest id in each group: it is the row every earlier night already
    # pointed at, so keeping it moves the fewest references.
    op.execute(
        """
        CREATE TEMP TABLE instrument_dedup_map AS
        SELECT i.id AS drop_id, k.keep_id
        FROM instrument i
        JOIN (
            SELECT exchange_id, symbol, COALESCE(series, '') AS series_key, MIN(id) AS keep_id
            FROM instrument
            GROUP BY exchange_id, symbol, COALESCE(series, '')
            HAVING COUNT(*) > 1
        ) k
          ON k.exchange_id = i.exchange_id
         AND k.symbol = i.symbol
         AND k.series_key = COALESCE(i.series, '')
        WHERE i.id <> k.keep_id
        """
    )

    for table, (column, other_key) in REFERENCING.items():
        if other_key:
            match = " AND ".join(f"y.{c} IS NOT DISTINCT FROM s.{c}" for c in other_key)
            cols = ", ".join(f"s.{c}" for c in other_key)
            group = ", ".join(f"p.{c}" for c in other_key)
            join = " AND ".join(f"x.{c} IS NOT DISTINCT FROM p.{c}" for c in other_key)
            # One duplicate promoted per (survivor, key), chosen by lowest id.
            #
            # Several duplicates routinely map to one survivor for the same key — the six rows of
            # `SGBDE31III-GB` all carry 27 Aug — and a plain UPDATE passes the NOT EXISTS check for
            # every one of them, because the survivor genuinely has nothing there yet. They then
            # collide with each other inside the same statement, which is how the first attempt
            # died on `pk_fundamental_daily`, key (9199, 2026-08-27).
            #
            # Identified by (instrument_id, key) rather than by `ctid`: these are TimescaleDB
            # hypertables, and a system column is refused on a compressed chunk with "transparent
            # decompression only supports tableoid system column". That killed the second attempt.
            # The unique key is the honest identifier here anyway.
            op.execute(
                f"""
                UPDATE {table} x
                SET {column} = p.keep_id
                FROM (
                    SELECT m.keep_id, {cols}, MIN(s.{column}) AS chosen
                    FROM {table} s
                    JOIN instrument_dedup_map m ON s.{column} = m.drop_id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM {table} y
                        WHERE y.{column} = m.keep_id AND {match}
                    )
                    GROUP BY m.keep_id, {cols}
                ) p
                WHERE x.{column} = p.chosen AND {join}
                """
            )
            # Whatever could not move is a row the survivor already has under the same key.
            op.execute(
                f"DELETE FROM {table} x USING instrument_dedup_map m WHERE x.{column} = m.drop_id"
            )
        else:
            op.execute(
                f"""
                UPDATE {table} x
                SET {column} = m.keep_id
                FROM instrument_dedup_map m
                WHERE x.{column} = m.drop_id
                """
            )

    op.execute("DELETE FROM instrument i USING instrument_dedup_map m WHERE i.id = m.drop_id")
    op.execute("DROP TABLE instrument_dedup_map")

    # And make the constraint mean what it was written to mean, so the next night cannot refill it.
    op.execute("ALTER TABLE instrument DROP CONSTRAINT uq_instrument_exchange_id_symbol_series")
    op.execute(
        "ALTER TABLE instrument ADD CONSTRAINT uq_instrument_exchange_id_symbol_series "
        "UNIQUE NULLS NOT DISTINCT (exchange_id, symbol, series)"
    )


def downgrade() -> None:
    # The merged rows are not recoverable — they were duplicates of a survivor that remains, and
    # the pre-migration state is a dump taken beside this deployment, not something a downgrade
    # can reconstruct. Only the constraint is reversible.
    op.execute("ALTER TABLE instrument DROP CONSTRAINT uq_instrument_exchange_id_symbol_series")
    op.execute(
        "ALTER TABLE instrument ADD CONSTRAINT uq_instrument_exchange_id_symbol_series "
        "UNIQUE (exchange_id, symbol, series)"
    )
