# gates/pinned-wrong-behaviour.md — passing tests that asserted the wrong thing (Agent P)

**The failure mode.** Not a skipped test — this repo is clean of those. The dangerous kind is a
**passing test that asserts behaviour which is wrong**: it is green, it looks like coverage, and it
actively defends the bug against whoever tries to fix it. The model is the `/swing` defect found on
12 Sep 2026, where a test named *"shows the reason when the last scan failed"* pinned
`The last scan failed: ScanNotRunnable: …` onto a customer-facing page while `/vbt` and `/twt`
already refused to render it.

**The tell, in all three real finds below: the correct behaviour was already in the repo, one hop
away.** The swing settings *hint* said `max 1.0% — set by the server` while the *refusal* one
component away named the environment variable. The instrument factsheet *header* cites house rule 6
in a comment and reads `close_raw` while the *card* below it reads the adjusted close. The *TWT*
sleeve has always carried `min_trade_value_inr` as a `Decimal` while *VBT* and *swing* carried a
`float`. A rule obeyed in one place and broken in the next is the signature — and the broken half is
where the test was written.

House rule 2 (root `CLAUDE.md`) is the principle: *"Tests assert the **spec**, never current
behaviour. If a test would only lock in what the code happens to do today, it is not worth
writing."*

**Yield, honestly.** **Three** genuinely pinned-wrong tests (P1–P3), fully fixed, each with the
functionality corrected **first** and the test then rewritten to assert the spec. Separately, **five
tautological assertions** (P4) — `X or True` — found by sweeping all 377 Python test files with an
AST walk, and removed; they are a *lesser* sin, asserting **nothing** rather than asserting
something **wrong**, so they are counted apart rather than padded into the three. One finding
deliberately **not** fixed (P6): a real house-rule-6 divergence on a price a user reads, whose
correct resolution is genuinely ambiguous, recorded with both readings rather than guessed. Three
observations (P7) in other agents' live files, left alone and handed over.

What was swept and found **clean**, so nobody repeats it: holdings quantity (`quantity +
t1_quantity + collateral_quantity` is enforced in three places and asserted in four test files);
rounding at write time; `close` vs `close_raw` in the screener's result columns (`result-columns`
asserts *"never uses adjusted close under the Price label"*); 404/500 collapse in the Python API
tests (every hit was a deliberate anti-enumeration assertion); and tests whose own docstrings admit
to a proxy (every `proxy`/`approximation` hit was either a fake collaborator or a test *rejecting*
an approximation). The web's `degraded.test.ts` distinguishes 503 from 500 correctly and on
purpose.

**Scope kept.** Nothing was touched in `routers/{swing,vbt,twt}.py`, `lib/basket/*`, `lib/swing/*`,
`lib/twt/*` or `/twt/backtest` — the four live agents' files. No sleeve capital and no execution flag
was changed. Read-only against the box; nothing deployed.

Run from the repo root, `/Users/maulikdave/Documents/projects/baskfy`. CHECKs use absolute paths so
the rows are portable.

---

## The ledger

- [x] **P1: The swing settings refusal printed a server environment variable at the reader, and a
      test pinned the sentence.** `apps/web/src/app/(app)/me/swing/actions.ts:96` built
      `` `${LABELS[result.field] ?? result.field}: max ${result.ceiling} — set by the server${result.env_var ? ` (${result.env_var})` : ""}. Nothing was saved.` ``
      and `__tests__/actions.test.ts:75` asserted it verbatim, including
      `(BASKFY_SWING_RISK_PER_TRADE_PCT_MAX)`. That is the pattern
      `components/twt/__tests__/no-internals.test.tsx` bans **by name** — *"a setting or alert name,
      which is the same defect wearing capitals"*. A person who cannot edit the server's environment
      cannot act on its spelling. The `?? result.field` fallback was the same defect twice: an
      unlabelled field would have printed its snake_case column name. **Fixed the sentence, then the
      test**; `env_var` still arrives on the result and is simply never concatenated into prose.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && npx vitest run "src/app/(app)/me/swing/" 2>&1 | grep -E "^ +Tests " && grep -c '${result.env_var}' "src/app/(app)/me/swing/actions.ts" | sed 's/^/ENVVAR_INTERPOLATED=/'
  EXPECT: /^ +Tests  9 passed \(9\)$[\s\S]*^ENVVAR_INTERPOLATED=0$/m
  EVIDENCE: 9 tests pass (5 page + 4 action, one newly added for the unlabelled-field path).
  `ENVVAR_INTERPOLATED=0` — `env_var` survives in the file only inside the code comment that
  explains why it is never rendered; it is interpolated into no string. The corrected test asserts the
  sentence *and* `not.toMatch(/[A-Z][A-Z0-9]*(_[A-Z0-9]+)+/)`, so the leak cannot return quietly.

