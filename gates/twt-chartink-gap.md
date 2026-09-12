# TWT vs Chartink — if the detector ran, would it name Maulik's stocks?

**Leaf 6 of `PLAN-SCAN-SYNC.md`. 12 Sep 2026.** Read-only against the box. No deploy. No change to
core scan logic.

**The question this file does *not* answer.** Every `tw_*` table on the box is 0 rows because the
detector has never run there — the TWT code deployed today. That explains an empty screen and
explains **nothing** about the rule. The parent already established it and this leaf did not
re-measure it.

**The question this file answers, with a measurement.** Given the plant's own bars for
**2026-09-11**, which of the names Chartink's three-weeks-tight scan lists for that session does
the detector's reading produce, and for each name it does not, *which line refused it*.

---

## The answer, in three numbers

| | |
|---|---|
| Chartink's own names for 2026-09-11 | **63** |
| The detector's names for 2026-09-11 | **57** |
| In both | **52 — recall 82.5 %, precision 91.2 %** |

**Of the 20 names Maulik quoted, the detector produces 19.** The one it does not is `E2E`, and it
is a plant gap: the box holds **21 bars of the last 54 sessions** for E2E, and `SMA(Volume, 50)`
needs 45 of 50. Two of the names he quoted — `MATRIMONY` and `AYE` — are **not in Chartink's own
11 Sep export at all**, and the detector names them anyway (see G14).

**So the honest answer to "if it ran, would it find these names?" is yes — it finds 19 of the 20
you read out, and the misses are the data plant, not the rule.**

---

## Where the numbers come from, and the one thing that had to be borrowed

Nothing here re-implements a line of the scan. Every figure is `tools/twt/twt_scan.py`'s own
functions — `tight_state_parts`, `stock_days`, `chartink_stock_days`, `weekly_closes`,
`month_low_back`, `rolling_mean` — scored by `twt_recall.score`, exactly as `docs/twt/01-method.md`
§§1–2 prescribes. `tools/twt/twt_chartink_gap.py` is a **scorer**, and G3 asserts it holds no
threshold of its own.

What had to be borrowed is **bars**. The research panel `research/volume-breakout/data/panel.pkl`
ends **2026-09-09**; every local plant database on `localhost:5433` ends **2026-09-04**
(`baskfy_tw9_plant` and `baskfy_bt`, 3.73 M rows each; `baskfy_preseed` ends 2026-08-18). Neither
reaches 2026-09-11. The two missing sessions were read from the production box —
`SELECT` only, via `tools/deploy/box.sh`, paged because SSM caps a response at 24,000 characters —
and spliced onto the panel's right edge by `tools/twt/twt-chartink-pull.sh`.

### And the second splice, which changed the answer

**The 1.7 GB research panel is thinner than the plant for some names, and scoring those as misses
would blame the scan for the panel's gap.** `SIGIND` is the proof: the panel carries **7** bars of
it, first 2026-08-27; the box carries **1,917**, first 2018-08-29. Run on the panel alone SIGIND is
a miss for want of a volume window; run with the box's own bars it is **found**. `--repair`
overwrites every Chartink name's bars from 2026-04-01 with the box's, which covers both the
50-session volume window and the month-3 low. G10 is that difference, measured.

---

## The 63, grouped by what happened

