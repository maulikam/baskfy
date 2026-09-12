# Open items carried forward

Moved out of `CLAUDE.md` so it is not re-read into context every session. Nothing was cut:
this is the same list, in full, and it remains the honest record of what has never run.


- **THE PUBLIC API IS OFF, AND TWO THINGS HOLD IT SHUT.** `BASKFY_PUBLIC_API_ENABLED` is false
  everywhere, and `baskfy_core.public_api.DATA_REDISTRIBUTION_REVIEW.signed_off` is a **source
  constant** that is `False` and must stay `False` until a lawyer has produced the written
  data-redistribution opinion docs/11 §Compliance requires. The router is not mounted while either
  is shut, so the routes are absent from `openapi.json` and from the generated client entirely.
  `packages/core/tests/test_public_api_policy.py` fails the build if the constant is flipped
  (`docs/DECISIONS.md` §20.1). **Flipping it is a commit, not a config change. Do not do it
  without the opinion.**
- **The "derived analytics, not raw bars" line is drawn at "no field priced in rupees per
  share"**, and it is not airtight. `ma_20(t)·20 − ma_20(t−1)·20` recovers `close(t) − close(t−20)`,
  which is why the moving averages are withheld too — but no analysis bounds what a determined
  caller could reconstruct from the *served* series jointly across many `as_of` dates. Treat
  `PUBLIC_COLUMNS` as a starting position for the legal opinion, not a solved problem
  (`docs/DECISIONS.md` §20.4).
- **The webhook sender does no SSRF protection.** `_check_url` refuses a non-`http(s)` scheme and
  plain `http` in production, and does **not** resolve the host or block loopback, link-local or
  RFC 1918 ranges. An authenticated user can point an endpoint at `169.254.169.254` and have the
  server POST a signed body to it (blind — the response never reaches them). Not fixed; the
  feature's unreleased state is what is containing it (`docs/DECISIONS.md` §20.9).
- **No API key has ever authenticated a request outside the test suite,** because the surface they
  open is not mounted in any committed configuration. Key creation, rotation, revocation and the
  usage dashboard all work today and meter nothing.
- **No alert email has ever been delivered.** The dispatch, the diff, the digest grouping and the
  unsubscribe link are all tested against a real database and a recording transport; no Celery
  worker has run `baskfy.alerts.dispatch` and no mail has left the machine. Same class of gap as
  the Razorpay webhook and the API → broker → worker wire.
- **No webhook has ever been POSTed to a real receiver.** Every delivery test drives an
  `httpx.MockTransport`.
- **The alert dispatch runs a screen query per subscribed screen, every night.**
  `baskfy_api.alerts.ensure_run` evaluates each subscribed screen for the published date because
  the nightly publish records no `screen_run` row. Nothing bounds how many subscriptions that is
  (`docs/DECISIONS.md` §20.15).
- **`api_key_usage_daily` is written on the request path** — one upsert per accepted public
  request. Correct at this scale, and the first thing to move if the public API ever carries
  volume (`docs/DECISIONS.md` §20.6).
- **The interactive reference loads Redoc from a CDN with no Subresource Integrity hash.** The URL
  is version-pinned and configurable; the hash is missing because computing one needs a network
  fetch and the suite is network-blocked. Add one before the reference is served publicly
  (`docs/DECISIONS.md` §20.8).
- **`/api-keys` and `/alerts` are not in the sidebar.** `docs/08`'s Account group is four items and
  `src/lib/nav.ts` is pinned to it by a test, so both live in the user menu — the same arrangement
  `/admin` has. Neither page has Playwright coverage.

- **`docs/05` §8's skip-month definition is now REFUTED, not resolved.** Prompt 19's
  reconciliation shows candidate A (`P_{t-21}/P_{t-252}`, the one we ship) implies **521.31%** for
  CUPID against the published **608.37%** — a 16.7% gap the 21-vs-22 / 247-vs-252 offset mismatch
  cannot explain. Candidate B is *not* confirmed; it is merely uncontradicted. The 93-column export
  carries no skip-month column, so nothing in this repository can settle it. **`docs/05` §8 needs a
  hand-edit** pointing at `docs/DECISIONS.md` §19.5, and the same goes for §12
  (circuit detection, §19.6). Both still read "INFERRED", which remains correct.
- **`pos_days_N` and `rsi_N` are not scale invariant for a daily return within one ULP of zero.**
  Found by hypothesis, not by review. A strict-inequality count cannot survive a multiplication
  that moves a 1e-16 tick across zero. Harmless in production (prices are stored at 2 dp) and
  pinned by `TestKnownDiscontinuity` in `packages/core/tests/test_factor_properties.py` so nobody
  "fixes" it by loosening the invariance property.
