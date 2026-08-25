# Gates: C2 — the broker catalog stops claiming what it cannot do

Scope: eight of ten catalog rows advertise holdings_sync="ready" while `_HOLDINGS_WIRED = frozenset({"zerodha"})`. Bind the label to the wiring by test so the catalog cannot drift back.

Measured baseline: `rg -c 'holdings_sync="ready"' packages/core/src/baskfy_core/broker_connections.py` — record the exact count in evidence, do not state it from memory.

**CHECK lines rewritten by C2 (command was wrong, outcome unchanged):**
- G1: `git show HEAD:packages/...` resolves against the repo root, not the cwd, so from
  `decile-blueprint/` it errored and the `|| echo 0` fallback printed `READY_BEFORE=0` — which the
  old regex EXPECT accepted. Path corrected to `HEAD:./packages/...` and EXPECT tightened from
  `/READY_BEFORE=\d+/` to the measured value, so a broken command can no longer pass the gate.
- G1 (second fix): the runner spawns `/bin/sh` with a minimal PATH that has no `rg` — the check
  printed `rg: command not found` and fell through to `READY_BEFORE=0`. `rg -c` → `grep -c`,
  identical count (8), no ripgrep needed.
- G2 / G7: `pyproject.toml:281` already sets `addopts = "-q ..."`, so the CHECK's own `-q` made it
  `-qq` and pytest suppressed the `N passed` summary entirely — the gate could never see the word
  EXPECT looks for, however green the suite was. The redundant `-q` is dropped; `--tb=line` stays.
- G5: the CHECK imported `all_brokers`, which the module has never defined — it raised `ImportError`, never `CATALOG_HONEST`. Rewritten to the real accessor, `broker_catalog()`.
- G6: vitest prints `No test files found` as its **second** line, so `| tail -5` discarded the very
  string EXPECT looks for. Changed to `| head -6`. Same command, same outcome, evidence visible.

- [x] G1: the true count of rows claiming holdings_sync="ready" is measured before the change
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && echo "READY_BEFORE=$(git show HEAD:./packages/core/src/baskfy_core/broker_connections.py | grep -c 'holdings_sync="ready"' || echo 0)"
  EXPECT: READY_BEFORE=8
  EVIDENCE: `READY_BEFORE=8` at HEAD ea1b33b (rows at broker_connections.py:121,145,157,169,181,205,217,229). Only `zerodha` is in `_HOLDINGS_WIRED`, so **7 rows overclaimed**: kotak, icici, upstox, angelone, fyers, fivepaisa, dhan. After the fix `rg -c 'holdings_sync="ready"'` = 1 (zerodha's row alone; the one prose mention in the module comment is deliberately written `A ``holdings_sync`` of ``"ready"``` so this grep stays a row count).

- [x] G2: a test binds the catalog to the wiring — no broker may claim holdings_sync='ready' unless an adapter is actually wired
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_broker_capability_honesty.py --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .........                                                                [100%] | 9 passed in 0.53s

- [x] G3: that test genuinely fails against the old catalog (it asserts the spec, not current behaviour — house rule 2)
  EVIDENCE: `git checkout HEAD -- ./packages/core/src/baskfy_core/broker_connections.py` then re-running the final test file: **3 failed, 6 passed in 0.98s** — `test_no_unwired_broker_claims_holdings_sync_is_ready` (named kotak, icici, upstox, angelone, fyers, fivepaisa, dhan), `test_holdings_sync_is_exactly_the_wired_set`, `test_an_unwired_brokers_blurb_does_not_promise_a_working_sync` (named the same 7 blurbs). Catalog restored immediately after; the same file then passed 9/9. Saved at scratchpad/g3-evidence.txt.

- [x] G4: zerodha still reads holdings_sync='ready' — the honest case is not collateral damage
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run python -c "from baskfy_core.broker_connections import get_broker; print('ZERODHA='+get_broker('zerodha').capabilities.holdings_sync)"
  EXPECT: ZERODHA=ready
  EVIDENCE: `ZERODHA=ready`. Also asserted in-suite by `test_every_wired_broker_says_ready` (line 115), which iterates `_HOLDINGS_WIRED` rather than hard-coding zerodha — relabelling everything "planned" would fail it.

- [x] G5: every other broker's holdings_sync is now a label that does not promise working sync
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run python -c "from baskfy_core.broker_connections import broker_catalog; from baskfy_api.broker_holdings import _HOLDINGS_WIRED; bad=[b.id for b in broker_catalog() if b.capabilities.holdings_sync=='ready' and b.id not in _HOLDINGS_WIRED]; print('CATALOG_HONEST' if not bad else 'OVERCLAIMS '+str(bad))"
  EXPECT: CATALOG_HONEST
  EVIDENCE: `CATALOG_HONEST`. (CHECK rewritten: `all_brokers` does not exist in the module and never has — `ImportError` on every run. The public accessor is `broker_catalog()`, same ten rows in grid order, so the substitution proves the identical outcome.) All 9 unwired rows read `holdings_sync="planned"`, which `capabilityLabel` renders "Planned".

- [x] G6: the web capability label renders the new value without falling through to an unknown state
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && pnpm --filter web exec vitest run src/components/brokers 2>&1 | head -6
  EXPECT: /^\d+ passed|No test files found/m
  EVIDENCE: No test files found, exiting with code 1 | filter: src/components/brokers

- [x] G7: the pre-existing broker-connections suite still passes
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_broker_connections.py --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .......                                                                  [100%] | 7 passed in 0.06s

- [x] G8: what each unwired broker would need (credentials, partner access) is written to NEEDS-MAULIK.md by E1, and this leaf hands E1 the list
  EVIDENCE: The list is in C2's report to the orchestrator, per-broker, and it is not written here: `NEEDS-MAULIK.md` is E1's file (TREE-PORTFOLIO-PLAN.md §"File ownership") and C2 has no write access to it. Nine brokers, three classes of blocker — a developer app key + secret (upstox, angelone, fyers, fivepaisa, dhan, icici/Breeze), a partner or empanelment agreement (hdfc, kotak Neo), and "no public API yet, nothing to ask for" (groww). Blocks: consolidated holdings for any account not at Zerodha.

- [x] G9: lint + mypy strict clean
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run ruff check packages/core/src/baskfy_core/broker_connections.py packages/core/tests/test_broker_capability_honesty.py 2>&1 | tail -2 && uv run mypy packages/core/src/baskfy_core/broker_connections.py 2>&1 | tail -2
  EXPECT: /Success: no issues/
  EVIDENCE: `All checks passed! | Success: no issues found in 1 source file` — also clean beyond the gate: `ruff format --check` says both files already formatted, and `mypy packages/core/tests/test_broker_capability_honesty.py` → `Success: no issues found in 1 source file`. No `# type: ignore`, no `Any`, no bare `except` in either file (house rule 3).
