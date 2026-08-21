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