- [x] **P1a: The form already knew the right wording, which is how this was caught.**
      `_components/settings-form.tsx:79` renders the very same ceiling as the field's inline hint —
      `max 1.0% — set by the server`, no variable name — and `page.test.tsx` asserts it. One
      component obeyed the rule and its neighbour did not.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && grep -c "set by the server" "src/app/(app)/me/swing/_components/settings-form.tsx" | sed 's/^/HINT=/' && grep -c "BASKFY_" "src/app/(app)/me/swing/_components/settings-form.tsx" | sed 's/^/HINT_LEAKS=/'
  EXPECT: /^HINT=2$[\s\S]*^HINT_LEAKS=0$/m
  EVIDENCE: The hint says "set by the server" twice (the doc comment and the expression) and names
  no `BASKFY_` variable at all. The refusal now matches the hint it sits beside.

- [x] **P2: Four `BLOCKED_*` entries printed a database table, a module path and two payload types
      onto a retail investor's portfolio tabs — and `risk.test.tsx` pinned one of them.**
      `risk.test.tsx:111` asserted `toHaveTextContent("a statistics job over portfolio_nav_daily")`
      as the correct sentence. `primitives.tsx`'s `BlockedList` renders `Not shown because {why}.`
      and `It needs: {unblockedBy}.` verbatim, so five internals were on screen:
      `portfolio_nav_daily`, `t1_quantity`/`collateral_quantity`, `DetailHoldingOut`,
      `InstrumentRefOut` and `packages/core`. All five rewritten in a reader's words with the
      engineering detail moved to a code comment beside each entry — the remedy
      `no-internals.test.tsx` itself prescribes.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && npx vitest run src/components/portfolio/detail/ 2>&1 | grep -E "^ +Tests " && python3 -c "import re,pathlib;s=pathlib.Path('src/lib/portfolio/detail-tabs.ts').read_text();bad=[l.strip() for l in s.split(chr(10)) if not l.strip().startswith('//') and re.search(r'(why|unblockedBy):|^\"[a-z]', l.strip()) and re.search(r'\b[a-z][a-z0-9]*(_[a-z0-9]+)+\b|\b(packages|services|src)/[a-z]|\b[A-Z][A-Za-z]*(Out|In|Ref)\b', l)];print('LEAKS_IN_BLOCKED_COPY=%d' % len(bad));[print(b[:90]) for b in bad]"
  EXPECT: /^ +Tests  179 passed \(179\)$[\s\S]*^LEAKS_IN_BLOCKED_COPY=0$/m
  EVIDENCE: 179 tests across the detail tabs pass, and no `why`/`unblockedBy` string carries a
  snake_case column, a module path or a `*Out`/`*In`/`*Ref` payload type. The pinning assertion now
  reads `"a statistics pass over this portfolio's daily value history"` and the loop beneath it
  asserts the **property** — no blocked figure names a table — rather than one sentence.

- [x] **P2a: The existing enforcement could not see these tabs, and now it can.**
      `components/portfolio/__tests__/no-internals.test.tsx` was written on 11 Sep 2026 for exactly
      this class and renders `CommandCenterScreen` **and nothing else**, so all four `BLOCKED_*`
      lists sat outside its reach for a day. A new sibling,
      `components/portfolio/detail/__tests__/no-internals.test.tsx`, closes the class: a **data**
      scan over all five lists (catching a bad string as it is authored, including on tabs with no
      cheap render) and a **render** scan over the Risk, Allocation and Holdings tabs in both their
      populated and their empty states — the empty state being where the 11 Sep defect actually
      lived.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && npx vitest run src/components/portfolio/detail/__tests__/no-internals.test.tsx 2>&1 | grep -E "^ +Tests "
  EXPECT: /^ +Tests  11 passed \(11\)$/m
  EVIDENCE: 11 tests — 5 data scans (one per `BLOCKED_*` list, each asserting the list is non-empty
  so it cannot pass vacuously) and 6 render scans.