| class | n | names |
|---|---:|---|
| **Found** | **52** | 3BBLACKBIO ACE AKUMS AMAGI APARINDS ARMANFIN ASKAUTOLTD AZAD BANG BLUEJET CARTRADE CUPID DEVYANI DIVISLAB EMSLIMITED ENTERO ETERNAL GABRIEL GLAND GNA IPCALAB JINDALSAW KALYANKJIL KAMATHOTEL KDDL KENNAMET LAMBODHARA LUMAXTECH MARKSANS MMFL MPSLTD MSTCLTD NAZARA NPST OPTIEMUS ORIENTHOT PARAS POLYMED PRICOLLTD PRUDENT PVRINOX RPTECH SAMBHV SAPPHIRE SIGIND TBOTEK TFCILTD THELEELA TVSSRICHAK UGARSUGAR UNIPARTS VIMTALABS |
| **Missed — the plant has no clean 50-session volume window** (`04` §2.2 needs 45 of 50; the box has the same gap, G9) | **6** | ASAHISONG (28 of 54) · E2E (21) · KCPSUGIND (21) · KRN (24) · TIRUPATIFL (33) · SCPL (48, but 44 of the 50 the window spans — one short) |
| **Missed — genuinely too new for a 50-session window** (not a gap; no such history exists anywhere) | **1** | EMAMIPAP — first bar **2026-07-27**, 32 bars in total |
| **Missed — the month-3 (June 2026) low is missing or built from too few bars** | **2** | SHANKARA (**0** June bars → the rule's denominator does not exist) · ORBTEXP (**2** June bars → the min is biased high; 237.00 / 212.55 = 1.115 against the 1.30 the rule wants) |
| **Missed — near-miss at the 3.01 % tightness edge** (the only genuine rule disagreement) | **2** | ALIVUS **3.092 %** · AVALON **3.091 %** |

**Nine of the eleven misses are the data plant. Two are the rule, and both are within 0.09 of a
percentage point of the threshold.** Not one is unexplained, and not one is a bug in the scan.

`KCPSUGIND` is double-blocked and worth naming: even given a volume window it reads **3.313 %**
tight and would still be refused.

### The five the detector names and Chartink's 11 Sep export does not

`AYE · ECLERX · IOLCP · MATRIMONY · SUDARSCHEM`. All five have a full 50 of 50 bars and pass every
line. `AYE`, `IOLCP` and `SUDARSCHEM` appear in Chartink's export on other sessions (AYE 17–21 Aug,
IOLCP 17–21 Aug, SUDARSCHEM 27–31 Jul); `ECLERX` and `MATRIMONY` appear **nowhere** in the whole
21 Jan → 11 Sep export. **And `AYE` and `MATRIMONY` are in the list Maulik read out.** That is the
strongest single piece of evidence in this file: the two names the scorer counts against us as
false positives are names Maulik's own screen shows.

---

## T4's 64.9 % — it reproduces exactly, and it is the wrong number to hold a Friday to

`NEEDS-MAULIK.md` T4 says the point-in-time reading reproduces **64.9 %** of Chartink's stock-days,
with **832** no-bar days and **602** names lacking a clean 50-session volume window.

**It is not stale. It reproduces to the digit** (G11) — 64.9 % at 61.5 % precision, `no_bar` 832,
`no_volume_average` 602, `not_tight` 1741, `not_above_month_low` 73, `below_a_floor` 1; and the
look-ahead reading reproduces 83.1 % at 97.8 %. Nothing in today's evidence contradicts it.

**But it is an average over all five weekdays, and applying it to a Friday understates the sleeve
by about eighteen points.** Split by weekday over the same 9,254 stock-days (G12):

