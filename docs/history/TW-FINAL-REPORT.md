# Baskfy — the three-weeks-tight sleeve, code complete

**12 September 2026.** TW0 through TW10, branch `developer`. The sleeve is built, both trees are
green, and **nothing has traded**: `BASKFY_TWT_EXECUTION_ENABLED` is false in every environment,
`tw_config.sleeve_capital_inr` is ₹0, and the only orders that exist anywhere are simulated ones
in a drill's journal. Three documents matter more than this one:

* **[`NEEDS-MAULIK.md`](NEEDS-MAULIK.md) § TWT** — T1, T2 and T3, the three things only your hands
  can supply, and T4, the one thing to expect rather than fix.
* **[`docs/twt/STATUS.md`](docs/twt/STATUS.md)** — every module's numbers and its "did NOT do".
* **[`docs/twt/DECISIONS-TW.md`](docs/twt/DECISIONS-TW.md)** — every call made without you, tagged
  `⚠ UNREVIEWED`.

**Where a number carries a command, that command was run on 12 Sep 2026 and the number is its
output**, from the repo root unless the command says otherwise. The *Built* table below carries no
commands: its one-line descriptions are each module's own gate file summarised, and the gate files
are where that evidence lives. Where a number contradicts an older document, the number is the
later fact and the document is the stale half.

## Where it stands