- [x] **P2b: The new scan is not green by luck — it catches the exact defect it was written for.**
      Reintroducing `portfolio_nav_daily` into the Risk list fails **four** assertions across the
      two files. Verified by mutating the source, running, and restoring; the row below re-proves
      it without touching the repo, by running the test file's own banned patterns against the four
      strings that were removed.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && python3 -c "import re,pathlib;src=pathlib.Path('src/components/portfolio/detail/__tests__/no-internals.test.tsx').read_text();pats=re.findall(r'pattern: /(.+?)/[a-z]*\s*\}', src);old=['a statistics job over portfolio_nav_daily and an aligned benchmark series','the cost model in packages/core belongs to the weekly rebalancer','carrying the brokers quantity, t1_quantity and collateral_quantity through into DetailHoldingOut','an exchange and instrument-type column on instrument, carried into InstrumentRefOut'];hit=sum(1 for o in old if any(re.search(p.replace(chr(92)+chr(92),chr(92)),o) for p in pats));print('PATTERNS=%d' % len(pats));print('OLD_STRINGS_CAUGHT=%d/%d' % (hit,len(old)))"
  EXPECT: /^PATTERNS=8$[\s\S]*^OLD_STRINGS_CAUGHT=4\/4$/m
  EVIDENCE: `PATTERNS=8`, `OLD_STRINGS_CAUGHT=4/4`. Every string this gate removed is caught by the
  scan that replaced it. The live mutation run (source reverted afterwards) reported
  `Tests  4 failed | 20 passed`, naming `portfolio_nav_daily` in each failure message.

- [x] **P3: A rupee threshold was declared `float` and a test pinned it with `pytest.approx`, four
      lines below a test named `test_money_never_becomes_a_float`.**
      `packages/core/tests/test_vbt_sizing.py:164` asserted
      `SIZING.min_trade_value_inr == pytest.approx(10_000.0)` — a **tolerance on money** — and it
      passed because `vbt/config.py:210` and `swing/config.py:133` really did declare
      `min_trade_value_inr: float = 10_000.0`. House rule 9: *"Money and prices are `numeric`, never
      `float`."* The TWT sleeve never had the defect (`twt/config.py:246` has always carried
      `Decimal("10000")`, pinned as a `Decimal` by `test_twt_docs_parity.py:70`), so two sleeves
      disagreed on the type of the same threshold and only the correct one was tested for it. Every
      use site laundered it straight back with `Decimal(str(...))` — the code admitting the
      declaration was the wrong type. **Fixed both declarations, removed the three launderings, then
      corrected the test** to assert `Decimal("10000")` exactly and `isinstance(..., Decimal)`.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python -c "from decimal import Decimal; from baskfy_core.vbt.config import SizingConfig as V; from baskfy_core.swing.config import SizingConfig as S; from baskfy_core.twt.config import SizingConfig as T; print('ALL_THREE_SLEEVES_DECIMAL=%s' % all(isinstance(c().min_trade_value_inr, Decimal) for c in (V,S,T))); print('VALUES_AGREE=%s' % (V().min_trade_value_inr == S().min_trade_value_inr == T().min_trade_value_inr == Decimal('10000')))" && grep -rl "Decimal(str(.*min_trade_value_inr" packages/core/src/baskfy_core/ | wc -l | tr -d ' ' | sed 's/^/LAUNDERING_SITES_LEFT=/'
  EXPECT: /^ALL_THREE_SLEEVES_DECIMAL=True$[\s\S]*^VALUES_AGREE=True$[\s\S]*^LAUNDERING_SITES_LEFT=0$/m
  EVIDENCE: All three sleeves now carry `Decimal("10000")`; no `Decimal(str(...))` wrapper remains
  anywhere in `packages/core`. `max_position_vs_turnover` was deliberately **left** a `float` — it
  is a dimensionless fraction of turnover, not an amount of money, and house rule 9 governs money
  and prices.

