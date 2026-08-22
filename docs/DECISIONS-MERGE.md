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

---

## M11 — RED GATE, not green

### M11.1 — `docs/05` §1's return base was wrong, and it is now corrected ⚠ UNREVIEWED
**The parity test runs for the first time.** With `BASKFY_PARITY_BARS` pointed at 1,138,300 real
adjusted bars exported from the restored backfill, `TestStep2FullRowReproduction` executes instead
of skipping — the first time in this repository's life.

It confirmed §21.7 independently. Sweeping every shift from 15 to 261 and counting exact matches
against the export reproduces the overnight run's table exactly:

| column | best shift | exact |
|---|---:|---:|
| `absolute_return_one_month` | **21** | 265 / 271 |
| `absolute_return_three_months` | **63** | 260 / 270 |
| `absolute_return_six_months` | **120** | 256 / 270 |
| `absolute_return_nine_months` | **183** | 250 / 269 |
| `absolute_return_one_year` | **245** | 240 / 268 |

At the documented base the counts are **0, 1, 0, 0, 0**.

**Fixed spec-first, in the order §21.7 prescribed.** `docs/05` §1 was hand-edited from
`P_{t-N}` to `P_{t-(N-1)}` — the base is the window's **first bar**, the calendar-offset window
being inclusive of both endpoints — with the measurement table and CUPID's worked example in the
document. Then one line of `baskfy_core.factors`: `shift(n)` → `shift(n - 1)`.

**Two consequences followed, and both are real.**

The **cross-validation oracle** disagreed, because `packages/core/tests/factor_oracle.py` is a
deliberately naive re-implementation of the spec. It moves when the spec moves, never to chase the
engine — updated with the same comment.

More interestingly, a golden test broke: *"an instrument without a full window gets null"*.
`docs/05` §Notation says a factor is "never computed on a short window, because that would break
the shared-denominator property" — and that guarantee turned out to be held up by **the
off-by-one itself**. With the base one bar before the window, a calendar holding exactly N days ran
out of bars and produced a null by accident. Correcting the base removed the accident and exposed
that **no actual check existed**. `FactorWindow.spans_full_window` is that check
(`calendar_first <= calendar_start`), and short windows now null explicitly.

**Suite after all of it: 1385 passed, 794 skipped — exactly the M0 baseline.**

### M11.2 — The gate is NOT green, and the residual is one precisely-stated question ⚠ UNREVIEWED
**6,934 of 9,166 compared cells still fail.** `absolute_return_one_month` reproduces; the rest do
not, and the reason is no longer the base — it is the **window lengths**.

Our calendar resolves 22 / **65** / **122** / 185 / 247 where `docs/13` §3 requires
22 / **64** / **121** / 185 / 247 (M10.1). With the base at `length − 1`, a 3-month length of 65
gives shift 64 where the export needs 63. **The two defects were coupled all along**: fixing the
base alone cannot reproduce a window whose length is wrong.

**An experiment settles what the answer looks like, and raises the cost of choosing it.** Making
the window *exclusive* of the boundary day — snap forward strictly past the calendar offset —
yields lengths 22/64/121/**184**/**246**, and `length − 1` then gives shifts
**21/63/120/183/245**: *exactly* the five measured best shifts, from one rule rather than five
fitted constants. Failures drop from 6,934 to **5,115**, the lowest of any configuration tried.

But it breaks `positive_days_percent` at 9 and 12 months (0 → 271/271 failures), because those are
the columns `docs/13` §3 used to **recover** N in the first place — by finding the minimal N making
all 271 values integer multiples of 1/N. That method is rigorous, and it says 185 and 247.

**So the return base and the positive-days denominator want windows that differ by one bar**, and
`docs/05` and `docs/13` do not reconcile. That is a specification question about what the product
computes for every user on every screen, not a debugging one.

**Decided: stop here, keep the evidenced fix, do not guess the window rule.** The base correction
is committed — one rule, five windows, overwhelming evidence, suite green. The boundary change is
**reverted**, because it fits three columns by breaking two others and contradicts the only
rigorous derivation anyone has of N. Choosing between them is Maulik's call.

**Rule 7 applies: M13 does not open.** Rule 11 does — M15, M16, M17, M19, M20 and M18's
script-building depend only on the code and the uploads corpus, not on these numbers, and the run
continues there.

### M11.3 — Nine columns are declared unverifiable, with their reasons
Reported by the harness itself rather than dropped: `beta` (needs the NIFTY 50 level series —
`index_snapshot_daily` holds 30 days, not the year `beta_12m` needs), `marketcap` (an optional
*input*, never computed; nothing fetches fundamentals), `high_all_time` and
`away_from_high_all_time` (need history from each listing date; ours starts 2024 — a running
maximum wearing an all-time label, §21.2), and the five `circuits_*` columns (§19.6: the export
carries a per-window count and no daily band, and more than one rule reproduces it).

