# Baskfy — repo hygiene run (RH). Paste this whole file as the first message to the CLI.

You are working in `/Volumes/T7-Work/Dev/projects/baskfy` on branch `developer`. Read `CLAUDE.md`, then
`docs/LOCAL.md`, then this prompt. Nothing else before you start.

## Token discipline (Maulik, 30 Oct 2026 — CLAUDE.local.md applies)

Code, moves and commits — not prose. One-line commit messages + the Co-Authored-By line. Status is one
table in `docs/repo-hygiene/STATUS.md` (item · state · commit). Decisions below are pre-made; record a
deviation as one line in `docs/DECISIONS-MERGE.md` tagged `RH-n ⚠ UNREVIEWED`. No narrated progress,
no closing summary. **Done means:** every acceptance check in §6 green, STATUS.md all `done`, tree
pushed to `origin/developer`.

## What this run is

Turn the working tree into an industrial-grade monorepo: one obvious root, one command entry point, no
build output or stale artefacts inside the repo, every secret outside it, every merged branch and
worktree gone, and a CI that proves the binaries we actually deploy. **No behaviour change to any
binary.** If a step would change what `baskfy-api`, `baskfy-desk` or `baskfy-worker` do at runtime,
stop that step and record it — it belongs to a different run.

## Hard rules (in addition to CLAUDE.md's safety rails — those win)

- Never place an order, call `/execute`, or run anything against live Kite credentials. Every
  `cargo test`/`pytest` runs with `DRY_RUN=true` (the harnesses already pin it).
- Do not touch: `frozen/strangle/`, `kite-momentum-rebalancer/data/portfolio.db`,
  `kite-momentum-rebalancer/data/uploads/*.csv`, `/Volumes/T7-Work/baskfy-local/` (except where §3.3
  says to *add* a file), `/Volumes/T7-Work/baskfy-rs-testdb/`, the running `baskfy-local` Docker stack,
  ports 8480/5432.
- Moves are `git mv`. Deletions of **tracked** files are `git rm` in their own commit with the file list
  in the message. Deletions of **untracked** build output (`target/`, `node_modules/`, `__pycache__`,
  `.next`, `graphify-out/cache`) are plain `rm -rf` and need no record.
- Never rewrite history, never force-push, never `git gc --prune` or `git reflog expire`. D2 says the
  history is an asset.
- A worktree with dirty files is **not** deleted until §2.2 has saved them.
- Do not change any value in `.env*`, compose files or `Caddyfile` except the three path strings §3
  names. Do not "fix" a setting that disagrees with a doc (CLAUDE.md, "the DECISION wins").
- One commit per numbered item below; message `RH<n>: <one line>`.

## 0. Preflight (no commits)

```bash
git status --short                      # expect ~14 dirty files, 9 in rust/crates (order path) — see §1.1
git worktree list                       # expect 12 prunable worktrees
git branch --merged developer | wc -l   # expect 49
cat /Volumes/T7-Work/baskfy-local/rs.tag /Volumes/T7-Work/baskfy-local/pipeline.tag
```
Write these four outputs as the first rows of `docs/repo-hygiene/STATUS.md`.

## 1. The working tree

1.1 **Dirty files.** Run `git diff --stat`. The 9 modified files under `rust/crates/*` (api `indices.rs`,
`routes/meta.rs`; core `lib.rs` + untracked `special_session.rs`; desk `swing/context.rs`,
`opps_analyze.rs`, `opps_ticket.rs`, `swing_exec/execute.rs`) are **order-path or core code from an
interrupted Cursor session**. Do not commit them to `developer` blind: `git stash push -u -m "RH: cursor
WIP 7 Nov" -- rust/` then `git stash branch wip/cursor-2026-11-07` later, or simply
`git checkout -b wip/cursor-2026-11-07 && git add -A rust/ && git commit -m "WIP: cursor session 7 Nov,
unverified"` and return to `developer`. Either way `developer` ends clean and the work is on a named
branch. `CLAUDE.md` / `kite-momentum-rebalancer/CLAUDE.md` edits: read the diff; if it is prose only,
commit as `RH1`. `.claude/settings.local.json`, `.pnpm-store/`, `data/`: add to `.gitignore` (§3.1),
do not commit.

