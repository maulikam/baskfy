# Gates: 1.2.3 DRY_RUN Friday drill through Baskfy's gateway

Scope: prove end-to-end that Baskfy can produce a rebalance plan and drive it through its own
gateway including GTT, with DRY_RUN=true and zero orders reaching a broker. Depends on 1.2.1.

**G1's CHECK was rewritten, and why.** The planned entry point was
`python -m baskfy_worker.tasks.desk --drill`. The drill cannot live there:
`services/worker/tests/test_desk_tasks.py::test_the_scheduled_jobs_cannot_reach_the_order_path`
reads that module's source and asserts it contains none of `place_order`, `OrderGateway`,
`baskfy_execution` or `execute(` — the scheduled desk jobs are collection only, and
`packages/execution` is the only path to an order. Putting a gateway drill in `desk.py` would have
required weakening that test, which the contract forbids and which would have been the wrong trade
anyway. The drill therefore lives at **`tools/friday-drill.py`** with a one-command runner at
**`tools/friday-drill.sh`**, and `desk.py` was left untouched. Every CHECK below names what
actually exists.

**Two gates are ABANDONED (G4, G5).** Both are cases leaf 1.2.2 predicted: the enforcement they
describe does not exist anywhere in `packages/` or `services/`, and building it is item 1, which
Maulik has not authorised. The drill measures the absence on every run and reports it as an OPEN
gap rather than asserting it away, so each gate's CHECK is left in place and flips the day the gap
closes. Reasons are at the bottom of this file.

**Every assertion was shown able to fail.** `--mutate {skip-a-stop, wrong-client-id,
touch-the-broker, live-setting}` injects one defect each; every mutation produces exactly one FAIL
and exit 1. All four are inert with respect to a real account — the worst calls the spy, and
`live-setting` feeds its offending line to the parser as a string rather than writing it to a
runnable file.

- [x] G1: The drill runs to completion from a single command
  CHECK: cd decile-blueprint && DRY_RUN=true timeout 900 uv run python ../tools/friday-drill.py --drill 2>&1 | tail -2
  EXPECT: /drill: PASS - \d+ of \d+ checks passed/
  EVIDENCE: `drill: PASS - 25 of 25 checks passed`, one command, ~8s, exit 0. The run covers:
  `baskfy_core.basket.build_plan` over a synthetic 21-name scan and a 6-position book -> 15 legs, 14
  with a non-zero delta -> sells-first-then-buys through `OrderGateway.place` -> simulated fills (one
  partial) -> 12 GTT stops through `OrderGateway.place_gtt_stop` -> 1 stale trigger through
  `delete_gtt` -> the whole plan re-posted. Plus guard probes (SGB, inverted trigger, NFO option
  under CNC, MIS with INTRADAY_ENABLED off) on a separate gateway and journal, an unfilled-buy probe,
  and a self-audit. `ruff check` clean and `mypy --strict` clean on the file;
  `packages/execution/tests` + `services/worker/tests` still green (exit 0). `tools/friday-drill.sh`
  is the same run with DRY_RUN exported and the cd done for you.

- [x] G2: Zero orders reached a broker — asserted, not assumed
  CHECK: cd decile-blueprint && DRY_RUN=true timeout 900 uv run python ../tools/friday-drill.py --drill 2>&1 | grep -E "A5 the broker was never called|A6 the journal holds no post-broker"
  EXPECT: /\[PASS\] A5 the broker was never called: SpyBroker recorded 0 attempts/
  EVIDENCE: Two independent witnesses. (1) `[PASS] A5 the broker was never called: SpyBroker recorded
  0 attempts` — SpyBroker implements place_order, modify_order, cancel_order, place_gtt, modify_gtt,
  delete_gtt, instruments, quote, ltp, holdings; each APPENDS to a counter and THEN raises, so even a
  caught exception leaves the trace. (2) `[PASS] A6 the journal holds no post-broker event: 27
  journal lines, events={'dry_run': 14, 'gtt_dry_run': 12, 'gtt_dry_run_delete': 1}` — none of
  placed/rejected/error/gtt_placed/gtt_error/gtt_deleted/gtt_delete_error, which are the only events
  written after a broker has answered. (3) `[PASS] A7`: all 41 results are DRY_RUN / DRY_RUN_GTT /
  DRY_RUN_GTT_DELETE / DUPLICATE. `instruments()` was never called either, so the live tick-snap path
  was never entered. Proven able to fail: `--mutate touch-the-broker` swallows one spy refusal and A5
  reports `recorded 1 attempts: ['place_order']`, exit 1.

