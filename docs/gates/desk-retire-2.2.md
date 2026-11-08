# Gates: 2.2 Plan-expiry enforcement into packages/ — the prerequisite for any execute route

Scope: Maulik lifted the execute-route rule on 2026-08-31, scoped MULTI-TENANT. That case is
gated by counsel item C3, so the route ships FLAG-OFF. This leaf builds the part that must exist
first and is valuable regardless: non-negotiable #1's enforcement, in packages/, not on the desk.

Measured by leaves 1.2.2 and 1.2.3, driver-verified: `confirm=true`, `plan_id` and the 30-minute
expiry exist ONLY at kite-momentum-rebalancer/app/main.py:519-524. `client_id = plan_id:symbol`
is constructed at main.py:581. `PLAN_TTL` is mentioned 15 times under packages/ and compared
against a clock ZERO times. Retiring the desk deletes the enforcement, not just the code.

**This leaf adds NO route.** The route is 2.3, after C3.

- [x] G1: A pure predicate beside PLAN_TTL decides whether a plan is expired, given a clock
  CHECK: grep -rc "def plan_is_expired\|def is_expired" decile-blueprint/packages/core/src/baskfy_core/curated_plans.py
  EXPECT: /[1-9]/
  EVIDENCE: returns `1`. `plan_is_expired(*, issued_at, now) -> bool` at
  `decile-blueprint/packages/core/src/baskfy_core/curated_plans.py:57-105`, immediately below
  `PLAN_TTL` (line 54), with `plan_expires_at(issued_at)` beside it; `_expires_at_hint` now calls
  `plan_expires_at`, so the preview's `expires_at_hint` and the enforcement are one definition.
  Pure per law #1 — no state, no clock, no I/O; asserted by
  `test_plan_expiry.py::TestThePredicateIsPure`, which AST-strips docstrings and comments and
  then fails on `datetime.now(`/`time.time(`/`utcnow(` in the module's *code*.
  Total: `1800.0` seconds, asserted directly.

- [x] G2: A plan store in packages/ or services/ that can issue, look up and expire a plan_id
  EVIDENCE: `decile-blueprint/services/api/src/baskfy_api/plan_store.py` (341 lines).
  `PlanStore.issue(plan, *, tenant, now) -> StoredPlan` mints `plan-<uuid4>`;
  `.lookup(plan_id, *, tenant, now)` refuses `UnknownPlan` (404) / `PlanExpired` (410);
  `.authorize(plan_id, *, confirm, tenant, now)` adds `PlanNotConfirmed` (400) — the whole of
  `app/main.py:519-524` in one call. `.purge_expired(*, now)` bounds the map (the desk's `PLANS`
  dict grew for the life of the process).
  HOME, against law #1: the store is state + a clock, which core may not hold, so the *predicate*
  is core and the *store* is `services/`; `baskfy_api` already depends on `baskfy_core` (the
  predicate) and `baskfy_execution` (the mint). It never re-implements the TTL — asserted by
  `test_plan_store.py::test_expiry_is_the_core_predicate_and_not_a_second_copy_of_it`, which
  requires `plan_is_expired` in the code and forbids the literals `1800` and `minutes=30`.
  Clock is injected everywhere: `test_the_store_reads_no_ambient_clock` scans the code (not the
  prose) for `datetime.now(` / `time.time(` / `utcnow(`.

- [x] G3: Tests assert the SPEC — 30 minutes exactly, a plan at 29:59 is live and at 30:01 is
      not, an unknown plan_id is refused, and a re-presented plan cannot double-send
  EVIDENCE: 84 tests across three new files, all green, no `sleep` anywhere.
  `packages/core/tests/test_plan_expiry.py` (169 lines): `PLAN_TTL == timedelta(minutes=30)` and
  `total_seconds() == 1800`; 29:59 live, one microsecond before the deadline live, **30:00 exactly
  expired**, 30:01 expired. The 30:00 case is a recorded judgement call — the desk's `> 1800` kept
  the plan alive for that instant, this closes the interval (`[issued, issued+TTL)`), the stricter
  and safer reading. Also: a backwards clock does not invalidate a live plan, a future-stamped
  plan still dies a TTL after its own stamp, naive/aware mixing raises rather than guesses, and
  `expires_at_hint` is exactly where the predicate flips.
  `services/api/tests/test_plan_store.py` (420 lines): 29:59 live / 30:00 / 30:01 through the
  store; unknown id refused; empty id refused; another tenant's plan reads as UnknownPlan; the
  same user on a different broker_account_id is a different tenant; `confirm` refused for
  `"" "false" "True" "TRUE" "yes" "1" "true " "on"`; confirmation rescues neither an expired plan
  nor another tenant's; an expired plan answers 410 every time it is asked, not 410-then-404.
  Re-presentation: `TestARePresentedPlanIsIdempotent::test_the_second_confirmation_places_nothing_new`
  drives a real `OrderGateway` in DRY_RUN over a two-leg plan — first pass `["DRY_RUN","DRY_RUN"]`,
  second pass `["DUPLICATE","DUPLICATE"]`, journal holds exactly two `dry_run` lines whose
  `client_id`s equal the plan's. Idempotent, not double-sent.
  `packages/execution/tests/test_client_id_journal.py` (410 lines) covers the mint and the journal.
  DRY_RUN throughout; the broker doubles are plain objects and the repo-wide `block_network`
  fixture refuses every non-loopback socket.

