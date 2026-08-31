# M14 shadow harness — re-measured, 31 Aug 2026

**What this answers.** `docs/00-merge-status.md` has recorded, since 22 Aug 2026, that the shadow
harness's first run found "**4 order deltas**, one a corporate-action substitution." That entry
predates M24, M27 and M28, which recovered and applied 285 corporate actions. This re-runs the
harness and reports what it says **today**. The 22 Aug figure of 4 is history and should not be
quoted as current.

## 0. What was measured, and at which commit

Measurement is pinned, because a sibling leaf was editing `packages/core/src/baskfy_core/windows.py`
while this ran and the number moved under it. Both states are reported.

| | Run A — the committed tree | Run B — the working tree |
|---|---|---|
| Commit | `7d6b7fb3aa6d23dadb30d645d28884a8b7eb6d84` (`M62: green — a day NSE published is never inferred a holiday`) | same commit |
| `baskfy_core` source | `git archive HEAD` extracted to a temp dir, put ahead of the editable install on `PYTHONPATH` | the live working tree |
| `windows.py` | as committed — `bisect_left` in `resolve_window` | leaf 1.4.1's fix present — `bisect_right`; sha256 `8803fd6f4cf712f8406f214048daffda020be56030e77b96b2862464752c6ac4` |
| Anniversary-day bug | **present** | **fixed** |
| **Order deltas** | **2** | **3** |
| Date compared | 2026-08-18 | 2026-08-18 |
| Database | `baskfy-postgres` (localhost:5433), `ohlcv_daily` 3,534,860 bars 2017-01-02 → 2026-08-21, `corporate_action` 289 rows | same |
| `SCAN_SOURCE_DEFAULT` | `upload` (off) | `upload` (off) |
| `DRY_RUN` | `true` | `true` |

Leaf 1.4.1's window fix is **uncommitted**. Run B is therefore not reproducible from git alone; it is
recorded by file hash. Run A is the reproducible one and is what the tables below use unless a row
says otherwise.

The date is 2026-08-18 and not a later Friday because the comparison is against a **fixed oracle** —
the hand-exported `scan_1787143663_Investing_001__1_.csv`, dated 2026-08-18. Generating for any
other date would compare that Friday's bars against a different Friday's export, which measures
nothing. `ohlcv_daily` runs to 2026-08-21, so the data is not the constraint; the corpus is.

## 1. How the harness works

`kite-momentum-rebalancer/scripts/shadow_mode.py`, invoked as `make shadow DATE=…` from
`decile-blueprint/`. It is not `reconciliation/desk_parity.py`; the two are different instruments
aimed at the same seam, and the relationship matters:

| | `reconciliation/desk_parity.py` (M12) | `scripts/shadow_mode.py` (M14) |
|---|---|---|
| Compares | top-25 **membership and rank** | **orders** — symbol, side, quantity |
| Stops at | `score()` | `build_plan()` |
| Green when | the top-25 delta table is empty | the order diff is empty |
| Gates | M13 opening at all | `SCAN_SOURCE_DEFAULT=generated` |
| Reads | the oracle CSV + Postgres, via `momentum_scan.build` | the same, via `app.scan_source.generate` |

`desk_parity.py` is strictly upstream. It can be green while shadow mode is red, because a rank that
does not move can still carry a score that moves enough to change a weight, and a weight change of
0.15 pp is a share.

The harness itself:

1. **One oracle, two scans.** `load_scan()` reads the uploaded CSV. `scan_source.generate()` builds
   the same thirty columns from the screener's Postgres for the same `as_of`, through
   `baskfy_core.momentum_scan.build` — the real pipeline, not a reproduction of it.
2. **Four columns are carried, not computed.** `marketcap`, `beta`, `circuits_*` and `is_nifty_fno`
   are taken from the upload and injected into the generated scan (`CARRIED_COLUMNS`), because
   nothing in either repository computes them. Every remaining difference is therefore attributable
   to the factor engine, which is the thing under test — and equally, the harness says nothing about
   those four.
3. **Both scans go through the same scorer and the same planner.** `app.scoring.score` →
   `baskfy_core.basket.build_plan`, with the desk's own `app/config.py` on both sides. There is one
   set of sizing constants, so the 2026-08-14 change to the position caps cannot produce a delta.