- **`compute_factors_unrounded` exists and must never be called by a write path.** It is the
  cross-validation harness's seam; `test_factor_crossvalidation.py` asserts no `src/` tree calls it.
- **mutmut and cosmic-ray do not work in this workspace**, and the reason is structural (editable
  src-layout packages resolved by `.pth`). `tools/mutation.py` replaces them;
  `docs/DECISIONS.md` §19.4.
- **A browser has now run part of the Playwright suite — `e2e/portfolios.spec.ts`, 4 passed, on
  22 Aug 2026 (Baskfy M17).** It was the first execution of any spec in this repository. Two things
  had to exist that never had: a populated database and a `baskfy_e2e` database — the config
  migrates and seeds the latter but does not create it, which CI does explicitly and nothing local
  did. The other nine journeys, including the backtest one, are **still unexecuted**.
- **NO LAWYER HAS READ THE FOUR LEGAL DRAFTS.** `apps/web/src/content/legal/*.mdx` are drafts
  written by engineers from `docs/11` §Compliance and Prompt 18 §3. Each opens with a
  `DRAFT REQUIRING LEGAL REVIEW` marker in an MDX comment, asserted by
  `apps/web/src/lib/__tests__/legal-drafts.test.ts` — which also asserts the marker never reaches
  the rendered page. `apps/web/src/content/legal/DRAFT-NOTICE.md` lists the **eight** decisions a
  reviewer must make before the first real charge. Nothing here is launch-ready
  (`docs/DECISIONS.md` §18.10, §18.12).
- **No grievance officer and no Data Protection Officer exists.** Both are `[BRACKETED]` in the
  privacy policy and the terms. The Consumer Protection (E-Commerce) Rules, 2020 want the first
  before payments are taken; the DPDP Act wants a contact for the second.
- **The statically generated public routes get a different CSP.** `script-src 'self'
  'unsafe-inline'` on `/`, `/faq`, `/about`, `/support`, `/blog/*`, `/december-2026-update` and the
  four legal pages; the nonce policy everywhere else. A nonce cannot appear in a prerendered file,
  so SSG (`docs/08` §Routes) and the nonce CSP (`docs/11` §Security) are mutually exclusive per
  route. **If a marketing page ever renders untrusted input, revisit this**
  (`docs/DECISIONS.md` §18.2). The one list that decides is
  `apps/web/src/lib/marketing/routes.ts`'s `isStaticPublicPath`.
- **The root layout no longer reads `headers()`.** The CSP nonce moved into `(app)/layout.tsx` and
  `(auth)/layout.tsx`, and `<Providers>` moved with it, because a `headers()` call in the root
  layout makes every route in the app dynamic.
- **`/pricing` is the one page in `docs/08`'s SSG row that is still dynamic**, because Prompt 13
  built it to render the caller's current plan. `apps/web/e2e/static-generation.spec.ts` asserts
  that positively, so making it static later is a deliberate change (`docs/DECISIONS.md` §18.3).
- **The FAQ's question set is reconstructed, not observed.** `docs/01` §1 records that the
  reference product has a `/faq` and captures nothing on it, so every question is derived from
  something `docs/01` observed directly (`docs/DECISIONS.md` §18.4).
- **`POST /support` is not in `docs/07`.** It emails and stores nothing — no ticket table, so a
  failed delivery loses the message and the endpoint answers 500 rather than claiming a send. It
  is not an open relay: no recipient parameter, and the subject carries only a closed-set value
  (`docs/DECISIONS.md` §18.5). **It has never delivered a real email.**
- **The copy lint is negation-aware and scoped to the content routes.** "advice" and
  "recommendation" appear in the disclaimer `docs/11` mandates, so a substring ban is impossible;
  each occurrence must sit in a negated sentence, a sentence naming a SEBI-registered adviser, or a
  question (`docs/DECISIONS.md` §18.6). It does not scan the screener, the error catalogue or the
  admin surface (§18.7).
- **The landing page's sample screen renders nothing when the API is down**, rather than falling
  back to the committed reference export — stale numbers with no label are worse than an empty slot
  (`docs/DECISIONS.md` §18.8).
- **The consent banner's `analytics` category gates nothing**, because no analytics tag ships. The
  toggle exists so that adding one is a gated change rather than a silent one (§18.11).
- **Server-log retention is `[RETENTION PERIOD — NOT YET SET]` in the privacy policy.** Nothing in
  the codebase expires a log line.

- **NO RUNBOOK HAS BEEN EXECUTED AGAINST STAGING.** Prompt 17's third acceptance criterion —
  "every runbook has been executed once against staging and updated with real output" — is **not
  met**. There is no staging environment in this repository. Each of the five carries a
  `Verified against: NOT YET` line and `services/worker/tests/test_ops_and_alerts.py` asserts that the line
  is there, so the day one is quietly marked verified without output to back it, the suite says so.
