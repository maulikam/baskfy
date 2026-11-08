# Contributing

## Branches

- `developer` is trunk and the only long-lived branch.
- Work on short-lived `<area>/<topic>` branches. Squash or fast-forward into `developer`, then delete
  the branch the same day.
- Each activated release gets a `deployed/*` tag. There are no release branches.

## Worktrees

Use a worktree only for a parallel run. Remove it and run `git worktree prune` the same day.

## Builds

`.cargo/config.toml` sends every build to `/Volumes/T7-Work/baskfy-cargo-target`, so a worktree
does not cost a fresh 100 GB target. Do not override it locally.

## Schema changes

Add the next numbered file to `db/migrations/`. Never edit an applied one. Alembic is frozen at
`0063_mtf_leverage` and only records where the baseline came from.

## Commits

One logical change per commit, in one complete sentence. Never rewrite history or force-push; the
history is an asset.

## Before you push

`make lint`, `make test`, and `make test-py` or `make test-web` if you touched those trees. CI runs the
Rust, Python, web, desk and namespace jobs. Rust bed tests are skipped unless
`BASKFY_RS_TEST_DATABASE_URL` is set.

Optional local hooks: `pre-commit install`, using `.pre-commit-config.yaml`.

## Never commit

- Anything under `data/`.
- `.env*`, except the example files.
- `ops/*password*`, or any other secret.
- Build output, archives or stale lock files.

## Safety

Every test harness pins `DRY_RUN=true`. An agent never places a live order. The full rails are in
[CLAUDE.md](CLAUDE.md).