- [x] G4: `client_id = plan_id:symbol` is MINTED in packages/, not merely consumed
  EVIDENCE: `decile-blueprint/packages/execution/src/baskfy_execution/client_ids.py` (new, 97
  lines): `mint_client_id(*, plan_id, symbol)`, `parse_client_id`, `CLIENT_ID_SEPARATOR`, all
  exported from `baskfy_execution.__init__`. Byte-for-byte the desk's `main.py:581` format —
  `mint_client_id(plan_id="PLAN1", symbol="RELIANCE") == "PLAN1:RELIANCE"` — plus the validation
  an f-string could not carry: empty half, embedded `:` (so `("a:b","c")` and `("a","b:c")` cannot
  mint one string for two orders), and embedded whitespace (so `"PLAN1 "` and `"PLAN1"` are not
  two keys for one plan). Symbol upper-cased: one NSE instrument, one key; safe because the id is
  a local idempotency key and a journal field, never sent to the broker.
  `StoredPlan.client_id_for(symbol)` / `.client_ids()` are the store's use of it, and
  `client_id_for` refuses a symbol the plan does not name (`SymbolNotInPlan`) — on the desk the
  mint sat inside the loop over `plan["orders"]` so the plan constrained the symbols structurally;
  once the mint is a callable, that has to be checked.

- [x] G5: The gateway journal records the client_id on every line — closing leaf 1.2.3's
      ABANDONed G4, which found a production journal cannot be reconciled to a plan
  EVIDENCE: `_journal(self, rec, *, client_id)` — keyword-only, **no default**, so a journal line
  added later cannot omit it — stamps `rec["client_id"]` on every write. All 20 call sites in
  `gateway.py` pass it (`grep -c "self._journal({"` = 20, `grep -c "client_id="` = 20), and
  `test_client_id_journal.py::test_structurally_every_journal_call_in_the_gateway_passes_one`
  re-checks that over the AST so an unexercised line still cannot ship without one.
  `cid` is now resolved at the TOP of `place()` and `place_gtt_stop()` instead of just above the
  idempotency check: the guard block and the risk block — the two lines an operator most needs to
  tie to a plan — used to be journalled with no id at all.
  Behavioural coverage: dry-run order, **live order** (1.2.3's finding was "in live as well as
  DRY_RUN"), risk refusal, armed GTT, refused GTT, cancelled GTT. `delete_gtt` gained an optional
  `client_id` (journal only, no part in idempotency — cancelling twice is not a double-send) and
  records an explicit `null` when the caller has no plan, so a reconciliation script can tell "no
  plan" from "field missing".
  Corroborating find, NOT fixed here (desk tree is outside this leaf's contract):
  `kite-momentum-rebalancer/scripts/friday_drill.py:265` counts "orders that reached a broker" by
  reading `entry.get("plan_id")` — a field the gateway has never written — so that check has
  always counted zero. With `client_id` on every line it becomes
  `parse_client_id(entry["client_id"])[0]`. Recorded in DECISIONS-MERGE and the status page.

- [x] G6: **Still no execute route anywhere.** This leaf builds enforcement, not a firing pin.
  CHECK: grep -rc "@router.post" decile-blueprint/services/api/src/baskfy_api/routers/kite.py
  EXPECT: 0
  EVIDENCE: returns `0`. No router file was touched by this leaf — the four files written are
  `packages/core/src/baskfy_core/curated_plans.py` (predicate),
  `packages/execution/src/baskfy_execution/client_ids.py` (mint),
  `packages/execution/src/baskfy_execution/gateway.py` (journal client_id only), and
  `services/api/src/baskfy_api/plan_store.py` (store), plus tests and docs. `plan_store.py`
  registers nothing: `test_plan_store.py::TestNoFiringPin` fails if its code names `@router.`,
  `APIRouter`, `fastapi`, `place_order`, `place_gtt`, `OrderGateway` or `kiteconnect`. The store
  authorises a plan and returns it; it cannot reach a broker. `PlanRefused.status` records the
  desk's 400/404/410 for the later route to read — naming a status is not registering a route.
  Neither desk tree was modified (`git status` shows no change under `kite-momentum-rebalancer/`
  from this leaf).

- [x] G7: full suite green, lint clean, no test weakened
  CHECK: cd decile-blueprint && timeout 1800 uv run pytest 2>&1 | tail -2
  EXPECT: /passed/
  EVIDENCE: `3256 passed, 1138 skipped in 108.15s (0:01:48)` — zero failures, exit 0.
  (An earlier run in this leaf showed `test_momentum_scan.py::test_the_bytes_are_stable` red;
  that is sibling leaf 2.1's RSI/volatility amendment mid-flight — it went green on its own once
  2.1 regenerated `fixtures/momentum_scan.csv`, and it also passed with this leaf's changes
  stashed. Not this leaf's, and untouched by it.)
  LINT: `ruff check` and `ruff format --check` clean on every file this leaf wrote; `mypy` clean
  on all three new/changed source modules (`Success: no issues found in 3 source files`). No
  `# type: ignore`, no `Any`, no swallowed exception — `test_no_escape_hatches.py` passes.
  NO TEST WEAKENED: no existing assertion was changed. `test_no_escape_hatches.py` gained a
  docstring only — its regex and its assertion are byte-identical, and the docstring argues
  *against* teaching the scanner to skip comments (the hatch is a comment; any exemption wide
  enough for prose about it is wide enough to hide one). The house convention was followed
  instead: a comment that must name the hatch writes it without the leading hash.
