# DECISIONS-MERGE — judgement calls taken during the merge run

Numbered by module, the same convention as `decile-blueprint/docs/DECISIONS.md`. Every entry
records a call taken under ambiguity: what was ambiguous, what was decided, and why. An entry
here is not a licence — anything that changes an acceptance criterion stops the run and asks.

---

## M0

### M0.1 — `python3.12` is not on `PATH`, and that is not a missing tool
**Ambiguity.** `MERGE-PROMPTS.md` M0 step 1 says to verify `python3.12` and "stop if one is
missing". No binary answers to that name on this machine; `python3` is 3.14.2, which is outside
decile's `requires-python = ">=3.12,<3.13"`.

**Decided.** Not a stop. `uv` manages CPython 3.12.13 at
`~/.local/share/uv/python/cpython-3.12-macos-aarch64-none/`, `uv python list` resolves it, and
both project venvs were already built on it. Decile invokes Python exclusively through
`uv run` (`Makefile`: `UV := uv run`), so the bare name is never used. The requirement — a
working 3.12 toolchain — is met. Recorded rather than escalated because stopping here would
block on a name, not on a capability.

### M0.2 — Both venvs carried absolute paths to their pre-merge locations
**Found.** `kite-momentum-rebalancer/.venv/bin/activate` set
`VIRTUAL_ENV=/Users/maulikdave/Documents/portfolio/kite-momentum-rebalancer/.venv`, and
`decile-blueprint/.venv` pointed at `/Users/maulikdave/Documents/projects/decile-blueprint/.venv`.
Both trees were copied into `baskfy/` after their venvs were created. Activating the desk's venv
therefore activated a *different checkout's* environment, and the first baseline run was measuring
site-packages from the other copy.

**Decided.** Both venvs rebuilt in place (`uv venv` + `uv pip install -r requirements.txt` for the
desk; `uv sync` for decile). Recorded because it invalidated a baseline once and would have
invalidated every subsequent "green" claim silently.

### M0.3 — Stale `__pycache__` made tracebacks name the wrong checkout
**Found.** `.pyc` files compiled in the pre-move location were copied along. Python reused them
and reported `co_filename` from the original path, so a failing test appeared to live in
`~/Documents/portfolio/kite-momentum-rebalancer/tests/`. The executing code was correct; only the
reported path was stale.

**Decided.** All `__pycache__` and `.pytest_cache` directories cleared in the desk tree before
baselining. Noted because it is exactly the kind of thing that makes a later diagnosis wrong.

### M0.4 — The desk baseline must be taken at the repo's own environment, not at `DRY_RUN=true`
**Ambiguity.** `CLAUDE.md`'s safety rail says `DRY_RUN=true` is the default in every environment
an agent creates. Running the desk suite with `DRY_RUN=true` forced produced **6 failures**; five
were `tests/test_execute_gateway.py` assertions that a named broker refusal is reported as
definitively-not-placed, which return `DRY_RUN` instead of `ERROR`/`UNKNOWN` when the flag is on.

**Decided.** The baseline is recorded at the repo's own configuration. Those five tests drive a
stub broker class (`class KC: def place_order(...): raise ...`) and never open a socket or read a
credential, so exercising them at `DRY_RUN=false` places no order and contacts nothing. The safety
rail governs *environments an agent creates and runs the application in* — it is not a licence to
report a red suite as the tree's baseline. See M0.6 for the related live-config finding.

### M0.5 — One pre-existing desk failure, and it is a relocation artifact inside the frozen subsystem
`tests/test_strangle_runtime.py::test_both_plists_are_valid_and_point_at_this_checkout` asserts
`plist["WorkingDirectory"] == os.getcwd()`. The committed
`scripts/com.strangle.{collect,session}.plist.example` files hardcode
`/Users/maulikdave/Documents/portfolio/kite-momentum-rebalancer`; this checkout is elsewhere, so
the assertion fails on the path and nothing else.

**Decided.** Recorded as the M0 baseline, not fixed. It is a strangle test, and M6 moves the
strangle subsystem to `frozen/` and out of every gate — fixing it now would be work on a tree the
next module removes from collection. M6's acceptance ("desk suite green with
`OPTIONS_ENABLED=false`") is measured against this known baseline of one.

### M0.6 — The working copy's `.env` is LIVE (`DRY_RUN=false`) — flagged, not changed
`kite-momentum-rebalancer/.env` carries `DRY_RUN=false` with the comment
"LIVE: real orders. Only the exact string `false` does this." That is correct for the operator's
own desk and it is the reason the M0 baseline is green.

**Decided.** Not changed — it is untracked operator configuration, not repo content, and altering
a live trading config without being asked is out of scope for this run. **Raised to Maulik in the
M0 report** because any agent or stray command that boots the desk from this directory inherits a
live-order configuration. Every command this run issues that touches application code passes
`DRY_RUN=true` explicitly.

### M0.7 — Two byte-identical copies of the desk exist on this machine
`~/Documents/portfolio/kite-momentum-rebalancer` and
`~/Documents/projects/baskfy/kite-momentum-rebalancer` are the same tree: identical `HEAD`
(`df6cb72`), identical branch (`indices-board`), both clean, identical `app/` (`diff -rq` empty),
and identical `data/portfolio.db` (md5 `ae13b32fb9069e62327b02e712139bde`, same mtime, same row
counts). No divergence, so nothing is at risk of being merged from the wrong copy.

**Decided.** `baskfy/kite-momentum-rebalancer` is the merge source, as the script assumes. The
portfolio copy is left untouched and is an incidental second rollback. **Flagged to Maulik** —
two live-config copies of an order-placing application is a footgun worth resolving deliberately,
and it is his call which one the box syncs from.