- [x] G3: Every simulated buy produced a simulated GTT stop; count of buys equals count of GTTs
  CHECK: cd decile-blueprint && DRY_RUN=true timeout 900 uv run python ../tools/friday-drill.py --drill 2>&1 | grep -E "A8 buys simulated|A11 every trigger is inside"
  EXPECT: /\[PASS\] A8 buys simulated == stops simulated: buy legs sent=12, of which filled>0=12, GTT stops simulated=12; zero-fill legs=none; symbol sets match/
  EVIDENCE: `[PASS] A8 buys simulated == stops simulated: buy legs sent=12, of which filled>0=12, GTT
  stops simulated=12; zero-fill legs=none; symbol sets match` — counts AND set equality, so a missing
  stop cannot be cancelled out by a spurious one. Four more checks reinforce it: A9, all 12 arming
  attempts returned DRY_RUN_GTT (none BLOCKED or errored); A11, every trigger sits inside [8%, 12%]
  below its reference price and the gateway modified none of them; A10, stops are sized to the FILL
  not the plan (GOLF planned=441, filled=264, stop_qty=264 — the 18 Aug 2026 over-cover bug); A21, an
  unfilled buy is armed with nothing (DELTA filled 0, 11 stops for the other 11 — the PARAS bug).
  Stops are armed AFTER fills, which is the desk's own sequence (`main.py:611-623`: arming at execute
  time sizes triggers from planned quantities). A4 separately recomputes all 14 plan stops longhand
  from `clamp(ann_vol/sqrt(52)*2.2, 8%, 12%)` — the momentum-rebalance skill's formula, transcribed
  rather than imported so the comparison is not circular — with zero mismatches. Proven able to fail:
  `--mutate skip-a-stop` gives `GTT stops simulated=11 ... symbol sets DIFFER by ['DELTA']`, exit 1.

- [ ] G4: The journal recorded every simulated action with `client_id = plan_id:symbol`
  CHECK: cd decile-blueprint && DRY_RUN=true timeout 900 uv run python ../tools/friday-drill.py --drill 2>&1 | grep -E "A15 one journal line|G4 the journal line names"
  EXPECT: /\[CLOSED\] G4 the journal line names the client_id/
  EVIDENCE: ABANDONED — reason below. Measured every run: `[OPEN] G4 ... 0 of 27 journal lines carry
  a client_id field`. What does hold, and is proven: `[PASS] A15 one journal line per simulated
  action: orders 14/14, stops 12/12, cancels 1/1`; `[PASS] A12 14/14 order ids are
  DRY-<plan_id>:<symbol>`; `[PASS] A13 replay of 14 legs returned ['DUPLICATE'], dry_run journal
  lines 14 before the replay, 14 after`; `[PASS] A14` the 12 stops reused the identical
  `plan_id:symbol` and none came back DUPLICATE of the buy that created it — 1.2.1's separate GTT
  idempotency map, exercised rather than assumed. A12 proven able to fail: `--mutate wrong-client-id`
  gives `0/14 order ids are DRY-<plan_id>:<symbol>`, exit 1.