| reading | Mon | Tue | Wed | Thu | **Fri** | all |
|---|---|---|---|---|---|---|
| point-in-time (what the sleeve ships) | 61.9 / 45.7 | 60.0 / 51.5 | 57.7 / 58.5 | 64.4 / 73.7 | **83.4 / 98.8** | 64.9 / 61.5 |
| look-ahead (what Chartink's backtester does) | 83.0 / 97.1 | 82.8 / 97.1 | 82.4 / 97.2 | 83.7 / 99.0 | **83.5 / 98.9** | 83.1 / 97.8 |

recall / precision, per cent.

**The reason is `01` §2's own finding, followed one step further than `01` §2 follows it.** Chartink
evaluates a *completed* weekly candle, so on Monday it knows Friday's close. **On a Friday there is
nothing left to know** — today's close *is* the week's final close — so the two readings are the
same function, and the measurement says so: on 2026-09-11 `point_in_time` and `look_ahead` produce
**byte-identical output** (G13).

**What this changes in practice.** `docs/twt/05` §1.2 warns the user that `/twt` will list fewer
names than Chartink. That warning is right Monday to Thursday and close to wrong on a Friday — and
the weekly rebalance is a Friday job. Quoting "we only reproduce 65 % of Chartink" at Maulik on a
Friday would have been a true sentence used to explain the wrong thing.

---

## The gap between "Maulik's 68" and the 63 this file scored — say it out loud

**Chartink's export in this repo holds 63 names for 11-09-2026, not 68**, and
`research/tight-close/chartink_today.csv` (captured 11 Sep, 15:40 IST) holds the *same* 63 names.
Maulik's 68 includes `MATRIMONY` and `AYE`, which are in neither. **His 68 is therefore a fresh,
live 12 Sep scan**, and the repo has no export of it (G14).

So the table above is scored against the **11 Sep** answer key, which is the one that exists, and
against **the 20 names of the 68 Maulik read out**, of which the detector produces 19. The full
68 × 68 reconciliation is **not measurable here** and is `NEEDS-MAULIK.md` T4-b: a Chartink export
of the 12 Sep run, which only Maulik's browser session can produce.

---

## Ledger — 16 / 16

- [x] G1: **Neither the research panel nor any local plant reaches 2026-09-11**, which is why a box
      read was needed at all. The local plants end a full week earlier.
  CHECK: docker exec -e PGPASSWORD=baskfy baskfy-postgres psql -U baskfy -h localhost -p 5432 -d baskfy_bt -tAc "select max(date) from ohlcv_daily"
  EXPECT: /^2026-09-0[0-9]$/m
  EVIDENCE: `baskfy_bt` max 2026-09-04 (3,731,298 rows, 4,855 instruments); `baskfy_tw9_plant` max 2026-09-04 (3,731,218 rows, 4,785); `baskfy_preseed` and `baskfy_d1_probe` max 2026-08-18. `panel.pkl` last session **2026-09-09** (4,186 instruments × 2,396 sessions).

- [x] G2: **The box's 2026-09-10 and 2026-09-11 bars were read and arrived complete** — the pulled
      row count equals the box's own count for those two sessions, so no SSM page was lost.
  CHECK: wc -l < /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/data/outputs/twt/box_topup.psv
  EXPECT: /8736/
  EVIDENCE: box `select date, count(*) from ohlcv_daily` → 2026-09-10 **4,378**, 2026-09-11 **4,358**; 4,378 + 4,358 = **8,736**, the line count of the cached PSV. The repair pull is 1,333 bars over 17 names from 2026-04-01.

- [x] G3: **The measurement is a scorer, not a scanner.** It holds no threshold of its own and
      never assigns to one of `twt_scan`'s, so it cannot tune the rule it is measuring
      (`PLAN-SCAN-SYNC` leaf 6: *"may not change core scan logic"*).
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -cE '3\.01|10_000|MIN_CLOSE_RAW *=|TIGHT_PCT *=|VOL_SMA_BARS *=|scan\.[A-Z_]+ *=' tools/twt/twt_chartink_gap.py; grep -cE '^import twt_scan as scan$' tools/twt/twt_chartink_gap.py
  EXPECT: /^0\n1$/m
  EVIDENCE: 0 thresholds, 0 assignments into `twt_scan`, 1 import of it. `git status` shows no modification to `tools/twt/twt_scan.py`, `twt_panel.py`, `twt_recall.py` or `packages/core/src/baskfy_core/twt/`.

- [x] G4: **The detector, run locally over the plant's bars for 2026-09-11, produces 57 names.**
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python ../tools/twt/twt_chartink_gap.py measure --session 2026-09-11 2>/dev/null | grep point_in_time
  EXPECT: /produced 57 expected 63 both 52/
  EVIDENCE: `point_in_time  produced 57 expected 63 both 52 recall 82.5% precision 91.2%`.

- [x] G5: **52 of Chartink's 63 are found — 82.5 % recall at 91.2 % precision** on this session.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python ../tools/twt/twt_chartink_gap.py measure --session 2026-09-11 2>/dev/null | grep -c "recall 82.5% precision 91.2%"
  EXPECT: /^2$/m
  EVIDENCE: two lines, because on a Friday both readings are the same function (G13).

- [x] G6: **Every one of the 11 misses is attributed to the line that refused it.** No bucket is
      "unexplained", which is the bucket that would mean a defect.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python ../tools/twt/twt_chartink_gap.py measure --session 2026-09-11 --json 2>/dev/null | python3 -c "import json,sys,collections; d=json.load(sys.stdin)['point_in_time']; print(sorted(collections.Counter(v['verdict'] for v in d['per_name'].values()).items()))"
      ⚠️ **`MUST NOT MATCH` is not a thing the runner understands.** It parses `/regex/flags`
      and nothing else, so this EXPECT was read as a literal pattern and the row could never
      pass. The absence of an `unexplained` class is pinned by naming the whole breakdown —
      stronger than a negation, because it also catches a miss silently changing class.
  EXPECT: /^\[\('FOUND', 52\), \('no_volume_average', 7\), \('not_above_month_low', 2\), \('not_tight', 2\)\]$/m
  EVIDENCE: `[('FOUND', 52), ('no_volume_average', 7), ('not_above_month_low', 2), ('not_tight', 2)]` — 63 names, 52 found, 11 missed, 0 unexplained, 0 unknown_symbol, 0 no_bar, 0 below_a_floor.

- [x] G7: **19 of the 20 names Maulik read out are in the detector's 57.**
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python ../tools/twt/twt_chartink_gap.py measure --session 2026-09-11 --json 2>/dev/null | python3 -c "import json,sys; p=set(json.load(sys.stdin)['point_in_time']['produced_names']); m='UGARSUGAR AKUMS PRICOLLTD AZAD LUMAXTECH PRUDENT BANG CARTRADE RPTECH OPTIEMUS DEVYANI PVRINOX ENTERO AMAGI ORIENTHOT CUPID APARINDS MATRIMONY AYE E2E'.split(); print(sum(s in p for s in m), [s for s in m if s not in p])"
  EXPECT: /^19 \['E2E'\]$/m
  EVIDENCE: `19 ['E2E']`.

- [x] G8: **The one name of the twenty that is missed is missed for want of bars, not for want of
      tightness.** E2E reads 2.469 % tight — inside the 3.01 % rule — and is refused only because
      the volume window is not valid.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python ../tools/twt/twt_chartink_gap.py measure --session 2026-09-11 --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['point_in_time']['per_name']['E2E'])"
  EXPECT: /'verdict': 'no_volume_average'/
  EVIDENCE: `{'verdict': 'no_volume_average', 'tight_pct': 2.469, 'bars_in_50': 21, 'volume_sma_50': None, 'close': 610.35, 'close_raw': 610.35, 'month_3_low': None}`.

