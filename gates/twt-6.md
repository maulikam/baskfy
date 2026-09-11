# TW6 — the desk plan, `/twt/execute`, the GTT ratchet, and the six routes the runbook needs

**Plan:** `docs/twt/06-module-plan.md` § TW6 **and § TW6a**. **Spec:** `04` §§10, 11; `05` §2.
**Reads:** TW1's `plan.py`, TW4's `next_trigger`, TW5's sizing and counter.

**This is the module that touches orders.** `/CLAUDE.md`'s seven non-negotiables are the law:
never auto-execute; every buy gets a GTT stop the same session; CNC only; everything through the
gateway with `client_id = plan_id:symbol`; filter-rejected and excluded instruments untouchable.
**`BASKFY_TWT_EXECUTION_ENABLED` stays false and no auto-execute flag is added for this sleeve.**

> **Two repairs to the CHECK lines, and neither is a goalpost move.**
>
> **1. G2–G9 named the wrong tree, and the wrong-tree runs were false greens.** They ran
> `cd decile-blueprint`, and the desk is `kite-momentum-rebalancer`. It always has been:
> `POST /swing/execute` lives in `app/swing_desk.py`, `POST /vbt/execute` in `app/vbt_desk.py`,
> and their tests in `kite-momentum-rebalancer/tests/`. Every gate from G2 to G9 is about a
> **desk route**, so as written each one collected TWT tests belonging to other modules — G4's
> `naked` matched nineteen of TW1's and TW7's core tests and reported them green, which is worse
> than reporting nothing, because it says a route was proven that does not exist in that tree.
> The `cd` is corrected and the interpreter is the desk's own venv (`python` does not resolve on
> this machine; `.venv/bin/python` is what every other desk command uses). Nothing else changed:
> same `-k` expressions, same EXPECT.
>
> **2. G1 needs a live Postgres.** The evening plan is a database module and its tests carry
> `requires_db`; without `BASKFY_TEST_DATABASE_URL` exported they *skip*, and the first
> unattended run of this gate recorded `1 passed, 19 skipped` as a pass. A gate that goes green
> on nineteen skips is not a gate. Every G1 run below is against a real Postgres, on a scratch
> database (`baskfy_tw6`) rather than the shared `baskfy_test` — three sessions were contending
> for that one all evening and TW5 recorded the same workaround for the same reason.

- [x] G1: The evening plan writes `tw_plan` (source `EVENING`) with its lines and skips, and the
      morning plan rebuilds it as `MORNING` **re-sized and NOT re-detected** (`04` §11.3).
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tw6 uv run pytest -k "twt and (evening or morning) and plan" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `20 passed, 7622 deselected in 17.24s`.
  `services/worker/src/baskfy_worker/tasks/twt_evening.py` ships `run_twt_evening`, and
  `services/worker/tests/test_twt_evening.py` is 23 tests against a real database.
  **The plan row:** `source=EVENING`, `session_date=2026-09-10`, `gate=OPEN`,
  `sleeve_equity_inr=2500000.00`, `total_new_exposure_inr=250000.00`,
  `expires_at - built_at == 30 minutes` read from `TW_PLAN_TTL_MINUTES` (the schema's constant,
  not a number typed twice), and a 64-character `plan_hash`. **The line:** one `BUY_AT_OPEN`,
  2,500 shares (a tenth of ₹25 lakh at ₹100), `value_inr=250000.00`, `stop_price=80.00`,
  `state=PROPOSED`, `client_id` ending `:TWTCO` — no kind suffix, because `04` §10.4 gives that
  only to a GTT leg. **The skips:** five signals give three lines and two `SESSION_CAP`; a
  `SCAN_ONLY` row gives `BELOW_LIQUIDITY_FLOOR` (DECISIONS-TW **TW6.1** — VBT-1 drops those rows
  before building and this one does not, because a skip the planner never sees cannot be
  recorded); a shut gate gives `GATE_SHUT`; a ₹0 sleeve gives `NO_SLEEVE_CAPITAL`; a held name
  gives `ALREADY_HELD`. **The exits come first** in the stored order, and the plan emits **no**
  `SELL_AT_OPEN` even with a position marked far under its stop. **The morning:** a second
  `tw_plan` row with a **different** `plan_id` for the **same** `session_date`, the evening's
  still on disk, and `test_the_morning_resizes_and_does_not_redetect` doubles a held position's
  mark between the two builds — the entry line grows with the equity (2,500 → 2,600 shares) while
  `tw_signal_daily` still holds exactly the one row the evening read. A re-run of the **same**
  source replaces its own plan rather than duplicating it (house rule 7). A session with no
  `tw_breadth_daily` row plans nothing and says why.