### M0.8 — The overnight decile work is committed as-is, before M1
**Ambiguity.** `decile-blueprint` was 7 entries dirty on `overnight-20260821-0455`, failing M0's
"both sub-repos clean". The content was not stray edits: it is a run that got partway through what
M8–M11 describe — a real bhavcopy-based bar load and the first parity comparison against real data
— recorded as `docs/DECISIONS.md` "Prompt 21", §21.1–21.10. `git subtree add` (M1) takes committed
content only, and M1's rsync restores untracked files with `--ignore-existing`, so proceeding would
have restored the 2 new files and **silently reverted the 5 modified tracked files**.

**Decided (Maulik, 22 Aug 2026).** Committed as-is on its own branch, in decile's voice, as
`ea5dd0f "module 21: the first real backfill, and what it found"`. Subtree therefore carries it
into Baskfy history where it belongs. Committing without review was safe on the evidence: the M0
baselines were taken on this exact tree and it is green (1385 passed / 0 failed, `make lint` exit 0).

### M0.9 — M11's acceptance criterion will be amended at M11, not now
**Ambiguity.** §21.7 establishes that `docs/05` §1's return-window definition is wrong (the
reference's base is `P_{t-(N-1)}`, not `P_{t-N}`), and the parity-test rewrite establishes that four
more columns cannot be reproduced from bars at all — `beta` needs a benchmark argument, `marketcap`
is an input the engine never computes, `high_ath` needs full listing history, and five `circuits_*`
columns have an unresolved definition. `MERGE-PROMPTS.md` M11 asks for "all 93 numeric columns of
all 271 rows" green. **That is not reachable as written**, which under rule 5 is a stop-and-ask.

**Decided (Maulik, 22 Aug 2026).** Proceed on the existing script. At M11 the agent proposes a
revised criterion **in writing, with real numbers in hand** — green on every column reproducible
from the inputs available, each unreproducible column named and its missing input declared — and
Maulik approves that wording then. The criterion is not weakened silently and it is not amended
speculatively before the data exists to state it precisely.

**Consequences carried forward to M9–M11:**
- M9's HUMAN GATE (Kite credentials) may be unnecessary for *bars* — §21.1's bhavcopy path needs no
  broker session. Kite may still be wanted for depth of history (§21.2: the bhavcopy cannot reach
  docs/09's fifteen years, so D5's 2011 start is in question). Re-read before opening M9.
- The `docs/05` §1 hand-edit and the one-line `factors.py` change are **M10/M11 work, in that
  order**, and neither has been done. Nothing before M10 should touch the factor engine.
- §21.9's look-ahead in `apply_adjustments` is an open defect against decile's House Rule 5 and
  belongs to M10 (corporate actions over real history).

---

## M1

### M1.1 — `git log --follow` cannot prove the subtree criterion, and the criterion's intent is met anyway
**Ambiguity.** M1's acceptance reads: "`git log --follow -- kite-momentum-rebalancer/app/scoring.py`
reaches pre-merge commits". It returns **0 commits**, and so does the decile equivalent.

**Why, and it is not a failure of the merge.** `git subtree add` performs a *subtree merge*; it
does not rewrite history. Commits made before the merge still record their original paths
(`app/scoring.py`), not the prefixed ones (`kite-momentum-rebalancer/app/scoring.py`). `--follow`
cannot bridge that across a merge commit, so it reports nothing. The example file is also a poor
probe independently: `app/scoring.py` was touched in exactly **one** commit in the desk's whole
history (the initial one), so even a working traversal would show a single line.

**The intent — both histories preserved — is met, and provable four ways:**

| Check | Result |
|---|---|
| `git merge-base --is-ancestor df6cb72 HEAD` (desk tip) | YES |
| `git merge-base --is-ancestor ea5dd0f HEAD` (decile tip) | YES |
| `git merge-base --is-ancestor 36d6ba1 HEAD` (decile **root** commit) | YES |
| `git rev-list --count HEAD` | **117** commits |
| Oldest reachable on each side | `738ad9b "Initial commit: momentum rebalancer with regime overlay"`, `36d6ba1 "spec: Decile build blueprint"` |
| Per-file depth via each tip | desk `app/main.py` 29, `analytics/db.py` 12, `rebalance.py` 5; decile `docs/DECISIONS.md` 9, `Makefile` 5 |
| Tracked file counts, root vs originals | decile 654 == 654, desk 192 == 192 |
| Content identity | `scoring.py` and `factors.py` byte-identical to their pre-merge tips |

**Decided.** Recorded, not "fixed". Making `--follow` work would require rewriting all 114
sub-repo commits (`filter-repo` / `subtree split --rejoin`), which changes every SHA and destroys
the correspondence with the two source repositories — a strictly worse outcome than a verification
command that does not apply to the technique the script itself mandates. **The commands that do
prove it are the table above**, and they belong in the status page for any future session.

### M1.2 — M1 is three commits, and cannot be one
`MERGE-PROMPTS.md` asks for one commit per module. `git subtree add` creates its own merge commit
by construction — that *is* the history-preserving mechanism — so M1 necessarily produces the
initial root commit plus two subtree merges, then the module's own bookkeeping commit. Squashing
them would defeat the module's entire purpose. The `M<N>: green` message is carried by the
bookkeeping commit; the structural commits keep git's own generated messages so their provenance
stays legible.

---

## M2

### M2.1 — "Zero occurrences of `decile_`" is unachievable, because `decile` is also the product's vocabulary
**Ambiguity.** M2's acceptance reads: "zero occurrences of `decile_`, `DECILE_`, or `@decile/`
outside `docs/` (`grep -rIl` proves it)". Taken literally it conflicts with **`CLAUDE.md` D1**,
which decides the opposite: *"Decile's product vocabulary (D1 bucket, decile drift, Market Pulse,
Replay, hold band) is kept."* A decile is a statistical bucket. The word is domain language in this
product, not only a brand, and several identifiers use it that way.