### M11.4 — The failure report now groups by column
271 rows × ~60 columns meant the first forty failures were forty rows of one column. The assertion
now prints a per-column count before the examples, which is what turned "7,319 cells failed" into a
diagnosis. Small change; it is the reason the rest of this entry could be written.

### M11.5 — The per-family hypothesis, tested and refuted; one more real defect found ⚠ UNREVIEWED
Maulik's hypothesis: the window rule may not be global — bar factors read `K` bars inclusive with
base = first bar, interval factors read the `K−1` returns inside that same window. If it held for
all five windows, `docs/05` and `docs/13` would reconcile by naming both concepts.

**Tested by sweeping every family's parameter independently on real bars against all 271 rows.**
The full evidence table is `decile-blueprint/reconciliation/PARITY-FAMILIES.md`; it is P1.5's
written explanation.

**Refuted, and precisely.** `positive_days_percent` recovers **exactly** `docs/13` §3's
22/64/121/185/247 — an independent confirmation of that derivation. The return base wants
`K−1` for `K` = 22/64/121/**184**/**246**. The two families **agree at the short windows and
disagree by one bar at nine and twelve months**, which no single `K`-with-`K−1`-intervals scheme
can produce.

**Two columns turn out not to be window questions at all.** `volatility_*` and `rsi_*` reproduce
**0 of 268 at every parameter tried**. A grid over return type × `ddof` × annualisation × window
finds no exact combination for volatility; CUPID comes within 2.6e-6 at simple/`ddof=0`/√250/245,
but that combination reproduces **1 of 268** file-wide, so it is coincidence. `docs/05` §2
specifies sample stdev and √252 and the reference does neither. These belong with `circuits_*`:
**definitions the export cannot arbitrate**, reported unchecked rather than compared.

**And the sweep found a third real defect, independent of the window.** `high_one_year` and
`away_from_high_one_year` read `close`; the reference reads the **intraday high**:

| input | exact |
|---|---:|
| `max(high)` | **249 / 268** |
| `max(close)` | 6 / 268 |

**Flat across every window length from 243 to 248** — which is what proves it is the input and not
the window, and what made it safe to fix while the window question stays open. `docs/05` §10's own
worked example already used CUPID's `299.00`, the highest price it *traded* at, against the
`294.86` it closed at: the prose contradicted the example, and the engine implemented the prose.
Corrected spec-first, then the engine, then the oracle.

A property test failed on the change and was **not** the property's fault: `frame_for` synthesises
`high = close * 1.01`, so every bar now sits exactly 1/1.01 below its own high and
`away_high_ath` is a constant instead of exactly zero at the peak — hypothesis found two paths to
the same ratio 2e-13 apart. Compared with an epsilon now, with the reason written down.

**Step 3 — the stratification — answered in passing and worth stating.** ZENTEC's and
JINDALSTEL's `close` were the two cells `DECISIONS.md` §21.9 reported as out by exactly a dividend.
They now match to the paisa (1982.4 and 1101.5), which confirms **M10.2's look-ahead fix moved
these numbers** and that no future-dated action remains in the data.

**Decision, per Maulik's rule: per-family optima are incoherent, so keep only the evidenced
fixes.** Two landed (return base, intraday highs), taking failures **7,319 → 6,934 → 6,454** with
the suite green at its M0 baseline throughout. The boundary change stays reverted: it buys the
return family by breaking the family `docs/13`'s derivation rests on.

**M12 arbitrates.** The desk's `data/uploads/` corpus is the oracle that gates M13 — those CSVs
are what a real portfolio was scored, ordered and stopped against, and empty top-25 delta tables
settle the window semantics in the only terms that matter.

---

## M12 — the arbiter ran, and it renamed the problem

### M12.1 — 23 of 25, and the gap is missing corporate actions, not the window ⚠ UNREVIEWED
Both files through the desk's **unmodified** `scoring.py`. Full report:
`decile-blueprint/reconciliation/DESK-PARITY.md`.

**`sample_scan.csv` is excluded, with reason.** It is generated by `tests/make_sample_scan.py`,
whose docstring says to replace it with a real export, and it contains invented rows built to trip
filter branches — `TINYCO` ("Micro Cap Ltd"), `CIRCUITX`, `FALLENANG`, `DOWNTREND` — plus the SGB
the desk must never touch. Comparing a price engine against invented prices measures nothing. The
oracle is the 271-row scan, which is the real weekly export the desk was traded on **and** the same
file `docs/13` decodes.

**Result: 23 / 25 top-25 membership**, scores agreeing to a tenth at the top (`LAURUSLABS`
83.1 → 83.2, `CUPID` 76.9 → 76.8), and 15 of 19 rank deltas at ±3 or less.

**One delta is real, and it named the actual problem.** `SHILPAMED` falls #10 → #57. Its `ma_20`
and `ma_50` match **to the paisa** while `ma_100` and `ma_200` are wildly high and the one-year
return flips +55.51 → −21.31. That is the signature of an unapplied corporate action, and the
series confirms it: **778.75 on 2025-10-01, 384.95 on 2025-10-03** — an unadjusted split. Windows
that do not reach October 2025 are perfect; every window that crosses it is wrong.