4. **The book is fixed and empty, capital fixed at ₹10,00,000.** `BOOK = []`, `CASH = 1_000_000.0`.
   The only variable is the scan.
5. **Prices come from each scan's own `close`**, never live LTP — otherwise the runs would differ
   for a reason unrelated to the scans, and the harness would need a Kite session and market hours.
6. **Breadth is taken from each scan's own rows** (`breadth_override=None`), deliberately: feeding
   both sides the same external breadth would hide a real disagreement.
7. **"The same order" is the triple (symbol, action, delta)**, for every row with a non-zero delta.
   `_orders()` builds `{symbol: (action, delta)}`; `_diff()` reports any symbol where the two tuples
   are not identical. A ±1 share difference is a delta. A `HOLD` with delta 0 is not an order.
8. **One JSON line per run** appended to `data/shadow-mode.jsonl`, and exit code 1 when red.

Sizing is `tgt_qty = round(capital * weight / 100 / px)`, with `weight` rounded to 2 dp. So a delta
requires either a different `px` or a different `weight` — and `weight` is score-proportional, so it
traces back to `SCORE`, which traces back to the factors.

## 2. Today's count, against 22 Aug

```
$ cd kite-momentum-rebalancer && DRY_RUN=true \
    PYTHONPATH=<HEAD copy of packages/core/src> \
    .venv/bin/python -m scripts.shadow_mode --date 2026-08-18

generated scan 0db6fe59674293e7: 5 of 271 symbols carry an unadjusted corporate action
shadow mode 2026-08-18  scan_1787143663_Investing_001__1_.csv vs 0db6fe59674293e7: 2 ORDER DELTAS
  orders: 15 uploaded / 15 generated   breadth: 68.6 / 68.6   suspect symbols: 5
  SHILPAMED      uploaded=('BUY', 81)  generated=('BUY', 80)
  WELCORP        uploaded=('BUY', 35)  generated=('BUY', 36)
```

Run B, the same command without the pinned `PYTHONPATH`:

```
shadow mode 2026-08-18  scan_1787143663_Investing_001__1_.csv vs 0db6fe59674293e7: 3 ORDER DELTAS
  orders: 15 uploaded / 15 generated   breadth: 68.6 / 68.6   suspect symbols: 5
  SKYGOLD        uploaded=('BUY', 84)  generated=('BUY', 83)
  SYRMA          uploaded=('BUY', 45)  generated=('BUY', 46)
  WELCORP        uploaded=('BUY', 35)  generated=('BUY', 36)
```

| | 22 Aug 2026 | 31 Aug 2026, Run A | 31 Aug 2026, Run B |
|---|---:|---:|---:|
| Order deltas | 4 | **2** | **3** |
| Of which a substitution (a name bought on one side only) | 1 (2 rows) | **0** | **0** |
| Of which ±1 share | 2 | 2 | 3 |
| Orders, uploaded / generated | 15 / 15 | 15 / 15 | 15 / 15 |
| Breadth, uploaded / generated | 68.6 / 68.3 | **68.6 / 68.6** | 68.6 / 68.6 |
| Symbols flagged with an unadjusted corporate action | 41 | **5** | 5 |
| `screen_run_id` | `0db6fe59674293e7` | `0db6fe59674293e7` | `0db6fe59674293e7` |

Three things moved, all of them the right way:

* **The substitution is gone.** `SHILPAMED` is bought on both sides now, and `DIVISLAB` — which had
  taken its place — is out of the generated basket entirely. §4 traces this.
* **Breadth now agrees exactly** (68.6 / 68.6, from 68.6 / 68.3). The 0.3 pp gap was itself
  contaminated prices moving names across their 20-DMA.
* **The suspect-symbol banner fell 41 → 5**, which is M28's headline arriving in the orders.

Nothing got worse. The count did not reach zero.

`screen_run_id` is unchanged across all three runs because it hashes `definition | as_of |
data_version` — none of which M28 bumped. That is a real weakness: **the run id does not change when
the underlying bars are rewritten**, so an old plan can name a scan id whose prices no longer exist.
Worth filing; out of scope here.

## 3. Every delta, enumerated

Run A (the committed tree) — **2 deltas, both listed, count matches the harness output**:

| # | Symbol | Side | Qty, Baskfy (generated) | Qty, desk (uploaded) | Δ | Exact target, gen. / upl. | Cause |
|---|---|---|---:|---:|---:|---|---|
| 1 | SHILPAMED | BUY | 80 | 81 | −1 | 79.896 / 80.514 | Score 75.9 vs 76.4. No penalty threshold crossed; diffuse percentile drift — B_momentum −0.279, C_sharpe −0.370, D_consistency −0.069, E_liquidity +0.266. **Data/definition, sub-threshold.** |
| 2 | WELCORP | BUY | 36 | 35 | +1 | 36.096 / 35.366 | Score 81.1 vs 79.4. **F_penalty +2.00** — the desk's `rsi_one_month` is 79.86 (> 78 → −2), Baskfy's is 73.61 (no penalty). **A scoring rule fires on one side and not the other.** |

Run B (with leaf 1.4.1's window fix) — **3 deltas, all listed**:

| # | Symbol | Side | Qty, Baskfy (generated) | Qty, desk (uploaded) | Δ | Exact target, gen. / upl. | Cause |
|---|---|---|---:|---:|---:|---|---|
| 1 | SKYGOLD | BUY | 83 | 84 | −1 | 83.265 / 83.643 | Score 77.7 vs 77.9, rank 8 on **both** sides. No threshold crossed — both sides already carry the same −1.5 volatility penalty. Diffuse: B −0.055, C −0.034, D −0.009, E −0.091. **Rounding boundary.** |
| 2 | SYRMA | BUY | 46 | 45 | +1 | 45.530 / 45.464 | Score 80.1 vs 79.9, `F_penalty` 0 on both. Driven by `D_consistency` +0.183 — the positive-days percentiles. The two engines agree on target size to **0.15%** and land on opposite sides of a half share. **Pure rounding boundary.** |
| 3 | WELCORP | BUY | 36 | 35 | +1 | 36.148 / 35.366 | Score 81.3 vs 79.4, rank **2 vs 6**. `F_penalty` +2.00 — identical cause to Run A #2, the RSI-78 cliff. **Survives the window fix**, which is what makes it the real one. |

Every delta is a **quantity** difference on a name both sides buy. There is no symbol either side
buys alone, no side disagreement, and no sell. Reference prices are **identical to the paisa on both
sides for all fifteen orders**, so no delta is a price difference; all of them run through `weight`,
and therefore through `SCORE`.

### Checked against the rebalance discipline, and not explained by it

`kite-momentum-rebalancer/.claude/skills/momentum-rebalance/` defines the rules that can legitimately
suppress or generate an order. None of them accounts for a delta here — and the reason is itself a
scope limit worth stating:

| Rule | Applies here? |
|---|---|
| Retain within N+5; replace only on a 5–10+ pt edge; never exit on one weak week | **No — the book is empty.** With `BOOK = []` there are no keepers, no runners, no exits. The retention band, the `REPLACEMENT_EDGE` hurdle and the never-exit-on-one-week rule are all bypassed by construction. |
| Weekly turnover < 20–30% | No — an initial build, so turnover is 100% by definition on both sides. |
| Skip swaps whose edge < round-trip cost + tax | No — no swaps. |
| Runners capped, never added to | No — no holdings to be runners. |
| Max single 15% / min 6% | No — all thirty weights sit in 6.39–7.11%, no cap binds, and the constants are one `app/config.py` shared by both sides. |
| Cluster cap 25% | No — and it is not evaluated at all. `_load_clusters()` reads `data/sectors.csv`, which does not exist, so the cluster map is empty on **both** sides. It cannot cause a delta; it is also entirely untested by this harness. |
| Position ≤ 0.5–1% of median daily traded value | No — the largest position is ₹69k against a `MAX_POS_VS_DAY_VALUE` allowance in the lakhs. |

So all three deltas are genuine engine disagreements, not discipline. **But the harness only ever
exercises the initial-build path.** Every rule in the top half of that table — the ones that decide
whether a *held* position is sold — has never been through this comparison at all. That is the
largest single gap in what four green Fridays would prove.

### Where the RSI disagreement comes from

`reconciliation/PARITY-FAMILIES.md` already records it: `rsi_*` reproduces **0 of 268** cells at
every Wilder period in range, verdict "**definition unknown**". Baskfy computes a Wilder RSI whose
period is the window's trading-day count (~21 for one month), seeded with a simple mean; the
vendor's initialisation, and possibly its period, differ, and the export carries no intermediate
that could arbitrate. The generated RSI is systematically the lower of the two on this corpus:

| Symbol | `rsi_one_month`, desk | `rsi_one_month`, Baskfy | Gap | Crosses a threshold? |
|---|---:|---:|---:|---|
| WELCORP | 79.8612 | 73.6134 | −6.25 | **Yes — the 78 line. −2 points.** |
| SHILPAMED | 76.5749 | 73.5996 | −2.98 | No |
| DIVISLAB | 76.1102 | 72.8607 | −3.25 | No |
| AETHER | 68.1764 | 65.9397 | −2.24 | No |
| SYRMA | 64.3799 | 62.0934 | −2.29 | No |

That column is scored twice — `F_penalty` at 78 and 82, and `PARABOLIC_RSI = 82.0` in the planner,
which trims a held name to a runner. A ~3–6 point systematic gap around a hard 78/82 cliff is not a
rounding artifact. It is the one finding here that will recur every week.

### And one near miss worth naming

In **Run A**, SHILPAMED's generated `volatility_one_year` is **0.449614** against an `F_penalty`
cliff at **0.45** — it missed a −1.5 point penalty by **0.0004**. Leaf 1.4.1's window fix moves it to
**0.429818** in Run B, comfortably clear. The near miss was therefore transient, but it is the shape
that matters: a factor whose definition is unsettled, sitting four ten-thousandths from a hard
scoring threshold.

The window fix is also independently confirmed here. Recomputing SHILPAMED's annualised volatility
straight from `ohlcv_daily` — simple returns, sample stdev, √252 — gives:

| Window | Value |
|---|---:|
| last 252 returns | 0.452622 |
| last 247 returns | **0.429818** |
| last 252, log returns | 0.450385 |
| desk's export | **0.398002** |

Run B's 0.429818 matches the 247-return figure **to six decimal places**, which is exactly what
`bisect_left` → `bisect_right` should do: drop the anniversary day, 248 returns → 247. The desk's
0.398002 is not reachable by any of them — a 13% relative gap, and `volatility_*` is the other column
PARITY-FAMILIES marks "definition unknown" at 0/268.

Both sides' vol-scaled stops happen to agree closely anyway (SHILPAMED ₹712.6 on both, WELCORP
₹1708.1 vs ₹1707.6) because `stop_from_vol` clamps to 8–12% and most names clip.

