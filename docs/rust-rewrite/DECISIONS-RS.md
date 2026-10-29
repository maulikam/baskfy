# DECISIONS-RS — judgement calls in the Rust rewrite

Same convention as `docs/DECISIONS-MERGE.md` and `docs/go-rewrite/DECISIONS-GO.md`: context,
choice, rejected alternatives, how to reverse. Entries an agent decided under the autonomy
charter are tagged **⚠ UNREVIEWED** until Maulik reviews them.

## R0.1 — Rust, not Go: the whole backend, strangler-style (29 Oct 2026, Maulik — his decision)

**Context.** `docs/go-rewrite/DECISIONS-GO.md` G0.1 (30 Aug 2026) chose Go for the backend
rewrite and rejected Rust for compile times and borrow-checker churn in an agent loop. The Go
sprint produced the language-neutral goldens (`go/testdata/golden`, 126 files) and the plan,
but no Go code is in the tree.

**Choice.** Maulik, in session, 29 Oct 2026: *"also backend needs to rewrite complelty in
RUST"* — and, asked to scope it: **the whole backend**, **strangler-style** (Python stays live
until each Rust module reproduces it exactly), run as **multi-agent workflows**. His goals:
**lower memory, speed, one binary / simpler ops, type safety.** This reverses G0.1.

**Rejected.** Go as planned in G0.1 — faster agent iteration and seconds-long builds, but it
answers none of the four goals better than Rust, and polars-rs lets `baskfy_core`'s polars code
port nearly 1:1, which Go (no polars) could not.

**How to reverse.** Nothing Python is retired by this entry; each module moves only after its
goldens pass, so stopping is free at any point. The goldens are language-neutral, so a return to
Go reuses them unchanged: delete `rust/`, restore G0.1 as current.

## R0.2 — Workspace shape, crates and pinned dependencies (29 Oct 2026) ⚠ UNREVIEWED

**Context.** Porters start in parallel the moment the foundation lands; they need a fixed
layout, fixed dependency versions and one way to grade against the goldens.

**Choice.**

* **Cargo workspace at `rust/`**, edition 2024, resolver 3, `rust-toolchain.toml` pinned to
  1.99.1 (the installed toolchain). Crates: `baskfy-core` (lib, pure) and `baskfy-testkit`
  (dev-dependency only). Later crates (`baskfy-providers`, `-execution`, `-api`, `-worker`,
  `-desk`, then the single `baskfy` binary) are added by the step that needs them.
* **Every third-party version pinned exactly** (`=x.y.z`) in `[workspace.dependencies]`:
  polars 0.55.2, rust_decimal 1.43.0, chrono 0.4.45, serde 1.0.229, serde_json 1.0.154,
  thiserror 2.0.21. A golden must never fail because a dependency moved.
* **polars** with `default-features = false` — no csv/parquet/IO — plus only computation
  features (`lazy`, `dtype-date`, `temporal`, rolling/ewma/cum_agg/rank/diff/pct_change/
  round/abs/is_in/is_between/log/strings, `fmt_no_tty`). Why polars at all: `baskfy_core` is
  written in polars expressions (`.over("instrument_id")` windows); the same engine reproduces
  its float behaviour far more faithfully than hand-rolled loops. A porter may add a computation
  feature; an IO feature fails `law_one.rs`.
* **rust_decimal** with `serde-str`, so a `Decimal` serialises as `"12.34"` — exactly
  golden.py's encoding — with no per-field attribute, and scale is preserved like Python's
  `Decimal`. Chosen over `bigdecimal` (heap-allocated, slower) because 28 significant digits
  match Python's default decimal context.
* **chrono** with `default-features = false, features = ["std", "serde"]` — **no `clock`**, so
  `Utc::now()`/`Local::now()` do not exist in the build: law 1 enforced by the compiler, not
  only by the scan.
* **thiserror** for `CoreError`; **serde** for every input/output type.
* **Workspace lints:** `unsafe_code = forbid`, `missing_docs = warn` (fails under `-D
  warnings`), clippy `unwrap_used`/`expect_used`/`panic`/`todo`/`unimplemented`/`dbg_macro`/
  `print_stdout`/`print_stderr`/`float_cmp` = deny. Test crates opt out with an explicit
  `#![allow(...)]` at the top.