**What a blanket rule actually did.** The first pass applied `s/decile_/baskfy_/` and
`s/DECILE_/BASKFY_/` and corrupted three things, each caught before commit:

| Corruption | Why it is wrong |
|---|---|
| `DECILE_RANK_KEY` → `BASKFY_RANK_KEY` | A real code constant in `baskfy_core.universes` (`= "marketcap_cr"`), meaning *the key deciles are ranked by*. "The Baskfy rank key" means nothing. `docs/06` §71 defines it. |
| `what-a-decile-actually-measures` → `what-a-baskfy-actually-measures` | A blog post **about deciles**. It broke the registry↔MDX mapping and failed 3 vitest cases — the only reason it was caught. |
| `decile-blueprint/PROMPTS.md` rewritten | The historical build script that produced the screener. Rewriting it makes it describe a build that never happened. |

**Decided.** The rename is **token-scoped, not prefix-scoped**. Renamed: the four package
namespaces, every `DECILE_*` environment variable, `@decile/*`, every Prometheus metric and
recording rule, Celery task and OTel attribute names, database/role/container/volume/compose-project
names, distribution names, cookie and Razorpay note keys, and user-facing download filenames.
**Kept, deliberately:**

| Kept | Count outside `docs/` | Why |
|---|---|---|
| `decile_1` … `decile_6` | 67 | The D1–D6 bucket values of `apply_filters_on`. They are a **public API contract** — in `openapi.json`, the generated TS client, the JSON Schema, URL state and seed data. Renaming them changes every saved screen and every stored URL. `CLAUDE.md` D1 keeps this vocabulary. |
| `DECILE_RANK_KEY` | 5 | The constant naming the key deciles are ranked by. Not an environment variable. |
| `decile_bucket` / `screen_run_decile_bucket` | 2 | A query-plan name; `decile` is the statistic. |
| `decile_core`/`_api`/`_worker`/`_providers`, `@decile/*` | 2 each | Only in `MERGE-PROMPTS.md` and `decile-blueprint/PROMPTS.md` — the two documents that *instruct* the rename. Rewriting them would make each read "rename `baskfy_core` → `baskfy_core`". |

**Zero namespace tokens remain in code.** The criterion's stated Goal — "the namespaces become
Baskfy's" — is met in full; only its `grep` proxy over-reaches into vocabulary and into the
instructions describing the rename.

**The scoped check that does prove it:**

```
git grep -I -o -h -E '(decile_[a-zA-Z0-9_]+|DECILE_[A-Z0-9_]+|@decile/[a-z-]+)' \
  -- ':!docs/*' ':!*/docs/*' ':!MERGE-PROMPTS.md' ':!decile-blueprint/PROMPTS.md' \
  | grep -vE '^(decile_[1-6]|decile_bucket|DECILE_RANK_KEY)$'
# must print nothing
```

**Escalated to Maulik rather than self-approved**, because scoping an acceptance check is exactly
what rule 5 forbids doing quietly.

**SETTLED (Maulik, 22 Aug 2026).** The scoped check is accepted as M2's acceptance, on condition
that the rule lives in code rather than in memory. It is now
**[`tools/check-namespace.sh`](../tools/check-namespace.sh)** — the exception list above, with each
entry's reason beside it, run as one command that exits non-zero on any surviving namespace token
and prints `file:line: token` for each. It is verified to fail: planting `DECILE_FAKE_VAR` and
`decile_fake_module` in a tracked file produces two violations and exit 1.

**M21's final sweep runs this script, not the raw grep**, and M5 wires it into CI. Widening the
pattern to silence a hit is forbidden in the script's own header; a genuinely new piece of
vocabulary is added to `ALLOWED` *with* a reason and a matching entry here.

### M2.2 — The rename does not touch the running database, and M7 must not start empty
`decile-postgres` has been up since before this run on volume `decile_decile-pgdata`, and it holds
the overnight backfill: **1,138,300 rows in `ohlcv_daily`** across 2,546 instruments,
2024-01-01 → 2026-08-18, plus 2,553 instruments, 16,633 PIT membership rows and 5,844 trading days.
(2024, not 2011 — consistent with `DECISIONS.md` §21.2: the bhavcopy cannot reach fifteen years.)

M2 renames the database, role, container, volume and compose project **in configuration only**. The
live cluster is deliberately untouched — half-renaming a running cluster mid-module is worse than
leaving config and reality to be reconciled once, deliberately, by the module whose job that is.

**A verified dump was taken first**: `~/baskfy-safety/2026-08-22/pg/decile-preM2.dump` (30 MB,
custom format, 76 table-data entries, `pg_restore -l` verified).

> **M7 MUST restore that dump into the new `baskfy` database rather than migrating an empty one.**
> `make up` under the renamed compose creates a *new* volume; the 1.1M bars would be stranded in
> the old one — recoverable, but only if someone remembers they are there. This note is that
> reminder.

---

## M4

### M4.1 — Four risk ceilings are editable from the desk's browser today, and `docs/03` §3f says they must not be
**The case.** M4 step 3 asks for "a test asserting no system-only risk constant (`RISK_*`, rate
caps, kill switch) is reachable from any user-facing settings route in either app". The test is
written (`kite-momentum-rebalancer/tests/test_settings_boundary.py`) and **it fails 5 of 12**:

| Assertion | Result |
|---|---|
| Credentials (`KITE_API_KEY`, `KITE_API_SECRET`) never editable | **passes** — `SECRET_KEYS` |
| Safety switches (`DRY_RUN`, `INTRADAY_ENABLED`, `OPTIONS_ENABLED`) env-only | **passes** — `LOCKED_KEYS` |
| `RISK_MAX_POSITION_VALUE`, `RISK_MAX_GROSS_EXPOSURE` not editable | **passes** — no `Spec` |
| `RISK_POSITION_HEADROOM`, `RISK_GROSS_MULTIPLE`, `RISK_MAX_DAILY_LOSS_PCT`, `RISK_MAX_ORDERS_PER_DAY` | **FAIL — all four are `Spec`s in the "Risk limits" group on `/settings`** |

