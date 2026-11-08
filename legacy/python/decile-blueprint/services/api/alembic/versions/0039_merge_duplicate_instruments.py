"""One symbol on one exchange is one instrument, even when NSE changes its series.

Maulik, 11 Sep 2026: *"when I synced holdings it says '18 holdings synced. 1 symbol(s) not
recognised: GAYAPROJ.' fix it"*.

GAYAPROJ was not missing. It was there **twice** — id 1088 with `series='BE'` and id 83745 with
`series='EQ'`, same ISIN, same Kite token, both `is_active`. `portfolios.resolve_symbols` answers
AMBIGUOUS for a symbol with two active candidates, an ambiguous resolution has no
`instrument_id`, and the sync reports that as "not recognised". One of eighteen holdings was
silently dropped from his portfolio.

**120 symbols were in this state**, all of them the same shape: one ISIN, two rows, differing
only in `series` — EQ/BE where NSE moved a stock in or out of trade-to-trade surveillance, ST/SM
in the SME segment. Any of the other 119 would have failed the same way the moment he held one.

WHY IT HAPPENED
---------------
`refresh_instruments` upserts on `(exchange_id, symbol, series)` and does not list `series` among
its mutable columns. So `series` is part of the instrument's IDENTITY, when it is really an
attribute that changes over a listing's life. When NSE moves GAYAPROJ from BE to EQ:

* the conflict key `(NSE, GAYAPROJ, 'EQ')` matches nothing, so a NEW row is inserted;
* the old BE row is no longer in the dump, so nothing updates it — and `is_active` stays true
  forever, because deactivation only ever happens by being *present and changed*, never by being
  absent.

The module's own docstring says the two sources are "merged onto `instrument`, keyed by symbol".
That was the intent; the index disagreed with it. This migration and the change to
`tasks/instruments.py` make the code match the sentence.

WHAT THIS DOES
--------------
Merges each duplicate group onto its **lowest id** — the oldest row, which carries the longest
price history and the most references — then makes `(exchange_id, symbol)` unique so the state
cannot recur.

The keeper inherits the *current* series (the highest id's, i.e. the most recently observed), and
any `kite_token` / `isin` / `listed_on` it was missing. Every foreign key pointing at a loser is
repointed; rows that would collide with one the keeper already has are dropped, because they are
the same fact recorded twice — a bar for the same instrument on the same date is one bar.

**It refuses to run if any group has more than one ISIN.** On this database none does (checked:
zero), and a group that did would not be a duplicate at all — it would be two different companies
sharing a ticker, and merging them would corrupt both. That is a stop, not a warning.

The repointing walks `pg_constraint` rather than naming tables, so a table added later is covered
without anyone remembering to edit this file — and TimescaleDB's chunk-level foreign keys are
skipped, because updating the parent hypertable moves its chunks with it.

It repoints **one pair at a time with literal ids**, which looks needlessly slow and is not.
`ohlcv_daily` is compressed and segmented by `instrument_id`; a statement joining to a temp table
cannot be pruned to a segment, so the first version decompressed 3.4 million tuples to move 800
rows and hit TimescaleDB's limit mid-deploy. A literal id prunes to that instrument. See the
comment on the loop.

Revision ID: 0039_merge_duplicate_instruments
Revises: 0038_broker_pile_flag
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_merge_duplicate_instruments"
down_revision: str | None = "0038_broker_pile_flag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_UNIQUE = "uq_instrument_exchange_id_symbol_series"
_NEW_UNIQUE = "uq_instrument_exchange_id_symbol"

#: Repoint every reference from a loser to its keeper, dropping rows that would collide.
#:
#: Written as PL/pgSQL over `pg_constraint` rather than as twenty hand-written statements: the
#: list of tables referencing `instrument` is twenty-odd today and grows, and a merge that misses
#: one leaves an orphaned row that the DELETE at the end then fails on — loudly, but halfway
#: through. Introspection cannot miss one.
_MERGE = """
DO $$
DECLARE
  fk       record;
  pair     record;
  key_cols text[];
  join_on  text;
  moved    bigint;
  rows_now bigint;