- [x] G2: `POST /twt/execute` requires `confirm=true` and a `plan_id`; **an expired plan is 410 and
      a missing confirm is 400.** Plans expire in 30 minutes.
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests -k "twt and (expired or confirm) and execute" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `12 passed, 1982 deselected in 2.80s`. `app/twt_execute.py::_validate` answers, in the
  order a caller hits them: **400** on a missing or false `confirm` — *before anything is read*,
  so a form posted by accident costs no database work and no broker read; **404** on an unknown
  `plan_id` (and on a malformed one — `PgTwtStore.plan` refuses a non-uuid itself, because on
  Postgres that would otherwise be a driver error, a 500 where the contract says 404); **410** on
  an expired plan, with the lapse time in the message; **400** on a `SELL_AT_OPEN`
  (DECISIONS-TW **TW6.4**); **409** on a line that is not `PROPOSED`. Asserted both directly and
  **through the mounted route** (`tests/test_twt_desk.py::TestTheExecuteRoute`): a live confirm is
  200 with `SIMULATED`, a missing confirm is 400, an expired plan is 410. `store.orders == {}` on
  every refusal. The expiry comparison is `>=`, not `>` — DECISIONS-TW **TW6.6**: at the instant
  it expires, it is expired, which is the safe direction and is what makes `/twt/halt`'s plan
  expiry exact rather than nearly exact.

- [x] G3: **A stop never falls.** A `RAISE_GTT_STOP` at or below the resting trigger is `BLOCKED`,
      and one at or above the last price is `BLOCKED`.
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests -k "twt and (never_falls or stop_below or above_last)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `5 passed, 1990 deselected in 5.53s`. Five refusals, and **every one of them leaves
  the resting stop exactly where it is**: a new trigger *equal* to the resting one is refused
  (cancelling and re-arming the same level is a moment of nakedness for nothing); one *below* it
  is refused naming both numbers; the resting trigger is read as `max(stop_price, gtt_trigger)` —
  TW4.8's rule, restated on the surface that refuses a fall, so a raise to 95.00 against
  `stop_price=96.00`/`gtt_trigger=80.00` is blocked at **96.00**; a trigger at or above the last
  price is refused ("that would fire at once"); and a raise with **no** last price at all is
  refused rather than guessed. In all five the gateway records `cancelled == []` and
  `armed == []` — nothing was sent, and the old trigger is still resting. This is the third of the
  three places that say a stop never falls; the first is the `max` inside
  `baskfy_core.twt.exits.ratchet` and the second is `exit_lines`.

- [x] G4: **The delete-and-replace path's two failure halves.** A cancel that fails leaves the OLD
      stop resting and places nothing. A cancel that succeeds with an arm that fails leaves
      `gtt_id` null, records the intent, and reports the position **NAKED** — the state that can
      actually cost money, so it is named rather than swallowed.
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests -k "twt and (cancel_fails or arm_fails or naked)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `11 passed, 1983 deselected in 2.25s`. **The cancel fails:** outcome `BLOCKED`,
  `naked is False`, reason ends "the old stop at 80.00 is still resting and nothing was placed",
  the position still carries `gtt_id=551234` and `gtt_trigger=80.00`, `stop_price` is untouched,
  and the gateway's `armed` list is **empty** — nothing was placed, because two triggers sell the
  position twice when they fire. **The cancel succeeds and the arm fails:** outcome `BLOCKED`,
  `naked is True`, the reason names the position id and the word NAKED, `gtt_id` and `gtt_trigger`
  are **null**, and `stop_price` is **104.00** — the intent is recorded so the next re-arm rests
  the stop at the right level rather than at the old one. A third test walks the seam: after that
  failure, `naked_positions(store)` returns the line, which is what the 15:15 sweep and the page's
  red band both read. A fourth covers the same rule on the entry side — a fill whose GTT will not
  arm comes back `naked=True` with the position named. Both halves are driven through a gateway
  that fails on purpose, because a dry-run gateway always succeeds and these two states cannot
  otherwise be reached; every other test in the file uses the real one.

