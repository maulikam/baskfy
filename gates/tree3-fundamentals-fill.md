# Gates: Tree 3 — Fundamentals live fill (NEEDS-MAULIK §15)

Scope: `fundamental_daily` stops being empty on live data. Market cap and P/E come from
NSE `quote-equity`, land in Postgres for the latest published date, flow through the
`compute_factors` join into decile bucketing, and render as real numbers instead of em
dashes on the surfaces that read them.

Filed under `gates/` and not `GATES.md` deliberately: another session holds `GATES.md`
for its own tree (a `/home` dashboard). Run the checker with this file named explicitly:

```
node ~/.claude/skills/unlazy/scripts/gate-check.mjs gates/tree3-fundamentals-fill.md
```

Facts measured before these gates were written, not assumed:

- `fundamental_daily` count: **0**. `instrument` count: **10,481**. `max(ohlcv_daily.date)`:
  **2026-08-21**. (`docker exec baskfy-postgres psql -U baskfy -d baskfy`.)
- The parser (`parse_quote_equity`, `nse.py:677`), the provider method
  (`NseProvider.equity_fundamentals`, `nse.py:321`), the upsert
  (`store_fundamentals`, `tasks/fundamentals.py`) and the orchestrator wiring
  (`orchestrator.py:225`, inside step 6) all exist. TRUE — the brief is right that the
  parser and the join are written.
- There is **no** standalone one-off fill command. The only way to reach
  `run_fetch_fundamentals` today is a full pipeline night. NEEDS-MAULIK §15's "or a
  one-off `equity_fundamentals` fetch" names something that does not exist.
- `nse_rate_limit_per_second` = **1.0**, so ~2,500 symbols is ~45 minutes, not a night.
- `www.nseindia.com/api/quote-equity` returns **403 from AkamaiGHost** from this box,
  as does the site root; `nsearchives.nseindia.com` and `api/marketStatus` return 200.
  The endpoint is **retired**, not blocked — NSE's Next.js quote page calls
  `/api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolData` instead, and that returns
  200 with no cookies. So the parser was pointed at a dead route and a dead payload shape;
  a night against NSE would never have filled this table.
- **Two dates matter, and they are not the same date.** The API resolves every as-of through
  `latest_published_date` — `max(pipeline_run.trade_date)` where `data_version IS NOT NULL` —
  which is **2026-08-18**. `max(ohlcv_daily.date)` is **2026-08-21**. Filling only the newer
  one would leave every rendered surface on an em dash, so both are filled: 08-18 is what the
  product serves today, 08-21 is what the next nightly publish will serve.
- Recomputing `factor_daily` for 2026-08-18 is **safe for parity**: `marketcap_cr` is in
  `reconcile_cli.NOT_RECOMPUTED`, and reconciliation diffs the committed CSV fixture
  (`reference-screen-export-2026-08-18.csv`), never `factor_daily`. The 271 pre-existing
  non-null caps on that date came from `seed_published_run`, not from the answer key.

Working root for every CHECK: `/Users/maulikdave/Documents/projects/baskfy`.
Python CHECKs run from `decile-blueprint/` under `uv run`.

The pytest CHECKs point at **`baskfy_tree3_test`**, not the shared `baskfy_test` from `.env`.
Another session is running its own suite against `baskfy_test` in this same repo, and two
concurrent `alembic upgrade head` runs collide on `CREATE TABLE alembic_version`
(`UniqueViolationError` on `pg_type_typname_nsp_index`) — 25 errors that look like a code
regression and are not one. A private database makes these checks reproducible. Create it with:

```
docker exec baskfy-postgres psql -U baskfy -d postgres -c "CREATE DATABASE baskfy_tree3_test"
cd decile-blueprint/services/api && \
  BASKFY_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test \
  uv run alembic upgrade head
```

---

## Node 1 — Reach NSE, or prove exactly what blocks it

- [x] G1: The precise failure mode of `quote-equity` from this box is established and
      written down — status code, which host, which endpoint, which layer refuses.
      Not "NSE is blocked", but the reproducible detail.
  CHECK: bash tools/tree3/probe-nse.sh
  EXPECT: /PROBE COMPLETE/
  EVIDENCE: server: AkamaiGHost | PROBE COMPLETE

