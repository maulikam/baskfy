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

## R3.1 — twt compares floats IEEE-style; NaN-free input frames are a precondition (RS3, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** The twt reviewer found that a literal NaN in an input bar frame is handled
differently. Python's polars compares floats in a total order (NaN largest, NaN == NaN,
`NaN > 1.0` true; `max_horizontal`/`min_horizontal` fold like x86 `maxsd`, so argument order
matters; group `min` skips NaN). The twt port uses IEEE comparisons in `cmp_f` (the tight band,
`close >= month_low*multiple`, `close_raw > min`, `vol_sma >= min`), `liquidity_floor`, breadth's
`breadth_above_dma`/`breadth_series`, `limit_locked` (`frame::same`), `month_low_back`'s min and
`tight_state`'s `f64::max/min`. Verified live: with `close = NaN`, `sma_dma = 1.0`, polars counts
the name as above the DMA and Rust does not. vbt (`vbt::calendar::frames`) and fno research
(`pl_gt`) already port the total order.

**Choice.** Not fixed at RS3 (the integrator fixes only mechanical issues). Recorded as a
**precondition: twt's functions take NaN-free float columns.** Real input cannot violate it:
bars come from Postgres `numeric` through Kite/NSE ingestion and `_prepare` casts to Float64,
which yields no NaN; every derived column is finite on positive prices; the only derived NaN,
`tight_range_pct = 0/0`, compares false in both. The golden and fuzz encoders cannot express a
NaN input (NaN encodes as `null`). The backtest panel path tests `math.isfinite` in both.

**Rejected.** Shipping it as "sound" without a record (it is a real semantic gap); porting the
total order inside the integrator commit (a behaviour change across seven call sites with no
NaN-capable test to grade it).

**How to close (before the TWT plan strangles its Python twin).** Reuse vbt's total-order
helpers in the listed twt call sites, port the horizontal fold and the NaN-skipping group min,
and add a NaN-capable differential test (inputs built in-process, not via the JSON encoder).

## R3.2 — vbt prints a config float with Python's `repr`, not Rust's `Display` (RS3, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** The vbt reviewer's fuzz found `first_live_multiplier` (72 of 1,600 cases) and
`PublishedStudy::cagr` turning a whole-number float into `"1"` / `"26"` where Python's
`Decimal(str(1.0))` is `"1.0"`: both went through `swing::sizing::decimal_from_py_float`, which
formats with Rust's `{x}`. No golden covered it.

**Choice.** Both now go through `vbt::exits::wide::from_py_float_str` (Python's `float.__repr__`)
and then to `Decimal`. Share counts never moved (the multiplier scales a budget that is then
floored); the printed multiplier and CAGR now match. `swing::sizing::decimal_from_py_float`
keeps the gap in general — it belongs to the swing lane and no swing golden or fuzz case shows
a printed whole float through it — and is left untouched.

**How to close fully.** Route `decimal_from_py_float` through the same repr helper (one function;
re-run the swing goldens and fuzz). **How to reverse:** restore the old call; the fuzz shows it.

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
change, best done with the R3.4 housekeeping) would make the type names exact everywhere.

## R3.4 — Wave 3 reused `WideDecimal` in place and duplicated shared VBT/fno code (RS3, 29 Oct 2026) ⚠ UNREVIEWED

**Context.** STATUS asked wave 3 to lift `WideDecimal` into a shared module first. The lanes ran
in parallel and each owned only its own files.

**Choice (as landed).** Every lane imports `crate::backtest::helpers::WideDecimal` where it is,
with vbt layering its Python-float helpers on top (`vbt::exits::wide`, shared with `vbt::plan`
and `vbt::sizing`). twt carries private copies, written before the vbt lane landed, of VBT's
config shape (`twt::config::vbt_shared`), calendar (`twt::calendar::shared`), breadth
(`twt::breadth::shared`) and indicator arithmetic; `fno::ledger` carries private copies of
`fno::exits`' `f1_paused`/`f2_month_paused`/`book_paused`; `fno::costs` takes a role→price map in
`LegRole`'s declaration order instead of `fno::condor::LegRole`; `fno::ledger` ports
`options.journal.max_drawdown` privately. twt and vbt plan hashes reuse the screener's SHA-256,
so the integrator widened `screener::sha256` to `pub(crate)` (the one edit outside the three
areas). All R2.3's pattern; every copy is graded by its own goldens, so none can drift silently.

**Rejected.** Collapsing them inside the integrator commit (not mechanical: it rewires call
sites in three lanes at once).

**How to close.** The housekeeping step before wave 4 (STATUS): move `WideDecimal` and the
py-float helpers to a shared module, re-export `vbt::{config, calendar, breadth, indicators}` from
twt, point `fno::ledger` and `fno::costs` at `fno::exits` and `fno::condor`, and keep 1,702 of
1,702 green throughout.

## R3.5 — Polars-expression functions are graded as the expression applied to a frame (RS3, 29 Oct 2026) ⚠ UNREVIEWED

`vbt.signals.chartink_scan`/`trend_filters` and twt's `bucket_keys`/`liquidity_floor` return
polars *expressions* in Python; the Rust ports return them **evaluated over a frame** (a
`Boolean` series), which is what every caller does and what the dumpers store. A null on either
side of a comparison is a fail (Kleene logic, then `fill_null(False)`), as in Python. vbt's
calendar refuses a null `date` where Python would carry a `None` "session" into the calendar
(every caller and golden passes a polars `Date`). **How to reverse:** return lazy `Expr`s if a
caller ever needs to compose them; the goldens grade the evaluated result either way.

## R3.6 — vbt parses some config floats with Rust's `Display` where they are only compared (RS3, 29 Oct 2026) ⚠ UNREVIEWED

`vbt::orders::fill_if_touched`'s threshold and `vbt::sizing::size_entry`'s `PyDec::from_float`
parse a config float via `format!("{x}")` rather than Python's `repr`. The values are compared or
floored, never printed, so they agree in value (the fuzz with 1e30 thresholds agreed). A finite
`fill_through_pct` near 1e300 could fail `WideDecimal` parsing where Python returns `None`. Left as
is; no real config is near it. **How to close:** use `from_py_float_str` there too (R3.2).

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
