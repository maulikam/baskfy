# PARITY-M13 — is the generated scan the same universe the desk has been trading?

**Leaf 1.4.2 of the desk-retirement tree.** M13 built `MomentumScan` to replace the weekly CSV
exported by hand from `momoindiascreener.in`. It has never been switched on, because
`docs/00-merge-status.md` and `docs/DECISIONS-MERGE.md` §M13.1 record that *"a generated scan
passes 223 symbols where the upload passes 239"*. Those sixteen symbols are the reason the cord is
still attached.

This document does not restate that figure. It measures the gap today, names every symbol in it,
gives each a cause, and returns a verdict.

---

## 0. Measured at

A sibling leaf (1.4.1) was editing `packages/core` **while this ran**, so the state is pinned
rather than assumed. Anyone re-running this must reproduce this state or expect different numbers.

| | |
|---|---|
| git HEAD | `7d6b7fb3aa6d23dadb30d645d28884a8b7eb6d84` (branch `developer`) |
| working tree | **dirty in `packages/core`** — `windows.py` carries 1.4.1's uncommitted fix |
| `git diff -- packages/core` sha256 | `4f69fa4f…09ee` (final run) / `c6677553…9e29` (earlier runs; `windows.py` byte-identical between them, only a test fixture moved) |
| screener DB | `ohlcv_daily` 3,534,860 bars, 2017-01-02 → 2026-08-21; `corporate_action` 289 rows; `instrument` 10,481 |
| oracle | `kite-momentum-rebalancer/data/uploads/scan_1787143663_Investing_001__1_.csv` — 271 rows, as-of 2026-08-18, read-only |
| `screen_run_id` | `0db6fe59674293e7` |
| wall clock | 2026-08-31 17:54 IST |

**The tree moved under the first measurement and the result changed.** That is recorded here
rather than smoothed over, because it is the single most important caveat in this document:
the first run of the harness returned **237 vs 239**, and a re-run twenty minutes later returned
**239 vs 239**. Nothing in this leaf changed; 1.4.1 landed its `windows.py` fix in between.
§4 measures both states deliberately so the result does not depend on when you happened to look.

The 22 Aug baseline for comparison: `ohlcv_daily` held 1,145,922 bars from 2024-01-01 and
`corporate_action` held **four** rows. M24/M27/M28 have since tripled the bar history, pushed it
back three years, and taken corporate actions from 4 to 289.

---

## 1. Current measured counts

"Passes" means the row survives `baskfy_core.score.apply_filters` — the six hard eligibility
filters in `kite-momentum-rebalancer/.claude/skills/momentum-rebalance/SKILL.md` (below both
50- and 200-DMA; 3M and 6M returns both negative; `circuits_three_months > 5`;
`median_volume_one_year < ₹5cr`; `away_from_high_one_year < −30`; series in `{BE, BZ}`), plus the
`EXCLUDED_SYMBOLS` guard. This is the same measure §M13.1 used, so 223/239 and the figures below
are comparable.

Both frames go through the desk's **unmodified** `app/scoring.py` config, imported rather than
restated. The generated frame comes through the real `baskfy_core.momentum_scan.build` contract,
not a reimplementation of it.

```
$ cd decile-blueprint && uv run python <harness>          # reconciliation/desk_parity.py machinery

ORACLE  scan_1787143663_Investing_001__1_.csv  rows=271  as_of=2026-08-18
screen_run_id: 0db6fe59674293e7  (definition + as_of + data_version)
GENERATED rows=271

=== G1: ELIGIBLE-UNIVERSE COUNTS ===
  upload    passes apply_filters: 239
  generated passes apply_filters: 239
  net gap            : 0
  symmetric difference: 0  (upload-only 0, generated-only 0)
```

| | 22 Aug 2026 (recorded) | 31 Aug 2026 (measured) |
|---|---:|---:|
| upload passes | 239 | **239** |
| generated passes | 223 | **239** |
| net gap | 16 | **0** |
| symmetric difference | (not recorded) | **0** |
| symbols in the upload absent from the generated frame | (not recorded) | **0** |

Reproduced three times in one session, including once after the sibling leaf touched the tree
again. `239 / 239` each time.

**The 16-symbol gap is closed. The status page's figure is nine days stale.**