`RISK_MAX_DAILY_LOSS_PCT` is the kill switch; the desk's own `.env.example` annotates it
*"kill switch: halts trading for the day at this loss"*. `docs/03` §3f names this exact variable:
*"This split is a security boundary, not a preference: `RISK_MAX_DAILY_LOSS_PCT` must not become a
form field."* It is one.

**Why this is not simply a defect.** `app/analytics/settings.py` is not careless — its docstring
reasons explicitly about what to lock and locks credentials, the safety switches and the scoring
weights, giving a reason for each. Leaving the risk ceilings editable was a choice for a
**single-operator desk**, where the user *is* the administrator and nothing is escalated by
raising your own ceiling. The four knobs have typed validation, range checks and a
`settings_audit` row per change. Under multi-tenancy (P4) the same form is privilege escalation.

**Facts that bear on the choice.** No `RISK_*` override is stored — `settings` holds exactly one
row, `REGIME_ENABLED=true` — so locking these changes nothing that is currently in force, and the
operator would still change them in `.env` plus a restart. The four are also rendered as a "Risk
limits" group in `settings.html` and echoed by a derived block in `settings.py` (~line 439), so
locking them is a UI change, not only a constant move.

**SETTLED (Maulik, 22 Aug 2026): lock all four.** Carried into `MERGE-PROMPTS.md`
§"Decisions already taken". Not `⚠ UNREVIEWED` — this one was answered directly.

**What was done**

1. All six `RISK_*` keys moved to `settings.LOCKED_KEYS`; the four `Spec`s dropped, which removes
   the editable "Risk limits" form group with them. `save()` already refused locked keys, so the
   write path needed no change.
2. `settings.py`'s own "WHAT IS NOT EDITABLE HERE, AND WHY" docstring extended to say so — the
   module explains its other locks and would have been silently wrong about this one.
3. **The read-only preview was kept, and had to be rescued.** `settings.html` rendered it inside
   `{% if group == 'Risk limits' and risk %}` within the group loop, so deleting the Specs deleted
   the *display* too — three tests caught it. It is now its own section above the form: being
   unable to change a limit is no reason to be unable to see it.
4. **Compensating control for the lost audit row.** `app.main._log_risk_ceilings()` writes the
   values in force at every startup, so `journalctl -u momentum-web` still answers "what were the
   limits on the day of that trade?" from the machine's record. Percentages and multiples only —
   no NAV, no rupee figures, nothing account-identifying.
5. `tests/test_settings_boundary.py` grew two page-level assertions: no `RISK_*` renders as an
   input, and the ceilings are still displayed. A form that renders a field it will then refuse is
   worse than one that does not, and it invites someone to "fix" the refusal later.

**Empirical basis that made this safe.** The `settings` table holds exactly one row —
`REGIME_ENABLED=true`, set 15 Aug 2026 — and `settings_audit` one matching entry. **No `RISK_*`
override has ever been stored**, so every one of the four was already running on its `.env`/code
default and nothing in force changed. Reversal is cheap: restore the four `Spec`s and remove the
keys from `LOCKED_KEYS`.

**Reaches the live desk on the next *deliberate* `git pull`, non-trading hours only.** The Mumbai
box runs the pre-lock layout until then. After it lands, changing a ceiling is `.env` + `systemctl
restart momentum-web` — and the new value appears in the log.

