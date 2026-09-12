# TWT — `GET /twt/backtest`, the twin of the empty-page bug, one room along

**12 Sep 2026.** An hour after `GET /twt/today` was found missing, the Backtest tab was found
missing the same way. `apps/web/src/lib/twt/fetch.ts:286` calls `readOrNull<TwtBacktest>(
"/twt/backtest")`; nothing served it; `readOrNull` turns the 404 into `null`; `null` is the card's
empty state. `services/api/src/baskfy_api/routers/twt.py` said so in its own words — *"`/twt/backtest`
is still unserved and still answers `null` honestly"* — and `test_twt_readonly.py` said the count
would go red *"when somebody serves it, which is the point"*. It did. This is that entry.

**Why this half was worse than the hub's, not better.** The hub's bug was loud: 58 detected names
in the database and a page saying nothing had been read. This one is silent, because
`tw_backtest_run` really does hold **0 rows on the box** (B8), so the card's empty state has been
true *by accident*. It would have gone on saying the same sentence, word for word, the first
evening TW9's job produced a settled result, and nobody reading the page could have told the two
apart. A reader that does not exist and a writer that has not run are the same silence.

**What was NOT built.** TW9 already built the backtest — `tools/twt/backtest.py`,
`baskfy_worker.tasks.twt_backtest`, the `tw_backtest_run` table, `gates/twt-9.md` 8/8. This serves
what that produces and computes nothing.

**The shape.**

```
GET /api/v1/twt/backtest   ->  200
{ "runs":     [ {id, source, started_at, finished_at, params, stats, drift, error} ],  # <= 1 per source
  "caveats":  [ {id, text} x 4 ],        # docs/twt/01 §8, verbatim
  "reason":   null | "<which absence this is>" }
```

`runs` is the latest **finished** run per source — finished *and* carrying stats, so a run in
flight or a failed re-run never displaces the last good number (`03` §9) — and never the history,
because the table is append-only and every settled row carries a nine-year equity curve.

**On the box this answers `runs: []` and always will until a backtest is run.** That is expected,
not a broken route, which is why B5 makes the empty answer *say which of the three absences it is*
— never asked for, still running, or finished without a result — instead of leaving the card to
guess with one sentence that is right two times in three.

**Read-only, and it stays that way.** `/twt` still has exactly one non-GET (the scan POST), this
route selects from one table, and nothing on the surface names a broker, the sleeve's funded
capital or the execution flag (B7).

---

- [x] B1: **The route the page asks for is served, is a GET, and is the only new path.** The
      surface is four now; the one mutating route is still the scan and nothing else.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python -c "from baskfy_api.app import create_app; s=create_app().openapi(); p=sorted(x for x in s['paths'] if '/twt' in x); m=sorted(x for x in p if any(v!='get' for v in s['paths'][x])); print('paths=%d served=%s verbs=%s mutating=%s' % (len(p), '/api/v1/twt/backtest' in p, sorted(s['paths']['/api/v1/twt/backtest']), m))"
  EXPECT: /^paths=4 served=True verbs=\['get'\] mutating=\['\/api\/v1\/twt\/scan'\]$/m
  EVIDENCE: paths=4 served=True verbs=['get'] mutating=['/api/v1/twt/scan'] — `/api/v1/twt/backtest` joined `/twt/today`, `/twt/scan` and `/twt/scan/{run_id}`, and the one non-GET is still the scan. `test_twt_readonly.py`'s exact equalities were *designed* to go red here (its own note said "when somebody serves it the count fails again, which is the point") and they did: paths 3→4, handlers 3→4, `scoped_sole_user_id(` 3→4, plus a new `test_the_backtest_read_the_web_app_asks_for_is_served_and_is_a_get`.

- [x] B2: **The regression.** Seed the row TW9's job writes, read the path `fetch.ts` asks for —
      derived from that file, not spelt out again — and the same figures come back as the same
      strings. House rule 9 runs to the database; a route that parsed and re-rendered them would
      round a second time and nothing downstream would notice.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentB uv run pytest services/api/tests/test_twt_backtest_to_page.py -k "settled_run_is_a_page_with_numbers or two_kinds_of_run or another_books_run" 2>&1 | tail -3
  EXPECT: /[1-9][0-9]* passed/
  EVIDENCE: 3 passed, 8 deselected in 3.99s — `test_a_settled_run_is_a_page_with_numbers_on_it`, `..._two_kinds_of_run_come_back_separately_and_are_never_mixed`, `..._another_books_run_is_not_this_books_run`. The URL is read out of `lib/twt/fetch.ts` by regex, not spelt out here, so the suite cannot stay green against a path the page has stopped using. Round-tripped unchanged: `cagr_pct="22.17"`, `max_drawdown_pct="-26.47"`, `trades=169`, `gate_off_cagr_pct="14.91"`, the yearly table, the equity curve and the whole `params` object — every figure still the decimal **string** the job stored.