- [x] G5: **Every fill arms a GTT in the same request** (non-negotiable 4), and a fourth
      `BUY_AT_OPEN` confirm in one session is `BLOCKED (SESSION_CAP)`.
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests -k "twt and (arms_gtt or session_cap)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `3 passed, 1991 deselected in 2.36s`. `_apply_fill` writes the `tw_position`, the
  `tw_fill` and the GTT in **one request**: after a confirm the position carries a non-null
  `gtt_id`, `gtt_trigger=80.00` (20 % under the ₹100 fill, floored to the tick),
  `initial_stop=80.00`, `high_since=100.00`, and the fills are `["BUY"]`. The stop comes from the
  **fill**, not from the plan's preview — a confirm at ₹120 gives `entry_avg=120.00` and
  `initial_stop=96.00` (`04` §7.1, §5.3). The fourth entry of a session is `BLOCKED` with a reason
  beginning `SESSION_CAP`, the line is marked `REJECTED`, and `len(store.orders)` is still 3; the
  cap counts orders from **any** plan, which is the whole content of `04` §6.3.

- [x] G6: A second confirm of the same line is refused by the idempotency key
      (`client_id = plan_id:symbol`, non-negotiable 6) — a re-posted plan cannot double-send.
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests -k "twt and (idempot or double_send or client_id)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `4 passed, 1990 deselected in 2.40s`. A second confirm of the same line raises **409**
  and `len(store.orders)` stays 1 — refused *before* the gateway's own idempotency map is
  consulted, so a re-posted form costs no broker read. The order's `client_id` is exactly
  `{plan_id}:TWTCO` (no kind suffix), and a GTT leg's is `{plan_id}:TWTCO:ARM_GTT` — `04` §10.4's
  two spellings, minted by `baskfy_core.twt.plan.client_id_for`, which the evening job and the
  desk both call so the writer and the reader cannot disagree. The store round-trips the same key
  out of sqlite (`tests/test_twt_desk.py::test_a_plan_and_its_lines_round_trip`).

- [x] G7: **With the flag false, `place` is called only on the dry-run adapter — 0 orders reach a
      broker in the whole suite**, asserted by a spy rather than by reading the code.
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests -k "twt and (dry_run or no_broker or spy)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `3 passed, 1992 deselected in 4.71s`. **Three independent proofs of one sentence.**
  (a) `SpyGateway` wraps the real gateway and asserts on **every** `place`, `place_gtt_stop` and
  `delete_gtt` as it happens — not once at the end, so the guarantee does not depend on test
  order — that the status starts `DRY_RUN`; the closing test re-reads the shared tape and finds
  `("place", "DRY_RUN")` present and nothing outside the dry-run statuses. (b) `ExplodingKC`
  raises on `place_order`, `place_gtt`, `delete_gtt`, `cancel_order` and `instruments`, so a live
  path would have crashed long before the status was read. (c)
  `test_no_broker_client_method_is_ever_invoked_by_a_confirm` runs the longest path in the module
  — a confirm that fills and arms — against a counting broker client and asserts the recorder is
  **empty**: not "the call failed", but "no call was made". Beside them: `twt_gates()` is
  `dry_run=True, intraday_enabled=False, options_enabled=False` (non-negotiable 5, CNC-only
  unconditionally), `C.TWT_EXECUTION_ENABLED is False`, no `TWT_AUTO*` name exists in
  `app/config.py`, and the buy is `product="CNC"`, `order_type="MARKET"`, `side="BUY"`.
  **No test in either new file flips the flag.**

