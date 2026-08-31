# Gates: 1.1.4 Fix the DRY_RUN token-poisoning path (URGENT — now on the live path)

Scope: with the Kite redirect pointed at Baskfy, `GET /api/v1/brokers/callback` is reachable by a
real login. Today that path writes a FAKE token over the real session and reports success.

Driver-verified, `services/api/src/baskfy_api/routers/brokers.py:305-317`:
    simulated = dry_run_enabled() or not os.environ.get("BASKFY_KITE_API_SECRET","").strip()
    access_token = exchange_request_token(...)        # -> "sim_<sha40>" when simulated
    store_access_token(access_token, store=token_store_for())   # UNCONDITIONAL
    return CallbackOut(connected=True, token_stored=True, simulated=simulated)
`DRY_RUN` defaults to "true" (`broker_oauth.py:56-58`) and the secret is empty, so BOTH triggers
fire. `token_store_for()` resolves to the same store the M58 bridge fills.

## Behaviour chosen (and why)

Four options were on the table: refuse to store when simulated; a separate namespace for
simulated tokens; an explicit opt-in env for stub storage; a non-200 when the exchange could not
really happen. Three of them are complementary and the fix takes all three — the fourth (delete
the stub) was rejected because the stub is what makes the flow exercisable without credentials.

1. **`simulated` is decided once, by the exchange, and returned with the token**
   (`TokenExchange`). The old code recomputed it from the environment a second time and reported
   *that* — the flag was correct and nothing acted on it. Reading it off the result is what makes
   the field and the behaviour unable to disagree.
2. **Two files, and the split is enforced at the write path, not by the caller.**
   `token_store_path()` is the real session; `simulated_token_store_path()` is a sibling
   `*.simulated.enc` that no reader opens. `store_access_token` raises `SimulatedTokenRefused` on
   either crossing. A store shared with a live bridge is not a safe place for a fake value, and a
   rule that lives in the caller is a rule the next caller does not inherit.
3. **Storing a stub at all is opt-in** (`BASKFY_BROKER_OAUTH_ALLOW_SIMULATED`, default off), and
   **without it the callback answers 503** naming every missing precondition — not a stored stub
   and a cheerful 200. 503 rather than 4xx: nothing is wrong with the request, and the browser
   lands here directly, so the one person who can act is the operator reading the detail.
   Precedent is `billing.py`'s "The payment gateway is not responding."
4. **Even opted in, `connected` is `false`.** The flow ran; there is no broker behind it.

The four combinations a real login now hits:

| | secret present | secret absent |
|---|---|---|
| **DRY_RUN=true** | 503, nothing stored, real session untouched | 503, nothing stored, real session untouched (**this is the box today**) |
| **DRY_RUN=false** | live `session/token`, real token → real store, `connected: true` | 503, nothing stored, real session untouched |

With `BASKFY_BROKER_OAUTH_ALLOW_SIMULATED=true`, every 503 cell becomes `200 {connected: false,
simulated: true, token_stored: true}` with the stub in `*.simulated.enc`; the real store is
untouched in that case too.

- [x] G1: A simulated token can never overwrite a real stored session
  CHECK: cd decile-blueprint && timeout 600 uv run pytest services/api/tests -k "simulated or stub or poison" 2>&1 | tail -3
  EXPECT: /passed/
  NOTE: the original CHECK passed `-q`, which doubles the `-q` already in `[tool.pytest.ini_options]
  addopts` and makes pytest print no summary line at all — EXPECT `/passed/` could never match.
  Dropped the redundant flag; nothing else about the check changed.
  EVIDENCE: `21 passed, 1381 deselected in 0.51s`. The guard is `store_access_token`, the single
  write path: it derives the token's kind from `SIMULATED_TOKEN_PREFIX` and raises
  `SimulatedTokenRefused` if a `sim_` value is aimed at the real store (and, symmetrically, if a
  real token is aimed at the simulated one — a live session filed under the wrong name is a
  session the pipeline cannot find). Covered by
  `test_store_refuses_a_simulated_token_in_the_real_store`,
  `test_store_refuses_a_real_token_in_the_simulated_store`,
  `test_store_routes_by_kind_when_no_store_is_named`,
  `test_simulated_store_is_a_different_file_from_the_real_one`,
  `test_stub_storage_is_off_unless_explicitly_opted_in`. The stub itself now builds its value
  from the same constant the guard matches on, so a rename cannot make future stubs invisible.

