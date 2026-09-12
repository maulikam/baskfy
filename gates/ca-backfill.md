# Corporate-action backfill — 2026-08-01 .. 2026-09-30, on the box

**12 Sep 2026, Saturday ~12:15 IST.** `NEEDS-MAULIK.md` §CA set three conditions on this run:
*"a large mutation that wants its own gate file, its own run, and daylight — not a midnight
follow-on to an incident fix."* This is the gate file; it is its own run; and it is noon on a
Saturday with the market shut and no nightly due until 18:45. Maulik asked for it.

**The gap, as `gates/ca-truncation.md` G5 measured it:** NSE holds **606** actions with an ex-date
in the window and the box holds **147**. 87 were published for 2026-09-11 alone against our 22.
The cause is fixed and deployed (`a4d8871`, on the box since `8b074c7`) — the feed now asks for a
date range instead of taking NSE's 20-row default page. Shipping the fetcher re-fetches nothing,
which is why this run exists.

---

## The split this file is built around, and why it is not a technicality

**Storing an action and applying it to a price are two different acts, and only the first is
unambiguously right.**

`run_fetch_corporate_actions` upserts on `(instrument_id, action_type, ex_date)`, so capture is
idempotent and converges. It then *returns* the instruments it touched, and it is the
**orchestrator** — not the fetch — that hands those to `apply_adjustments`. Calling the fetch alone
writes no price.

That separation matters here because the missing actions are **mostly dividends**, and this repo
has a measured decision about dividends. `docs/DECISIONS-MERGE.md` **M27.1**: the corpus was asked
to discriminate between price return and total return and it answered **price 42, total 3** over 45
deciding rows; the 271-row cross-check found that *"applying the dividends on top pushes them below
even today's unadjusted baseline."* `baskfy_worker.action_recovery` already refuses to write cash
actions for exactly this reason.

Against that, `ohlcv_daily.close` today **is** dividend-adjusted for the 137 dividends the box
already holds — `docs/DECISIONS.md` §21.9 found eight export rows disagreeing "by exactly a
dividend". So the convention is not clean, and adding 460 more dividends is not obviously "more of
the same" nor obviously a reversal.

**So this run captures and measures, and does not apply.** A1–A5 below are the backfill. A6 is the
measurement of what applying *would* change, taken without applying it. Applying is a separate
decision with its own evidence, and it is Maulik's — reverting a 42–3 measurement by side effect
is exactly what the root `CLAUDE.md` rule about decisions and documents forbids.

---

- [x] A1: **Baseline recorded before anything is written**, so the change is attributable.
      ⚠️ **This row cannot be re-run to the same answer, and that is inherent rather than a fault.**
      Its CHECK reads the live count, so after the backfill it returns 581, not the 147 it returned
      before. A baseline is a point-in-time measurement; the evidence below is the record of it and
      the CHECK only proves the query still works. A3 is the row that actually asserts the change.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/box-sql.sh "select count(*) from corporate_action where ex_date between '2026-08-01' and '2026-09-30'" 2>&1 | tail -1
  EXPECT: /^[0-9]+$/m
  EVIDENCE: 147 actions in the window before the run (12 Sep 2026, ~12:15 IST), matching what `gates/ca-truncation.md` G8 recorded against the box an hour earlier.

- [x] A2: **The window is fetched through the existing rate-limited NSE provider**, not around it
      (root `CLAUDE.md`: "Network calls go through the existing rate-limited providers only").
  EVIDENCE: `ops/ca-backfill/fetch-window.py` calls `build_pipeline_dependencies()` and passes `deps.provider` to `run_fetch_corporate_actions` — the same composite provider the nightly step 3 uses, with NSE's cookie/header discipline and rate limiting intact. Nothing here talks to NSE directly. It ran inside the deployed `worker` container on the box, so the code that fetched is the code that ships.

