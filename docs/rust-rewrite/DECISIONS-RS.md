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

## R1.3 — Decimals beyond `rust_decimal`'s scale 28 (RS1, 29 Oct 2026; resolved RS2) ⚠ UNREVIEWED

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

**Resolved at RS2 (29 Oct 2026) as option (b), by the wave-2 lead ⚠ UNREVIEWED.** Python and
every published number stay unchanged; Rust reproduces the wide values exactly.
`backtest::helpers::WideDecimal` is Python's `Decimal` in its default context — a sign, a
coefficient of any length, an exponent, a signed zero — with `add`/`sub`/`mul`/`div`/`quantize`
written step for step from CPython's `_pydecimal` (28 significant digits, half-even, the
specification's ideal exponents, so `Decimal(0) / Decimal("365.25")` is `0E+2`).
`year_fraction.case_004` grades green on it, and `run_backtest`'s whole day walk computes in it
(every division rounds where Python's does); values leave as `rust_decimal::Decimal` once they
are quantized. The screener needed the same thing for pydantic inputs (`Decimal('1E+300')`,
40-digit strings) and carries its own exact `(sign, digits, exponent)` `DecimalValue` with
libmpdec's exponent limits. **Not yet moved onto it:** `options::execution::simulate_fill`
(fuzz-only gap, no golden red). Wave 3 lifts one of the two into a shared module first, since
46 twt/vbt keys print scale-29 values (`r_multiple`, `years`; pinned in `every_golden.rs`).
Rejected: (a) quantizing in Python (changes the answer key and the API's bytes); `bigdecimal`
(a new dependency, and its division does not round like Python's context).

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

## R2.1 — serde_json parses floats exactly (`float_roundtrip`) (RS2, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** Three wave-2 lanes found independently that `serde_json` without `float_roundtrip`
parses many 16–17-digit JSON floats one ulp off (a probe: 59,216 of ~200k random f64;
`123456789.12345679` → `123456789.1234568`). Every golden is therefore decoded onto slightly
perturbed inputs, and expected outputs are read back perturbed; the default 1e-9 tolerance hid
it. The factors lane showed it matters: with exact parsing, 10,904 rank_frame/explain cases and
640 factor cases grade bit-equal; without it, a zero-tolerance comparison fails on values Python
and Rust compute identically.

**Choice.** The integrator enabled `features = ["float_roundtrip"]` on the existing exact pin
(`serde_json =1.0.154`) in `rust/Cargo.toml`. No new crate, no version move; `law_one.rs` is
unaffected. All 455 goldens stay green with it (none passed by a lucky perturbation).

**Rejected.** Leaving it off (golden inputs are not what the file says); passing floats as
strings in tests (fixes the tests, not any future JSON ingress in the API).

**How to reverse.** Drop the feature; the goldens still pass at 1e-9, inexactly.

## R2.2 — Python's numeric kernels are reproduced, not approximated (RS2, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** Wave 2 is mostly floats. Agreement within 1e-9 is not enough where a float feeds
a ten-decimal rounding and a SHA-256 hash (`metrics_hash`), a rank tie, or a stored factor.

**Choice.** Each port reproduces the exact operation order of the Python library underneath:
numpy's pairwise summation for `sum/mean/std/var`; Python 3.12's compensated (Neumaier)
built-in `sum()` (`ranking_engine::py_sum`; a plain fold gave `0.6000000000000001` for
`0.1+0.2+0.3`); `np.quantile`'s linear method as NumPy 2.5.2 implements it, `(n-1)*q`;
polars' scalar division as multiplication by the reciprocal (`pos_days_N`: `10/22*100` is
`45.45454545454546`); `np.percentile`'s `_lerp`; CPython's `bisect_left` loop; numpy's size-1
broadcasting (`benchmark_returns - rf_daily`); Python's `float ** float` raising
(`TypeError`/`OverflowError`) as `CoreError::Arithmetic` instead of NaN/inf (`py_pow`); a
rounded negative mean keeping its sign (`"-0.00"`, extending R1.2). The rolling statistics call
polars' own kernels per instrument, which is what `.over` does.

**Residual, recorded.** `np.cov` sums through BLAS (Accelerate gemm) in an order that cannot be
reproduced; beta/alpha can differ by ~1 ulp (inside 1e-9; no case of 403 shows it). And
`rust_decimal` keeps 28 decimal *places* where Python keeps 28 significant digits, which can
move a `float()` conversion by an ulp (no case shows it).

**How to reverse.** Each kernel is one helper; replacing it with the idiomatic Rust call is
local, and the zero-tolerance fuzz will show the drift.

## R2.3 — Python modules with no Rust home live as private submodules (RS2, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** Porters own one file each (RS2.0) and may not edit `lib.rs`, but `factors` needs
`windows`, `circuits`, `momentum`, `precision`, `instrument_regime` and `factors_ranking`;
`factor_registry` needs `blends`, `windows`, `ranking.FactorPreference`; `screener` needs
`screen_definition`, `universes`, `models`' column types and ranking constants; the backtest
engine needs `rank_buffer.plan_rebalance`'s union of entries/holds/inside_wrh.

**Choice.** Each lives as a private submodule of the port that needs it, ported only as far as
that port reads it. `backtest_metrics` takes its own `BacktestOutcome`/`MetricsConfig`/`Trade`
subset instead of depending on the engine port, so the two lanes ran in parallel. `windows` is
therefore ported twice (factors, factor_registry).

**How to close.** When a module's own port lands (its goldens first), the private copies are
replaced by `use` of it, and the duplicates disappear. STATUS keeps these Python modules at
`not started` until then.

## R2.4 — The screener reproduces pydantic-core, speedate, SQLAlchemy and CPython reprs byte for byte (RS2, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** `validate_definition` publishes pydantic's error list (type, loc, message), and the
query builders publish SQLAlchemy's rendered text; both are graded byte-exact, and a 30,000-case
differential fuzz found 11 classes of divergence no golden caught.

**Choice.** Reproduce the libraries at the versions the Python pins: pydantic-core 2.46.4 over
speedate 0.17.0 (int/float/bool/date coercion, underscores, `'.0'` tails, timestamps,
`year 0 is out of range`), alias handling (the loc names the key actually read; both `from` and
`from_` is `extra_forbidden`), Python's `Decimal(str)` grammar (Unicode whitespace and Nd digits,
`_`, NaN payloads), CPython 3.12 / Unicode 15.0 `repr` (a generated 712-range non-printable
table), `float.__repr__` including the power-of-two boundary (checked on 428,927 floats),
SQLAlchemy's `BIGINT` for an int bind with `bit_length() >= 32`, and `str(container)` as its
repr. An opaque object's repr carries a memory address no port can reproduce; it renders as
`<type object>`.

**Consequence.** A pydantic, SQLAlchemy or Python minor bump on the Python side means
regenerating the ranking goldens and re-running the fuzz before the Rust screener is trusted.

**How to reverse.** Not sensibly — these strings are the API's bytes.

## R2.5 — Python ints wider than i64 in a screen definition (RS2, 29 Oct 2026; decided RS3) ⚠ UNREVIEWED

**Context.** pydantic accepts an arbitrarily large int for `median_volume_1y`,
`ignore_above_beta`, circuits, the top-risk count, and the bounded `positive_days`/
`away_from_high` (where Python's message is a `value_error` naming the number). Rust refuses
them with `int_parsing_size`. It is the only class left in the 30,000-case fuzz (15 cases). A
JSON number that large cannot reach Rust as an int anyway (no `arbitrary_precision`).

**Options.** (a) a `PyInt` type through `ScreenDefinition` and the SQL binds — exact, invasive;
(b) bound these fields in Python (`le=2**63-1`) so both refuse — changes the answer key and
the API's validation, Maulik's call; (c) leave the divergence documented. **Taken for now:
(c)** — no real definition carries such a value, and the screener does not strangle its
Python twin until this is settled. Recommended: (b).

**How to reverse.** Either fix closes it; the fuzz case list is in the screener lane's report.

**Decided at RS3 (29 Oct 2026), by the wave-3 integrator under the autonomy charter ⚠ UNREVIEWED:
option (c), a documented divergence.** Rust refuses an int wider than `i64` in a screen
definition with `int_parsing_size`; Python accepts it. Why (c) over the recommended (b): (b)
changes the Python API's validation and the answer key, which is Maulik's call, not an agent's;
(c) changes nothing that is live. A JSON number that large cannot reach Rust as an int at all
(`serde_json` without `arbitrary_precision`), no real definition carries one, and the refusal is
loud, never a wrong screen. **How to reverse:** add `num-bigint` parsing (a new dependency, an
R-entry) behind a `PyInt` type for these fields, or take (b) in Python and regenerate the ranking
keys. The screener still does not strangle its Python twin until Maulik has reviewed this.

## R2.6 — ranking_engine edges no frame exercises are left as they are (RS2, 29 Oct 2026) ⚠ UNREVIEWED

`_numeric` on a String column always raises (pandas' `to_numeric` would parse `'1.5'`); a
polars Datetime `last_bar_date` is refused where `pd.Timestamp(str(x))` accepts it; under
`within_sector` a non-string sector is stringified; `explain` takes a `usize` position (no
negative `iloc`); `int(inf)` saturates instead of raising `OverflowError`. The test harness, not
the port, supplies the output pandas dtype names (`rank=Int64`, `excluded_reason=object`,
bool-with-null `object`) — representation only. Two real ones were fixed: `explain` with a
missing `instrument_id` raises Python's `ValueError` (float column) or 3.12's `TypeError`
wording. **How to reverse/close:** each is one function; add a golden first, then match it.

## R2.7 — A benchmark level beyond scale 28 is refused (RS2, 29 Oct 2026) ⚠ UNREVIEWED

`BacktestResult.benchmark` (and equity, cash) is `rust_decimal::Decimal`, converted from
`Decimal(str(float))`. A level needing scale > 28 (|level| < ~1e-12 at full float digits, or
> ~7.9e28) is `BacktestError::Decimal` in Rust where Python returns a value. Stored levels are
`numeric(18,4)`, so real data cannot reach it; widening the type would change what
`backtest_metrics` reads. **How to close:** move the result's money onto the shared wide decimal
when wave 3 creates it (R1.3).

## R2.8 — Wave-3 answer keys: integrator fixes before the first commit (RS2, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** The twt, vbt and fno keys (1,247) arrived uncommitted; the harness proof
(`every_golden.rs`) failed on them three ways.

**Choice.**

* **fno: whole-number floats written as ints.** `golden_fno.py`'s `frame_doc` wrote rows by
  hand, so a whole-number spot in a `Float64` column printed `50000` where golden.py writes a
  polars float as `50000.0` — the frame did not round-trip (19 files: `underlying` in
  `research.*`, `retest.run_family`, `scan.f2_signals`, `series.continuous_futures`).
  `frame_doc` now writes a Float64 cell as a float, and the fno keys were regenerated: exactly
  those 19 files changed, the rest byte-identical, and two more runs (one under another
  `PYTHONHASHSEED`) byte-stable. Python reads both forms into the same Float64, so no output
  moved. These keys had never been committed, so this is the dumper's fix before publication,
  not an edit of an answer key.
* **vbt: `List(String)`** (`failed_filters`) — the testkit's frame codec learned it.
* **46 new wide decimals** (twt/vbt `r_multiple` and `years`, unquantised in Python) were
  reviewed and pinned in `KNOWN_WIDE_DECIMALS`; the wave-3 porters reproduce them with the
  shared wide decimal (R1.3).

**How to reverse.** Revert the `frame_doc` change and regenerate; drop the `List(String)` arm.

## R2.9 — Python behaviour the wave-3 keys lock in (RS2, 29 Oct 2026) ⚠ UNREVIEWED

Recorded, not changed (a Python fix first, then a regeneration — same rule as R1.4):
`vbt.backtest.yearly` raises `IndexError` on a result with no sessions (`summarise` returns
`None` for the same input) — `yearly.case_003`, `known_bug`; `fno.research.summarise` raises
`TypeError` on a one-trade frame (`std` of one value is null), unguarded in
`retest.run_family` — `summarise.case_005`, `run_family.case_003`, `known_bug`;
`twt.breadth.breadth_above_dma(as_of=None)` counts every row of the frame and labels the result
with the latest session (inherited from VBT's shared code); `twt.exits.tick_floor` on a negative
price rounds toward zero; vbt `detect_signals` sorts a null `rank_key` first even descending;
`vbt.drift.compare` can return `-0.0`. A port reproduces each; grading never relaxes for
`known_bug`.

## R3.1 — twt compares floats IEEE-style; NaN-free input frames are a precondition (RS3, 29 Oct 2026; closed at the wave-3 housekeeping, last site at RS4a) ⚠ UNREVIEWED

**Context.** The twt reviewer found that a literal NaN in an input bar frame is handled
differently. Python's polars compares floats in a total order (NaN largest, NaN == NaN,
`NaN > 1.0` true; `max_horizontal`/`min_horizontal` fold like x86 `maxsd`, so argument order
matters; group `min` skips NaN). The twt port uses IEEE comparisons in `cmp_f` (the tight band,
`close >= month_low*multiple`, `close_raw > min`, `vol_sma >= min`), `liquidity_floor`, breadth's
`breadth_above_dma`/`breadth_series`, `limit_locked` (`frame::same`), `month_low_back`'s min and
`tight_state`'s `f64::max/min`. Verified live: with `close = NaN`, `sma_dma = 1.0`, polars counts
the name as above the DMA and Rust does not. vbt (`vbt::calendar::frames`) and fno research
(`pl_gt`) already port the total order.

**Choice at RS3.** Not fixed (the integrator fixes only mechanical issues); recorded as a
precondition that twt's float columns are NaN-free.

**Closed (wave-3 housekeeping).** polars' float semantics now live once, in
`baskfy_core::pl` (declared in `lib.rs` beside `decimal` and `py`), used by twt, vbt and fno;
the precondition is gone.

* **Comparisons:** `pl::{tot_cmp, cmp, gt, ge, lt, le, eq}` — total order, NaN largest and equal
  to itself, `-0.0 == 0.0`, Kleene null. twt's `cmp_f` sites, `liquidity_floor` and
  `limit_locked` call them; `vbt::calendar::frames::{tot_cmp, cmp_opt}` are now re-exports of them
  (one implementation); fno research's `pl_gt` is `pl::gt`.
* **Aggregations:** `pl::{nan_min, nan_max}` — `col.min()`/`col.max()` in `group_by().agg`,
  `.over()` or `Series.min()` skip NaN (NaN only when nothing else is there), skip nulls, and order
  `-0.0` below `0.0`. twt's `month_low_back` and fno research's `continuous` spot max call them.
  Fuzzed against the live Python over 2,000 random groups: no mismatch.
* **`max_horizontal`/`min_horizontal` were not one rule.** Probing the live Python showed the
  RS3 reading was half right: polars reduces the columns **pairwise in rayon's reduction tree**
  (split at `len/2`: three columns are `a op (b op c)`) and each pair runs one of **two kernels
  chosen by the columns, not the row** — the IEEE `if l > r { l } else { r }` kernel (`maxsd`, NaN
  loses on the left and wins on the right) when neither column of the pair holds a null and the
  frame is more than one row; otherwise a total-order kernel (nulls skipped, NaN largest, ties to
  the right). So `max_horizontal(NaN, 101, 102)` is `102` in a null-free frame and `NaN` in a
  one-row frame or when a column holds a null anywhere. `pl::{max_horizontal, min_horizontal}`
  take whole columns and reproduce both kernels and the tree (the committed generator fuzzes 3,000
  frames of one to four columns and one to four rows; the adversarial review re-ran 8,000 more of
  one to six columns, up to 17 rows, `±inf` included: no mismatch); `pl::{max,min}_horizontal_total` are the
  total kernel alone, for a column against a broadcast literal (fno research's
  `max_horizontal("gc", 1.0)`) and for fno's TR, whose `shift(1)` columns always hold a null.
  twt's `tight_state` now reduces its weekly-close columns with the column form. Five or more
  columns with fewer rayon threads than splits would fold a sub-slice left to right instead; no
  caller passes more than four.
* **Breadth** is VBT's (R3.4), which already compared in the total order.
* **Test:** `tests/pl_semantics.rs` — recorded cases (always run) whose frames hold NaN, `±inf`,
  `±0.0` and null, built in-process, with every expected value printed by the live Python, through
  twt's `tight_state` (a NaN close counts as above the month low; a NaN week wins or loses by
  column position; signed zeros give a `±inf` range), `month_low_back`, `liquidity_floor`,
  `breadth_above_dma`/`breadth_series` and `limit_locked`; plus an ignored `differential_fuzz`
  over 5,765 live-Python cases written by `tests/fuzz/pl_semantics_fuzz.py` (seeded, byte-stable
  over two runs), green.

**How to reverse.** Each port calls one `pl` function; swapping it back to an IEEE comparison is
one line, and the recorded cases and the fuzz show the drift.

**Last site closed at the RS4a integration.** The wave-3 review found that
`fno::series::held_bars` computed `ca_flag` as the IEEE `gc > ca_up_ratio || gc < ca_down_ratio`.
Python's `(gc > 1.4) | (gc < 0.7)` uses polars' total order, so a NaN `gc` (a NaN `settle`, or
`0 / 0`) counts as a corporate action in Python, which drops it from the series. Rust treated it
as an ordinary session and compounded a NaN level. The RS4a integrator changed that one site to
`crate::pl::gt`/`pl::lt`. Before the change it confirmed on the live polars that
`[NaN, 1.0, 2.0, 0.5]` flags `[true, false, true, true]`. Every fno golden stays green. R3.1 now
has no open site.

## R3.2 — vbt prints a config float with Python's `repr`, not Rust's `Display` (RS3, 29 Oct 2026; closed at the wave-3 housekeeping) ⚠ UNREVIEWED

**Context.** The vbt reviewer's fuzz found `first_live_multiplier` (72 of 1,600 cases) and
`PublishedStudy::cagr` turning a whole-number float into `"1"` / `"26"` where Python's
`Decimal(str(1.0))` is `"1.0"`: both went through `swing::sizing::decimal_from_py_float`, which
formats with Rust's `{x}`. No golden covered it.

**Choice at RS3.** Both vbt sites went through `vbt::exits::wide::from_py_float_str` (Python's
`float.__repr__`); `decimal_from_py_float` kept the gap.

**Closed (wave-3 housekeeping).** Python's float printing lives once, in `baskfy_core::py`:
`py::float_repr` (`repr(float)`: shortest round-trip digits, the correctly rounded one among
equals, `.0` on an integral value, scientific outside `[1e-4, 1e16)`, `nan`/`inf`),
`py::wide_from_float_str` (`Decimal(str(x))` kept wide) and `py::decimal_from_float_str` (the same
as a `rust_decimal::Decimal`, scale kept). `swing::sizing::decimal_from_py_float` is now
`py::decimal_from_float_str` (so `1.0` is `Decimal("1.0")`, `1e-05` is `0.00001`); its error text
names the value by its Python repr (`nan`), and a positive-exponent print (`1e+16`) is held at
exponent 0 (R3.8's limit). The **seven** private `repr(float)` copies — `screener::py_float_repr`,
`backtest_metrics`, `backtest::engine`, `swing::plan`, `twt::published`, `vbt::exits::wide`,
`fno::condor::num` — are gone: each module calls `py::float_repr` (the screener keeps its public
`py_float_repr` as `json.dumps`' spelling, `NaN`/`Infinity`, over it). The copies that took Rust's
shortest digits unrefined now also pick the correctly rounded candidate, as the screener's and
twt's did. `twt::published::decimal_of_float` and `vbt::exits::wide::from_py_float_str` parse
through `py::wide_from_float_str`, keeping their own error wording. R3.6's two `format!("{x}")`
parses (`vbt::orders::fill_if_touched`'s threshold, `vbt::sizing`'s `PyDec::from_float`) go through
it too, which closes R3.6. Swing's 126 goldens, every other golden and the py unit tests (`1.0`,
`26.0`, `1e-05`, `1e+16`, `5e-324`, `f64::MAX`, NaN) are green.

**Review (wave-3 adversarial pass).** `py::float_repr` was graded against the live CPython over
200,014 floats (random bit patterns, rounded decimals, powers of two, subnormals): no mismatch;
`wide_from_float_str` matched `format(Decimal(repr(x)), "f")` on every finite one. The one
mismatch was `decimal_from_float_str(-0.0)`: `rust_decimal`'s parser drops a zero's sign, so it
gave `0.0` where Python's `Decimal('-0.0')` is `-0.0`; it now restores the sign (pinned in the py
unit test). `rust_decimal` arithmetic still normalises a negative zero away — no config carries one.

**How to reverse.** Restore the old call in the one function; the vbt fuzz shows the scale loss.

## R3.3 — Python errors map onto `CoreError`/`FnoError`; unreachable integer and index edges (RS3, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** Goldens store a raise as `{"raises": {type, message}}`. twt and vbt grade raises
through `CoreError` (the twt porter mapped `IndexError`, `DivisionByZero` and `InvalidOperation`
all onto it); fno carries `FnoError`, one variant per Python exception class it raises
(`ValueError`, `KeyError`, `TypeError`, `PooledRows`, decimal errors, `Core`), with
`type_name()` giving the Python class name.

**Choice.** Kept as the porters built them. Two twt edges are left as they are:
`SessionCalendar::advance` with a step below `-len` returns `None` where Python's tuple indexing
raises `IndexError` (inside `-len..0` Rust mirrors Python's negative indexing; no caller passes a
negative step); `size_entry` returns `CoreError::Arithmetic` for a share count above `i64::MAX`
where Python returns a big int (needs, e.g., ₹1e15 equity at a price of 1e-4). `FnoError` has no
`SchemaError` variant, so `retest::concat`'s polars schema mismatch raises where Python raises but
reports the type as `CoreError` (R3.7).

**How to close.** One shared Python-exception enum across `CoreError` and `FnoError` (a crate-wide
change, best done with the R3.4 housekeeping) would make the type names exact everywhere. *(The
housekeeping closed the twt `advance` edge — twt now uses VBT's `SessionCalendar`, which raises —
and left the exception enum for its own step: it renames error variants every lane matches on.)*

## R3.4 — Wave 3 reused `WideDecimal` in place and duplicated shared VBT/fno code (RS3, 29 Oct 2026; closed at the wave-3 housekeeping) ⚠ UNREVIEWED

**Context.** STATUS asked wave 3 to lift `WideDecimal` into a shared module first. The lanes ran
in parallel and each owned only its own files.

**Choice at RS3.** Every lane imported `crate::backtest::helpers::WideDecimal` where it was, and
twt carried private copies of VBT's config shape (`twt::config::vbt_shared`), calendar
(`twt::calendar::shared`), breadth (`twt::breadth::shared`) and densify; `fno::ledger` copied
`fno::exits`' pause predicates; `fno::costs` carried its own `LegRole` table.

**Closed (wave-3 housekeeping), only where the Python itself shares the code:**

* **`WideDecimal`** and `Rounding` moved to `baskfy_core::decimal`, with their own error,
  `DecimalError` (the message alone, as `BacktestError::Decimal`'s display was, so every
  `e.to_string()` is unchanged). `backtest::helpers` re-exports them (its public path still
  works for the backtest goldens) and converts with `From<DecimalError> for BacktestError`; every
  other lane imports `crate::decimal`. The py-float helpers went to `baskfy_core::py` (R3.2) and
  polars' float semantics to `baskfy_core::pl` (R3.1).
* **twt → vbt, as `twt/*.py` imports `vbt/*.py`:** `twt::config::vbt_shared` is deleted and
  `as_shared_config` builds `vbt::config::VbtConfig`; `twt::calendar` re-exports
  `vbt::calendar::SessionCalendar` and its four functions call `vbt::calendar`'s; `twt::breadth`
  calls `vbt::breadth` (mapping `vbt::config::Gate` onto twt's own `Gate`, which Python also keeps
  separate); `twt::indicators::densify` is a re-export of `vbt::indicators::densify`. Behaviour
  that changes with the collapse, all toward VBT's port of the same Python: `SessionCalendar::
  advance` returns `CoreResult<Option<_>>` and raises where Python's tuple indexing raises (closing
  the twt edge in R3.3; no twt caller uses it); a null `date` is refused by the calendar builders
  (R3.5) where twt's copy skipped it; a NaN breadth threshold is `InvalidInput` instead of
  `Arithmetic` (no golden can encode one); `densify` gives a null-id row no symbol (polars' join
  never matches a null key). Every twt and vbt golden is green.
* **Kept as two, because the Python has two:** twt's and vbt's `DataConfig.min_samples`,
  `_prepare`, rolling/indicator arithmetic, exits (`twt/exits.py` and `vbt/exits.py` are separate
  implementations), and the twt `frame`/vbt `frames` column plumbing (Rust-only helpers).
* **fno:** `fno::ledger` imports `fno::exits::{f1_paused, f2_month_paused, book_paused}` as
  `fno/ledger.py` does; the private copies are gone. (They had drifted: the copy read
  `pause_consecutive = 0` as an empty tail, so "paused"; `fno::exits` ports Python's
  `closed_r[-0:]`, the whole list.) `fno::costs` keeps `RolePrices` (the golden encoding, a
  role-value map) but walks `condor::LegRole::ALL` with `condor::sign_of` instead of its own
  table. `fno::ledger`'s private `max_drawdown` stays: `baskfy_core.options.journal` has no Rust
  module yet (R2.3); it moves when that module is ported.
* `screener::sha256` stays `pub(crate)` for the twt/vbt plan hashes.

1,702 of 1,702 goldens stayed green after each step; `cargo fmt`, `clippy -D warnings` green.

**How to reverse.** Each collapse is an import; restoring a copy is reverting one file, and its
goldens grade it.

## R3.5 — Polars-expression functions are graded as the expression applied to a frame (RS3, 29 Oct 2026) ⚠ UNREVIEWED

`vbt.signals.chartink_scan`/`trend_filters` and twt's `bucket_keys`/`liquidity_floor` return
polars *expressions* in Python; the Rust ports return them **evaluated over a frame** (a
`Boolean` series), which is what every caller does and what the dumpers store. A null on either
side of a comparison is a fail (Kleene logic, then `fill_null(False)`), as in Python. vbt's
calendar refuses a null `date` where Python would carry a `None` "session" into the calendar
(every caller and golden passes a polars `Date`). **How to reverse:** return lazy `Expr`s if a
caller ever needs to compose them; the goldens grade the evaluated result either way.

## R3.6 — vbt parses some config floats with Rust's `Display` where they are only compared (RS3, 29 Oct 2026; closed at the wave-3 housekeeping) ⚠ UNREVIEWED

`vbt::orders::fill_if_touched`'s threshold and `vbt::sizing::size_entry`'s `PyDec::from_float`
parse a config float via `format!("{x}")` rather than Python's `repr`. The values are compared or
floored, never printed, so they agree in value (the fuzz with 1e30 thresholds agreed). A finite
`fill_through_pct` near 1e300 could fail `WideDecimal` parsing where Python returns `None`. Left as
is; no real config is near it. **How to close:** use `from_py_float_str` there too (R3.2). **Closed:** both now parse through `py::wide_from_float_str` (R3.2).

## R3.7 — fno research reproduces polars' NaN order, null-dtype concat and schema inference (RS3, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** The research reviewer's fuzz found four classes no golden caught.

**Choice (all fixed).** (1) `futures_signals`' `gt`/`lt` now use `pl_gt`, polars' total order:
`oi5`/`oi1` are `oi_total / oi_total.shift(k) - 1`, and `oi_total` is 0 whenever OI is 0 or null,
so `0/0 = NaN` fires A1/A1R/A3 in Python — with IEEE comparisons `futures_signals` passed 18 of
40 fuzz cases, `simulate_futures` 19 and `run_family` 32; now 40 of 40 each. (2) `retest::concat`
follows strict `pl.concat`: the first part sets the dtypes, a later `Null` column is cast to them,
any other mismatch is a polars `SchemaMismatch` with polars' message (the reported type name is
`CoreError`, R3.3). (3) `frames::from_rows` infers a dtype from the first 100 rows, as
`infer_schema_length` does, and refuses a column that is all-null there and valued later
(`ComputeError`); polars' silent int→float truncation past row 100 is not reproduced (no caller
mixes int and float in one column). (4) `settle > 0` filters keep NaN settles (`pl_positive`),
`continuous`' spot max skips NaN, and `rank_average` canonicalises NaN payloads and `-0.0` before
ranking, as live polars does. **How to reverse:** each is one helper; the fuzz shows the drift.

## R3.8 — fno: two R1.3-class limits left loud (RS3, 29 Oct 2026) ⚠ UNREVIEWED

`fno::sizing::size` given an exponent-form capital (`"1E+7"`) prints `risk_budget_inr` as
`"100000.0"` where Python prints `"100000"` (and `"0.100000"` vs `"0.1"`): `rust_decimal` has no
positive exponent, so it parses as exponent 0 — 28 of 600 reviewer fuzz cases, all exponent-form;
lots, state and every comparison agree. `fno::condor::one_sd` at `iv = 1e-12` raises
`InvalidOperation` ("does not fit a 96-bit, scale-28 decimal") where Python returns a scale-29–31
value (36 cases) — loud, never a wrong value. Postgres `numeric` and Kite never produce either.
**How to close:** carry these values in `WideDecimal` (R3.4's shared module) end to end.

## R3.9 — fno research: unreachable deviations and the `erf` ulp (RS3, 29 Oct 2026) ⚠ UNREVIEWED

Left as they are and documented: `leg_costs` with `lot = 0` returns `inf` where Python raises
(lots fall back to 500); `simulate_futures`' `pnl/e` with `e = 0` reports `ValueError` where
Python raises `ZeroDivisionError` (`continuous` replaces `o <= 0` with 1.0, so `e` is never 0); a
negative horizon is clamped to 0 where numpy would index from the end (`FAMILIES` fixes every
horizon positive); `trading_days_before(n < 0)` returns `None` where Python raises `IndexError`;
polars' **unstable** sort on ties in `simulate_futures`' signal `di` (A2 can give one symbol both
sides on one `di` when fewer than 20 names trade) is matched only where polars falls back to its
stable small-slice sort. `fno::vol::greeks::erf` is FreeBSD's fdlibm `s_erf.c`: CPython's
`math.erf` calls the platform libm, which stable Rust does not expose and the `unsafe` ban keeps
out of reach; it can differ from Apple's libm by 1–2 ulp, so an implied-vol bisection branch can
flip only within 1 ulp of the root. **How to close:** each is one function; add a golden first.

## R3.10 — Wave 4 order: providers → db → api → worker → desk → execution last (RS3, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** The README's step table put `baskfy-execution` third, straight after providers.
The wave-3 workflow brief ordered the I/O layers providers → db layer → api → worker → desk →
execution **last**.

**Choice.** Execution last, and a `baskfy-db` step (sqlx against the existing schema, read-only
first, graded on a DB copy) added between providers and the API; README and STATUS updated. Why:
law 2 makes execution the one path to an order, so it should be built when everything that
*plans* orders already reproduces the Python byte for byte, against the most evidence, and it
carries the most risk if wrong. The desk's Rust port runs its two shadow Fridays in `DRY_RUN`
comparing plans only; no Rust order path exists until step 7, and the Python execution package
keeps placing the desk's orders until Maulik's explicit cutover.

**Rejected.** Execution third (it would sit unused, with nothing upstream in Rust to feed it, and
ages its review before it is exercised); skipping a separate db crate (every service would carry
its own queries and the read-only-first discipline would be per-service, not enforced once).

**How to reverse.** Swap the README rows back; no code depends on the order yet.

## R4.1 — Wave 4's I/O stack, pinned once for every porter (RS4, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** `baskfy-providers` is the first crate that touches the network, Redis and disk.
The kite and nse porters run in parallel after the foundation and must not both edit
`rust/Cargo.toml`, so every dependency they plausibly need is pinned now.

**Choice.** In `[workspace.dependencies]`, each exact (`=x.y.z`): **tokio 1.54.1** (rt-multi-thread,
macros, time, sync, net, fs, io-util; tests add `test-util` for paused time); **reqwest 0.12.28**
with `default-features = false` and `rustls-tls` (no OpenSSL), `cookies` (NSE's session
discipline), `gzip`/`deflate`/`brotli` (what httpx decodes), `json`, `http2`, `charset`;
**redis 0.32.7** (`script`, `tokio-comp`, `connection-manager`; 0.32 is the newest non-rc line);
**async-trait 0.1.92**; **fernet 0.2.2** on its RustCrypto backend (no OpenSSL); **rand 0.9.5**;
**rustix 1.1.6** (`time` — see R4.4); **zip 9.1.3** with only `deflate-flate2-zlib-rs`
(pure-Rust inflate for the bhavcopy zips); **csv 1.4.0** (Kite's instrument dump); **regex
1.13.1** and **percent-encoding 2.3.2** (nse.py's `re` and `urllib.parse.quote`); **sha2 0.10.9**
and **hmac 0.12.1** (archive SHA-256 metadata, Kite's login checksum, a hand-signed S3 SigV4 if
the archive porter takes that route — the 0.10/0.12 lines are the ones fernet already builds);
**tempfile 3.27.0** (tests). No `chrono-tz`: IST has no DST, so `FixedOffset` (+05:30) is exact.
The ports are **async traits** (Python's are synchronous because its callers are Celery tasks; the
Rust callers are axum and a tokio worker).

**Rejected.** reqwest 0.13 (feature names changed; 0.12 is what the registry already held and is
maintained); `aws-sdk-s3` now (hundreds of crates for one PUT/GET — the archive porter decides,
with an entry); blocking reqwest with sync ports (would force `spawn_blocking` at every API call
site); native-tls/OpenSSL (a system library in the one-binary image).

**How to reverse.** Each is one line in `rust/Cargo.toml`; a bump re-runs the providers tests.

## R4.2 — polars IO features are declared by `baskfy-providers` only, never on the workspace pin (RS4, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** `records.py`'s frame schemas are `pl.Decimal(18, 4)` columns, and `nse.py` parses
every CSV/JSON with `pl.read_csv(infer_schema_length=None)` / `pl.read_json` over in-memory bytes
— polars' own schema inference is the behaviour to reproduce. `law_one.rs` forbids IO features
on the **workspace** polars line that `baskfy-core` reads.

**Choice.** `crates/baskfy-providers/Cargo.toml` adds `dtype-decimal`, `csv` and `json` to its own
`polars = { workspace = true, features = [...] }`. The workspace pin is unchanged and `law_one.rs`
passes. Cargo unifies features in a workspace build, so the shared `polars` compiled for
`cargo test --workspace` carries them; `baskfy-core`'s **source** still cannot use them (its scan
forbids the tokens) and `cargo test -p baskfy-core` builds without them. A future fixtures port
that reads Parquet adds `parquet` the same way, with its own entry.

**Rejected.** Hand-rolled CSV inference with the `csv` crate for NSE (reproducing polars'
dtype inference by hand is where a port silently diverges); widening the workspace pin (would
fail law 1's check, and should).

**How to reverse.** Drop the three features from the providers manifest; the nse porter then
parses with `csv` and builds frames by hand.

## R4.3 — One `ProviderError` with Python's class as a kind, and `Foreign` for non-provider exceptions (RS4, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** Python's hierarchy is answered by `isinstance`; retry retries only
`TransientProviderError` and the breaker counts only it. Some raises on the provider path are
*not* `ProviderError`s — a `ValueError` from a config check, a `redis.ConnectionError` from the
limiter — and Python lets them straight through both.

**Choice.** `ProviderError { kind: ErrorKind, message, provider, detail, source }`, one variant per
Python class, with `is_transient`/`is_unavailable`/`is_data_error`/`python_type`; `Display` is
Python's `__str__` (`[provider] message`). `ErrorKind::Foreign` carries any other Python class by
name, so it propagates untouched by retry and the breaker, exactly as in Python. Message texts
are Python's (checked against the live classes).

**How to reverse.** Split into a provider enum plus an outer error; the call sites' `?` change.

## R4.4 — The production monotonic clock is `CLOCK_MONOTONIC` (RS4, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** `RedisTokenBucket` stamps the shared Redis bucket with the *caller's*
`time.monotonic()` (the NSE limiter, key `baskfy:ratelimit:nse`). A Rust process sharing that
key with Python processes must read the same clock. `std::time::Instant` is opaque.

**Choice.** `clock::SystemClock::monotonic` reads `clock_gettime(CLOCK_MONOTONIC)` through
rustix's safe API — Python's clock on Linux, so processes in containers on one host agree.
**Known gap:** on macOS Python reads `mach_absolute_time` (stops during sleep) and rustix exposes
no `CLOCK_UPTIME_RAW`; a native-Mac Rust process and a native-Mac Python process sharing one NSE
bucket would disagree after a sleep. Today Python runs in Docker (Linux), so no shared bucket is
affected. The call spacers use Redis's `TIME` and are unaffected anywhere.

**How to reverse.** Swap `system_monotonic()`; or move the bucket to Redis `TIME` in both
languages (a Python change).

## R4.5 — The network block: one chokepoint plus source scans, instead of patching sockets (RS4, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** Python's `network_guard.py` monkeypatches `socket.connect`/`getaddrinfo` for the
whole test session. Rust cannot patch the standard library.

**Choice.** `net.rs` is the only place an HTTP client is built (`client_builder()` installs a
guarded DNS resolver and a redirect policy; `GuardedClient` checks every URL's host, so an IP
literal is refused too). `net::block_network()` arms a process-wide flag; loopback (`127.0.0.1`,
`::1`, `localhost`, `0.0.0.0`) and Unix sockets stay usable, as in Python. Redis connects only
after `guard_url`. `tests/network_is_blocked.rs` ports Python's cases and adds two scans: no
client/socket constructor outside `net.rs` (Redis only in `ratelimit.rs`, after its guard), and
every test in `tests/` calls `arm()` first. **Porters:** build clients from
`net::client_builder()`, and start every test with `common::arm()`.

**Rejected.** An `LD_PRELOAD`/seccomp sandbox (platform-specific, outside cargo); trusting
discipline (the Python suite's reason for the guard is that discipline fails silently).

**How to reverse.** Delete the flag; the scans then have nothing to protect.

## R4.6 — Records are validated at construction and read-only by `Deref` (RS4, 29 Oct 2026) ⚠ UNREVIEWED

Each pydantic record `X` is `X(XFields)`: built only by `X::new` or by deserialising (both run
the validators — min lengths, `gt`/`ge`, positive ratio legs, non-negative quantities and prices,
an aware minute-bar timestamp), `deny_unknown_fields` for `extra="forbid"`, and `Deref<Target =
XFields>` without `DerefMut`, so `record.symbol` reads and nothing writes (`frozen=True`). Literal
types are enums; Python datetimes that may be naive (Kite's quote stamps) are `PyDateTime`
(naive or aware, Python's `isoformat`). Decimals serialise as strings (R0.2) and deserialise
from strings only — pydantic also accepts a JSON number. `BasketMarginRecord.shape` is a
`BTreeMap` (sorted keys; Python keeps insertion order; it feeds a probe's log only). Validation
messages follow pydantic's wording but not its full `ValidationError` layout. The frame schemas
are polars `Schema`s with `Decimal(18, 4)`/`Decimal(20, 2)`; `conform` is a strict cast, as
Python's. **How to reverse:** public fields and `validate()`; the tests show what is lost.

## R4.7 — Settings read the environment case-insensitively and parse dotenv by hand (RS4, 29 Oct 2026) ⚠ UNREVIEWED

`ProviderSettings::from_env` reproduces pydantic-settings' sources and precedence (process env >
`../../.env` > `.env` > defaults), its case-insensitive `BASKFY_` matching (verified: Python
honours `baskfy_kite_rate_limit_per_second`), lax number coercion (whitespace, `_` separators,
an integer's `.0` tail), every field constraint (all reported together, then the bulk-lane
check, whose message is Python's byte for byte). Credentials and `redis_url` are `Secret`s that
never print. Not reproduced: python-dotenv's `${VAR}` interpolation and multi-line values (no
`.env` here uses them); `SettingsError` is not pydantic's error layout. **How to reverse:** add
the `dotenvy` crate and an interpolation pass.

## R4.8 — The rate limiter is tested against an exact in-memory Redis on virtual time (RS4, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** Python's `test_ratelimit.py` runs against the live Redis that `make up` provides,
in real time, and skips without one; the brief asks for deterministic tests with an injectable
clock, and agents must not touch the `baskfy-local` containers.

**Choice.** `ratelimit.rs` runs Python's two Lua scripts **byte for byte** against a real Redis
(`RedisScriptBackend`; a test holds the strings equal to `ratelimit.py`), with Python's argument
encoding (`repr(float)`). `InMemoryScriptBackend` executes the same scripts' semantics exactly —
Lua 5.1 `math.min`/`math.ceil`, Redis 7's exact number-to-argument conversion, `tostring` as
`%.14g`, `PEXPIRE`/`PX` expiry, `TIME` from the injected clock — under one lock (Redis runs a
script alone). The Python cases run on tokio's paused clock: two workers interleave as on a real
box, and the "more than 3 req/s is impossible" bounds are asserted exactly. One concession:
tokio's timer fires on whole milliseconds, so the spacer tests allow 1 ms between a reserved slot
and the departure. `real_redis_single_worker_cases` repeats the single-worker cases against a
real Redis only when `BASKFY_TEST_REDIS_URL` names one (loopback only — the block refuses
anything else); unset, it returns without running.

**How to reverse.** Point the tests at `RedisScriptBackend`; they become real-time and need Redis.

## R4.9 — Token store: Python-compatible Fernet blobs, written owner-only from the first byte (RS4, 29 Oct 2026) ⚠ UNREVIEWED

The blob is Python's exactly: Fernet over `json.dumps({"version": 1, "access_token", "issued_at"})`
(`ensure_ascii`, Python's separators, `isoformat()` with microseconds only when non-zero). A test
decrypts a blob Python's `AccessTokenStore.save` wrote (`tests/fixtures/python-token-blob.json`, a
throwaway key) and checks Rust writes Python's plaintext byte for byte. Two deliberate
differences: the file is created with mode `0600` (Python writes, then `chmod`s — a window where
the umask applies); and a stored naive `issued_at` (neither writer produces one) is read as IST,
where Python's `astimezone()` would use the box's local zone. A non-object JSON payload is
`TypeError` as in Python; `str()` of a non-string token follows Python's spelling for
`None`/`True`/`False`. **How to reverse:** write-then-chmod; read naive times in local time.

## R4.10 — Kite: `KiteConnect` reimplemented, its known bug reproduced, and the limits it refuses (RS4a, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** Zerodha has no Rust SDK. `kite.py` drives `kiteconnect` 5.2.1's `KiteConnect` and
reads what it returns as Python objects (`dateutil` dates, `csv.DictReader` rows, `requests`
errors).

**Choice.**
* **The SDK's read-only REST half is reimplemented** in `kite.rs` as `KiteConnect`: URLs,
  `quote_plus` query encoding, headers, the timeout, response validation and the SDK's
  exception classes, including the edge cases a differential found (a non-dict `data` in
  `quote()` gives `{}` or `TypeError` as the SDK's comprehension does; a UTF-8 BOM and a bare `\r`
  in the instrument dump fail as Python's `csv` fails). It has **no order verb** (law 2). The
  `Authorization` header is marked sensitive (RS4a integration) so no `Debug` can show it.
* **KNOWN BUG, reproduced and not fixed:** `kite.py`'s translation table names the builtin
  `TimeoutError`/`ConnectionError`. `requests` raises `ReadTimeout`/`ConnectionError`, which
  subclass neither, so a Kite read timeout or a refused connection is not translated, not
  transient and **not retried** in either language. The fix belongs in both languages at once
  (translate the transport classes to `UpstreamUnavailable`), after the cutover gates.
* **Coercions are Python's own parsers:** `_date`/`_aware_ist` port CPython 3.12's C
  `datetime.fromisoformat` (`py_fromisoformat`; the foundation's `PyDateTime::parse` was not
  that parser, and differed on 13 inputs); `_decimal` drops underscores as `Decimal(str)` does.
  Graded by `coercions_match_the_live_python` (about 140 inputs) and
  `instrument_dumps_parse_like_the_sdk` (24 CSV bodies against kiteconnect's
  `_parse_instruments`), both with committed generators beside their answer keys.
* **`TradeFill` lives in `kite.rs`** privately (the R2.3 pattern) until `baskfy_core.tradebook`
  is ported.
* **Frames are built eagerly** at `Decimal(18, 4)`, with `conform`'s rounding (half to even) and
  its refusal message reproduced, rather than through `records::conform`. Reason:
  `conform` runs polars' lazy engine, whose `block_in_place` **panics on a current-thread tokio
  runtime**. An adapter method must not panic. This is a hazard for the foundation owner: any
  async caller of `records::conform` on a current-thread runtime panics. Each chunk is checked
  before the next request, as Python's list comprehension does, so a bad price fails after one call.
* **Limits (loud, never silent):** a Decimal NaN/Infinity or beyond 28 significant digits is
  refused; an int beyond i64 is `OverflowError`; `DictReader`'s `None`-keyed extra cells are
  dropped (no caller reads them); a sub-second `fromisoformat` UTC offset is refused because
  `FixedOffset` cannot hold it. Kite sends none of these.
* **Health:** `check()` reports *every* token-store error as a health problem. Python catches only
  `AccessTokenExpired`, `CredentialsMissing` and `UnexpectedPayload` and lets an `OSError` raise.
  The `HealthReporting` trait returns `ProviderHealth`, not a `Result`, so Python's behaviour needs
  a trait change. Left as a documented deviation.

**Rejected.** A Python sidecar for the SDK (two runtimes on the order-adjacent path); trusting
`csv`'s and chrono's parsers (both differ from Python's at the edges the differential found).

**How to reverse.** The SDK half is one type behind `KiteClient`; a future official SDK slots in
through `KiteRuntime::client_factory`.

## R4.11 — NSE: Python's dynamic cell values are carried as `PyValue` (RS4a, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** `nse.py` reads CSV/JSON with polars and walks the rows as Python objects
(`str(value)`, `value or ""`, `Decimal(str(float))`). Those conversions *are* the behaviour: a
cell `800.00` is inferred as a float and becomes `Decimal("800.0")`.

**Choice.** `PyValue` carries each cell as Python sees it, and `py_str`, truthiness, `_decimal`,
`_int` and `_date` are Python's, case by case. A 229-file corpus run through the live Python
and Rust gives 215 identical results. Fixed at review: `strptime` no longer clamps seconds 60/61
to 59. Python refuses them, and the clamp fabricated a timestamp. `equity_fundamentals`'
concurrent path stops starting new symbols after the first failure in input order, as `pool.map`
does. Without that, about 3,300 symbols each ran a full retry budget against a failing NSE.

**Documented deviations that remain** (none changes a value Python would store):
(a) NaN/Infinity/sNaN text is refused with `ValidationError`, where Python raises
`OverflowError`/`InvalidOperation`/a panic; (b) values beyond R1.3's limits (over 28 digits or
scale 28, ints beyond i64, e.g. a 1e20 volume) are `OverflowError` where Python accepts or fails
later; (c) a positive-exponent Decimal prints `100000` where Python prints `1E+5` (the values are
equal); (d) Unicode `Nd` digits (`١`) give `None` where Python gives 1; (e) library error *texts*
differ (polars CSV, zip EOCD, UTF-8 positions); (f) the redirect cap is 10, where httpx allows 20;
(g) `CorporateAction.raw` key order follows `serde_json::Map`; (h) `records::conform` maps every
polars failure to `InvalidOperationError`, where Python raises `ComputeError` on a float-to-decimal
precision overflow (a foundation-lane item).

**How to reverse.** Each deviation is one helper; the differential corpus shows the drift.

## R4.12 — `FixtureProvider` waits for polars' `parquet` feature (RS4a, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** The fixture files are Parquet. R4.2 deliberately left `parquet` off the providers
manifest, to be added with its own entry.

**Choice (for now).** Not added in RS4a. The integrator fixes only integration issues, and the
feature adds a large compile surface. `FixtureProvider` serves frames handed to it in memory
(`from_frames`), and every rule above the file read is tested. A fixture *file* is reported
unreadable (`check()` says so, and every read is `ProviderUnavailable`). **Consequence:** docs/03's
`local` environment cannot run on the Rust stack until this lands. Eight file-backed cases
from `test_fixtures.py` are `#[ignore]`d with this entry as the reason, and they run once the
feature is declared. `TestAnchoredToRealData` (4 cases, needs core's `reference_export`) and
`TestDeterminism` (1 case, needs `fixture_builder`) have no Rust counterpart yet.

**How to reverse / complete.** Add `"parquet"` to the providers crate's polars features (never the
workspace pin: `law_one.rs`), replace the refusal with `ParquetReader`, and un-ignore the eight
tests.

## R4.13 — The factory bounds the Redis connect wait at 5 s (RS4a, 29 Oct 2026) ⚠ UNREVIEWED

Python's `Redis.from_url(url).ping()` fails at once on a refused connection. redis-rs's connection
manager retries with backoff instead, so `RedisSource` waits at most
`REDIS_CONNECT_TIMEOUT_SECONDS = 5.0` and then reports Redis unreachable, which leaves the
provider with no limiter, so it refuses to call upstream, as in Python. **RS4a integration:** an
unparseable Redis URL is no longer quoted in `guard_url`'s `ValueError` (a Redis URL can carry a
password); `an_unparseable_redis_url_never_reaches_the_message` pins it. **How to reverse:** change
the constant.

## R4.14 — The raw archive signs S3 requests by hand (SigV4) with botocore's timeouts and no retries yet (RS4a, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** R4.1 left the archive porter to choose between `aws-sdk-s3` and a hand-signed client.
The archive needs exactly `PutObject`, `GetObject` and `HeadObject`, path-style, against R2 or S3.

**Choice.** `SigV4S3Client`, built on `net::GuardedClient` (so the network block covers it) with
the already-pinned `sha2`/`hmac`. Credentials are `Secret`s, and its `Debug` shows only the
endpoint and region. Fixed at review: there were no timeouts, so a stalled bucket would hang
archive-then-parse and the nightly behind it. It now uses botocore's `DEFAULT_TIMEOUT` (60 s
connect, 60 s read; `S3_TIMEOUT_SECONDS`), as Python's boto3 client does.