## 4. The corporate-action substitution, traced

The 22 Aug run's substitution was:

```
SHILPAMED   uploaded=('BUY', 81)  generated=None
DIVISLAB    uploaded=None         generated=('BUY', 7)
```

**The action.** One row in `corporate_action`:

| Symbol | Type | Ex-date | Ratio | Source | Confirmed |
|---|---|---|---|---|---|
| SHILPAMED | `split` | 2025-10-03 | 2 : 1 | `ratio_recovery` — the ratio between the NSE bhavcopy exchange print and Kite's adjusted history (M24/M28) | yes |

Its `raw` payload is candid about what it is: *"Not sourced from a corporate-action feed. A split and
a bonus are the same price factor, so the type is a choice; the factor is not."* Ambiguous with a
1:1 bonus, and it does not matter — the price factor is 2.0 either way.

**What the desk did.** The vendor's export was already adjusted. Its `away_from_high_one_year` for
SHILPAMED read −1.89: the stock was a point and a half off its one-year high, and the desk bought 81
shares.

**What Baskfy did on 22 Aug.** `ohlcv_daily` held the raw exchange print, which steps 778.75 → 384.95
overnight on 2025-10-03. Baskfy read that as a −50.6% day. The one-year high stayed at the pre-split
level, so `away_from_high_one_year` came out around −50, past `MAX_AWAY_FROM_HIGH = −30`, and the
`far_from_high` filter **rejected SHILPAMED outright** — not scored it lower, rejected it. Its slot
went to the next eligible name, DIVISLAB, at 7 shares. SHILPAMED was one of 41 symbols in that
condition; 14 of them were being rejected the same way.

**What Baskfy does today.** M28 applied the factor. The bars now read:

| date | `close` (adjusted) | `close_raw` (exchange print) |
|---|---:|---:|
| 2025-10-01 | 389.3750 | 778.7500 |
| 2025-10-03 | 384.9500 | 384.9500 |
| 2025-10-06 | 377.4500 | 377.4500 |

The adjusted series is continuous across the ex-date and `close_raw` still carries the print, which
is house rule 6 satisfied rather than worked around. The largest daily move in SHILPAMED's trailing
year is now −15.68% on 2025-09-09, a genuine one; no residual jump survives.

Consequently `away_from_high_one_year` is **−1.890 on both sides, identical to three decimals**. The
filter passes, SHILPAMED is ranked #10 (desk) / #12 (Baskfy) in Run A and #10 / #10 in Run B,
and it is bought on both sides. The status page's post-M28 line for M12 — `SHILPAMED` #10 → #12 —
is the same observation one layer upstream.
DIVISLAB is out of the generated basket entirely.

**This specific delta no longer exists.** It was the only one of the four that was a substitution,
and it was the only one that was a data defect rather than a definitional one. What replaced it is a
−1 share difference on the same symbol, from percentile drift with no threshold crossed.

The residue: **five** symbols still carry an unadjusted action — `ARVIND`, `CANBK`, `DIACABS`,
`NMDC`, `PRIVISCL`. None of them reaches the basket on this Friday, so none produces an order delta
today. That is luck of the corpus, not a property of the system.

## 5. What the harness does not, and cannot, prove

Stated because the verdict below rests on it as much as on the count:

* **Nothing about execution.** Both plans are built; neither is sent. `packages/execution` — guards,
  risk, rate limits, journal, the gateway — is not in this comparison at all.
* **Nothing about selling.** The book is empty by construction, so exits, runners, the N+5 retention
  band, `REPLACEMENT_EDGE` and the parabolic-RSI trim are all untested. **The most dangerous order a
  momentum desk places is a sell, and this harness has never generated one.**
* **Nothing about the four carried columns.** `marketcap`, `beta`, `circuits_*`, `is_nifty_fno` are
  copied from the upload onto both sides. Four green Fridays would say the *factor engine* agrees. It
  would say nothing about those four, and `E_liquidity` and `F_penalty` both read them.
* **One Friday, one universe.** 271 symbols on 2026-08-18, and the same 271 on both sides because
  the generated scan is built from the upload's symbol list.
* **Nothing about the cluster cap.** `data/sectors.csv` is absent, so `clusters` is `{}` on both
  sides and the 25% cap is never evaluated. A green Friday says nothing about it.
* **Not four Fridays.** `docs/SHADOW-MODE.md` requires four consecutive green Fridays on four
  different weeks, and the counter is at **zero**. This is one red run on a re-measured old date.

## 6. Verdict

**No. Not yet — and the reason is not the count.**

The count is genuinely good. Two deltas out of fifteen orders, both one share, on a corpus where the
same harness found four nine days ago including a whole position substituted for another. Breadth is
exact. The corporate-action contamination that was "the biggest blocker in the project" is down from
41 symbols to 5 and produces no order delta today. Reference prices match to the paisa on all fifteen
orders. If the question were "has the factor engine converged on the desk's answers", the honest
answer is that it very nearly has.

The reason to keep the flag off is what the remaining deltas are made of, and what the harness has
not looked at.

**Residual risk 1 — a scoring rule that fires on one side and not the other.** WELCORP is not
rounding. `rsi_one_month` is 79.86 on the desk's export and 73.61 in Baskfy, and 78 is a hard cliff
worth −2 points. `PARITY-FAMILIES.md` already records `rsi_*` as reproducing 0 of 268 cells at every
parameter, definition unknown. So this is a **known, unresolved definitional gap sitting directly
under a threshold**, and the gap runs ~3–6 points in the same direction across the corpus. Any name
whose vendor RSI lands in roughly 78–84 will score differently. At 82 the consequence stops being a
weight and becomes a *decision*: `PARABOLIC_RSI = 82.0` trims a held position to a runner. Two
systems that disagree by 6 RSI points will, on some Friday, disagree about whether to trim a full
position — and that is a large order, not a share. This will recur every week until the RSI
definition is settled, and no number of green Fridays on an empty book will surface it.