- [x] G9: **The gap is the plant's, not the panel's** — the production box has the same holes for
      the names bucketed as data gaps, so a detector running on the box would miss them too.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/box-sql.sh "select count(*) from ohlcv_daily o join instrument i on i.id=o.instrument_id where i.symbol='E2E' and o.date>='2026-06-30'"
  EXPECT: /^2[0-9]$/m
  EVIDENCE: bars held by the box since 2026-06-30, of **54** sessions — ASAHISONG 28 (June bars 0) · E2E 21 (0) · KCPSUGIND 21 (0) · KRN 24 (0) · TIRUPATIFL 33 (19) · SCPL 48 (11) · SHANKARA 45 (**0 June**) · ORBTEXP 51 (**2 June**) · EMAMIPAP 32 (first bar 2026-07-27, 32 in total) — against ALIVUS 54 (21) and AVALON 54 (21), which are complete and are the two genuine near-misses.

- [x] G10: **The research panel is thinner than the plant, and it changed the score.** Scored on
      `panel.pkl` alone the answer is 51/63; with the box's own bars for the disputed names it is
      52/63. `SIGIND` is the whole difference — 7 panel bars against 1,917 on the box.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python ../tools/twt/twt_chartink_gap.py measure --session 2026-09-11 --no-repair 2>/dev/null | grep point_in_time
  EXPECT: /produced 56 expected 63 both 51/
  EVIDENCE: `--no-repair` → `produced 56 expected 63 both 51 recall 81.0% precision 91.1%`, with SIGIND in the `no_volume_average` bucket; with repair → 57/52, SIGIND found. Box: `SIGIND` 1,917 bars from 2018-08-29, 50 of the last 55 sessions.

- [x] G11: **T4's published numbers reproduce exactly. The measurement is not stale.**
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python ../tools/twt/twt_chartink_gap.py weekdays --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['point_in_time']['overall'], d['point_in_time']['miss_reasons']['no_bar'], d['point_in_time']['miss_reasons']['no_volume_average'], d['look_ahead']['overall']['recall_pct'])"
  EXPECT: /'recall_pct': 64.9.*832 602 83.1/
  EVIDENCE: window 2026-01-21 → 2026-09-09, 9,254 Chartink stock-days. point-in-time produced 9,769 / matched 6,005 → **64.9 % at 61.5 %**; misses `not_tight` 1741, `no_bar` **832**, `no_volume_average` **602**, `not_above_month_low` 73, `below_a_floor` 1. look-ahead → **83.1 % at 97.8 %**. Every figure is T4's, to the digit.

- [x] G12: **…and it is an all-weekday average that does not describe a Friday.** On Fridays the
      point-in-time reading reaches 83.4 % recall at 98.8 % precision — level with the look-ahead
      reading — against 57.7 % on a Wednesday.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python ../tools/twt/twt_chartink_gap.py weekdays 2>/dev/null | grep point_in_time
  EXPECT: /Fri 83.4\/98.8/
  EVIDENCE: `point_in_time  recall 64.9% precision 61.5%   Mon 61.9/45.7  Tue 60.0/51.5  Wed 57.7/58.5  Thu 64.4/73.7  Fri 83.4/98.8`, against `look_ahead … Fri 83.5/98.9`. Fridays are 1,623 of the 9,254 stock-days.