1.2 **Tag what is deployed.** `git tag deployed/local-$(cat /Volumes/T7-Work/baskfy-local/rs.tag)
<the sha the image was built from>` — find it with `docker image inspect baskfy-rs:<tag> --format
'{{index .Config.Labels "org.opencontainers.image.revision"}}'`; if the label is absent, tag the commit
whose short sha equals the tag, and add the OCI label to `Dockerfile.rust` (§4.2) so the next build
carries it.

## 2. Branches and worktrees

2.1 **Shared cargo target first** (otherwise the next build in any worktree recreates 100+ GB):
`rust/.cargo/config.toml` gains
```toml
[build]
target-dir = "/Volumes/T7-Work/baskfy-cargo-target"
```
(the folder exists). Then `cargo build -p baskfy-core` once from `rust/` to prove it writes there.
Commit `RH2`.

2.2 **Save dirty worktree work.** For each of `../baskfy-rs8-opps` (25 dirty), `../baskfy-sc-run` (18),
`../baskfy-rs8-opps-setups` (3), `../baskfy-rs8-opps-web` (3): `cd` in, `git add -A && git commit -m
"WIP: saved by RH, unverified"` on its own branch. `codex/sc-run` (3 commits ahead: SC0–SC2 curated
catalog) and `claude/jolly-brown-6aeyjb` (2 ahead, re-applied by content as `c438ff1`/`cb48771`): leave
the branches, do **not** merge, list them in STATUS as "Maulik decides".

2.3 **Remove every worktree** (all 12 are `prunable`): `git worktree remove --force ../baskfy-rs8-*
../baskfy-sc-run`, then `git worktree prune`. Before each remove: `rm -rf <dir>/rust/target
<dir>/node_modules <dir>/decile-blueprint/node_modules`. Record freed GB per folder in STATUS
(`du -sh` before). No commit (nothing tracked changes).

2.4 **Delete merged branches.** `git branch --merged developer | grep -vE '^\*|developer' | xargs git
branch -d` (49 branches). Remote: delete `origin/af/*` (merged) with `git push origin --delete`.
Keep: `developer`, `codex/sc-run`, `claude/jolly-brown-6aeyjb`, `af/{E,F,G}-rebased` (unmerged — list
for Maulik), `wip/cursor-2026-11-07`. Branch policy goes into `CONTRIBUTING.md` (§5).

2.5 **Clean the main target.** `rm -rf rust/target` (212 GB; builds now go to the shared dir).

## 3. Layout

Target tree (names already used in the repo; nothing renamed that an import or a running command
references, except the three paths called out):

