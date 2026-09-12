# gates/scan-e2e-regression.md — the test that would have caught it (Agent T)

**What happened.** "Scan now" shipped on 12 Sep 2026 with **15 UI gates and 24 backend gates, all
green**. Maulik pressed the button. The scan ran, succeeded in 19 seconds, and wrote **58 tight
names, 2 signals and a breadth reading** to the production database. The page then said:

> Nothing has been read for this strategy yet.

**Thirty-nine green gates and not one of them could have caught it**, because every test stopped at
its own boundary. `services/api/tests/test_api_twt_scan.py` proved the button writes a
`tw_scan_run` row, publishes `baskfy.twt.scan` and moves no money — it never read a name back.
`test_twt_detect.py` and `packages/core/tests/test_twt_*.py` proved the detector writes
`tw_state_daily`, `tw_signal_daily` and `tw_breadth_daily` — they never asked how a page would get
at them. The `/twt` page's tests proved it renders **given a payload**, from fixtures hand-written
to `docs/twt/03` — they never asked whether anything serves one. The bug lived in the gap:
`apps/web/src/lib/twt/fetch.ts` asked for `/twt/today`, **no such route existed**, `readOrNull`
turned the 404 into `null`, and `null` is the empty state.

**Goal of this leaf.** One test that seeds or drives a real detector run, reads back through the
**real read path the page uses**, and asserts the names come back — and that **fails** if a writer
and a reader disagree about which session or which user. Not the fix (Agent R owns the route), not
the wider contract audit (Agent C owns that). The regression.

**Where it went, and why there.**
`decile-blueprint/services/api/tests/test_twt_scan_to_page.py` — a `db`-marked Python integration
test, four tests, ~5 seconds. The disagreement is between a job that writes rows and an HTTP route
that reads them; both are Python, both run against a real Postgres in seconds, and the whole of the
ambiguity — which session, which tenant, which table — is on that side of the wire. A Playwright
test would add a Next.js server, a login and thirty seconds, and would still assert the same
payload one layer further away, where the failure reads "the page is empty" rather than "the route
is not there". `fetch.ts` is thin by design (a URL, a bearer, a timeout, `readOrNull`) and has its
own unit tests; what it cannot cover, and what this does, is whether anything answers the URL.

**What the test drives.** `POST /twt/scan` over HTTP → the worker's own `run_twt_scan` (which
picks the session itself from `pipeline_run`, as it does on the box) → the detector that call makes
→ `GET` of the path **read out of `fetch.ts` at run time** rather than spelt out again here. Only
the Celery hop is collapsed. The bar panel is `test_twt_detect`'s own fixture, imported rather than
copied, ending on the **published** session so the writer's choice of day and the fixture agree.

**Red, then green — the honest history.** The suite was written and run against the tree as it
stood: all three end-to-end tests **failed**, at the read, with
`GET /api/v1/twt/today answered 404`. Agent R's `GET /twt/today` landed while this leaf was
running and they went green without a line of the test changing. Because a test that has only ever
been green proves nothing about its own teeth, **T3–T5 below re-break the reader three ways** and
assert that exactly the right test fails each time.

**Constraints honoured.** Read-only against the box (nothing here touches it), no deploy, no
execution flag, no sleeve capital — the suite asserts detection rows only, and
`test_api_twt_scan.py::TestItMovesNoMoney` remains the money rail.
`BASKFY_TEST_DATABASE_URL` is `baskfy_agentT` throughout, never the shared `baskfy_test`: other
agents were migrating that database in the same hour.

---

## The ledger

- [x] **T1: The regression suite exists, is four tests, and is green against the current tree.**
      It drives the button, the worker, the detector and the page's read path in one transaction.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentT timeout 900 uv run pytest services/api/tests/test_twt_scan_to_page.py --no-header -p no:randomly 2>&1 | tail -1 | sed 's/ in [0-9].*//'
  EXPECT: /^4 passed$/m
  EVIDENCE: `4 passed`. Before Agent R's route landed the same command printed `3 failed, 1 passed`
  with `GET /api/v1/twt/today answered 404` on each — see T3, which reproduces that state on
  demand.

