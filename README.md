# Baskfy

Baskfy is a momentum screener and data plant joined to a live execution desk for NSE cash equities
through Zerodha Kite. It runs as a Rust stack in Docker Compose on one machine.

## Layout

| Path | What |
|---|---|
| `apps/` | Deployables: `api`, `desk`, `worker` (Rust) and `web` (Next.js) |
| `crates/` | Shared Rust libraries: `core`, `db`, `providers`, `testkit` |
| `packages/` | Shared TypeScript: `api-client` |
| `testdata/golden/` | Golden cases the Rust port is graded against |
| `db/migrations/` | Schema migrations, applied by `baskfy-worker migrate` |
| `legacy/python/kite-momentum-rebalancer/` | Python desk oracle; its templates and static files ship in the image |
| `deploy/` | Dockerfiles, compose files, Caddyfile, `monitoring/`, `backup/` |
| `tools/deploy/` | `local.sh` and verification scripts; AWS-era scripts are in `retired/` |
| `legacy/python/tools/parity/` | Rust-against-Python diff harnesses |
| `legacy/python/tools/runs/` | One-off run packs |
| `docs/` | Design record and decisions; `history/`, `gates/` and `audit/` are the archive |
| `frozen/` | Frozen options lab, out of every gate |
| `legacy/python/` | All Python: both oracles, parity harnesses, run packs, `research/`, `ops/` |

## Commands

```
help                    List these targets
dev                     Debug build of the three binaries
test                    Rust tests, always dry
test-py                 Python oracle suites
test-web                Web lint and unit tests
lint                    fmt, clippy, namespace
image                   docker build baskfy-rs:TAG
up down status verify   The local stack, via tools/deploy/local.sh
pipeline                Redeploy worker, ingest-worker, beat at TAG
clean                   Remove build output
```

## Running it

See [docs/LOCAL.md](docs/LOCAL.md).

## Secrets and state

Env files, image tags, tokens and release state live in `/Volumes/T7-Work/baskfy-local`, outside git.
Data lives in Docker volumes. Neither is ever committed.

## Rules

The two laws, the desk's seven non-negotiables and the screener's nine house rules are in
[CLAUDE.md](CLAUDE.md). Read them before touching the order path or the data plant.
