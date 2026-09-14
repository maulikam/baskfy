# Repair runbook — factor data, 1 Jul – 11 Sep 2026

**Written 14 Sep 2026 (NSE holiday), about 11:10 IST.** Nothing in this file has been run yet.
Every number below comes from read-only `SELECT`s against the production box at that time. The
code citations are to `developer` at `cc35c20`.

**Who runs this: an operator.** Steps 0–4 write to the production database. An agent does not run
them (root `CLAUDE.md`, "Safety rails"). No step places an order, touches `portfolio.db`, or calls
Kite or NSE.

**Read before starting:** root `CLAUDE.md` house rules 5 and 7, the section "Which date the product
shows", and `RUN-AND-TEST.md` §3e.

---

## 1. What is broken (measured, 14 Sep 11:10 IST)

There are 53 NSE trading days in 1 Jul – 11 Sep: 23 in July, 21 in August and 9 in September.
`trading_day` marks 14 Sep as a holiday.

### (a) `index_member_daily` is missing or partial

The reference day is 10 Sep, where the counts are `1:50 2:50 3:100 4:200 5:500 6:750 7:250 8:150
9:250 10:250 11:400 12:4247 13:210 14:350 15:472`.

| Dates | State |
|---|---|
| **No rows at all (22):** 07-02, 07-03, 07-07, 07-09, 07-10, 07-14, 07-16, 07-17, 07-21, 07-23, 07-24, 07-28, 07-30, 07-31, 08-04, 08-06, 08-07, **08-20, 08-21, 08-24, 08-25, 08-26** | Every universe is empty |
| **Partial (19):** 07-01, 07-06, 07-08, 07-13, 07-15, 07-20, 07-22, 07-27, 07-29, 08-03, 08-05, 08-10–08-14, 08-17, 08-18, 08-19 | Total-market 271/750, large-mid-250 92/250, microcap-250 87/250 and mid-small-400 146/400. `nifty-allcap` is **absent** on 07-01…08-05, **40** on 08-10…08-17, **2,540 of 2,967** on 08-18, and **271** on 08-19. `nse-sme-emerge` is absent on all 19 dates. These dates hold 1,979 rows each, all `derived`, carried back by `breadth_backfill`. The exception is 08-10…08-19, which mixes `nse_file` and `reconstructed` rows. |
| 08-27 – 09-11 | File universes 1–11 are complete. `nifty-fno` (13) and `etf` (14) are absent on **08-27 … 09-07**. |

### (b) Bars exist but `factor_daily` has no rows (16 dates)

07-02, 07-03, 07-07, 07-09, 07-10, 07-14, 07-16, 07-17, 07-21, 07-23, 07-24, **07-28, 07-30, 08-04,
08-06, 08-07**.

The five dates in bold were already known. The other eleven follow the same every-other-day
pattern: `factor_daily` has only 8–10 dates a month from 2025-06 onward.

The other 25 dates before 08-27 do have rows, but **fewer rows than bars**. For example, 08-20 has
2,541 factor rows against 2,990 bars. The quality gate's invariant is "factor rows == bars ±0",
and it holds on every date from 08-27 onward.

### (c) `universe_mask` disagrees with the membership rows

This is the same comparison `tasks/factors.py::assert_mask_matches_membership` makes.

| Date | Rows whose mask ≠ membership bitmask | Bits that differ |
|---|---|---|
| 07-06, 07-13, 07-20, 07-27, 08-03 | 583 / 583 / 584 / 584 / 583 | Every mask is 0 |
| 08-27, 08-31, 09-01 | 1,268 / 1,369 / 1,257 | 2048 (allcap) |
| 09-02, 09-03, 09-04, 09-07 | 22 / 32 / 21 / 21 | allcap, SME, microcap |
| 09-08 | 600 | 349 etf (8192), 210 fno (4096), 39 allcap and others |
| 09-09, 09-10 | 59 / 65 | allcap, SME |

Once step 1a lands, every date before 08-27 disagrees too, because it gains `nifty-allcap` rows.

### Knock-on effects

- **`mom_pctile`** only covers rows with `universe_mask <> 0`. Before 08-18 that is about 565 names
  a day, and on 08-20…08-26 it is **none**.
- **`rank_persist_20` is NULL on every date in the range (0 rows).** It needs a non-NULL
  `mom_pctile` on each of the last 20 `factor_daily` dates (`factors_ranking.rank_persistence`),
  and the gap on 08-20…08-26 breaks every window.
- **`desk_score_daily`** has 30 dates, 07-27…09-11, all at `desk-score-2026.09.13`. It has nothing
  before 07-27 and nothing on the 16 dates with no factor rows.
- **`market_health_daily`** has no rows on 07-31 or 08-20…08-26.

### What is *not* broken

