# DECISIONS-GO — judgement calls in the Go rewrite

Numbered `G<lane>.<n>`. Every entry: context · choice · rejected alternatives and why · how to
reverse. `⚠ UNREVIEWED` until Maulik strikes the tag.

## G0.1 — Language: Go, not Rust, not hybrid (30 Aug 2026, Maulik — reviewed)

Context: rewrite of ~100k lines of Python backend by eight parallel agents in one evening.
Choice: Go 1.23, single module. Rejected: Rust (polars-rs would port `packages/core` almost
1:1, but minute-long compiles and borrow-checker churn roughly double agent wall-clock);
hybrid Go + Rust core (best long-term shape, worst same-day cost: two toolchains and an FFI
seam nobody owns tonight). Reverse: `internal/core` is I/O-free by law, so it is the one part
that could later be re-homed in Rust behind the same typed-slice contracts.

## G0.2 — The web app is out of scope; `openapi.json` is the contract (reviewed)

Context: `apps/web` is 80k lines of TypeScript generated against `openapi.json`. Choice: the
Go API is generated from the same file and must serve it unchanged. Reverse: none needed.

## G0.3 — No schema changes; Alembic remains the migration tool (reviewed)

Exception recorded in advance: River's job tables in a separate `river` schema (G8.1, to be
written by L8 when created).

## G0.4 — The Python desk remains the only order path until two clean shadow Fridays (reviewed)

Non-negotiable #1 in rewrite form. No agent may schedule, flag, or wire a live Go order path.