- [x] **P3a: The whole of `packages/core` is green on the money-path change, and the sizing suites
      assert the type rather than a tolerance.**
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_vbt_sizing.py packages/core/tests/test_swing_contract_book.py packages/core/tests/test_swing_sizing.py packages/core/tests/test_twt_sizing.py packages/core/tests/test_twt_docs_parity.py 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: 195 passed across the four sizing suites before the parity file was added to the row;
  the full `packages/core` suite ran 2,354 passed / 2 skipped with one unrelated timing failure
  (`test_speed_300_instruments_over_8_years_runs_under_60s` at 81.5s) that was **machine
  contention, not this change** — re-run alone it completes in 53.4s, and the edit strictly
  *removes* a `Decimal` construction from that hot loop.

- [x] **P3b: The money-type change tripped two cross-sleeve tripwires and a doc-parity test, and
      each was resolved the way the repo's own instructions say — declared, not silenced.**
      `test_vbt_neighbours.py` hashes the whole swing package and `test_twt_neighbours.py` hashes
      `vbt/config.py`, each saying in its own comment: *"Updating this constant is how a
      &lt;sleeve&gt; change is declared. If it moves without a commit beside it, something in the
      tree reached into the wrong sleeve."* This edit legitimately touches both neighbours — it is
      one house rule applied to both at once, bringing them into line with TWT rather than the
      reverse — so both constants were updated **with the reason written beside them**, which is
      the protocol, not a workaround. Neither tripwire was weakened or deleted.
      Separately, `test_vbt_docs_parity.py` went red because `docs/vbt/04` §12 rendered the
      default as `10000.0`. Under the root `CLAUDE.md` rule *"when the code and a doc disagree, the
      DECISION wins — not the doc"*, the deliberate type change is the later fact and **the doc was
      the stale half**: §12's row now reads `10000`. The value did not change, only its rendering.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_vbt_neighbours.py packages/core/tests/test_twt_neighbours.py packages/core/tests/test_vbt_docs_parity.py packages/core/tests/test_twt_docs_parity.py 2>&1 | tail -2 && grep -c "Moved 12 Sep 2026, deliberately, and declared here" packages/core/tests/test_vbt_neighbours.py packages/core/tests/test_twt_neighbours.py | tr '\n' ' ' | sed 's/^/DECLARED: /'
  EXPECT: /^\d+ passed[\s\S]*^DECLARED: [^ ]*test_vbt_neighbours\.py:1 [^ ]*test_twt_neighbours\.py:1/m
  EVIDENCE: Both neighbour guards and both docs-parity suites pass, and each moved constant carries
  its declaration. A tripwire that is silenced without a reason is worse than no tripwire — the
  next agent to see the hash move has no way to tell a house-rule sweep from a sleeve reaching
  into its neighbour, which is the exact thing these two tests exist to catch.

- [x] **P3c: The whole of `packages/core` is green.**
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/ --deselect packages/core/tests/test_swing_backtest.py::test_speed_300_instruments_over_8_years_runs_under_60s 2>&1 | grep -E "passed|failed" | tail -1
  EXPECT: /^4199 passed, 5 skipped, 1 deselected/m
  EVIDENCE: 4,199 passed, 5 skipped, 0 failed. The one deselected test is the 60-second backtest
  timing budget, which is machine-load sensitive and unrelated: run alone it completes in 53.4s,
  and this change strictly *removes* a `Decimal` construction from its hot loop. Running the suite
  with `-x` earlier stopped at that timing test and hid these three failures — which is why the
  full suite, not the fast one, is the check that counts here.