---

## 2. Every differing symbol, enumerated

### 2a. The gap table

The measured gap is **0**, so the table of symbols in the gap has **0 rows**. Stated explicitly so
the arithmetic can be checked: `len(upload_passes) − len(generated_passes) = 239 − 239 = 0`;
`|upload_passes △ generated_passes| = 0`; **rows in the gap table = 0 = the measured gap.**

An empty table is a claim, not a finding, so §2b enumerates instead **every symbol in the 271-row
corpus that differs between the two paths in any respect that a desk decision could touch** —
eligibility, reject reason, or contaminated inputs. That list is complete: no symbol outside it
differs in any of those three ways.

### 2b. Every symbol that differs, by name, with cause and direction

Vocabulary as specified: `missing bars` · `missing corporate action` · `filter difference` ·
`marketcap/fundamental gap` · `series difference` · `unexplained`.

| symbol | differs how | direction | cause | live today? |
|---|---|---|---|---|
| ASTRAL | eligible in the upload, rejected `neg3M&6M` in the generated scan — **under the pre-1.4.1 window convention only** | upload-only (coverage we lacked) | filter difference — window anchor off by one bar | **no** — closed by 1.4.1 |
| HINDCOPPER | eligible in the upload, rejected `neg3M&6M` in the generated scan — **pre-1.4.1 only** | upload-only (coverage we lacked) | filter difference — window anchor off by one bar | **no** — closed by 1.4.1 |
| DIACABS | rejected by both, but for more reasons generated (`+illiquid;far_from_high`) | neither — verdict agrees | missing bars (11 of 121 bars in the 6M window) compounded by missing corporate action | yes, harmless |
| INOXGREEN | rejected by both, but for fewer reasons generated (`−neg3M&6M`) | neither — verdict agrees | missing bars (10 of 121 bars in the 6M window) | yes, harmless |
| ARVIND | flagged `suspect` — a −56.4% overnight step on 2018-11-28 | neither — verdict agrees, both eligible | missing corporate action (the Arvind Fashions demerger), **outside every factor window** | yes, no effect |
| CANBK | flagged `suspect` — a +40.9% step on 2017-10-25 | neither — verdict agrees, both eligible | missing corporate action, **outside every factor window** (the 2024-05-15 5:1 split *is* in `corporate_action` and is applied) | yes, no effect |
| NMDC | flagged `suspect` — a +85.7% step on 2022-10-27 | neither — verdict agrees, both eligible | missing corporate action, **outside every factor window** (the 2024-12-27 3:1 split *is* applied) | yes, no effect |
| PRIVISCL | flagged `suspect` — repeated ±100%/−50% steps through 2020 | neither — verdict agrees, both eligible | not a corporate action: the back-adjusted 2020 close rounds to ₹0.10/₹0.20 and flaps one tick. A rounding artefact of deep back-adjustment, **outside every factor window** | yes, no effect |

**Row count: 8. Unexplained: 0.** Every symbol carries a cause from the fixed vocabulary. Zero
rows sit in the `unexplained` category, and that is a measured result, not an omission — the four
`suspect` names were each chased to a dated price step and, where one exists, to the presence or
absence of the matching `corporate_action` row.

**Directions.** Both entries in the historical gap ran **one way only: upload-only** — coverage
the generated scan lacked, names the desk could trade that Baskfy would have gone blind to. There
was never a generated-only symbol: the generated scan has never at any measured point proposed a
name the desk's own upload rejected. That asymmetry is the reassuring direction to have been wrong
in, and it is worth stating because the two mean very different things.

---

## 3. Three symbols traced end to end

### ASTRAL — the mechanism, in full

`as_of = 2026-08-18`. The 3-month anniversary is `2026-05-18`, which **is** a trading day.

| | date | close | 3M return from it |
|---|---|---:|---:|
| engine, pre-1.4.1 (`bisect_left` — anniversary admitted to the window) | 2026-05-18 | 1545.70 | `1520/1545.70 − 1` = **−1.66%** |
| the export | 2026-05-19 | 1448.90 | `1520/1448.90 − 1` = **+4.91%** |