- [x] **T2: The path the suite drives is read out of `fetch.ts`, not written down a second time.**
      The bug *was* two files disagreeing about a string; a test that spelt the route out again
      would be a third place for the same string to drift. Proved by pointing `FETCH_TS` at a file
      that names a different path and watching the helper follow it.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && D=$(mktemp -d) && printf '%s\n' 'import sys, pathlib' "sys.path.insert(0, 'services/api/tests')" 'import test_twt_scan_to_page as t' "print('live=%s' % t.page_read_path())" "fake = pathlib.Path(sys.argv[1]) / 'fetch.ts'" 'fake.write_text(sys.argv[2])' 't.FETCH_TS = fake' "print('follows=%s' % t.page_read_path())" > $D/p.py && uv run python $D/p.py $D 'return readOrNull<TwtToday>("/twt/proof", {});'; rm -rf $D
  EXPECT: /^live=\/twt\/today$[\s\S]*^follows=\/twt\/proof$/m
  EVIDENCE: `live=/twt/today`, `follows=/twt/proof` — the helper reads the page's own module and
  would drive a renamed route without anybody remembering to edit the test.

- [x] **T3: With the day's route removed, the suite reproduces the incident — and its failure
      message names what the writer had just written.** This is the state the tree was in at 09:00
      on 12 Sep. Three of the four tests fail at the read, each saying how many tight names,
      signals and breadth rows the scan had put in the database a line earlier, which is the whole
      difference between "404" and "the writer and the reader disagree".
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && D=$(mktemp -d) && printf '%s\n' 'import pytest' 'from baskfy_api.routers.twt import router' '@pytest.fixture(autouse=True)' 'def _m():' "    keep=[r for r in router.routes if getattr(r,'path','')!='/twt/today']" '    every=list(router.routes)' '    router.routes[:]=keep' '    yield' '    router.routes[:]=every' > $D/m.py && out=$(PYTHONPATH=$D BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentT timeout 900 uv run pytest services/api/tests/test_twt_scan_to_page.py --no-header -p no:randomly -p m 2>&1); rm -rf $D; printf 'wrote=[%s] incident=%s %s\n' "$(echo "$out" | grep -o 'The scan wrote [0-9]* tight names, [0-9]* signals and [0-9]* breadth' | head -1)" "$(echo "$out" | grep -c 'AssertionError: THE WRITER')" "$(echo "$out" | tail -1 | sed 's/ in [0-9].*//')"
  EXPECT: /^wrote=\[The scan wrote 3 tight names, 3 signals and 1 breadth\] incident=3 3 failed, 1 passed$/m
  EVIDENCE: `wrote=[The scan wrote 3 tight names, 3 signals and 1 breadth] incident=3 3 failed, 1
  passed`. The writer half is real — the run reached `DONE`, the detector wrote three state rows,
  three signal rows and a breadth reading for the session it chose — and the page's path answered
  404 anyway. The mutant is written into a temp directory and deleted; nothing in the repo is
  touched.

- [x] **T4: A reader that forgets *which book* fails exactly the tenant test.** The mutant unions
      every tenant's rows for the session. One test fails and it is the right one — a plausible
      bug is caught, and the other three do not fire, so the suite is discriminating rather than
      merely brittle.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && D=$(mktemp -d) && printf '%s\n' 'import pytest, sqlalchemy as sa' 'from baskfy_api import twt as s' 'from baskfy_core.models import TwStateDaily' '@pytest.fixture(autouse=True)' 'def _m(monkeypatch):' '    original = s._tight' '    async def forgetful(se, user_id, day):' '        ids = (await se.execute(sa.select(TwStateDaily.user_id).where(TwStateDaily.date == day).distinct())).scalars().all()' '        rows = []' '        for i in ids:' '            rows.extend(await original(se, int(i), day))' '        return tuple(rows)' "    monkeypatch.setattr(s, '_tight', forgetful)" > $D/m.py && out=$(PYTHONPATH=$D BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentT timeout 900 uv run pytest services/api/tests/test_twt_scan_to_page.py --no-header -p no:randomly -p m 2>&1); rm -rf $D; printf 'who=%s %s\n' "$(echo "$out" | grep '^FAILED' | sed 's/.*:://' | tr '\n' ',')" "$(echo "$out" | tail -1 | sed 's/ in [0-9].*//')"
  EXPECT: /^who=test_another_tenant_s_names_are_not_served_as_this_one_s, 1 failed, 3 passed$/m
  EVIDENCE: `who=test_another_tenant_s_names_are_not_served_as_this_one_s, 1 failed, 3 passed`.
  The planted foreign row sits on the **same session** and on an instrument this book has no row
  for, so only the `user_id` filter keeps it off the page — a copy of the same names under another
  id would have come back as the same set of symbols and this mutant would have passed.

