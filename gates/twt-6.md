# TW6 — the desk plan, `/twt/execute`, the GTT ratchet, and the six routes the runbook needs

**Plan:** `docs/twt/06-module-plan.md` § TW6 **and § TW6a**. **Spec:** `04` §§10, 11; `05` §2.
**Reads:** TW1's `plan.py`, TW4's `next_trigger`, TW5's sizing and counter.

**This is the module that touches orders.** `/CLAUDE.md`'s seven non-negotiables are the law:
never auto-execute; every buy gets a GTT stop the same session; CNC only; everything through the
gateway with `client_id = plan_id:symbol`; filter-rejected and excluded instruments untouchable.
**`BASKFY_TWT_EXECUTION_ENABLED` stays false and no auto-execute flag is added for this sleeve.**

- [ ] G1: The evening plan writes `tw_plan` (source `EVENING`) with its lines and skips, and the
      morning plan rebuilds it as `MORNING` **re-sized and NOT re-detected** (`04` §11.3).
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (evening or morning) and plan" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G2: `POST /twt/execute` requires `confirm=true` and a `plan_id`; **an expired plan is 410 and
      a missing confirm is 400.** Plans expire in 30 minutes.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (expired or confirm) and execute" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G3: **A stop never falls.** A `RAISE_GTT_STOP` at or below the resting trigger is `BLOCKED`,
      and one at or above the last price is `BLOCKED`.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (never_falls or stop_below or above_last)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G4: **The delete-and-replace path's two failure halves.** A cancel that fails leaves the OLD
      stop resting and places nothing. A cancel that succeeds with an arm that fails leaves
      `gtt_id` null, records the intent, and reports the position **NAKED** — the state that can
      actually cost money, so it is named rather than swallowed.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (cancel_fails or arm_fails or naked)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G5: **Every fill arms a GTT in the same request** (non-negotiable 4), and a fourth
      `BUY_AT_OPEN` confirm in one session is `BLOCKED (SESSION_CAP)`.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (arms_gtt or session_cap)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G6: A second confirm of the same line is refused by the idempotency key
      (`client_id = plan_id:symbol`, non-negotiable 6) — a re-posted plan cannot double-send.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (idempot or double_send or client_id)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G7: **With the flag false, `place` is called only on the dry-run adapter — 0 orders reach a
      broker in the whole suite**, asserted by a spy rather than by reading the code.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (dry_run or no_broker or spy)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G8: **TW6a's six routes exist and are tested**, and `/twt/halt`'s four behaviours are each
      pinned — capital zeroed and audited, live plans expired, **protection never touched** (a
      resting GTT is still resting and `RAISE_GTT_STOP` still plans after a halt), and one line of
      output saying what it did.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (halt or reconcile or rearm or sweep_route)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G9: Every POST to the desk carries an `Origin` header and one without it is refused —
      `DeskSecurity` 403s a state-changing request that cannot name its origin, and the runbook's
      phone commands depend on it.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and origin" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G10: **The desk suite's own non-negotiable tests are still green** and the swing book, the
      weekly book and R1–R4 are untouched.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && python -m pytest tests -q 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G11: Both Python suites green and `make lint` clean.
  CHECK: cd decile-blueprint && uv run ruff check . 2>&1 | tail -2 && uv run mypy --strict 2>&1 | tail -2
  EXPECT: /Success|All checks passed/
  EVIDENCE: pending

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit. -->