**40 of 271 symbols — 14.8% — carry an unadjusted jump**, including `NESTLEIND` (×0.098),
`BAJFINANCE` (×0.100) and `ANGELONE` (×0.099). `corporate_action` holds **four rows**, because
§21.8 records that NSE serves only a recent window. The adjustment code is fine — M10 proved it on
CUPID — it has almost nothing to apply.

**This reframes M11.** A window disagreement of one bar cannot flip a return from +55% to −21%; an
unadjusted 2:1 split can. The dominant residual in both gates is **corporate-action history**, and
the window question is a smaller, second-order effect sitting behind it. Re-running the family
sweep after a corporate-action backfill is the right next measurement — the current optima are
contaminated by 15% of the universe carrying wrong prices at long horizons.

### M12.2 — Breadth cannot be reconciled, and that is M14's problem stated precisely
The desk stores `pct_above_20dma` over whatever scan was uploaded; the pipeline stores
`pct_above_50dma` and `pct_above_200dma` over a named index universe. **Different metrics, different
populations**, and there is no 20-day figure in the pipeline at all. The desk's single row is
derived from the synthetic fixture, so its population is invented too.

M14 points `_cash_pct` at `market_health_daily`. That needs either a `pct_above_20dma` column added
to the pipeline, or the desk's cash bands re-expressed against the 50-day figure — and the bands
are **calibrated on the 20-day number**, so the second is a strategy change, not a wiring change.
Recorded now so M14 does not discover it as a surprise.

### M12.3 — Verdict: not empty, M13 stays shut
Rule 7. The gate is materially closer than the cell-level parity implied, and the remaining gap has
a name and an owner: a corporate-action backfill needs a source that serves history — the same
dependency `NEEDS-MAULIK.md` item 3 already carries for bar depth. Queued there.

---

## M15 — partial: two modules in core, the regime collision closed

### M15.1 — `score.py` moved with its formulas byte-for-byte, and pandas came with it ⚠ UNREVIEWED
`app/scoring.py` → `baskfy_core.score`. **Two things changed and only two:**

1. **Configuration is passed, not imported.** The desk's `config.py` reads the environment at
   import; core's first law is that it touches nothing. The eleven constants arrive as `cfg`, a
   `Protocol` the desk's config module satisfies by duck typing. `RET_COLS` and `REQUIRED` were
   module-level lists derived from `MOMENTUM_BLEND`, so they became `ret_cols(cfg)` /
   `required(cfg)`.
2. **`load_scan` did not move.** It reads a CSV, which makes it a boundary, and boundaries stay at
   the desk (docs/04 §2). `app/scoring.py` is now that boundary plus a thin binding — so every
   existing `from .scoring import score, audit, load_scan, stop_from_vol` still works.

**Verified byte-identical, not asserted.** The pre-move file was checked out of git and run
alongside the moved one over the whole M12 corpus: `DataFrame.equals` on every cell of both scans,
for `score()` **and** `audit()`. All true. That comparison cannot survive the move, so its durable
form is `tests/fixtures/scoring_golden.json` + `tests/test_scoring_moved_to_core.py` — a digest of
the scored frame and its top ten, which answers the question that matters afterwards: *has the
strategy's output changed since?*

**pandas is now in core, and docs/02 says "pandas only at boundaries".** This is not a boundary.
A Polars port is the principled answer and it is **a rewrite with its own rounding**, against an
acceptance criterion of byte-identical output. Recorded as a deviation to revisit deliberately.
`packages/core` now depends on pandas transitively through this module alone.

### M15.2 — The desk's code met a type checker for the first time
The desk has no ruff or mypy gate; core has both, with `strict = true`. Moving two modules in
produced **27 ruff errors and 13 mypy errors**, none of which were defects — all of it was
idiomatic pandas meeting strict stubs, plus lines longer than a limit the desk never had.

Fixed properly rather than suppressed: `dict` type arguments, a narrower `Mapping[str, float]` for
`plan_cost`'s orders, `Collection[str]` on the config Protocol (which removed two `isin` errors),
an explicit annotation on the one running total that starts as a Series and is then combined with
`np.where` arrays, and four wrapped lines.

**One configuration exception, in the project's own idiom.** `PLR2004` (magic value in comparison)
is disabled for `score.py` via `per-file-ignores`, beside the two exceptions already there. Its
thresholds — RSI over 82, volatility over 0.55, extension over 25% — **are** the strategy, and they
are specified that way in the desk's `SKILL.md`. Lifting each to a named constant would rewrite the
one file whose whole point is that it did not change, and would move the numbers a step further
from the spec that defines them. Not a `# noqa`, not a `# type: ignore` — a stated, reviewable
line in `pyproject.toml`.

### M15.3 — `costs.py` moved unchanged; two tests learned to follow the code
Already pure — no config, no I/O — so it moved as-is. Two tests scanned `app/costs.py` **by path**
and one read the module `__doc__`; all three now resolve through the module object, so the next
move will not break them either.