```
baskfy/
  README.md  CONTRIBUTING.md  CLAUDE.md  AGENTS.md  CLAUDE.local.md  Makefile  .gitignore  .editorconfig
  .github/workflows/ci.yml
  rust/            (unchanged; + .cargo/config.toml target-dir)
  rust/testdata/golden/      ← from go/testdata/golden   [PATH CHANGE 1]
  decile-blueprint/          (unchanged this run — Python oracle, web, infra)
  kite-momentum-rebalancer/  (unchanged — Python desk oracle; templates/static still copied by Dockerfile.rust)
  deploy/                    ← from decile-blueprint/infra/docker/{Dockerfile.*,compose.*.yml,Caddyfile,initdb/}  [PATH CHANGE 2]
  tools/deploy/              local.sh, verify-*.sh (keep); ship.sh, exit-aws.sh, tf.sh, push-images.sh,
                             preflight-aws.sh, install-*.sh, box*.sh, ses-credentials.sh → tools/deploy/retired/
  tools/parity/  tools/check-namespace.sh  tools/ci-local.sh  tools/coverage-combined.sh (keep)
  tools/{fno,options,swing,twt,vbt,portfolio,gates,tree3,tree3b,migrate-desk,rewrite,marketing,friday-drill.*,reclaim-ram.sh,verify-nse.py}
                             → tools/runs/<name>/  (one-off run packs; keep, out of the way)
  docs/                      00–08, DECISIONS-MERGE.md, LOCAL.md, README.md, rust-rewrite/, swing/, twt/, vbt/, fno/, options/, live/, overlap/, condor/ (unchanged)
  docs/history/              ← the 30 root *.md reports/plans/prompts (list in §3.2)
  docs/gates/                ← gates/ (284 briefs)
  docs/audit/                ← AUDIT-2026-09-12.md, docs/audit-fix/
  research/                  (keep; 150 tracked files)
  frozen/                    (untouched)
  ops/                       keep aws-backups/, kite-token-state.py, desk-task-probe.py; move
                             baskfy-staging-gate-password.txt to /Volumes/T7-Work/baskfy-local/  [secret out]
```
Not in the tree after this run: `go/` (only goldens, moved), `graphify-out/` (untracked, delete),
`_to_delete/` (untracked, delete), `holdings-status/` (1 tracked file → `research/holdings-status/`),
`pitch-assets/` → `docs/history/pitch-assets/`, `logo.svg` → `decile-blueprint/apps/web/brand-src/`,
`skills-lock.json`, `smallcase-product-documentation.md` → `docs/history/`, `Claude outputs/`
(untracked, leave, add to `.gitignore`), `data/` (untracked, gitignore).

3.1 **`.gitignore` (root)** — add: `/data/`, `/Claude outputs/`, `.claude/settings.local.json`,
`.pnpm-store/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`, `**/__pycache__/`, `**/.next/`,
`**/node_modules/`, `rust/target/`, `*.tgz`, `*.lock-*`, `coverage.json`. Keep everything already there.
Add `.editorconfig` (utf-8, lf, final newline, 4-space Rust/Python, 2-space TS/YAML). Commit `RH3`.

3.2 **Root docs → `docs/history/`** via `git mv`: AUDIT-2026-09-12.md (→ docs/audit/),
CHRONICLE-PITCH-PROMPT, D3-UNLOCK-REPORT, DESIGN, FINAL-REPORT, FLOW-REFINE-PROMPT, FO-FINAL-REPORT,
GATES, MERGE-PROMPTS, OP-FINAL-REPORT, PITCH-SOURCE, PLAN-DESK-RETIREMENT, PLAN-DISCOVER,
PLAN-SCAN-SYNC, PLAN-TREE4-INTEGRITY, PLAN, PORTFOLIO_REDESIGN, REMAINING, RUN-AND-TEST, SC-AC-REPORT,
SC-FINAL-REPORT, SW-FINAL-REPORT, TREE-PORTFOLIO-PLAN, TREE4-BACKLOG-REPORT, TREE6-NAV-PERF-REPORT,
TREE7-PLAN, TW-FINAL-REPORT, VB-FINAL-REPORT, baskfynavrefactorreport.md, smallcase-product-documentation.md.
**Keep at root:** CLAUDE.md, AGENTS.md, CLAUDE.local.md, NEEDS-MAULIK.md (CLAUDE.md names its path).
Then `grep -rn` every moved filename across the repo (`CLAUDE.md`, `AGENTS.md`, `docs/`, `tools/`,
`.github/`) and fix the references in the same commit. `docs/history/README.md`: one table, file · date
· one line. Commit `RH4`.

3.3 **Secret out of tree.** `mv ops/baskfy-staging-gate-password.txt /Volumes/T7-Work/baskfy-local/`
(it is untracked and ignored, so no git change); `grep -rn gate-password tools docs` and point any
reader at the new path. If `git log --all -- ops/baskfy-staging-gate-password.txt` shows it was ever
committed, write that in NEEDS-MAULIK.md (rotation is his hand) and stop nothing else.