- [x] B3: **`docs/twt/01` §8 travels with the numbers, verbatim** — checked against the document
      itself, not against a second copy of the prose, with only whitespace normalised. House rule
      9: disclaimers are components, not footers, and `05` §3 restates it for this page by name.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python -c "import unicodedata, pathlib; from baskfy_api.twt import CAVEATS; doc=' '.join(unicodedata.normalize('NFC', pathlib.Path('../docs/twt/01-method.md').read_text()).split()); missing=[k for k,t in CAVEATS if ' '.join(unicodedata.normalize('NFC',t).split()) not in doc]; print('caveats=%d verbatim=%s missing=%s' % (len(CAVEATS), not missing, missing))"
  EXPECT: /^caveats=4 verbatim=True missing=\[\]$/m
  EVIDENCE: caveats=4 verbatim=True missing=[] — all four paragraphs of `docs/twt/01` §8 (`trades`, `giveback`, `history`, `capital`) are substrings of the document once its 96-column wrapping is collapsed; nothing else is touched, so the numbers, en dashes, rupee signs and markdown emphasis all have to match. The integration test asserts the same thing from the HTTP payload and additionally that §8's quantities survive it: `164 trades`, `53 %`, `601`, `20 %`, `₹10 lakh`, `₹25 lakh`. ⚠️ §8's fourth paragraph contains "no backtest **in this repository**", which DECISIONS-TW **TW8.2** says a reader must never be shown — so this field is the **record** and `components/twt/caveats.tsx` stays the **rendered copy**; both the constant and the route say so in place (TW14.2).

- [x] B4: **TW9's drift flag travels with the figure it disputes.** The plant's own run is flagged
      — 22.17 % at -26.47 % on 169 trades against the study's 20.92 / -24.7 / 164 — and a route
      that served the number without the dispute would turn a contested result into a settled one
      on the way through.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentB uv run pytest services/api/tests/test_twt_backtest_to_page.py -k "drift or caveats_arrive" 2>&1 | tail -3
  EXPECT: /[1-9][0-9]* passed/
  EVIDENCE: 2 passed, 9 deselected in 3.76s — `test_the_drift_flag_travels_with_the_figure_it_disputes` and `test_the_caveats_arrive_with_the_numbers_and_are_the_documents_own_words`. `flagged=True`, `run_cagr_pct="22.17"`, `published_cagr_pct="20.92"`, `cagr_pct_delta="1.25"`, `trades_delta=5` — TW9's own run really is more than a CAGR point out (`gates/twt-9.md` G5, which refused to re-point the comparison to make it go away), and the card's warning names both figures, so both have to arrive.

- [x] B5: **The empty case is a 200 that says which absence it is**, and a settled run carries no
      reason at all. Three facts, three sentences: never asked for, still running, finished
      without a result.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentB uv run pytest services/api/tests/test_twt_backtest_to_page.py::TestTheEmptyCaseSaysWhichAbsenceItIs 2>&1 | tail -3
  EXPECT: /^4 passed/m
  EVIDENCE: 4 passed in 4.13s — the whole `TestTheEmptyCaseSaysWhichAbsenceItIs` class. No rows → "No backtest has been run for this strategy yet. The rule has never been replayed over this product's own price history, so there is no result to show — which is not the same as a result of zero." A row with no `finished_at` → the in-flight sentence. A row finished with an `error` and no stats → the failed sentence. A settled run → `reason` is null. The caveats are served in every one of them, because the terms are part of the page whether or not there is a number on it. **Not yet rendered:** `TwtBacktest` in `lib/twt/fetch.ts` types only `runs`, so `reason` arrives and is ignored until whoever owns that module wires it (TW14.2, and see the report).