The move initially rewrote the docstring, which **destroyed the documented cost breakdown a test
sums** — the Rs 8,583.95 table and the note about the Rs 7,695.87 subset that once mislabelled it.
Restored verbatim with the move note appended. Worth recording: a docstring can be load-bearing.

### M15.4 — The regime name collision is closed, and enforced
`baskfy_core.regime` → `baskfy_core.instrument_regime` (docs/03 §3c). One labels **an instrument**
bull/bear/neutral from a Wasserstein distance and decides nothing; the desk's overlay is a
**portfolio** R1–R4 tier that decides how much money is deployed. Confusing them is not a naming
quibble.

`packages/core/tests/test_regime_names_do_not_collide.py` enforces it by AST — no module may import
the ambiguous name, and no module may reason about both concepts at once.

### M15.5 — What remains of M15
Not done, and not started: **`rebalance.py` → core `basket.py`** (17 config constants plus a
`data/sectors.csv` read that must lift to a caller-supplied mapping) and **`core/regime.py` +
`regime_alloc.py` → core `exposure/`** (1,598 lines, already pure by design, with the requirement
that regime replay stay byte-identical to the persisted `regime_evaluations` rows).

Core's purity gates cover the moved modules automatically — `test_no_escape_hatches` scans
`src/baskfy_core`, and `network_guard` blocks sockets suite-wide — so M15 step 5 is satisfied for
what has moved and will be for what follows.

**Suites at the end of this commit:** decile **1434 passed, 794 skipped**; desk **1205 passed, 17
skipped**; `ruff check` and `ruff format --check` clean; `mypy` clean across 256 files.

### M15.6 — `rebalance.py` and the exposure overlay are in core; M15 is complete ⚠ UNREVIEWED
**`app/rebalance.py` → `baskfy_core.basket`.** Three things arrive as arguments now, each of which
was I/O or ambient state before: `cfg` (the seventeen strategy constants), `clusters` (the
symbol→sector mapping, whose `pd.read_csv` of `data/sectors.csv` stayed at the desk as a boundary),
and **`tradeable` — the untouchable-instrument guard**.

That last one is the place this refactor could have made the system *less* safe, so it has **no
default**. The guard exists because `EXCLUDED_SYMBOLS` held `"SGBDE31III"` while the holding was
`"SGBDE31III-GB"`, and the planner proposed **EXIT −392** on a ₹60 lakh position it must never
touch. Injecting it means a caller could pass something permissive; a required keyword-only
parameter turns "forgot the guard" into a `TypeError` at the call site instead of a silent
behaviour change. `tests/test_basket_moved_to_core.py` asserts the signature has no default, that
the desk binds `core.guards.assert_tradeable`, and that the planner still refuses `SGBDE31III-GB`.

**Verified identical, not asserted:** the pre-move file was run out of git alongside the moved one
over both scans at three cash levels (0, ₹2.5 L, ₹50 L) including an untouchable holding —
**every plan identical**, modulo the per-build `plan_id`.

**`app/core/regime.py` + `regime_alloc.py` → `baskfy_core.exposure`.** `regime.py` is
**byte-identical — zero changed lines**; `allocation.py` differs in one import. Both were already
written to core's first law, which is why the move was a `git mv`.

A full replay of the 13 stored `regime_evaluations` rows was **not** performed: each carries a
complete `input_snapshot_json`, but nothing reconstructs `IndexSignals`, `BreadthReading`,
`BookWeights` and `ExposureSnapshot` from it. What *was* verified is that `ALGORITHM_VERSION`
(`regime/1.0.0`) and the configuration fingerprint (`571bfb93ce64019f`) reproduce against **every**
stored row — the fingerprint walks the whole `RegimeConfig`, so it is a real check. The replay
harness is outstanding.

### M15.7 — A repo-wide `ruff --fix` rewrote 509 lines of live trading logic ⚠ UNREVIEWED
**My error, and the instructive one of this module.** After moving the exposure overlay I ran
`ruff check --fix .` and `ruff format .` across the repository. They rewrote **509 lines** of code I
had verified byte-identical minutes earlier: reflowing frozensets, unquoting annotations, moving
`Iterable`/`Mapping`/`Sequence` from `typing` to `collections.abc`, and **deleting a `replace`
import** judged unused because its only consumers reach it through the desk's star re-export. Five
desk tests went red, and the cause was a formatter rather than a decision.

**Restored verbatim from git**, and the rule is now in configuration so a future sweep cannot repeat
it: `[tool.ruff.format] exclude = ["packages/core/src/baskfy_core/exposure/*.py"]`. Excluded from
*formatting*, not from *linting* — `ruff check` still reads them, with the rules that would demand
restructuring scoped in `per-file-ignores`, each with its reason.