* **`baskfy-core/tests/law_one.rs`** scans `src/` (line comments excluded) for IO, clock, env
  and `unsafe` tokens, checks `[dependencies]` against an allow-list of the five computation
  crates, and checks the workspace pins keep polars IO and chrono's clock out. `include_str!`
  is **allowed**: it embeds a reference table at compile time (e.g. `data/nse_trading_holidays.csv`)
  and does no runtime IO.
* **Testkit grading is strict:** strings byte-exact (so Decimal scale must match Python's),
  int vs float is a mismatch, floats by `tol · max(1, |expected|)` from `meta.tolerance.float`,
  unknown tolerance keys are an error rather than ignored, `known_bug` never relaxes grading.
  The golden root is resolved from the testkit's manifest dir, never the cwd.
* **Modules declared up front** so porters never edit `lib.rs`: `swing::{market,
  opening_range, setups, sizing, stops, plan}` and `options::{condor, costs, execution, gating,
  ledger}` as asked, **plus** `swing::{config, indicators}` and `options::config`, because every
  swing/options golden takes a `config` input and `detect_setups` runs over
  `with_swing_indicators` — without them two porters would race to create the same file.

**Rejected.** A single crate (the testkit would ship in the core); `ndarray`/hand loops instead
of polars (diverges from Python's float paths); `f64` money (house rule 9); caret version
ranges (silent drift between porter runs); value-equality for Decimal strings (would hide a
missing `quantize`, and the API serialises the string).

**How to reverse.** Each choice is one line in `rust/Cargo.toml` or one constant in
`law_one.rs` / the testkit; changing one is a new R-entry. A dependency bump re-runs every
golden.

## R1.1 — A stale golden is graded and left red, not edited (RS1, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** `L1/swing/build_entries.case_010` was dumped on 3 Sep, before LV10 (669cf4b,
Maulik's pyramiding decision, 28 Sep). The live Python and the Rust port both give
`BUY HELD 1250`; the golden says `ALREADY_HELD`. The Python suite itself is red on 13 of the 16
`build_entries` goldens (`position_id: null` was added to every line at LV10).

**Choice.** The swing-b porter followed the decision, not the stale file (root CLAUDE.md: the
decision wins over the document): case_010 runs in its own test, which fails by design, and the
other 15 grade green. `PlanLine.position_id` keeps `skip_serializing_if = Option::is_none` so the
committed pre-LV10 key set still matches. Nobody regenerated the goldens — that edits the
answer key and is a golden owner's step, not a porter's.

**Rejected.** Filtering case_010 out (hides red); porting the pre-LV10 behaviour to match the
file (reverts a decision); regenerating the goldens inside a port (porters never edit goldens).

**How to reverse / close.** `cd decile-blueprint && uv run python ../tools/parity/golden.py
swing`, commit with the reason, drop the `skip_serializing_if`, and delete the separate case_010
test so it rejoins the main grading. Add a golden that sets `open_entry_counts` (LV10's per-name
counts; today only the fuzz covers them — `golden.py`'s `_decode_build_entries` never passes it).


**Resolved 29 Oct 2026, by Maulik's go-ahead** ("no need to wait on me, you can continue … regenerate
the swing keys"): `tools/parity/golden.py swing` re-run from the live Python (byte-stable over two
runs). It first had to be fixed: `_sub_config` handed a Decimal config field back as the string it
is encoded as (`SizingConfig.min_trade_value_inr`), so the dumper crashed in `size_position`; it now
decodes by the field's declared type. 49 swing goldens changed (new config fields echoed into the
inputs; `build_entries` lines carry `"position_id": null`; case_010 is LV10's `BUY HELD`). Python's
`test_swing_goldens.py` went from 13 red to green; Rust's `PlanLine.position_id` is always encoded
and the stale-case split in `golden_swing_b.rs` is gone — 126 of 126 goldens green.
## R1.2 — Python's signed zero is reproduced (RS1, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** Differential fuzzing found that Python's `Decimal` keeps a negative zero
(`Decimal('-0.001').quantize(Decimal('0.01'))` is `-0.00`; a zero product carries the XOR of the
signs) and `rust_decimal` drops it, so a refusal or a journal figure printed `0.00` where Python
prints `-0.00`. Strings are graded byte-exact.

**Choice.** Reproduce it: `swing::sizing::quantize_cents` restores the operand's sign (and
`swing::plan` now reuses it instead of its own copy — the integrator removed the duplicate);
`options::costs` `mul`/`add`/`sub` apply Python's zero-sign rules; `options::ledger` parses
`"-0.00"` with a sign-keeping deserializer. Non-finite or out-of-range float configs that Python
tolerates (`inf` caps, `±inf` buffers) are tolerated the same way; only NaN errors, as in Python.

**Rejected.** Normalising `-0.00` to `0.00` on both sides — would need a golden edit and a Python
change for a cosmetic difference the API already serialises.

**How to reverse.** Remove the sign restoration; the fuzz will then show the mismatches again.

## R1.3 — Decimals beyond `rust_decimal`'s scale 28 (RS1, 29 Oct 2026) ⚠ UNREVIEWED — needs a decision

**Context.** Python's default context keeps 28 *significant* digits, so a quotient below 0.1
prints at scale 29+ (`"0.02384192989230769230769230769"`); `rust_decimal` caps scale at 28.
Seen in `options::execution::simulate_fill` (fuzz only — the port rounds half-even to 28 places,
value within 1e-28, string one digit short) and in the committed golden
`L1/backtest/year_fraction.case_004` (`"-0.07939767282683093771389459274"`), which made the
harness proof fail when the backtest goldens arrived.

**Choice (harness only).** The testkit's structural decoder now classifies such a string as
`Decoded::WideDecimal(text)` instead of failing, and `every_golden.rs` pins the exact list of
them (today one: `year_fraction.case_004` at `$`), so a new one fails the proof and is looked at.
Grading is unchanged: byte-exact strings, so the port of `year_fraction` will be red on that
case until the decision below is taken.

**Open, for Maulik.** Either (a) Python quantizes these values (`avg_price`, `year_fraction`) to
a fixed scale — changes the answer key and the API's bytes; or (b) the Rust side uses a wider
decimal for these few functions (e.g. a 128-bit-mantissa or `bigdecimal` type) and prints with
Python's 28-significant-digit rule. Recommended: (b) for the two functions, scoped, because it
leaves Python and every published number unchanged.

**How to reverse.** Remove the `WideDecimal` variant and the pinned list; the proof fails again
on `year_fraction.case_004`.

## R1.4 — Regime columns' look-ahead is recorded, not fixed (RS1, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** The ranking dumper found `compute_factors` does not bound `regime`,
`regime_distance_bull` and `regime_distance_bear` by `as_of`: `classify_regimes` labels only the
frame's latest date, so bars past `as_of` give NULLs on the `as_of` row (house rule 5).

**Choice.** Recorded as `meta.known_bug` on `factors.compute_factors.case_005` and
`factors.compute_factors_unrounded.case_003`; the goldens hold what Python does. Callers pass
bars up to `as_of` only. The wave-2 porter reproduces the Python behaviour (grading never
relaxes for `known_bug`); a fix is a Python change first, then a golden regeneration.

**How to reverse.** Fix `instrument_regime` in Python to cut at `as_of`, regenerate, port.

## R1.5 — The harness proof's frame rule matches the decoder's (RS1, 29 Oct 2026) ⚠ UNREVIEWED

`every_golden.rs` round-tripped any 3-key object with `columns` and `rows` as a polars frame;
the ranking goldens' pandas input shape `{columns, date_columns, rows}` matched and failed.
The test now uses the decoder's own rule (`columns` + `dtypes` + `rows`, exactly). Pandas shapes
decode as maps until wave 2 adds their decoder (STATUS, "Testkit gaps").