- [x] G8: **TW6a's six routes exist and are tested**, and `/twt/halt`'s four behaviours are each
      pinned — capital zeroed and audited, live plans expired, **protection never touched** (a
      resting GTT is still resting and `RAISE_GTT_STOP` still plans after a halt), and one line of
      output saying what it did.
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests -k "twt and (halt or reconcile or rearm or sweep_route)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `30 passed, 1964 deselected in 2.38s`. All six exist to the runbook's exact spellings
  and are mounted on `app.main`: `POST /twt/execute`, `POST /twt/halt`, `POST /twt/rearm`,
  `POST /twt/sweep`, `POST /twt/reconcile`, plus `GET /twt` and `GET /twt/data`; the sixth
  command, `make twt-plan DATE=… [SOURCE=EVENING|MORNING]`, is in `decile-blueprint/Makefile` over
  `baskfy_worker.twt_cli --plan`.
  **`/twt/halt`, behaviour by behaviour.** (1) `sleeve_capital_inr` → 0 with a `tw_config_audit`
  row carrying the previous value, so it is restored by reading a row rather than by remembering.
  (2) Every unexpired `tw_plan` is expired — a confirm of a plan that was live a moment earlier is
  **410**. (3) **Protection is never touched**, and it is pinned twice: at unit level
  (`test_halt_never_touches_protection`) and **through the mounted route** — after the halt the
  position still carries `gtt_id=551234`, `gtt_trigger=80.00`, `quantity_open=1000` and
  `state=OPEN`, and a `RAISE_GTT_STOP` confirmed *after* the halt still moves the trigger to
  104.00. A halted sleeve cannot **buy**: the same confirm of a `BUY_AT_OPEN` comes back
  `BLOCKED NO_SLEEVE_CAPITAL` with `store.orders == {}`, because `04` §10.5 re-sizes the line
  through the same rules the plan used. (4) The reply is one line, names the previous capital and
  ends "every resting GTT is untouched and the ratchet still works". A halt without `confirm=true`
  is 400 and changes nothing. **There is no line in `halt_sleeve` that could cancel a GTT.**
  Beside it: `rearm` arms a naked line and **refuses** one that already carries a trigger (two
  triggers sell twice what is held) and one whose stop is at or above the last price ("a GTT that
  would fire on the next tick is not protection"); the sweep re-arms what is naked, is idempotent
  on the day, and writes what it could not fix into `tw_session.naked_at_1515`; `reconcile`
  attaches a hand-armed GTT to its position and **leaves a trigger it cannot match alone**,
  counted — the safe direction for a discrepancy is always *more* protection.

- [x] G9: Every POST to the desk carries an `Origin` header and one without it is refused —
      `DeskSecurity` 403s a state-changing request that cannot name its origin, and the runbook's
      phone commands depend on it.
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests -k "twt and origin" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `7 passed, 1987 deselected in 2.25s`. All five POSTs — `/twt/execute`, `/twt/halt`,
  `/twt/rearm`, `/twt/sweep`, `/twt/reconcile` — answer **403** with "did not come from the desk"
  when the header is absent, and `sleeve_capital_inr` is still `2500000.00` afterwards: the halt
  in particular did not happen. A POST carrying `Origin: http://evil.example` is refused the same
  way. `GET /twt` and `GET /twt/data` are 200 without one, because a safe method is not a state
  change. **This needed a fixture that takes the header back off**: `tests/conftest.py` adds
  `Origin` to every `TestClient` by default (a browser sends it on every POST, and route tests are
  about routes), so a test *about the header* has to `client.headers.pop("origin")` — otherwise it
  would assert a refusal the harness had already prevented. That is exactly the trap
  FIRST-LIVE-MORNING §1 warns a person about at 09:15, one layer down.

- [x] G10: **The desk suite's own non-negotiable tests are still green** and the swing book, the
      weekly book and R1–R4 are untouched.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && .venv/bin/python -m pytest tests -q 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `1977 passed, 17 skipped, 174 warnings, 12 subtests passed in 86.58s`. The baseline
  before this module was `1883 passed, 17 skipped`; the 94 new tests are TW6's two files and
  nothing else moved. **One pre-existing test went red and was fixed rather than worked around:**
  `test_regime_data.py::test_every_env_var_the_code_reads_is_documented` scans `app/**.py` for
  `os.getenv("…")` and requires every name to appear in `.env.example`. Adding
  `BASKFY_TWT_EXECUTION_ENABLED` to `app/config.py` broke it, and the fix is the one the test
  exists to force: the variable is documented in `.env.example`, false, with the conditions
  `02` §3 puts in front of a flip and the sentence that there is no `BASKFY_TWT_AUTO_EXECUTE`.
  Outside the new files the diff is four things: the router mount in `app/main.py`, the flag in
  `app/config.py`, its paragraph in `.env.example`, and the new template. No file of the swing
  book, the weekly book, VBT-1 or R1–R4 is touched.

- [x] G11: Both Python suites green and `make lint` clean.
  CHECK: cd decile-blueprint && uv run ruff check . 2>&1 | tail -2 && uv run mypy --strict 2>&1 | tail -2
  EXPECT: /Success|All checks passed/
  EVIDENCE: `All checks passed!` and `Success: no issues found in 644 source files`.
  `uv run ruff format --check .` is clean after one file this module wrote was reformatted
  (`750 files already formatted`). The desk suite is G10's `1977 passed, 17 skipped`. The data
  plant's suite is recorded under G1 for the TWT subset and run whole with
  `BASKFY_TEST_DATABASE_URL` pointed at the scratch database; TW6's 23 new worker tests are
  included and no test outside `test_twt_evening.py` changed.

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit. -->