- **Browser exceptions are not reported to Sentry.** `@sentry/nextjs` is initialised from
  `apps/web/src/instrumentation.ts` only — no `withSentryConfig`, no `instrumentation-client.ts` —
  so the client bundle is byte-identical to before Prompt 17 and the 250 KB screens-route budget
  is unaffected. Server components, route handlers and server actions *are* covered
  (`docs/DECISIONS.md` §17.12).
- **The Grafana dashboards have never been rendered.** `infra/grafana/dashboards/*.json` is
  asserted to be valid JSON whose every panel queries a metric we publish; no Grafana has loaded
  them. The compose profile (`--profile observability`) exists so that can be checked locally.
- **The alert rules have never fired.** `promtool check config` passes in CI, and a test asserts
  every rule names a published metric and an existing runbook — but no Prometheus has evaluated
  them against real data.
- **The restore drill takes its own backup; it never reads R2.** CI has no bucket credentials, so
  `.github/workflows/restore-drill.yml` dumps a freshly seeded database, restores it and asserts
  it. That exercises everything except "the object in the bucket is readable"
  (`docs/DECISIONS.md` §17.13). **No backup has ever been uploaded to or read from a real bucket,
  and no WAL segment has ever been archived.**
- **Point-in-time recovery is not possible today.** `infra/backup/wal-archive.conf` is written
  from the PostgreSQL 16 documentation and has never run, and PITR needs a *physical* base backup
  (`pg_basebackup`) that nothing here takes. The recovery point objective is last night's dump.
- **The worker's `/metrics` is one port per process.** With a prefork pool only the first child to
  bind serves and the rest are silent. `PROMETHEUS_MULTIPROC_DIR` is the supported answer and is
  not configured.
- **An abandoned run is detected by age, not by a heartbeat** — up to 105 minutes after the worker
  died (90-minute stale window + a 15-minute sweep). The trade is argued in
  `docs/DECISIONS.md` §17.6.
- **Queue depth is read with `LLEN` on the queue name**, which is how Celery's *Redis* transport
  stores a queue. A move to RabbitMQ gives a silent zero, not an error.
- **`app_user.is_staff` is granted by `BASKFY_STAFF_ALLOWLIST` (default: the founder) on Google
  sign-in and `GET /me`, and can still be set by hand.** The list promotes, never demotes. No
  self-service form. `docs/runbooks/pipeline-failed.md` §"Making yourself staff" has the
  `UPDATE` for an address that is not on the list. A non-staff caller gets **404** from
  `/admin/*`, so "the admin page 404s" is what a missing bit looks like (`docs/DECISIONS.md`
  §17.3).
- **There is no `providers login` command,** so replacing an expired Kite token means pasting a
  heredoc at 3am with a single-use token that expires in minutes.
  `docs/runbooks/kite-token-expired.md` §3 says so and says it should be a command.

- **The GST rate and the SAC code need a chartered accountant.** 18% and SAC 998439 are defaults,
  not advice. Both are settings (`BASKFY_GST_RATE_PERCENT`, `BASKFY_GST_SAC_CODE`) and every
  issued invoice stores what it was raised at, so changing them cannot rewrite history — but
  nothing has confirmed them (`docs/DECISIONS.md` §13.2). **Confirm before the first real charge.**
- **The advertised prices are GST-inclusive.** `docs/01` §1 gives ₹500 / ₹3,999 / ₹14,999 with no
  mention of tax; charging ₹590 for a plan the page calls ₹500 would be the alternative. The
  taxable value is back-computed and the tax is the remainder, so the invoice total is always the
  payment to the paisa (`docs/DECISIONS.md` §13.1).
- **The Razorpay webhook has never seen a real delivery.** The signature scheme, the event names
  and the entity shapes are written from the documented formats, not against the live gateway —
  the suite is network-blocked and every test drives an `httpx.MockTransport`. Confirm against a
  test-mode delivery before taking money (`docs/DECISIONS.md` §13.17).
- **Nothing collects a customer GSTIN or a place of supply.** The columns, the arithmetic and the
  inter-state (IGST) path all exist and are tested; no UI fills them, so every invoice raised
  today is B2C, intra-state, at the supplier's own state. A B2B customer cannot claim input credit
  until a form exists.
- **`payment.status` never becomes `refunded`.** `refund.*` events are acknowledged and ignored.
  `docs/07` has no refund endpoint and `docs/11` names a Refund Policy page that is not written.
- **The rebalance tracker stores quantities and does nothing with them.** `quantity` and
  `avg_price` are imported, stored and echoed back; no weight, exposure or P&L is derived from
  them, because `docs/01` §8's tracker is a symbol diff. Target weights are equal-weight, which
  `docs/07` does not specify (`docs/DECISIONS.md` §14.1).