BEGIN
  CREATE TEMP TABLE dupmap ON COMMIT DROP AS
    SELECT i.id AS loser, k.keep
    FROM instrument i
    JOIN (
      SELECT exchange_id, symbol, min(id) AS keep
      FROM instrument GROUP BY exchange_id, symbol HAVING count(*) > 1
    ) k ON k.exchange_id = i.exchange_id AND k.symbol = i.symbol
    WHERE i.id <> k.keep;

  IF NOT EXISTS (SELECT 1 FROM dupmap) THEN
    RAISE NOTICE 'no duplicate instruments; nothing to merge';
    RETURN;
  END IF;

  -- Captured now, because the rows these come from are deleted before they are applied.
  CREATE TEMP TABLE keepinfo ON COMMIT DROP AS
    SELECT DISTINCT ON (d.keep)
           d.keep, i.series, i.kite_token, i.isin, i.listed_on, i.is_active
    FROM dupmap d JOIN instrument i ON i.id = d.loser
    ORDER BY d.keep, i.id DESC;

  -- A group spanning two ISINs is two companies sharing a ticker, not a duplicate. Merging them
  -- would corrupt both price histories, so this stops rather than guessing which one is meant.
  IF EXISTS (
    SELECT 1 FROM instrument
    GROUP BY exchange_id, symbol
    HAVING count(*) > 1 AND count(DISTINCT isin) > 1
  ) THEN
    RAISE EXCEPTION
      'refusing to merge: some duplicate symbols carry more than one ISIN, which means two '
      'different companies share a ticker. Resolve those by hand first.';
  END IF;

  FOR fk IN
    SELECT c.conrelid::regclass::text AS tbl, a.attname AS col
    FROM pg_constraint c
    JOIN unnest(c.conkey) k ON true
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k
    WHERE c.confrelid = 'instrument'::regclass
      AND c.contype = 'f'
      AND c.connamespace = 'public'::regnamespace
    GROUP BY 1, 2
    ORDER BY 1, 2
  LOOP
    -- The other columns of whichever unique key covers this reference, so a row the keeper
    -- already has can be recognised before the UPDATE would collide on it.
    SELECT array_agg(att.attname ORDER BY att.attnum) INTO key_cols
    FROM pg_constraint u
    JOIN unnest(u.conkey) uk ON true
    JOIN pg_attribute att ON att.attrelid = u.conrelid AND att.attnum = uk
    WHERE u.conrelid = fk.tbl::regclass
      AND u.contype IN ('p', 'u')
      AND fk.col = ANY (SELECT a2.attname FROM unnest(u.conkey) k2
                        JOIN pg_attribute a2 ON a2.attrelid = u.conrelid AND a2.attnum = k2)
      AND att.attname <> fk.col;

    IF key_cols IS NOT NULL THEN
      SELECT string_agg(format('k.%I IS NOT DISTINCT FROM l.%I', c, c), ' AND ')
        INTO join_on FROM unnest(key_cols) AS c;
    ELSE
      join_on := NULL;
    END IF;

    -- ONE PAIR AT A TIME, WITH LITERAL IDS, AND THAT IS NOT FUSSINESS.
    --
    -- `ohlcv_daily` is a compressed hypertable segmented BY `instrument_id`. A statement that
    -- joins to a temp table cannot be pruned to a segment, so TimescaleDB decompresses every
    -- chunk that might match — on the live box that was 3,406,169 tuples against a limit of
    -- 100,000, and the deploy failed there:
    --
    --     ConfigurationLimitExceededError: tuple decompression limit exceeded by operation
    --
    -- to move 800 rows. A literal `instrument_id = 83745` prunes to that instrument's segments,
    -- which is a few hundred tuples. The limit is left alone deliberately: raising it would let
    -- this decompress the whole history to do a tiny job, and the guard is right to object.
    --
    -- 120 pairs across ~20 tables is a few thousand small statements. That is cheap, and it is
    -- the difference between a migration that runs and one that cannot.
    moved := 0;
    FOR pair IN SELECT loser, keep FROM dupmap LOOP
      IF join_on IS NOT NULL THEN
        EXECUTE format(
          'DELETE FROM %s l WHERE l.%I = %s AND EXISTS '
          '(SELECT 1 FROM %s k WHERE k.%I = %s AND %s)',
          fk.tbl, fk.col, pair.loser, fk.tbl, fk.col, pair.keep, join_on
        );
      END IF;
      EXECUTE format(
        'UPDATE %s SET %I = %s WHERE %I = %s',
        fk.tbl, fk.col, pair.keep, fk.col, pair.loser
      );
      GET DIAGNOSTICS rows_now = ROW_COUNT;
      moved := moved + rows_now;
    END LOOP;
    IF moved > 0 THEN
      RAISE NOTICE 'repointed % row(s) in %.%', moved, fk.tbl, fk.col;
    END IF;
  END LOOP;

  -- DELETE BEFORE UPDATE, and this order is load-bearing. The keeper takes the CURRENT series —
  -- the most recently inserted row's, since that is what the last dump produced — and while the
  -- loser still exists it *holds* that series, so setting it on the keeper collides on the old
  -- `(exchange_id, symbol, series)` key. Proved by running it: the first version of this
  -- migration failed exactly there.
  --
  -- Which means the values have to be captured before the rows carrying them are gone.
  DELETE FROM instrument WHERE id IN (SELECT loser FROM dupmap);

  UPDATE instrument keep SET
    series      = newest.series,
    kite_token  = coalesce(keep.kite_token, newest.kite_token),
    isin        = coalesce(keep.isin, newest.isin),
    listed_on   = coalesce(keep.listed_on, newest.listed_on),
    is_active   = keep.is_active OR newest.is_active,
    updated_at  = now()
  FROM keepinfo newest
  WHERE keep.id = newest.keep;
END $$;
"""


def upgrade() -> None:
    op.execute(sa.text(_MERGE))
    op.drop_constraint(_OLD_UNIQUE, "instrument", type_="unique")
    op.create_unique_constraint(_NEW_UNIQUE, "instrument", ["exchange_id", "symbol"])


def downgrade() -> None:
    """Restores the old key. The merged rows are NOT recreated, and cannot be.

    A merge is not reversible: once two histories are one, nothing records which bar came from
    which row, and inventing a split would be worse than leaving it merged. What the downgrade
    restores is the *constraint*, so a database rolled back to 0038 accepts the old shape again —
    and will, in time, grow the same duplicates.

    Widening a unique key never fails on existing data: every row that satisfied
    `(exchange_id, symbol)` satisfies `(exchange_id, symbol, series)` as well.
    """
    op.drop_constraint(_NEW_UNIQUE, "instrument", type_="unique")
    op.create_unique_constraint(_OLD_UNIQUE, "instrument", ["exchange_id", "symbol", "series"])