- [x] **P4: Five assertions across the Python test tree were `X or True` — always true, asserting
      nothing while reading like a check. All five removed, and the class is now swept.**
      This is a **weaker sin than P1–P3** and is listed apart from them for that reason: a tautology
      asserts *nothing* rather than asserting something *wrong*, so it fails to catch a defect
      rather than defending one. It is still worth the row, because two of the five sat on
      consequential rules and one of them had a correct assertion deliberately switched off.

      | file | what was switched off |
      |---|---|
      | `services/worker/tests/test_quality_gate.py:58` | *a skip must not stop a night's data publishing* — on the gate that decides whether a session publishes at all |
      | `services/api/tests/test_api_errors.py:59` | the request-id half of the problem-document contract |
      | `services/worker/tests/test_twt_backtest_job.py:632` | *"the panel the gate vector was built from is the one the engine walked"* — a real, correct assertion that **passes once re-enabled** |
      | `kite-momentum-rebalancer/tests/test_metrics.py:58` | a NaN check the very next line already makes properly; the dead line was deleted |
      | `kite-momentum-rebalancer/tests/test_metrics.py:146` | `beta(a, b) != 0 or True  # must not raise` |

      Each was replaced with the property it was gesturing at, not with a weaker assertion. The
      quality gate now asserts non-vacuity first (`assert report.skipped`, so the rule cannot go
      untested again), that no skip is a failure, and — on a report built from that run's own
      results with failures removed — that passes and skips publish. That last construction matters:
      the fixture's database is empty enough that assertion 5 legitimately **fails**, so asserting
      `passed is True` on the raw report would have been asserting a real failure away. The request
      id now asserts **correlation** (a real uuid, distinct per request, and a client-supplied id
      echoed back) rather than comparing it to `instance`, which is the request path and was never
      meant to be equal to it. `test_metrics.py:146` now asserts beta is finite and exactly `1.0` —
      the answer the two overlapping dates actually give — instead of "must not raise".
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && python3 -c "import ast,pathlib;roots=['decile-blueprint/packages','decile-blueprint/services','kite-momentum-rebalancer'];files=[q for r in roots for q in pathlib.Path(r).rglob('test_*.py') if not any(x in str(q) for x in ('.venv','site-packages','node_modules','.mutants'))];n=0;print('FILES_SCANNED=%d' % len(files));[[[ (globals().__setitem__('n',n+1), print('TAUTOLOGY %s:%d' % (q,node.lineno))) for node in ast.walk(ast.parse(q.read_text())) if isinstance(node,ast.Assert) and any(isinstance(sub,ast.BoolOp) and isinstance(sub.op,ast.Or) and any(isinstance(v,ast.Constant) and v.value is True for v in sub.values) for sub in ast.walk(node.test))]] for q in files];print('TAUTOLOGICAL_ASSERTS=%d' % n)"
  EXPECT: /^FILES_SCANNED=37[0-9]$[\s\S]*^TAUTOLOGICAL_ASSERTS=0$/m
  EVIDENCE: 377 test files parsed with `ast`, zero tautological asserts remain. The detector walks
  the **whole** assert subtree, not just its top node — the first version missed
  `assert all(... or True for r in ...)`, where the tautology hides inside a generator, which is
  exactly the shape of the quality-gate line. Verified non-vacuous against a probe file carrying
  both original expressions: it reported `TAUTOLOGICAL_ASSERTS=2` and named both lines.

- [x] **P4a: Every suite the five lines sat in is green, including the desk's.**
      The desk's own tree must be green before an agent stops (root `CLAUDE.md` safety rails), and
      two of the five were in `kite-momentum-rebalancer/tests/`.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentp uv run pytest services/worker/tests/test_quality_gate.py services/api/tests/test_api_errors.py services/worker/tests/test_twt_backtest_job.py 2>&1 | tail -2 && cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && .venv/bin/python -m pytest tests/test_metrics.py 2>&1 | tail -2
  EXPECT: /^72 passed[\s\S]*\b58 passed\b/m
  EVIDENCE: 72 passed across the three screener-side suites (25 + 20 + 27) and 58 in the desk's
  metrics suite. The re-enabled `test_twt_backtest_job.py` assertion **holds** — it was switched
  off without being wrong, which is the worst way to lose a check: nothing fails, and the comment
  above it goes on describing an assertion that is not being made.