3.4 **Goldens: `go/testdata/golden` → `rust/testdata/golden`** [PATH CHANGE 1]. 2,419 tracked files,
46 references. `git mv go/testdata rust/testdata`, then update: `rust/crates/baskfy-testkit/src/golden.rs:20`
(`../../../go/testdata/golden` → `../../testdata/golden`), every `.rs`/`.py`/`.sh`/`.md` hit of
`grep -rln "go/testdata"`, and `tools/parity/golden*.py` output paths. Delete the empty `go/`.
Acceptance: `cargo test --workspace` reports the same 1,702 graded goldens as before (count in
`docs/rust-rewrite/STATUS.md`) and `uv run pytest -q` on the five `test_*_goldens.py` files passes.
Commit `RH5`.

3.5 **Deploy files → `deploy/`** [PATH CHANGE 2]. `git mv decile-blueprint/infra/docker deploy`.
Update: `tools/deploy/local.sh` (`DOCK=`), `tools/deploy/smoke-local.sh`, `tools/deploy/compose.rust.yml`
comment, `Dockerfile.rust`/`Dockerfile.web`/`Dockerfile.python` build-context comments,
`.github/workflows/ci.yml`, `docs/LOCAL.md`, `docs/08-aws-architecture.md`, `RUN-AND-TEST.md` (now in
history). `Dockerfile.rust` and `Dockerfile.web` keep the repo root as context, so `COPY` paths do not
change. **Do not** edit the compose files' contents. Acceptance: `bash tools/deploy/local.sh stage`
renders `/Volumes/T7-Work/baskfy-local/compose.prod.yml` byte-identical to before (diff against a copy
taken in §0). Commit `RH6`.

3.6 **Gates, retired scripts, run packs, strays** (`git mv`): `gates/` → `docs/gates/`; AWS scripts
→ `tools/deploy/retired/` (each already exits 1 with a "retired" line — keep that); the per-sleeve
tool dirs → `tools/runs/`; `holdings-status/`, `pitch-assets/`, `logo.svg`, `skills-lock.json` as in
the tree above. Fix references (`grep -rn` each old path in docs/, tools/, CLAUDE.md, .github/).
`rm -rf graphify-out _to_delete` (untracked). Commit `RH7`.

## 4. Build and CI

4.1 **Root `Makefile`** — the one entry point, each target a thin call into what exists:
```
make help            # lists these
make dev             # cargo build (debug) all three bins
make test            # cargo nextest run --workspace  (install cargo-nextest if absent)
make test-py         # cd decile-blueprint && uv run pytest -p no:randomly -q ; cd kite-momentum-rebalancer && .venv/bin/python -m pytest -q
make test-web        # pnpm --filter @baskfy/web run lint test
make lint            # cargo fmt --check && cargo clippy --workspace --all-targets -- -D warnings && tools/check-namespace.sh
make image TAG=…     # docker build -f deploy/Dockerfile.rust -t baskfy-rs:$(TAG) --label org.opencontainers.image.revision=$(git rev-parse HEAD) .
make up / down / status / verify   # bash tools/deploy/local.sh <verb>
make pipeline TAG=…  # bash tools/deploy/local.sh pipeline $(TAG)
make clean           # cargo clean (shared target), rm -rf node_modules .next __pycache__
```
`make test` must pass with `DRY_RUN=true` (already forced by `rust/.cargo/config.toml`). Commit `RH8`.

4.2 **`Dockerfile.rust`:** add `LABEL org.opencontainers.image.revision=$GIT_SHA` via `ARG GIT_SHA`, a
`cargo chef` prepare/cook stage before `COPY rust/ ./` so dependency layers survive source edits, and a
`HEALTHCHECK` for the api/desk CMD (`curl -fsS localhost:8000/health`). No change to the runtime stage's
env or user. Acceptance: image builds; `docker run --rm baskfy-rs:probe baskfy-desk --help` works.
Commit `RH9`.

