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

## R4.14 — The raw archive signs S3 requests by hand (SigV4) with botocore's timeouts and no retries yet (RS4a, 29 Oct 2026; **retries closed at RS5.0, 30 Oct 2026**) ⚠ UNREVIEWED

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

**Closed (RS5.0, wave 5's foundation, 30 Oct 2026).** Read from the archive's own client rather
than assumed: the Python archive builds `boto3.client("s3", endpoint_url=…, region_name=…, …)`
with no `config`, so botocore 1.43.75 resolves `retry_mode` from `AWS_RETRY_MODE`/the config file,
else **`legacy`** (`configprovider._DEFAULT_RETRY_MODE`; `AWS_NEW_RETRIES_2026` is unset, so
`NEW_RETRIES_ENABLED` is false). Introspecting a client built offline confirmed the registered
handler: `MaxAttemptsDecorator(max_attempts=5)` over `_retry.json`'s `__default__` policies plus
`s3`'s two. `SigV4S3Client::send` now reproduces exactly that:

* **Five attempts in all** (the first included), each re-signed (a fresh `x-amz-date`), the last
  attempt's failure returned (`S3_MAX_ATTEMPTS`).
* **Retried:** any transport error (botocore's `GENERAL_CONNECTION_ERROR` family —
  `ConnectionError`, `ConnectionClosedError`, `ReadTimeoutError`, `EndpointConnectionError`, and
  their subclasses `ConnectTimeoutError`, `SSLError`); statuses 429, 500, 502, 503, 504, 509; a
  400 whose XML `<Code>` is `Throttling`, `ThrottlingException`, `ThrottledException`,
  `RequestThrottledException`, `ProvisionedThroughputExceededException`, or S3's own
  `RequestTimeout` / `BadDigest`. **Not retried:** every other answer (301, a plain 400, 403,
  404, …) and a request refused before it is sent (the network block, a bad URL).
* **Backoff:** `delay_exponential(base="rand", growth_factor=2)` — `random() * 2 ** (n - 1)`
  seconds after the n-th failed attempt, a fresh draw each time — through the crate's `Sleeper`
  and `Random` seams (`SigV4S3Client::with_seams`).
* A GET's body is read after the retry loop, as botocore's `StreamingBody` is, so a body that
  breaks mid-read is not retried in either language.

Tested in `tests/s3_retry.rs` (8 cases) against a stub S3 on loopback with a recording sleeper and
a fixed draw: the policy table, recover-after-500/503, five-and-out with sleeps `0.5, 1, 2, 4`,
the code-named 400s retried and a plain 400 not, 429 retried and 403/404 answered at once, a
dropped connection and a refused port retried, a blocked host never attempted. Not reproduced
(unreachable against R2): `S3RegionRedirectorv2` (a 301/400 region redirect re-sent to the right
region) and botocore's `_update_status_code` for 200-with-error bodies, which apply to
`CopyObject`/`CompleteMultipartUpload`, neither of which the archive sends.

## R4.15 — RS4a integration: decision numbers renumbered, no new dependency (RS4a, 29 Oct 2026) ⚠ UNREVIEWED

The kite and nse lanes ran in parallel and both used "R4.10". Kite keeps R4.10. The nse lane's
archive citation (SigV4) becomes R4.14. R4.11–R4.13 are as the nse lane cited them. RS4a adds
**no dependency** beyond R4.1's pins: `Cargo.toml` is unchanged by both lanes. The secret audit
covered `Debug` derives, `format!` of credentials, error strings and print calls. It found no
`Debug` that shows a credential and no print/log call in the crate. Two hardenings were made: the
sensitive `Authorization` header (R4.10) and the redacted Redis URL (R4.13). Every
non-loopback URL in `tests/` is either a string under test or a call the armed block refuses
(`network_is_blocked.rs`, `nse_http_wire.rs`). Every test file calls `arm()`.

## R5.1 — Wave 4b's dependencies: sqlx, axum, tower, base64, and serde_json's `raw_value` (RS4b, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** `baskfy-db` and `baskfy-api` are the first crates that talk to Postgres and serve
HTTP. Five API lanes follow in parallel and must not edit `rust/Cargo.toml`.

**Choice.** In `[workspace.dependencies]`, exact pins: **sqlx 0.8.6** with
`default-features = false` and only `runtime-tokio`, `tls-rustls-ring-webpki` (no OpenSSL),
`postgres`, `rust_decimal`, `chrono`, `json` — **no `macros`/`migrate`**: queries are runtime
strings, so no build ever needs a live database, and Alembic stays the only schema owner.
**axum 0.8.9** with `default-features = false` and `tokio`, `http1`, `matched-path`,
`original-uri` — no `json`/`query`/`form` extractors, because the API parses query strings and
renders bodies itself to match Starlette and pydantic exactly (R5.8). **tower 0.5.3** (no
features; already in the lock through axum) for the `Service` trait the slash-redirect probe
uses. **base64 0.22.1** (already in the lock through reqwest) for JWT segments. `serde_json`
gains **`raw_value`**, so a Python `float` is written with `repr` (`1e-05`, `1e+16`) instead of
ryu's spelling. No `jsonwebtoken` (R5.5), no `tower-http` (CORS hand-ported, R5.8), no
`tracing` (a 150-line logger reproduces Python's records and redaction), no `uuid` (a request id
is 16 random bytes with the v4 bits set).

**Known side effect.** sqlx's `chrono` feature enables chrono's `clock` in a **workspace** build
by feature unification (as R4.2 noted for polars' IO features). `baskfy-core`'s manifest still
declares chrono without it, `law_one.rs` still passes, and its source scan still refuses
`Utc::now`/`Local::now`; `cargo test -p baskfy-core` builds without the clock.

**Rejected.** sqlx 0.9.0 (a new major line; 0.8.6 is the established one, and the one the
Postgres numeric decoding was checked against on the bed); sqlx `macros` (compile-time checking needs
`DATABASE_URL` at build time — the live database must never be one); `tokio-postgres` (no pool,
no typed decimals); actix-web (axum is tower-native and already shares tokio/hyper with
reqwest).

**How to reverse.** Each is one line in `rust/Cargo.toml`.

## R5.2 — The pool is read-only on every connection, with Python's sizing and no statement timeout (RS4b, 29 Oct 2026) ⚠ UNREVIEWED

**Choice.** `baskfy_db::Db` sends `default_transaction_read_only=on` in the **startup packet**
(not a `SET` after connect, which a pool reset could lose), so no statement on any connection can
write; `tests/bed.rs::a_write_is_refused` proves `UPDATE` and `CREATE TABLE` (also inside an
explicit transaction) fail with SQLSTATE `25006`. `READ_ONLY` is a constant, not a setting:
turning it off is a code change with its own entry, one table at a time (README step 3).
Sizing is `create_engine`'s: `max_connections = db_pool_size + db_max_overflow`,
`acquire_timeout = db_pool_timeout_seconds`, `max_lifetime = db_pool_recycle_seconds`,
pre-ping, statement cache `db_statement_cache_size`, lazy connect (the app starts and answers
`/health` with the database down). **Statement timeout: none**, because `db.py` sets neither
`statement_timeout` nor asyncpg's `command_timeout` — the brief's "statement timeouts matching
db.py" is therefore "none"; inventing one would change which requests fail. Idle connections are
never reaped (SQLAlchemy's `QueuePool` keeps `pool_size` of them; sqlx's default would close
them after 10 minutes). `application_name = baskfy-rs`.

**Consequence.** Every write route waits: `PATCH /me`, `/auth/*`, the settings PATCHes, scans.
The one read route Python makes write (`GET /me`'s staff grant) is R5.7.

**How to reverse.** Flip `READ_ONLY`, per crate feature or per table, with an entry.

## R5.3 — `numeric` is decoded from the wire, `jsonb` into `OrderedJson` (RS4b, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** asyncpg gives `Decimal` with the column's display scale, so the API prints
`"0.00"` for `0::numeric(10,2)`. sqlx's `rust_decimal` decoder returns `0` (scale lost) for a
zero — found by `tests/bed.rs::numeric_keeps_its_scale`. And `serde_json::Value` sorts object
keys (the workspace has no `preserve_order`), where Python's `json.loads` keeps jsonb's stored
order and FastAPI writes it back in that order.

**Choice.** `row::decimal` decodes Postgres's binary `numeric` itself (`ndigits, weight, sign,
dscale`, base-10000 digits) into a `Decimal` with exactly `dscale` places; NaN/±Infinity and
anything beyond 28 digits are **errors, never rounded** (the R1.3 class). `row::jsonb` decodes
into `baskfy_db::OrderedJson`: keys in arrival order, a repeated key keeps its first place and
last value (Python's `dict`), an integer literal stays an int, any other number is a float
written back with `repr` (so jsonb `1.50` echoes as `1.5`, as `json.dumps(json.loads(...))`
does). **Known limit:** an integer outside `i64`/`u64` becomes a float (Python keeps an exact
`int`); no jsonb column holds one today.

**Rejected.** `serde_json`'s `preserve_order` workspace-wide (changes `Value` iteration for the
testkit and every golden decoder); `arbitrary_precision` (changes number handling everywhere).

**How to reverse.** Use sqlx's decoder once it keeps a zero's scale.

## R5.4 — Database tests run on the bed, and are reported *ignored with the reason* elsewhere (RS4b, 29 Oct 2026) ⚠ UNREVIEWED

`baskfy-db` and `baskfy-api` each have a `build.rs` that sets `cfg(bed)` when
`BASKFY_RS_TEST_DATABASE_URL` is non-empty at **build** time (rerun when it changes). Every test
that needs the database carries `#[cfg_attr(not(bed), ignore = "BASKFY_RS_TEST_DATABASE_URL is
unset at build time: the bed tests are skipped")]`, so without the bed they show up in the
summary as ignored with that sentence — never as a silent pass, which an early `return` would
have been. Run the full suite with
`BASKFY_RS_TEST_DATABASE_URL=postgres://baskfy:rs-test-only@127.0.0.1:55432/baskfy` set. The
database-free API tests point at a dead loopback port (`127.0.0.1:9`), so they never need it.

## R5.5 — PyJWT's acceptance rules are reimplemented, not delegated to a JWT crate (RS4b, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** Which tokens the API accepts is the contract, and PyJWT 2.13 has specific opinions:
`require=["exp","iat","sub"]` means present and non-null; `iat` and `nbf` are checked against
the leeway; `int("1790000500")` is a valid `exp` and `int([1])` escapes as a `TypeError` (a 500);
an `aud` claim with no configured audience is refused, an empty one is not; `kid` must be a
string; `crit` may name only `b64`; `sub` and `jti` must be strings.

**Choice.** `auth::pyjwt_decode` reproduces those rules in order with `hmac`/`sha2`/`base64`;
`tests/jwt.rs` holds about forty assertions; every decode outcome in it was first run
against PyJWT 2.13 in the decile-blueprint venv (except the `exp == now - leeway` boundary,
which follows PyJWT's `<=` in its source; the epoch reading is the API's `_token_epoch`). Errors carry PyJWT's class name for the log line; the client sees one sentence (no oracle).
Approximations, recorded: base64's non-strict decode discards characters outside the alphabet
(binascii's exact padding edge cases are not reproduced); a header or payload with `NaN`/
`Infinity` tokens (Python's `json` accepts them) is refused as undecodable. Both need a
correctly signed token to reach, i.e. the shared secret.

**Rejected.** `jsonwebtoken` 11 (different `aud`/`iat` semantics, a crypto backend choice, and
its own error taxonomy to map back).

## R5.6 — `baskfy_core::screener::lax_date_from_str` is public (RS4b, 29 Oct 2026) ⚠ UNREVIEWED

FastAPI validates a `datetime.date` query parameter with pydantic's lax string parse
(`2026-10-01`, a midnight datetime, a midnight Unix timestamp; speedate's error messages). The
screener port already reproduces that byte for byte in a private `speedate` module (R2.4). A
public wrapper, `lax_date_from_str(s) -> Result<NaiveDate, (type, message)>`, now exposes the
string branch; `baskfy_api::query::Validation::date` uses it. Pure, so law 1 holds; no golden
changed. **How to reverse:** move the module out of the screener into a shared `pydantic` module.

## R5.7 — `GET /me` does not persist the staff-allowlist grant on the read-only pool (RS4b, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** Python's `GET /me` calls `ensure_staff_from_allowlist`, which **writes**
`app_user.is_staff = true` for an allowlisted address not yet marked staff, then answers
`is_staff: true`. R5.2 forbids writes.

**Choice.** The Rust route answers `is_staff = stored || settings.is_staff_email(email)` — the
same response — without the write; every later request computes it again, so the answer never
diverges, only the stored bit lags. On the bed all three allowlisted accounts are already staff,
so the parity cases are byte-equal either way. The write moves into the route when the pool may
write `app_user` (and the never-demote rule comes with it).

**How to reverse.** Add the `UPDATE` once `app_user` is writable.

## R5.8 — Starlette's and FastAPI's framework behaviour is reproduced in the pipeline (RS4b, 29 Oct 2026) ⚠ UNREVIEWED

What a client can observe of the framework, each found by probing the Python API on the bed:

* **Routing on the decoded path** (`/api/v1/meta%2Fcolumns` reaches `/meta/columns`): the URI is
  rewritten to the decoded path before routing (re-encoding only what a URI cannot carry, `%`
  included, so axum's `Path` decodes once to Python's value). This wraps the router as a service:
  axum's `Router::layer` runs *after* routing.
* **`redirect_slashes`**: an unmatched path whose slash-toggled twin matches any route (any
  method — a partial match counts) is a `307` with Starlette's absolute, quoted `location` and
  the query kept; `/` never redirects; a `405` wins over a redirect. Implemented by probing the
  router with a marker request that the outermost route layer answers before any dependency or
  handler runs.
* **404/405 problems**: `detail` `Not Found` / `Method Not Allowed`; 405 is `internal-error`
  with status 405 and **no `Allow` header** (axum adds one; it is removed). **`HEAD`** is never
  served (FastAPI's GET routes are `{"GET"}` only): 405 on a known path, 404 otherwise.
* **The `Exception` catch-all** runs in Starlette's `ServerErrorMiddleware`, outside every user
  middleware, so a 500 carries neither `X-Request-Id` nor CORS headers; `Problem::outside_middleware`
  reproduces that. A database error is such a 500 (`SQLAlchemyError` has no handler).
* **CORS** is Starlette's `CORSMiddleware`, hand-ported: preflight `200 "OK"`/`400 "Disallowed
  CORS …"` with the safelisted headers merged and sorted, `Vary: Origin` appended, credentials and
  expose headers on every simple response.
* **The `/api/v1` router dependency** (principal, then rate limit) runs only for matched routes,
  so an unrouted path never touches the database or the bucket, and a bad token is refused before
  the limit is charged.

**Known difference for porters.** Starlette serves the first route that matches in registration
order; axum prefers a static segment over a parameter. Where a Python router registers `/{id}`
before a static sibling, the lane checks what Python actually serves and records it.

**Not ported yet:** `/metrics`, `/api/v1/openapi.json`, `/api/v1/docs` (the OpenAPI gate in the
README step table is still open).

## R5.9 — The API harness: high limits, a fixed account roster, explicit masks (RS4b, 29 Oct 2026) ⚠ UNREVIEWED

`tools/parity/api_diff.py` (README §"Grading the API") starts both servers with rate limits of
1,000,000/min: the Python and Rust processes share one Redis bucket per caller (by design,
R4.4's caveat on a native-Mac clock aside) and five lanes share the bed's Redis, so real limits
would make results depend on run order. The 429 shape is graded in `tests/api_errors.rs` against
the in-memory Lua backend on paused time. Accounts are a fixed roster of the bed's real users
(`USERS`), chosen for what the foundation cases need (staff with overrides, a paying
subscriber, a plain account for tenant isolation) plus every token failure mode. A mask is the
only way to ignore a field and is printed in the report; `location` is compared with the port
replaced; `etag` is compared by value unless a mask applies (a masked body would legitimately
hash differently). The harness refuses any database other than the bed's port unless
`--allow-db-url`, refuses the live stack's ports outright, starts servers from empty working
directories so no `.env` leaks in, and stops only the processes it started.

## R5.10 — RS4b integration: the five API lanes merged, decision numbers assigned, one foundation test re-aimed (RS4b, 30 Oct 2026) — **the helper collapse done at RS4c.0, R6.9** ⚠ UNREVIEWED

**Context.** Five lanes (reference, sleeves, derivatives, portfolio, accounts) ported the GET
routes of the 44 Python routers in parallel, each owning its `routes/*.rs` files and its
`tools/parity/api_cases/<lane>.json`, none allowed to edit `routes.rs`, `app.rs`, the shared
modules, `baskfy-db` or the docs. Each lane then had an adversarial verifier, which fixed what it
found and grew the case files. The lanes proposed their judgement calls under private labels
(reference R6.4/R6.5, derivatives R6.D1–D5 written into this file, sleeves "lane api-sleeves"
(a)–(g), portfolio "api-portfolio P1–P12", accounts R-acc.1–12).

**Choice.**
* Every lane call is numbered here as **R5.11–R5.36**, the lane's own label kept in the title so
  a code comment citing it (`api-portfolio P3`, `lane api-sleeves`) still resolves by search.
  R6.D1–D5 became R5.19–R5.23. The code comments that cited R6.4, R6.5 and R6.D2–D4 now cite the
  new numbers.
* `tests/api_errors.rs::every_problem_carries_the_instance_and_the_request_id` asked
  `GET /api/v1/screens/unknown-id` against a **dead** database, as an unrouted path. The accounts
  lane routed `/screens/{public_id}`, so it now answers the database-failure 500, which carries
  no request id, exactly as Python's does. The test now asks `/api/v1/still-unrouted/unknown-id`. It
  asks the same property (a problem carries `instance` and a fresh or echoed uuid4 request id), so
  it is not weakened. Python's own test asks `/screens/unknown-id` with a live database (a 404 from
  the route), and the bed's `/screens/{id}` 404 is graded by the accounts case file.
* Nothing else needed integration work. With all five lanes merged, `cargo fmt --check`,
  `clippy --workspace --all-targets -D warnings` and `cargo test --workspace` (bed URL set) were
  green at the first try, apart from the test above.
* **Grading the whole API.** `api_diff.py` runs over all six case files in one run (2,113 cases),
  twice: once with `--env BASKFY_SOLE_USER_ID=1` and once with nothing set (the 503 path). Both runs
  set `--env BASKFY_FIXTURE_DIR=<an empty dir>` (R5.33). Both runs: **2,113 of 2,113 EQUAL**.

**Left for later (not changed here, to keep this an integration):** the lanes wrote four or five
private copies of the same helpers. These are `scoped_sole_user_id`/`resolve_sole_user_id`
(reference, sleeves, derivatives, portfolio `tenant.rs`, accounts), pydantic's lax int/bool/str
parsers (reference, sleeves, accounts, portfolio `params.rs`), `today_ist`, and
`resolve_as_of`/`latest_published_date`. Collapse them into one `routes/common` module (or
`query.rs`) before wave 4c adds more. Every copy is graded EQUAL today, so the collapse must keep
all 2,113 cases EQUAL.

## R5.11 — The live Kite overlay is ported on its no-session path only, in every router (RS4b, 30 Oct 2026; lane labels reference R6.4, sleeves (c), portfolio P3, accounts R-acc.3) ⚠ UNREVIEWED

**Context.** Python overlays live Kite marks in several places when a broker session exists:
`/meta/status` and `/meta/live-marks` (`live_quotes`); the `last_price` column of the swing,
watch, positions, TWT and VBT pages; `/portfolio/overview`, `/portfolio/holdings` and
`/portfolio/{id}` (`live_prices_by_instrument`: the holdings read, then quotes); and the curated
investments and costs. The Rust API has no Kite quote or holdings read path. `baskfy-providers`
`KiteProvider` has no quotes call, and the token-store "is there a usable session" check is only
partly wired. The bed has no session, and live Kite must never be called from it.

**Choice.** Every one of these answers Python's **no-session** branch:
* `quotes_permitted` is false whenever `BASKFY_KITE_API_KEY` is set;
* `last_price` is `null`;
* the portfolio money figures are the close (a warning is logged when a session *might* exist);
* the curated overlay is `{}`, so `marked_at_cost` is true whenever there are holdings.

`/sleeves/state` reads the local token blob through `baskfy_providers::tokens`. Its verifier
checked a real, a `sim_`, an expired, a garbage and a missing blob, all EQUAL.

**Consequence — a cutover gate.** These routes must **not** move to Rust on any host where a
Kite session can exist (the Mac, the box) until a quote/holdings read path is ported:
`/meta/status`, `/meta/live-marks`, `/swing/{setups,watch,positions}`, `/twt/today`,
`/vbt/today`, `/portfolio/*`, `/cb/investments*`. **How to reverse:** port the Kite quote and
holdings reads (rate-limited, through `baskfy-providers`) and the token-store session check, then
grade them on recorded fixtures.

## R5.12 — Decimal query values are held exactly; amounts produce Python's unbounded integers (RS4b lane reference, 30 Oct 2026; supersedes the proposed R6.5) ⚠ UNREVIEWED

**Context.** The porter proposed (as R6.5) that a Decimal query value with an exponent of five or
more digits keep only its sign and answer 500 when used. The verifier found that wrong, and found
two real status differences hiding behind it:
* `GET /explore/{slug}/kite?amount=1e30` (and anything from about 5e27): Rust 400, Python 200 with
  26-to-4,300-digit share quantities;
* `GET /explore?max_min_amount=`: SQLAlchemy binds it as `$n::NUMERIC(20, 2)` (the model type),
  so Python 500s from 1e18 and accepts `1e-10000`.

**Choice.**
* `PyDec` keeps `str(d)` (exact, scientific) for finite and extreme values.
* Share quantities are Python ints held as digit strings and written as bare JSON numbers. The
  integral value is taken as Python takes it (`to_integral_value(ROUND_DOWN)`).
* Python's Emax `Overflow` is a 500 and `ValueError` a 400.
* `max_min_amount` is cast to `numeric(20, 2)` and answers asyncpg's encode limits (an exponent
  below −16383 is a 500) and the certain overflow directly.

All 43 + 27 probe cases are EQUAL. Not graded: quantities beyond 4,300 digits (they need Python's
`-X int_max_str_digits=0`), and amounts ≥ 1e999999, where Python hangs.

## R5.13 — How the reference lane reproduces Python's arithmetic and order (RS4b lane reference, 30 Oct 2026) ⚠ UNREVIEWED

* **Medians and percentiles.** SQLAlchemy's result processor turns `percentile_cont`'s float8
  into `Decimal("%.{scale}f")` using the ordered column's own scale. A bigint column stays a
  float and becomes `Decimal(repr)`. `percent_rank()` goes through `Decimal("%.10f")` and then
  `float()`.
* **Regime distances.** numpy 2.5 is reproduced operation for operation: pairwise summation; the
  default argsort, an unstable introsort transcribed from `aquicksort`/`aheapsort` (a tie-order
  unit test matches numpy); linear quantile with `_lerp`; `linspace`; Python `round()`.
* **Curated decimals.** Chained NAV, coverage, `weight_pct` and share counts go through
  `WideDecimal` (prec 28, half-even).
* **Desk.** `numeric` is read as `::text` then `float()`, because unconstrained numeric can
  exceed 28 digits. CPython 3.12's Neumaier `sum()` is reproduced. `datetime.fromtimestamp` uses
  modf with half-even microseconds.
* **Listings cursor.** CPython's lax `a2b_base64` is ported, and `date.fromisoformat` accepts all
  of 3.12's forms.
* **Error order is FastAPI's.** The auth dependency (401) comes before path/query validation;
  validation errors accumulate path then query in signature order; the desk router checks the
  desk schema before validating `limit`.

Verified beyond the case file: 550 random-symbol factsheets and 19 heavy paths (500-row listings,
every basket's performance and kite payload, a 1,000-row tradebook), all byte-equal.

## R5.14 — The sole tenant is read from the process environment per request, with Python's `int()` (RS4b, 30 Oct 2026; lane labels derivatives R6.D1, sleeves (b), accounts R-acc.2/R-acc.11, portfolio P1) — **one copy since RS4c.0, R6.9** ⚠ UNREVIEWED

**Choice (all five copies agree).** `resolve_sole_user_id` reads `BASKFY_SOLE_USER_ID` from
`os.environ`, not `Settings`. The rules, as Python's:
* the value is stripped, with Python's `int()` underscore rule;
* blank or not a number counts as unset, with a warning;
* unset falls back to the seeded `e2e000000001` account, then a 503 `pipeline-degraded`;
* a principal who is not the tenant gets a 404 with `reason: not-the-sole-tenant`.

The sleeves copy also handles an id beyond i64. Python then refuses every caller with the 404, or
answers 500 where asyncpg cannot bind it; Rust does the same (`SoleId::Huge`). Its verifier
graded 133 cases under that env, all EQUAL. Graded under `1`, `6`, `' +6 '`, `'abc'`, `'1_0_'`,
blank and unset. The sleeves porter also checked the no-tenant path with `--attach` (blank on Python,
`"abc"` on Rust, 25 routes): 25 of 25 EQUAL. One harness run carries one environment, so that
case file is scratch-only.

**Known limit.** Python's `int()` also reads non-ASCII decimal digits (`'١'`). The derivatives and
accounts copies read ASCII only, and fall back to "unset" for such a value. Only an operator
misconfiguring the variable can reach this; no request can.

**How to reverse:** collapse the copies onto the sleeves one, the most complete (R5.10).

## R5.15 — Paths that only have write routes in Python: 404 in four lanes, 405 in portfolio (RS4b, 30 Oct 2026; lane labels sleeves (g), accounts deviation 3, reference watchlist, portfolio P7) — **closed at RS4c.0, R6.11** ⚠ UNREVIEWED

**Context.** Starlette answers 405 when a path matches but the method does not. A path that has
only a POST/PUT/PATCH/DELETE in Python therefore answers 405 to a GET. Rust registers no write
route.

**State.**
* The portfolio lane registers its write-only paths with an empty `MethodRouter`, so a GET gets
  Starlette's 405, decided by routing before the token is read. This covers `/portfolio`,
  `/portfolio/{id}/holdings`, `/portfolio/reconciliation/{id}/resolve`,
  `/portfolios/{id}/rebalance`, `/trades/import`, `/trades/sync-today`,
  `/brokers/{id}/{connect,sync-holdings}`, `/checkout/session`, `/webhooks/razorpay`,
  `/cb/paywall/checkout`, `/cb/public-signup` and `/cb/fees/collect`; 14 cases are graded EQUAL.
* The other lanes leave those paths unrouted, so Rust answers 404 where Python answers 405. This
  covers `GET /swing/scan`, `/twt/scan`, `/vbt/scan`, `/alerts/{id}`, `/managers/{slug}`,
  `/keys/{id}`, `/webhook-endpoints/{id}`, `/cb/investments/mark` and
  `/watchlist/{anything but instruments|discover-preferences}`. Not graded.
* `POST /watchlist/instruments` gets Rust 405 where Python answers 400.
* The reference verifier tried an inert DELETE on `/watchlist/{slug}`. It broke the lane's
  read-only structural test, so it was reverted rather than weakening the test.

**Choice.** Left as is, because wave 4c registers every one of those paths with its real method
and the difference disappears. **Cutover rule until then:** Caddy routes by method and path, and
every non-GET stays on Python.

## R5.16 — The sleeves lane: two JSON spellings, negative zero, pydantic's lax parsing (RS4b lane sleeves, 30 Oct 2026; lane labels (b), verifier fixes) ⚠ UNREVIEWED

* `routes/sleeves/common.rs` holds one value tree `J`/`Obj` with two spellings:
  * `canonical_json` (swing, VBT, `/twt/scan`): decimals unquoted at stored scale, isoformat
    `+00:00`, `NaN`;
  * pydantic JSON (`/twt/today`, `/twt/backtest`, `/sleeves/state`, portfolios, baskets):
    `str(Decimal)` quoted, UTC `Z`, `NaN` → `null`.
* **Negative zero.** `rust_decimal` has none, but `Decimal.quantize` keeps the operand's sign, so a
  negative value rounding to zero is `-0.00`. `J::NegZero(dp)` and `quantize_j` reproduce it in
  journal R and P&L, the by-setup expectancy, distances, `pct_above_dma`, `r_multiple` and
  `unrealised_*`. It was found on seeded data and checked against
  `baskfy_core.swing.journal.summarize`.
* **Lax parsing.** pydantic-core's lax int and bool, and `lax_float_str` for numeric strings in a
  `basket_snapshot` payload: the trimmed text first, then the untrimmed text without underscores.
  Python's `float()` underscore rule is used for string weights.
* **Bigint casts.** Every integer column is cast to `::bigint` in SQL, so sqlx decodes it one way.
  An int that pydantic accepts but i64 cannot hold answers the 500 at the point asyncpg would
  refuse it.

## R5.17 — The sleeves lane refuses with a 503 rather than serve an ungraded number (RS4b lane sleeves, 30 Oct 2026; lane labels (d), (e)) ⚠ UNREVIEWED

None of these is reachable on the bed. Each answers 503 `pipeline-degraded` naming the gap:
* `/swing/journal` when a finished `sw_backtest_run` with stats exists (the backtest card's
  transform is not ported);
* `/portfolios/{id}/allocation` for a screen sleeve that names a screen (needs `run_screen`);
* `/portfolios/{id}/allocation` for a basket sleeve whose live version has constituents (needs
  `portfolio_units.allocate_units`).

`GET /baskets` serves `basket_snapshot` only. With no snapshot, Python builds the basket live
from the desk's config and its newest uploads CSV. On the bed that took 69 s and still returned
200, despite its own 3 s budget. Rust answers the budget's 404 instead, because momentum_scan and
the basket engine arrive with the desk wave. So only `/baskets`' 401/400 cases and eight seeded
snapshot cases are graded. **Cutover:** `/baskets` stays on Python until the desk wave.

## R5.18 — The sleeves lane: small reproductions that look like choices (RS4b lane sleeves, 30 Oct 2026; lane label (f)) ⚠ UNREVIEWED

* `_found_in`/`_funnel_count` treat a JSON bool as an int, as Python's `isinstance` does.
* Sector numbers follow `float(x or 0.0)` and `int(x or 0)`.
* `created_at` uses `fromtimestamp`'s half-even microseconds.
* `planned_ref_price` goes str → f64.
* `datetime.fromisoformat` is reproduced only for the shapes the workers write. Anything else,
  or a naive time, is `null`, where Python would raise or keep it naive. No worker stores one.
* The verifier graded the data paths the bed lacks by **seeding temporary rows into the bed copy**
  and deleting them afterwards (every count back to 0). Those rows were `sw_position`,
  `vb_order`/`vb_position`, `vb`/`tw_backtest_run`, `portfolio_sleeve` and `basket_snapshot`.
  The seeded case files are scratch-only.
* One exploratory case sent `PUT /portfolios/3/sleeves {"sleeves":[]}` to the Python server on
  the bed. It was a no-op (portfolio 3 had no sleeves) and is not in the committed file.

## R5.19 — The derivatives lane's shared code lives in `routes/derivatives/`, declared from `options.rs` (RS4b lane derivatives, 30 Oct 2026; lane label R6.D1) ⚠ UNREVIEWED

**Context.** The lane owns `routes/{options,fno,overlap,backtests}.rs` and may not edit
`routes.rs`, but the four routers share the sole-tenant guard (`curated_tenant.scoped_sole_user_id`
/ `curated_seed.resolve_sole_user_id`), the IST clock, pydantic's lax `int`/`Literal` query
parsing, Python's `json.dumps` escaping and the Redis `GET` Python's `app.state.cache` makes.

**Choice.** `routes/options.rs` declares `#[path = "derivatives/mod.rs"] pub(crate) mod
derivatives;` and the other three reach it as `super::options::derivatives`. The sole tenant is
read from the **process environment per request** (`BASKFY_SOLE_USER_ID`, stripped, Python's
`int()` grouping rule; blank or not a number is "unset" with a warning), then the seeded
`e2e000000001` account, else 503 — exactly `resolve_sole_user_id`, which reads `os.environ`, not
`Settings`. A non-tenant principal gets the 404 with `reason: not-the-sole-tenant`. Redis is read
through `baskfy_providers::ratelimit::RedisScriptBackend` with a one-line Lua `GET` (the API crate
has no `redis` dependency), one lazily connected client per process; a failure reads as "nothing
stored", as every Python caller treats `RedisError`.

**Grading.** `tools/parity/api_diff.py --env BASKFY_SOLE_USER_ID=1` (the bed's owner holds every
options/FO row) for the data paths; the same 176 cases without the variable are all EQUAL too
(the 503 branch), and with a malformed value (`" 1x"`).

**Known overlap.** Another lane's in-progress `routes/reference` carries its own sole-tenant
helper; the integrator should keep one (this one, or theirs) in a shared module.

**How to reverse.** Move `derivatives/` under a shared `routes/common` once `routes.rs` can be
edited.

## R5.20 — Python core modules the options tab needs, ported privately (RS4b lane derivatives, 30 Oct 2026; lane label R6.D2) ⚠ UNREVIEWED

`baskfy_core.options.calendar` (`role`, `next_session`, over the expiry dates `op_expiry` lists —
`options_read._stub`'s one contract per expiry), `options.journal` (`summarize`: the `04` §12
summary, Python's `Decimal` division, `sum(..., Decimal(0))`, `max`'s first-wins on ties and
`min`'s, all through `WideDecimal`), `market_hours_cb.is_nse_session_open`, and
`OptionsConfig.tier3_min_sessions` have no Rust port, so they live in
`routes/derivatives/options_core.rs` (R2.3's rule). `options_read.thresholds()` — 148
`(section, key, str(value), anchor)` rows over `DEFAULT_OPTIONS_CONFIG` — is a **table generated
from the live Python**, because Rust's `options::config` does not expose every field with
Python's `str()`; a DECISIONS-OP change to a threshold must regenerate it (the test pins the
first and last rows and the count). **How to reverse:** move them into `baskfy-core` with goldens
when wave 5 ports `options.calendar`/`options.journal`.

## R5.21 — `GET /overlap` (the overlap read itself) is not ported yet (RS4b lane derivatives, 30 Oct 2026; lane label R6.D3) ⚠ UNREVIEWED

**Context.** The read needs `baskfy_core.candidate_review` (~590 lines: the row's state in words,
`review_key`, `rules_opinion` with its English reasons) and `baskfy_core.catalyst_tags` (~620:
the keyword tagger, `resolve_tag`, `cache_key`), `ScreenDefinition.definition_hash`, the shared
live-marks overlay (`live_prices.live_marks_for_symbols`, Kite), and it **writes to Redis on the
read path** (`SADD catalyst_tag:wanted`, `HSET candidate_review:wanted`). On the bed it returns
real rows (all three sleeves, ~10 screens, 169k catalyst rows), so a partial port would be graded
red.

**Choice.** Not registered; Python keeps serving `GET /api/v1/overlap` (Caddy keeps that one route
on Python). Ported: `GET /overlap/tags/export`, `GET /overlap/reviews/export`,
`GET /overlap/catalyst-scan`. `validate_literal` (the `scope` parameter) is in place and tested.
**Next:** port `candidate_review` and `catalyst_tags` into `baskfy-core` with goldens first, then
the read; the Redis writes need a decision (a write on a read path).

## R5.22 — A backtest still in flight answers its event stream as Python's no-cache branch (RS4b lane derivatives, 30 Oct 2026; lane label R6.D4) ⚠ UNREVIEWED

**Context.** `GET /backtests/{id}/events` streams Redis pub/sub frames for up to 30 minutes for a
queued or running run. `baskfy-api` has neither a pub/sub client nor a streaming-body dependency
(`futures-util` / `tokio-stream`; axum's `Body::from_stream` needs a `Stream`).

**Choice.** A finished run (`done`/`failed`) is byte-identical to Python: `event: progress` then
`event: end` with the frame synthesised from the row (`json.dumps(sort_keys=True)`, ASCII-escaped),
`text/event-stream; charset=utf-8`, `Cache-Control: no-store`, `X-Accel-Buffering: no`. A run in
flight answers the same two frames with the stored (or synthesised) frame — what Python itself
does when it has no Redis (`terminal or cache is None`); the web client reconnects on its own.
**Proposed for the integrator:** pin `tokio-stream` (or `futures-util`) and a pub/sub connection,
then port the live loop (heartbeat every 15 s, 30-minute cap). Until then keep `/events` of
running runs on Python if the progress bar matters.

## R5.23 — The derivatives lane's reads: what is reproduced and how (RS4b lane derivatives, 30 Oct 2026; lane label R6.D5) ⚠ UNREVIEWED

* **Writes are not registered** (R5.2): `POST`/`DELETE /options/event-day`, `PATCH
  /options/config`, `PATCH /fno/config`, `POST`/`DELETE /backtests`, the overlap corrections,
  labels and `POST /catalyst-scan`. Python serves them. No route here places, modifies or cancels
  an order; the execution flags are only reported.
* **Bodies.** The options/fno routers answer `model_dump_json()` (pydantic's own JSON), whose float
  spelling (`1e-5` → `0.00001`) differs from `repr` but parses equal — the harness compares parsed
  values, and every number on these pages is a `Decimal` string or an int. The overlap routes
  answer `canonical_json` (decimals unquoted in fixed notation, insertion order).
* **Stored JSON re-validated.** `BacktestOut.config` is `BacktestConfig.model_validate(row.config)`
  dumped again: Rust re-dumps the stored `jsonb` through the model's field list (order, defaults,
  `Decimal` re-printed as `str(Decimal)`, unknown keys → the 500 Python raises). The equity/
  drawdown/fragility points and the CSV artefacts (`csv.DictReader`, `_blanks_to_none`) go through
  the same closed-model coercion.
* **Order.** Enum columns (`fo_sleeve`, `op_sleeve`) are ordered by the enum, as SQLAlchemy orders
  them; `skips_by_reason` keeps the order of an unordered `SELECT op_session.*`, the statement
  SQLAlchemy sends, so both read the heap in the same order (graded on 37 skipped sessions).
* **Validation order** is FastAPI's: the principal (401) before the query (400), the query before
  the handler's tenant check (404), entitlement (402) and sleeve check (404).
* **Clock.** `today`, `market_open`, `live`, `stale` and the export link's `expires` are wall-clock
  reads, as in Python; two servers asked within the same minute agree (no case needed a mask).

## R5.24 — The portfolio lane's core logic lives privately under `routes/portfolio/` (RS4b lane portfolio, 30 Oct 2026; lane labels api-portfolio P1, P12) ⚠ UNREVIEWED

Several Python core modules have no Rust port in `baskfy-core`: `allocation_ledger`,
`cash_ledger` (with `curated_accounting.xirr`), `portfolio_nav`, `reconciliation`,
`grouping_suggestions`, `portfolio_costs` (with `costs.order_cost`), the tradebook subset and
`portfolio_graph`. They are ported line for line as crate-private modules under
`routes/portfolio/`, mounted with `#[path]` from `portfolios.rs`, because `routes.rs` was not the
lane's to edit. `RebalanceOut.model_validate` is there too. **How to reverse:** move them into
`baskfy-core` together with goldens (R2.3's rule), which is wave-5 material.

## R5.25 — A decimal with adjustable precision for NAV chain-linking and XIRR (RS4b lane portfolio, 30 Oct 2026; lane label api-portfolio P2) ⚠ UNREVIEWED

`WideDecimal` is fixed at precision 28. `portfolio_nav` chain-links under
`localcontext(prec=60)`, and the XIRR solver raises a Decimal to non-integer powers.
`routes/portfolio/pydec.rs::Dec` is WideDecimal's algorithm with the precision as a parameter.
Its `pow` is `exp(y·ln x)` computed with 20 extra digits and rounded half-even to 28, which
matches libmpdec ("correctly rounded in almost all cases"). Above Emax it errors, giving Python's
`decimal.Overflow` 500. Graded against the live Python, all equal:
* 60 random powers, digit for digit;
* 40 random XIRR solves, including 8 overflows;
* 30 random NAV series (daily P&L, drawdown, maximum drawdown).

That data lives in `routes/portfolio/golden.rs`. **Risk:** "almost all cases" means a rare
double-rounding difference remains possible; move it into `baskfy-core` with a dumper.

## R5.26 — `GET /invoices/{invoice_number:path}/pdf` is registered for 1–8 path segments (RS4b lane portfolio, 30 Oct 2026; lane label api-portfolio P4, rewritten by the verifier) ⚠ UNREVIEWED

Starlette's `:path` converter matches `.*` in the middle of a path, and axum cannot express that.
The route is registered for invoice numbers of 1 to 8 segments; real numbers have 3. A number
containing `\n` is answered Starlette's plain 404 before the token check, because Python's `.`
never matches a newline. **What differs** (the porter's first description was wrong; empty
segments and `%2F` are EQUAL):
* more than 8 segments: Python's 404 "No invoice with id …", Rust's generic 404 "Not Found";
* a `%0A`-containing number with a trailing slash: Python 404, Rust 307 (the slash probe matches);
* the same number with a non-GET method: Python 404, Rust 405.

None is reachable with a real invoice number. **How to reverse:** a mid-path catch-all in the
router, or a change to the probe layer in `app.rs`.

## R5.27 — `GET /brokers/callback` serves only its refusals (RS4b lane portfolio, 30 Oct 2026; lane label api-portfolio P5) ⚠ UNREVIEWED

Rust never mints an OAuth state, because connect is a POST and is not ported, so every state it
sees is "Invalid or expired OAuth state.". The refusals are EQUAL: validation, 401, the
sole-tenant 503/404 and the sign-off gate (17 cases under three env values). The success path
writes the token and syncs holdings, so **the route stays on Python until wave 4c**, especially
where `BASKFY_BROKER_OAUTH_STATE_PATH` shares state between processes.

## R5.28 — Python-side defects found by the port and reproduced on purpose (RS4b, 30 Oct 2026; lane labels api-portfolio P6, reference verifier) — **decided: both fixed at RS4c.0 (R6.1, ⚠ UNREVIEWED), committed at RS4c**

The strangler rule is "reproduce exactly", so Rust answers both of these the way Python does,
byte for byte. Each should be **fixed in Python first, then in Rust**, with the case files'
expected answers changing in the same commit:
1. **`GET /trades` leaks other tenants' trade dates.** SQLAlchemy compiles the bounds aggregate
   against the table instead of the subquery: `FROM (scope) AS anon_1, broker_trade` (confirmed by
   compiling it). So `total` = (caller's rows) × (every user's rows), and `first_trade_on` /
   `last_trade_on` span every user's trades. The query is parameterised, so no injection, but it
   is a tenant-isolation leak of dates and a wrong count.
2. **`GET /instruments/{symbol}/rank-history` has no auth dependency.** An anonymous caller who
   knows a private screen's `public_id` can read its rank history. The case
   `rank-history-other-tenant` covers it.

**Closed at RS4c.0 (R6.1):** both are fixed, in Python first and then in Rust; the cases now
grade the fixed behaviour.

## R5.29 — Portfolio lane: parameters, ordering and rounding (RS4b lane portfolio, 30 Oct 2026; lane labels api-portfolio P8, P9, P10) ⚠ UNREVIEWED

* **P8 — parameters.** Parameters follow pydantic's lax int: trimmed, `1_000` and `12.0`
  accepted, `12.` refused, big values kept. A value beyond bigint is the 500 at the point the
  query runs, where Python raises asyncpg's unhandled `DataError`. Cursors use Python's `int()`.
* **P9 — tradebook ordering.** Python sorts with a naive midnight when `executed_at` is missing.
  When one instrument has both kinds on the same day, Python raises `TypeError` (500), and Rust
  answers a 500 too. This approximates exactly when Python would compare the two (8 mixed cases
  EQUAL).
* **P10 — charge amounts.** Charge amounts are floats rounded with `Decimal(str(round(x, 2)))`.
  `round` is reproduced on the float's exact decimal expansion, half-even, so
  `round(0.125, 2)` = 0.12.

## R5.30 — Lanes graded data paths the bed lacks on temporary data (RS4b, 30 Oct 2026; lane labels api-portfolio P11, sleeves/accounts/derivatives verifiers) ⚠ UNREVIEWED

The bed copy has empty tables for much of what the routes serve, such as cash flows,
reconciliation items, payments, investments, alerts, keys, webhooks, positions and done
backtests. The lanes graded those paths in two ways, and **neither result can be committed**,
because the harness grades against the bed as it is:
* **Side databases.** The portfolio porter and its verifier each built a temporary database inside
  the bed container (`baskfy_pf_lane`, `baskfy_rv_pf`) from a schema dump plus synthetic rows.
  63 + 347 cases were EQUAL. Both databases were dropped.
* **Seeded rows in the bed copy.** The sleeves, accounts and derivatives verifiers inserted
  temporary rows into the bed copy itself (ids ≥ 990001, or user 6's rows) and deleted them
  afterwards, reporting the counts back at their earlier values. Results: accounts 292 cases;
  derivatives 303 cases, under four env values, plus a local archive dir; sleeves' seeded files.
  Everything was EQUAL except R5.3's big-integer class, and the derivatives backtest cursor,
  which was fixed.

This was the bed (a copy), never the live database. **Recommendation for wave 4c:** commit a
**fixture seed** (an SQL file applied to a fresh bed copy), so these paths are graded by
committed cases instead of scratch ones.

## R5.31 — Accounts lane: private helpers, exact decimals, trending (RS4b lane accounts, 30 Oct 2026; lane labels R-acc.1, R-acc.4, R-acc.8) ⚠ UNREVIEWED

* The lane's shared helpers live in `routes/accounts/`, declared from `screens.rs` with `#[path]`.
  `admin.rs` pulls in `admin_providers.rs` and `admin_resync.rs` the same way (R5.10 for the
  collapse).
* **Exact decimals.** The snapshot math (`money` = half-up to 2), the fee and net-worth totals,
  `moved_pct` and the detail's weight go through `WideDecimal`. The weight is a 28-digit quotient
  serialised with `str()` (the R1.3 class).
* **Trending.** `baskfy_core.curated_trending` (pure, no goldens) is ported as
  `accounts/trending.rs`. The `curated_metrics` return-convention constants are copied by hand.
  Move it into `baskfy-core` once a dumper grades it.
* **Verifier fix: error order.** An `investment_id` beyond bigint answered the 500 before the
  sole-tenant gate; Python resolves the tenant first. It now carries `Option<i64>` and fails after
  the tenant check (68 ordering cases EQUAL under three env values).

## R5.32 — `GET /screens/{id}/csv` compiles the export query exactly as SQLAlchemy + asyncpg do (RS4b lane accounts, 30 Oct 2026; lane label R-acc.5) ⚠ UNREVIEWED

* **The query.** `baskfy_core::build_export_query` is compiled, then SQLAlchemy's expanding
  `IN (__[POSTCOMPILE_x])` is expanded into one `$n::VARCHAR` per element, renumbering every later
  `$n`, as asyncpg does. Decimals are bound as their `str()` against the `::NUMERIC` casts.
* **The columns.** `EXPORT_COLUMNS` and `FACTOR_COLUMN_MAP` are copied by hand, and a test checks
  the header against the committed reference CSV.
* **The body.** It is built in full rather than streamed (same bytes, at most 4,000 rows).

All 22 cases are byte-equal, including real exports and `as_of` snapping.

## R5.33 — `/admin/providers` and `/admin/resync` are graded under conditions that keep them off the network (RS4b lane accounts, 30 Oct 2026; lane labels R-acc.6, R-acc.7) ⚠ UNREVIEWED

* **`/admin/providers`** is graded with `BASKFY_FIXTURE_DIR` pointing at an empty directory. With
  the default directory, Python reports the fixture provider available with 8 `serves_now`
  entries; Rust reports it unavailable because it cannot read Parquet (R4.12). **This is a real
  field difference**, and it disappears when polars' `parquet` feature is pinned for
  `baskfy-providers`.
* **`/admin/resync`** is ported in full (the publication probe through
  `bhavcopy_publication_check`, the token-store arithmetic). It is graded only with `days=30` and
  `days=1`, because a wider window holds inferred holidays (the bed has 83), and Python would then
  probe NSE live, which the bed forbids.

## R5.34 — Accounts lane: unordered reads, `/me/export`, bind limits (RS4b lane accounts, 30 Oct 2026; lane labels R-acc.9, R-acc.10, R-acc.12) ⚠ UNREVIEWED

* **Unordered reads.** Reads Python makes without `ORDER BY` stay without one: the export lists,
  the investment list, holdings and unordered batches. Both sides see heap order.
* **`GET /me/export`** is added to `auth.rs` (read-only). It reports the stored `is_staff` (the
  export does not apply the allowlist, unlike `GET /me`, R5.7), and its datetimes use isoformat
  with `+00:00`.
* **Bind limits.** An integer that cannot be bound as bigint/int4 (`run_id`, `investment_id`,
  version numbers, the cursor) answers the 500 catch-all, which is asyncpg's bind error in Python.

## R5.35 — Derivatives: the backtest cursor reads Unicode digits as Python does (RS4b lane derivatives verifier, 30 Oct 2026) ⚠ UNREVIEWED

Python's `_page` runs `cursor.isdigit()` and then `int(cursor)`, so:
* `'١'` (Arabic-Indic one) is offset 1;
* `'²'` passes `isdigit()` but makes `int()` raise, a 500.

Rust read ASCII only, so it gave 404 in both. `cursor_offset()` now uses two tables generated from
CPython 3.12 (Unicode 15.0.0): 68 runs of decimal digits, and 20 ranges that are digits but not
decimal. **Regenerate them** if the Python runtime moves to a newer Unicode version.

## R5.36 — Known residual differences, none reachable on the bed's data (RS4b, 30 Oct 2026) ⚠ UNREVIEWED

Recorded so a cutover can decide route by route; each is outside every committed case.
1. **JSON integers beyond u64** (R5.3) — **closed at RS4c.0 (R6.10).** `OrderedJson` had no big-integer variant, so a stored
   `1000000000000000000000` (e.g. a metric float 1e21 that Postgres keeps as numeric) is echoed
   `1e+21`, where Python echoes the integer. It reaches every raw-echoed `jsonb`: backtest metrics
   and params, scan detail and funnel, `op_scan`/`fo_scan` numbers. Three verifiers hit it on
   seeded data. **Fix next:** an arbitrary-digit integer variant in `baskfy-db::json`.
2. **Desk JSON.** An integer literal beyond i64 is a 500, as is a `NaN`/`Infinity` literal in
   `holdings_json`/`reasons_json` (Python's `json` accepts both). `float()` of a holdings string
   is approximated by an f64 parse.
3. **Starlette's `$` matches before a trailing newline.** `GET /api/v1/plans%0A` is Python 200,
   Rust 404; `/portfolio/overview%0A` is Python 200, Rust 400. Python also drops CR/LF from a
   problem's `instance`, and Rust keeps them. This is foundation routing, not graded.
4. **`GET /backtests/{id}/events` of a run in flight** (R5.22): two frames, not a 30-minute
   stream.
5. **`GET /overlap`** (R5.21): Python serves it.
6. **The write-only paths** (R5.15): 404, not 405 — **closed at RS4c.0 (R6.11).**
7. **`/metrics`, `/api/v1/openapi.json`, `/api/v1/docs`**: not ported, so Python serves them.
   The contract tests generated from `openapi.json` (README) wait for it.

## R6.1 — R5.28's two tenant-isolation defects are fixed, in Python first, then in Rust (RS4c.0, 30 Oct 2026; decided by the lead under the autonomy charter — the stricter security boundary) ⚠ UNREVIEWED

**Context.** R5.28 recorded two Python defects the port reproduced byte for byte: `GET /trades`'s
bounds aggregate cross-joined every account's trades, and `GET /instruments/{symbol}/rank-history`
had no principal at all. The strangler rule is "reproduce exactly", but a tenant-isolation leak is
not a behaviour worth preserving, and the tie-break is the stricter security boundary.

**Choice.**
* **`GET /trades`** (`routers/trades.py`): the aggregate reads the scoped subquery's own columns
  (`min(anon_1.trade_date)`), so `total`, `first_trade_on` and `last_trade_on` are the caller's
  alone. Rust (`routes/trades.rs`) sends the statement SQLAlchemy now compiles.
* **`GET /instruments/{symbol}/rank-history`** (`routers/instruments.py`): takes `PrincipalDep`, as
  every read of a screen does, and applies the screen's own rule — a system screen
  (`user_id IS NULL`) is everyone's, a private one its owner's only, and not-yours is the same
  `404 not-found` as no-such-screen. `routers/screens.py`'s private `_is_visible` became the public
  `is_visible` (same body) so both routers use one predicate; Rust's `screens::visible_to` is the
  same. The explanation is a **code comment, not the docstring**: the docstring is the OpenAPI
  description, and `packages/api-client/openapi.json` (and the generated client) stay unchanged.
* **Python tests assert the spec** (house rule 2): `test_api_trades.py` gains
  `test_the_list_counts_and_dates_only_the_callers_own_trades` (two accounts, the bounds and the
  symbol filter) and `test_an_account_with_no_trades_sees_none_of_anyone_elses`;
  `test_rank_history_visibility.py` asserts owner 200, anonymous 404, another account 404, the 404
  indistinguishable from a missing screen, and a system screen readable by anyone. Both specs were
  run against the unfixed code first and failed there. The affected suites (`test_api_trades`,
  `test_api_instruments`, `test_api_screens`, `test_api_artifacts`, `test_audit_auth_gates`,
  `test_instrument_appearances`, `test_api_auth`, `test_api_errors`, `test_broker_trades`, the new
  file) are green on a scratch database created for the run in the bed container (`rs_wc_pytest`,
  migrated with Alembic, dropped afterwards) — never the live database.
* **Case files.** `reference.json`'s rank-history cases on the owner's private screens now run as
  the owner (they were anonymous); four cases are added (anonymous and another account get the
  404, user 6 reads its own screen, a system screen stays readable by another account). The trades
  cases are unchanged in shape; their expected answers changed with the fix (the bed's owner now
  gets `total` 10,222, not 10,222²).

**How to reverse.** Revert the two Python hunks and the two Rust ones; the cases follow.

## R6.2 — A read-write pool, reachable only by write routes, one transaction per request (RS4c.0, 30 Oct 2026) ⚠ UNREVIEWED

**Context.** R5.2 made every connection read-only. Wave 4c ports the 94 write routes, and the
brief requires that a GET handler **cannot** obtain a pool that writes.

**Choice.**
* `baskfy_db::WriteDb`: `create_engine`'s sizing again (10 + 10, 5 s checkout, 1800 s recycle,
  pre-ping, statement cache), **without** `default_transaction_read_only`, `application_name =
  baskfy-rs-write`. It has no `pool()` accessor and no way to run a statement outside a
  transaction: `begin()` is the only door. `Db` (read-only) is unchanged and still serves every
  read, including the reads inside a write route that Python makes after the flush.
* `AppState.write` is an opaque `WriteAccess`; only `session.rs` reads it. The route layer
  `session::write_access` (innermost on `/api/v1`, after the principal and the rate limit) inserts
  a `WritePool` extension **for POST, PUT, PATCH and DELETE only**. A GET handler that asks for it
  gets axum's missing-extension 500 and never runs (`session.rs` unit test); a source test
  (`tests/write_access.rs`) proves no file outside `state.rs`/`session.rs` names the pool's
  internals and every handler taking a `WritePool` is mounted on a write verb.
* `session::run(pool, body)` is `db.get_session`: begin, run, **commit only on `Ok`**, roll back
  on any `Err` (a `Problem` is a raised exception in Python, which unwinds through the session
  dependency's `rollback()`). Graded by the writes harness, rows and all (R6.4 shows why it
  matters).
* `GET /me` still does not persist the staff-allowlist grant (R5.7): a GET never writes, by
  construction now.

**Known cost.** A process now holds two pools, so its peak connection count doubles (40, not 20,
per API process). Nothing serves both from Rust yet. **How to reverse / tune:** size the write
pool down (it serves a small fraction of requests), or route reads inside write requests through
the write transaction and drop the second pool.

## R6.3 — The session routes are ported; Google's verifier is not (RS4c.0, 30 Oct 2026) ⚠ UNREVIEWED

`PATCH /me`, `DELETE /me`, `POST /me/restore`, `POST /auth/refresh` and `POST /auth/logout`
(`routes/auth_writes.rs`) are ported in full with `auth_service`'s logic: `issue_session` (a
refresh row in its family, the access token stamped with the epoch), `rotate_refresh` (reuse
revokes the family, expiry, a vanished account, the rotation and `replaced_by_id`),
`revoke_one`, `revoke_all_for_user` (**`session_epoch + 1`, then every live refresh token** — the
"sign out everywhere" Python performs on `DELETE /me` and on restore; Python has no separate
logout-all route), `request_deletion`/`cancel_deletion`, the restore code
(`jwt.encode({sub, purpose, iat, exp})`, verified with `require=["exp","sub","purpose"]`), the
double-submit CSRF check, Starlette's `Set-Cookie` spelling for `set_auth_cookies` and
`clear_auth_cookies`, and the deletion mail. `writes_foundation.json`: **74 of 74 responses
EQUAL, side effects EQUAL** (tables, sequences, Redis, mail).

**`POST /auth/google`** is ported up to the verifier: body validation, the "not configured" 401
(the bed's state), and the uniform 401 sentence. **Google's RS256 verification is not ported**: it
needs the JWKS fetched from Google and an RSA verifier (`ring` is in the lock through rustls but
not a declared dependency). With `BASKFY_GOOGLE_CLIENT_ID` set, Rust refuses every token with the
same uniform 401 and logs why. **Cutover gate:** `/auth/google` stays on Python wherever sign-in is
configured. `link_google_identity` (account creation, adoption, consent records) is therefore not
ported either. **How to reverse:** pin `ring` (RSA PKCS#1 v1.5 SHA-256 over the JWKS `n`/`e`),
fetch the JWKS through `baskfy-providers`' network chokepoint with PyJWKClient's 600 s cache,
port `link_google_identity`, and grade it with a recorded JWKS.

## R6.4 — Python rolls back the refresh-token reuse revocation (found by the writes harness; reproduced on purpose) ⚠ UNREVIEWED — **for Maulik**

**Found.** `rotate_refresh` flushes its security writes and *then* raises: the family revocation
on reuse (`revoke_family(..., "reuse")`), the `expired` stamp, and the family revocation for a
vanished account. The `TokenReused`/`InvalidCredentials` it raises becomes a 401, and the
exception unwinds through `get_session`, which **rolls the transaction back**. So a replayed
refresh token is refused, but its family is **not** revoked: the thief's still-live successor
token keeps working. The harness shows it (`refresh-revoked-user3`, `refresh-owner-reuse`: no row
changes on either side). The same unwinding drops `clear_auth_cookies(response, ...)` on every
refresh refusal (the 401 is a fresh response), which is harmless.

**Choice.** Reproduced byte for byte (the strangler rule), with the session semantics that cause
it (R6.2). **Fix in Python first:** commit the revocation before raising (a separate session, or
`await session.commit()` before `raise`), then here.

## R6.5 — The writes harness: lane templates, three clones per run, side effects compared (RS4c.0, 30 Oct 2026) ⚠ UNREVIEWED

`tools/parity/api_diff.py --writes --lane L --smtp-port P` (mechanics in `api_writes.py`):
* **Clones.** `CREATE DATABASE … TEMPLATE baskfy` needs the template to have no session, and the
  bed always has one (TimescaleDB's scheduler, plus any lane's API servers). So the lane clones
  `baskfy` **once** into `rsw_tpl_<lane>` at a quiet moment (it waits while other sessions are
  connected and ends only the TimescaleDB scheduler of `baskfy`, a system worker the launcher
  restarts at once — no data is touched), unschedules every TimescaleDB job in that copy (no
  aggregate refresh or compression mid-scenario, no telemetry call), and closes it to
  connections. Each run then clones three databases from it with `STRATEGY FILE_COPY` (about 3 s
  each for the 3 GB bed): `…_base`, `…_py`, `…_rs`, all dropped at the end (`--keep-dbs` keeps
  them). The brief's "clone from `baskfy`" is honoured once per lane; per-run clones come from the
  lane copy. Every name created or dropped must start `rsw_`; `baskfy` is refused as a target; the
  container must publish 127.0.0.1:55432.
* **Tables.** After both servers stop (their backends exit and flush their counters), every
  `public` table whose insert/update/delete count moved on either side, plus the case file's
  `"tables"`, is diffed against base by primary key; each side's change set must be identical. A
  masked column compares as `<null>`/`<unchanged>`/`<changed>`/`<set>`, so a server-stamped value
  still has to be stamped by both. Sequences' `last_value` are compared too.
* **Redis.** One database index per side (`--redis-db-py 12`, `--redis-db-rs 13`; never 0; flushed
  before and after). Rate-limit buckets are compared by presence (their values hold a clock);
  Celery messages have their task ids normalised.
* **Mail.** One SMTP sink per lane; a message belongs to the case and side whose request was in
  flight. Envelope, headers and each part's decoded text are compared; line endings are
  normalised (a 7bit/QP part arrives CRLF, a base64 part decodes to what was encoded).
* **Scenarios.** `"capture"` (`json:/ptr`, `cookie:NAME`, `header:NAME`, `mail:REGEX`) is per side;
  `${name}` substitutes in path, query (raw or URL-encoded), headers and body. `"seed_sql"` runs in
  all three clones before the servers start, so a fixture the bed lacks is baseline, never diff.
  `"cookie-attr:expires"` masks a deleted cookie's wall-clock `expires`.
* GET mode skips `"mode": "writes"` files; writes mode accepts nothing else. `--dump-dir` writes
  each side's raw answer per case.

**Webhooks.** None of the foundation routes sends one; outbound webhook delivery is the worker's
(Celery, compared through Redis). An HTTP sink is left for the lane that ports a route that calls
out synchronously.

## R6.6 — Mail: console and plain SMTP ported; STARTTLS/AUTH and Resend are not (RS4c.0, 30 Oct 2026) ⚠ UNREVIEWED

`mailer.rs` ports `Mailer` (never raises; a failure is logged `email delivery failed` and reported
`false`), `ConsoleTransport`, and `SmtpTransport` for an unencrypted, unauthenticated relay: a
`multipart/alternative` message, text first, each part base64 with `set_content`'s trailing
newline, `MAIL FROM` the address inside `BASKFY_EMAIL_FROM`. Graded against Python's through the
harness's sink. **Not ported:** `BASKFY_SMTP_STARTTLS`/a username (TLS and AUTH — what SES needs)
and `resend` (an HTTPS API); both are reported as a failed send where Python would deliver.
**Cutover gate:** a route that mails (`DELETE /me` today) stays on Python on a deployment
configured that way. **How to reverse:** a TLS client (rustls is already in the tree) and
`AUTH LOGIN`; Resend through `baskfy-providers`' network chokepoint.

## R6.7 — Request bodies are read as FastAPI 0.13x reads them; three edges approximated (RS4c.0, 30 Oct 2026) ⚠ UNREVIEWED

`body.rs` reproduces FastAPI's body read and pydantic's model validation for the write routes:
`strict_content_type` (a body with **no** `Content-Type` is not parsed — this FastAPI defaults it
on; graded), `application/*+json`, a JSON `null` read as the field missing, `json.detect_encoding`
(UTF-8, UTF-8 BOM, UTF-16/32), CPython's C scanner with its exact error **positions** (26 cases
pinned from CPython; the harness grades more), `NaN`/`Infinity`, Python's unbounded ints, the
`int_max_str_digits` and recursion failures as Starlette's `400 There was an error parsing the
body`, and pydantic's errors in field order then extras (`model_attributes_type`, `missing`,
`string_type`, `string_too_short`/`_long` with pydantic's singular/plural). `email_address.rs` is
pydantic's `EmailStr` (the pretty `Name <addr>` form, then email-validator 2.3) — **4,064 inputs
dumped from the live Python, every ASCII one exact** (`tests/email_vectors.rs`).

**Approximations, recorded:** (1) any non-ASCII email, and an ASCII domain with a Punycode
(`xn--`) label, are refused with "…characters this deployment does not validate yet." where
Python runs IDNA/UTS-46 and may accept them (`é@b.co`); both routes compare the address with the
account's own, so such an input is refused either way, with a different body; an invalid
`IPv6:` literal gets the same sentence instead of `ipaddress`'s. (2) A lone surrogate escape
(`"\ud800"`) is the 500 catch-all: Python keeps it in a `str`, which no Rust `String` can hold.
(3) The nesting depth at which Python's decoder raises `RecursionError` is approximated (980).

## R6.8 — `Idempotency-Key` is ported, ready for the routes that create (RS4c.0, 30 Oct 2026) ⚠ UNREVIEWED

`idempotency.rs` ports `reserve`/`release`/`remember`/`replay` with Python's key
(`idempotency:api:<scope>:<key[:255]>`), `PENDING`, TTLs and the compare-and-delete release
script verbatim; Redis failures degrade to no idempotency, logged. `reserve` runs `SET NX EX` and
the `GET` in **one** script, so the "expired between the two calls" race resolves to the key's
state at that instant instead of reserving — the three outcomes are Python's. No route ported so
far takes the header (the session routes do not in Python either); `POST /screens` and
`/screens/{id}/duplicate` will. `tests/idempotency.rs` runs the contract on the bed's Redis
(database 14, its own keys deleted) and the no-Redis degradation. The process-wide Redis client
moved from the derivatives lane into `cache.rs` (R5.19's copy) and is now keyed by URL.

## R6.9 — R5.10's collapse: one `routes/common.rs` (RS4c.0, 30 Oct 2026) ⚠ UNREVIEWED

The five lanes' copies are now one module, and the lanes' names delegate to it (no call site
moved): pydantic's lax `int`/`bool` (`lax_int`, `lax_bool`), Python's `int()` (`python_int`, with
R5.35's Unicode decimal-digit tables, now shared with the backtest cursor), the sole tenant
(`resolve_sole_tenant`, `scoped_sole_user_id`), the IST clock (`ist`, `now_ist`, `today_ist`) and
`latest_published_date`. Where the copies differed, the most complete won, which changes two
unreachable-on-the-bed answers in four lanes toward Python's: a `BASKFY_SOLE_USER_ID` beyond
`i64` now refuses every caller (404) or answers the 500 asyncpg would, instead of reading as
"unset" (R5.14's sleeves rule); and non-ASCII decimal digits in it are read, as Python's `int()`
reads them (R5.14's known limit, closed). The lanes' unit tests for the removed copies moved,
unchanged, into `common.rs`. `resolve_as_of` (reference vs screens) differs in shape and is left.
Both GET runs: **2,285 of 2,285 EQUAL**.

## R6.10 — R5.36.1 closed: JSON integers of any size stay exact (RS4c.0, 30 Oct 2026) ⚠ UNREVIEWED

`OrderedJson` gains `BigInt(String)` (canonical decimal digits) and `OrderedJson::parse` is now a
hand-written parser (Python `json.loads`' grammar, key order, last-value-wins duplicates, ints of
any size); serde's `Deserialize` reads a `RawValue` and goes through the same parser, so a `jsonb`
column (sqlx's `Json<T>`) and any other caller agree. A stored `1000000000000000000000` now echoes
as that integer, never `1e+21`. Every exhaustive match in the API handles the variant as Python's
`int` would (`repr`, truthiness, `float()`, pydantic's `int`, the JWT claim checks). Still
refused, unchanged: `NaN`/`Infinity` tokens and lone surrogates in stored JSON (R5.36.2).

## R6.11 — R5.15 closed: every write-only Python path answers 405, in every lane (RS4c.0, 30 Oct 2026) ⚠ UNREVIEWED

Enumerating the live app's routes found **46** paths Python serves only with write methods and
Rust did not route (beyond the portfolio lane's 14). **37** are registered now with
`common::write_only()` (an empty `MethodRouter`: Starlette's 405 for every method, decided by
routing before the token), in the router that owns each in Python; **4** session paths
(`/auth/google`, `/auth/refresh`, `/auth/logout`, `/me/restore`) got their real handlers (R6.3).
**5** are deliberately not registered — `/screens/preview`, `/screens/explain`,
`/screens/selection`, `/portfolios/import-csv` and `/cb/investments/mark` — because a Python GET
route also matches each (`GET /screens/{public_id}`, `GET /portfolios/{portfolio_id}`,
`GET /cb/investments/{investment_id}`), so Starlette serves that GET, and so does the Rust GET
route; a static empty route would shadow it in axum. `foundation.json` gains 168 cases (GET as
anonymous and as owner, HEAD, and the slash redirect for each of the 42 non-session paths), all
EQUAL in both GET runs.


## R6.12 — Lane writes-screens: pydantic's body validation for typed models (RS4c lane writes-screens, 30 Oct 2026) [lane label R-ws.1] ⚠ UNREVIEWED

**Context.** `body.rs` validates the string-only models of the session routes. The lane's 30
Python write routes declare `int`, `Decimal`, `bool`, `Literal`, `date`, `list[Model]`,
`dict[str, int | Decimal]`, nested models and `ScreenDefinition`, mixed `extra="forbid"`
(`schemas._In`) and `extra="ignore"` (plain `BaseModel`).

**Choice.** A private validator, `routes/screens/body_model.rs` (shared by the lane's routers),
written against probes of the live pydantic 2.13 / FastAPI 0.141 rather than from documentation:
lax Python-mode rules (`int` takes a bool, an integral float and pydantic's lax string; `Decimal`
takes int/float/str — `Decimal(str(f))`, Unicode digits and `_` — never a bool, never a non-finite
value; `bool` takes 0/1, 0.0/1.0 and the lax string set), constraint messages with the bound's
`str()`, `too_short` checked only when every item validated, nested non-objects as
`model_attributes_type` (FastAPI's `from_attributes=True` propagates, so the core's
`ScreenDefinition` `model_type` errors are renamed to it), path-parameter errors before body
errors, and the body read **before** any dependency (a malformed body is a 400 even when
anonymous — `get_request_handler` parses before `solve_dependencies`). `Decimal`s are carried as
`WideDecimal` so a response echoes `str(Decimal)` exactly (`"1E+3"`, `"0.33333"`), and are bound as
text cast to `numeric`, so the column rounds as it rounds asyncpg's value.

**Approximations, recorded.** A float other than 0.0/1.0 for a `bool` is reported `bool_type`
(pydantic may say `bool_parsing` for some); integers beyond `i128` saturate (Python's are
unbounded: a `dict[str, int]` holding one would size a plan differently — both answers are
nonsense, neither is reachable from the web app).

**How to reverse.** Fold the validator into `body.rs` when a second lane needs it (the sleeves and
api-keys lanes wrote their own copies in parallel — `sleeves/pyd.rs`, `api_keys/model.rs`; the
integrator should collapse the three as R6.9 collapsed the GET helpers).

## R6.13 — Lane writes-screens: the routes that run the screener are not ported (cutover gate) [lane label R-ws.2] ⚠ UNREVIEWED

`POST /screens/preview`, `/screens/explain`, `/screens/selection`, `POST /screens/{public_id}/run`,
`POST /cb/baskets/from-screen` and `POST /cb/baskets/{slug}/recut` all execute a screen
(`baskfy_api.screener.execute_screen` / `run_screen` / `rank_definition`: the compiled query, the
ranking-engine path, the desk-score columns, the Redis screen cache with its single-flight,
`screen_provenance`, `record_run`, `screen_ranking.explain_symbol`, `ranking_selection`,
`basket_sizing`). No Rust module executes a screen yet (the core builds the SQL and the payload;
nothing runs it), and porting that service half-way would answer with numbers Python would not give.
**They stay on Python**: in Rust, `/screens/{public_id}/run`, `/cb/baskets/from-screen` and
`/cb/baskets/{slug}/recut` remain R6.11's empty 405 routes, and `preview`/`explain`/`selection`
stay unregistered (their GET falls to `GET /screens/{public_id}`, as in Python). **Cutover gate**
for all six. **How to reverse:** port `screener.py`'s execution half (and `screen_ranking.py`,
`baskfy_core.ranking_selection`, `basket_sizing` with goldens first), then these routes.

## R6.14 — Lane writes-screens: the drift routes' desk fallback is always empty in Python, so the port skips the read [lane label R-ws.3] ⚠ UNREVIEWED — **for Maulik**

`routers/curated_drift._desk_holdings` reads `desk.snapshots.holdings_json` through a bare
`text()` query. The column is `text` on the bed, and asyncpg returns an untyped `jsonb` as a `str`
too, so `raw` is never a `list` and the function always returns `[]`: a scan or fix that declares
no `broker_holdings` is always the 400 "declare broker_holdings or sync a desk holdings snapshot
first". The port answers that 400 without the query (graded: `drift-nothing`,
`drift-empty-list`). **Found:** the desk fallback documented by the router cannot work; fix in
Python first (`json.loads` the text) if it is wanted. Also reproduced, not changed: a scan whose
body names one symbol twice is an unhandled `ValueError` (`detect_drift`'s duplicate check), the
500; the fix route de-duplicates (last wins) and answers 200.

## R6.15 — Lane writes-screens: an UPDATE is sent only for a net change, as the ORM flushes (found by the writes harness) [lane label R-ws.4] ⚠ UNREVIEWED

SQLAlchemy emits no `UPDATE` when an assigned attribute equals its loaded value (`portfolio_id`
re-filed where it already sits, an unpublish of a PRIVATE basket, a manager resubmission that
changes nothing, a drift payload that is unchanged, a holding whose quantity is numerically the
same). A Rust `UPDATE` with the same value still writes a new tuple, and `GET /cb/investments` (no
`ORDER BY` in Python either) then lists the investments in a different heap order — the harness
caught exactly that. Every such Rust `UPDATE` carries `... IS DISTINCT FROM ...` (a row
comparison for the manager), and `cb_investment_holding.updated_at` is stamped `now()` only with a
real change (`onupdate`). **For Maulik:** `GET /cb/investments` and `GET /cb/fees`-adjacent reads
have no `ORDER BY`; their order is the heap's.

## R6.16 — Lane writes-screens: plan previews are graded on the clock the run sees [lane label R-ws.5] ⚠ UNREVIEWED

`POST /cb/plans/{invest,apply,exit}` and `/cb/investments/{id}/customize` answer the
closed-market body outside 09:15–15:30 IST and a preview inside it; Python reads the wall clock
(`_now()`), with no override. The scenario grades whichever branch the run meets (the closed one
at the lane's runs, `next_open_ist` masked only because a run may straddle a session boundary);
the open branch's arithmetic (`curated_plans`, `shares_at_amount`, `market_hours_cb`) is graded by
unit tests whose expected answers were dumped from the live Python (`curated_plans::core`
tests). Plans are previews with a synthetic `cb-sim-{uuid4}` id: **no order, ever** (law 2).

## R6.17 — Lane writes-screens: observations for the foundation and the lead [lane label R-ws.6] ⚠ UNREVIEWED

* **Body before token.** FastAPI parses the body before it solves the router-level dependency that
  resolves the principal, so a *refused token* plus a malformed body is Python's 400
  `json_invalid`; the Rust pipeline resolves the principal in a layer before any handler, so it
  answers the 401. No lane can fix that inside a route; the cases avoid the combination (an
  anonymous caller with a malformed body is graded and equal: the lane reads the body first).
* **Redis database indices.** A run that used `--redis-db-py 7` lost its Python-side idempotency
  keys mid-scenario (another run flushed that index); `3`/`4` were clean. Lanes running at once need
  distinct indices.
* **Responses that echo the posted value, not the stored one.** `POST /cb/baskets` returns each
  weight as posted (`"0.33333"`) while `cb_constituent.weight` stores four places; `POST
  /cb/investments/{id}/sip` returns `amount` as posted while the column holds two places.
  Reproduced (the ORM object after the flush), recorded.

## R6.18 — Lane writes-screens: what the adversarial review found and fixed [lane label R-ws.7] ⚠ UNREVIEWED

* **Multi-row INSERTs, as the ORM flushes them.** SQLAlchemy 2.0 sends the rows of one table in a
  flush as one `INSERT ... VALUES (...), (...)` (insertmanyvalues, 1,000 rows a page). A value
  the column cannot hold (`weight` 1000 in `numeric(7,4)`, `qty` 1E+17 in `numeric(20,4)`) makes
  the whole statement fail when it is planned, before any identity value is drawn. The port
  inserted rows one at a time, so the rows before the bad one used up ids. Python's
  `cb_constituent_id_seq` stayed at 261; Rust's moved to 262 (`basket-create-overflow`). Now
  `POST /cb/baskets` (constituents), `POST /cb/investments/mark` (holdings) and
  `/drift/fix` (new holdings) send multi-row statements in pages of 1,000
  (`write_kit::values_rows`). `/drift/fix` also follows `persistence.save_obj`'s order: UPDATEs
  in primary-key order, then INSERTs, then DELETEs (`drift-fix-overflow-update`).
* **Plan quantities.** `shares_at_amount` truncates with `to_integral_value`, which has no
  28-digit limit. The port used `quantize(0)`, which raised for a quotient of 29 or more digits.
  The open-market plan routes gave a 400 where Python gives an integer
  (`150000000000000000000000000000` for 1.5E+29). This is fixed, and the unit tests check it
  against the live Python's answers.
* **Dates from numbers** now follow pydantic-core's `int_as_datetime`/`float_as_datetime` and
  speedate: the millisecond watershed, the microsecond carry, the 0000/9999 range checked before
  exactness, NaN, and a year-0 midnight refused as `date_parsing`. Before the fix, `1e400` was
  reported as `date_from_datetime_inexact`, and an integer beyond `i128` was read as 0, which is
  1970-01-01 (accepted). These rules were probed against the live pydantic 2.13.
* **Still open (foundation, for the lead): R6.17's body-before-token order.** Confirmed on the
  bed: `POST /screens` or `/cb/baskets` with an expired or garbage token and a malformed JSON body
  gets Python's 400 `json_invalid` and Rust's 401. The fix belongs in `app.rs`'s
  `versioned_dependencies`, not in a route. The rate-limit charge has the same order difference
  (Python charges nothing when the body is malformed).

## R6.19 — Lane writes-portfolio: the portfolio loaders run on the connection they are handed (RS4c lane writes-portfolio, 30 Oct 2026) [lane label R-wp.1] ⚠ UNREVIEWED

**Context.** A write route answers with what Python reads *after* its flush on the same session
(`_portfolio_out`, `portfolio_detail`, the refreshed ledger after a resolve). A read on the
read-only pool would not see the transaction's own writes.
**Choice.** Every loader of the lane (`routes/portfolio/load.rs`, `tradebook.rs`, the readers in
`portfolios.rs` and `portfolio_overview.rs`) takes `&mut PgConnection` instead of `&AppState`.
A GET route acquires one connection from the read-only pool (`db::acquire`) and hands it on —
Python's session holds one connection per request too; a write route hands its transaction.
`portfolio_detail`'s body is `detail_on(conn, ...)`. No SQL changed; the GET case files stay
538 of 538 EQUAL in both environments. **Reverse:** wrap the old signatures back around
`state.db.pool()` (the write routes would then need their own copies of the readers).

## R6.20 — Lane writes-portfolio: request bodies, multipart forms, CSV and `strptime` reproduced privately [lane label R-wp.2] ⚠ UNREVIEWED

* **Order of refusals** is FastAPI's: the body (JSON or form) is read and a syntax error answered
  **before** any dependency, then the 401, then every validation error of path, query, header and
  body together. (The foundation resolves a bad *token* in middleware, before the body; Python
  answers a bad token after a bad body. No case of this lane combines the two.)
* `routes/portfolio/model.rs`: pydantic 2.13's Python-mode `int` (a `bool` is an int; whole floats;
  `int_from_float`, `int_parsing_size`, `finite_number`), `Decimal` (`Decimal(repr(f))` for floats,
  whitespace and `_` in strings, `decimal_type`/`decimal_parsing`/`finite_number`), `Literal`
  enums, lists (`list_type`, `too_short`) and nested models (`model_attributes_type`, extras last).
  The writes-screens lane wrote its own (`screens/body_model.rs`, R6.12) in parallel; the
  integrator should collapse the two.
* `routes/portfolio/multipart.rs`: Starlette 1.6's `request.form()` with python-multipart 0.0.32's
  state machine and `parse_options_header` (a type with parameters is not lower-cased; RFC 2231
  `filename*` ignored; the last `file` wins; an unterminated body finishes no part; Starlette's own
  messages for a missing boundary or `name`). No new dependency.
* `routes/portfolio/pycsv.rs`: CPython 3.12's `_csv` reader (lazy, so a later row's `csv.Error`
  does not pre-empt an earlier `TradebookError`); `tradebook_csv.rs`: `DictReader` (blank rows
  skipped, `restkey`, duplicate field names) and `_strptime`'s regexes, ASCII digits only.

## R6.21 — Lane writes-portfolio: two decimal edges in uploaded files are not Python's [lane label R-wp.3] ⚠ UNREVIEWED

Python's `Decimal(text)` accepts non-ASCII digits (`"١٢"`) and the specials `NaN`/`Infinity`. Rust
reads non-ASCII digits as unreadable everywhere (a CSV cell is then `unreadable_quantity`, a
tradebook number a refusal, a JSON string `decimal_parsing`), and a portfolio-CSV `NaN`/`Infinity`
cell as unreadable — where Python keeps `Decimal('NaN')` and writes it to `numeric(20,4)` (`NaN`
is storable) or fails on `Infinity`. No broker export carries either. **Reverse:** a `WideDecimal`
with special values, and Unicode digit tables (R5.35 has them for `int()`).

**Adversarial review, 30 Oct 2026: the form reader missed Starlette's limits; fixed.** The review
found the lane's `multipart.rs` had no `max_fields`/`max_files` (1,000 each), no `FormParser`
limits (1,000 fields; a name plus value over 1 MiB), checked a text part's 1 MiB only once the part
finished (Starlette checks while it streams, so an unterminated part is refused too), and decoded
names, filenames and text values as UTF-8 whatever the `Content-Type`'s `charset` said. With 1,001
text fields Python refused with a 400 and Rust **created the portfolio**; with `charset=latin-1`
the two named it differently. All are now ported and graded (`mp-*` cases in
`writes_portfolio.json`; the two 1 MiB cases are unit tests pinned from Starlette 1.6). One
residual stays here: a `charset` naming a real codec other than UTF-8, ASCII or latin-1 (`cp1252`,
`utf-16`, …) is read as latin-1, where Python would use that codec. **Reverse:** a codec table.

## R6.22 — Lane writes-portfolio: the live broker reads are a cutover gate [lane label R-wp.4] ⚠ UNREVIEWED

`POST /brokers/{id}/sync-holdings` and `POST /trades/sync-today` are ported for every answer that
reads no broker. Where Python would read Kite (`DRY_RUN=false`, an app key, a stored session that
loads and is fresh), Rust answers **503 `pipeline-degraded`** and logs why, instead of reading:
the live holdings read and `broker_holdings_sync.sync_holdings_into_portfolio` (772 lines that write
the broker's holding group) and Kite's `GET /trades` are not ported. Both routes stay on Python
wherever a broker session exists. `POST /brokers/{id}/connect` is ported in full: with both Kite
Connect credentials set it mints the one-time `state` and registers it exactly as
`register_oauth_state` does — in this process and, when `BASKFY_BROKER_OAUTH_STATE_PATH` is set, in
the same JSON file (Python's format, owner-only) that Python's callback re-reads (graded: Python
and Rust appended to one file in the scratch run). Because the callback's success path stays on
Python (R5.27), **connect and the callback must move together, or share that file.** Nothing here
reaches an order path.

## R6.23 — Lane writes-portfolio: `POST /portfolios/{id}/rebalance` is not ported [lane label R-wp.5] ⚠ UNREVIEWED

It runs the screen (`screener.execute_screen`: the legacy statement or the ranking engine over
`build_ranking_frame_query`) and `rank_buffer.plan_rebalance`, then records the payload. The same
screener path is what R6.13 left on Python; the two should be ported together, once. Until then the
path answers 405 in Rust for every verb and Python serves the POST (a cutover gate).

## R6.24 — Lane writes-portfolio: Python behaviour reproduced on purpose, and one approximation [lane label R-wp.6] ⚠ UNREVIEWED — **for Maulik**

* **One instrument reached by two symbols** in one `POST`/`PUT` body or CSV (`INFY` and an alias of
  it) makes `replace_holdings` insert the same primary key twice: an `IntegrityError`, Python's 500.
  Reproduced (`create-dup-instrument`, `put-dup-alias`). A fix in Python would merge the rows.
* **A benchmark id beyond `smallint`** on `POST /portfolio` is asyncpg's `DataError` (500), not a
  404. Reproduced.
* **The reconciliation answer's `resolved_at`** is the application's wall clock (Python's
  `datetime.now`), masked in the scenario.
* **Approximation:** a `HoldingKeyIn` id beyond `bigint` is saturated, so a broker account that
  large is the 404 "No broker account" where asyncpg would raise (500). Unreachable from a client
  that read its ids from this API.

## R6.25 — Lane writes-sleeves: a pydantic emulation for typed bodies, private to the sleeve routers (RS4c lane writes-sleeves, 30 Oct 2026) [lane label R-wsl.1] ⚠ UNREVIEWED

**Context.** `body::validate_model` covers flat string models. The swing, TWT, VBT, sleeves,
options, F&O, overlap and backtests write models carry `int`, `Decimal`, `bool`, `date`, `time`,
`Literal`, `StrEnum`, nested models (`OptionsConfigPatch.book`, `BacktestConfig.costs`), lists,
`dict[SleeveGroup, …]`, tuples and model validators (before and after).

**Choice.** `routes/sleeves/pyd.rs`: a type-directed validator (`Ty`, `Model`, `PV`) reproducing
pydantic-core's lax Python-mode rules as FastAPI calls them (`from_attributes=True`): fields in
declaration order then extras, `missing`, `model_attributes_type`, `Value error, …` for an
after-validator at the model's location, a before-validator's exception escaping as the response
(`underlying-not-allowed`), dict keys at `<k>.[key]`, and every message probed against the tree's
pydantic. `PyDec` keeps a `Decimal`'s Python spelling (`Decimal(repr(float))`, `1E+2`, scale), so
`str(value)` in audit rows and problem details is Python's. Dates and times follow speedate's
messages for the shapes probed (digits-only strings as unix timestamps, `T`/`t`/`_`/space
separators, the range messages). Two other lanes built their own (`screens/body_model.rs`,
`api_keys/model.rs`); **the three should be collapsed** at integration, as R6.9 did for reads.

**Known limits.** A number a `numeric` column cannot hold (beyond `rust_decimal`'s 28 digits) is
the 500 catch-all, as asyncpg's overflow is in Python; a timezone-aware `hard_exit_time`
(`"14:00Z"`, or a number of seconds) is the 500 Python's naive/aware comparison raises; speedate
shapes not probed (week dates, ordinal dates) are refused with the nearest message.

## R6.26 — The Celery producer is Rust's; the harness normalises two values (RS4c lane writes-sleeves, 30 Oct 2026) [lane label R-wsl.2] ⚠ UNREVIEWED

`routes/sleeves/writes.rs::send_task` writes what kombu's Redis transport writes for
`CeleryTaskQueue.send_task(name, args)`: `SADD _kombu.binding.<queue>` and `LPUSH <queue>` of the
protocol-2 JSON message (headers, properties, base64 body `[args, {}, {callbacks…}]`), queue from
a copy of `PRODUCER_TASK_ROUTES`. Scan routes publish **after** the commit and stamp `task_id` in a
second short transaction (`deferred_publish`); `POST /backtests` publishes after the commit
(`BackgroundTasks`). Graded through Redis by the writes harness, all EQUAL.

Harness edits (`tools/parity/api_writes.py`, `api_diff.py`; additive): Celery's `origin`
(`gen<pid>@<host>`) is normalised, the producer pid differing by construction; and a writes file
may name `"redis_mask_captures"` — captured per-side values (a backtest's random `public_id`) read
as `<name>` in that side's Redis dump. **How to reverse:** drop both; nothing else depends on them.

## R6.27 — The ORM's identity map is reproduced where a response reads it (RS4c lane writes-sleeves, 30 Oct 2026) [lane label R-wsl.3] ⚠ UNREVIEWED

Found by the harness, reproduced on purpose:
* `POST /swing/watch` answers with the row's **in-memory** `trigger`/`stop_ref` (the value sent,
  `4100.005`), not the column's rounding, because `_one_watch` re-reads through the session's
  identity map. `_one_watch` reads **every** state (`state=None` default), so a note on an EXPIRED
  row is committed and answered.
* `PATCH /fno/config` does not `refresh` (unlike the swing/VBT/options settings). The identity map
  is weak, so CPython frees each row once nothing refers to it — except the **last** sleeve group
  of the request, which the router's loop variable `current` still holds when `_config_out` runs.
  That one group is answered with the values sent (`"0.5"`, `"100000.5"`); the book and every
  earlier group are re-read from the table. A latent Python inconsistency — **for Maulik**: fix
  in Python by `refresh`ing in `apply_*_patch`, then drop the overlay here.
* Every config patch writes the ORM's way: an `UPDATE` only for attributes whose value changed
  (numbers compared numerically), so `updated_at` moves only when one is emitted; an audit row is
  written whenever `str(before) != str(value)` even when the number is equal (`0.500` → `0.5`).

## R6.28 — `POST /backtests` refuses the in-process executor (RS4c lane writes-sleeves, 30 Oct 2026) [lane label R-wsl.4] ⚠ UNREVIEWED — **cutover gate**

Python's default `BASKFY_BACKTEST_EXECUTOR=inline` runs the simulation inside the API process
(`baskfy_worker.tasks.backtests.run_backtest_inline`). The worker is a later wave, so with
`inline` Rust answers **503 pipeline-degraded before anything is reserved or written**, rather
than accept a run that would never start. With `celery` the route is ported in full (validation of
`BacktestConfig` and inline `ScreenDefinition`s, entitlement and universe gates, stale data
version, `Idempotency-Key` reserve/replay/release/remember, the stale-run reaper rolled back with a
429, both concurrency caps, the insert, the hand-off) and graded EQUAL. **Gate:** route
`POST /backtests` to Rust only where the executor is `celery`.

## R6.29 — `PUT`/`DELETE /overlap/reviews` stay on Python; the tag corrections port a slice of `catalyst_tags` (RS4c lane writes-sleeves, 30 Oct 2026) [lane label R-wsl.5] ⚠ UNREVIEWED

The review labels re-read the row through `GET /overlap`'s view (`_row_on_the_list`), which is not
ported (R5.21); they remain 405 in Rust and Python serves them. `PUT`/`DELETE /overlap/tags` need
`tag_headline`, `tag_from_laya`, `resolve_tag` and `cache_key`: ported privately in
`routes/overlap/tags.rs` with `RULES` and `TAG_SCHEMA` **generated** from the Python module
(`rules_table.rs`), pending R5.21's move of `catalyst_tags` into `baskfy-core` with goldens.
`str.casefold()` is reproduced **exactly** from `casefold_table.rs` (all 1,530 code points CPython
3.12 / Unicode 15.0 folds to something else; the review replaced a lower-casing approximation
that missed Greek iota-subscript, the symbol variants, `ǰ` and Cherokee — and the result is a
**stored** key, `headline_key`, R6.32). The miss path
writes `catalyst_tag:wanted` as Python does (graded through Redis); a cached Laya answer could not
be seeded on the bed (each side's Redis database is flushed), so `tag_from_laya` is unit-tested only.

## R6.30 — Found in the foundation: a refused token beats a malformed body in Rust, not in Python (RS4c lane writes-sleeves, 30 Oct 2026) [lane label R-wsl.6] ⚠ UNREVIEWED — **for the integrator**

FastAPI parses a JSON body **before** solving dependencies, and the principal is a dependency
(the `/api/v1` router dependency included). So a malformed JSON body with an expired, stale-epoch
or forged token is Python's **400** `json_invalid`; `app.rs::versioned_dependencies` resolves the
principal (and the rate limit) before any handler reads the body, so Rust answers **401**. Not
reachable from a route file. The case (`sw-watch-expired-badjson`) was dropped from
`writes_sleeves.json` rather than graded red. **Fix:** in `versioned_dependencies`, carry a
token refusal as a pending problem for write methods with a JSON content type, and let
`body::receive` run first.

## R6.31 — Lane writes-sleeves: smaller calls (RS4c lane writes-sleeves, 30 Oct 2026) [lane label R-wsl.7] ⚠ UNREVIEWED

* **Scan 429 timing.** `Retry-After`/`retry_after` are `int(wait + 0.999)` of each side's own
  clock; Python and Rust are asked milliseconds apart, so a second boundary between them flips the
  number (≈1–3 % of runs of `vb-scan-owner-too-soon`). A re-run settles it; no mask is used.
* **Redis databases.** This lane grades on `--redis-db-py 10 --redis-db-rs 11`: 6/7 were in use by
  another lane during the run (their keys appeared in this scenario's dump).
* **Tests touched outside the lane's files**, both updated to the write-wave spec: the sleeve lane's
  read-only source test (`sleeves/common.rs`) now asserts the exact writes Python allows, and
  `tests/derivatives.rs`' 405 sweep skips the five writes now served.
* `POST /overlap/catalyst-scan`'s Redis status and lock hold a timestamp, so they are graded by
  presence; their content is graded through the response and `GET /overlap/catalyst-scan`.

## R6.32 — Adversarial review of lane writes-sleeves: two divergences fixed, coverage widened (RS4c, 30 Oct 2026) [lane label R-wsl.8] ⚠ UNREVIEWED

* **`POST /backtests` with the Celery executor and no broker** (no `BASKFY_REDIS_URL`, so Python's
  `task_queue` is `None`): Python writes the row and fails it in the same request
  (`_why_it_cannot_start`: `status='failed'`, the reason as `error`, `finished_at`) and answers
  `202 {"status": "failed"}`; Rust inserted it `queued`, logged, and answered `queued` — a run that
  would never start, the M42 bug Python fixed. Now the same INSERT + UPDATE in one transaction and
  the same answer. Graded by the new scenario `writes_sleeves_nobroker.json` (9/9 EQUAL with side
  effects; it also covers the three scan routes and the tag correction with no queue or cache).
* **`casefold`** was an approximation, and `cache_key` is stored (`catalyst_tag_correction.
  headline_key`) and used by `tag_headline`: a headline carrying a fold the approximation missed
  was filed under a key Python never reads. Now exact (R6.29); graded by `ov-tag-fold` /
  `ov-untag-fold` (a delete found by its folded spelling) and a unit test against CPython's output.
* **Coverage added to `writes_sleeves.json`** (217 of 217 EQUAL, side effects EQUAL): tenant
  isolation on `PUT /portfolios/{id}/sleeves` across two real accounts (owner on the subscriber's
  portfolio, subscriber on the owner's, write and read refused, the second account's own write
  accepted, the owner's division unchanged after); a second account reading the owner's backtest;
  and rollback of valid changes beside a refused ceiling in each of `PATCH /swing/config`,
  `/options/config` and `/fno/config`.
* **Found in the foundation, not fixed here (for the integrator):** with no Redis URL Python builds
  no rate limiter (`app.py`: `RateLimiter(cache, …) if cache is not None and …`) and serves
  unmetered, while `state.rs` builds one whenever `rate_limit_enabled`, which then refuses
  **every** request with 503 "Rate limiting is unavailable". The no-broker scenario runs with
  `BASKFY_RATE_LIMIT_ENABLED=false` for that reason. **Fix:** in `AppState::from_settings`, build
  the limiter only when `redis_url` is non-empty.

## R6.33 — Lane writes-accounts: pydantic's lax rules for typed bodies, private to the lane, graded against pydantic's own output (RS4c lane writes-accounts, 30 Oct 2026) [lane label R-wa.1] ⚠ UNREVIEWED

`body.rs` validates the two string kinds the session routes needed. The lane's twenty write
routes also declare `int` (`ge`/`le`), `bool`, `datetime`, `Literal[...]`, `list[str]` and
`list[Literal]`, and one path parameter is a `date`. `routes/api_keys/model.rs` (declared from
`api_keys.rs`, used by all six of the lane's routers) reproduces them: pydantic-core's lax `int`
(JSON ints of any size, bools, integral finite floats within `i64`, `lax_int` strings) and `bool`;
speedate's RFC 3339 grammar and its error words; unix timestamps (seconds, or milliseconds beyond
±2e10, and pydantic's own float arithmetic: the floor as the integer, the fraction's **magnitude**
as microseconds — so `-1.25` reads as `-2 + 0.25` — and a fraction of a *millisecond* beyond the
watershed); the lax fallback to a bare date, whose error is what a failing string reports; year 0;
`Literal` wording (`'a', 'b' or 'c'`); `null` accepted only by `X | None`. **Graded by vectors
dumped from pydantic 2.13**: 168 `datetime` inputs (`routes/api_keys/datetime_vectors.json`) and
60 `date` strings (`date_vectors.json`), every one exact, plus the harness. A parsed `datetime`
keeps its offset (or its absence): Python echoes the parsed object for a created key's
`expires_at`, but answers the stored UTC instant for an entitlement override (graded both ways).

**Overlap:** the sleeves lane (`sleeves/pyd.rs`, R6.25) and the screens lane
(`screens/body_model.rs`) wrote their own emulations in parallel. **How to reverse / collapse:**
one module under `crate::body`, keeping the vector tests.

## R6.34 — A naive `datetime` is stored as UTC; the scenario runs both servers with `TZ=UTC` (RS4c lane writes-accounts, 30 Oct 2026) [lane label R-wa.2] ⚠ UNREVIEWED — **for Maulik**

asyncpg encodes a **naive** `datetime` for a `timestamptz` column with `astimezone(utc)`, which
reads it as the *process's local time*: a key created with `"expires_at": "2027-01-01 10:20"` is
stored as 04:50 UTC by a Python API on an IST host and as 10:20 UTC in a UTC container, while the
response (and `is_active`, which reads naive as UTC) say 10:20 either way. Rust has no clock-zone
dependency (chrono is built without `clock`), so it stores naive as UTC — Python's behaviour in
every container. `writes_accounts.json` sets `"env": {"TZ": "UTC"}` so both sides are graded on
that behaviour. **Fix in Python (recommended):** refuse naive datetimes in the request models
(`AwareDatetime`), or `replace(tzinfo=UTC)` before storing.

## R6.35 — The webhook SSRF guard reproduces `urlsplit`, `ipaddress` 3.12 and the resolver (RS4c lane writes-accounts, 30 Oct 2026) [lane label R-wa.3] ⚠ UNREVIEWED

`routes/webhook_endpoints/ssrf.rs`: CPython 3.12's `urlsplit` (leading C0/space stripped, tab/CR/LF
removed, the scheme rule, `//netloc` up to `/?#`, the bracket checks), the `hostname`/`port`
properties, `ipaddress.ip_address` (dotted-quad IPv4 with no leading zeros; IPv6 with a `%zone`),
3.12.13's not-public sets (private with its exceptions, loopback, link-local, reserved, multicast,
unspecified; an IPv4-mapped IPv6 by its IPv4; `100.64.0.0/10` passes, as in Python), and the name
resolved with the system's `getaddrinfo` (tokio `lookup_host`, off the runtime), every returned
address checked. The `ValueError`s Python does not catch (an unbalanced bracket, a bad port, an
IPv4 in brackets, the `idna` codec's empty-label check) are the same 500. 30 graded URLs.
**Approximated:** a non-ASCII network location is neither NFKC-checked nor IDNA-encoded before the
lookup, and a name resolving to a scoped IPv6 address prints without its `%zone` in the refusal.
Registering a hostname performs a DNS lookup, in Python and in Rust alike (the scenario uses IP
literals and one `.invalid` name).

## R6.36 — The admin actions publish through a second copy of the Celery producer (RS4c lane writes-accounts, 30 Oct 2026) [lane label R-wa.4] ⚠ UNREVIEWED

`routes/admin/queue.rs` writes what kombu's Redis transport writes for `send_task` (the binding
`SADD`, the `LPUSH` of the protocol-2 envelope, `PRODUCER_TASK_ROUTES`), in one Lua script through
`cache::eval`. The re-run and the resync publish before their audit row, the reprocess after its
instrument lookup — Python's order. `origin` is normalised by the harness (R6.26; the lane's own
first edit for it was withdrawn in favour of that one). **Duplicate:** the sleeves lane's
`sleeves/writes.rs::send_task` is the same producer; collapse onto one `crate::queue`.
`POST /admin/resync` is graded only with `days=30` and `days=1` (R5.33: a wider window would probe
NSE). `admin_action.detail` holds a random task id, so the table's `detail` is masked in the
row diff and graded instead through `GET /admin/actions` (task ids masked by pointer).

## R6.37 — Billing: the Razorpay client, the webhook and the GST invoice are ported (RS4c lane writes-accounts, 30 Oct 2026) [lane label R-wa.5] ⚠ UNREVIEWED

* **Checkout.** The gateway is reached through `baskfy_providers::net::GuardedClient`, the
  bodies serialised as httpx serialises `json=` (key order kept, compact separators). The
  committed scenario points `BASKFY_RAZORPAY_API_BASE` at a closed local port, so a checkout that
  reaches the gateway is refused by the connection (503) on both sides and nothing leaves the box.
  The success path was graded separately against a local stub (not committed — the harness has no
  HTTP sink): 14 of 14 responses and side effects EQUAL, the 10 gateway requests byte-equal
  (path, `Authorization`, `Content-Type`, body); and the no-keys path (500) 10 of 10.
* **Webhook.** Signature (constant-time), `request.json()` failures as the 500, `str()` of the
  event name, `ON CONFLICT DO NOTHING` dedupe (the sequence moves on a replay, as in Python), the
  lifecycle (`_period_end` reads a `bool` as an `int`), and the captured payment: the `payment`
  row, `_grant_for` (promote or create the grant), and the invoice — `invoice_counter`'s gapless
  number, `gst.split_inclusive`, `pdf.render_pdf`/`invoice.render_invoice_pdf` byte for byte (a
  golden PDF dumped from Python, `routes/billing/invoice_golden.pdf`), stored in the same
  `RawArchive` the download route reads, and the row stamped. Graded: 23 webhook deliveries and
  the PDF download of the invoice each side raised, EQUAL.
* **Known limit:** the taxable value divides with `rust_decimal`, not Python's 28-digit context;
  the two can differ only on a quotient that rounds to a half-paisa tie at 28 digits, which an
  inclusive split of a two-decimal amount cannot produce.

## R6.38 — Lane writes-accounts: routing and Python behaviour reproduced on purpose (RS4c lane writes-accounts, 30 Oct 2026) [lane label R-wa.6] ⚠ UNREVIEWED — **for Maulik**

* `PATCH`/`DELETE /alerts/unsubscribe` reach the `{public_id}` handlers with the id `unsubscribe`
  (Starlette matches them there: a 404 "No alert with id 'unsubscribe'"); axum's static route
  would otherwise answer 405. `POST /alerts/unsubscribe` takes no session but still resolves the
  bearer (a stale token is a 401, as the router dependency makes it in Python).
* **Python defect, reproduced:** `DELETE /keys/{id}` of a key that a rotation replaced always
  fails with a 500 — `api_key.rotated_from_id` references it with no `ON DELETE`, so the delete
  violates the foreign key (graded: `keys-delete-rotated-from`). Fix in Python first (`ON DELETE
  SET NULL` on `rotated_from_id`, or refuse with a 400), then here.
* Reproduced as Python answers them: `rotate_api_key` lets a `ValueError` other than
  `KeyLimitReached` escape (a 500); a `PATCH /webhook-endpoints` whose URL collides with another
  endpoint on the same screen is the unique-constraint 500; an `int` column value beyond int32
  (`top_n`, `min_move`) is asyncpg's 500 before the statement is sent; the ORM writes only net
  changes, so `updated_at` moves only with them; the body is parsed before the principal is
  resolved (an anonymous caller with malformed JSON gets the 400), then the principal, then the
  model — except where the principal is a router-level dependency that Rust resolves in middleware
  for a *refused* token (R6.30).
* **Harness hygiene:** seeds give every server-defaulted timestamp an explicit value (a clone made
  a second later otherwise differs from base), and the lane ran on Redis databases 8/9 — an
  earlier run on 6/7 lost a `_kombu.binding` key on the Python side mid-scenario, consistent with
  another process flushing that database.

## R6.39 — Adversarial review of lane writes-accounts: two billing divergences fixed, 15 cases added (RS4c review, 30 Oct 2026) [lane label R-wa.7] ⚠ UNREVIEWED

* **Fixed — `_grant_for`'s `or`.** Python takes `_text(subscription, "id") or _text(payment,
  "subscription_id")`: an *empty* subscription id is falsy, so the payment's `subscription_id`
  names the grant. Rust used `Option::or_else`, which only falls through on a missing id, so such a
  charge was refused as unattributable instead of being recorded against the subscription (graded
  now: `rv-webhook-empty-sub-id-falls-to-payment`, which records `pay_rv_c5` against `sub_rsparity1`).
* **Fixed — a 2xx gateway body that is not JSON.** `RazorpayGateway._post` calls
  `response.json()` outside its `try`; the `JSONDecodeError` is not a `RazorpayError`, so Python
  answers the 500 catch-all. Rust mapped it to the 503 "gateway is not responding". It is now its
  own `GatewayError::Unhandled` (500). Graded off the record against a local stub (orders answer
  HTML; customers `{"id": ""}`; subscriptions `{"id": 12345}`), because the committed scenario's
  gateway is a closed port. Still open, by choice: `json.loads` accepts `NaN`/`Infinity` in a
  gateway answer and `serde_json` does not — Razorpay never sends them.
* **Cases added** (`rv-*` at the end of `writes_accounts.json`): `X-API-Key` alone is a 401 on the
  account routes; an empty revoke reason is stored as `''`; tenant isolation on the second seeded
  key, alert and endpoint; an upper-case signature does not verify; a lifecycle followed by a
  payment that overflows `numeric(12,2)` (and one beyond int64) rolls the claim and the lifecycle
  back, burning the same `payment` id on both sides; the same event id afterwards is a clean retry;
  a period end past year 9999 is a 500 and one in year 5138 is processed; a non-staff `PUT` on
  another account is a 404.
* **Masks reviewed.** `admin_action.detail` is masked whole in the table diff because it carries the
  random Celery `task_id`; every other member is graded through `admin-actions-after`, which masks
  only `/data/N/detail/task_id`. The remaining masks are random ids, digests of random tokens, and
  server clocks.

## R6.40 — Wave 4c integration: renumbering, one foundation fix, the unassigned routes, the bed (RS4c, 30 Oct 2026) ⚠ UNREVIEWED

* **Renumbering.** The lanes' labels became R6.12–R6.39 everywhere they appear (docs, code comments,
  case notes): R-ws.1–7 → R6.12–R6.18, R-wp.1–6 → R6.19–R6.24, R-wsl.1–8 → R6.25–R6.32,
  R-wa.1–7 → R6.33–R6.39. Each heading keeps its lane label in brackets.
* **Fixed — the rate limiter with no usable Redis URL** (found by the writes-sleeves verifier,
  R6.32). Python builds the limiter only when `_open_cache` returns a client, and
  `Redis.from_url` raises `ValueError` unless the URL starts with `redis://`, `rediss://` or
  `unix://` (case-sensitive; probed in the API venv). So with `BASKFY_REDIS_URL=""` Python serves
  unmetered (after logging that requests "will be refused" — its own message, reproduced), while
  Rust refused every request with 503. `AppState::from_settings` now builds the limiter only when
  that prefix check passes. Not reproduced: redis-py's later `ValueError`s (a non-numeric port or
  db path, a bad query argument), which no deployment's URL reaches. Graded by the new
  `writes_sleeves_nobroker_ratelimit.json` (the no-broker scenario with rate limiting left on):
  9 of 9 EQUAL, side effects EQUAL. Rejected: leaving it to the cutover list — it is a one-function
  fix with a graded case, and the cheapest to reverse.
* **Not fixed here — body before token** (R6.17, R6.30). FastAPI parses the JSON body before the
  router-level principal and rate-limit dependency, so a refused token with a malformed body is
  Python's 400 and Rust's 401. Changing the middleware order touches every route of every lane and
  is not an integration fix; it is the first item on the write-cutover list. No case grades it.
* **Unassigned routes.** `explore.py`'s `POST /watchlist` and `DELETE /watchlist/{slug}` were in
  no wave-4c lane. Rust answers them as R6.11 left them (405, or the GET route Python also
  matches); Python keeps serving them. They join the next write pass with the screener routes.
* **Count.** Python's routers declare 94 write routes. Ported: 82. Partly: `/auth/google`
  (R6.3). On Python with a reason: 11 — six screener routes (R6.13), `/portfolios/{id}/rebalance`
  (R6.23), `/overlap/reviews` ×2 (R6.29), and the two `explore` routes above.
* **The bed.** The lanes left seven `rsw_tpl_*` templates (~3 GB each); the integrator dropped
  them and its own. The integration did not compare `baskfy`'s row counts with the live database,
  because the bed rules forbid touching the live stack; it compared `baskfy` with itself before
  and after the writes runs (row counts and `pg_stat_user_tables` write counters), unchanged.
* **Python tests of the R5.28 fix.** `screener_helpers.migrate` shells out to `uv run alembic`,
  which fails here because the editable installs point at an old path. The integrator ran the
  suites with a test-environment shim (a `uv` on `PATH` that runs `python -m alembic` in the
  venv) rather than editing the helper: 65 passed.


## R7.1 — Cutover schedules the Rust nightly through the Celery beat entry, which shells out to the binary (RS5.0, 30 Oct 2026) ⚠ UNREVIEWED

**Context.** Python's nightly is a Celery task (`baskfy.pipeline.nightly`) fired by Beat at 15:50 and
18:45 IST, plus the catch-up sweep (06:45 and on a Kite login). The Rust worker has no Celery: a
Celery worker in Rust would mean reimplementing kombu's protocol and Beat's schedule for a job that
runs once a night.

**Choice.** `baskfy-worker nightly --date D` is the Rust entry point, and it implements the task's
**explicit-date** body only (three transactions, the alert, the return value). At cutover the Beat
entry keeps firing and its Python task keeps its scheduled-path guards — not a trading day, already
published, before `SESSION_DATA_READY_IST` (18:00 IST), and `kite_session_cli.refresh_quietly()` —
then **shells out** to `baskfy-worker nightly --date <resolved day>` and relays its JSON and exit
code. The same holds for `session_catch_up` (`baskfy-worker catch-up`, the gate lane's). The
binary exits 0 only for a succeeded run, 1 for a refused gate, an aborted day or a raised chain,
2 for a usage or configuration error.

**Rejected.** A Rust scheduler now (it would own the 18:00 guard before the steps it guards are
ported, and two schedulers could fire one night twice — the advisory lock would save the data, not
the alert noise); a Celery-protocol consumer in Rust (large, and it dies with the shell-out anyway
once the whole worker is Rust, when the single `baskfy` binary's own scheduler can replace Beat).

**How to reverse.** Point the Beat task back at `run_nightly_pipeline` (the Python body never
left). Both chains take the same advisory lock (`PIPELINE_CHAIN_LOCK_KEY = 0x0BA5C1FA`, bound as a
`bigint` in both), so a Python and a Rust chain can never run concurrently during the switch.

## R7.2 — `baskfy-worker`: one crate, the API's `Settings` and `Mailer`, no new dependency (RS5.0, 30 Oct 2026) ⚠ UNREVIEWED

* **Layout.** `rust/crates/baskfy-worker` (lib + bin `baskfy-worker`), declared in the workspace.
  `steps.rs` mirrors `steps.py` (the 14 `PipelineStep` strings in order, `NIGHTLY_CHAIN`,
  `POST_PUBLISH_STEPS`, `StepStatus`, `RunStatus`, `StepOutcome` with its detail in `dict.update`
  order, `HardFailure`, `open_run`/`adopt_run`/`close_run`, `record_step`); `orchestrator.rs`
  mirrors `orchestrator.py`; `nightly.rs` the task; `ops.rs`, `calendar.rs`, `alerts.rs`,
  `catch_up.rs`, `settings.rs`, `deps.rs`, `window.rs` the pieces they need.
* **Dependencies.** `baskfy-api` (lib) for `Settings` (the database URL, mail, the alert sinks)
  and `Mailer` — Python's worker imports `baskfy_api.settings` and `baskfy_api.email`, so the
  dependency runs the same way. Everything else (`sqlx`, `redis`, `tokio`, …) is already pinned;
  **no new third-party dependency**. The cache handle is a lazy `redis::Client` (Python's
  `Redis.from_url`, `None` when the URL does not parse).
* **Settings.** `WorkerSettings` has Python's 53 fields, environment names (the three desk aliases
  `DRY_RUN`, `OPTIONS_ENABLED`, `INTRADAY_ENABLED` included), defaults and constraints, held to
  `tests/fixtures/python_worker_settings.json` (generated from `WorkerSettings.model_fields` in an
  empty directory). Its coercion helpers duplicate `baskfy_api::settings`' private ones; collapse
  them into one public module at the next API foundation change.
* **The write pool.** The chain runs on `baskfy_db::WriteDb::begin()` — one transaction, as
  Python's `session_scope`. The desk score's savepoint is sqlx's nested `begin()` on the chain's
  connection (`SAVEPOINT`), Python's `begin_nested()`.
* **Lanes own files, never `lib.rs` or the orchestrator.** `steps/ingest.rs` (steps 1–4 and the
  calendar reconcile), `steps/index.rs` (5–6, fundamentals folded in), `steps/compute.rs` (7–8,
  the desk score), `steps/gate.rs` (9–10, and `catch_up.rs`), `steps/sleeves.rs` (11–14). Each is
  a declared stub returning `WorkerError::NotPorted`; its signatures are the orchestrator's
  contract. `WorkerError::python_type()` gives the Python class every graded message needs
  (`skipped_reason = "{type}: {message}"`, the failure row's `type`).
* **Law 2.** `tests/law_two.rs` scans the crate for any order or GTT verb, Kite write endpoint or
  execution crate.

**How to reverse.** Delete the crate and its workspace line; nothing else depends on it.

## R7.3 — The replay provider: one recorded day, two adapters, zero network (RS5.0, 30 Oct 2026) ⚠ UNREVIEWED

**Context.** Grading the nightly differentially needs both chains to read the **same** provider
answers, with no Kite, NSE or R2 call. Recorded HTTP would have to be replayed through two
different HTTP stacks and parsers (whose agreement is wave 4's job, already graded); the worker's
grading needs the inputs at the port.

**Choice.** A provider adapter at the **port** level, in both languages, behind each language's
production `CompositeProvider`: `baskfy_providers::replay::ReplayProvider` (Rust) and
`tools/parity/replay_provider.py` (Python, harness-only). Both read a **bundle directory**
(format 1): `manifest.json` (`trade_date`, `variant`, a `refuse` list) and one JSON file per call
under `calls/` — records as pydantic's `model_dump(mode="json")`, bars and bhavcopy rows with
decimal strings, built into frames on `DAILY_BARS_SCHEMA`/`BHAVCOPY_SCHEMA` exactly. A call the
bundle cannot answer is `ProviderUnavailable("[replay] no recorded answer for <call>")` in both;
a `refuse` entry raises the provider error class it names for a matching call (arguments compared
by their string form). `daily_bars` answers only inside the recorded window and only for a recorded
token (a token with no bar that day answers an empty frame, as Kite does); `equity_fundamentals`
answers the recorded rows for the asked symbols in the asked order, each once.

`tools/parity/replay_build.py` derives the bundles for a day from the bed (read-only; what each call
is rebuilt from is in its docstring) — deterministic, so an unchanged bed rebuilds the same hash.
The 2026-10-29 day: 11,241 Kite instruments (10,779 tokens, 3,635 with a bar), 2,653 + 585
listings, a 4,205-row bhavcopy, 41 corporate actions since 22 Oct, 12 constituent files, 151 index
snapshots, 3,518 fundamentals rows. Fidelity to the real night is partial and said so: the bed
stores no raw bhavcopy for 29 Oct, Kite's and NSE's records are rebuilt from the merged
`instrument` rows, and the real fundamentals fetch failed upstream that night (the bundle serves
the latest stored row instead, to exercise the path). The archive tarball in
`/Volumes/T7-Work/baskfy-aws-exit-20261029` holds raw files only up to 28 Oct for the bhavcopy, so
it was not used; it was not modified.

`replay_selfcheck.py` drives both adapters over a bundle with 22 calls (every method, answered and
refused, plus the edges) and compares value for value: **22 of 22 EQUAL** over `base` and over
`provider-failure-midway`. `tests/replay.rs` (5 cases) covers the Rust adapter alone.

**The injection.** Python: `nightly_py.py` monkeypatches `baskfy_worker.providers.build_provider_stack`
at process start (plus `kite_session_cli.refresh_quietly` and a loopback-only socket guard);
production Python is unchanged. Rust: `baskfy-worker --replay-bundle DIR` swaps the stack and arms
`net::block_network()`. The flag is a test seam in a production binary, chosen over an environment
variable because a flag cannot be inherited by accident.

**How to reverse.** Delete `replay.rs`, the example, the flag and the three Python files.

## R7.4 — `record_step` writes a Rust `traceback`; the span and the histogram are not ported (RS5.0, 30 Oct 2026) ⚠ UNREVIEWED

`record_step` upserts the `(run_id, step)` row twice (running, then final), so the identity
sequence moves by two per step in both languages; on success the `error` column is the detail or
JSON `null` (SQLAlchemy binds `None` into `JSONB` as `'null'` — checked by compiling Python's own
statement); on failure `{"type", "message", "traceback", **detail}`, where Python's `traceback` is
`traceback.format_exc(limit=20)` and Rust's is the error's `Debug`. They cannot match; the harness
masks the key as `<set>`. It persists only for a failure the chain survives, which today is none (a
raising step rolls the chain back). Not ported: the OpenTelemetry span per step and the
`observe_step` histogram — the step row is the graded record; tracing returns with the single
binary's observability work.

## R7.5 — Alerts: the log line and email ported; Sentry and the webhook not (RS5.0, 30 Oct 2026) ⚠ UNREVIEWED

`alerts.rs` ports `Alert`, `as_dict()` (stamped `raised_at`, Python's `isoformat`),
`ops.alert_for_outcome`, the task's `pipeline_failed` alert for a raised chain, and `dispatch` to
the log and to email through `baskfy_api::mailer` — the subject, text and HTML exactly as
`_send_email` builds them, the body `json.dumps(…, indent=2)` with `ensure_ascii` (`pyjson.rs`).
Sentry (no DSN on the local box) and the JSON webhook are not ported; a configured webhook logs
that it is not. The harness masks `raised_at` in mail. The nightly's three alert paths (gate
refusal, raised chain, none) are graded by the `gate-refuse`, `provider-failure-midway` and
`fresh` scenarios once the steps land.

## R7.6 — The chain is split into blocks; `step` runs one; Python's step mode is a pinned mirror (RS5.0, 30 Oct 2026) ⚠ UNREVIEWED

**Context.** The brief asks for `baskfy-worker step <name> --date D --run-id N`, run into an open
run, so a lane can be graded one step at a time on a clone primed by Python's chain. Python has no
such entry point: `_run_chain` is one linear function.

**Choice.** `orchestrator.rs` splits `_run_chain` into one **block** per step — the `record_step`
plus the glue that belongs to it (`active_instruments` before step 2, the calendar reconcile after
it, the desk score inside step 7, the gate's `HardFailure` after its row) — and `run_chain` walks
`NIGHTLY_CHAIN` through `run_block`; the statements run in Python's order. `step` runs one block on
one transaction after taking the chain lock: committed when it returns (a refused gate included —
the row is written, the run is **not** closed, that is the full chain's job), rolled back when it
raises. The only value a block needs from an earlier one that the database cannot answer is step
3's `touched` set; `--context ctx.json` carries it (`{"touched": [ids]}`), written by the prime.

For the Python side the harness mirrors `_run_chain` in `nightly_py.run_block` — the same task
calls in the same order — and pins `orchestrator.py`'s source (`_run_chain`, the three wrappers,
the desk-score step, `run_nightly_pipeline`) by SHA-256: `step` and `prime` refuse to run once it
changes. The chain mode never uses the mirror; it runs `nightly_pipeline` itself.

**Rejected.** Driving Python's real `_run_chain` with patched task functions to skip steps (the
glue between blocks would still run, and asymmetrically with Rust); a production Python entry
point for one step (the brief keeps production Python unchanged).

## R7.7 — `nightly_diff.py`: digests, masks, the `fresh` day, cached templates (RS5.0, 30 Oct 2026) ⚠ UNREVIEWED

* **Reuse.** `api_writes.py`'s container check, `psql`, sequences, Redis dump and SMTP sink; its
  per-table full-row read could not scale (`ohlcv_daily` holds 5.25 M rows), so tables are compared
  by an **order-free digest**: per row the JSON of its columns (masked columns replaced), `md5` per
  row, `string_agg` sorted per bucket of `hashtext(primary key) & 1023`, and the differing buckets'
  rows fetched and printed side by side. `pipeline_run`/`pipeline_run_step` are read in full.
* **Masks.** Only values that differ between two correct runs: ids (as ranks in the run tables, the
  payload's `run_id`), wall-clock stamps (`<before>`/`<during>` the side's own start, so "stamped by
  this run" still has to agree), `duration_ms` (`<set>`), the failure row's `traceback` (R7.4), mail's
  `raised_at`. Any further column mask goes in `TABLE_MASKS` with its reason; the report prints each.
* **`fresh`.** The bed with the day's chain output deleted (`FRESH_DELETES`): the day's
  `pipeline_run` (its steps cascade; the sleeves' `pipeline_run_id` columns are `SET NULL`) and the
  day's rows of the fifteen tables the chain writes by date. Upserts with no dated trace
  (`instrument`, `corporate_action`, `trading_day`, the adjusted history) stay; the chain rewrites
  them idempotently.
* **Templates and the budget.** `rsn_tpl_<kind>_<date>` (TimescaleDB jobs unscheduled, closed to
  connections) and `rsn_primed_<step>_<hash>` are **kept** between runs — the brief's priming cache
  — and evicted least-recently-used whenever a new database would exceed five besides `baskfy`. Every
  database created *for the run* is dropped on exit (`atexit` and SIGINT/SIGTERM handlers).
  `--drop-cache` removes the templates. A prime that fails is dropped, never cached.
* **Environment.** Both sides run from an empty working directory (no `.env`), `TZ=UTC`,
  `BASKFY_SOLE_USER_ID=1` (the box's sole tenant, so the sleeve steps run), alert mail to the sink,
  no OTEL, no Sentry, no fixture directory, no Kite key, no S3 bucket.

## R7.8 — Python behaviour the wave-5 foundation found, reproduced on purpose (RS5.0, 30 Oct 2026) ⚠ UNREVIEWED — **for Maulik**

* **A refused gate commits the chain's rows.** `orchestrator.py`'s module docstring says a hard
  failure rolls the transaction back; the code catches `HardFailure`, closes the run `failed` and
  returns, and `session_scope` then **commits** — bars, factors and every step row of the refused
  day land, only `data_version` does not (the `gate-refuse` scenario shows it). The Rust port does
  the same. Whether the docstring or the code is the intent is a decision, not an edit (root
  CLAUDE.md: the decision wins; here nobody wrote one down).
* **A step with no detail stores JSON `null`, not SQL `NULL`,** in `pipeline_run_step.error` (and so
  does the `running` row) — SQLAlchemy's `JSONB` binds `None` as `'null'`. Reproduced.
* **The replay builder's first bundle had two candles for one Kite token** (462 tokens are shared on
  the bed by a legacy `X-SM`/`X-SZ` row and its bare Emerge row), and Python's `upsert_bars` then
  failed the fresh chain with `CardinalityViolationError` — on both sides identically, so the harness
  graded it EQUAL. Real Kite returns one candle per token, so the bundle was fixed (one record and one
  candle per token, the bare row's). Worth knowing for the ingest lane: the shared tokens are real,
  and `active_instruments` hands both rows the same token.

**How to reverse.** Each is a Python change first, then the same in Rust; the harness grades both.

## R7.9 — Lane **index** (steps 5 and 6, fundamentals folded in): the calls (RS5, 30 Oct 2026) ⚠ UNREVIEWED

Lane label; the integrator renumbers. Files: `rust/crates/baskfy-worker/src/steps/index.rs` and the
private `steps/index/{membership,snapshots,fundamentals,universes,db_tests}.rs`.

* **Bulk upserts as arrays.** Python sends one multi-row `INSERT ... VALUES` per universe / per
  file; Rust sends the same rows as `INSERT ... SELECT * FROM UNNEST($1::smallint[], ...)` with the
  same `ON CONFLICT` target, `SET` list and (membership) `WHERE index_member_daily.source !=
  'nse_file'`. The rows written are identical; a repeated key fails both the same way ("cannot affect
  row a second time"); arrays carry no 32,767-parameter ceiling, which no current batch reaches in
  Python either. Numeric arrays are unconstrained `numeric[]`, so the assignment cast to
  `numeric(18,4)` etc. rounds exactly as Python's per-parameter bind does.
* **`Decimal` arithmetic is Python's context.** The guard's `level / anchor - 1`, its
  `{move * 100:+.1f}` (half-even, as `Decimal.__format__` takes the context's rounding), `_pe_on`
  and `_marketcap_cr` run on `baskfy_core::decimal::WideDecimal` (28 significant digits, then
  `quantize(..., HALF_UP)`), and the stored numerics they read (`level`, `close_raw`) are selected
  `::text` so the anchor keeps its stored scale in the message (`against 24398.7000`).
* **`baskfy_core.universes` stays private to the lane** (`universes.rs`: the 15 pinned ids/slugs,
  `slugify_index` with Python's `str.strip` whitespace set — it includes `\x1c`–`\x1f`, which
  `char::is_whitespace` does not — `FIRST_NON_UNIVERSE_INDEX_ID`, `sorted(SME_SERIES)`). Lift it to
  `baskfy-core` when a second lane needs it rather than keeping two copies.
* **Python's `getattr(provider, ...)` duck-typing** collapses to the composite's methods (it has
  all five). The exception sets are kept exactly: `_from_named_method` swallows any provider-tree
  error, `_from_provider` only `UnexpectedPayload`, `refresh_membership` records any provider-tree
  error per universe; an error outside the tree (`ErrorKind::Foreign`) raises in all three.
* **Unreachable-in-practice error classes.** A second dashboard id past 32,767 in one batch fails
  in Python inside asyncpg's int16 encoder; Rust refuses it before binding as `DBAPIError` with a
  similar message. A `WideDecimal` failure maps to `InvalidOperation`. Neither path has data that
  reaches it.
* **Database tests on temp tables.** The Python DB tests for these modules (`test_reference_data`
  membership/source/registration, `test_snapshots`, `test_fundamentals`) are ported as 14 tests that
  create `TEMP` tables shaped like production (keys, the `source` CHECK, numeric scales) on one
  connection to a scratch database named by `BASKFY_RS_SCRATCH_DATABASE_URL` (the bed container's
  `postgres` database; `baskfy` refused); without it they are `ignored`. Nothing persists. The worker
  crate has no `build.rs`/`cfg(bed)` and the lane does not own its `Cargo.toml`, hence `#[ignore]`.

**Graded** (`nightly_diff.py --step`, py vs rs, 2026-10-29 replay, bundle `33841c9f…`): `refresh_index_membership` fresh and rerun, `refresh_index_snapshots` fresh, rerun and provider-failure-midway — all **EQUAL** (payload, `pipeline_run`, `pipeline_run_step`, `index_member_daily`, `index_snapshot_daily`, `fundamental_daily`, sequences, Redis, mail), with no mask beyond R7.7's.

**How to reverse.** Each bullet is local to the lane's files.

## R7.10 — Lane **gate** (steps 9 and 10, `catch_up.rs`): the calls (RS5, 30 Oct 2026) ⚠ UNREVIEWED

Lane labels in the code: `R-gate.1`–`R-gate.5` (renumber at integration).

* **R-gate.1 — The gate is ported whole; every number is Python's.** `steps/gate/quality.rs` ports
  `tasks/quality.py`'s eight checks with the SQL SQLAlchemy compiles for each (same text, same bind
  order — assertion 2's offender list has no `ORDER BY`, so the plan's row order is kept by keeping
  the statement). Assertion 2's move is `Decimal` arithmetic in the default context
  (`WideDecimal`), the limit is `Decimal(repr(setting))`, `float(move)` goes through the decimal
  string; assertion 1's `statistics.median` keeps Python's int-or-float (an even count gives
  `4216.5`, stored as a JSON float); `f"{x:.0f}"`/`.2f`/`.0%` round half to even on the exact
  binary value (Rust's `{:.N}` does the same). The step detail is `report.to_payload()` and the
  `GateReport` handed to the orchestrator keeps its fields (`observed` lives in the detail only).
  The gate reads its thresholds from `WorkerSettings::from_env()`, as Python's
  `get_worker_settings()`. **Found by the harness:** SQLAlchemy types
  `percentile_cont(...).within_group(ret_12m)` as the column's `Numeric(14, 2)`, so assertion 8's
  medians reach Python as `float(Decimal("%.2f" % value))` — Postgres's `-18.009999999999998`
  is read as `-18.01`, which moves the stored mean and sigma in their last digits. Reproduced.
* **R-gate.3 — `statistics.fmean`/`pstdev` reproduced bit for bit with a private big integer.**
  Assertion 8 stores `mean`, `sigma`, `band` as JSON numbers. `fmean` is `fsum(data)/n` (an exactly
  rounded sum); `pstdev` (CPython 3.12) is the correctly rounded square root of the exact rational
  variance (`_ss` with `Fraction`s, `_float_sqrt_of_frac` with round-to-odd). A naive `f64`
  computation differs in the last bits, so `steps/gate/pystats.rs` carries a ~200-line unsigned big
  integer (add, sub, mul, shifts, long division, `isqrt`) — cheaper than a new dependency
  (`num-bigint` is only transitive). Held to CPython by 60 random lists (`pystats_cases.txt`,
  seed 20261030) and the bed's 59 real `ret_12m` medians (`pystats_bed_medians.txt`), all bit-equal.
* **R-gate.2 — `publish` ports the screener's warm-up for the legacy SQL path only; a
  `ranking_terms` screen raises.** `run_publish` needs `baskfy_api.screener`'s
  `current_data_version`, `purge_screen_cache` and `warm_screen_cache` (`top_screen_definitions`,
  `run_screen` with `resolve_as_of`, `execute_screen`, `_attach_desk_columns`, `screen_provenance`,
  `payload_with_provenance`, `record_run`, `json_number`). No Rust module executed a screen (R6.13),
  so `steps/gate/publish.rs` ports that half privately over `baskfy_core::screener`
  (`build_screen_query`, `cache_key`, `canonical_json`, `ScreenDefinition::model_validate`), with
  asyncpg's cell types (`numeric` → `Decimal` at its stored scale) and a restatement of the API's
  crate-private `csv_export::executable` (the expanding-`IN` rendering). The **ranking-engine path**
  (`rank_definition` over a pandas object frame) is not ported: no saved screen on the bed has
  `ranking_terms`; meeting one, `publish` raises `NotPorted` (the chain rolls back, the alert says
  why) rather than warm it differently from Python. **Cutover gate**, shared with R6.13: port the
  ranking-engine execution once, for both. Also approximated: a Redis failure's message
  (`cache_purge_failed`/`cache_warm_failed` carry redis-rs's text, not redis-py's); the key, its
  value and its `EX 172800` are Python's.
* **R-gate.4 — `catch_up.py` ported; the Celery task `session_catch_up` is not.** `catch_up.rs` has
  `landed_sessions`, `gate_refused_sessions`, `unlanded_sessions` (today excluded before
  `SESSION_DATA_READY_IST` = 18:00 IST — never a day still in session; the calendar read without an
  exchange filter, as Python's), `demote_derived_after_missing_bhavcopy` and
  `calendar.mark_inferred_holiday` (public here; the ingest lane's reconcile can share it), plus
  `split_sweep` (the `max_sessions` bound). The task body runs `nightly_pipeline` per day and then
  `swing_eod_task` (the desk's swing plan — a later wave) and the bhavcopy publication probe; it
  stays on Celery/Python until the swing EOD half is ported. The foundation's `catch-up` command
  calls `unlanded_sessions` only.
* **R-gate.5 — `refresh_basket` is not ported by this lane.** Its stub lives in `steps/sleeves.rs`
  (the sleeves lane's file, called by the orchestrator), and its body is the desk's: it imports the
  desk's `app.config`, reads the newest `kite-momentum-rebalancer/data/uploads/scan_*.csv` **by
  file mtime**, and runs `momentum_scan.build` → `score.apply_filters`/`score` (pandas quantile
  clips and `rank(pct=True)`) → `basket.build_plan` (398 lines), with no goldens for any of them.
  Porting it half-way would store a basket Python would not. It belongs with the desk wave
  (`baskfy-desk` shares `score`/`basket`/`app.config`); goldens first. Until then the stub raises
  `NotPorted` and the chain on Rust stops at step 11 — a **cutover gate**. (On the box the step
  writes nothing — run 103 skipped, "no basket could be built", because the container has no
  uploads; under the harness Python finds the repo's uploads and builds one.)
* **Database tests** follow R7.9: `steps/gate/db_tests.rs` (16 tests: every
  `test_quality_gate.py` assertion test and the finder half of `test_session_catch_up.py`) run on
  `TEMP` tables in the scratch database named by `BASKFY_RS_SCRATCH_DATABASE_URL`; `ignored`
  without it. The pure verdicts are tested always (`steps/gate/tests.rs`, `catch_up.rs`).

**How to reverse.** Each bullet is local to the lane's files (`steps/gate.rs`, `steps/gate/`,
`catch_up.rs`).

## R7.11 — Lane **ingest** (steps 1–4 and the calendar reconcile): the calls (RS5, 30 Oct 2026) ⚠ UNREVIEWED

**Scope.** `steps/ingest.rs` (the foundation's signatures, unchanged) and private submodules
`steps/ingest/{instruments,bars,calendar,corporate_actions,adjustments,adjust,frame}.rs` port
`tasks/instruments`, `tasks/listings` (`run_refresh_listings`, `store_listings`), `tasks/bars`,
the part of `bhavcopy_backfill` the step calls (`backfill_bars_from_bhavcopy` on the chain's own
transaction, `_rows_for_day`, `upsert_day`), `calendar.reconcile_calendar` with the M62
publication check, `tasks/corporate_actions`, `tasks/adjustments` (`run_apply_adjustments`,
`reprocess_instrument` with its `as_of` bound, `_rescale_deep_segment`) and
`baskfy_core.adjustments`. `reprocess_instrument` is public from `steps::ingest` for a
point-in-time caller; the nightly passes `as_of = None` (house rule 5's deliberate "as of today"
series — not "fixed").

* **`baskfy_core.adjustments` lives in the worker (`ingest/adjust.rs`), pure.** It had no Rust
  home and the lane owns only `steps/ingest/`. Nothing in it touches I/O, so it moves to the core
  unchanged when someone wants it there. Its Python unit tests (`test_adjustments.py`) are ported
  beside it. **Reverse:** move the file to `baskfy-core/src/adjustments.rs`.
* **The adjusted prices reproduce polars 1.43's decimal arithmetic, not Python's.** Python's
  `adjust_bars` multiplies polars `Decimal` columns: every factor literal is `Decimal(38,10)`
  (rounded half-even), and a column product is the exact product rounded **once**, half-even, to
  the wider of the two scales — measured against the installed polars (`2.5e-10 → 2e-10`,
  `3.5e-10 → 4e-10`, `Decimal(38,25) × Decimal(38,10)` keeps 25 places exactly) and pinned by
  `adjust.rs`'s `polars_rounds_half_even_once_at_the_wider_scale` and a three-action history
  copied from a Python run. The factors themselves stay Python-context `Decimal`
  (`WideDecimal`). Two exact operations were **added** to `baskfy_core::decimal::WideDecimal`
  for it — `mul_exact`, `rescale_exact` and `coefficient_len` (no existing item changed).
* **`_load_raw_bars`' frame is reproduced, inference window included.** The frame is built from a
  list of dicts with `strict=False`, so each decimal column is `Decimal(38, s)` with `s` the
  widest scale among the **first 100 rows** (polars' `infer_schema_length`), and later values are
  rounded half-even onto `s` (measured). A row without `open_raw` is `open / adj_factor` in
  Python's 28-digit context, so `s` can reach 20-odd and the product's scale with it — on the bed,
  1,330 of the touched instruments' rows take that path. The loader reads every numeric as
  **text**, so each value keeps the scale asyncpg hands Python (`numeric(18,4)` → 4 places).
  *Review correction (30 Oct 2026):* a value that needs more than 38 digits at `s` is **null**
  under `strict=False` (measured: `[1.000…01 (scale 35), 108159362.54…, 558.2000]` →
  `[…, None, …]`), not an error; the port raised `ComputeError` there and now yields null, which
  then fails the `NOT NULL` write on both sides alike. `s > 38` is a polars panic; the port raises
  `PanicException` (residual: Python's escapes `record_step` as a `BaseException`). A reviewer's
  differential (1,900 generated histories through Python's `adjust_bars`, `_price`, `_factor`,
  `int(volume)`, `_issue_price` and the deep-segment maths vs the Rust functions) agrees on every
  row, including the overflow cases where both sides refuse.
* **Statement shape: `UNNEST` instead of multi-row `VALUES`; the rows per statement are Python's
  where they are observable.** Each identity-keyed upsert (`instrument`, `corporate_action`)
  consumes one sequence value per proposed row, so the instrument merge keeps Python's 2,048-row
  batches, the register and the corporate actions one statement each; `ohlcv_daily` (no identity)
  keeps the 2,000/1,000-row chunks so a duplicate key fails where Python's would. The Kite rows
  are written in one buffered pass across instruments (Python: one statement per instrument after
  the fetch) — the same rows, the same conflict targets. The deep-segment rescale issues one
  `UPDATE` per changed row by primary key, as the ORM's flush does (an `UPDATE … FROM UNNEST`
  over compressed chunks was not risked).
* **`reconcile_calendar` writes the same verdicts in two statements** (observed days, inferred
  holidays) and does not issue the final "still provisional" count, which nothing reads.
* **Provider errors are caught exactly where Python's `except ProviderError` is**
  (`ProviderError::is_provider_error`); a non-provider error propagates. `CredentialsMissing` is
  recognised by kind and prefixed as Python's `f"CredentialsMissing: {exc}"`.
* **Residuals, none reachable on the replay day:** `_issue_price` parses a string payload with
  `WideDecimal` (Python's `Decimal` also accepts `NaN`/`Infinity`, which would make the action
  `INVALID` with decimal's signal text; here it is `None`, i.e. `INSUFFICIENT_DATA`); a database
  error inside the bhavcopy write is recorded with the Rust error's message, not SQLAlchemy's
  (and aborts the transaction on both sides alike).

**How to reverse.** Every bullet is local to the lane's files and the three added `WideDecimal`
methods; `steps/ingest.rs` can return to its `NotPorted` stubs.

## R7.12 — Lane **compute** (steps 7 and 8, the desk score folded into 7): the calls (RS5, 30 Oct 2026) ⚠ UNREVIEWED

Files: `rust/crates/baskfy-worker/src/steps/compute.rs` and `steps/compute/{factors,ranking,desk,score,market_health,numeric}.rs`.
No new dependency, no edit outside the lane's files.

* **Python modules with no Rust home, ported as private submodules (R2.3's pattern).**
  `tasks/factors`, `engine` (I/O half), `tasks/ranking` with `factors_ranking.cross_sectional_pctile`
  and `rank_persistence`, `tasks/desk_score` with `desk_score_service.score_day`/`project`, the
  scan half of `momentum_scan.build` (the columns `score.required` reads; `unadjusted_symbols` is not
  ported — `score_day` never reads `suspect_symbols`), `score.apply_filters`/`score.score` with
  `DESK_CONFIG`'s values, `tasks/market_health` with `breadth.breadth_query`'s compiled SQL. The factor
  arithmetic is `baskfy_core::factors::compute_factors` (goldens), untouched.
* **How a float reaches a `numeric` column.** asyncpg binds a Python float as `Decimal(float)` — the
  exact binary — and PostgreSQL rounds it to the column's scale, half away from zero. `rust_decimal`
  cannot hold an exact double, so `numeric::float_to_numeric` rounds the integer `m·2^e` itself (u128),
  giving PostgreSQL's digits; the scale comes from `pg_attribute` (`format_type`), so the writer binds
  exactly the frame's columns that `factor_daily` has (a column the frame lacks is neither inserted nor
  updated, as Python's payload). A float bound to an integer column truncates (`int(obj)`).
  The desk score's `_stored` is different on purpose and reproduced as such: half-up on Python's
  **shortest repr** (`Decimal(str(x))`), not on the binary (`quantize_half_up_repr`).
* **One upsert per table, not Python's bind-parameter chunks.** `INSERT ... SELECT * FROM UNNEST(...)
  ON CONFLICT DO UPDATE`; the same rows. The two cross-sectional columns are one `UPDATE ... FROM UNNEST`
  each (Python's executemany). The bar history (3 calendar years, ~3 M rows) and the desk's bars are read
  through a server-side cursor (`DECLARE ... FETCH 50000`) inside the chain's transaction — Python's
  `stream(...).partitions(50_000)`; no `futures` crate needed.
* **polars' `sum_horizontal` is a tree, not a left fold.** `blend_expr` over four columns adds
  `(a + b) + (c + d)`; the left fold split ties differently and moved 1,259 stored `mom_pctile` values on
  the replay day until fixed (measured: 0 of 20,000 random rows differ from the pairwise sum).
* **pandas/NumPy reproduced operation for operation in `score.rs`**: `np.percentile`'s linear method with
  `_lerp`, `clip` with NaN bounds ignored, `rank(pct=True)`, `sum()` from integer 0, `np.round` as
  `rint(x·10)/10`, and the SCORE sort: pandas `nargsort` over NumPy's **unstable** `aquicksort_`
  (median-of-three introsort, `SMALL_QUICKSORT = 15`, heapsort fallback) — arm64 has no SIMD argsort, so
  the generic one is the one Python runs. Checked against `np.argsort(kind="quicksort")` on 3,000 random
  tie-heavy arrays (0 differences), and the whole `score` + `_stored` against Python on 40 generated
  scans of up to 1,500 rows with NaNs and ties (`score_matches_python_on_generated_scans`, ignored by
  default: it needs `BASKFY_SCORE_DIFF_JSON`, a dump from the lane's generator). If the box ever runs
  NumPy on x86 with AVX-512, its argsort is x86-simd-sort and the tie order of equal SCOREs would differ
  **in Python** too — the rank is then platform-dependent in both languages; a stable sort would be a
  Python change first (it is the desk's code, so Maulik's call).
* **Order-dependent reads kept as Python issues them.** `risk_masks`' cut breaks ties in the order
  `index_member_daily` rows come back (no `ORDER BY` in Python); the Rust query is the same statement and
  the sort is stable descending, as `list.sort(reverse=True)`. `assert_mask_matches_membership` reports the
  first 20 in the same unordered read.
* **Not reproduced: one pandas failure mode.** If an entire scan column were NULL, pandas would make it
  `object` dtype and `score.score` would raise `TypeError` on a comparison; Rust treats the column as NaN
  and scores. Unreachable on real data (every column has values); noted, not ported.

**How to reverse.** Delete `steps/compute/` and restore the stub; nothing else depends on it.

## R7.13 — Lane **nightly-sleeves** (steps 12–14: `compute_swing`, `compute_vbt`, `compute_twt`): the calls (RS5, 30 Oct 2026) ⚠ UNREVIEWED

Lane label; the integrator renumbers. Files: `rust/crates/baskfy-worker/src/steps/sleeves.rs` and the
private `steps/sleeves/{common,precision,qulla,swing,swing_market,twt,vbt}.rs`.

* **Two workspace dependencies added to the worker's manifest**, both already pinned in the
  workspace: `polars` (the detectors hand `baskfy_core` frames) and `regex` (VBT/TWT's ETF patterns,
  `re.IGNORECASE` + `search`). No new third-party crate.
* **Python modules with no Rust home live with the lane (R2.3).** `precision.rs` copies
  `baskfy_core.precision` (`COLUMN_PRECISION`, `INTEGER_COLUMNS`, `round_expr`, `integer_expr`,
  `quantise`; the core's copy is private to `factors`); `qulla.rs` ports `baskfy_core.exits.qulla`
  (`mean`, `adr_pct`, `trail_for`, `decide`) over `WideDecimal` (Python's 28-digit context);
  `swing_market.rs` ports the parts of `swing.market`/`swing.indicators` the core has not
  (`breadth_snapshot`, `ema`, `index_reading`, `market_gate`, `drawdown_pct`, `liquid_expr`), with
  CPython 3.12's compensated `sum()` and Python's `round(x, 2)`. Lift them into `baskfy-core` when
  `swing::market`, `exits` and `precision` get their own modules; then delete these copies.
* **Bars and floats are read as `::float8`.** Postgres's `numeric → float8` is correctly rounded,
  which is exactly Python's `float(Decimal)`; Decimals that stay Decimals in Python (`tw_position`,
  the TWT exchange series, `sw_position`, `sw_fill`, `sw_config`) are read with `baskfy_db::row`
  (scale kept) and computed in `WideDecimal`.
* **What Python writes is reproduced cell by cell.** A float written through `_decimal` is
  `Decimal(repr(x))` (`baskfy_core::py`); `_int` is truncation; precision applies exactly when
  Python's would — the column is a float **and** in the table (`common::stored` reads the frame's
  dtype). Postgres then rounds into the column's scale on both sides alike.
* **One upsert per row** instead of SQLAlchemy's multi-row `VALUES` chunks: the same rows and the
  same `ON CONFLICT` set lists. A duplicate key inside one chunk would fail Python and not Rust; no
  frame can produce one (keys are unique per day).
* **`tw_position` is updated only when a value changed**, then with `updated_at = now()`: the ORM's
  flush emits no `UPDATE` for an unchanged row (and so `updated_at` stays), and Rust does the same.
  Written columns are the queue ones and `trail` only — never `stop_price`, `gtt_trigger`,
  `quantity_open` (law 2).
* **`run_twt_ratchet` is not ported.** `config.exits.qulla_exits` is a code default (`True`) that no
  setting can change (`load_twt_config` reads only `stop_pct`/`trail_pct`), so the nightly never
  reaches the 20 % ratchet. If it ever did, Rust raises `NotPorted` and the wrapper records the step
  `skipped`. Port it the day the flag becomes a setting.
* **Unreachable in the nightly, not ported:** `extra_bars`/`provisional=True` (LV8 live scans),
  `reference_date`, `scan`, `live_index_level`. The provisional-straggler sweeps that a **real**
  run performs are ported.
* **`execution_enabled`** (swing) is accepted and read by nothing, as in Python since A10.
* **`refresh_basket` (step 11) is not in this lane's brief** and stays `NotPorted`; it needs
  `baskfy_api.baskets.build_current_basket` (the `/baskets` live build, an API cutover gate). Until
  someone ports it the Rust chain stops at step 11 (its wrapper, unlike 12–14, lets the error out).

**Graded.** `nightly_diff.py --step compute_swing|compute_vbt|compute_twt`, scenarios `fresh` and
`rerun`, sides `py,rs`: **6 of 6 EQUAL** (payload, `pipeline_run`, `pipeline_run_step`, `sw_setup_daily`
509 / `sw_market_daily` 41, `vb_signal_daily` 551 / `vb_breadth_daily` 33, `tw_state_daily` 1,580 /
`tw_breadth_daily` 32, sequences, Redis, mail) with no mask beyond R7.7's. `rerun` exercises the
settled-ladder branch of `write_market_row` (the bed's 29 Oct row is `settled_by: swing-eod`).
`gate-refuse`, `provider-failure-midway` and `non-trading-day` never reach steps 12–14. Not exercised
by the bed's day: a `tw_position` whose queue changes (its six OPEN positions already carry tonight's
decision), any `tw_signal_daily` row, a thin session — covered by unit tests ported from
`test_twt_qulla.py`, `test_swing_detect.py`'s helpers and `test_vbt_detect.py`'s universe tests.

**Adversarial review (30 Oct 2026).** `nightly_diff.py` gained `--mutate FILE` (one SQL file applied
identically to both clones before either side runs; its sha256 is printed) and a `tw_position.updated_at`
stamp mask (the ORM's `onupdate=func.now()`, reached only when a queue changes). Three mutations over
`fresh`, each run for all three steps: **edges** (a real swing book — closes inside/after the date, a
null `r_multiple`, a simulated close, an OPEN line on a name with no bar, a PARTIAL with SELL and BUY
fills, an entry after the date — a peak above NAV and a lock; a daytime scan's provisional
`sw_setup_daily`/`sw_market_daily` leftovers, the market row settled with `drawdown_pct` 0; changed
`vb_config`/`tw_config`; provisional VBT/TWT leftovers, one TWT signal referenced by a `tw_order`; five
`tw_position` queues tonight must rewrite, a NULL trail, a GTT above the stop, a recent entry),
**nobars** (edges with every 29 Oct bar deleted: swing's `no instrument has a bar` + `_drop_provisional_day`,
VBT/TWT's `not a session`), **thin** (150 of 4,205 bars kept and the three config rows deleted: the
thin-session rows, the defaults path, `sleeve_drawdown` with no config). All EQUAL with no mask beyond
the two stamps; `--scenario rerun` with edges too. Listed, not ported: VBT's `signal_rows` rebuilds its
frame with `pl.DataFrame(list_of_dicts)` (100-row schema inference), so a column `None` in the first
100 scan hits and non-null later raises polars' `ComputeError` in Python (the step goes `skipped`) while
Rust writes the rows. No bed day reaches it (it needs 100+ hits all lacking, say, a 200-session SMA ahead
of one that has it); reproducing polars' message byte for byte is not worth the fragility. The fix
belongs in Python (`infer_schema_length=None`), after which both agree.

**How to reverse.** Every bullet is local to the lane's files and two manifest lines.

## R7.14 — Step 11, `refresh_basket`, ported by the integrator; the harness pins the desk's `.env` (RS5, 30 Oct 2026) ⚠ UNREVIEWED — **for Maulik**

**Context.** The gate and sleeves lanes both left step 11 a `NotPorted` stub (R-gate.5 in R7.10;
R7.13 item 3). Python's step never raises, but the Rust stub did, so every Rust `--chain` stopped at
step 11 and rolled back `publish`'s `data_version`: the wave could not be graded. On the bed the box's
own run 103 skipped the step ("no basket could be built": no uploaded scan inside the container), but
the harness runs Python from this checkout, where `kite-momentum-rebalancer/data/uploads/` holds one
`scan_*.csv` (271 symbols, read-only regression corpus), so Python builds and stores a 15-name basket.

**Choice.** Ported whole in `steps/sleeves/basket.rs`: `_carried` (the newest `scan_*.csv` by mtime that
has the wanted columns, read with pandas' `utf-8-sig` and default NA spellings), `_bars` (every bar on
or before the date for those symbols, no exchange filter, `float(Decimal)` with NULL as 0.0),
`momentum_scan.build` (through the compute lane's `build_scan`, now `pub(crate)` with `DeskBars` and
`Carried`), `unadjusted_symbols`, `score.score` (the compute lane's), and the slice of
`baskfy_core.basket.build_plan` an empty book reaches (cash band, selection by rank with the 0.85
quantile, score-proportional weights with the 15% cap, the 6% floor, `round(·, 2)`, order sizing with
the 1%-of-day-value cap, `stop_from_vol`, the no-trade band with `costs.cost_pct`), then the
`basket_snapshot` upsert on `(as_of, data_version)`. Facts reproduced on purpose, **for Maulik**:

* `build_current_basket` passes no `data_version` to `momentum_scan.build`, so every snapshot's
  `data_version` is **1** and `screen_run_id` is `sha256("desk_momentum_v1|<date>|1")[:16]`, whatever
  `publish` bumped — contrary to the module docstring ("the basket's identity includes it"). Two nights
  on one date therefore overwrite one row. Rust reproduces the code.
* `DESK_ROOT` is source-relative in Python; in Rust it is `BASKFY_DESK_ROOT` when set, else the desk
  beside the workspace (`CARGO_MANIFEST_DIR/../../../kite-momentum-rebalancer`). Cutover gate: the
  deployed binary must be given `BASKFY_DESK_ROOT`.
* The desk's `app.config` runs `load_dotenv()` at import, which walks up from `<DESK_ROOT>/app` to the
  first `.env` — on this Mac, the desk's real `.env` (Kite keys, `FULLY_INVESTED=true`). Python's
  harness side therefore loaded those keys into its environment during every RS5.0/lane chain run (none
  was printed or logged). `nightly_diff.py`'s `TEST_ENV` now pins every key that file carries
  (`FULLY_INVESTED=false`, the Kite keys empty, `DRY_RUN=true`, ...); python-dotenv never overrides a set
  key. Rust's `fully_invested()` reads `FULLY_INVESTED` the same way (environment, then only that
  key's line of the first `.env` found).
* A build failure is recorded as `skipped_reason = "{type}: {message}"` with Python's hint and the
  chain continues; an empty scan raises Python's `AttributeError: 'DataFrame' object has no attribute
  'ma_50'` text; `invest == 0` raises `ZeroDivisionError: float division by zero`.

`csv` and `sha2` join `baskfy-worker`'s manifest; both were already exact-pinned in the workspace
(providers, API), so no new third-party dependency.

**Rejected.** Making the Rust stub record `skipped` like a failed build — it would store no snapshot
where Python stores one, so `basket_snapshot` and the step row would differ on every graded chain.
Deferring to the desk wave (R-gate.5's suggestion) — it blocks the wave's acceptance on code that is
~500 lines and pure.

**Not reproduced (unreachable with an empty book).** Holdings, runners, exits, the replacement edge,
the half-size rule (a scan frame never carries `listed_on`), cluster warnings, `plan_cost`, the
`plan_id` (`uuid4`, not stored). A CSV with a non-numeric cell in a numeric column raises a
`ValueError` here where pandas would keep the column as strings and fail later with another message.

**Graded.** `--step refresh_basket --scenario fresh` py,rs **EQUAL** (`basket_snapshot` 1 row, 15
names, 43 suspect symbols, masks `computed_at`/`computed_ms` only), and again with
`FULLY_INVESTED=true` on both sides; then every `--chain` scenario (R7.15).

**How to reverse.** Restore the stub in `sleeves.rs` (one function) and drop `basket.rs`; drop the
`TEST_ENV` keys (re-exposing the harness to the desk's `.env`).

## R7.15 — Wave 5 integration: the lanes renumbered, the catch-up decision graded, the bed (RS5, 30 Oct 2026) ⚠ UNREVIEWED

* **Renumbering.** The lanes' entries `R7.index`, `R7.gate`, `R7.ingest`, `R7.compute`, `R7.sleeves`
  are now **R7.9–R7.13** in file order (every reference in the docs and sources updated); their
  internal labels (`R-gate.1`…`R-gate.5`, numbered sub-items) are kept. R-gate.5 (`refresh_basket`)
  is closed by R7.14.
* **Catch-up.** `baskfy-worker catch-up` now prints its decision — `{"checked_through": D,
  "sessions": [...]}` from `unlanded_sessions(through=today IST, lookback_days=7, now=now IST)` — on a
  transaction it rolls back, and takes `--now YYYY-MM-DDTHH:MM[:SS]` (naive IST) so the clock can be
  injected. `tools/parity/catch_up_diff.py` asks Python and Rust the same question on one clone at
  moments inside the session, a second before 18:00 IST, at it, after it, on a Saturday and on the
  next Monday's pre-open, on the bed and with 2026-10-29's run deleted. Running the nightly for each
  answer, the swing EOD and the derived-holiday demotion stay with the Celery task (R-gate.4): the
  cutover keeps Python's beat entry around the binary (R7.1).
* **Integration edits only.** `compute.rs` exposes `desk` and `score` to the crate; `desk.rs`'s
  `DeskBars`, `Carried` and `build_scan` become `pub(crate)`. No lane logic changed; no test was
  weakened; no golden or tolerance touched. Python's worker production code is unchanged.

* **Found while re-grading the API, not fixed (pre-existing, RS4c).** `writes_screens.json`'s
  `probe-exit-huge-int` differs when graded during market hours: the session is open, so
  `POST /cb/plans/exit` builds a plan, and Rust's `i128` leg quantity saturates on the case's 42-digit
  integer where Python's `int` is exact; the case also masks neither `desk_plan_id` (`uuid4`) nor
  `expires_at_hint`. At RS4c it was graded after the close and took the closed-market branch.
  `baskfy-api` is unchanged by this wave. The fix belongs to the API (an unbounded quantity, or
  Python's refusal reproduced) plus the two masks; until then the case is red between 09:15 and
  15:30 IST.

**How to reverse.** Revert the visibility edits with R7.14; `catch-up` without `--now` reads the
system clock as before (its new stdout line is additive).