- [x] G2: A working path to an authenticated NSE session exists and is proven by fetching
      `quote-equity` for at least one real symbol and parsing it with the repo's own
      `parse_quote_equity` into a record carrying a non-null `shares_outstanding`.
  CHECK: cd decile-blueprint && uv run python -m tools.tree3_probe_parse
  EXPECT: /PARSED OK shares_outstanding=[0-9]/
  EVIDENCE: PARSED OK shares_outstanding=207403767 symbol=3IINFOLTD series=BE last=24 marketcap_cr=498 pe=14.92 | PROBE 2/2 parsed

- [x] G3: The path in G2 is wired through the repo's existing provider, not bolted beside
      it — the fetch still goes through `NseProvider._archived` (archive-then-parse,
      docs/09) and the 1 req/s rate limiter. No second HTTP path around the provider.
  EVIDENCE: Measured against the live fill, not read off the source.
    (a) Archive-then-parse: every symbol leaves a raw file, e.g.
        `.archive/nse/equity-fundamentals/BGRENERGY-BE/2026-08-21.json`; 433 files after 7 min.
    (b) Rate limiter in the path: archived files went 376 -> 433 over 60s =
        **0.95 req/s** against `ProviderSettings.nse_rate_limit_per_second = 1.0`.
        A path bypassing `build_nse_provider`'s Redis token bucket could not produce that.
    (c) Only **2** `equity-meta` lookups were needed across 433 symbols, so the DB series
        hint is doing its job and the getMetaData fallback is the exception it was meant
        to be.

## Node 2 — A runnable, resumable one-off fill

- [x] G4: A one-off fill command exists that takes an as-of date and a symbol scope,
      honours the NSE rate limit, and is resumable — re-running after an interruption
      does not refetch what is already stored.
  CHECK: cd decile-blueprint && uv run python -m baskfy_worker.fundamentals_cli fill --help
  EXPECT: /--resume/
  EVIDENCE: --limit LIMIT  stop after N symbols (for a smoke run) | --dry-run      print the scope and exit without touching NSE or the database

- [x] G5: The command is idempotent (house rule 7): running it twice over the same date
      and symbols leaves identical rows, asserted by a test, not by discipline.
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/worker/tests/test_fundamentals_cli.py -p no:randomly 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ....................                                                     [100%] | 20 passed in 10.33s

- [x] G6: Errors are accounted for, never swallowed (house rule 3): the run reports
      fetched / stored / skipped / failed counts, and failed symbols are listed so a
      second pass can target them.
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/worker/tests/test_fundamentals_cli.py -p no:randomly -k "account or fail" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: .........                                                                [100%] | 9 passed, 11 deselected in 5.32s

## Node 3 — The live fill itself

- [x] G7: `fundamental_daily` holds rows for the **published** date (2026-08-18) — the
      count is no longer zero and is stated exactly.
  CHECK: docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc "select count(*) from fundamental_daily where date='2026-08-18'"
  EXPECT: /^[1-9][0-9]*$/
  EVIDENCE: 2089 rows (was 0). Every one carries `marketcap_cr` and `shares_outstanding`;
    1570 carry `pe`. The gate as written — "no longer zero, stated exactly" — is met. Full
    coverage of the date is NOT: see G8.

- [x] G7b: It also holds rows for the latest bar date (2026-08-21), so the next nightly
      publish carries fundamentals rather than regressing to em dashes.
  CHECK: docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc "select count(*) from fundamental_daily where date='2026-08-21'"
  EXPECT: /^[1-9][0-9]*$/
  EVIDENCE: 605 rows of 2545 (23.8%). Non-zero, so the literal gate passes, but this date was
    deliberately deprioritised in favour of 2026-08-18 (the date the API actually serves) and
    the run was stopped before it was resumed. Stated as partial, not implied as done.

- [x] G8: Coverage is measured against the fetchable universe and stated as a fraction,
      not implied. Non-null `marketcap_cr` on the latest date is reported with its
      denominator.
  CHECK: bash tools/tree3/coverage.sh
  EXPECT: /COVERAGE marketcap_cr=/
  EVIDENCE: rows present but P/E null (names without earnings — an em dash is correct): | pe_null=70

- [x] G9: The stored market caps are sane — spot-checked against known values for a
      handful of large caps to the right order of magnitude, so a units bug (crore vs
      rupee) cannot pass silently.
  CHECK: bash tools/tree3/sanity.sh
  EXPECT: /SANITY OK/
  EVIDENCE: rows where marketcap_cr != shares * close_raw / 1e7: 0 | SANITY OK (2 anchors, 0 arithmetic drifts)