- [x] **T5: A reader that forgets *which session* fails exactly the session test.** The mutant
      unions every date this book has rows for. The planted name was tight a week earlier and never
      on the detected session, so a reader reading the whole table serves a name the scan did not
      find.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && D=$(mktemp -d) && printf '%s\n' 'import pytest, sqlalchemy as sa' 'from baskfy_api import twt as s' 'from baskfy_core.models import TwStateDaily' '@pytest.fixture(autouse=True)' 'def _m(monkeypatch):' '    original = s._tight' '    async def forgetful(se, user_id, day):' '        days = (await se.execute(sa.select(TwStateDaily.date).where(TwStateDaily.user_id == user_id).distinct())).scalars().all()' '        rows = []' '        for d in days:' '            rows.extend(await original(se, user_id, d))' '        return tuple(rows)' "    monkeypatch.setattr(s, '_tight', forgetful)" > $D/m.py && out=$(PYTHONPATH=$D BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentT timeout 900 uv run pytest services/api/tests/test_twt_scan_to_page.py --no-header -p no:randomly -p m 2>&1); rm -rf $D; printf 'who=%s %s\n' "$(echo "$out" | grep '^FAILED' | sed 's/.*:://' | tr '\n' ',')" "$(echo "$out" | tail -1 | sed 's/ in [0-9].*//')"
  EXPECT: /^who=test_the_reader_serves_the_run_s_session_and_only_that_session, 1 failed, 3 passed$/m
  EVIDENCE: `who=test_the_reader_serves_the_run_s_session_and_only_that_session, 1 failed, 3
  passed`.

- [x] **T6: The suite is collected by `make test-db`.** Four tests under the `db` marker. A
      regression test CI does not run is a regression test that does not exist, and this one needs
      a live Postgres.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && printf 'db_marked=%s\n' "$(BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentT uv run pytest services/api/tests/test_twt_scan_to_page.py -m db --collect-only 2>&1 | grep -c '::')"
  EXPECT: /^db_marked=4$/m
  EVIDENCE: `db_marked=4`.

- [x] **T7: It commits nothing.** Everything runs inside the rolled-back `screener_session`
      transaction, so a suite that writes a scan run, three detection tables and a planted
      instrument leaves none of them behind — the discipline every db-marked module in that
      directory keeps, and the reason the shared Postgres survives being shared.
  CHECK: docker exec baskfy-postgres psql -U baskfy -d baskfy_agentT -tAc "select 'left_behind states=' || (select count(*) from tw_state_daily) || ' scans=' || (select count(*) from tw_scan_run) || ' planted=' || (select count(*) from instrument where symbol like 'PLANTED%')"
  EXPECT: /^left_behind states=0 scans=0 planted=0$/m
  EVIDENCE: `left_behind states=0 scans=0 planted=0`, read after a full run of the suite.

- [x] **T8: House rule 4 — lint, format and `mypy --strict` are clean on the new file.** No
      `# type: ignore`, no `Any`, no `noqa` (the two ruff added by habit were unused and were
      removed rather than kept).
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && printf 'ruff=%s fmt=%s noqa=%s mypy=%s\n' "$(uv run ruff check services/api/tests/test_twt_scan_to_page.py >/dev/null 2>&1 && echo clean || echo dirty)" "$(uv run ruff format --check services/api/tests/test_twt_scan_to_page.py >/dev/null 2>&1 && echo clean || echo dirty)" "$(grep -c 'noqa\|type: ignore' services/api/tests/test_twt_scan_to_page.py)" "$(uv run mypy services/api/tests/test_twt_scan_to_page.py 2>&1 | tail -1)"
  EXPECT: /^ruff=clean fmt=clean noqa=0 mypy=Success: no issues found in 1 source file$/m
  EVIDENCE: `ruff=clean fmt=clean noqa=0 mypy=Success: no issues found in 1 source file`.

---

## What this leaf deliberately did NOT do

* **It did not write the route.** Agent R owns `GET /twt/today`; this file never edited
  `apps/web/src/lib/twt/fetch.ts`, `routers/twt.py` or `baskfy_api/twt.py`. The mutants in T3–T5
  are pytest plugins written to a temp directory and deleted in the same command.
* **It did not widen to the rest of the payload.** `positions` and `half_size` are asserted only
  as *present* — the sleeve holds nothing yet and a test that pinned an empty book would lock in
  today's behaviour rather than the spec (house rule 2). Agent C's contract audit is the right
  place for the field-by-field comparison against `docs/twt/03`.
* **It did not touch `/twt/backtest`.** `fetch.ts` asks for that too and nothing serves it either.
  That is the same class of bug, and it is named here rather than fixed here.