**Three dead locals are reported, not deleted.** `h20` and `sentinel_below_50` in
`classify_candidate`, `by_symbol` in the allocation solver. All three are pure assignments that
nothing reads — `h50`, `h200` and `sentinel_above_50` beside them *are* used, so these look like
leftovers rather than a missing condition. I removed them, then restored them with the file. In
code that sizes real orders, "looks dead" is worth a person's glance before it is worth an edit.

**mypy is scoped, not silenced.** These modules arrived from a tree with no type checker. Annotating
one return as `dict[str, object]` took the error count from **26 to 50**, because narrowing a
payload surfaces every downstream access at once. Doing it honestly means giving the plan, the
regime state and the allocation real shapes — TypedDicts or dataclasses — across ~1,900 lines whose
acceptance criterion is that they did not change. So `[[tool.mypy.overrides]]` relaxes exactly the
strictness that only bites because the payloads are untyped dicts today; `disallow_untyped_defs`
and the rest still apply. **That typing work is the real remaining debt from M15.**

**Final state:** decile **1438 passed, 794 skipped**; desk **1214 passed, 17 skipped**;
`ruff check`, `ruff format --check` and `mypy` all clean across 260 files.

---

## M16 — the execution package and the broker split

### M16.1 — `packages/execution` exists, and the gates fail closed ⚠ UNREVIEWED
`app/core/{guards,risk,ratelimit,gateway}.py` → `baskfy_execution`. **`guards.py`, `risk.py` and
`ratelimit.py` are byte-identical — zero changed lines.** `gateway.py` changed in one respect: the
three product switches it read off the desk's `config` module now arrive as an injected
`ProductGates`.

**They fail closed.** Every default refuses — `dry_run=True`, `intraday_enabled=False`,
`options_enabled=False`. A caller who forgets to supply gates gets *simulate, cash-equity only*,
which is the opposite of what a default usually does and the only sane choice for the module that
talks to a broker. It is also exactly non-negotiable 5: "both default off, enforced inside the
gateway."

**Passed as a callable, not a value.** `C.DRY_RUN` was read at the moment of each order, so a
setting changed mid-session took effect on the next order. Capturing the gates at construction
would have quietly made it take effect on the next *restart*. The desk's shim passes
`_gates_from_config`, which reads the config when the order is placed.

### M16.2 — The seven non-negotiables have seven named tests
`kite-momentum-rebalancer/tests/test_seven_non_negotiables.py`, 16 cases, one section per rule, in
the order `CLAUDE.md` lists them. Structural where the property is structural (the four layers
appear in order; nothing outside the gateway calls `place_order`, proven by AST over `app/`),
behavioural where it can be exercised (`DRY_RUN` places nothing; a gated product returns `BLOCKED`;
`SGBDE31III-GB` raises before the broker is touched).

They duplicate coverage that exists elsewhere, deliberately. The rules live across three trees now,
and this is the one file a reviewer can open to check that all seven still hold.

### M16.3 — The broker's two faces are disjoint, which is the enforceable half of the rule ⚠ UNREVIEWED
`docs/03` §3b: *a user's access token must never fetch universe data, and the system token must
never place an order.* **A token is an opaque string; nothing can type-check which one a caller
holds.** What can be enforced is that the two clients do not share a surface.

`baskfy_execution.brokers` names both sets — `MARKET_DATA_ONLY` and `TRADING_ONLY` — and
`tests/test_broker_faces.py` asserts `baskfy_providers.KiteProvider` has none of the trading
methods and the desk's `Kite` has none of the ingestion ones. If the market-data client never grows
`place_order`, the system token cannot reach the order path *at all*; if the trading client never
grows `daily_bars`, a user's token cannot run the pipeline by accident.

Weaker than per-token authorisation, much stronger than a comment, and it becomes load-bearing at
P4 when there is more than one account. The test lives in the **desk's** suite because that is the
only environment where both clients are importable; a copy under `packages/execution/tests` would
skip the half that matters and then drift.

### M16.4 — The access token is encrypted at rest, and the key story is stated honestly ⚠ UNREVIEWED
The token was plain JSON at mode 0600 — the right mode, and still a readable secret in any backup,
`rsync` or snapshot of the directory. It now goes through `baskfy_providers.tokens.AccessTokenStore`,
the screener's Fernet store, so the merged system keeps a Kite token exactly one way.

Verified by round-trip: the token value **does not appear** in the written bytes, it loads back
identically, and both files are 0600.

**On the key, plainly.** `KITE_TOKEN_ENCRYPTION_KEY` in the environment is the real protection — the
key lives where the ciphertext does not. When it is absent, `app/token_store.py` generates one
beside the token at 0600 and **logs that it did, saying what that is and is not worth**. A
co-located key defends the secret against being *copied* and does **not** defend it against anything
that can already read files as this user.

Rejected: refusing to store a token without configuration. It is the stricter choice and it breaks
login on a desk whose safety rail is that it must be able to rebalance on a Friday. The fallback is
loud, documented in `.env.example`, and stops being used the moment the variable is set.

