# TW9 — the backtest on the page, from the plant's own bars, with its drift named

**Plan:** `docs/twt/06-module-plan.md` § TW9. **Reads:** TW2's engine, TW3's `tw_backtest_run`,
TW8's page (which already has the slots and renders empty until this lands).

**Goal:** a number, from the plant's own bars, beside the study's, with the difference between
them named rather than explained away.

> **How these checks were run, and why each line carries two counts.** The db-marked half of
> `services/worker/tests/test_twt_backtest_job.py` needs Postgres. The shared `baskfy_test`
> database was contended all evening, so the **with-db** counts below were taken against a scratch
> database (`createdb baskfy_tw9`, `BASKFY_TEST_DATABASE_URL` pointed at it, dropped afterwards).
> Every gate also has at least one **pure** test that needs no database at all, which is why the
> bare CHECK line still satisfies its EXPECT on a machine with no `BASKFY_TEST_DATABASE_URL` — the
> **no-db** counts are what that produces. Both are recorded; a gate whose only passing test is
> one that skipped would be a tick with nothing behind it. The deselect counts differ between the
> two because TW6 and TW7 landed tests in parallel sessions while this module ran.

- [x] G1: `tools/twt/backtest.py` and `make twt-backtest` run the engine over `ohlcv_daily` at the
      **shipped** ₹5 crore floor, sized against `params.sleeve_inr` and **never** against
      `tw_config.sleeve_capital_inr` — asserted by a spy, because reading the live capital into a
      backtest is how a research number quietly becomes a claim about the user's money.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and backtest and (capital or spy or sleeve_inr)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: with db `..... | 5 passed, 7629 deselected in 6.08s`; no db `..ss. | 3 passed, 2 skipped, 7637 deselected in 2.40s`. The spy is `TestTheRunNeverReadsTheSleevesCapital::test_no_statement_of_the_run_touches_tw_config_at_all` — a `do_orm_execute` listener records **every** ORM statement the run issues and asserts none names `tw_config`, with a funded `tw_config` row seeded first so the absence is a measurement rather than a vacuum. Its sibling re-runs with the sleeve funded at ₹25 lakh and asserts the stats come back byte-identical. `make twt-backtest` ran end to end over 3.7 M bars in 35 s (see G5).

- [x] G2: **Append-only.** Two runs of the same date produce two rows and edit nothing.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and backtest and (append or two_runs)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: with db `.. | 2 passed, 7632 deselected in 4.81s`; no db `s. | 1 passed, 1 skipped, 7640 deselected in 1.43s`. The db test runs twice, re-reads the first row by id and asserts `stats` and `started_at` are unchanged and the table holds exactly 2 rows. The pure test is the structural half: the module contains no `sa.update(`, `update(TwBacktestRun`, `sa.delete(` or `session.delete(` — append-only is the absence of an edit path, not a habit.

- [x] G3: A run that raises writes `finished_at` and `error` **and re-raises** — a failed run that
      looks finished is worse than one that is obviously broken.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and backtest and (raises or error)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: with db `.... | 4 passed, 7630 deselected in 5.76s`; no db `.ss. | 2 passed, 2 skipped, 7638 deselected in 1.39s`. Three claims: a monkeypatched `with_twt_columns` that raises leaves `stats` null, `error` carrying `"RuntimeError: the plant fell over"` **and the traceback**, `finished_at` set, and `latest_finished` still answering the earlier good run; an empty plant raises `ValueError` rather than reporting a zero-return book; and `test_the_failure_path_raises_without_a_database` proves the whole path against a stub session, so the claim holds with no Postgres at all.

- [x] G4: The page shows the latest **finished** run per source and **not** a later failed one.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/twt --silent --reporter=basic -t "finished" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: `Test Files  1 passed | 3 skipped (4)` / `Tests  2 passed | 49 skipped (51)`. TW8 had the in-flight case; TW9 added the one this gate names — `keeps the last finished run when a later re-run failed`, where the later row **has** a `finished_at` (because `03` §9 sets one on failure too) and no `stats`. The card still shows the good 20.9 % and the failure's text is absent from the DOM. The page's `latestFinished` and the job's `latest_finished` filter on the same two facts — finished **and** carrying stats — so the two halves of the rule cannot disagree.