- [x] G13: **2026-09-11 is a Friday, so the two readings are one function**, and the run proves it
      rather than asserting it: identical produced/matched/recall/precision and identical miss
      buckets.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python ../tools/twt/twt_chartink_gap.py measure --session 2026-09-11 --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); a,b=d['point_in_time'],d['look_ahead']; print(d['weekday'], a['produced_names']==b['produced_names'] and a['per_name']==b['per_name'])"
  EXPECT: /^Fri True$/m
  EVIDENCE: `Fri True`. This is why the "we see fewer names than Chartink" caveat must not be quoted at a Friday rebalance.

- [x] G14: **"Maulik's 68" is not the 11 Sep set, and the repo cannot score it.** Chartink's export
      holds **63** names for 11-09-2026; `chartink_today.csv` holds the same 63; `MATRIMONY` and
      `AYE` are in neither, so the 68 is a 12 Sep run. The detector names both anyway.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && python3 -c "import csv,io; rows=[r for r in csv.reader(io.open('research/tight-close/chartink_backtest.csv',encoding='utf-8-sig')) if r and r[0]=='11-09-2026']; s={r[1] for r in rows}; print(len(s), 'MATRIMONY' in s, 'AYE' in s)"
  EXPECT: /^63 False False$/m
  EVIDENCE: `63 False False`. The 63 of the backtest export and the 63 of `chartink_today.csv` are the **same set** (zero either way). The full 68 × 68 reconciliation is recorded as `NEEDS-MAULIK.md` T4-b — it needs an export only Maulik's browser session can make.

- [x] G15: **Read-only, as leaf 6 is required to be.** No write reached the box's database, no
      container was restarted, nothing was deployed, and no order path was touched.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -vE '^[[:space:]]*#' tools/twt/twt-chartink-pull.sh tools/twt/twt_chartink_gap.py | grep -ciE 'push-images|deploy-swing|insert |update |delete |place_order|OrderGateway|alter table|BASKFY_TWT_EXECUTION_ENABLED|sleeve_capital'
  EXPECT: /^0$/m
  EVIDENCE: 0. Every box call is a `SELECT` through `tools/deploy/box.sh`; the only mutation anywhere on the box was a `/tmp/twt_pull_*.b64` scratch file created to page a large result past SSM's 24,000-character response cap, removed by the same script before it exits (`rm -f`, and verified by hand: `cleaned`). `tw_config`, `BASKFY_TWT_EXECUTION_ENABLED` and every `tw_*` table are untouched and still 0 rows.

- [x] G16: **The new tool meets the house rules**: lint clean, types clean, namespace clean, and the
      plant bars it caches cannot be committed.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run ruff check ../tools/twt/twt_chartink_gap.py --output-format=concise && uv run ruff format --check ../tools/twt/twt_chartink_gap.py && uv run mypy ../tools/twt/twt_chartink_gap.py && cd .. && git check-ignore -q decile-blueprint/data/outputs/twt/box_topup.psv && echo CLEAN
  EXPECT: /CLEAN/
  EVIDENCE: `All checks passed!`, `1 file already formatted`, `Success: no issues found in 1 source file`, `tools/check-namespace.sh` OK, and `decile-blueprint/data/outputs/twt/.gitignore` keeps the pulled bars and the derived JSON out of history (house rule: `data/` stays untracked). No `# type: ignore`, no `Any`, no swallowed exception.

---

## What this leaf deliberately did NOT do

* **It did not run the detector on the box.** `tw_*` stays at 0 rows; writing a `tw_signal_daily`
  row is leaf 4's route and the parent's deploy, not a measurement's side effect.
* **It did not tune anything.** `ALIVUS` at 3.092 % and `AVALON` at 3.091 % are 0.08 of a point
  outside a threshold that `docs/twt/01` §1 measured against alternatives and chose. Widening
  3.01 to 3.10 would have "found" two more names today and is exactly the move
  `PLAN-SCAN-SYNC` forbids this leaf. It is recorded here as a *measurement*, not a proposal.
* **It did not touch `docs/twt/05` §1.2's caveat**, which the Friday finding makes only partly
  true. The copy is leaf 5's column. The finding is written up here and in `NEEDS-MAULIK.md` T4 so
  whoever owns that page has the number.
