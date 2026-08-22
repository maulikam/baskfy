# Shadow mode — how the generated scan earns the right to be the default

**Status: running, not passing.** The first run, 22 Aug 2026 against the 2026-08-18 corpus,
returned **four order deltas**. The flag stays off.

---

## What this is

The desk has been traded off a CSV exported by hand from momoindiascreener.in every week. M13 built
the path that generates the same thirty columns from the merged engine and deliberately left it
off. Shadow mode is what turns it on: run both, compare the orders, and require four consecutive
green Fridays before the default changes.

Four is calendar time. It cannot be hurried, batched, or simulated by running the same Friday four
times — the point is four independent weeks of market conditions, not four executions of a script.

## Running it

```bash
make shadow DATE=2026-08-22                       # from decile-blueprint/
.venv/bin/python -m scripts.shadow_mode --date 2026-08-22   # or from the desk
```

It touches no Kite session, no live prices and no holdings, so it runs on a Sunday — which is when
anyone will actually look at it. Both plans are built against the same fixed book at the same fixed
capital, so the **only** variable is the scan.

Every run appends one JSON line to `data/shadow-mode.jsonl`.

## What counts as green

**An empty order-level diff.** Symbol, side and quantity, all three matching, for every order.

Not scores. Not ranks. `reconciliation/DESK-PARITY.md` already compares those, and a score that
moves by a tenth changes nothing anyone can lose money on. An order that appears, disappears, or
changes size is the entire difference between the two systems.

**A ±1 share difference is not green.** It is small, it is almost certainly rounding, and it is
still a different order. The bar is an empty table because "small enough to ignore" is a judgement
that gets easier to make every week.

## The first run, and why it is not green

```
shadow mode 2026-08-18  scan_1787143663_Investing_001__1_.csv vs 0db6fe59674293e7: 4 ORDER DELTAS
  orders: 15 uploaded / 15 generated   breadth: 68.6 / 68.3   suspect symbols: 41
  AETHER         uploaded=('BUY', 40)  generated=('BUY', 39)
  DIVISLAB       uploaded=None         generated=('BUY', 7)
  SHILPAMED      uploaded=('BUY', 81)  generated=None
  WELCORP        uploaded=('BUY', 35)  generated=('BUY', 36)
```

Two of the four are one share. The other two are one **substitution**: `SHILPAMED` is bought by the
uploaded plan and not by the generated one, and `DIVISLAB` takes its place.

`SHILPAMED` is one of the forty-one symbols whose bar history carries an unadjusted corporate
action — it steps from 778.75 to 384.95 overnight on 2025-10-03. Its returns and its distance from
its one-year high are computed against a price that was never adjusted, so the generated engine
scores it lower and drops it out of the basket.

**This is not a shadow-mode failure. It is `NEEDS-MAULIK.md` item 4, showing up in the only place
that ultimately matters — the orders.** Shadow mode will not go green before the corporate-action
history is backfilled, and it should not.

## The four-Friday protocol

1. Every Friday after the close, run `make shadow DATE=<that Friday>`.
2. Green → note it. Red → the deltas are the week's work; fix, and the counter restarts at zero.
3. Four consecutive green Fridays, on four different weeks.
4. Then, and only then, flip the flag.

The counter restarts on **any** red, including a one-share difference. Three green weeks and a
rounding delta is a restart, because the alternative is a rule that bends, and a rule that bends
once has no fourth week either.

## The flag

```bash
SCAN_SOURCE_DEFAULT=generated      # in .env
```

One config change. `upload` (the default) means `/analyze` needs a file or an explicit
`generate_for=YYYY-MM-DD`. `generated` means a POST with neither builds the scan itself for the
latest date the pipeline holds bars for.

Uploading keeps working in both settings, permanently. The cord is cut, not removed: an export
someone downloads by hand is still the fastest way to check the desk against the outside world,
and it is the only way to trade a date the pipeline has no bars for.

**Rolling back is the same one change, in reverse.** That is deliberate, and it is why this is a
config flag rather than a deletion.

## What it does not prove

* **Nothing about execution.** Both plans are built and neither is sent. The order path is
  `packages/execution`, and it is not in this comparison at all.
* **Nothing about a universe the pipeline does not cover.** The harness compares one Friday's
  scan against the same Friday's bars.
* **Nothing about the carried columns.** `marketcap`, `beta`, `circuits_*` and `is_nifty_fno` are
  taken from the uploaded scan on both sides, because nothing in either repository computes them
  yet. Four green Fridays would say the *factor engine* agrees. It would say nothing about the
  four columns that never went through it.