- **A BSE scrip code in an uploaded CSV is reported, never resolved.** We hold NSE instruments and
  have no BSE-code mapping; `532540` comes back `unmatched` with reason `bse_code`
  (`docs/DECISIONS.md` §14.4). The same goes for a symbol that names more than one listing — it
  is `ambiguous` with its candidates, and nothing is imported for it.
- **No holdings editor exists.** Holdings are set by CSV upload or by `PUT /portfolios/{id}/holdings`;
  there is no add-a-row form. `docs/08` §"Rebalance tracker" describes the wizard and asks for none.
- **The rebalance tracker has no Playwright coverage.** The rule, the parser, the endpoints and the
  React components are all tested, but no browser test walks the wizard end to end.
- **`docs/DECISIONS.md` §15 should become `docs/10a-backtest-implementation-notes.md`.** Same
  reason as §13 and §14: the overnight run could append to `DECISIONS.md` and change nothing else
  under `docs/`. Note that `docs/10a` is currently the *instrument factsheet* notes, so the
  backtest file needs a different number.
- **`dividends: "cash"` and `dividends: "reinvest"` are refused, not implemented-and-wrong.**
  This bullet said the opposite until M43, and every clause of it was false. `docs/09`'s algorithm
  *would* fold cash dividends into `adj_factor`, making `ohlcv_daily.close` a total-return series —
  but **it does not run**. M27 put the question to the reference corpus and measured the answer: the
  price convention won 42 of 45 deciding symbol-windows and matched all 25 dividend-paying symbols
  exactly at stored precision on the three windows that reproduce. M28 then applied the 47
  share-count actions and deliberately **not** the 38 dividend-shaped ones
  (`reconciliation/RECOVERED-ACTIONS.md`, "VERDICT: PRICE RETURN").

  So `ohlcv_daily.close` is a **price-return** series: splits and bonuses are inside it, cash
  dividends are not. `ignore` is therefore exact, needs no extra data, and is the default — it is
  what the engine has always actually computed, and until M39 it was mislabelled `reinvest`. The
  two that need a dividend schedule are `cash` and `reinvest`, because both have to *add* a
  dividend back; the engine raises rather than quietly serving a price return under a
  total-return name (`docs/DECISIONS-MERGE.md` §M39.3, §M43).

  **Every return this product publishes is a price return, and is lower than a total return by
  roughly the dividend yield — about 1.2% a year on NSE, compounding.** The backtest assumptions
  panel says so; any new surface that shows a return owes the reader the same sentence.
- **The backtest's risk-free rate is a flat annual rate defaulting to 0.** `docs/10` §Outputs asks
  for "rf from a configurable T-bill series" and `docs/04` has no T-bill table. Every Sharpe and
  Sortino on the page is therefore an *excess-over-zero* figure until one exists
  (`docs/DECISIONS.md` §15.5).