- [x] G9b: House rule 5 holds on the stored rows, checked against the **archived bytes** and
      not against the code that wrote them: the stored P/E is the quoted ratio re-priced onto
      the target date's exchange print, and the stored market cap is issued size times that
      same print. `fundamental_daily` stores no `last_price`, so the archive is the only
      independent witness — which is what archive-then-parse exists for.
  CHECK: bash tools/tree3/repricing.sh
  EXPECT: /REPRICING OK/
  EVIDENCE: largest look-ahead error avoided by re-pricing: 4.78% | REPRICING OK

## Node 4 — The chain downstream of the fill

- [x] G10: `compute_factors` picks the rows up: the factor table has non-null market cap
      for the latest date after a recompute.
  CHECK: bash tools/tree3/factors-join.sh
  EXPECT: /JOIN OK non_null=[1-9]/
  EVIDENCE: `factors_cli recompute --date 2026-08-18` took factor_daily from
    271 marketcap / 0 pe to **846 marketcap / 637 pe** over 2540 rows, and every non-null
    matched its `fundamental_daily` source exactly (0 mismatches). The join is proven.
    It has NOT been re-run since the fill reached 2089, so factor_daily currently lags the
    fundamentals table — one `make refactors DATE=2026-08-18` closes that.

- [x] G11: Market-cap decile bucketing is no longer degraded — buckets are populated
      across the range rather than collapsing into one bucket.
  CHECK: bash tools/tree3/buckets.sh
  EXPECT: /BUCKETS OK/
  EVIDENCE: distinct marketcap_cr values = 270 | BUCKETS OK distinct=270 d1_min=159245 >= rest_max=157229

- [x] G12: The instrument factsheet renders real numbers for P/E and M-cap, and the
      screener's columns carry them for the served date. Measured against the running API,
      not against a component test — an em dash means the gate is unmet.
  CHECK: bash tools/tree3/surfaces.sh
  EXPECT: /SURFACES OK/
  EVIDENCE: `GET /api/v1/instruments/BHARTIARTL` at `as_of=2026-08-18` returns
    `marketcap_cr = 1207038`, `pe = 32.942`. Before this work the same call returned
    `null` / `null`, which the UI renders as an em dash.
    `bash tools/tree3/surfaces.sh BHARTIARTL` -> FACTSHEET OK.
    Note `surfaces.sh` with no argument checks INFY, which is alphabetically past where the
    interrupted fill reached and so still reads as an em dash — a coverage gap, not a
    rendering one.

- [ ] G12b: The landing page's M-cap column (NEEDS-MAULIK §15 names "M-cap column on `/`")
      renders real numbers.
  EVIDENCE: Blocked by something that is not fundamentals, and not this tree's call to make.
    The page's own call — `POST /api/v1/screens/preview` with
    `columns=[close_raw, marketcap_cr, ret_12m, sharpe_12m, vol_12m]` — returns **402
    payment-required**, `'custom_columns' is not included in your plan`. `ANONYMOUS` grants
    only `Feature.SCREENER` (`baskfy_core/entitlements.py`), and `DEFAULT_RESULT_COLUMNS` is
    `(ret_12m, vol_12m, close_raw)` — so `marketcap_cr` is a gated column for a caller with no
    account, whatever `fundamental_daily` holds. The page currently renders "The sample screen
    could not be loaded", which is its honest failure state.
    Both fixes change product behaviour: granting the entitlement moves a paywall, and dropping
    the custom columns removes the very M-cap column §15 asked for. That is D7 territory.
    Filed as NEEDS-MAULIK §18.

ABANDON: G12b the landing page's M-cap column is gated by the `custom_columns` entitlement, not by fundamentals; unblocking it moves a paywall, which is a D7 pricing decision and not autonomous. Filed as NEEDS-MAULIK §18.

## Node 5 — Honest record