| | | measured by |
|---|---|---|
| Modules | **TW0 → TW10**, 14 ledger rows in `gates/twt-root.md` | `grep -cE '^- \[[ x]\] R' gates/twt-root.md` |
| Gate files | **14 files, 142 gates**, 0 incomplete | `python3 tools/gates/ledger.py gates/twt-*.md --exclude gates/twt-10.md:G8` |
| `decile-blueprint` suite (core + providers + execution + api + worker, `test_load` deselected) | **7,640 passed, 6 skipped, 3 deselected**, 19 min 58 s, exit 0** | `cd decile-blueprint && BASKFY_TEST_DATABASE_URL=<baskfy_test> uv run pytest packages/core packages/providers packages/execution services/api services/worker -p no:cacheprovider --deselect services/api/tests/test_load.py` |
| Desk suite | **1,984 passed, 17 skipped**, 12 subtests, 72 s | `cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests -q` |
| Lint, decile tree | **clean** — ruff and mypy over 645 source files | `cd decile-blueprint && uv run ruff check . && uv run mypy` |
| The safety properties | **21 passed**; under mutation (the flag flipped true) **7 of 21 go red** | `cd decile-blueprint && uv run pytest packages/core/tests/test_twt_safety_properties.py` |
| The DRY_RUN drill | `signals 1, confirms 2, fills 1, ratchets 1`, trigger **80.00 → 104.00**, sweep re-armed 1 and left 0 naked, **0 orders reached a broker** | `cd decile-blueprint && uv run python ../tools/twt/drill.py --database-url <baskfy_drill>` |
| Orders that reached a broker, in any of this | **zero** — every line of the journal is a dry run | `grep -cv dry_run decile-blueprint/data/outputs/twt_orders_journal.jsonl` → `0` |
| The execution flag | **false everywhere**; no assignment to true exists in either tree | `grep -rn "BASKFY_TWT_EXECUTION_ENABLED" --include=*.py --include=*.example --include=*.ts decile-blueprint kite-momentum-rebalancer \| grep -iE "=\s*true\|default.?=.?True" \| wc -l` → `0` |
| The sleeve's capital | **₹0**, written explicitly rather than defaulted | `seed_twt_config` in `services/api/src/baskfy_api/seed.py` |
| Schema | **13 `tw_` tables**, migration `0041_twt` (the downgrade round-trip is TW3's evidence, not re-run today) | `psql -d baskfy_test -tAc "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'tw\_%'"` |
| The pure core | **12 modules** under `baskfy_core/twt/` plus the package `__init__`, and **29 `test_twt_*` modules** across both trees | `ls decile-blueprint/packages/core/src/baskfy_core/twt/*.py \| wc -l` → `13` |
| Decisions taken without you | **70**, every one tagged `⚠ UNREVIEWED` (the two counts agree, so none is untagged) | `grep -cE '^#+ .*TW[0-9]+\.[0-9]+' docs/twt/DECISIONS-TW.md` and `grep -cE '^#+ .*⚠ UNREVIEWED' …` |

## Built

| Module | Commit | One line |
|---|---|---|
| TW0 | `5056aa1` | The pack: ten documents, eight pre-taken decisions, the run's root gate file, and one correction pushed back into the research note |
| TW1 | `9637e86` | `baskfy_core.twt` — the pure core, law 1 asserted; cross-checked against the research's own scanner at 9,600 cells, 0 mismatches |
| TW2 (harness) | `b299c35` | The golden harness, and it confirms the core matches the study stock-day for stock-day |
| TW3 | `a2bf343` | The thirteen `tw_` tables, `0041_twt`, the seed at ₹0, four bounds, one of them a floor |
| TW8 | `104d93b` | The sleeve's pages, built against the contract before the data existed |
| TW10 (runbook) | `511e31f` | The first live morning, written before the code, and it found a missing stop switch |
| TW2 (engine), TW4, TW5, TW6, TW7, TW9 | `8b5ef41` | Six modules in one commit — see the note below |
| TW10 (safety) | this commit | The property test over every route and every task, the drill, and the root ledger filled |

**The convention was broken once, and it is worth saying so rather than tidying it away.** The root
`CLAUDE.md` asks for one commit per module with a message in a complete sentence. `8b5ef41` is
named `twt modules` and carries six of them across 105 files. It cannot be split now without
rewriting history that is already the parent of later work, so the mapping above is the record
instead. Every module's own gate file still carries its own evidence, which is what a reader
actually needs; what was lost is the ability to revert one module without reverting five.

Supporting commits in the same run, none of them a module: `a72bbfb` (three tests had been red for
days, two of them safety guards), `3c00393` (the root `CLAUDE.md` said the web app's portfolio
marks were live Kite quotes and they never have been), `9beedb0` (house rule 5's warning named a
violation `c4c424f` had already fixed), `983a66d` (thirty-one gate checks in this run could never
have passed), `30390cc`, `eed34a2`, `c4e4dc5` (the root ledger, and TW0's four gates that were
never actually run).

## The real-money gate — `docs/twt/02` §3, with the evidence

§3 numbers six items. **Items 1–5 are preconditions for the flip; item 6 governs the first ten
entries after it.** `NEEDS-MAULIK.md` § TWT calls them "five conditions", which is right about the
preconditions and is why the sixth is listed separately here.

| | Condition | Evidence |
|---|---|---|
| 1 | **TW10 green** — a property test proves no TWT path reaches `OrderGateway.place` with the flag false, and the full drill produces a simulated fill, GTT and ratchet with 0 orders reaching a broker | `gates/twt-10.md` 8/8 · 21 safety-property tests · drill output above. *It was 7/8 until 12 Sep: G8 asserts the whole ledger and could not be filled until `gates/twt-root.md`'s seven pending rows were run* |
| 2 | **A green tree** — both suites pass and the neighbours still run | the two suite rows above · `gates/twt-10.md` G5 reads `0/0`: no neighbour core file changed and not one line was deleted from the desk's `app/` |
| 3 | **The backtest on the page** — run over the plant's own bars, on `/twt/backtest` beside the research numbers, with `01` §8's caveats verbatim and the drift flag | `gates/twt-9.md` 8/8 · **and it is FLAGGED**, see the caveats below |
| 4 | **The written runbook** — `docs/twt/FIRST-LIVE-MORNING.md`, down to the Kite login before 09:00 and how to stop the sleeve in one command | `gates/twt-10-runbook.md` 8/8, re-run today |
| 5 | **Your own flag flip** | **not done, and not an agent's to do.** No delegation exists for this sleeve |
| 6 | Half size for the first ten live **entries**, applied at plan time | built and tested; the counter has never counted, because it moves only on a live fill |

**Conditions 1 through 4 carry evidence today. Condition 5 is yours.**

## The DRY_RUN drill — the sentence to look for the night before

`tools/twt/drill.py` runs a whole session against a throwaway database: the evening plan, the
morning rebuild, a confirm on every line, a fill, a GTT armed the same session, a new high, the
ratchet that cancels the resting trigger and arms a higher one, a deliberately planted naked line,
and the 15:15 sweep that finds and fixes it.

```
cd decile-blueprint && uv run python ../tools/twt/drill.py \
  --database-url postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_drill
```

Measured today:

| | |
|---|---|
| mode | `DRY_RUN` |
| signals / confirms / fills / ratchets | 1 / 2 / 1 / 1 |
| the ratchet | trigger **80.00 → 104.00**, the old GTT cancelled and a new one armed |
| the sweep | `naked_before=1, rearmed=1, naked=0` |
| `naked_at_1515` | 0 |
| the broker | `calls: 0  []` |
| the last line | **`0 orders reached a broker.`** |

**The drill asserts its own steps.** Its first version printed `fill -> None`,
`ratchet -> BLOCKED` and `sweep -> naked_before=0` and then declared a whole session had run:
three no-ops under a success sentence. Each step now raises `DrillFailure` if it did not fire.

**Re-verified by mutation on 12 Sep 2026**, and the first attempt at the mutation was itself wrong,
which is worth the two lines. `NEW_HIGH` is ₹130.00 and the resting trigger is ₹80.00. Lowering it
to **₹100.50** changed nothing — 20 % under ₹100.50 is ₹80.40, still above ₹80.00, so the ratchet
still had something to do and the drill still passed. It takes **₹100.00**, where the raised stop
lands exactly on the resting trigger and `04` §7.2 refuses it, to kill the step: the drill then
stops at step 5 with `ratchets=0` and **exits 1**. A mutation that does not mutate is the same
failure as a gate that cannot fail.

## What this run found broken in its own ledger

This is the part worth reading twice, because it is about how the gates were being kept rather
than about the sleeve.

1. **Thirty-one `CHECK:` lines could never have passed** (`983a66d`). Every one ran
   `uv run pytest … -q` while `pyproject.toml` already sets `addopts = "-q"`. Two `-q` make `-qq`,
   which suppresses the `N passed` line entirely, so `EXPECT: /passed/` was unsatisfiable against
   any result, green or red.
2. **Four more in `gates/twt-10.md` alone.** G2's grep counted the tree's own *prohibitions*
   ("THERE IS NO BASKFY_TWT_AUTO_EXECUTE") as offenders; G5's diff counted TWT's own new desk files
   as damage to a neighbour; G6 invoked `tools.twt.drill` as a module path that resolves to a
   package with no `twt` in it; and G5's **repair** carried `EXPECT: /^0\/0$/`, which cannot match
   because a checker tests its regex against `stdout + "\n" + stderr` and an un-`m`-flagged `$`
   binds only before the final newline. The command's answer had been written in by hand. The
   EXPECT beside it had never been run.
3. **The root ledger's four module rows expected a string the checker never prints.** R6, R7, R9
   and R10 each ran `gate-check.mjs` on a module's file with `EXPECT: /0 unchecked/`; the checker
   says `ALL MET (11 met)` or `UNMET: 3`. Underneath the wrong string was a wrong question — see
   the note on `rerun.py` below. That makes **eight** unrunnable checks found in this module alone,
   on top of the thirty-one.
4. **`gate-check.mjs` drops a lone file argument.** Its filter is
   `args.filter((a, i) => !a.startsWith("--") && i !== tIdx + 1)`; with no `--timeout` present
   `tIdx` is `-1`, so the predicate excludes index 0 — the only argument there is. The file list
   falls back to every `gates/*.md` in the tree, and because `gates/twt-root.md` invokes the
   checker itself, it recurses without bound — five nested levels within two minutes, before it
   was killed. `--status <file>` or `--timeout N <file>` dodges it; the bare file does not. **This is in the unlazy skill, outside
   this repo**, and is reported rather than patched here.
5. **A gate cannot be its own evidence.** `gates/twt-10.md` G8 asserts that every gate file in this
   run is complete, and its own `EVIDENCE:` reads `pending` until it passes — so scanning itself it
   always found one pending gate and could never go green. `tools/gates/ledger.py` takes
   `--exclude FILE:GATE_ID` and **prints the exclusion in its summary line**, so what is not being
   asserted is visible rather than hidden.

Two small tools came out of this and live in `tools/gates/`:
`rerun.py` re-executes every `CHECK` in a gate file and verifies each against its own `EXPECT`
(which is what a parent row needs, since the checker only re-runs gates it already believes unmet),
and `ledger.py` reports which files are incomplete.

## Three commands in the runbook did not work, and one of them was the important one

`docs/twt/FIRST-LIVE-MORNING.md` is a condition of the gate, so it was re-read command by command
rather than taken as green because its gate file was. Three were wrong:

| Where | Was | Now |
|---|---|---|
| §2.1, the DRY_RUN drill | `cd <repo root> && uv run python tools/twt/drill.py` — and there is no `pyproject.toml` at the repo root, so `uv run` resolves a bare environment and the drill dies on `ModuleNotFoundError: No module named 'sqlalchemy'` before doing anything. It also passed no `--database-url`, which the drill cannot run without | run from `decile-blueprint/`, with the scratch database created first |
| §9.2, the 15:15 sweep | the same `uv run`-from-the-root fault | run from `decile-blueprint/` |
| §2.3, `make twt` | tagged `[NOT YET REAL — TW4]` | `[REAL]` — it has existed since TW4 |

**The drill one is the important one.** It is the single command in that file a person runs cold,
at night, alone, and the sentence it prints — `0 orders reached a broker` — is the one the runbook
tells him to look for before he flips the flag. It had never been run as written.

**§11's summary table was stale in four rows too** — the one table a reader consults to find out
what exists. `make twt`, `make twt-backtest`, `tools/twt/sweep.py`, `tools/twt/drill.py` and the
`BASKFY_TWT_EXECUTION_ENABLED` setting are all built and were still listed as "Named in `06`".
Each was checked against the tree before being marked, not marked because its module is green.

The §2.2 markers that say the config route and the settings page are not real are **correct** and
stay — they are now marked **STILL NOT BUILT** in that table rather than left to read as pending
work somebody is doing. See *Not done* below for what that costs.

## Not done

* **The ratchet has never executed anywhere except the drill**, and the drill is a simulation
  against a throwaway database. There was no paper phase; `02` §3 records the consequence in its own
  words. **It will first ratchet with real money behind it.**
* **No order has ever been placed and no `tw_position` row has ever existed outside a test or the
  drill.**
* **The live-order branch of `_buy_at_open` has never run.** With the flag false the confirm always
  takes the dry-run path. The real path answers `SENT` and waits for `on_order_update`, a handler
  that is written, unit-tested, and **has never seen a broker postback**.
* **`/twt/reconcile` has never met a real GTT list.** The shape of `kc.get_gtts()` is from Kite's
  documentation and no live response has been read. If a field is named differently the reconcile
  attaches nothing and the page keeps calling a protected line naked — which is the right way round
  for that discrepancy to fail, and is why it is here rather than trusted.
* **Nothing schedules the evening, the morning or the sweep.** The Celery tasks exist and are
  registered; no Beat entry runs them and `app/swing_clock.py` has no TWT entry. Today they are
  `make twt-plan`, `POST /twt/sweep`, and a person.
* **The half-size counter has never counted.** It moves only on a live, non-simulated fill.
* **The sleeve cannot be funded through any surface.** Found on 12 Sep 2026 while re-reading the
  runbook: `PATCH /api/v1/twt/config` does not exist (`twt_settings.py` has the functions and no
  `APIRouter`, and nothing mounts it), there is no `me/twt` page, and `seed twt` takes no
  `--capital` flag though `seed swing` does. The runbook's §2.2 always tagged those three commands
  `[NOT YET REAL — TW3/TW8]`; what was missing is that nothing added the tags up to **"and
  therefore T3 has no keystroke to be"**. A direct `UPDATE` on `tw_config` works and bypasses
  `record_system_change`, so it leaves no audit row. Written up in `NEEDS-MAULIK.md` § TWT T3 with
  the two ways to close it properly. **Not built here** — this run's brief is that it never funds
  the sleeve, and building the funding surface at the end of an unrelated module is not mine to
  decide.
* **No deploy.** The box and `tools/deploy/` are yours; nothing in this run touched either.
* **No Prometheus rule** for `TWT_POSITION_NAKED` or `TWT_GTT_MISSING_AT_1515`. Both are raised
  in-process, which is why that is survivable — DECISIONS-TW TW7.4.

## Needs you

In order, from `NEEDS-MAULIK.md` § TWT:

1. **T1 — the daily Kite login**, before 09:00. Nothing at all happens before one. On this sleeve
   it matters slightly more than on the others: the exit *is* a ratcheting GTT, so a morning
   without a login is a morning on which every `RAISE_GTT_STOP` stays where it was. The position is
   still protected by the old stop; the trail is just a day behind.
2. **T3 — the sleeve's capital, and read this one before the morning rather than during it.**
   Seeded at ₹0 and never written by this run. A sleeve at ₹0 plans nothing and skips every signal
   `NO_SLEEVE_CAPITAL`, by design, so that is the expected first symptom rather than a fault to
   debug at 09:15. The standing default is ₹25,00,000. **But there is nowhere to type it** — see
   *Not done*. Somebody has to build the surface, or you fund it with a direct `UPDATE` that
   leaves no audit row. This is the one item on this list that is not simply waiting on you.
3. **T2 — the execution flag**, `BASKFY_TWT_EXECUTION_ENABLED`, in two files on the box, by your
   hand. The swing sleeve's 5 Sep delegation is about the *swing* flag and does not extend here. If
   you want the same for this sleeve, one line in `NEEDS-MAULIK.md` says so.
4. **T4 — nothing from you, but expect it.** The plant's bars are thinner than Chartink's on some
   days: 832 stock-days have no bar and 602 names lack a clean 50-session volume window in the
   verification period. The live scan will show fewer names. The page says so.

## Caveats I will not bury

* **TW9's backtest drifts, and it is flagged rather than explained away.** Over the plant's own
  bars: **22.17 % CAGR at −26.47 % max drawdown on 169 trades**, against `01` §6's research
  numbers of **20.92 % / −24.7 % / 164**. Drift is +1.25 CAGR points, over the 1.0-point threshold,
  so the page flags it. DECISIONS-TW TW9.3 names the ₹5 crore liquidity floor — which this build
  ships and the research did not — as most of the difference. **It is not reconciled to zero and
  should not be read as if it were.**
* **The sweep has never run against a database** outside the drill. `_load_open_lines` reads
  `tw_position`, and until the drill there had never been a `tw_position` row anywhere to read.
* **`build_rearm()` still returns `unavailable_rearm` in `tools/twt/sweep.py`.** The desk's
  `twt_execute.rearm_callable` is the seam and the drill wires it; the standalone chore refuses
  every line with a reason naming `FIRST-LIVE-MORNING` §9.2 step 3. That is deliberate — a
  placeholder answering `armed=True` would have the sweep report `naked: 0` over a book it had
  done nothing to protect — but it means the standalone sweep can find a naked line and not fix
  one.
* **The sweep's day key is a file, not a column.** `data/twt/sweep/<date>.json` is idempotent
  across runs on one machine and not across machines. DECISIONS-TW TW7.2 names the `tw_session`
  column to add if that ever stops being the same thing.
* **Six modules share one commit.** See the note under Built.
* **70 decisions were taken without you**, every one tagged `⚠ UNREVIEWED` in
  `docs/twt/DECISIONS-TW.md`, each with what was chosen, what was rejected and how to reverse it.
  Three are TW10's and two of those are about the gate tooling rather than the sleeve; the third,
  **TW10.3**, is the call not to build the funding surface.