- [ ] G5: A plan older than 30 minutes is refused (non-negotiable #1's expiry)
  CHECK: cd decile-blueprint && DRY_RUN=true timeout 900 uv run python ../tools/friday-drill.py --drill 2>&1 | grep -E "G5 a plan older than 30 minutes"
  EXPECT: /\[CLOSED\] G5 a plan older than 30 minutes is refused/
  EVIDENCE: ABANDONED — reason below. Measured every run by parsing all five source roots:
  `[OPEN] G5 ... PLAN_TTL/expires_at_hint appears at 15 sites in packages/ and services/, of which 0
  compare it against a clock: NONE - the expiry is stamped, never enforced`. The probe is AST-based
  (`ast.Compare` nodes), not grep: the grep version reported
  `def _expires_at_hint(now: dt.datetime) -> dt.datetime:` as enforcement because a return arrow
  contains a greater-than sign, and would have closed this gate on a function signature.

- [x] G6: `DRY_RUN=false` was never set at any point in this leaf
  CHECK: cd decile-blueprint && DRY_RUN=true timeout 900 uv run python ../tools/friday-drill.py --drill 2>&1 | grep -E "A24 no file this leaf owns|A23 the drill refuses to start"
  EXPECT: /\[PASS\] A24 no file this leaf owns can set a live DRY_RUN: parsed friday-drill.py, friday-drill.sh; live dry_run settings=none/
  EVIDENCE: `[PASS] A24 no file this leaf owns can set a live DRY_RUN: parsed friday-drill.py,
  friday-drill.sh; live dry_run settings=none` — an AST scan of the .py for any `dry_run=` keyword or
  assignment that is not the literal True, plus a comment-stripped scan of the .sh for any `DRY_RUN=`
  assignment with a live value. Parsed, not grepped: the grep version counted the docstring sentence
  explaining why nothing can return a live value, and a check that fails on its own explanation
  teaches you to delete the explanation. Reinforced by `[PASS] A23` (the drill refuses to start under
  false/0/no/off in any case — tested as a pure function of the string, so no shell in this leaf ever
  had to set it) and `[PASS] A22` (`ProductGates()` defaults to
  dry_run=True/intraday_enabled=False/options_enabled=False, and the gates callable handed to the
  gateway is a closure over nothing that returns dry_run=True unconditionally, so the environment can
  only make it safer). Every command run in this leaf used DRY_RUN=true; `friday-drill.sh` exports it.
  Proven able to fail: `--mutate live-setting` feeds `export DRY_RUN=false` to the same parser AS A
  STRING — never written to a runnable file — and A24 reports
  `['mutant-runner.sh:2 export DRY_RUN=false']`, exit 1.

ABANDON: G4 The gateway writes a journal line for every simulated action but never writes the
client_id onto it. `gateway.py`'s `dry_run`, `placed`, `gtt_dry_run`, `gtt_placed`,
`gtt_dry_run_delete` and `gtt_deleted` branches all journal symbol/side/qty/price and omit the id
they deduplicated on, in DRY_RUN and in live alike — so a journal cannot be reconciled to a plan by
client_id, and the drill measures 0 of 27 lines carrying the field. Everything else G4 asks for
holds and is proven by the drill (one line per simulated action, exact; `client_id = plan_id:symbol`
constructed for every leg; honoured by both idempotency maps; visible in each order's
`DRY-<plan_id>:<symbol>`; a re-posted plan returning DUPLICATE for all 14 legs while adding no
journal line). Closing the gap is a small change inside
`packages/execution/src/baskfy_execution/gateway.py` — add the client id to the six `_journal` calls
on the order and GTT paths — which this leaf does not own; sibling 1.2.1 does. The drill's
`[OPEN] G4` line turns `[CLOSED]` the day it lands.

ABANDON: G5 No code under `packages/` or `services/` refuses a stale plan, so there is nothing to
exercise. `baskfy_core.curated_plans` defines `PLAN_TTL = 30 minutes` and stamps `expires_at_hint`
on every plan it builds; the drill parses all five source roots and finds 15 mentions of those two
names and zero comparisons of either against a clock. The `confirm=true` gate, the plan lookup and
the 30-minute expiry live only at `kite-momentum-rebalancer/app/main.py:519-524`, which desk
retirement deletes; the client_id is built two lines further down at `main.py:581`. Baskfy's own
desk router (`services/api/src/baskfy_api/routers/desk.py`) is read-only — 576 lines of GET
endpoints — and has no execute route at all. Where the enforcement would have to live: a
`POST /execute` in `services/api` holding a plan store, refusing `confirm != "true"` (400), an
unknown plan (404) and `now - created_at > PLAN_TTL` (410), with the pure predicate in
`baskfy_core.curated_plans` beside `PLAN_TTL` so it is testable without the API. Building it is
item 1 of the retirement plan and is not authorised.
