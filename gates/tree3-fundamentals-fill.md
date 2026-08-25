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

- [ ] G5: The command is idempotent (house rule 7): running it twice over the same date
      and symbols leaves identical rows, asserted by a test, not by discipline.
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/worker/tests/test_fundamentals_cli.py -q -p no:randomly 2>&1 | tail -3
  EXPECT: /\d+ passed/
  EVIDENCE: pending

- [ ] G6: Errors are accounted for, never swallowed (house rule 3): the run reports
      fetched / stored / skipped / failed counts, and failed symbols are listed so a
      second pass can target them.
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/worker/tests/test_fundamentals_cli.py -q -p no:randomly -k "account or fail" 2>&1 | tail -3
  EXPECT: /\d+ passed/
  EVIDENCE: pending

## Node 3 — The live fill itself

- [ ] G7: `fundamental_daily` holds rows for the **published** date (2026-08-18) — the
      count is no longer zero and is stated exactly.
  CHECK: docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc "select count(*) from fundamental_daily where date='2026-08-18'"
  EXPECT: /^[1-9][0-9]*$/
  EVIDENCE: pending

- [ ] G7b: It also holds rows for the latest bar date (2026-08-21), so the next nightly
      publish carries fundamentals rather than regressing to em dashes.
  CHECK: docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc "select count(*) from fundamental_daily where date='2026-08-21'"
  EXPECT: /^[1-9][0-9]*$/
  EVIDENCE: pending

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

- [ ] G10: `compute_factors` picks the rows up: the factor table has non-null market cap
      for the latest date after a recompute.
  CHECK: bash tools/tree3/factors-join.sh
  EXPECT: /JOIN OK non_null=[1-9]/
  EVIDENCE: pending

- [x] G11: Market-cap decile bucketing is no longer degraded — buckets are populated
      across the range rather than collapsing into one bucket.
  CHECK: bash tools/tree3/buckets.sh
  EXPECT: /BUCKETS OK/
  EVIDENCE: distinct marketcap_cr values = 270 | BUCKETS OK distinct=270 d1_min=159245 >= rest_max=157229

- [ ] G12: The instrument factsheet renders real numbers for P/E and M-cap, and the
      screener's columns carry them for the served date. Measured against the running API,
      not against a component test — an em dash means the gate is unmet.
  CHECK: bash tools/tree3/surfaces.sh
  EXPECT: /SURFACES OK/
  EVIDENCE: pending

- [ ] G12b: The landing page's M-cap column (NEEDS-MAULIK §15 names "M-cap column on `/`")
      renders real numbers.
  EVIDENCE: pending

## Node 5 — Honest record

- [ ] G13: The touched suites are green (house rule 4).
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/worker/tests/test_fundamentals.py services/worker/tests/test_fundamentals_cli.py packages/providers/tests/test_nse.py -q -p no:randomly 2>&1 | tail -3
  EXPECT: /\d+ passed/
  EVIDENCE: pending

- [ ] G14: `NEEDS-MAULIK.md` §15 and `docs/00-merge-status.md` state what is now true —
      closed if filled, or precisely what remains and why, with no overclaim.
  EVIDENCE: pending

- [ ] G15: Every number in the final report is re-measured at report time, not recalled.
      The ledger is pasted with its N-of-N count.
  EVIDENCE: pending

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