- [x] B6: **A run in flight or a failed re-run never displaces the last good number** (`03` §9),
      and reading the page appends nothing to the append-only table it reads.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentB uv run pytest services/api/tests/test_twt_backtest_to_page.py -k "never_displaces or writes_nothing or appends_no_row" 2>&1 | tail -3
  EXPECT: /[1-9][0-9]* passed/
  EVIDENCE: 2 passed, 9 deselected in 4.10s — `test_a_failed_or_unfinished_rerun_never_displaces_the_last_good_number` and `test_reading_the_page_appends_no_row_and_starts_no_run`. A failed run **has** a `finished_at` (the job sets one on failure so "still running" and "failed" are different states), so "latest finished" alone would have served the failure; finished **and** carrying stats is the pair, the same one `baskfy_worker.tasks.twt_backtest.latest_finished` and the page's `latestFinished` use. Three reads left the row count unchanged. ⚠️ Found while writing this: SQLAlchemy's JSON types store a Python `None` as JSON `null`, which satisfies `stats IS NOT NULL` — the fixture had to stop assigning `None` and leave the column off the constructor, the way `_open_run` does. A seeded row the writer cannot produce would have failed the route for nothing.

- [x] B7: **Still read-only, and still unable to reach an order.** One non-GET on `/twt`, no
      module naming the execution package, the funded capital or the execution flag, every route
      authenticated and scoped to the sole tenant — plus TW10's property suite over the desk.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/api/tests/test_twt_readonly.py packages/core/tests/test_twt_safety_properties.py packages/core/tests/test_no_escape_hatches.py 2>&1 | tail -3
  EXPECT: /[1-9][0-9]* passed/
  EVIDENCE: 54 passed in 1.64s — `test_twt_readonly.py` (16), `test_twt_safety_properties.py` (TW10's property suite over the desk's nine routes and its tasks, with `DRY_RUN=False` so the sleeve's own flag is the only thing holding it) and `test_no_escape_hatches.py`. The new route names no broker, no execution package, no auto-execute flag and not the sleeve's funded capital; it takes a principal and resolves the sole tenant like the other three. The exact-equality check that the one mutating route is the scan is still exact.

- [x] B8: **The box's `tw_backtest_run` is empty, read-only, no deploy** — so "the tab is still
      empty after this change" is the expected answer there and not evidence of a broken route.
      B5 is what makes the difference visible to a reader.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/box-sql.sh "select count(*)||':'||coalesce(max(source),'none') from tw_backtest_run" 2>&1 | tail -1
  EXPECT: /^0:none$/m
  EVIDENCE: 0:none — `select count(*)||':'||coalesce(max(source),'none') from tw_backtest_run` on the box, read-only via `tools/deploy/box-sql.sh`, **no deploy**. So this route answers `runs: []` there and will until a backtest is run: "the tab is still empty after the change" is the expected result and not evidence of a broken route, which is exactly why B5 makes the answer say *which* absence it is.

- [x] B9: **The artefacts agree with the code.** `openapi.json` and the generated TypeScript
      client carry the route, `docs/07`'s expected-path table has an entry for it (a route
      reachable without one is a contract change nobody agreed to), and lint and types are clean
      across the touched modules.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/api/tests/test_api_artifacts.py 2>&1 | tail -2 && uv run ruff check services/api packages/core 2>&1 | tail -1 && uv run mypy services/api/src/baskfy_api/twt.py services/api/src/baskfy_api/routers/twt.py 2>&1 | tail -1
  EXPECT: /Success: no issues found/
  EVIDENCE: 12 passed (`test_api_artifacts.py`) | All checks passed! (ruff over `services/api` and `packages/core`) | Success: no issues found in 2 source files (mypy). `make openapi` and `make client` regenerated `packages/api-client/openapi.json` and `src/generated/schema.ts` — both carry `/api/v1/twt/backtest` alongside another agent's in-flight `last_scan` work, neither of which was clobbered. `EXPECTED_PATHS` in `test_api_artifacts.py` gained a `/twt/backtest` entry with its reason (a route reachable without a `docs/07` entry is a contract change nobody agreed to). `apps/web`'s `served-paths.test.ts` 4/4: its `KNOWN_UNSERVED` register had listed `/twt/backtest` as the one open bug and is now empty, which is the register working in the direction that catches a fix. **Full `services/api` suite: 1 failed, 1986 passed, 1 skipped, 1 xfailed in 12m36s** — the one failure is `test_load.py::test_p95_stays_under_four_hundred_milliseconds_with_no_errors` (500 requests, **0 errors**, p95 514 ms against a 400 ms budget), a screener latency budget on a laptop running several agents and Docker Postgres at once. It touches no `/twt` code and is environmental.