- NIFTY 50 and NIFTY 500 levels (the engine's benchmarks) exist on all 53 dates.
- No `factor_daily`, `index_member_daily`, `desk_score_daily` or `market_health_daily` chunk in the
  range is compressed (Timescale 2.17.2; the only compression policy is on `ohlcv_daily`).
- No derived membership row sits on an instrument without a bar or on a non-EQ instrument.
- `fundamental_daily` exists everywhere `factor_daily.marketcap_cr` is non-NULL, so a recompute
  cannot lose a market cap. It will **gain** market caps on 08-21 and 08-24…08-26.

---

## 2. The CLIs, exactly as argparse defines them

| Tool | Syntax | Range? | Refuses a published day? | Writes |
|---|---|---|---|---|
| `baskfy_worker.reference_backfill` | `--from D [--to D] [--skip-listings] [--no-reconstruct] [--database-url U]` | Yes (trading days only) | No | Listings (unless skipped), then per day: membership, `index_snapshot_daily`, `market_health_daily` |
| `baskfy_worker.factors_cli recompute` | `recompute --date D` | **No, one date per call** | **No**. It overwrites whatever is there. Exit 1 only if it wrote 0 rows | Whole `factor_daily` rows: every engine column, the three masks, then `mom_pctile` and `rank_persist_20` for D. **Not** `desk_score_daily` |
| `baskfy_worker.factors_cli backfill-ranking` | `backfill-ranking --from D --to D [--resume \| --force] [--database-url U]` | Yes, oldest first, one commit per day | Skips *complete* days unless `--force`. `--resume` is the default behaviour and cannot be combined with `--force` | C1 columns (UPDATE only, never inserts), `mom_pctile`, `rank_persist_20`, `desk_score_daily`. **Never touches `universe_mask`**, and skips a day with 0 factor rows (`skipped_no_factor_rows`) |

Supporting facts from the code:

- **Recompute runs in one transaction.** It uses `run_in_session` → `session_scope`, which commits
  on a clean exit and rolls back on any exception or kill.
- **What "complete" means for `backfill-ranking`** (`ranking_backfill.day_is_complete`): desk rows
  at `DESK_SCORE_VERSION` equal the NSE factor rows, **and** no row with `universe_mask <> 0` and
  all four Sharpe components lacks `mom_pctile`.
- **The nightly skips holidays.** `tasks/celery_tasks.py:170` returns `skipped` when no date is
  given and `ops.is_trading_day` is false. `orchestrator.py:152` `require_trading_day` also aborts
  a non-trading date. **Today (14 Sep) the 18:45 chain does not run**, so the usual 18:40–21:15
  contention does not arise tonight.

---

## 3. Where membership comes from, and the look-ahead verdict (house rule 5)

`tasks/membership.py::_members_for` resolves each of the 15 universes (`baskfy_core/universes.py`)
in one of three ways:

| Bit (index_id) | Universe | Source | Point-in-time for a past date? |
|---|---|---|---|
| 1–11 | NIFTY 50 … MID SMALL 400 | `NSEProvider.index_constituents` → `nsearchives…/content/indices/ind_<x>list.csv` (`nse.py:258-281`) | **No.** The URL carries no date; NSE serves only today's constituents. `_archived(kind, on, url)` (`nse.py:654-676`) keys the archive by `on`, so an archived key is re-read, but a **missing key fetches today's file and files it under the past date**. `membership.py:179-181` then stamps it `nse_file`, which reads as "certain", and the upsert refuses to downgrade an `nse_file` row later (`membership.py:127-139`). |
| 12 | `nifty-allcap` | `DERIVED_BY_RULE`: every EQ instrument with a bar on that date (`membership.py:54, 240-262`) | **Yes.** It reads only `ohlcv_daily` for that exact date. |
| 15 | `nse-sme-emerge` | `DERIVED_BY_RULE`: allcap filtered to series SM/ST/SZ | **Yes** for the bars. The series is current-state, a minor caveat. |
| 13 | `nifty-fno` | Kite's *current* instrument dump (`kite.py:291`) | **No** |
| 14 | `etf` | `eq_etfseclist.csv`, archived under **`dt.date.today()`** rather than `on` (`nse.py:385-405`) | **No.** Always today's list. |

When no file comes back, `_reconstruct` (`membership.py:265-296`) copies the **earliest later**
`nse_file` day and labels it `reconstructed`. That is look-ahead by design; the label is there so a
backtest can exclude it.

### No point-in-time file exists for any date before about 27 Aug

- **The box archive was unwritable until 27 Aug.** The archive volume is `baskfy-archive`
  (`compose.prod.yml:32-39`, M56). It is a local volume, not S3: `s3://baskfy-archive` has no `nse/`
  prefix.
- **08-27's `nse_file` rows were written by runs 9–11 on 29–30 Aug** (`pipeline_run`).
- **08-18's rows are marked `pipeline_run` id 1, dated 21 Aug 18:37.**

So running `reference_backfill` for any date before 27 Aug today would write the **14 Sep**
constituents, stamped `nse_file`, onto August dates. **Rejected.**

### Does the look-ahead reach the masks? Only through bits 1–11, 13 and 14

- **`mom_pctile` ranks exactly the rows with `universe_mask <> 0`**
  (`factors_ranking.cross_sectional_pctile`).
- **On 08-28, 09-08 and 09-11, every row with a non-zero mask carries the allcap bit (0
  exceptions),** and every mask=0 row is an INDEX instrument (131 on 09-11).
- **So the ranked population is exactly `nifty-allcap`, which is point-in-time.** Membership
  look-ahead can therefore **not** move `mom_pctile`, `rank_persist_20` or ranks.

It **does** reach three things:

- `top_beta_mask` and `top_volatility_mask`, which are cut per universe (`tasks/factors.py::risk_masks`).
- Screener universe filters and `market_health_daily` for the 11 NSE-file universes.
- The desk score's `is_nifty_fno` component (`tasks/desk_score.py::load_carried`).

### What the stored rows show

- **Indices 1, 2, 3, 4, 5, 8 and 9 have never changed.** Their 08-18 membership (written 21 Aug)
  is **identical** to 08-19, to 08-27 (written 29–30 Aug) and to 09-11: 0 added, 0 dropped.
- **Indices 6, 7, 10 and 11 on 08-18 are strict subsets** of the 08-27 files. The missing names
  had no instrument id in the table at the time.
- **The union identities are exact.** `universes.UNION_IDENTITIES`
  (large-mid-250 = NIFTY 100 ∪ midcap-150; mid-small-400 = midcap-150 ∪ smallcap-250), applied to
  **08-18's** components, reproduce 08-27's observed files **exactly** (0 extra, 0 missing).

### Recommendation

1. **1a — derived universes only, for all 53 days.** Call `refresh_membership` with a provider
   object that has no file or list methods. `_from_named_method` and `_from_provider` then return
   `[]`, and with `allow_reconstruction=False` only `nifty-allcap` and `nse-sme-emerge` are
   written. No network, no look-ahead.
2. **1b — carry the earliest observation *forward*, never a later one *back*.**
   - For 08-20…08-26: copy 08-18's rows for indices 1–5, 8 and 9, labelled `reconstructed`.
   - Build 7 and 11 from those copied components by the union identities, also `reconstructed`.
   - On 08-18 and 08-19 themselves, complete 7 and 11 from the same date's components, labelled
     `derived`.
   - This reads only an earlier date. The one residual is that 08-18's file was fetched on about
     21 Aug, so 08-20 carries at most a one-session provenance lag. Every later observation agrees
     with it.
3. **Leave empty, on purpose:**
   - **Total-market (6) and microcap-250 (10) on 08-20…08-26.** No complete earlier source exists,
     and only a later file could fill them.
   - **File universes on the 17 July/August dates that have no rows.** Their only earlier source
     is `breadth_backfill`'s carried-back copy, which is already survivorship-biased
     (`NEEDS-MAULIK.md` item 12).
   - **`nifty-fno` and `etf` on 08-20…09-07.** The only source is today's list.
   - An empty universe on a past date is a visible gap. `membership.resolve_universe` chose that
     over a silently wrong one.

Record 1b as a `⚠ UNREVIEWED` entry in `docs/DECISIONS-MERGE.md` when it is run. It is reversible
by deleting the rows it inserted (§7).

---

## 4. Order, duration and memory

| # | Step | Dates | Estimate | Memory |
|---|---|---|---|---|
| 0 | Preflight + backup tables | — | 10–15 min | DB only (~110 k + 132 k + 93 k rows copied) |
| 1a | Derived membership | 53 days | 2–5 min | <500 MB |
| 1b | Carry-forward SQL | 08-18…08-26 | <1 min | DB only |
| 2 | `factors_cli recompute`, oldest first, one container per date | 53 days | **~6–9.5 h** | see below |
| 3 | `market_health_daily` | 36 days | ~10 min (≈11 s/day in the nightly) | <500 MB |
| 4 | `backfill-ranking --resume` 07-01…09-11, then `--force` 09-08 | 41 computed + 12 skipped + 1 forced | **~5–8 h** | 1.37 GB plateau, 2.67 GB peak (§3e) |
| 5 | Final verification | — | 15 min | — |
| | **Total** | | **≈ 12–18 h** | |

### Where the durations come from

- **Recompute** is the nightly's `compute_factors` step alone. `pipeline_run_step.duration_ms` for
  07–11 Sep is **569–641 s** on days with ~4,400 bars. There is no measurement for the 2,500–3,000
  bar days before 27 Aug; the lower bound assumes they are proportionally faster.
- **`backfill-ranking`** measured 682.5 s/day on 11 Sep (`RUN-AND-TEST.md` §3e). Its 41 computed
  days are 07-01…08-26, which are all incomplete after step 2 because desk rows ≠ the new factor
  row count, or there are no desk rows at all.

### Memory risk

- **Recompute has never been measured under a cap.** It runs `load_history` (3-year window,
  streamed, bounded since `f52df2c`) plus a ~70-column upsert of at most ~4,400 rows. The box-down
  incident of 14 Sep came from `load_desk_bars`, which recompute does not call. Expect a profile
  like `backfill-ranking` without the desk load: **~1.5–2.5 GB**.
- **The 3,500 MB cap turns a runaway into an OOM kill of one container.** The date's transaction
  rolls back and nothing is half-written. The loop then **stops** instead of skipping, because
  later dates' `rank_persist_20` depends on it.
- **Two capped jobs must never run at once.** 7.8 GB box, 11 services.

### Window

- **Today is ideal.** It is a holiday, with no market hours and no nightly chain. Started by about
  12:00 IST, the whole run should end around 04:00–06:00 on 15 Sep.
- **Hard stop: 09:15 IST, 15 Sep (market open).** If step 4 is still running at 08:45, stop it
  (§6). Resume after the 15 Sep nightly (after 21:15). It resumes cleanly because finished days
  become complete.
- **06:45 on 15 Sep is the `session-catch-up` beat.** With 14 Sep a holiday it should find nothing
  to run; check `docker stats` then anyway.
- **Do not deploy anything while this runs.** That includes the pending token-alert fix in
  `NEEDS-MAULIK` §35b.

---

## 5. Commands

All commands run from the repo root on the operator's machine. There is no SSH; everything goes
through SSM (`tools/deploy/box.sh`).

```bash
export AWS_PROFILE=baskfy-poc BASKFY_INSTANCE_ID=i-086986250704e4392
cd /Users/maulikdave/Documents/projects/baskfy
# push a local file to /opt/baskfy/repair/ on the box
push()    { bash tools/deploy/box.sh "mkdir -p /opt/baskfy/repair/logs && echo $(base64 < "$1" | tr -d '\n') | base64 -d > /opt/baskfy/repair/$(basename "$1")"; }
# run a local .sql file on the box's Postgres, stopping at the first error (WRITES — operator only)
boxpsql() { bash tools/deploy/box.sh "cd /opt/baskfy && echo $(base64 < "$1" | tr -d '\n') | base64 -d | docker compose -f compose.prod.yml --env-file .env.staging.compose exec -T postgres psql -U baskfy -d baskfy -v ON_ERROR_STOP=1 -f -"; }
# read-only checks
q()       { BOX_SQL_LINES=${2:-80} bash tools/deploy/box-sql.sh "$1"; }
```

Save each block below to a local scratch directory (for example `/tmp/repair/`) under the file
name given, then `push` it.

### 5.0 Preflight

```bash
# 1. nothing else heavy is running (expect: no ranking-backfill / recompute-* / ranking-repair containers)
bash tools/deploy/box.sh "docker ps -a --format '{{.Names}} {{.Status}}' | grep -E 'ranking|recompute|backfill' || echo none; free -m; df -h /var/lib/docker | tail -1"
# 2. no long query on the database (expect: 0 rows)
q "select pid, now()-query_start, left(query,60) from pg_stat_activity where datname='baskfy' and state<>'idle' and pid<>pg_backend_pid()"
# 3. running release contains f52df2c (bounded loads); expect a release at or after b87521d
bash tools/deploy/box.sh "cd /opt/baskfy && grep -E '^BASKFY_RELEASE|TAG' .env.staging.compose | head -3"
# 4. calendar: 14 Sep is a holiday, 15 Sep a session
q "select date, is_trading_day, source from trading_day where date in ('2026-09-14','2026-09-15')"
# 5. duplicates BEFORE (expect four zeros) — query V-DUP in §6
```

**`00-backup.sql`** holds the backups for rollback: it copies every affected row into a separate
schema. Check free disk first (step 1 above); this needs about 150 MB.

```sql
begin;
create schema if not exists repair_20260914;
create table repair_20260914.factor_daily        as select * from factor_daily        where date between '2026-07-01' and '2026-09-11';
create table repair_20260914.index_member_daily  as select * from index_member_daily  where date between '2026-07-01' and '2026-09-11';
create table repair_20260914.desk_score_daily    as select * from desk_score_daily    where date between '2026-07-01' and '2026-09-11';
create table repair_20260914.market_health_daily as select * from market_health_daily where date between '2026-07-01' and '2026-09-11';
commit;
select 'factor_daily', count(*) from repair_20260914.factor_daily
union all select 'index_member_daily', count(*) from repair_20260914.index_member_daily
union all select 'desk_score_daily', count(*) from repair_20260914.desk_score_daily
union all select 'market_health_daily', count(*) from repair_20260914.market_health_daily;
```

```bash
boxpsql /tmp/repair/00-backup.sql
```

Expect about **107,898 / 131,786 / 92,515 / 372**. The market-health figure is 31 dates × 12;
confirm it from the output. Rerun the counts if the preflight was more than an hour ago.

**`capped.sh`** is the only way this runbook starts a worker container. It enforces
`RUN-AND-TEST.md` §3e.

```bash
#!/usr/bin/env bash
# capped.sh NAME CMD... — one worker container: started, memory-capped, waited on, logged, removed.
set -uo pipefail
cd /opt/baskfy
NAME="$1"; shift
LOG="/opt/baskfy/repair/logs/${NAME}.log"
if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then echo "container $NAME already exists; inspect it" >&2; exit 2; fi
docker compose --env-file .env.staging.compose -f compose.prod.yml run -d --no-deps \
  -v /opt/baskfy/repair:/repair:ro --name "$NAME" worker "$@" >/dev/null || exit 3
docker update --memory 3500m --memory-swap 3500m "$NAME" >/dev/null || { docker rm -f "$NAME"; exit 4; }
CODE="$(docker wait "$NAME")"
docker logs "$NAME" >"$LOG" 2>&1
OOM="$(docker inspect -f '{{.State.OOMKilled}}' "$NAME")"
docker rm "$NAME" >/dev/null
echo "$(date '+%F %T') $NAME exit=$CODE oom=$OOM"
exit "$CODE"
```

```bash
push /tmp/repair/capped.sh
```

### 5.1a Derived membership: `nifty-allcap` and `nse-sme-emerge` only

**`membership_derived.py`**

```python
"""Step 1a: write only the rule-derived universes for each trading day. No network, no files.

The provider below has no index_constituents / etf_symbols / fno_underlyings, so
baskfy_worker.tasks.membership answers [] for every file- or list-sourced universe; with
allow_reconstruction=False nothing is carried from a later date. Only DERIVED_BY_RULE is written,
from ohlcv_daily on the exact date (house rule 5). Upserts on (index_id, date, instrument_id).
"""
import asyncio
import datetime as dt
import sys

from baskfy_worker.db import session_scope
from baskfy_worker.reference_backfill import trading_days_in
from baskfy_worker.tasks.membership import DERIVED_BY_RULE, refresh_membership
from baskfy_worker.window import DateWindow


class NoPublishedFiles:
    """A provider that publishes nothing."""


async def main(start: dt.date, end: dt.date) -> None:
    async with session_scope() as session:
        days = await trading_days_in(session, DateWindow(start, end))
    for day in days:
        async with session_scope() as session:
            result = await refresh_membership(
                session, NoPublishedFiles(), day, allow_reconstruction=False
            )
            written = {slug: n for slug, n in result.per_universe.items() if n}
            if set(written) - DERIVED_BY_RULE:  # raising here rolls this day back
                raise SystemExit(f"{day}: unexpected universes written {written}")
        print(day, written, result.per_source, result.failures or "", flush=True)


asyncio.run(main(dt.date.fromisoformat(sys.argv[1]), dt.date.fromisoformat(sys.argv[2])))
```

```bash
push /tmp/repair/membership_derived.py
bash tools/deploy/box.sh "bash /opt/baskfy/repair/capped.sh membership-derived python /repair/membership_derived.py 2026-07-01 2026-09-11; tail -60 /opt/baskfy/repair/logs/membership-derived.log"
```

Expect 53 lines. Each should be `{'nifty-allcap': N, 'nse-sme-emerge': M}`, with N equal to the
day's EQ bars (07-01 = 2,498 … 08-20 = 2,990 … 09-11 = 4,227) and exit 0.

**Verify V1a** (expect **0 rows**):

```bash
q "select o.date, count(*) filter (where i.instrument_type='EQ') eq_bars, (select count(*) from index_member_daily m where m.date=o.date and m.index_id=12) allcap, count(*) filter (where i.instrument_type='EQ' and i.series in ('SM','ST','SZ')) sme_bars, (select count(*) from index_member_daily m where m.date=o.date and m.index_id=15) sme from ohlcv_daily o join instrument i on i.id=o.instrument_id where o.date between '2026-07-01' and '2026-09-11' group by o.date having count(*) filter (where i.instrument_type='EQ') <> (select count(*) from index_member_daily m where m.date=o.date and m.index_id=12) or count(*) filter (where i.instrument_type='EQ' and i.series in ('SM','ST','SZ')) <> (select count(*) from index_member_daily m where m.date=o.date and m.index_id=15) order by 1"
```

Then run V-DUP (§6).

### 5.1b Carry forward: 08-18 → 08-20…08-26, and union completion on 08-18/08-19

**`01b-carry-forward.sql`**

```sql
begin;
-- Guard: the source day must still look exactly as measured on 14 Sep (else stop and re-plan).
do $$
declare bad int;
begin
  select count(*) into bad from (
    select index_id, count(*) c from index_member_daily
    where date = '2026-08-18' and index_id in (1,2,3,4,5,8,9) group by index_id
  ) x
  where (index_id, c) not in ((1,50),(2,50),(3,100),(4,200),(5,500),(8,150),(9,250));
  if bad > 0 then raise exception '08-18 source membership changed; stop'; end if;
end $$;

-- (i) 08-18 and 08-19: large-mid-250 (7) = NIFTY 100 (3) ∪ midcap-150 (8);
--     mid-small-400 (11) = midcap-150 (8) ∪ smallcap-250 (9). Same-date components -> 'derived'.
insert into index_member_daily (index_id, date, instrument_id, source)
select distinct u.target, m.date, m.instrument_id, 'derived'
from index_member_daily m
join (values (7,3),(7,8),(11,8),(11,9)) as u(target, component) on u.component = m.index_id
where m.date in ('2026-08-18','2026-08-19')
on conflict (index_id, date, instrument_id) do nothing;

-- (ii) 08-20..08-26: carry 08-18's complete files forward, labelled 'reconstructed'.
insert into index_member_daily (index_id, date, instrument_id, source)
select m.index_id, d.date, m.instrument_id, 'reconstructed'
from index_member_daily m
cross join (values (date '2026-08-20'),(date '2026-08-21'),(date '2026-08-24'),
                   (date '2026-08-25'),(date '2026-08-26')) as d(date)
where m.date = '2026-08-18' and m.index_id in (1,2,3,4,5,8,9)
on conflict (index_id, date, instrument_id) do nothing;

-- (iii) 08-20..08-26: 7 and 11 from the carried components, labelled 'reconstructed'.
insert into index_member_daily (index_id, date, instrument_id, source)
select distinct u.target, m.date, m.instrument_id, 'reconstructed'
from index_member_daily m
join (values (7,3),(7,8),(11,8),(11,9)) as u(target, component) on u.component = m.index_id
where m.date in ('2026-08-20','2026-08-21','2026-08-24','2026-08-25','2026-08-26')
on conflict (index_id, date, instrument_id) do nothing;
commit;
```

```bash
boxpsql /tmp/repair/01b-carry-forward.sql
```

**Verify V1b.**

```bash
q "select date, string_agg(index_id||':'||c, ' ' order by index_id) from (select date, index_id, count(*) c from index_member_daily where date between '2026-08-18' and '2026-08-26' and index_id<100 group by 1,2) x group by 1 order by 1"
```

Expected results:

- **08-18:** `1:50 2:50 3:100 4:200 5:500 6:271 7:250 8:150 9:250 10:87 11:400 12:2967 13:83 15:426`
- **08-19:** the same, except `12:2976 15:439`.
- **08-20, 08-21, 08-24, 08-25, 08-26:** `1:50 2:50 3:100 4:200 5:500 7:250 8:150 9:250 11:400`,
  plus 12 = EQ bars (2990 / 2996 / 3014 / 2982 / 2976) and 15 = SME bars (448 / 450 / 476 / 453 /
  450). There is no 6, 10, 13 or 14 on these dates, by decision.

Then run V-DUP.

### 5.2 Recompute `factor_daily`, oldest first, all 53 days

**`dates.txt`** has one date per line, in this exact order:

```
2026-07-01
2026-07-02
2026-07-03
2026-07-06
2026-07-07
2026-07-08
2026-07-09
2026-07-10
2026-07-13
2026-07-14
2026-07-15
2026-07-16
2026-07-17
2026-07-20
2026-07-21
2026-07-22
2026-07-23
2026-07-24
2026-07-27
2026-07-28
2026-07-29
2026-07-30
2026-07-31
2026-08-03
2026-08-04
2026-08-05
2026-08-06
2026-08-07
2026-08-10
2026-08-11
2026-08-12
2026-08-13
2026-08-14
2026-08-17
2026-08-18
2026-08-19
2026-08-20
2026-08-21
2026-08-24
2026-08-25
2026-08-26
2026-08-27
2026-08-28
2026-08-31
2026-09-01
2026-09-02
2026-09-03
2026-09-04
2026-09-07
2026-09-08
2026-09-09
2026-09-10
2026-09-11
```

### Why all 53 days, including the consistent 08-28 and 09-11

- **`rank_persist_20` is computed from stored earlier `mom_pctile`**, so every later date must be
  rewritten after the earlier ones change.
- **Walking every date in order makes recompute leave all rank columns final.** Step 4 then only
  has to add desk scores.

**`recompute.sh`**

```bash
#!/usr/bin/env bash
# Resumable: finished dates are listed in recompute.done. Touch /opt/baskfy/repair/STOP to halt
# cleanly between dates. A failed date STOPS the run — never skip it (later rank_persist_20 reads it).
set -uo pipefail
R=/opt/baskfy/repair
touch "$R/recompute.done"
while read -r D; do
  [ -z "$D" ] && continue
  grep -qx "$D" "$R/recompute.done" && continue
  [ -f "$R/STOP" ] && { echo "STOP file present before $D"; exit 0; }
  if bash "$R/capped.sh" "recompute-$D" python -m baskfy_worker.factors_cli recompute --date "$D"; then
    grep RECOMPUTED "$R/logs/recompute-$D.log"
    echo "$D" >> "$R/recompute.done"
  else
    echo "FAILED at $D — see $R/logs/recompute-$D.log; fix and rerun this script (it resumes here)"
    exit 1
  fi
done < "$R/dates.txt"
echo "RECOMPUTE ALL DONE $(date '+%F %T')"
```

```bash
push /tmp/repair/dates.txt; push /tmp/repair/recompute.sh
# try ONE date first, synchronously (§3e rule); box.sh waits up to 30 min
bash tools/deploy/box.sh "bash /opt/baskfy/repair/capped.sh recompute-2026-07-01 python -m baskfy_worker.factors_cli recompute --date 2026-07-01 && grep RECOMPUTED /opt/baskfy/repair/logs/recompute-2026-07-01.log && echo 2026-07-01 >> /opt/baskfy/repair/recompute.done"
```

Expect `RECOMPUTED date=2026-07-01 rows=2498` and `exit=0 oom=false`. Note the elapsed time and
multiply by 53. If `oom=true`, **stop**: do not raise the cap past 3,500 MB on this box. Re-plan
instead.

Then start the rest in the background:

```bash
bash tools/deploy/box.sh "setsid nohup bash /opt/baskfy/repair/recompute.sh >> /opt/baskfy/repair/logs/recompute.out 2>&1 < /dev/null & echo started"
# progress / memory, any time:
bash tools/deploy/box.sh "tail -5 /opt/baskfy/repair/logs/recompute.out; wc -l < /opt/baskfy/repair/recompute.done; docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' | grep recompute || true"
```

**Verify V2** after `RECOMPUTE ALL DONE`. Expect **0 rows**, meaning every date has factor rows ==
bars, no mask mismatch, and every rankable row has a `mom_pctile`.

```bash
q "with m as (select date, instrument_id, bit_or(1<<(index_id-1)) mask from index_member_daily where date between '2026-07-01' and '2026-09-11' and index_id<=15 group by 1,2), b as (select date, count(*) bars from ohlcv_daily where date between '2026-07-01' and '2026-09-11' group by 1), f as (select f.date, count(*) rows, count(*) filter (where f.universe_mask<>coalesce(m.mask,0)) mismatched, count(*) filter (where f.universe_mask<>0 and f.mom_pctile is null and f.sharpe_12m is not null and f.sharpe_6m is not null and f.sharpe_3m is not null and f.sharpe_1m is not null) unranked from factor_daily f left join m on m.date=f.date and m.instrument_id=f.instrument_id where f.date between '2026-07-01' and '2026-09-11' group by f.date) select b.date, b.bars, f.rows, f.mismatched, f.unranked from b left join f using (date) where f.rows is distinct from b.bars or f.mismatched<>0 or f.unranked<>0 order by 1"
```

The four Sharpe columns in `unranked` are `factors_ranking.MOM_PCTILE_COMPONENTS`, checked on
14 Sep.

**Verify V2b** (information, not pass/fail). `mom_pctile` should now reach about 2,000+ names a
day before 27 Aug, where it was about 565 or none. `rank_persist_20` should be non-NULL from about
07-28 onward.

```bash
q "select date, count(*) rows, count(*) filter (where universe_mask<>0) masked, count(mom_pctile) pctile, count(rank_persist_20) persist, count(marketcap_cr) mcap from factor_daily where date between '2026-07-01' and '2026-09-11' group by 1 order by 1"
```

Then run V-DUP, and also this check (expect 0):

```bash
q "select count(*) from factor_daily f where f.date between '2026-07-01' and '2026-09-11' and not exists (select 1 from ohlcv_daily o where o.instrument_id=f.instrument_id and o.date=f.date)"
```

### 5.3 Market health for the dates whose inputs changed (36 days)

This covers the 31 dates that already have rows, plus 08-20…08-26. The 17 no-membership dates are
deliberately left without rows; see §3.

**`market_health.py`**

```python
"""Step 3: re-run compute_market_health (DB only) for the dates given, oldest first."""
import asyncio
import datetime as dt
import sys

from baskfy_worker.db import session_scope
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.market_health import run_compute_market_health


async def main(days: list[dt.date]) -> None:
    for day in sorted(days):
        async with session_scope() as session:
            written = await run_compute_market_health(session, StepOutcome(), day)
        print(day, written, flush=True)


asyncio.run(main([dt.date.fromisoformat(arg) for arg in sys.argv[1:]]))
```

```bash
push /tmp/repair/market_health.py
bash tools/deploy/box.sh "bash /opt/baskfy/repair/capped.sh market-health python /repair/market_health.py 2026-07-01 2026-07-06 2026-07-08 2026-07-13 2026-07-15 2026-07-20 2026-07-22 2026-07-27 2026-07-29 2026-08-03 2026-08-05 2026-08-10 2026-08-11 2026-08-12 2026-08-13 2026-08-14 2026-08-17 2026-08-18 2026-08-19 2026-08-20 2026-08-21 2026-08-24 2026-08-25 2026-08-26 2026-08-27 2026-08-28 2026-08-31 2026-09-01 2026-09-02 2026-09-03 2026-09-04 2026-09-07 2026-09-08 2026-09-09 2026-09-10 2026-09-11; tail -40 /opt/baskfy/repair/logs/market-health.log"
```

**Verify V3.** Expect 36 rows of `12`; 08-20…08-26 show `constituent_count` 0 for total-market
(6) and microcap (10).

```bash
q "select date, count(*), string_agg(index_id||':'||constituent_count, ' ' order by index_id) from market_health_daily where date between '2026-07-01' and '2026-09-11' group by 1 order by 1"
```

### 5.4 Desk scores and final rank columns

Run this only after V2 passes.

```bash
bash tools/deploy/box.sh "setsid nohup bash /opt/baskfy/repair/capped.sh ranking-repair python -m baskfy_worker.factors_cli backfill-ranking --from 2026-07-01 --to 2026-09-11 --resume >> /opt/baskfy/repair/logs/ranking.out 2>&1 < /dev/null & echo started"
# progress (the container's own log; one line per day):
bash tools/deploy/box.sh "docker logs --tail 3 ranking-repair 2>&1; docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' | grep ranking-repair || true"
```

Expected progress:

- **07-01…08-26:** `computed`. That is 41 days, because desk rows ≠ the new factor rows or there
  are none.
- **08-27…09-11:** `skipped_complete`. That is 12 days, because the row counts are unchanged and
  step 2 filled every `mom_pctile`.
- **Final line:** `BACKFILL-RANKING days=53 computed=41 skipped_complete=12 … failed=0`, and
  `ranking.out` ends with `ranking-repair exit=0 oom=false`.

Then force the one complete-but-stale day. On 09-08 the recompute added the `nifty-fno` bit to 210
rows, and the desk score reads that bit.

```bash
bash tools/deploy/box.sh "bash /opt/baskfy/repair/capped.sh ranking-0908 python -m baskfy_worker.factors_cli backfill-ranking --from 2026-09-08 --to 2026-09-08 --force; tail -3 /opt/baskfy/repair/logs/ranking-0908.log"
```

**Check whether any other complete day needs `--force`** (expect **0 rows**). This compares the
desk score's carried inputs before and after, using the backup:

```bash
q "select f.date, count(*) from factor_daily f join repair_20260914.factor_daily b using (instrument_id, date) where f.date between '2026-08-27' and '2026-09-11' and f.date<>'2026-09-08' and (f.series is distinct from b.series or f.marketcap_cr is distinct from b.marketcap_cr or f.beta_12m is distinct from b.beta_12m or f.circuits_3m is distinct from b.circuits_3m or f.circuits_12m is distinct from b.circuits_12m or (f.universe_mask & 4096) <> (b.universe_mask & 4096)) group by 1 order by 1"
```

Any date listed there gets the same `--force` single-day run as 09-08.

**Verify V4.** This is the desk half of `day_is_complete` (`exchange_id=1` is `NSE_EXCHANGE_ID`); expect **0 rows**.

```bash
q "select f.date, count(*) nse_rows, (select count(*) from desk_score_daily d where d.date=f.date and d.score_version='desk-score-2026.09.13') desk from factor_daily f join instrument i on i.id=f.instrument_id where f.date between '2026-07-01' and '2026-09-11' and i.exchange_id=1 group by f.date having count(*) <> (select count(*) from desk_score_daily d where d.date=f.date and d.score_version='desk-score-2026.09.13') order by 1"
```

> The `score_version` literal is the value found on the box on 14 Sep. If a deploy has moved
> `DESK_SCORE_VERSION` since, read it first:
> `q "select distinct score_version from desk_score_daily where date='2026-09-11'"`.

---

## 6. Checks used throughout

**V-DUP** (expect four zeros). Run it before step 0 and after every step.

```bash
q "select 'factor_daily', count(*) from (select instrument_id, date from factor_daily where date between '2026-07-01' and '2026-09-11' group by 1,2 having count(*)>1) x union all select 'desk_score_daily', count(*) from (select instrument_id, date from desk_score_daily where date between '2026-07-01' and '2026-09-11' group by 1,2 having count(*)>1) x union all select 'index_member_daily', count(*) from (select index_id, date, instrument_id from index_member_daily where date between '2026-07-01' and '2026-09-11' group by 1,2,3 having count(*)>1) x union all select 'market_health_daily', count(*) from (select index_id, date from market_health_daily where date between '2026-07-01' and '2026-09-11' group by 1,2 having count(*)>1) x"
```

### Idempotence and duplicate safety, step by step

| Step | Write | Key | Re-run behaviour |
|---|---|---|---|
| 1a | `INSERT … ON CONFLICT (index_id, date, instrument_id) DO UPDATE SET source` where the existing row is not `nse_file` (`membership.py:127-139`) | PK `pk_index_member_daily` | Identical rows. It never deletes, and never downgrades `nse_file`. |
| 1b | `INSERT … ON CONFLICT DO NOTHING` | same PK | No-op on the second run |
| 2 | `INSERT … ON CONFLICT (instrument_id, date) DO UPDATE` of every column (`tasks/factors.py`), then UPDATE of `mom_pctile` and `rank_persist_20` | PK `pk_factor_daily` | Identical rows (house rule 7). It never deletes a factor row for an instrument that has lost its bar; the extra V2 check catches that. |
| 3 | `ON CONFLICT (index_id, date) DO UPDATE` | PK `pk_market_health_daily` | Identical |
| 4 | UPDATE of the C1 columns only; desk scores upserted on `(instrument_id, date)`, then stale ids for the date **deleted** (`tasks/desk_score.py`) | PK `pk_desk_score_daily` | Identical; one commit per day |

There are no foreign keys into any of the four tables.

### Stopping safely

- **Step 2:** `bash tools/deploy/box.sh "touch /opt/baskfy/repair/STOP"`. The script halts after
  the current date. To halt immediately, run `docker stop recompute-<date>`; that date rolls back
  and is not written to `recompute.done`. To resume, `rm` the STOP file and start `recompute.sh`
  again.
- **Step 4:** `docker stop ranking-repair`. The day in progress rolls back and finished days stay
  committed. To resume, run the same `--resume` command; finished days are now complete and are
  skipped.
  **Never resume step 4 with `--force` over the range.** That recomputes every finished day.

---

## 7. Rollback

The backup schema `repair_20260914` holds every affected row as it was before step 0. Rollback is
per table, in one transaction, and only while no newer nightly has written into 07-01…09-11. None
will, because the nightly only writes the current session.

```sql
begin;
delete from index_member_daily  where date between '2026-07-01' and '2026-09-11';
insert into index_member_daily  select * from repair_20260914.index_member_daily;
delete from factor_daily        where date between '2026-07-01' and '2026-09-11';
insert into factor_daily        select * from repair_20260914.factor_daily;
delete from desk_score_daily    where date between '2026-07-01' and '2026-09-11';
insert into desk_score_daily    select * from repair_20260914.desk_score_daily;
delete from market_health_daily where date between '2026-07-01' and '2026-09-11';
insert into market_health_daily select * from repair_20260914.market_health_daily;
commit;
```

`select *` relies on the column order being unchanged. **Do not roll back across a migration**
without naming the columns. You can also roll back a single step:

- **1b only:**
  ```sql
  delete from index_member_daily m
  where m.date between '2026-08-18' and '2026-08-26'
    and not exists (
      select 1 from repair_20260914.index_member_daily b
      where b.index_id = m.index_id and b.date = m.date and b.instrument_id = m.instrument_id
    )
    and m.index_id not in (12, 15);
  ```
  After that, re-run step 2 for 08-18…09-11 and step 4, because the masks read membership.
- **1a only:** the same delete for `index_id in (12,15)` over the whole range, followed by steps 2–4.
- **One date's factors:** delete that date from `factor_daily`, reinsert it from the backup, then
  run step 4 `--force` for that date and the 20 dates after it (rank persistence).

Drop the backup schema (`drop schema repair_20260914 cascade;`) **no earlier than one week** after
V1–V4 pass. Leave `/opt/baskfy/repair/logs` in place as the evidence.

---

## 8. What stays wrong after this runbook, stated so nobody mistakes it for fixed

- **File universes 1–11 before 18 Aug.** They are still `breadth_backfill`'s carried-back and
  partial rows: total-market 271/750, large-mid 92/250, microcap 87/250, mid-small 146/400. The 17 dates
  with no rows keep **no** file-universe membership. No point-in-time source exists
  (`NEEDS-MAULIK.md` item 12).
- **Total-market (6) and microcap-250 (10) on 08-20…08-26.** Empty.
- **`nifty-fno` (13) and `etf` (14) on 08-20…09-07.** Absent, so the desk's `is_nifty_fno` is 0 on
  those dates. The only source is today's list.
- **`factor_daily` before 1 Jul 2026.** It is still sampled at about 8–10 dates a month, back to
  2025-06 and beyond. `rank_persist_20` is NULL on 07-01…about 07-27 because its 20-date window
  reaches into those sampled dates.
- **Past dates' values change.** Saved screens replayed on 07-01…09-11 will return different
  results. The masks, the `mom_pctile` population and the new rows are the intended change.

---

## Execution log — 14 Sep 2026 (agent, on Maulik's go-ahead relayed by the main session)

Split agreed: §5.0–§5.3 on 14 Sep with a hard stop at 08:45 IST on 15 Sep; §5.4 only after the
15 Sep nightly. Release on the box: images `baskfy-py/web/desk:962b981` (contains `f52df2c` and
`b87521d`). **Run halted at V1a. §5.1b, §5.2 and §5.3 have NOT run. No recompute container, no
background job and no STOP timer exist.**

| IST | Step | Result |
|---|---|---|
| 11:35 | §5.0 preflight 1 | No running ranking/recompute/backfill container. One **exited** container, `ranking-backfill-r2` (image `b87521d`, `backfill-ranking --from 2026-07-27 --to 2026-09-11 --force`, exit 0 at 09:20 IST), left in place. `free -m` available 4,981 MB; `/` 16 GB free (85% used) |
| 11:35 | §5.0 preflight 2 | 0 non-idle queries |
| 11:35 | §5.0 preflight 3 | The runbook's grep (`^BASKFY_RELEASE\|TAG`) matches nothing: the env file names `BASKFY_*_IMAGE`. Release confirmed from those tags instead (962b981) |
| 11:35 | §5.0 preflight 4 | 09-14 `f holiday`, 09-15 `t derived` |
| 11:35 | V-DUP before | 0 / 0 / 0 / 0 |
| 11:36 | CSV snapshot | `/opt/baskfy/repair/backup/index_member_daily-20260914.csv` 131,786 rows, 131,787 lines, 3,652,592 B; `factor_daily-20260914.csv` 107,898 rows, 107,899 lines, 50,060,087 B; `market_health_daily-20260914.csv` 372 rows, 373 lines, 20,942 B. Every line count is rows + 1 header. Checksums are in `backup/SHA256SUMS` |
| 11:37 | `00-backup.sql` | Schema `repair_20260914`: 107,898 / 131,786 / 92,515 / 372, as expected |
| 11:37–11:38 | §5.1a | `membership-derived exit=0 oom=false`, 53 lines, only allcap and SME written (07-01 = 2,498, 08-20 = 2,990, 09-11 = 4,227) |
| 11:39 | V-DUP after 1a | 0 / 0 / 0 / 0. `index_member_daily` in range is now 254,840 rows |
| 11:39 | **V1a** | **11 rows, expected 0.** Days 08-27 through 09-10 each have SME (15) membership equal to SME bars + 1. The extra row is `DOLLEX` (instrument 66061), a `derived` row already in the backup, written by the nightly on those dates. The instrument's `series` is now `EQ`, so today's rule no longer selects it, and the upsert never deletes. This is §3's "series is current-state" caveat surfacing. 1a did not cause it. **Stopped here for a decision.** |

Options for the decision (none taken): (a) accept the 11 rows as point-in-time correct, since
DOLLEX was SME on those dates; (b) delete them so SME exactly matches today's series rule, then
re-run V1a. Either way, V2 compares masks with membership, so it stays consistent.

### Continued — state re-read from the box at 17:38 IST (the log above stopped at 11:39)

- **V1a decision: (a).** DOLLEX's 11 `derived` SME rows on 08-27…09-10 are still present, so they
  were kept as point-in-time correct. **§5.1b ran:** 08-20…08-26 carry indices 7 and 11 as
  `reconstructed` (250 / 400); 08-18 and 08-19 have 7 and 11 completed as `derived`.
- **§5.2 is running** under `recompute.sh` (pid started 12:19 IST), one capped container per date.
  07-01 finished 11:52 (the synchronous trial; peak 2,058 MB under the 3,500 MB cap, box available
  4,488 MB). 07-02…08-17 then landed at a steady **~9.5 min/day**; 34 of 53 dates were in
  `recompute.done` and `recompute-2026-08-18` was in progress. No OOM in any log. Expected
  `RECOMPUTE ALL DONE` about 20:40 IST.
- **Deploys during the run, against §4 "Window":** another session rolled `baskfy-py`/`web` to
  `27eb1fb` (api up since about 15:40) and `baskfy-desk` to `07ba71c` (about 17:33). The recompute
  containers run image `27eb1fb`, which contains `f52df2c` and `b87521d`. No date failed across the
  image change. Box memory at 17:38: 3,454 MB available.
- **19:0x–20:34, no watch:** the operator machine's AWS SSO token expired. The loop on the box did
  not depend on it and kept running.

| IST | Step | Result |
|---|---|---|
| 20:34 | §5.2 done | `RECOMPUTE ALL DONE 2026-09-14 20:34:00`; 53 / 53 in `recompute.done`, every container `exit=0 oom=false`. 09-11 wrote 4,358 rows |
| 20:35 | **V2** | **0 rows.** Every date has factor rows == bars, no mask mismatch, no unranked row |
| 20:35 | Orphan check | 0 factor rows without a bar |
| 20:35 | V-DUP after §5.2 | 0 / 0 / 0 / 0 |
| 20:35 | V2b | `mom_pctile` 2,004–2,258 names a day (was ~565 or none before 27 Aug). `rank_persist_20` non-NULL from **07-28** (1,894) onward, NULL on 07-01…07-27 as §8 predicts |
| 20:35 | Market caps vs backup | No date lost one. 14 dates gained: 08-24/25/26 from 0 to 2,515 / 2,507 / 2,356; 08-18 846 → 2,089; 08-21 605 → 2,526; 08-27 and 09-01…09-10 up by 21–371. Every after-count equals that date's `fundamental_daily` rows. Dates still at 0 (07-01…08-17, 08-19, 08-20, 08-31) were 0 in the backup too |
| 20:36 | §5.3 | `market-health exit=0 oom=false`, 13 s, 36 dates × 12 rows |
| 20:36 | **V3** | 36 dates, 12 rows each. 08-20…08-26 carry `6:0` and `10:0`, as intended. 08-18/08-19 now carry 7:250 and 11:400 |
| 20:36 | V-DUP after §5.3 | 0 / 0 / 0 / 0 |

**§5.0–§5.3 are complete. §5.4 has NOT run** — per the agreed split it waits for the 15 Sep
nightly to finish (after 21:15 on 15 Sep), and V4 follows it.

Noticed, not caused by this run and not fixed by it: `ohlcv_daily` holds only ~3,000 bars on 08-28
and 09-03 against ~4,300 on their neighbours, so `nifty-allcap` (a bars-derived universe) is 3,002
and 3,027 on those dates. The bhavcopy-only instruments look absent from those two days' bars.

**Deploy after §5.3, outside the nightly window:** images `836362c` pushed at 20:38; the box deploy
waited for 21:15 (the guard was not overridden, though 14 Sep has no nightly) and ran at 21:16.
`verify-pc-deploy`: `pins=3 running=11 release=836362c twt_execution_true=0`. `verify-swing` failed
its first pass while services were still starting (HTTP 000) and passed the rerun at 21:23 (`SWING
OK`). No repair container was running at deploy time. Nothing in this release touches the repair's
tables.