**Open residual.** boto3 retries throttling, 5xx and connection errors internally (legacy mode,
5 attempts). The Rust client does not retry, so one transient R2 error fails the archive step where
Python would have recovered. Matching it needs a choice of retry mode and backoff. Proposed:
legacy mode's 5 attempts with botocore's exponential backoff, through the crate's `retry` module.
This is recorded here and must close before the worker's nightly runs on Rust.

**Rejected.** `aws-sdk-s3` (hundreds of crates and its own HTTP stack outside `net.rs`'s
chokepoint, for three verbs).

**How to reverse.** Implement `S3ClientLike` over `aws-sdk-s3`; `S3RawArchive` does not change.

## R4.15 — RS4a integration: decision numbers renumbered, no new dependency (RS4a, 29 Oct 2026) ⚠ UNREVIEWED

The kite and nse lanes ran in parallel and both used "R4.10". Kite keeps R4.10. The nse lane's
archive citation (SigV4) becomes R4.14. R4.11–R4.13 are as the nse lane cited them. RS4a adds
**no dependency** beyond R4.1's pins: `Cargo.toml` is unchanged by both lanes. The secret audit
covered `Debug` derives, `format!` of credentials, error strings and print calls. It found no
`Debug` that shows a credential and no print/log call in the crate. Two hardenings were made: the
sensitive `Authorization` header (R4.10) and the redacted Redis URL (R4.13). Every
non-loopback URL in `tests/` is either a string under test or a call the armed block refuses
(`network_is_blocked.rs`, `nse_http_wire.rs`). Every test file calls `arm()`.
