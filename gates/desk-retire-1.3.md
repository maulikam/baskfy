# Gates: BRANCH 1.3 Data custody — integration

- [x] B1: All three children verified by the driver re-running their checks
  CHECK: for f in gates/desk-retire-1.3.1.md gates/desk-retire-1.3.2.md gates/desk-retire-1.3.3.md; do grep -c "^- \[ \]" $f; done | paste -sd+ | bc
  EXPECT: 0
  EVIDENCE: driver-verified 2026-08-31. 1.3.1 5/5, 1.3.2 6/6, 1.3.3 5/5, 0 unchecked.
    Driver re-ran 1.3.3's independent script: "19 tables checked, 43411 source rows, 43411
    target rows, 19 matched on BOTH row_count and digest / ALL TABLES MATCH", exit 0. Driver
    also ran 1.3.2's own suite: 37 passed, 21 skipped.

- [ ] B2: `trades` and `fills` exist in Baskfy's Postgres with counts matching the source
  EVIDENCE: **UNMET, and this is the branch's real finding.** The migration is a REHEARSAL, not a
    cutover. It landed in the LOCAL dev Postgres (`baskfy-postgres`, localhost:5433, schema
    `desk_migrated`), which is where 43,411/43,411 was proven. On the DEPLOYED box the query
    `select count(*) from information_schema.tables where table_name in ('trades','fills')`
    returns **0** — measured by the driver 2026-08-31. Three children each fully met and the
    record is still not in Baskfy: exactly the compose failure a branch gate exists to catch.
    1.3.2 said so itself ("`desk_migrated` is a full-scale rehearsal, not the cutover") and
    named the blocker: there is no path between the trading box and the Baskfy box, so any
    cutover today is laptop-mediated. Clearing that is Maulik's, not a leaf's.

- [x] B3: The forever archive exists outside the repo and its checksum is recorded
  EVIDENCE: `~/baskfy-safety/desk-migration-2026-08-31/portfolio-A-2026-08-31T1731-IST.db`,
    mode `-r--r--r--` (0444), 6,295,552 bytes — driver-listed. sha256
    `22a38d53d3a0ba9011851b6a3bb3a0d54874b099a41f609012c37ce06f283d1b`, which 1.3.3 re-read from
    the LIVE box today and found byte-identical to the sealed copy.
    CAVEAT recorded rather than waived: 1.3.3 found stale `-wal` (0 B) and `-shm` (32 KB)
    sidecars in that directory, created 17:42 — the archive was opened at least once without
    `immutable=1`. No data risk (WAL empty, .db sha unchanged) but a stale `-shm` travelling
    with a forever archive is a trap and should be removed.

- [x] B4: The desk's original datastore still exists and still works
  EVIDENCE: driver-verified over ssh 2026-08-31: `systemctl is-active momentum-web` -> `active`;
    `portfolio.db` 6,295,552 bytes; sha256 `22a38d53...f283d1b` unchanged before, during and
    after all three leaves. 1.3.3 confirmed no `-wal`/`-shm` ever appeared on the box and all
    three systemd timers remain active. Nothing wrote to the desk.