- [x] A3: **The box holds materially more actions for the window than it did**, and the count is
      in the neighbourhood of what NSE reported to G5 rather than the 20-row default.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/box-sql.sh "select count(*) from corporate_action where ex_date between '2026-08-01' and '2026-09-30'" 2>&1 | tail -1
  EXPECT: /^([4-9][0-9][0-9]|[1-9][0-9]{3,})$/m
  EVIDENCE: **581** actions in the window, up from 147 — the feed returned `rows_in=594`, upserted `rows_out=578`, and touched 569 instruments. Against NSE's 606 (G5), the shortfall is 25 and is accounted for: 14 symbols NSE publishes are absent from `instrument` (ANANTAM, ANZEN, BAGMANE, BIRET, CUBEINVIT, EMBASSY, INDIGRID, INTERISE, MINDSPACE, NDRINVIT, NXST, OSEINTRUST, SHREMINVIT, VERTIS — REITs and InvITs), so their actions have nothing to hang on.

- [x] A4: **PGIL's bonus survives.**
      ⚠️ **This check was wrong on its first run and the data was right.** It asked for
      `action_type = 'BONUS'` and the column holds lowercase — the fetch's own output names the key
      `2395:bonus:2026-08-21`. It answered 0 and read as "the hand-repair was destroyed", which
      would have been the single worst outcome of this run. Fixed to compare case-insensitively. It was repaired by hand on 11 Sep and re-running the feed must
      converge onto it, not duplicate or displace it — that is what the upsert key is for.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/box-sql.sh "select count(*) from corporate_action ca join instrument i on i.id = ca.instrument_id where i.symbol = 'PGIL' and lower(ca.action_type) = 'bonus'" 2>&1 | tail -1
  EXPECT: /^1$/m
  EVIDENCE: `bonus | 2026-09-11 | 1.000000 | 1.000000` — PGIL's Bonus 1:1, intact. The re-fetch converged onto the row repaired by hand on 11 Sep rather than duplicating or displacing it, which is what the `(instrument_id, action_type, ex_date)` key is for. The fetch reported it as one of two `duplicate_keys` it collapsed.

- [x] A5: **No duplicate actions were created.** The upsert key is
      `(instrument_id, action_type, ex_date)`; house rule 7 says re-running converges.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/box-sql.sh "select count(*) from (select instrument_id, action_type, ex_date from corporate_action group by instrument_id, action_type, ex_date having count(*) > 1) d" 2>&1 | tail -1
  EXPECT: /^0$/m
  EVIDENCE: **0** duplicate `(instrument_id, action_type, ex_date)` triples across the whole table after the run. House rule 7 holds.

- [x] A6: **The price series is untouched by this run, and the size of what was NOT done is
      measured.** `ohlcv_daily` must be byte-identical where it matters, and the number of
      instruments that would be reprocessed is recorded so the apply decision has a figure.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/box-sql.sh "select count(distinct instrument_id) from corporate_action where ex_date between '2026-08-01' and '2026-09-30'" 2>&1 | tail -1
  EXPECT: /^[0-9]+$/m
  EVIDENCE: **569** distinct instruments carry an action in the window, and **not one of their price series was touched** — the script never calls `apply_adjustments` and prints `APPLIED_ADJUSTMENTS=0` to say so. The composition is the number the apply decision needs: **558 dividends**, 9 rights, 8 splits, 4 bonuses, 2 demergers. So an apply would be 558 cash adjustments against 23 share-count ones, on a product whose corpus voted price-return 42–3.

- [x] A7: **Nothing about execution or the market changed**, and the stack is still healthy after
      the run.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/verify-pc-deploy.sh 8b074c7 2>&1 | tail -1
  EXPECT: /running=10 .*twt_execution_true=0/
  EVIDENCE: pins=3 running=10 command_center=2 release=8b074c7 twt_execution_true=0 — all ten services still up on the deployed tag after the run, and no execution flag moved. The run wrote to one table and read from NSE; it touched no service.

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: A<n> <reason> is the honest exit. -->
