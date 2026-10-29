# Baskfy → Rust: the rewrite plan

**What this is.** The plan and working rules for rewriting Baskfy's Python backend in **Rust**,
decided by Maulik in session on **29 Oct 2026** (DECISIONS-RS **R0.1**, which reverses the Go
choice in `docs/go-rewrite/DECISIONS-GO.md` G0.1). It runs as multi-agent workflows: one
foundation agent (this commit's `rust/` workspace and testkit), then porters in parallel, then
an integrator who commits.

**Read order for every Rust agent:** root `CLAUDE.md` (the two laws, the seven non-negotiables,
the nine house rules and the safety rails apply verbatim) → this file → `DECISIONS-RS.md` →
`STATUS.md` → the Python module you are porting, in full, and its tests.

## Goals (Maulik's, 29 Oct 2026)

Lower memory, speed, **one binary / simpler ops**, type safety. Every structural choice below
is measured against those four.

## Method: strangler, never big-bang

Python stays live — serving, scheduling, and (for the desk) placing orders — until each Rust
module **reproduces it exactly**, graded by goldens. Nothing turns a Python path off until its
Rust replacement has passed its gate. Order:

| Step | Rust crate (planned) | Python it replaces | Gate |
|---|---|---|---|
| 1 | `baskfy-core` (now) | `decile-blueprint/packages/core` (`baskfy_core`) | every golden for the module green; law 1 test green |
| 2 | `baskfy-providers` | `packages/providers` (Kite, NSE, bhavcopy) | recorded-fixture goldens; same rate limits (Kite ~3 req/s historical; NSE cookie/header discipline) |
| 3 | `baskfy-execution` | `packages/execution` (guards → risk → rate limit → journal → broker) | law 2 tests; the seven non-negotiables as tests; `DRY_RUN=true` end to end; **no live order, ever, from an agent** |
| 4 | `baskfy-api` (axum) | `services/api` | serves `openapi.json` unchanged; JSON bodies equal after normalisation (docs/go-rewrite/03 §Tolerances) |
| 5 | `baskfy-worker` | `services/worker` (nightly, catch-up, publish) | `pipeline_run_step` rows equal to the Python run's against a **copy** of the DB |
| 6 | `baskfy-desk` | `kite-momentum-rebalancer/app` | two shadow Fridays in DRY_RUN with byte-equal plans; cutover only on Maulik's explicit go |
| end | `baskfy` (one binary) | all of the above | subcommands `api`, `worker`, `desk`; one process image, one deploy |

Unchanged by the rewrite: `apps/web` (held to `openapi.json`), the Postgres schema (the second
contract; Alembic stays the migration tool until a decision says otherwise), `frozen/strangle/`.
The single address (`/desk` behind Caddy on the app's own port, commit `5412960`) is how the one
binary will be reached; a Rust service cuts over by moving one Caddy route at a time.

## Grading: the goldens are the answer key

`go/testdata/golden/**.json` — language-neutral, written by `tools/parity/golden.py` from the
live Python (the directory name is historical; it is shared by every port). Format:
`{"fn", "inputs", "output", "meta": {"known_bug", "tolerance", "notes", "stable"}}`.

* **Never edit a golden to make a port pass. Never loosen a tolerance.** A golden that looks
  wrong is either a Python bug (port it as-is, see `known_bug`) or a question for a
  `DECISIONS-RS.md` entry — not an edit.
* Strings (every Decimal, date, enum value) compare **byte-exact**: `"5.10"` ≠ `"5.1"`. Match
  Python's `quantize` with `Decimal::rescale` / `round_dp_with_strategy`.
* Integers exact; an int where the golden has a float (or the reverse) is a mismatch.
* Floats: `|actual − expected| ≤ tol · max(1, |expected|)`, `tol = meta.tolerance.float`
  (1e-9 in every golden today).
* `null` matches only `null` (NaN and inf are encoded `null`, so NaN matches NaN).
* `known_bug` does not relax grading: the port reproduces the bug with a `// KNOWN BUG` comment
  and a `DECISIONS-RS.md` entry saying how to fix it later.
* Python modules with no goldens yet: the porter writes the dumper first (`tools/parity/`, the
  tree's own venv, imports Python, never modifies it, no network, no live DB) and commits the
  goldens separately with a message saying why.

### The testkit (porters read this, not the code)

```rust
// rust/crates/baskfy-core/tests/swing_stops.rs — illustrative: `manage` takes whatever signature the port gives it
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
use baskfy_core::swing::stops::{manage, DailyBar, OpenPosition};
use baskfy_core::swing::config::StopConfig;

#[test]
fn manage_matches_every_golden() {
    for g in baskfy_testkit::cases_for("baskfy_core.swing.stops.manage").unwrap() {
        let position: OpenPosition = g.input_as("position").unwrap();
        let bar: DailyBar = g.input_as("bar").unwrap();
        let config: StopConfig = g.input_as("config").unwrap();
        let actions = manage(&position, &bar, &config).unwrap();
        g.assert_output(&actions); // panics with every mismatch, case name and known_bug
    }
}
```

Frames: `g.input_frame("bars")` gives a polars `DataFrame` with the dumped dtypes (Date
included); grade a frame output with `g.assert_frame(&df)` — column order, dtype names and
values all count. Full API: `rust/crates/baskfy-testkit/src/lib.rs` crate docs.

## Building — the only invocation

```bash
cd /Volumes/T7-Work/Dev/projects/baskfy/rust
CARGO_TARGET_DIR=/Volumes/T7-Work/Dev/projects/baskfy/rust/target CARGO_BUILD_JOBS=4 nice -n 15 ~/.cargo/bin/cargo fmt --all
CARGO_TARGET_DIR=/Volumes/T7-Work/Dev/projects/baskfy/rust/target CARGO_BUILD_JOBS=4 nice -n 15 ~/.cargo/bin/cargo clippy --all-targets -- -D warnings
CARGO_TARGET_DIR=/Volumes/T7-Work/Dev/projects/baskfy/rust/target CARGO_BUILD_JOBS=4 nice -n 15 ~/.cargo/bin/cargo test
```

`cargo` is `~/.cargo/bin/cargo` (not on PATH). `rust-toolchain.toml` pins 1.99.1. Never raise
the job count: the Mac runs the live nightly pipeline in Docker until ~21:15 IST, and agents
never touch Docker or the `baskfy-local` containers. The target dir is shared, so concurrent
cargo runs wait on its lock — expected, not a hang.

## Rules that bind Rust (house rules restated)

* **Law 1** — `baskfy-core` touches nothing: no tokio/reqwest/sqlx, no `std::fs`/`std::net`/
  `std::env`, no `SystemTime`/`Instant::now`/`Utc::now`/`Local::now` (chrono is built without its
  clock). `as_of` dates and "now" are parameters. Enforced by `baskfy-core/tests/law_one.rs`.
* **Law 2** — only `baskfy-execution` will ever reach a broker; plans in the core are advice.
* **Money and prices are `rust_decimal::Decimal` wherever Python uses `Decimal`** — never
  `f64`. Floats only where Python uses floats (indicators, scores in polars Float64 columns).
* **No `unwrap()`/`expect()`/`panic!`/`todo!` in non-test code** (workspace clippy lints deny
  them); no `unsafe` (`forbid`); no silently swallowed errors — return `CoreError`.
* Every public item has a doc comment naming its Python origin
  (`/// Ports baskfy_core.swing.stops.manage.`); `missing_docs` fails clippy.
* Module layout mirrors Python one-to-one. The declared modules are in place; a porter adds a
  new submodule in the parent file (`swing.rs`, `options.rs`), never edits `lib.rs` for a
  declared area. A new crate dependency is a `DECISIONS-RS.md` entry; law 1 refuses IO ones.
* Every judgement call goes in `DECISIONS-RS.md`, tagged `⚠ UNREVIEWED`; every module's state
  goes in `STATUS.md`. The integrator commits; porters do not.