- **The 15-year backtest budget is measured against a synthetic market, not the seeded dataset.**
  PROMPTS.md Prompt 15's last acceptance criterion says "on the seeded dataset"; the seeded
  database holds one trading day of *results* (`docs/13`'s export) and no price history, so no
  multi-year backtest can run against it at all. The budget is met with room to spare — 0.24 s for
  the pure engine over a 300-name panel, 1.44 s for the whole server-side path (181 screen queries
  + bar load + simulation) over a 250-name market seeded into PostgreSQL, against 10 s — but that
  is not the same measurement.
- **Until a real backfill has run, every backtest a user could queue fails.** `ohlcv_daily` holds
  no history, so the loader raises "no adjusted bars exist for any name this screen selected".
  That is correct behaviour and it will look like a bug in staging.
- **The API → broker → worker wire has never run.** `POST /backtests` publishes
  `baskfy.backtest.run` by name and the worker binds and routes it; both halves are tested
  separately, but no test starts a Celery worker. Same class of gap as the Razorpay webhook.
- **The backtest's `export` link is signed by us, not presigned by R2.** `docs/07` asks for a
  "signed URL"; the archive abstraction covers both a bucket and a directory, and a directory
  cannot presign. The link is an HMAC over `(public_id, artefact, expiry)` redeemed at an
  unauthenticated download route (`docs/DECISIONS.md` §15.15). CSV only — no Parquet writer.
- **Deleting a backtest leaves its R2 artefacts behind.** They are keyed by a `public_id` that is
  never reissued, so nothing can read them; but nothing sweeps them either
  (`docs/DECISIONS.md` §15.19).
- **The backtest surface has no Playwright coverage,** the same gap the rebalance tracker has.
- **An unpaid account's `max_screens` is 5, and 5 is invented.** `docs/07`'s example payload shows
  50, which is the paid number; the bundle gives no free-tier figure
  (`baskfy_core.entitlements.FREE_MAX_SCREENS`, `docs/DECISIONS.md` §13.12).
- **The ₹0 tier exists but is off.** `BASKFY_FREE_TIER_ENABLED` gates it, and the plan row is only
  seeded while the flag is set — turning it on means re-running `make seed`. Its "limited
  universe" is NIFTY 50, which `docs/01` does not specify (`docs/DECISIONS.md` §13.14).
- **The invoice PDF is written by hand, in Courier.** `docs/02` locks no PDF library, so
  `baskfy_core.pdf` writes PDF 1.4 directly; Courier because its fixed 600/1000 em width makes
  right-aligned figures exact without transcribing a font-metrics table. A designed invoice is
  later, deliberate work (`docs/DECISIONS.md` §13.6).
- **`docs/DECISIONS.md` §13 should become `docs/13b-billing-implementation-notes.md`.** The
  overnight run was permitted to append to `DECISIONS.md` and change nothing else under `docs/`,
  so Prompt 13's implementation notes are collected there instead of in the numbered file the
  `07a`/`08a`/`12a` convention would put them in.
- **One intermittent test deadlock, seen once and not reproduced.**
  `test_seed.py::test_reseeding_does_not_demote_reconciled_days` failed with
  `DeadlockDetectedError` on its `DROP SCHEMA ... CASCADE` during a full `pytest` run, then passed
  on two further full runs and on every targeted run. Two connections to the same test database
  contending; worth pinning down before CI relies on it.

- **The docs/11 "as measured" column lives in `benchmarks/AS-MEASURED.md`, not in `docs/11`.**
  The overnight run could not edit the spec, so the rendered table is committed beside the
  benchmarks and needs pasting in by hand (`docs/DECISIONS.md` §16.1).
- **The nightly-pipeline budget (< 45 min) is the one docs/11 row nothing measures.** Nine of
  docs/03's ten steps are network fetches and the suite is network-blocked; only
  `compute_factors` is timed, on a synthetic panel with no database on either side. The row reads
  "not measured" rather than being dropped.
- **Every server-side benchmark is in-process over an ASGI transport** — no socket, no uvicorn
  worker pool, one event loop. They are floors, not production p95s. `make loadtest URL=...`
  takes the same numbers against a running server and has never been run against a deployment.
- **`market_health_daily` and `index_snapshot_daily` are now hypertables,** which `docs/04`'s DDL
  does not say. `docs/03` §"Scaling plan" step 3 asks for continuous aggregates over them and a
  continuous aggregate needs a hypertable (`docs/DECISIONS.md` §16.2). The two aggregates
  (`market_health_monthly`, `index_snapshot_monthly`) exist, refresh on a policy and are asserted
  to agree with their source — **and nothing reads them.**
- **The screen cache's single-flight is per process, not distributed.** It collapses a cold
  stampede from N callers to one *per uvicorn worker*, which took the 50-concurrent p95 from
  503 ms to 304 ms. It is not a lock (`docs/DECISIONS.md` §16.8).

- **The trading calendar reconciles itself now, but only where data exists.** `reconcile_calendar`
  (Prompt 3) promotes dates with bars to `bhavcopy` and infers holidays across densely populated
  ranges, which is what fills in the lunar-calendar holidays the seed list is missing
  (`docs/04a-trading-day-addendum.md`). Until a full backfill has run, most of the calendar is
  still `derived` — a guess. Prompt 5 must still assert the 22/64/121/185/247 window lengths.
- **The 6 example screens are authored, not observed.** `docs/01` §1 records only that six exist.
  See the note in `seed_data.py`.
- **`docs/06`'s reference SQL skeleton is not PostgreSQL.** It uses `QUALIFY`, which only
  Snowflake and DuckDB have, and a bare `ORDER BY marketcap_cr DESC` that would put every
  NULL-marketcap row in the top decile. Both are translated, and every other departure from the
  letter of `docs/06` is written up in `docs/06a-screener-implementation-notes.md`. Read that
  before changing `screener.py`.
- **`docs/02` asks for credentials auth *and* Postgres sessions; Auth.js v5 cannot do both.** The
  Credentials provider requires `strategy: "jwt"`. The session is a cookie JWT; the Postgres
  adapter carries the user records and the OTP tokens (`docs/08a` §2).
- **The auth stub is gone.** Prompt 12 built `/auth/*`, so `apps/web` posts to the real endpoints,
  Argon2id verification happens where the hash lives, and the browser suite registers and signs in
  for real. `stub-endpoints.ts` and `BASKFY_ALLOW_STUB_AUTH` were deleted (`docs/12a` §5).
- **Six auth tables are additions to `docs/04`,** which defines only `app_user.password_hash`.
  Each is required by a numbered line of `docs/11` §Security or §Compliance
  (`docs/04c-auth-tables-addendum.md`).
- **Codes and refresh tokens are stored as SHA-256; only passwords are Argon2id.** Neither has an
  offline guessing game to slow down, and a KDF on every token refresh is a DoS surface
  (`docs/12a` §3).
- **Refresh-token reuse revokes the whole family,** so a client that races itself can be signed
  out. That is the documented trade against a stolen cookie working indefinitely (`docs/12a` §4).
- **`ConsoleTransport` logs the OTP on purpose** and is refused in production by
  `require_configured`. Every other path is stripped by `RedactingFilter` (`docs/12a` §7).
- **`PATCH /me` cannot change an email address.** That needs the new address proved first, which
  is a separate flow and is not built (`docs/12a` §8).
- **The registration consent checkbox names Terms and a Privacy Policy that are not written.**
  `consent_record.document_version` is stored, so the published text will need fresh consent
  (`docs/12a` §10).
- **Erasure anonymises an account that has invoices and deletes one that does not.**
  `payment.user_id` is NOT NULL and GST retention outranks DPDP erasure (`docs/12a` §9).
- **Test addresses are `@example.com`, not `@decile.test`** — `email-validator` refuses the
  special-use `.test` TLD outright (`docs/12a` §12).
- **Volatility's ×100 happens in `apps/web/src/lib/format.ts` and nowhere else.** The screener and
  the API both store and serve the decimal fraction (`docs/06a` §10, `docs/07a` §13).
- **`apps/web` has zero `any`, enforced by a source scan** (`src/lib/__tests__/no-any.test.ts`),
  not only by an ESLint rule that an inline comment could switch off.
- **Nav items for unbuilt routes render disabled, with the prompt that delivers them.** Keep
  `apps/web/src/lib/nav.ts` honest when a route lands (`docs/08a` §10).
- **An out-of-range `as_of` is a 422, not a clamp.** Prompt 7 changed this: a weekend still snaps
  backwards (`docs/06` §step 1), but a date later than the latest published day or earlier than
  `DATA_START_DATE` (1 Nov 2024, `docs/01` §2.13) is refused as `no-trading-day`.
  `docs/06a` §11 records the supersession; `docs/07a` §1 has the table.
- **The CSV export is `docs/13`'s 93 columns, not the screen's column set.** Prompt 7 built the
  latter; `docs/13` §5 step 6 makes reproducing the committed fixture the acceptance test, and
  Prompt 9 §6 says the same. Line endings are `\n` and `name` is quoted on every row
  (`docs/09a` §1).
- **The screen-cache key hashes the definition *and* the projection.** `docs/06`'s formula covers
  only the definition, which serves the wrong columns after a column-layout save
  (`docs/06a` §8a, `docs/09a` §3).
- **`apps/web/src/lib/screens/operands.ts` duplicates `CUSTOM_FILTER_OPERANDS`,** because no
  `/meta/` endpoint publishes it. `packages/core/tests/test_operand_parity.py` is the link — keep
  the two in step. `apps/web/src/lib/market/universes.ts` duplicates the twelve market-health
  universes for the same reason, with `test_market_health_universe_parity.py` as its link.
- **The browser suite needs `make up` and its own database.** `playwright.config.ts` migrates and
  seeds `baskfy_e2e` and starts both servers; since Prompt 12 it also turns the Argon2id cost down
  and points email at mailpit, and signs in with `baskfy_api.seed.E2E_PASSWORD` (`docs/09a` §5,
  `docs/12a` §12).
- **Eight PROS fire for CUPID where `docs/01` §5 observed six.** `docs/05` §16 tables eight rules
  and says "ship at least" them; the six the reference product showed are the first six, asserted
  word for word on both sides (`docs/10a` §1).
- **A PROS/CONS rule with a NULL input is neither.** Rendering the negation would put a CON on a
  young listing for a fact nobody knows; `undecided()` names those rules instead (`docs/10a` §2).
- **The percentile bars compare against the *narrowest* universe the instrument is in that day,**
  because `docs/08` never defines "the current universe". It is named in the payload and on the
  page (`docs/10a` §4).
- **The two Wasserstein distances are recomputed, not stored** — `docs/04` has no column for them
  and `docs/05` §15 requires them exposed. The stored `regime` label still wins when populated
  (`docs/10a` §3).
- **~~`factor_daily.pe` is NULL everywhere.~~ Filled for the published date** (Tree 3). The
  factsheet's P/E stat and "Price to Earnings" card render real numbers; a name NSE does not
  publish a P/E for still shows an em dash, which is correct rather than missing. `pb` and
  `div_yield` remain NULL everywhere and now permanently: NSE's current quote payload does not
  carry them at all (`docs/05` §14).
- **Nothing calls `POST /api/revalidate` yet.** The instrument pages carry the `factsheet` cache
  tag and the route that purges it exists, but wiring the nightly publish step to make the request
  is not done — until it is, ISR refreshes on its one-hour timer (`docs/10a` §11).
- **The dashboard fixture carries 117 indices, not `docs/01` §7's ~145.** The last ~28 are names
  we would be guessing at, and a fixture of invented index names teaches the reader a market that
  does not exist. The row count is asserted (`docs/11a` §1).
- **The dashboard has no sector grouping, because `docs/04` has no sector column.** Deriving one
  by pattern-matching index names would be inventing a taxonomy and attributing it to NSE
  (`docs/11a` §2).
- **The breadth arithmetic is `baskfy_core.breadth`, executed by both the worker and the seed.**
  A second copy in the seed would make the hand-computed acceptance test meaningless
  (`docs/11a` §7).
- **`1Y Return > 0%` reads 100% on the seeded data** — `docs/13`'s export is a momentum screen's
  output, so every row in it is positive by construction. Asserted, so nobody reads it as a bug
  (`docs/11a` §8).
- **Breadth exists for one date only** until the pipeline has run over a real backfill, so the
  history charts say "a line needs two" rather than drawing one (`docs/11a` §8).
- **"Revalidate on `data_version`" is a data-cache tag, not a static route.** Everything under
  `(app)` renders dynamically because the shell reads cookies; the tagged fetch is what
  `revalidateTag` invalidates (`docs/11a` §6, correcting `docs/10a` §11).
- **`docs/07` does not say where the client sends `data_version`,** but its catalogue requires a
  409 for a stale one. It is an optional field on the run and preview bodies (`docs/07a` §2).
- **`api_access` is false for everyone and `X-API-Key` is ignored** until Prompt 20 has a key
  store. Prompt 7's "entitled to everything except the three" reads otherwise; `docs/07`'s own
  example payload does not (`docs/07a` §4).
- **`503 pipeline-degraded` means "no published version at all".** A *failed* run keeps serving the
  last good `data_version` with `degraded: true` on `/meta/status`, which is what `docs/11`
  §Reliability asks for (`docs/07a` §8).
- **`ignore_top_beta.count` / `ignore_top_volatility.count` are accepted and ignored.** `docs/06`
  settles the semantics as a boolean flag precomputed per universe at `TOP_RISK_FLAG_PERCENTILE`;
  a per-request count cannot be served by testing a bit (`docs/06a` §5).
- **The export's row order is reproducible only to ±0.0075.** `docs/13` §2 finding 3 says so
  itself: the reference product ranked on unrounded values the export does not carry. 114 of the
  271 positions differ from the file, every one of them inside that rounding granularity
  (`docs/06a` §7). Note that the 48 inversions `docs/13` reports *do* reproduce exactly with
  `Decimal` — the note below about 53 was a float artefact.
- **~~`ix_factor_daily_date_marketcap_cr` is dead weight today.~~ Fixed in Prompt 16.**
  Migration 0008 drops `ix_factor_daily_date` and rebuilds the composite with
  `INCLUDE (instrument_id)`. Prompt 6's fifth acceptance criterion — "assert with EXPLAIN that it
  uses the (date, marketcap_cr) index" — now holds as written (`docs/DECISIONS.md` §16.4).
- **`docs/05` §2 and `docs/13` §4 disagree on volatility units** — percent vs decimal fraction.
  The export is the arbiter: it is a fraction. Resolve in Prompt 5.
- **`docs/02` and `docs/09` disagree on Kite adjustment.** `docs/02` says Kite gives
  "adjusted/unadjusted daily candles"; `docs/09` says "Kite returns unadjusted OHLC by default.
  Treat everything from Kite as raw." We follow `docs/09`: providers never adjust, and
  `apply_adjustments` (Prompt 3) derives `close` from `close_raw`.
- **Provider bar fixtures are synthetic.** Real symbols, names, index memberships, closing prices
  and CUPID's documented corporate actions; every bar before 2026-08-18 is a seeded random walk.
  See `tests/fixtures/providers/PROVENANCE.md`.
- **NSE URL shapes and column names are still unverified.** Written from the documented file
  layout, not against a live endpoint (the suite has no network). Confirm each URL and header row
  against a real fetch before the first production backfill.
- **THE DECISIVE PARITY TEST HAS NEVER RUN GREEN.** `docs/13` §5 step 2 reproduces every numeric
  column of all 271 export rows, which needs each instrument's adjusted daily closes for 248
  trading days. The bundle has no price history — `fixtures/` is one single-date snapshot of
  *results* — and the suite is network-blocked. `test_reference_parity.py` implements it in full
  and **skips**, with the reason attached. Set `BASKFY_PARITY_BARS` to a Parquet file of real
  adjusted history and it runs. Everything the export *can* verify is verified and passing.
- **One figure in `docs/13` §2 does not reproduce from the committed file.** Max sharpe error is
  0.01, not 0.0051. (The blend-inversion count *does* reproduce as 48 when the blend is summed in
  `Decimal`; the earlier reading of 53 came from float arithmetic.) An artefact of re-deriving
  from already-rounded inputs, which does not overturn a formula — the error distribution has *nothing*
  between 0.00 and 0.01. Written up in `docs/13a-verification-discrepancies.md`.
- **`docs/01` miscounts twice.** §3 is headed "62 ranking factors" but enumerates 64; §4 says
  "34 available columns" but enumerates 36. We implement every named key.
- **Window lengths do not yet match `docs/13` §3.** The engine's window arithmetic is right (the
  1-month window resolves to 22 exactly), but the seeded calendar is short ~9 lunar-calendar
  holidays a year, giving 22/67/127/191/256 against the required 22/64/121/185/247. Fixed by
  running `reconcile_calendar` over a real backfill, not by changing the engine.