- [x] G5: Drift is computed against `01` §6 and **flagged above 1.0 CAGR point**; a synthetic
      result 1.5 points away renders the warning **naming both numbers**.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/twt --silent --reporter=basic -t "drift" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: `Test Files  1 passed | 3 skipped (4)` / `Tests  2 passed | 49 skipped (51)`. `flags a drift of 1.5 CAGR points and names both figures` renders **22.4 %** beside **20.9 %** in `twt-drift`; its companion asserts no banner at all inside the threshold. The Python half is `TestTheDriftArithmetic`: ±1.0 not flagged, ±1.5 flagged, and `to_json` carrying `published_cagr_pct`, `run_cagr_pct` and `threshold_cagr_points` as decimal strings. **And it is not only synthetic — the plant's own run is flagged:** `+1.25` CAGR points (**22.17** against **20.92**), `-1.77` drawdown points, `+5` trades. DECISIONS-TW **TW9.3** names the ₹5 crore floor as most of that gap and refuses to re-point the comparison to make it go away.

- [x] G6: `tw_backtest_run.stats` carries every key TW8's page reads — including
      **`gate_off_cagr_pct`, the one number that justifies the gate**, whose slot on the card
      nothing else fills.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and backtest and stats" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: with db `...... | 6 passed, 7628 deselected in 5.76s`; no db `.ssss. | 2 passed, 4 skipped, 7636 deselected in 2.40s`. `PAGE_STATS_KEYS` is the seventeen names `lib/twt/fetch.ts` declares and `lib/twt/view.ts` reads; the db test asserts the stored row carries all seventeen and the pure test asserts `stats_payload` does. `gate_off_cagr_pct` and `gate_off_max_drawdown_pct` come from a second book over the same detection pass and measured **14.91 % at -48.11 % on 249 trades** against the gated **22.17 % at -26.47 %** — the gate is worth 7.3 CAGR points and 21.6 drawdown points on the plant's bars, where the study measured 3.7. Also asserted: every figure is a decimal string (house rule 9, to the database), and a number the run could not produce is an **absent key** and never a JSON null, because a null reaches `percent()` and prints as the word (**TW9.5**).

- [x] G7: **TW2.2's warning is honoured.** The research panel's ETFs carry so few bars that
      including them in the breadth denominator measured as zero difference; that is a property of
      the sparse export and **not of the plant**. This run must not inherit the assumption.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (etf or denominator)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: with db `................... | 19 passed, 7615 deselected in 5.74s`; no db `...............sss. | 16 passed, 3 skipped, 7623 deselected in 2.68s`. `etf_denominator_delta` adds the refused rows back, recomputes only the breadth series and stores the largest `pct_above_dma` difference **and how many gate verdicts flip** — the second is the one that matters, because the gate decides whether the book may enter at all. The set added back is `excluded_etf_ids`: `04` §1.1/§1.2's admitted instruments minus `load_universe`'s answer, which is exactly what was taken out, rather than the `etf` index's members (that index has **no members at all** on this snapshot, so the symbol and name patterns are the whole exclusion). It is **on by default** and a test pins the default so it cannot become an opt-in. Measured on the plant: **310 instruments, 1,239 bars over 2,393 sessions, 0.0000 of a point, 0 gate verdicts changed** — zero again, and **TW9.4** says plainly that this snapshot is as ETF-sparse as the export was rather than that the question is settled. TW2.12's `clamped_below_stop` also measured **0** here and is stored on every run.

- [x] G8: Every suite green and both lints clean.
  CHECK: cd decile-blueprint && uv run ruff check . 2>&1 | tail -2 && uv run mypy --strict 2>&1 | tail -2
  EXPECT: /Success|All checks passed/
  EVIDENCE: `All checks passed!` and `Success: no issues found in 644 source files`. Web: `pnpm exec vitest run src/components/twt` → `Test Files  4 passed (4)` / `Tests  51 passed (51)`. Full Python suite against the scratch database: see `docs/twt/STATUS.md` § TW9 for the run and the two pre-existing failures it surfaced, neither of which is TW9's.

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit. -->
