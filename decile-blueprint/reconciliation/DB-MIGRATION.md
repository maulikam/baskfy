# SQLite → PostgreSQL: the migration, drilled

**22 Aug 2026.** `kite-momentum-rebalancer/scripts/migrate_to_postgres.py`, run against a **copy**
of `data/portfolio.db` into the `desk` schema of the local `baskfy` database.

## Result

| | |
|---|---|
| Tables | **19 / 19** |
| Rows | **42,285** |
| Row counts match | **every table** |
| Checksums match | **every table** |
| NAV series identical | **yes** (8 snapshots) |
| `index_value` recomputes from `nav` | **yes** |
| Mode | truncate-and-reload |
| Re-run produces an identical report | **yes** |

The unrebuildable set came through intact: 8,198 trades, 9,262 fills, 133 rebalance orders, 13
rebalance versions, 13 regime evaluations, 13 regime exposures, 8 EOD snapshots, 1 breadth reading.

## Three assertions, and why the third exists

1. **Row counts**, per table.
2. **Checksums**, per table — SHA-256 over every row, with values normalised by **one** function
   that both databases call. That matters: a float rendered differently by two drivers, or a NULL
   arriving as `0.0`, would otherwise read as corruption or hide it. Row digests are sorted before
   hashing, so the comparison does not depend on either database's idea of row order — which is not
   guaranteed and is not part of the data.
3. **The NAV series recomputes.** With no cashflows the desk's index is `100 × nav / nav[0]`, so
   `index_value` is re-derived from the migrated `nav` column rather than compared byte-for-byte.

**The third is not redundant, and a test proved it.** Changing one snapshot's `nav` by **one paisa**
in the source left counts and checksums matching — the corruption was faithfully copied — and only
the NAV recomputation caught it. The run refused to commit and rolled back with zero schemas left
behind. The checksum's own discrimination is shown separately: `NULL` vs `0.0` differs, and the same
rows reordered do not.

## Where it lands, and why not `public`

A dedicated **`desk` schema**. The desk's `trades`, `settings` and `fills` do not collide with the
screener's 42 tables *today*, and relying on that is how a collision arrives later — `docs/04` §4
renames these at P4 anyway. `desk.trades` also says where a row came from, and the rollback is
`DROP SCHEMA desk CASCADE`.

## Truncate-and-reload, not upsert

The SQLite file is the source of truth; the `desk` schema is a **projection**. `--drop-existing`
rebuilds it inside one transaction. There is no merge and no `ON CONFLICT`, because until M19 flips
the desk's backend only SQLite is ever written — so "changed on both sides" cannot happen. An
upsert would be machinery for a situation that does not exist, and it would hide the one that does:
a row deleted in SQLite would survive in Postgres forever.

Without the flag a populated schema is **refused** with an actionable message, not a
`DuplicateTable` traceback half-way through. Nothing is ever partly applied.

## ⚠ THE DIVERGENCE WINDOW — what today's data is, and is not

**The desk still writes SQLite. Nothing reads Postgres.** `app/analytics/db.py` opens `sqlite3` and
nothing under `app/` references Postgres at all.

So the `desk` schema in `baskfy` right now is **rehearsal output**: a faithful projection of the
database as it stood on the morning of 22 Aug 2026, and **already stale the moment the desk writes
another row**. It is not authoritative, nothing should read it as current, and it exists to prove
the migration works.

For the same reason **the cutover did not happen here, and not because a precondition failed** —
three of the four held (assertions green; fresh verified backup plus refreshed offsite copy; NSE
closed on a Saturday with the next session ~50 hours out). It did not happen because **there is
nothing to cut over to**: the switch is M19's deliverable.

`~/baskfy-safety/sqlite-archive/portfolio-2026-08-22-pre-postgres.db` (0444, 5,996,544 bytes,
integrity `ok`) is therefore a **rehearsal archive**, not the forever copy. It was taken from the
sqlite-backup-API output rather than a `cp` of a live WAL-mode file, which is the hazard
`deploy/README.md` warns about in as many words.

## The real cutover sequence — M19 owns it

1. **Stop every desk writer** — the web service and the daily/autorun timers.
2. **Re-run this migration**, `--drop-existing`, against the live file. Proven idempotent: two
   consecutive runs produce identical reports.
3. **Full assertions again** — counts, checksums, NAV recompute. Nothing commits unless all pass.
4. **Switch the desk to Postgres** (M19's backend work), and only then
5. **Archive that SQLite state read-only as the forever copy**, superseding the rehearsal archive.

Precondition (c)'s "one-command rollback" acquires its real meaning at step 4: today "point the
desk back at SQLite" is simply its current state, so it is not yet a rollback — it is the status
quo. Once a config flip exists, the rollback is flipping it back, and that is what must be
exercised.