**The old plaintext token is still on disk and self-heals.** It is expired, the new path refuses it
with a clear "log in again", and the next login overwrites that same path with ciphertext. Noted
rather than deleted: it is the operator's credential, not mine to remove. It is also in the M0
safety copy at `~/baskfy-safety/2026-08-22/data/` — **that copy contains a plaintext token**.

### M16.5 — Test source-scanning now follows the module, once, for all of them
Three separate modules broke the same way across M15 and M16: tests that assert structural
properties by reading `open("app/core/gateway.py")`. Good tests — they catch a refactor that
reorders the guard and the risk check, which no behavioural test would. The **path** was the
coupling.

`tests/_source.py` provides `src_of(module)`. Every such read now asks the module where it lives.
Fixed in `test_execute_gateway`, `test_execution_report`, `test_regime_plan`, `test_costs`,
`test_no_trade_band` and the new files.

### M16.6 — The typing debt is now four packages wide, and it is one job
`baskfy_execution` joins `score`, `basket` and `exposure` under the mypy override: `no-untyped-def`
and `no-untyped-call` are disabled for it, because annotating means editing code whose acceptance
criterion is that it did not change. The package **is** now type-checked (added to mypy's `files`
and `mypy_path`; 266 source files, up from 260) — it is the strictness that requires edits that is
relaxed, not the checking.

**This is the single largest piece of deliberate debt the merge has taken on**: roughly 2,300 lines
across four packages that a strict checker has never fully read. Doing it properly means giving the
plan, the regime state, the allocation and the order result real shapes. It is one coherent job and
it should be one module of its own, after M21.

**State:** decile **1438 passed, 794 skipped**; desk **1243 passed, 17 skipped**; `ruff check`,
`ruff format --check` and `mypy` clean across 266 files.

---

## M17 — the rank buffer, demoted to one rule

