# Per-family window evidence — P1.5's written explanation

**Measured 22 Aug 2026** against all 271 rows of `fixtures/reference-screen-export-2026-08-18.csv`
using 1,138,300 real adjusted NSE bars. Each family's parameter was swept **independently**, on the
data, with no engine involved — the question asked is "what did the reference product do?", not
"does our code agree with itself".

`MERGE-PROMPTS.md` M11 step 2 requires that every non-green column get a written explanation. This
is that document.

## The hypothesis under test

That the window rule is **per family**, not global:

- **bar factors** (returns, moving averages, highs) read `K` bars inclusive, base = the first bar;
- **interval factors** (volatility, sharpe, RSI, positive days) read the `K−1` daily returns
  *inside* that same `K`-bar window.

If that scheme held for all five windows, `docs/05` and `docs/13` would be reconcilable by naming
both concepts. **It does not hold.** The evidence follows.

## Bar family

| column | best parameter | exact | verdict |
|---|---:|---:|---|
| `absolute_return_one_month` | base shift **21** | 265 / 271 | coherent |
| `absolute_return_three_months` | **63** | 260 / 270 | coherent |
| `absolute_return_six_months` | **120** | 256 / 270 | coherent |
| `absolute_return_nine_months` | **183** | 250 / 269 | coherent |
| `absolute_return_one_year` | **245** | 240 / 268 | coherent |
| `ma_20` | **20** bars | 222 / 271 | coherent |
| `ma_50` | **50** bars | 261 / 270 | coherent |
| `ma_100` | **100** bars | 250 / 270 | coherent |
| `ma_200` | 199 bars | **3 / 269** | **incoherent** |
| `high_one_year` | any K in 243–248, **on `high`** | **249 / 268** | input settled, window not |
| `away_from_high_one_year` | same | **249 / 268** | same |

The return shifts are `K − 1` for `K` = 22 / 64 / 121 / **184** / **246**.

## Interval family

| column | best parameter | exact | verdict |
|---|---:|---:|---|
| `positive_days_percent_one_month` | **22** returns | 266 / 271 | strong |
| `positive_days_percent_three_months` | **64** | 194 / 270 | weak |
| `positive_days_percent_six_months` | **121** | 253 / 270 | strong |
| `positive_days_percent_nine_months` | **185** | 109 / 269 | **barely better than noise** |
| `positive_days_percent_one_year` | **247** | 118 / 267 | **barely better than noise** |
| `volatility_*` | — | **0 / 268** at every parameter | **definition unknown** |
| `rsi_*` | — | **0 / 268** at every parameter | **definition unknown** |

`positive_days_percent` recovers **exactly** `docs/13` §3's N — 22 / 64 / 121 / 185 / 247 — which
independently confirms that derivation. But the return base wants 184 and 246 at nine and twelve
months where positive days wants 185 and 247.

**So the two families disagree by one bar at the long windows and agree at the short ones.** A
single `K` with intervals of `K−1` inside it cannot produce both. The hypothesis is refuted.

## `docs/13` §3's derivation, re-run

Its method — the minimal `N` for which every published value is an integer multiple of `1/N` —
**cannot be re-run literally**, because the export stores `positive_days_percent` at 2 dp and
rounding destroys exact divisibility. `docs/13` observed the *step* between values (4.545% = 1/22),
which is the same idea applied to a rounded file. Re-derived that way it confirms 22 / 64 / 121 /
185 / 247.

Extending it to the other interval families is **not possible, and that is itself the finding**:
volatility, sharpe and RSI are continuous statistics with no `1/N` quantisation to exploit, and
`circuits_*` is a raw count that pins nothing (`docs/DECISIONS.md` §19.6 already says so).

## Two columns are not a window problem at all

**`volatility_*` reproduces 0 of 268 at every window length**, and a grid over return type
(simple / log) × `ddof` (0 / 1) × annualisation (√252, √251, √250, √260, √365) × window
(240–251) finds **no exact combination**. CUPID alone comes within 2.6 × 10⁻⁶ at
simple / `ddof=0` / √250 / 245 — but that combination reproduces **1 of 268** across the file, so
it is a coincidence and not a rule. `docs/05` §2 specifies sample stdev and √252; whatever the
reference does, it is neither, and the export cannot arbitrate it.

**`rsi_*` likewise reproduces 0 of 268** — a Wilder RSI over any period in range. The
initialisation almost certainly differs; the export carries no intermediate to settle it.

Both belong with the `circuits_*` columns: *definitions the export cannot arbitrate*, not engine
defects. They are reported unchecked rather than compared, so nobody reads a mismatch as a bug.

## What was changed on this evidence, and what was not

**Changed — two fixes, each strong and each independent of the window question:**

1. **`docs/05` §1's return base.** `P_{t-N}` → `P_{t-(N-1)}`. Reproduces 0 / 1 / 0 / 0 / 0 at the
   documented base and 265 / 260 / 256 / 250 / 240 one bar later.
2. **`docs/05` §10's highs.** The high is the **intraday high**, not the close: 249 / 268 against
   6 / 268, and **flat across every window length from 243 to 248**, which is what proves it is the
   input rather than the window. The section's own worked example already used CUPID's 299.00 — the
   highest price it traded at, not the 294.86 it closed at — so the prose contradicted the example.

**Not changed — the window boundary.** Making the window exclusive of its boundary day fits the
return family perfectly and takes total failures to their lowest (5,115), and it breaks
`positive_days_percent` at nine and twelve months, which are the columns `docs/13`'s derivation
rests on. Choosing it would trade one family's evidence for another's. It is reverted.

## Where this leaves the gate

Total failing cells: **7,319 → 6,934** (base) → **6,454** (highs). The residual is dominated by
`volatility_*` and `rsi_*` — which are not window questions — and by the three- and six-month
window lengths.

**M12 arbitrates.** The desk's `data/uploads/` corpus is the oracle that actually gates M13: those
CSVs are what a real portfolio was scored, ordered and stopped against. Empty top-25 delta tables
there settle the window semantics operationally, in the only terms that matter — whether the merged
engine picks the same stocks in the same order.