- [x] G2: A test proves the exact live scenario: a real token in the store, then a DRY_RUN
      callback, then the real token is STILL there and the response does not claim connected
  EVIDENCE: `TestDryRunCallbackCannotPoisonAStoredSession` in
  `decile-blueprint/services/api/tests/test_broker_oauth.py`, two tests, both starting from
  `token_store_for().save(REAL_DESK_SESSION)`:
  · `test_dry_run_callback_leaves_the_real_session_intact_and_refuses` — DRY_RUN on, secret
    absent (the box's measured environment); asserts `Problem.status == 503`, then
    `token_store_for().load().value == REAL_DESK_SESSION` and `not
    simulated_token_store_path().exists()`.
  · `test_opted_in_dry_run_callback_still_leaves_the_real_session_intact` — same, opted in;
    asserts `result.connected is False`, the real token unchanged, and the stub in the
    simulated blob.
  Reproduced outside pytest against a temp store, printing the stored value either side:
      before : REAL-DESK-SESSION-FROM-M58-BRIDGE
      status : 503   type: pipeline-degraded
      after  : REAL-DESK-SESSION-FROM-M58-BRIDGE
      sim blob exists: False
  The pre-existing `test_happy_path_stores_encrypted_bytes` asserted `connected is True` for a
  simulated login writing the shared blob — the defect, spelled as a test. Replaced by
  `test_opted_in_simulated_callback_stores_encrypted_bytes_elsewhere`, which asserts the spec
  (house rule 2). No test was weakened: the new file has 22 tests against the old 8.

- [x] G3: A callback that cannot complete a real exchange fails loudly rather than storing a
      stub and answering 200 — the caller must be able to tell the difference
  EVIDENCE: 503 `pipeline-degraded`, no write, with every missing precondition named and also
  machine-readable in the `reasons` extension member. Measured detail on the box's environment:
      This deployment cannot complete a broker login: DRY_RUN is on, so no live broker call may
      be made; BASKFY_KITE_API_SECRET is not set on this deployment. Nothing was stored and any
      existing broker session is untouched. This login's one-time state has been spent, so start
      the connect flow again once the deployment is configured; or set
      BASKFY_BROKER_OAUTH_ALLOW_SIMULATED=true to exercise the flow with a simulated session
      instead.
      reasons: ['DRY_RUN is on, so no live broker call may be made',
                'BASKFY_KITE_API_SECRET is not set on this deployment']
  Names only, never values — these strings reach an HTTP body. Tests:
  `test_refusal_names_every_missing_precondition` (all three preconditions collected, not
  short-circuited on the first), and `test_a_real_exchange_is_stored_in_the_real_store` for the
  other side of the difference — `connected: true`, `simulated: false`, real store written.
  When the simulated path *is* opted into, the caller still tells the difference from
  `connected: false` plus the prose `note`, modelled on `SyncHoldingsOut.source`/`note`.

- [x] G4: The whole API suite still passes
  CHECK: cd decile-blueprint && timeout 900 uv run pytest services/api/tests 2>&1 | tail -3
  EXPECT: /passed/
  NOTE: same doubled-`-q` correction as G1.
  EVIDENCE: `503 passed, 899 skipped in 24.55s` (the skips are the `db`/`redis`-marked tests,
  unchanged). `test_api_artifacts.py::test_openapi_json_is_current` failed first on the new
  `CallbackOut` shape; regenerated `packages/api-client/openapi.json` (`uv run python -m
  baskfy_api.openapi`) and the TypeScript client (`pnpm --filter @baskfy/api-client run
  generate`) — mechanical consequences of the response model, not design changes.
  `uv run mypy` clean on both source files and the test file; no `# type: ignore`, no `Any`, no
  swallowed exception — `packages/core/tests/test_no_escape_hatches.py` passes (8 passed).

- [x] G5: lint clean
  CHECK: cd decile-blueprint && timeout 300 uv run ruff check services/api/src 2>&1 | tail -2
  EXPECT: /All checks passed|Found 7 errors/
  NOTE: the gate was written expecting 11 pre-existing errors. Measured baseline is **7**, not 11:
  `git stash` of only my two source files and re-running ruff gives `Found 7 errors` — i.e. four
  of the eleven were fixed by other work in flight before this leaf started. EXPECT updated to the
  verified baseline rather than to a number that can no longer occur.
  EVIDENCE: `Found 7 errors` with my changes; `Found 7 errors` with my two source files stashed.
  Net new errors from this leaf: **0**. The seven are RUF100 in `auth_google.py`, PLR0913 ×2 in
  `curated_investments.py`, PLC0415 in `desk_schema.py`, PLR0913+PLR0917 in `routers/auth.py`,
  and one pre-existing E501 at `routers/brokers.py:486` — inside a file I own but on the
  `connect_broker` credential message, untouched by this leaf and plausibly being edited by
  another; left alone rather than reflowed for a cosmetic win.

- [x] G6: No live order, no live Kite write call
  EVIDENCE: `grep -n "httpx|requests\.|kc\.|place_order|place_gtt"` over both source files
  returns exactly two lines, both the pre-existing `httpx.post` to `api.kite.trade/session/token`
  in `exchange_request_token` — a login exchange, not a write, and now unreachable unless all
  three of `DRY_RUN=false`, `BASKFY_KITE_API_KEY` and `BASKFY_KITE_API_SECRET` hold. `DRY_RUN`
  still defaults to `"true"`; every test in this leaf runs with it true, and the one test of the
  live branch (`test_a_real_exchange_is_stored_in_the_real_store`) monkeypatches
  `exchange_request_token` itself, so nothing reaches the wire. The pre-existing
  `TestNoPlaceOrder::test_oauth_modules_never_place_an_order` still scans both modules for
  `place_order` / `OrderGateway` / `confirm=true` / `kc.place` and passes. This leaf strictly
  *reduced* live reach: the callback used to substitute the literal `"dry-run-api-key"` for a
  missing key, which on a box with a secret and `DRY_RUN=false` would have posted a placeholder
  credential to Kite as though it were real. A missing key is now a refusal.

- [x] G7: Sibling audit — every other module touching the shared session blob is enumerated,
      and no unguarded second writer exists
  EVIDENCE: two assertions, so the audit is re-run rather than believed.
  `test_the_write_helper_has_exactly_two_call_sites` globs `*/*/src/**/*.py` and pins the callers
  of `store_access_token` to `broker_oauth.py` and `routers/brokers.py`.
  `test_every_module_touching_the_token_store_is_a_known_one` pins the eight modules that name
  `AccessTokenStore`: `providers/tokens.py` (the store), `providers/__init__.py` (re-export),
  `providers/errors.py` (names it in a message), `providers/kite.py` (reader),
  `baskfy_api/broker_oauth.py` (guarded writer), `worker/index_backfill.py` (reader),
  `worker/ops.py` (reader), `worker/kite_session_cli.py` (**the M58 bridge — the only other
  writer, and it always carries a real desk token, so it cannot poison anything and will
  overwrite a poisoned blob at the next daily pull**). Two sibling flaws found; one was in a file
  I own and is fixed (the `"dry-run-api-key"` placeholder, see G6), the other is G8.

- [x] G8: The read-side sibling is named and handed off, not fixed silently
  EVIDENCE: `services/api/src/baskfy_api/broker_holdings.py:297` opens `token_store_for()` and,
  if the blob is present and issued today, calls `require_fresh()` and hands the value to Kite's
  holdings endpoint. It cannot be poisoned by this route any more, but it does not *recognise* a
  `sim_` value: with a stub already in the blob it would call Kite with a fake credential, get a
  403, and fall back to `source: fixture, degraded: true` — honestly labelled, but it reports "we
  could not reach the broker" when the truth is "this session is fake". `broker_holdings.py` is
  outside this leaf's write contract, so it is not touched here. The one-line fix is an
  `is_simulated_token(token.value)` check beside the `require_fresh()` call, returning a
  `_degraded_holdings("the stored broker session is a simulated one")`.
  **Operational consequence, for whoever owns the box:** any Kite login completed against the
  staging redirect *before this leaf deploys* wrote a `sim_<sha40>` stub over
  `/var/lib/baskfy/state/kite-token.enc`. Deploying this change stops new poisoning but does not
  clean an already-poisoned blob; the next `kite_session_cli` pull overwrites it with the real
  desk session, or it can be checked directly for a value starting `sim_`.
