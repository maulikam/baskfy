# 13a — Addendum: verification figures in `docs/13` §2 re-measured against the committed file

`docs/13-csv-export-schema.md` §2 tabulates what the reference export proves. Two of its stated
figures were re-measured against the committed
`fixtures/reference-screen-export-2026-08-18.csv` during Prompt 5. **One of the two has since been
resolved in `docs/13`'s favour** (§2 below, Prompt 6); the other stands.

**Neither finding overturns a formula.** The one that stands is an artefact of re-deriving a value
from inputs the export has already rounded, and the shape of the error distribution is what proves
it. It is recorded here rather than absorbed into a widened tolerance, per Prompt 5: *"Treat any
column that cannot be reproduced as a specification bug to be investigated and documented, not as
a test to be loosened."*

Asserted in `packages/core/tests/test_reference_parity.py`.

---

## 1. Finding 1 — the sharpe identity's maximum error

`docs/13` §2 states:

> `sharpe_return_N = absolute_return_N / (volatility_N × 100)` — **1,355/1,355 cells match**, max
> error 0.0051 = pure 2-dp rounding

Re-measured over all 1,355 cells, the error distribution is:

| absolute error | cells |
|---|---|
| exactly 0.00 | 1,344 |
| strictly between | **0** |
| exactly 0.01 | 11 |

So the maximum is **0.01**, not 0.0051.

### Cause

The export stores `absolute_return_*` at 2 dp. The reference product divided its *unrounded*
return; we can only divide the rounded one it publishes. Where the true ratio sits within half a
tick of a `.xx5` boundary, the two land on opposite sides.

Worked example — **ATHERENERG, six months**:

```
stored return      99.78          → the true value lies in [99.775, 99.785]
stored volatility  0.47177847
true sharpe        in [2.114870, 2.115082]   → reference product stored 2.12
our recomputation  99.78 / 47.177847 = 2.1149757  → rounds to 2.11
```

Every one of the eleven outliers is explained this way, and the test asserts that individually:
for each mismatching cell, the stored value must lie inside the sharpe band implied by the
unrounded return.

### Why the formula is not in doubt

A different formula would scatter errors across a range. There is **nothing between 0.00 and
0.01** — the signature of a rounding boundary and of nothing else. `docs/13`'s 0.0051 figure was
presumably measured against unrounded inputs its author held and the export does not carry.

---

## 2. Finding 3 — the count of blend inversions — **RESOLVED, docs/13 was right**

`docs/13` §2 states:

> Export order reproduces `mean(sharpe 1y,6m,3m,1m)` monotonically; the only 48 "inversions" are
> ≤ 0.0075, i.e. exactly the rounding granularity of 2-dp inputs

This addendum previously recorded **53**, and attributed the gap to `docs/13` having measured
against unrounded components. That was wrong, and the error was ours.

Re-measured in `Decimal`: **48** inversions, maximum exactly `0.0075`. Both figures reproduce from
the committed file precisely as `docs/13` states them.

### Cause of the earlier reading

Binary floating point. `sum(float(x) for x in four_two_dp_values) / 4` cannot represent 2-dp
decimals exactly, and five pairs that are *exact ties* in the file drift by ~1e-16 and are then
counted as inversions — 34 adjacent ties become 28, and 48 inversions become 53. Nothing about the
export changed; only the arithmetic used to read it.

CLAUDE.md house rule 9 — "money and prices are `numeric`, never `float`" — is a rule about storage,
but this is the same failure it exists to prevent, one layer up: a float sum of stored decimals
made a correct document look wrong. `test_reference_parity.py` now sums blends in `Decimal` and
asserts the count *and* the bound, so the figure is pinned rather than merely bounded.

---

## What this changes

Nothing in the implementation. `packages/core/src/decile_core/factors.py` implements the identity
exactly as `docs/05` §3 states it, and `decile_core/blends.py` implements the blend exactly as
`docs/05` §4 states it. What changed is the assertion: the tests now check the property that is
true of the committed artefact —

- every cell within one unit in the last stored place,
- over 99% exact,
- **no cell in between**, which is what would break first if the formula were wrong,
- every outlier individually explained by its rounding band,
- and, for the blend ordering, the exact 48/0.0075 that `docs/13` records.

If unrounded reference data ever becomes available, `docs/13` §2's original figures should be
re-checked against it and this addendum retired.
