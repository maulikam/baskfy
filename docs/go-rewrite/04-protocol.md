# 04 — Working protocol for the nine terminals

## Branches and worktrees

`tools/rewrite/setup-worktrees.sh` creates, from a clean `developer`:

- branch `go/main` — the integration branch; only L0 commits to it (via `merge-lane.sh`).
- branches `go/L0` … `go/L8`, each checked out in `../baskfy-wt/L<n>/` (a git worktree of the
  same repo, so every agent sees the full Python trees read-only and its own Go directories).

An agent works only in its worktree, commits only to its branch, and **never** runs
`git checkout`, `git rebase -i`, `git push --force`, or touches another worktree.
Rebase onto `go/main` at every gate: `git fetch . go/main && git rebase go/main`
(conflicts in `internal/db/gen/**` or `internal/api/server/**` are resolved by regenerating —
`make sqlc` / `make oapi` — never by hand).

## Ownership (the no-conflict rule)

Write only under: your Go directories (01 §Layout), `go/internal/db/queries/L<n>_*.sql`,
`go/testdata/golden/L<n>/`, `tools/parity/dump_L<n>*.py`, `docs/go-rewrite/status/L<n>.md`,
your row in `STATUS.md`, your entries in `DECISIONS-GO.md` and `REQUESTS.md`, and a `## Go
rewrite` section of `NEEDS-MAULIK.md`. Generated directories are regenerated, not edited.
Everything else is someone else's — ask in `REQUESTS.md`.

## REQUESTS.md — asking for what you don't own

Append a block:

```
### R-<lane>-<n> · <what> · <status: open|done>
Need: a `domain.HoldBand` type with fields … / a `signals.Score` signature that takes … / the L6 handler for X to call …
Why: …
Proposed: <the exact Go you'd write>
Until then: <the stub you are coding against, in your own package>
```

L0 answers domain/config/db/testkit requests within 15 minutes by committing to `go/main` and
marking `done`. Requests to another lane are answered by that lane at its next status pass.
**Never block on a request** — code against your proposed shape in your own package and swap
when it lands.

## Commits

One commit per green step: `G<lane>.<k>: green — <one sentence in the repo's voice>`
(e.g. `G1.4: green — StopFromVol matches the desk on all 50 fixture stops`). Goldens get their
own commits: `G1.golden: score.score_frame, 12 cases from packages/core/tests/fixtures`.
Never commit red. Never commit `.env*`, `data/`, tokens, or anything under `_to_delete/`.

## Status cadence

Every 30 minutes, update your row in `STATUS.md` (one line: wave, must ✅/🟡/❌ counts,
current file, blockers) and your `status/L<n>.md` (the inventory table with ✅/🟡/❌ per file).
Loud about what is not done — that culture continues here.

## When you're ahead

Finish *should*, then *could*, then: port more test intent into goldens for your own scope;
then property tests; then help a folded lane **by writing goldens for it** (goldens need only
Python and are never a merge conflict). Do not start another lane's Go.

## When you're stuck

Ten minutes on one wall, then: record the decision you'd recommend in `DECISIONS-GO.md`
(`⚠ UNREVIEWED`), take it, continue. Parity delta you cannot explain after exhausting the
Python source: mark the golden `"disputed": "<why>"`, keep the test (skipped with reason), move on.
Something only Maulik can supply: `NEEDS-MAULIK.md`, keep working on what does not need it.

## Resuming (later tonight or next evening)

Paste your prompt again. It says to read `STATUS.md` and `status/L<n>.md` first and continue
from the first ❌/🟡; the goldens and the inventory make that lossless.