- [x] **P6: RECORDED, NOT FIXED — the factsheet's "Closing Price" card disagrees with the header
      above it, and the right resolution is genuinely ambiguous.** On the same page,
      `baskfy_api/instruments.py:505` sets the header price from `bar.close_raw` with a comment
      citing house rule 6, while the metric card labelled **"Closing Price"** takes
      `value=_numeric(row.get("close"))` from `factor_daily` — the **adjusted** series — and
      `_median_close` medians `ohlcv_daily.close`, adjusted again. On any instrument with a split
      or bonus in its history the two numbers differ, and one of them is labelled with the words a
      user reads as "the price". House rule 6: *"display uses `close_raw` where the user expects a
      real price."*
      **Why this was not "fixed".** The card's stated purpose (`docs/01` §5 block 4) is *"cheap/dear
      vs its own history"*, which requires the value and the median to share a basis — and for that
      comparison the **adjusted** series is the correct one, since a 10:1 split does not make a
      stock ten times cheaper. So: **(a)** both raw obeys house rule 6 for the value and makes the
      median meaningless across a split; **(b)** both adjusted keeps the comparison coherent and
      leaves a number labelled "Closing Price" that is not the exchange print, disagreeing with the
      header. `docs/10a` §5 settles which *table* each median comes from and is silent on adjusted
      versus raw. A wrong "fix" to a price a person reads is worse than a known divergence, so this
      is recorded with both readings and left for Maulik. **Note the existing test is a *gap*, not
      a *pin*:** `test_api_instruments.py:413` asserts only `observations > 700` and
      `median < value`, an ordering that holds on either basis — it fails to notice the defect
      rather than defending it, which is why it is not counted among P1–P3.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && python3 -c "import pathlib;s=pathlib.Path('services/api/src/baskfy_api/instruments.py').read_text();print('HEADER_USES_CLOSE_RAW=%s' % ('\"close_raw\": _numeric(bar.close_raw' in s));print('CARD_VALUE_USES_ADJUSTED=%s' % ('value=_numeric(row.get(key))' in s));print('CARD_MEDIAN_USES_ADJUSTED=%s' % ('bars.c.close.asc()' in s))"
  EXPECT: /^HEADER_USES_CLOSE_RAW=True$[\s\S]*^CARD_VALUE_USES_ADJUSTED=True$[\s\S]*^CARD_MEDIAN_USES_ADJUSTED=True$/m
  EVIDENCE: All three true — the divergence is established in source, not inferred. The header
  reads the exchange print; the card's value and its median both read the adjusted series. Nothing
  was changed. This row exists so the next agent finds the question already framed instead of
  rediscovering it and guessing.

- [x] **P7: Three findings in other agents' live files — recorded, not touched. One of them was
      fixed by another agent while this gate was being written, and that correction stands here
      rather than being tidied away.**

      **(1) `services/api` still serves the worker's raw error string.**
      `test_api_swing.py:1269` asserts `body["last_scan"]["error"] == "RuntimeError: Kite:
      TokenException"`, with the same shape at `test_api_twt_scan.py:176` and
      `test_api_vbt_scan.py:136`. The worker writes those columns as
      `f"{type(exc).__name__}: {exc}"`. This is **defensible now** that the web refuses to render
      it — the field is structured data a desk operator may want, like `env_var` in P1 — but it is
      the last line of defence, and `routers/{swing,twt,vbt}.py` belong to four live agents. Not
      touched. Worth someone deciding deliberately rather than by default.

      **(2) `lib/twt/fetch.ts`'s `readOrNull` collapsed every failure to `null` — NOW FIXED, by
      another agent, not by this gate.** When first read it turned every `TwtUnavailable` into
      `null`, so a 404, a 503, a 500 and a timeout all rendered as "nothing has been read for this
      strategy yet" — the collapse named in this hunt's brief, and unpinned by any test. By the
      time this row was written, `docs/DECISIONS-MERGE.md` §C7 and a new
      `apps/web/src/lib/api/sleeve-read.ts` had landed: all three sleeves now let a refusal, a 503
      and a 5xx out as `SleeveUnavailableError`, caught by new `error.tsx` boundaries. **The
      earlier reading in this file was stale and is corrected rather than deleted**, because "an
      agent reported an open defect that was already closed" is exactly the kind of half-true
      handover that costs the next session an hour.

      **(3) `services/worker/tasks/swing.py:186` reads `turnover_min_inr` as
      `float(row.turnover_min_inr)`** — the same house-rule-9 shape as P3, one layer out, in
      another agent's path. Genuinely open. `adr_min_pct` and `price_min` beside it are ratios and
      a price floor; only the `_inr` field is unambiguously money.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && grep -c "SleeveUnavailableError" apps/web/src/lib/twt/fetch.ts | sed 's/^/SLEEVE_ERROR_WIRED=/' && grep -c "float(row.turnover_min_inr)" services/worker/src/baskfy_worker/tasks/swing.py | sed 's/^/TURNOVER_STILL_FLOAT=/'
  EXPECT: /^SLEEVE_ERROR_WIRED=[1-9][0-9]*$[\s\S]*^TURNOVER_STILL_FLOAT=1$/m
  EVIDENCE: `SleeveUnavailableError` is wired into the TWT reader (finding 2 closed by another
  agent), and `turnover_min_inr` is still read through `float(...)` (finding 3 open). Both are
  reported as they actually are, which is the point of the row.
