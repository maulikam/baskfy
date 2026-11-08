# Baskfy

Baskfy is a momentum screener and data plant joined to a live execution desk for NSE cash equities
through Zerodha Kite. It runs as a Rust stack in Docker Compose on one machine.

## Layout

| Path | What |
|---|---|
| `rust/` | The three production binaries: `baskfy-api`, `baskfy-desk`, `baskfy-worker` |
| `rust/testdata/golden/` | Golden cases the Rust port is graded against |
| `decile-blueprint/` | Next.js web app and the Python oracle |
| `rust/migrations/` | Schema migrations, applied by `baskfy-worker migrate` |
| `kite-momentum-rebalancer/` | Python desk oracle; its templates and static files ship in the image |
| `deploy/` | Dockerfiles, compose files, Caddyfile |
| `tools/deploy/` | `local.sh` and verification scripts; AWS-era scripts are in `retired/` |
| `tools/parity/` | Rust-against-Python diff harnesses |
| `tools/runs/` | One-off run packs |
| `docs/` | Design record and decisions; `history/`, `gates/` and `audit/` are the archive |
| `research/` | Research notebooks and data notes |
| `frozen/` | Frozen options lab, out of every gate |
| `ops/` | Operational probes and backups |

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