Both reproduce the two sides to the paisa: the generated frame carried `−1.66`, the upload carried
`+4.91`. ASTRAL fell 6.3% on 19 May, so a one-bar shift in the base was worth 6.57 points of
return. With `absolute_return_six_months` already negative on both sides (−6.23 upload, −7.12
generated), the sign flip on the 3M leg is what fired `neg3M&6M` and removed ASTRAL from the
tradeable universe. `close`, `ma_20`, `ma_50` and `away_from_high_one_year` matched **exactly** —
the signature of a window-boundary difference, not a data difference.

### HINDCOPPER — the same mechanism, independently

| | date | close | 3M return |
|---|---|---:|---:|
| engine, pre-1.4.1 | 2026-05-18 | 581.15 | **−2.55%** |
| the export | 2026-05-19 | 569.50 | **−0.55%** |

Identical structure, smaller move. HINDCOPPER's 6M return also crossed zero (+0.48 upload → −0.54
generated), so both legs went negative and the filter fired. Neither symbol is missing a single bar
in the window (64 of 64 market days for both) and neither has a `corporate_action` row — this was
never a data problem.

### DIACABS — a real data problem, that happens not to matter

| column | upload | generated |
|---|---:|---:|
| `ma_20` | 334.77 | 236.94 |
| `ma_200` | 176.03 | **898.85** |
| `absolute_return_six_months` | +173.57 | **−73.39** |
| `away_from_high_one_year` | −2.92 | **−80.63** |
| `median_volume_one_year` | ₹27.7cr | **₹0.99cr** |

Short windows wrong, long windows wildly wrong, volume off by 28×: DIACABS holds **11 bars out of
121** market days in the 6M window. It barely trades. The desk rejects it anyway
(`circuits;T2T_series;` — 6 circuits and a BE series), and the generated scan rejects it harder.
The verdict agrees; only the stated reason differs. This is the one name where the M12-era
contamination story is still visibly true, and it is confined to a symbol neither side would touch.

### Why the anniversary rule was the whole gap

The five window anniversaries for as-of 2026-08-18:

| window | anniversary | a trading day? | pre-1.4.1 vs export |
|---|---|---|---|
| 1M | 2026-07-18 | no (Saturday) | **agree** |
| 3M | 2026-05-18 | yes | differ by one bar |
| 6M | 2026-02-18 | yes | differ by one bar |
| 9M | 2025-11-18 | yes | differ by one bar |
| 12M | 2025-08-18 | yes | differ by one bar |

`resolve_window` snapped *forward inclusive* (`bisect_left`); the export snaps *forward exclusive*.
The two agree on every anniversary that falls on a weekend or holiday and differ by exactly one bar
otherwise. 1M was the single family that already reproduced — by accident, because 18 July 2026 was
a Saturday. 1.4.1 changed the line to `bisect_right` and documented the measurement; this leaf
found the same off-by-one independently from the other end, through ASTRAL's price series.

---

## 4. The result under both window conventions

Because the tree moved mid-measurement, both conventions were run against the same database and
the same corpus, patched at `resolve_window` in-process — **no file was edited by this leaf**:

```
CURRENT (bisect_right, 1.4.1): upload 239  generated 239  symdiff 0
PRE-1.4.1 (bisect_left):       upload 239  generated 237  symdiff 2
    upload-only    ASTRAL         generated_reject='neg3M&6M;'
    upload-only    HINDCOPPER     generated_reject='neg3M&6M;'
```

So the honest ledger of the gap over nine days is **16 → 2 → 0**: the corporate-action and
bar-depth backfills (M24/M27/M28) took it from 16 to 2, and 1.4.1's window fix took the last 2.

**This document's headline result is conditional on 1.4.1 landing.** If 1.4.1's `windows.py` change
is reverted or never committed, the gap is 2, not 0, and the two names are ASTRAL and HINDCOPPER.

---

## 5. What still disagrees, below the eligibility line

A zero universe gap is not cell-level parity, and pretending otherwise would be the same mistake
this exercise exists to prevent. Agreement per column across all 271 rows, at the export's own
0.02 precision:

| column | rows matching | median \|Δ\| | p90 \|Δ\| |
|---|---:|---:|---:|
| `close` | **271 / 271** | 0 | 0 |
| `absolute_return_one_month` | 267 | — | — |
| `absolute_return_three_months` | 265 | — | — |
| `absolute_return_six_months` | 263 | — | — |
| `ma_20` / `ma_50` / `ma_100` | 267 / 265 / 264 | — | — |
| `away_from_high_one_year` | 266 | — | — |
| **`absolute_return_nine_months`** | **3** | 1.39 pts | 4.61 pts |
| **`absolute_return_one_year`** | **5** | 1.54 pts | 5.64 pts |
| **`ma_200`** | **15** | 0.05% | 0.11% |
| **`median_volume_one_year`** | **10** | 1.1% | 3.7% |
| **`rsi_one_month`** | **1** | 4.12 pts | 11.03 pts |
| `sharpe_return_nine_months` / `_one_year` | 57 / 54 | 0.04 | 0.11 / 0.17 |

Two different things are in that table. `ma_200` and `median_volume_one_year` "fail" only against a
0.02 tolerance — their median disagreement is 0.05% and 1.1%, which is precision, not a defect.
The **9M and 12M return families, and `rsi_one_month`, are genuinely and systematically off**: a
1.4–1.5 point median on the long returns, a 4.1 point median on RSI.

Those columns do not feed any eligibility filter — the filters read 3M/6M returns, the MAs, volume,
`away_from_high`, circuits and series — which is why the universe agrees while the long windows do
not. They do feed the **score**: 9M and 1Y are 15% each of B_momentum and 15% each of C_sharpe, and
RSI drives the F_penalty band. That is exactly where it shows up (§6).

A likely contributor, found while tracing and worth a separate leaf: the trading calendar is
derived as `select distinct date from ohlcv_daily`, and **2026-02-01 carries bars for 21
instruments** against ~1,800 on a normal day. That phantom session inflates every window that
crosses it — 6M, 9M and 12M — by one day for the 262 of 271 symbols that did not trade it, while
the return base is a per-instrument `shift(N−1)`. Not diagnosed to completion here; flagged.

---

## 6. Does any of it reach an order?

The eligibility set is the input to the rebalance discipline, not the output. Under
`.claude/skills/momentum-rebalance`, a scan difference only becomes an order difference if it
survives the hold band and the replacement-edge hurdle.

| gate | constant | measured |
|---|---|---|
| `REPLACEMENT_EDGE` — challenger must beat incumbent by | 8.0 pts | **max \|ΔSCORE\| = 4.30 pts**; symbols at or above 8.0: **0** |
| `RETENTION_BUFFER` — hold band | N+5 | top-25 **25/25** shared; top-15 **15/15** shared |
| `TARGET_POSITIONS` | (12, 15) | both sides select from the same 15 names |
| sizing: `MAX_SINGLE_WEIGHT` / `MIN_POSITION_WEIGHT` / `CLUSTER_CAP` / `RUNNER_MAX_VALUE` | 15.0 / 6.0 / 25.0 / ₹3,00,000 | **identical by construction** — both paths import one `app/config.py`; the scan source cannot produce a sizing delta |