4.3 **CI `rust` job** in `.github/workflows/ci.yml`: `dtolnay/rust-toolchain@1.99.1`, `Swatinem/rust-cache`,
`cargo fmt --check`, `cargo clippy --workspace --all-targets -- -D warnings`, `cargo test --workspace`
(bed tests skip without `BASKFY_RS_TEST_DATABASE_URL`; say so in the job), `cargo deny check` with a
`deny.toml` that bans `openssl`, allows the current licence set, and refuses any `baskfy-core`
dependency outside its current list (Law 1 in CI). Existing Python/web jobs unchanged. Commit `RH10`.

4.4 **Pre-commit** (`.pre-commit-config.yaml`, optional for Maulik, documented in CONTRIBUTING):
`cargo fmt`, `ruff`, `gitleaks` (secrets), `check-added-large-files --maxkb=2048`, forbid `*.tgz`,
`*.lock-*`, `.env*` except the two examples. Commit `RH11`.

## 5. Root README and CONTRIBUTING

`README.md` (≤ 80 lines): what Baskfy is (two sentences), the tree above with one line per folder,
`make help` output, how to run locally (`docs/LOCAL.md` link), where secrets and state live
(`/Volumes/T7-Work/baskfy-local`, never in git), the two laws and seven non-negotiables **by reference
to CLAUDE.md**, not copied. `CONTRIBUTING.md` (≤ 60 lines): `developer` is trunk; short-lived
`<area>/<topic>` branches, squash or FF into `developer`, delete after merge; worktrees only for a
parallel run and removed the same day; shared `CARGO_TARGET_DIR`; commit style; what CI must pass;
`deployed/*` tags; never commit under `data/`, `.env*`, `ops/*password*`. Commit `RH12`.

## 6. Acceptance (all must be true before "done")

```bash
git status --short | wc -l                      # 0
git worktree list | wc -l                       # 1
git branch | wc -l                              # ≤ 7 (developer, wip/cursor-2026-11-07, codex/sc-run, claude/jolly-brown-6aeyjb, af/{E,F,G}-rebased)
ls | wc -l                                      # ≤ 20 entries at root
test ! -d rust/target && test ! -d go && test ! -d gates && test ! -d graphify-out && test ! -d _to_delete
du -sh /Volumes/T7-Work/baskfy-cargo-target     # exists, growing; df -h /Volumes/T7-Work shows ≥ 300 GB freed vs §0
make lint && make test                          # green; golden count unchanged (1,702)
make test-py                                    # green (data-gated skips allowed, same count as before)
bash tools/deploy/local.sh stage && diff /Volumes/T7-Work/baskfy-local/compose.prod.yml /tmp/compose.prod.yml.before   # empty
bash tools/deploy/local.sh verify               # every money flag false, unchanged DRY_RUN report
grep -rn "go/testdata\|infra/docker" --include=*.rs --include=*.py --include=*.sh --include=*.yml --include=*.toml . | grep -v docs/history | wc -l   # 0
git grep -i -n "password\|secret_key\|fernet" -- ':!*.example' ':!docs/history' ':!NEEDS-MAULIK.md' ':!*.rs' ':!*.py' ':!*.ts' | grep -vi "BASKFY_[A-Z_]*=\${" | wc -l   # 0 literal values
git push origin developer wip/cursor-2026-11-07 --tags
```
Do **not** run `local.sh up`, `down`, `live`, `dry`, `pipeline` or `activate`; the running stack is not
part of this run. If `local.sh stage` differs, revert `RH6` and record it.

## 7. Stop

Update `docs/repo-hygiene/STATUS.md` (item · state · commit · GB freed), list for Maulik in
`NEEDS-MAULIK.md`: the three unmerged branch decisions, whether the gate password was ever committed,
and the shas of `deployed/*` tags. Then stop — no closing summary.