- **`compute_market_health` reports NULL, not 0%, until factors are populated for a date.**
- **Quality-gate assertion 6 reports SKIPPED, not PASSED.** It needs free-float weights and the
  index divisor to reconstruct a NIFTY 50 level; NSE publishes neither in the files `docs/09`
  lists and nothing populates `index_member_daily.weight`.
- **Assertions 3 and 7 are constraint checks, not data checks.** The PK and NOT NULL already
  forbid what they assert; they exist to catch a migration that relaxes either.
- **Rights issues are not adjusted.** TERP needs the subscription price, which NSE's free-text
  purpose usually omits. `baskfy_core.adjustments` returns `INSUFFICIENT_DATA` and leaves the
  series alone rather than guessing; every such action is surfaced in the step payload.
- **The ~145 dashboard indices are registered from data, not from a list.** The bundle names two
  of them (`docs/01` §7), so `refresh_index_snapshots` registers any index NSE publishes that we
  have no `index_def` row for, allocating ids from 100 upward. The 14 selectable universes stay
  hand-pinned at ids 1–14 because they carry `factor_daily` mask bits.
- **Pre-2018 index membership is reconstructed, and marked as such.** `index_member_daily.source`
  distinguishes `nse_file` from `reconstructed` and `derived` (`docs/09` §Backfill,
  `docs/04b`). Prompt 15's backtests **state** it rather than excluding it: every backtest's
  assumptions panel says so in as many words (`baskfy_api.backtests.assumptions`), which is what
  `docs/10` §"honesty features" asks for. Nothing filters a run by `index_member_daily.source`.