The largest score moves are `LUMAXTECH` +4.3 (#169→#152), `VARROC` +3.9 (#61→#47), `RKFORGE` +3.9
(#105→#87), `PGEL` −3.8 (#189→#200) — all deep in the tail, none inside the hold band. **No score
delta anywhere in the 239 reaches the 8-point replacement hurdle, so no swap decision can differ
between the two scan sources on this corpus.** The 2026-08-14 position-sizing change is not
implicated: both sides read the post-change constants from the same file.

Top-25 membership is also now **25/25**, against 23/25 in `reconciliation/DESK-PARITY.md`.
`ANANDRATHI`, `SHILPAMED`, `AKUMS` and `SHRIPISTON` — the four names that split the M12 top-25 —
now agree. SHILPAMED, M12's marquee failure at #10 → #57, reproduces: its `ma_100`, `ma_200` and
`absolute_return_six_months` all match the export.

---

## 7. Verdict

**Not yet — but the reason has changed, and the remaining reason is not the one on the status page.**

On the evidence measured here the generated scan is, for this corpus, decision-equivalent to the
upload: same 239 eligible names, same top-15, same top-25, no score delta within 3.7 points of the
hurdle that could flip a swap. The M13.1 objection — *"a desk that silently stopped seeing sixteen
names would look exactly like a desk that was working"* — no longer holds. It is answered.

It is still not safe to make the default, for one reason that has nothing to do with parity:

> **The generated path does not cut the CSV cord. It still requires one.**
>
> `app/main.py::_carried_columns()` sources `marketcap`, `beta`, `circuits_three_months`,
> `circuits_one_year`, `is_nifty_fno` and `series` from **the most recent uploaded scan CSV on
> disk**, and `momentum_scan.build` joins them onto the computed factors with an **inner join on
> `symbol`**. So the generated scan's universe is *defined by the last upload's symbol list*. The
> screener's database has bars for **2,540 instruments** on 2026-08-18; the generated scan returns
> **271**, because that is how many rows the last CSV had.
>
> This parity result is therefore measured on a comparison that holds the universe constant by
> construction. It answers *"given the same 271 names, do the two paths reach the same verdicts?"*
> — emphatically yes. It does not and cannot answer *"would Baskfy discover the same 271 names?"*,
> which is the question that actually retires the cord.

### Residual risks, named

1. **The cord is not cut (blocking).** Flipping `SCAN_SOURCE_DEFAULT` to `generated` today buys
   nothing operationally: without a fresh CSV the carried columns go stale, and
   `_carried_columns()` returns HTTP 503 when no upload exists at all. Sourcing marketcap, beta,
   circuits and F&O membership independently is the real M13 remainder. Until then the flag is
   cosmetic, and the parity above is a comparison, not a release.
2. **Stale carried columns (silent).** When the newest CSV is old, `marketcap` and `beta` are old
   too. They feed E_liquidity (up to 2 pts) and F_penalty (−2 for beta > 1.6). Recorded in the
   plan's provenance — but nothing expires them.
3. **Margin fragility at the eligibility line.** The set matches today with very little room:
   `GOKULAGRO` sits **0.00 points** from the 3M-return zero line, `LUPIN` 0.15 and `HINDCOPPER`
   0.48 from the 6M line; `BELRISE` is ₹0.46 and `EQUITASBNK` ₹0.72 below their MA lines; `BBOX` is
   0.48 points from the −30 `away_from_high` cut. With the 9M/12M returns still carrying a
   1.4-point median disagreement and the phantom 2026-02-01 session unresolved, **the zero gap is
   partly luck at the margin, and a different as-of date will not necessarily produce zero.** This
   result is one corpus on one date; it is not a standing guarantee.
4. **One corpus, one date.** Everything here is 2026-08-18. `sample_scan.csv` is synthetic and
   correctly excluded, so there is no second real export to cross-check against. A second weekly
   export would convert this from an observation into a trend.
5. **Long-window and RSI drift (§5), undiagnosed.** Harmless to eligibility today, inside the
   hysteresis band today. Not understood, and therefore not bounded for tomorrow.
6. **Depends on uncommitted work.** The headline 0 requires 1.4.1's `windows.py` fix, which was
   not committed at the time of measurement.

### Recommendation to the driver

Do not flip `SCAN_SOURCE_DEFAULT`. Not because the generated scan picks the wrong stocks — it
picks the same ones — but because it cannot yet run without the thing it was built to replace.
Update `docs/00-merge-status.md`: **"223 vs 239" is stale and should read "239 vs 239 as of
31 Aug 2026; the blocker is the carried-column dependency, not scan parity."** Leaving the old
figure in place would keep M13 blocked for a reason that has been fixed, while the reason it is
actually blocked goes unrecorded.

---

## Reproducing this

Requires the screener Postgres up on `localhost:5433` (`baskfy-postgres`) and 1.4.1's `windows.py`
in the tree.

```bash
cd decile-blueprint
uv run python reconciliation/desk_parity.py      # the committed top-25 harness — now 25/25
```

The eligibility-count and per-symbol comparison in §1–§2 is `desk_parity.py`'s `read_oracle()` and
`generate()` with `score.apply_filters` applied to both frames and the symbol sets differenced.
This leaf wrote no code into the repository: its scripts were scratch, and the only file it owns
is this one.

**No flag was flipped, no `.env` touched, no order placed, and the read-only regression corpus was
opened read-only.**