### M17.1 — `rebalance.py` → `rank_buffer.py`, and the band is now one function ⚠ UNREVIEWED
Renamed and rewired across seven consumers (`backtest.py`, `schemas.py`, `portfolios.py`, the
router, the worker's backtest, and two test modules). The name matters: `rebalance` implied a
second way to build a basket, and there is one — `baskfy_core.basket`, the desk's engine, which
turns ranks into orders with weights, cluster caps, cash bands, costs and stops. This module owns
the **rule**, not the construction.

**They were the same rule written twice, and now they are the same function.** The desk's planner
tested `r <= n + RETENTION_BUFFER`; the screener's tracker had the identical comparison written out
separately. `rank_buffer.inside_hold_band(rank, top_n, hold_buffer)` is that comparison, named once,
and `basket.py` calls it. Verified: **plans over the M12 corpus are identical** at three cash levels
after the change — it is genuinely the same test, not a near-miss.

**They are not the same rule end to end, and the difference is the interesting part.** The desk's
is this band **plus** a replacement-edge hurdle: a name that falls out of the band is *kept* unless
a challenger beats it by `REPLACEMENT_EDGE` points, where the tracker exits it outright. So the
desk's rule is strictly the richer one — which is what `docs/03` §3a claimed, now demonstrated
rather than asserted.

`packages/core/tests/test_rank_buffer_is_one_rule.py` pins both halves: the boundary is where
`docs/07` puts it (`rank == top_n + hold_buffer` **holds**, one worse exits), the tracker agrees
with the predicate on every rank, and the desk's source still contains **both** the shared call and
the hurdle — so a refactor that dropped `REPLACEMENT_EDGE`, quietly turning the desk's planner into
the tracker, fails here rather than in a live rebalance.

`cutoff_rank` became dead when `basket` started calling the predicate and was removed; unlike the
exposure overlay's dead locals, this one was dead *because of* the change and its removal is part
of it.

### M17.2 — A browser ran a Playwright spec for the first time in this repository's history
`e2e/portfolios.spec.ts`: **4 passed in 33.2s**, exit 0.

The screener's `CLAUDE.md` carried "The ten-journey Playwright suite has never been executed. It
type-checks; no browser has run it." **That is no longer true**, and the entry has been rewritten
to say what is now the case rather than left as a stale claim — the honest-open-items culture cuts
both ways.

Two things had to exist that never had. A **populated database**, which M7 restored. And a
**`baskfy_e2e` database**: `playwright.config.ts` migrates and seeds it but does not *create* it —
CI does that explicitly with a `psql -c "CREATE DATABASE baskfy_e2e"` step, and nothing local ever
had. The first attempt failed on exactly that, which is a real gap in the local setup and is now
recorded in the status page's how-to.

The run drove real endpoints — `/api/v1/plans`, `/listings`, `/indices/dashboard`,
`POST /screens/preview` — all 200. **The other nine journeys, including the backtest one, are still
unexecuted**; only the portfolios spec was run, because it is the one M17 asks about.

**State:** decile **1447 passed, 794 skipped**; desk **1243 passed, 17 skipped**; `ruff`, `format`
and `mypy` clean across 267 files; portfolios e2e **4 passed**.

---

## M18 — the migration, drilled; the cutover, deferred to where its mechanism lives

### M18.1 — 19 tables, 42,285 rows, and a one-paisa corruption caught ⚠ UNREVIEWED
Full report: `decile-blueprint/reconciliation/DB-MIGRATION.md`. Every row count and checksum
matched, the NAV series is identical, and `index_value` re-derives from `nav`.

The third assertion earned its place: changing one snapshot's `nav` by **a single paisa** in the
source left counts and checksums matching — the corruption was copied faithfully — and only the NAV
recomputation caught it. The run rolled back with nothing committed. A migration that only compares
bytes cannot tell a faithful copy of broken data from a correct one.

Checksums normalise through **one** function called by both databases, and sort row digests before
hashing so row order — which neither database guarantees and which is not part of the data — cannot
register as a difference.

**Landed in a `desk` schema, not `public`.** No collision with the screener's 42 tables today, and
relying on that is how a collision arrives later; `docs/04` §4 renames these at P4 regardless.
Rollback is `DROP SCHEMA desk CASCADE`.

**Truncate-and-reload, asserted as such.** The SQLite file is the source of truth and the schema is
a projection, so `--drop-existing` rebuilds it in one transaction; a populated schema without the
flag is refused with an actionable message rather than a `DuplicateTable` traceback. No upsert: until
M19 flips the backend, only SQLite is written, so "changed on both sides" cannot happen — and an
upsert would let a row deleted in SQLite survive in Postgres forever. Proven idempotent: two
consecutive runs produce identical reports.

### M18.2 — THE DIVERGENCE WINDOW: today's Postgres data is rehearsal, not truth ⚠ UNREVIEWED
**The desk still writes SQLite and nothing reads Postgres.** `app/analytics/db.py` opens `sqlite3`;
nothing under `app/` references Postgres at all.

So the `desk` schema is a faithful projection of the database **as it stood at 06:48 IST on 22 Aug
2026**, and stale the moment the desk writes another row. It is re-runnable rehearsal output. It is
not authoritative, and nothing should read it as current.

The cutover did **not** happen here, and **not because a precondition failed**. Three of four held:
assertions green on the copy with the NAV series identical; a fresh `scripts.backup` reporting `ok`
plus a refreshed offsite copy; and NSE closed on a Saturday morning with the next session ~50 hours
away. It did not happen because **there is nothing to cut over to** — the switch is M19's.

By the same logic `~/baskfy-safety/sqlite-archive/portfolio-2026-08-22-pre-postgres.db` is a
**rehearsal archive**, superseded at M19 by an archive of the state that is actually frozen at the
switch. It was taken from the sqlite-backup-API output rather than a `cp` of a live WAL-mode file —
I reached for `cp` first, which is precisely the hazard `deploy/README.md` warns about, and redid it.

**The real sequence, which M19 owns:** stop every desk writer → re-run the proven migration against
the live file → full assertions again → switch the desk to Postgres → **only then** archive that
SQLite state as the forever copy.

Precondition (c)'s "one-command rollback" gets its meaning at that switch. Today, "point the desk
back at SQLite" is not a rollback — it is the status quo, and exercising it proves nothing. Once the
config flip exists, flipping it back is the rollback, and that is what must be exercised.

---

## Point 2 — the token bridge

### TB.1 — fetch the token the box already has, rather than register a second redirect ⚠ UNREVIEWED
The Kite app's Redirect URL is `https://desk.modelbasket.in/callback` and **stays that way**. So the
login lands on the box and the token is minted there, while the merge's pipeline work runs on the
laptop and needs the same token.

Three ways out: register a second redirect against a **live trading app** (a production change for a
development convenience — no); copy the token by hand each morning (works, and gets skipped); or
fetch it over the SSH connection `deploy/sync.sh` already uses. `scripts/token_sync.py` +
`make token-sync TARGET=…` does the third.

**Verify before store.** The authenticated call runs *first*; a dead token never replaces a live one
on disk. **The token is never printed** — not stdout, not stderr, not a log record, and a test
asserts all three by running `main()` end to end and searching the captured output.

**`profile()`, not a bar fetch.** It costs no data quota and cannot fail for a reason unrelated to
the token. A `historical_data()` probe would 403 on the free data tier with a perfectly valid token
— the exact ambiguity NEEDS-MAULIK item 6 exists to remove.

**Reads both formats.** The box runs the pre-M16 layout until its next deliberate deploy, so its
token file is plain JSON today and a Fernet blob after. It tries JSON, then the encrypted store. The
laptop's copy is written encrypted either way.

### TB.2 — two defects the box found that review had not ⚠ UNREVIEWED
Running it against the real box, not a fixture, surfaced both:

1. **Pipeline exit status masked a permission failure.** The fingerprint probe ran
   `grep … | cut | sha256sum` as `ubuntu` against a file owned by `desk`. `grep` failed; the
   pipeline's status came from `cut`; the probe returned sha256 of *nothing* and the script
   confidently reported an api_key mismatch that did not exist. Fixed with `bash -o pipefail`.
2. **Nested quoting broke silently.** A `python3 -c` inside `sudo -n bash -c` inside an `ssh`
   argument survived one round of shell parsing and not two. Replaced with `sha256sum` on the far
   side and `shlex.quote` for the one level that remains.

Both produced a *plausible wrong answer* rather than an error, which is the failure mode worth
paying for a live run to catch.

### TB.3 — the run's finding: M9 and M10 stay blocked on a human ⚠ UNREVIEWED
The bridge works. The token does not: minted Friday 20:30 IST, dead since ~06:00 this morning. The
script diagnosed that itself — it compared api_key fingerprints across the two machines, found them
identical, and therefore named the token rather than guessing.

So **M9's 2011-depth backfill and M10's corporate-action work remain queued on one human login**, and
under the charter everything independent of them continues. What the bridge removed is the second
manual step, not the first: Kite mints tokens per human through a browser, and no amount of
engineering on this side changes that.

---

## M13 — the CSV cord is cut, and the cord stays plugged in

### M13.1 — the generated path is opt-in, not the default ⚠ UNREVIEWED
Rule 7 says M13 opens only on empty delta tables, and the table is not empty. The standing
instruction says finish M13. M13's own text says **"upload keeps working"**, and that is the reading
that satisfies both: everything M13 asks for is built, and none of it becomes the default.

`POST /analyze` still takes a file. `generate_for=YYYY-MM-DD` is the only way to the new path —
there is deliberately no "generate today's" shortcut, because opting in should cost a typed date
until the gate is green.

**Why that caution is not ceremonial:** a generated scan is currently a *smaller* tradeable universe
than an uploaded one. Measured on the 2026-08-18 corpus, the generated scan passes **223** symbols
where the upload passes **239** — and **fourteen of the sixteen** are rejected by `far_from_high`,
because an unadjusted pre-split high makes a stock look 80% below a peak it never reached. A desk
that silently stopped seeing sixteen names would look exactly like a desk that was working.

So the contamination is surfaced on the plan itself, in `plan["scan"]["warnings"]`, not only in a
log line nobody reads at 09:10.

### M13.2 — the contamination has a signature, and it accounts for all of it ⚠ UNREVIEWED
`momentum_scan.unadjusted_symbols` flags any overnight close-to-close step above **35%**. That
threshold sits between the two populations rather than inside either: NSE's circuit band caps a
genuine one-day move at 20%, and the smallest adjustment ratio in ordinary use (a 5:4 bonus) is a
20% step.

Every symbol whose `away_from_high_one_year` disagreed with the export by more than ten points had
one of these steps. **Every one.** `DIACABS` at −2.92 in the export and −80.63 generated;
`ANGELONE` stepping 2489.90 → 246.50 overnight; `MCX` 10989 → 2216.

This restates `NEEDS-MAULIK` item 4 far more sharply than "40 of 271 symbols carry unadjusted
actions". It is not blur in the fourth significant figure. **It removes tradeable names from the
universe.**

### M13.3 — an empty universe was a 500 ⚠ UNREVIEWED
`build_plan` divided by zero when nothing survived the filters — `sum(w.values())` over an empty
weight map. A market where every candidate sits below both its 50- and 200-day averages should
produce no buys, not a traceback in the middle of a rebalance.

It was unreachable in practice while every scan arrived as a 271-row upload. **M13 made it
reachable**, since a generated scan can come back with nothing tradeable in it. Guarded, with the
regression test one layer up as well: the route returns 200 with an empty plan.

### M13.4 — `screen_run_id` answers "what made this plan" for both paths ⚠ UNREVIEWED
A digest of the **inputs** — definition, as-of date, data version — not of the output. An output
digest tells you two runs differed without telling you why; the point is to resolve inputs.

An upload has no screen definition, so its honest answer is the file itself: `upload:<sha256[:16]>`
of the exact bytes. Same file, same id; one trailing newline different, different id. Both forms
land in `plan["scan"]` and in the persisted plan's note.

### M13.5 — three defects the fixture and the real database found ⚠ UNREVIEWED
1. **A join let the engine's empty `series` beat the carried one**, and `apply_filters` rejected
   sixteen names for a null series. Polars suffixes rather than merges; the earlier pandas version
   had the same defect and hid it. Now whatever the caller carries wins and the loser is dropped.
2. **`volume` names two different quantities** — a share count on a bar, exchange turnover in ₹ in
   the export (docs/13 §2 finding 5). Strict polars `rename` refused the collision; pandas had been
   silently producing two columns of that name.
3. **"452 of 271 symbols carry an unadjusted corporate action."** The detector ran across every bar
   fetched rather than the scan's own rows. A warning that is visibly wrong on its face is worse
   than no warning, because people learn to ignore it.

The first fixture was worse than useless before this: every symbol was rejected as illiquid at ₹1
crore turnover against a ₹5 crore floor, so "both paths produce the same plan" passed by comparing
an empty plan to an empty plan. The fixture now clears the floor, two names survive, and a test
asserts the plan is non-empty so it cannot quietly regress to comparing nothing with nothing.