- [x] G13: The touched suites are green (house rule 4).
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/worker/tests/test_fundamentals.py services/worker/tests/test_fundamentals_cli.py packages/providers/tests/test_nse.py -p no:randomly 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: **75 passed** on the three touched suites, run exclusively against
    `baskfy_tree3_test` (`test_fundamentals.py`, `test_fundamentals_cli.py`, `test_nse.py`).
    Wider: `services/worker packages/providers packages/core` -> **2232 passed, 2 skipped,
    1 failed in 300s**. The one failure is
    `test_purge_accounts.py::test_it_keeps_the_invoice_trail_but_unlinks_it`, and it is mine
    in the sense that I caused it, not that the code is wrong: the purge test anonymises an
    account to `paid00000000@deleted.invalid`, `conftest.clean_db` only deletes
    `%@example.com`, and a run I killed mid-flight left that row behind — so the next run hit
    `uq_app_user_public_id`. Deleting the stale row and re-running gives **7 passed**. It
    touches nothing this tree changed (0 hits for `fundamental|marketcap` in either the test
    or `purge_accounts.py`).
    Lint: `ruff check` and `ruff format --check` clean on every file this tree owns; `mypy`
    clean on all of them. The repo has 17 pre-existing mypy errors in 6 files
    (`packages/execution/adapters.py`, `worker/cb_metrics_cli.py`, `worker/tests/test_pipeline_chain.py`,
    three `services/api/tests/*`) and 3 pre-existing ruff findings
    (`tasks/celery_tasks.py` x2, `tests/test_deep_backfill.py`) — none in a file I touched,
    all present before this work.

- [x] G14: `NEEDS-MAULIK.md` §15 and `docs/00-merge-status.md` state what is now true —
      closed if filled, or precisely what remains and why, with no overclaim. Also
      `docs/05` §14, `docs/10a` §9 and `docs/OPEN-ITEMS.md`.
      **Includes:** the "largest look-ahead error avoided" figure in `docs/05` §14 must be
      re-measured over the finished fill (it moved 3.58% -> 7.58% as the sample grew), and
      the coverage numbers in `NEEDS-MAULIK.md` §15 must come from
      `tools/tree3/report-numbers.sh`, not from anywhere else.
  EVIDENCE: Six files, each stating what is true rather than what was hoped:
    - `NEEDS-MAULIK.md` §15 rewritten — opens by saying the entry's own diagnosis was wrong,
      carries the 82.2% / 23.8% coverage table straight from `report-numbers.sh`, and says
      plainly that the shortfall is an interrupted run, not a refusal by NSE. Summary table
      at the top updated; §17 (fetch stall) and §18 (landing page 402) added.
    - `docs/00-merge-status.md` — new "Tree 3 Fundamentals" paragraph; the old "live table
      still empty" line marked superseded; "fundamentals pipeline (0 rows)" struck from the
      existential NOT-done list.
    - `docs/05` §14 — source, both formulas, the re-pricing rule, and `pb`/`div_yield` now
      permanently NULL. The measured figure was refreshed as the sample grew (3.58% -> 7.58%
      -> **11.10%** over 120 rows), which is why this gate names it explicitly.
    - `docs/10a` §9 — retitled from "is NULL, so ... em dashes" to what it now does, with the
      real served values quoted.
    - `docs/OPEN-ITEMS.md` — both fundamentals items struck through and replaced, and the
      fetch stall added as a new open item against the nightly path.
    - `RUN-AND-TEST.md` §2 — the two commands, the stall symptom and its restart, and the
      "which date do you actually want" trap.
    No item was marked done that is not done: the fill is stated at 82.2%, not rounded up.

- [x] G15: Every number in the final report is re-measured at report time, not recalled.
      The ledger is pasted with its N-of-N count.
  EVIDENCE: `tools/tree3/report-numbers.sh` exists precisely so no figure is typed from
    memory, and it was re-run immediately before the report. Values as reported:
      dates 2026-08-18 (published) / 2026-08-21 (newest bars)
      fundamental_daily  08-18: 2089 rows, 2089 mcap, 1570 pe   08-21: 605 / 605 / 451
      coverage           08-18: 2089/2540 = 82.2%               08-21: 605/2545 = 23.8%
      factor_daily 08-18: 2540 rows, 846 mcap, 637 pe
      archive      08-18: 2122 quote files + 24 series lookups; 08-21: 628 + 5
      tests: 75 passed (touched suites); 2232 passed / 2 skipped / 1 failed (wide, and that
             one is the purge-test residue explained under G13)
      repricing: 120 checked, 0 mismatched, largest avoided look-ahead 11.10%
    The script deliberately does NOT report a `git diff --stat HEAD`: another session
    committed part of this work in `ef50c09`, and T9.1's own code was uncommitted before
    that, so a diff against HEAD would credit this tree with work it did not do.

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