**Residual risk 2 — the same shape, four ten-thousandths away, in volatility.** SHILPAMED's
generated `volatility_one_year` was 0.449614 in Run A against a penalty cliff at 0.45; the window fix
moved it to 0.429818 in Run B. `volatility_*` is the other column PARITY-FAMILIES marks 0/268,
definition unknown, and the desk's figure for the same stock is 13% lower and not reachable from the
bars by any tested combination. Today it costs nothing. A one-character change to a window moved it
by 4.4%, which is the point: it is one revision from crossing a threshold in either direction. The
same column also sets the GTT stop under non-negotiable #4.

**Residual risk 3 — the harness has never generated a sell.** `BOOK = []`. Every retention and exit
rule in the strategy is bypassed. The comparison covers the cheapest, most reversible order the desk
places and none of the expensive ones.

**Residual risk 4 — the answer moves when core moves.** This measurement changed from 2 to 3 deltas
inside one afternoon, from a one-character edit to `windows.py` in a sibling branch of work. That is
a correct fix and the count going up is not a regression — SKYGOLD and SYRMA are both within 0.4 of a
share of agreeing. But it means the protocol needs to name the commit each Friday's run was measured
at, or four green Fridays can be assembled from four different engines.

**What would change the answer.** Not a lower delta count on this corpus — the corpus is one Friday
and it is nearly exhausted as evidence. Specifically:

1. Settle `rsi_*` and `volatility_*` against a source the export can arbitrate, or accept that they
   are ours and re-tune the 78 / 82 / 0.45 / 0.55 thresholds to our own distribution rather than the
   vendor's. Today Baskfy applies the desk's thresholds to a different statistic.
2. Run the harness with a **real book** — the desk's own holdings — so exits, runners and the
   parabolic trim enter the comparison.
3. Four consecutive green Fridays on four different weeks, each stamped with the commit it ran at.
4. Bump `data_version` when bars are rewritten, so `screen_run_id` stops naming prices that no longer
   exist.

Until at least (1) and (2), Baskfy's order generation is trustworthy enough to **propose** a plan a
human reads — which is what `/analyze` already does — and not trustworthy enough to be the default
source the desk trades from unattended.

## 7. Reproducing this

```bash
# Run B — the working tree as it stands
cd kite-momentum-rebalancer
DRY_RUN=true .venv/bin/python -m scripts.shadow_mode --date 2026-08-18

# Run A — pinned to the committed tree
git archive 7d6b7fb3aa6d23dadb30d645d28884a8b7eb6d84 decile-blueprint/packages/core/src \
  | tar -x -C /tmp/pin
cd kite-momentum-rebalancer
DRY_RUN=true PYTHONPATH=/tmp/pin/decile-blueprint/packages/core/src \
  .venv/bin/python -m scripts.shadow_mode --date 2026-08-18
```

Each takes about four minutes; `scan_source._fetch` pulls every bar on or before `as_of` for every
instrument, 3.5M rows, because `symbols` is `None`.

Both runs above were logged to a scratch path via `--log`, so **`data/shadow-mode.jsonl` was not
appended to**: the Friday ledger still holds only the two 22 Aug lines, and the four-Friday counter
is untouched by this re-measurement. That is deliberate — this is a re-measurement of an old date,
not a Friday.

### Safety

* `SCAN_SOURCE_DEFAULT` is `upload` in `app/config.py`'s default and in `.env.example`; the effective
  value at run time was `upload`. **The flag was never flipped.**
* `DRY_RUN=true` on every invocation; `app.config.DRY_RUN` reads `True`.
* The harness cannot reach a broker. It imports `app.scan_source`, `app.rebalance`, `app.scoring` and
  `baskfy_core.momentum_scan` — no Kite client, no `packages/execution`, no gateway. This is asserted
  by test, not by inspection: `tests/test_breadth_and_shadow.py::test_the_harness_never_touches_a_broker`
  scans the module source for `place_order`, `OrderGateway`, `gateway(`, `kite(`, `core.gateway`.
* Both plans were built and neither was sent. No order, live or simulated, was placed.

```
$ .venv/bin/python -m pytest tests/test_breadth_and_shadow.py \
    -k "flag_is_off or never_touches_a_broker or diff_is_at_order_level or small_enough_to_ignore" -q
......                                                                   [100%]
6 passed, 11 deselected in 0.04s
```