- **~~No pipeline step fetches fundamentals.~~ Fixed — but read the next item.** T9.1 folded a
  fetch into step 6 and Tree 3 made it actually work: the endpoint it targeted
  (`/api/quote-equity`) had been retired by NSE and answered 403 at the Akamai edge, so the step
  had never written a row. It now calls `GetQuoteApi`, scoped to the day's traded names with the
  series `instrument.series` already holds, and `baskfy_worker.fundamentals_cli` back-fills a past
  date (`docs/05` §14, `DECISIONS-MERGE.md` §T3F.1–T3F.5).
- **Synthetic index levels reached a real table twice, and one Kite index is filed under another's name.** `baskfy_api.seed market|all|e2e` writes the fixture builder's random-walk `index_snapshots` (thirty days ending `FIXTURE_AS_OF`) into `index_snapshot_daily` on real dates; the development database's copy of those rows was seeded onto staging and read by the first swing scan as a 10,378 fifty-day average against a 23,222 close (SW16). The writer now refuses a > 40 % day-on-day level and `baskfy_worker.index_repair` rewrites a window from NSE, but the seeder still writes them locally, and M31's `("SERVSECTOR", "CONSUMERSERVICES")` puts *Nifty Services Sector* (~30,500) under `nifty-consumer-services` (NSE: ~3,650) — the guard refuses NSE's value for that slug on every day until the table is fixed and the index re-backfilled.
- **The NSE fetch stalls roughly every 600 requests, and step 6 is now long enough to hit it.**
  One ESTABLISHED idle socket, `provider retry` repeating, no recovery — while `curl` against the
  same URL answers in 0.3s. `call_with_retry` is bounded correctly, so the hang is inside a single
  `client.get`: httpx applies its `read` timeout **per socket read**, not per request, so a peer
  that goes silent mid-response is never timed out. The backfill works around it with
  `tools/tree3/drive-fill.sh` (bounded rounds, `--resume`); **the nightly has no such supervisor.**
  The fix is a total-request deadline around `NSEProvider._fetch`, with a test that a trickling
  server is abandoned. `NEEDS-MAULIK.md` §17, `DECISIONS-MERGE.md` §T3F.6.