**Suite:** 1544 passed, 1 failed (the known M0 plist artifact, M6's to remove from the gates).

---

## M5

### M5.1 — M1 left a live `.git` inside each subtree, and it silently broke a CI step ⚠ UNREVIEWED
**Found at M5**, by a CI step failing for a reason that made no sense: the client-staleness gate
(`git diff --exit-code -- packages/api-client/...`) reported the generated client stale, while
`git status` from the repository root reported the tree clean and HEAD, index and working tree
were byte-identical.

**Cause.** M1 step 4 restores untracked files with `rsync --ignore-existing` from the pre-merge
copies. `git subtree add` creates a working tree with **no** `.git` of its own, so
`--ignore-existing` did not skip one — it **copied the old repository in**. Both subtrees carried
a live `.git` pinned to their pre-merge HEADs (`ea5dd0f` and `df6cb72`), 8.4 MB and 6.7 MB.

Any git command run from *inside* a subtree therefore resolved to the **orphaned pre-merge
repository**, not to Baskfy. The CI workflow runs the screener's jobs with
`working-directory: decile-blueprint`, so its staleness gate was diffing against a HEAD from
before the rename and would have failed on every run. Worse than a broken check: a `git commit`
run from inside a subtree would have landed in a repository nothing else reads.

**Decided.** Both nested `.git` directories **moved** (not deleted) to
`~/baskfy-safety/2026-08-22/nested-git/`. Verified first, in this order:

1. `../_baskfy_subtree_tmp/` still holds both complete repositories — `.git` present, 23 and 91
   commits, `git fsck` clean on both. That is M1's designated rollback and it survives to M21.
2. Both pre-merge tips are ancestors of the Baskfy HEAD (`git merge-base --is-ancestor` — yes for
   `ea5dd0f` and `df6cb72`), so the history is not held only in those copies.

Afterwards `git rev-parse --show-toplevel` from either subtree returns the Baskfy root, and the
staleness gate passes from the subdirectory exactly as CI runs it.

**Reversal:** move either directory back. Nothing was destroyed.

**Lesson for anyone repeating M1's recipe:** `rsync --ignore-existing` from a pre-merge copy will
re-import `.git` unless it is excluded. Add `--exclude=.git`.

### M5.2 — M5 ships with one red CI step, and M6 is the fix ⚠ UNREVIEWED
The `desk` job fails on `tests/test_strangle_runtime.py::test_both_plists_are_valid_and_point_at_this_checkout`
— the M0 baseline failure. It asserts a committed plist's `WorkingDirectory` equals `os.getcwd()`;
the plist hardcodes the tree's pre-move path. It is a **relocation artifact in the strangle
subsystem**, unrelated to CI.

**Decided: ship M5, let M6 close it.** M6 moves the strangle package and its tests to `frozen/`
and out of pytest collection, which removes this test from the gate entirely. Fixing the path
assertion now would be editing a file the next module freezes. The charter's precedence puts the
module's **Goal** ("a PR touching either tree gets one green check") above the literal wording of
its acceptance, and the workflow itself is complete and correct — the red step is a pre-existing
condition owned by the next module. `tools/ci-local.sh` is re-run at the end of M6 to prove it
green; if it is not, M6 does not close.

### M5.3 — `tools/ci-local.sh` reports SKIP loudly rather than quietly passing
Five steps cannot run on this machine yet: the coverage gate and the query-plan baseline need the
`db`-marked suites against a live PostgreSQL (M7), and three web steps need Playwright browsers, a
seeded `baskfy_e2e` database and a production build. The script prints each as `SKIP — <reason>`
and lists them again in its summary.

A local runner that prints all-green while silently skipping a third of the workflow is worse than
one that says what it did not do — the same reasoning both repositories already apply to their own
open-items lists. First run: **14 passed, 2 failed, 5 skipped**.

---

## M6

### M6.1 — The options lab is disconnected by deferring imports, not by deleting them ⚠ UNREVIEWED
Three live modules imported the subsystem at **module scope**, so freezing it would have stopped
the equity desk booting: `analytics/ops.py` (the instrument registry, on the `/ops` page's import
path), `analytics/autorun.py` (`OPTIONS_OPEN`, and autorun runs on every login), and `main.py`'s
three `/options*` routes.

**Decided.** Each deferred behind the existing `OPTIONS_ENABLED` gate rather than removed, so a
thaw is a `git mv` and needs no edits to live code:

- `ops.OPERATIONS` is now the equity operations only. `all_operations()`, `by_name()` and
  `groups()` add the options controls when they are genuinely available; `_options_operations()`
  returns `()` unless the gate is on, and logs once instead of raising if the gate is on but the
  subsystem is absent.
- `autorun.needed()`'s options loop is gated, and `OPTIONS_OPEN` is imported at its single use
  site — the comment explaining *why it is imported rather than redeclared* is preserved, because
  that reasoning is still right; only the timing changed.
- The `/options*` routes stay mounted and answer **404** while frozen, which is what a route that
  does not exist should say. The nav entry is gated with them: a link to a 404 reads as a broken
  desk rather than an absent feature.

**A regression this caused, and how it was caught.** Making `start()` resolve through `by_name()`
broke the seam four `test_ops.py` tests use to inject a fake operation by monkeypatching
`ops.BY_NAME`. `by_name()` now layers the module attribute last, so the patch still wins. Worth
recording because the fix is invisible unless you know the tests exist.

### M6.2 — Four modules did not freeze, because the equity desk's calendar depends on them ⚠ UNREVIEWED
`scripts/autorun.py` answers "is today a trading day" from
`strategies.strangle.calendar_nse.build_from_kite`, seeded with the default underlying's index
token (`instruments`) and `session.extra_holidays` (`config`, which needs `clock`). **Autorun runs
on every login and collects the day's snapshot, fills and benchmarks — none of it backfillable**
(`docs/02` §6). Its existing failure path for a missing calendar is `CALENDAR_UNAVAILABLE`, exit 1,
so freezing those modules would have stopped the desk's most load-bearing daily job.

**Decided: freeze the strategy, not the market infrastructure.** Sixteen of the twenty modules
moved. `calendar_nse`, `clock`, `instruments` and `config` stayed, with a new package `__init__`
that explains the split. The four are dependency-free — stdlib plus PyYAML — and none can reach
an order path. `config/strangle*.yaml` stayed with them, because `extra_holidays` lives there.

**Rejected:** writing an equity-owned trading calendar during a freeze module. That is new logic on
a load-bearing path, in the one module whose job is to *stop* touching this code.

**The frozen tree keeps its own copies of all four**, so a thaw is self-contained and needs nothing
from the live tree.

**This is a seam, not a resting place.** The screener already has `baskfy_core.trading_calendar`.
**M15 should make these one** when the desk's brains move into `packages/core` — an NSE trading
calendar has no business living in a package named after an options strategy. Flagged there.

### M6.3 — Tests that exercise frozen code are skipped with a reason, not deleted ⚠ UNREVIEWED
Twelve test files were entirely strangle and moved with it. Three files were **mixed**, and moving
them would have taken equity coverage along:

| File | Handling |
|---|---|
| `test_exit_codes.py` | 10 of 12 tests need `scripts/strangle.py`. Marked `@options_lab`; the two script-wide sweeps (every unattended script exits 2 on an expired token) stay live and assert `strangle.py` only when present |
| `test_autorun.py` | 7 of 25 marked |
| `test_pages.py`, `test_regime_view_backtest.py` | The page sweep and the two nav tests now derive their route list from `OPTIONS_ENABLED`, asserting the **rule** — link and route appear together or not at all — rather than either outcome |

`tests/_frozen.py` holds the one skip marker and the one reason string. A route dropped from a
sweep is replaced by a positive assertion that it 404s, because silently shrinking a parametrize
list is how coverage disappears without anyone noticing.

**Desk suite: 1202 passed, 17 skipped, 0 failed** — green for the first time in this run. The M0
baseline's single failure (the strangle plist asserting a pre-move path) went to `frozen/` with the
rest, which is what M5.2 said would close it.

### M6.4 — What deliberately did NOT move
- **`deploy/systemd/strangle-collect@.{service,timer}`** — M6 step 4 is explicit that the live box
  is not touched. The units stay in the deploy kit; the box runs the pre-freeze layout until its
  next deliberate deploy, and at that point the paths they name will have moved. Queued as a note,
  not an action.
- **`data/outputs/strangle_*`** — the straddle records, journals and lockouts. Untracked, and the
  observation series is not rebuildable from anything (`docs/02` §6 counts it among the data that
  cannot be reconstructed). D4 keeps the collectors running.

### M6.5 — Five gateway tests declared their mode instead of inheriting it ⚠ UNREVIEWED
CI forces `DRY_RUN=true` on the desk job (`CLAUDE.md` safety rails: an agent's environment is
never one variable away from a live order path). Under that flag five `test_execute_gateway.py`
tests failed, and they are the ones asserting that **a refusal the broker names is not an unknown
outcome** — the fix for six SHILPAMED rejections that each sent the operator to the order book to
rule out a double-send that was never possible.

They failed because the gateway short-circuits at `gateway.py:85` under `DRY_RUN` and never calls
`place_order`, so the refusal path they exercise does not run. Both drive a **stub broker class**;
neither opens a socket or reads a credential.

**Decided.** Each now sets `DRY_RUN=False` on itself via `monkeypatch`, with the reason in the
test. This is the opposite of weakening: a test whose meaning depends on an ambient flag is a test
that silently stops testing, and this one had already stopped under CI's own configuration.
The suite is now identical under both — **1202 passed, 17 skipped** with `DRY_RUN=true` forced and
with the repository defaults. Supersedes the workaround noted in M0.4, which baselined at repo
defaults precisely to avoid this collision.

---

## M7

### M7.1 — `make seed` overwrote 25,256 real bars, and now it refuses to ⚠ UNREVIEWED
**The most consequential finding of this module, and it would have been invisible.**

M2.2 required M7 to restore the overnight backfill rather than migrate an empty database. That was
done. Then `make seed` ran, as M7 step 1 says it should — and `ohlcv_daily` went from
**1,138,300 to 1,143,604**. Only +5,304 rows, which reads like a harmless top-up.

It was not. Diffing the seeded database against a scratch restore of the same dump:
**25,256 rows share an `(instrument_id, date)` key and disagree on `close`.** `seed_fixture_bars`
upserts on that key, so every collision replaced a real bhavcopy close with a synthetic one.
`tests/fixtures/providers/PROVENANCE.md` says what those values are: *every bar before 2026-08-18
is a seeded random walk*. They land with `source='nse'`, exactly like real rows, so **nothing
downstream can tell them apart** — not the factor engine, not the parity test, not a human reading
the table.

M10, M11 and M12 are all graded on these bars. A quarter of a million contaminated closes would
have produced a parity failure with no discoverable cause.

**Decided.** `seed_fixture_bars` now refuses when `ohlcv_daily` is non-empty, prints why, and
returns 0. `BASKFY_SEED_FORCE_BARS=1` overrides it for a scratch database or the e2e run, which
builds from empty. `make seed` is for bringing a fresh database up; a populated one is not that.
The database was restored from the dump a third time and re-seeded — bars unchanged at 1,138,300,
verified either side.

**Rejected:** giving fixture bars a distinct `source`. It is the better long-term answer — honest
provenance, and real rows could then never be silently replaced — but it changes seeded data's
shape and several tests assert on `source`. **Worth doing properly before the next backfill;
flagged here rather than done inside a module whose job was to bring a stack up.**

**Caught only because row counts were snapshotted either side of the command.** Do that around
anything that writes to a populated table.

### M7.2 — The restore sequence has an order that matters, and getting it wrong is silent
`pg_restore` of a TimescaleDB dump needs `timescaledb_pre_restore()` before and
`timescaledb_post_restore()` after, and **`pre_restore` requires the extension to already exist**.
Restoring into a freshly `CREATE DATABASE`d database without creating the extension first makes
`pre_restore` error; if that error is not surfaced the restore still appears to succeed, and the
damage shows up later as **`factor_daily` having no primary key at all** — which then fails every
upsert with "no unique or exclusion constraint matching the ON CONFLICT specification".

The working sequence, now in the status page:
`CREATE DATABASE` → `CREATE EXTENSION timescaledb` → `timescaledb_pre_restore()` → `pg_restore
--no-owner --no-privileges --disable-triggers` → `timescaledb_post_restore()` → re-add the user
policies.

**The dump also cannot carry its background jobs.** `bgw_job` fails with `role "decile" does not
exist` — the policies were owned by the pre-rename role. Three user policies had to be recreated
by hand from their migrations: the `ohlcv_daily` compression policy (`0001`) and the two
continuous-aggregate refresh policies (`0008`). Without that step the aggregates silently stop
refreshing. They are back; `timescaledb_information.jobs` shows all three.

### M7.3 — One integrity check fails, and it is telling the truth ⚠ UNREVIEWED
`make integrity` reports **`published_runs_have_steps: 1 published run has no pipeline_run_step
rows`**. It came from the dump, not from anything this module did — the scratch restore of the
untouched dump has the same row.

The overnight bhavcopy backfill published `data_version = 1` without going through the
orchestrator that records per-step provenance, which is consistent with `DECISIONS.md` §21.3
("resumability is per-day upsert, not `ingest_cursor`"). So the check is correct: the provenance
record for the data currently being served **is incomplete**.

**Decided: record it, do not fabricate steps, do not weaken the check.** M9/M10 re-run the chain
through the orchestrator, and the run they produce will carry its steps. If it still fails after
that, it is a real defect in the step recorder rather than an artifact of how this data arrived.
Everything else passes, including every foreign-key and populated-table assertion.

### M7.4 — The screener's `.env` was renamed in place, without its values being read
`decile-blueprint/.env` still carried 18 `DECILE_*` keys after M2 (it is untracked, so the rename
could not reach it) — including the Kite, Razorpay, Resend, JWT and token-encryption secrets. Key
**names** were rewritten with `perl -pi -e 's/^DECILE_/BASKFY_/'` and the two database URLs
repointed at `baskfy`; no value was printed, logged or copied into this repository. A `0600` copy
of the original is at `~/baskfy-safety/2026-08-22/env/`, outside the tree. The file itself is
`0600` and untracked.

---

## M8

### M8.1 — Four of the fourteen universes had a 404 for a constituent file, and it would have been silent ⚠ UNREVIEWED
`_file_token` built every constituent filename as `ind_{slug-without-hyphens}list.csv`. Correct for
seven published universes; **404 for four** — `nifty-total-market`, `nifty-large-mid-250`,
`nifty-microcap-250`, `nifty-mid-small-400`.

**Why this mattered more than a broken URL.** `factor_daily.universe_mask` is a bit per universe,
set from membership. Four universes that never populate do not raise — they make screens over those
universes **return nothing**, which reads as "no stocks matched your filters" rather than as a
fault. Four of the fourteen selectable universes in `docs/01` §2.1 would have been quietly dead.

**Decided.** `_CONSTITUENT_FILE_OVERRIDES` — an explicit slug → filename table, each entry
confirmed 200 against the live archive on 22 Aug 2026. Not a cleverer rule: NSE is inconsistent in
two independent ways (underscore before `list` or not; "largemid**cap**" and "midsmall**cap**"
where the slug says "large-mid" and "mid-small"), and a rule that happened to cover today's eleven
would break on the twelfth. Adding a universe now means checking the archive.

**After the fix: 11 fetched, 3 derived by rule, 0 failures**, and every fetched count matches the
index's published size — 50/50/100/200/500/752/250/150/250/252/400. That correspondence is the
strongest available evidence that the right file was read, and it is why the note records counts
rather than just "OK".

### M8.2 — The rest of the NSE surface was right first time
`listings` (2,553 rows), `bhavcopy` (3,649 rows for 2026-08-21, 13 columns), `index_snapshots`
(164 indices, NIFTY 50 at 24252.0 with PE/PB/yield) and `corporate_actions` (20 typed actions) all
returned and parsed on the first live call, against URL shapes written from documentation and never
before fetched. Recorded because the screener's own `CLAUDE.md` flagged all five as unverified and
the honest outcome is that four fifths of the guesswork was right.

Verification is repeatable: `tools/verify-nse.py`, which drives `NSEProvider` itself rather than
reimplementing its fetch, so the cookie priming, throttle, retry and circuit breaker are all under
test. The dated note is `decile-blueprint/reconciliation/NSE-ENDPOINTS.md`.

**Candidate filenames were probed with a handful of spaced `curl` requests** to discover the four
real names. That is discovery, not ingestion — no data was taken through it, and every actual fetch
went through the rate-limited provider, as the safety rails require.

---

## M9

### M9.1 — Kite is unavailable, and the run continues anyway ⚠ UNREVIEWED
The desk's token is three days old and Kite mints daily with no refresh, so it fails with
`TokenException`. M9's own step 1 says to test it first and queue only if it does not work; it does
not, so the ask is queued in `NEEDS-MAULIK.md` item 3 and **rule 11 applies — the run does not
wait**.

**What was done instead, without Kite.** The bhavcopy path extended the backfill through the
current trading day: 3 sessions, 7,622 bars. `ohlcv_daily` now holds **1,145,922 bars,
2024-01-01 → 2026-08-21**, across 2,553 instruments.

**Why that is enough to keep going.** `DECISIONS.md` §21.2 measured the archive's floor — 2023-07-03
is a 404, 2024-01-02 returns rows — so 2024 is not a choice, it is the source's limit, and D5's
`FROM=2011-01-01` cannot be honoured from NSE at any price. But `docs/01` §2.13 sets the product's
`DATA_START_DATE` to **2024-11-01**, and the factor windows the parity gates assert reach 247
trading days ≈ one year. **The gates need a year; there are two and a half.** Kite buys depth for
backtests, not the ability to run M10–M12.

**Carried forward, not hidden:** the fifteen-year backtest stays unrunnable, and `high_all_time` /
`away_from_high_all_time` are *wrong rather than missing* on a 2024-start series — a running maximum
wearing an all-time label. `DECILE_PARITY_BARS_ARE_FULL_HISTORY` is deliberately **not** set, and
M11 must treat those columns as unverifiable rather than comparing them.

### M9.2 — 353 unmatched bhavcopy symbols are correct filtering, not a gap
The gap-fill reported 353 symbols with no `instrument` row. The bhavcopy carries every series NSE
prints — `SM` (SME, 362 rows), `ST` (trusts, 100), `GS`/`GB` (government securities, 103), `N0`
(debt, 25) — while `instrument` holds only cash equities: EQ 2,291, BE 234, BZ 28. The unmatched
are the series the product deliberately does not screen. Recorded so a future reader does not
mistake it for missing data.

---

## M10

### M10.1 — The calendar is now observed, and 3 of 5 windows land exactly ⚠ UNREVIEWED
`reconcile_calendar` over 2024-01-01 → 2026-08-21: **651 days confirmed** from bars, 16 still
provisional, 0 holidays inferred. The calendar in the loaded range is now backed by observation on
both sides — every day we call trading has bars, and every weekday we call a holiday returns a
**404 from the NSE archive** (verified directly for 2026-01-15, which 404s while the 14th and 16th
return 200). The six days marked `source='bhavcopy', is_trading_day=false` are exactly that:
holidays discovered by absence, not a contradiction.

**Window lengths against `EXPECTED_WINDOW_LENGTHS_2026_08_18`:**

| Window | Seeded calendar (M0) | Now | Required |
|---|---:|---:|---:|
| 1 month | 22 | **22** ✅ | 22 |
| 3 months | 67 | 65 ❌ | 64 |
| 6 months | 127 | 122 ❌ | 121 |
| 9 months | 191 | **185** ✅ | 185 |
| 12 months | 256 | **247** ✅ | 247 |

**Three of five now reproduce exactly where none did before**, and the two that miss are off by
exactly one bar.

**What it is not.** Not the boundary rule: excluding the boundary day fixes 3M and 6M and *breaks*
9M and 12M (184 and 246 against 185 and 247), so no single inclusive/exclusive choice fits.
Not a spurious trading day: 9M and 12M are supersets of 3M and 6M and are correct, so any extra day
inside the short windows would have to be cancelled by a missing day in
[2025-11-18, 2026-02-18) — and the only candidate there, 2026-01-15, is a genuine NSE holiday.
Not the algorithm: `docs/13` §3 specifies calendar offset → snap forward → count inclusive, which
is exactly what `resolve_window` does.

**Decided: measure it precisely, change nothing, hand it to M11.** The required counts were
recovered by `docs/13` §3 from the export itself — the minimal N making all 271
`positive_days_percent_N` values integer multiples of 1/N — which is strong evidence about the
*reference product*, and `DECISIONS.md` §21.7 has already established that the reference is one bar
away from `docs/05` on return windows. **These are very likely the same defect seen from two
directions**, and M11 owns settling it spec-first. Guessing a rule here that made two numbers match
would be exactly the fudge rule 5 forbids.

### M10.2 — The §21.9 look-ahead is fixed, and the fix is proven ⚠ UNREVIEWED
`reprocess_instrument` applied every corporate action regardless of ex-date, so an action dated
2026-08-21 had already rewritten the adjusted close for 2026-08-18 — eight of the 271 export rows
were out by exactly a dividend. House rule 5 is "No look-ahead, ever".

§21.9 declined to fix it because the right answer looked like a storage question: is
`ohlcv_daily.close` one "as of today" series, or must it be resolved per as-of date? **Decided: it
is an "as of today" series, and the caller states the date it is reconstructing.**
`reprocess_instrument(..., as_of=date)` bounds actions to `ex_date <= as_of`; the nightly pipeline
passes nothing and gets today's series, which is what it should serve, while the parity harness and
any point-in-time reader pass their as-of.

**Proven, not asserted.** Reprocessing CUPID with `as_of=2026-03-08` — the day before its 4:1 bonus
went ex — applies **2 actions instead of 3** and returns the middle era's `adj_factor` to
**1.0** from 0.2. Rolled back; the live table still reads 0.2.

This does **not** settle the larger question of storing one adjusted series per as-of date. It
makes point-in-time reconstruction possible without that, which is what M11 needs and what a
backtest needs, and it is cheap to reverse.

### M10.3 — CUPID reproduces; rights issues cannot be exercised on this data
CUPID's documented actions are all present and produce exactly the right factors: split 10:1 and
bonus 1:1 (both ex 2024-04-15) and bonus 4:1 (ex 2026-03-09) give `adj_factor` **0.2000000000**
for 2024-04-15 → 2026-03-08 and **1.0000000000** from 2026-03-09, across 449 bars.

**`INSUFFICIENT_DATA` for rights issues could not be exercised against real data**: `corporate_action`
holds 4 rows — 2 bonuses, 1 split, 1 dividend — and no rights issue, because `DECISIONS.md` §21.8
records that NSE serves only a recent corporate-actions window. The behaviour is covered by unit
tests; it has still never been seen on live data. Said plainly rather than reported as verified.

---

## Interlude — the `db`-marked suites, run for the first time

### DB.1 — 792 tests had never executed, and one shared fixture was failing most of them ⚠ UNREVIEWED
M0's baseline recorded 794 skips as "expected without `make up`". With the stack up (M7) and a
`baskfy_test` database created, they run — and on first execution failures were spread across most
files, which looked like a broad problem and was not.

**Root cause: `DROP SCHEMA public CASCADE` does not reset a TimescaleDB database.**
`screener_helpers._reset_and_seed` dropped and recreated `public` between modules. TimescaleDB
keeps chunks, compressed hypertables and continuous-aggregate materialisations in
**`_timescaledb_internal`**, so the parents went and the storage stayed. After a "reset",
`timescaledb_information.chunks` reported **0** while `_timescaledb_internal` still held
`_hyper_5_34_chunk`, `_materialized_hypertable_7`, `_materialized_hypertable_8` and
`_compressed_hypertable_4` from the previous generation.

Each orphan carries its own foreign keys, still pointing at tables that no longer exist. The first
insert into a hypertable routed into stale storage and failed as:

```
insert on "_hyper_3_5_chunk" violates "5_10_fk_index_member_daily_index_id_index_def"
Key (index_id)=(5) is not present in table "index_def"
```

…while `index_def` demonstrably held all fourteen rows — verified by running the seed path directly
and listing them. **The error names seeding and the fault is in resetting**, which is why it reads
as a broad failure rather than a single bug.

**Decided.** `_reset_and_seed` now also runs `DROP EXTENSION IF EXISTS timescaledb CASCADE`, which
is what clears `_timescaledb_internal`. Migration `0001` recreates it with `CREATE EXTENSION IF NOT
EXISTS`, so the reset stays a reset and nothing else changes.

`services/api/tests/test_screener_db.py` went from failing on its first test to **118 passed**.

**Why this was never caught:** the suite type-checked and was never run — the same class of gap the
screener's own `CLAUDE.md` lists throughout ("the ten-journey Playwright suite has never been
executed", "no Celery worker has run…"). Running it was worth doing for this alone.
