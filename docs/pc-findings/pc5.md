# PC5 — the momentum regime panel

Leaf of `docs/PORTFOLIO-COMMAND-CENTER.md` §6. Gates: `gates/pc5.md`, 8 of 8 met.

**Ships:** `apps/web/src/lib/portfolio/regime.ts` (pure),
`apps/web/src/components/portfolio/command/regime-panel.tsx`, and their two test files
(42 + 32 = 74 tests). **Not mounted into any page** — the parent wires it, contract below.

---

## 1. The prop contract — what the parent must fetch and pass

```tsx
import { RegimePanel } from "@/components/portfolio/command/regime-panel";

<RegimePanel
  regime={regime}                      // Schemas["RegimeOut"] | null
  today={today}                        // string | null — "YYYY-MM-DD" in Asia/Kolkata
  unavailableReason={regimeError}      // string | null | undefined
  className={undefined}                // string | undefined — optional
/>
```

| Prop | Type | Where it comes from |
|---|---|---|
| `regime` | `Schemas["RegimeOut"] \| null` | `fetchRegime` in `src/lib/desk/fetch.ts` → `GET /api/v1/desk/regime`. `null` when the call threw or the desk has evaluated nothing |
| `today` | `string \| null` | Today's date in **exchange time**, `YYYY-MM-DD`. `null` is a supported, honest value |
| `unavailableReason` | `string \| null \| undefined` | The `DeskUnavailable` message when `regime` is `null` |
| `className` | `string \| undefined` | Optional, for grid placement |

The component derives everything internally through `regimeReading()` — same idiom as
`CommandCenterScreen`, which takes the raw `overview` and calls `commandCenter()`. There is
nothing for the parent to compute.

**Suggested wiring** (the parent owns the page, so this is a suggestion, not an edit):

```tsx
let regime: Regime | null = null;
let regimeError: string | null = null;
try {
  regime = await fetchRegime();
} catch (error) {
  regimeError = error instanceof DeskUnavailable ? error.message : "the desk did not answer.";
}
```

### Two notes on the props

* **`today` must be exchange time, not the server's.** The module is pure and takes no clock, so
  the caller supplies it. Passing `null` is safe and explicit: the panel renders an *informational*
  notice saying the overdue check could not be made, and does **not** silently treat the evaluation
  as fresh. It never guesses.
* **`RegimeOut` is `Schemas["RegimeOut"]`, re-exported from `lib/portfolio/regime.ts`.**
  `src/lib/desk/fetch.ts`'s hand-written `Regime` interface is structurally compatible and can be
  passed directly. The generated type is stricter (it knows which fields are optional), which is
  why the panel and both test suites use it.

### What the parent does NOT need to pass

No `swing/config`, no `swing/market`, no exposure figures. Everything the panel shows comes from
the single `/desk/regime` payload. **No change to `src/lib/swing/fetch.ts` is required.**

---

## 2. The two sentences the brief asked for, and where each half comes from

> *"Risk reduced to R2 because market breadth weakened and the Smallcap index closed below its
> 50-DMA. Current exposure is 8 percentage points above target."*

| Half | Source | Not |
|---|---|---|
| "Risk reduced to R2" | `tier` + `previous_tier`, ranked by `RegimeTier.ordinal` | a narrative |
| "because breadth weakened … below its 50-DMA" | `reasons[]`, printed **verbatim** under a heading saying whose words they are | never paraphrased, never assembled from the numbers |
| "8 percentage points above target" | `actual_equity_pct − target_equity_cap_pct` | not a derived ratio |

**The gap uses the desk's own sign convention.** `regime_store.record_exposure` computes
`gap = actual_equity_pct - target_equity_cap_pct` before it writes the row, so the panel and the
desk can never disagree about which side of the cap the exposure is on. Over the cap is positive
and reads "above target"; under it is negative and reads "below target".

**The direction is taken from the *rounded* figure.** `70.02 − 70` is a real difference that
rounds to `0.0`, and a panel printing "0.0pp above target" would be lying by rounding. It reads
"on target". Held by a test.

---

## 3. Findings that §2.2 and §6.3 did not know

### 3.1 The missing figures are missing from the **response**, not from the product

§6.3 says `RegimeOut` "does not carry a candidate tier, per-index DMA distances, confirmation
progress, an algorithm version or a config hash". That is true, and it understates the situation in
a useful way: **the desk computes and stores every one of them.** `GET /api/v1/desk/regime`
(`services/api/src/baskfy_api/routers/desk.py`) simply does not select them.

| Figure | Where it actually lives |
|---|---|
| Candidate tier, and whether the re-risking limit clipped it | `regime_evaluations.raw_candidate_tier`, `.transition_limited` |
| NIFTY 50 / MIDCAP 150 / SMLCAP 250 against the 20-, 50-, 200-DMA | `regime_evaluations.input_snapshot_json.index_diagnostics` |
| Confirmation progress (`confirm_days` = 3 closes past a 150 bp buffer) | `MaSignal.confirming_closes`, inside the same snapshot |
| Breadth coverage, against `min_breadth_coverage_pct` (90) | `regime_evaluations.breadth_coverage_pct` |
| Pending execution against the cap | `regime_exposure.pending_buy_pct`, `.pending_sell_pct`, `.execution_status` |
| Algorithm version and config hash | `regime_evaluations.algorithm_version` (`regime/1.0.0`), `.config_hash` |

So the panel names each one with **the column that holds it and the change that would surface it**
(`DECLARED_UNAVAILABLE` in `regime.ts`), which is checkable against the router's select list rather
than being an assertion the reader has to take on trust. **Each is one route change away, not a
product gap.** That is a better answer than §2.2's "not real" and it is why this leaf's declared-
unavailable list reads as a backlog instead of a refusal.

**Recommendation for the API (not done here — outside this leaf's files):** widening `RegimeOut`
with `raw_candidate_tier`, `transition_limited`, `breadth_coverage_pct`, `algorithm_version`,
`config_hash`, the three `regime_exposure` execution columns, and `index_diagnostics` would make
six of this panel's blocked figures real, with no new computation anywhere.

### 3.2 The seventh missing field, and it is the one that hurts: `reason_codes`

The evaluation writes **both** `reason_codes_json` (stable codes — "never rename; add new ones
instead", an audit contract) and `reasons_json` (the rendered English). The route returns only the
second.

That matters for the **momentum sentinel's 50-DMA veto**, which the brief names. The veto's state
is not a field anywhere; the only trace of it in the response is the desk's own sentence. So the
panel matches the exact `DISPLAY_REASONS` strings, keyed by the reason code each comes from, and
the failure mode is chosen deliberately and one-sided:

* a recognised veto sentence → **In force**, with the desk's sentence quoted underneath;
* `SENTINEL_UNKNOWN`'s sentence → **Unconfirmed**;
* `R1_ALL_CONDITIONS_MET`'s sentence ("Health, breadth and the momentum sentinel all risk-on") →
  **Reported clear** — the only positive evidence available;
* anything else, including silence and a reworded sentence → **Not stated**, never "clear".

**Silence is genuinely ambiguous and the panel says so.** `apply_sentinel_floor` emits a sentinel
code only when the sentinel changes something, so an R2 evaluation with a healthy sentinel records
no sentinel sentence at all. The panel can under-report a veto it cannot see; it can never invent
one and can never read silence as reassurance.

**A deduction deliberately NOT made.** `resolve_new_buys` blocks entries whenever the sentinel is
below its 50-DMA, so `new_buys ∈ {full, half}` logically implies the veto was clear. That inference
is sound only while that rule holds, and a silent rule change would make the panel assert a market
fact it never read. It is not made. Exposing `reason_codes` would settle it exactly.

### 3.3 Two index vocabularies that must not be conflated

The **weekly desk's** regime reads three structural indices — `RegimeConfig.structural_indices`:
NIFTY 50, NIFTY MIDCAP 150, NIFTY SMLCAP 250 — with a momentum sentinel of **NIFTY 500 MOMENTUM
50** at its 50- and 200-DMA. The **swing** book's market gate reads NIFTY MidSmallcap 400 (SW17,
`67df9b4`). Two books, two questions.

Per the root `CLAUDE.md` section *"When the code and a doc disagree"*, the panel cites the desk's
own config as the source of the three names and a test asserts that neither "MidSmallcap 400" nor a
bare "NIFTY 500" appears in the regime panel's copy. Nothing here is pinned to a document.

### 3.4 `/regime`'s new-buy label has a dead branch (existing page, not fixed here)

The desk's `NewBuyMode` is `full | half | **blocked**` (`regime_view.NEW_BUY_LABEL` is
`{full, half, blocked}`). `src/app/(app)/regime/page.tsx` keys its explanatory sentence off
`BUYS = { full, half, **none** }`, so `BUYS["blocked"]` is `undefined` and **the sentence
explaining a blocked book never renders on the desk's own stance page** — precisely the state a
reader most needs explained. Its headline value survives only because "blocked" falls into the
ternary's else branch, which happens to print "None".

Two smaller things on the same line: `"none"` is a key the desk never writes, and a null
`new_buys` renders the bare en dash `–` with nothing behind it, which is the brief's §1 rule.

Not fixed by PC5: `page.tsx` is the parent's file (§6.1). **The parent has since taken this up** —
`BUYS` now keys on `blocked`, an unrecognised mode prints as written, and a null `new_buys` carries
a reason instead of the en dash. PC5's own module does the same three things.

### 3.5 R4 is not automatically an exit, and the panel reads the number to find out

`app/config.py` is explicit: *"R4 residual policy must be EXPLICIT. If you set max residual names
to 0, R4 becomes full cash — which is the honest configuration for 'exit everything', rather than
describing a 10% residual as a full exit."* The panel obeys the same rule from the recorded cap:
`0` reads as a full exit, anything above it reads as a residual holding, and a missing cap says the
question cannot be answered. Held by the `cap:` tests.

### 3.6 The ladder is context, never truth

`{R1: 100, R2: 70, R3: 40, R4: REGIME_R4_EQUITY_PCT}` from `kite-momentum-rebalancer/app/config.py`
is rendered in a collapsed table, labelled as the desk's configured defaults, with the R4 rung
citing `REGIME_R4_EQUITY_PCT` on its own row. The live cap is `target_equity_cap_pct` from the
evaluation and **nothing else** — a test sets the recorded cap to 55 on an R2 payload and asserts
the panel shows 55, and another asserts a *missing* cap renders its reason with no `70` anywhere
in it. `baskfy_core/sleeves.py:185`'s R4 = 0 fallback is documented in the module comment as a
fallback in a different module, which is a second reason not to guess.

### 3.7 `mode` decides whether the whole panel means anything, and it was easy to miss

`RegimeMode` is `observe | propose | enforce`, and in `observe` the desk **forces no selling**. A
tier and a cap shown without the mode read as though the portfolio had already been moved to the
cap. The mode is therefore a chip beside the tier and a sentence under the stance, not a footnote.

---

## 4. Vocabulary: `PORTFOLIO_REDESIGN.md` §8 bans "book"

`src/lib/__tests__/no-jargon.test.ts` bans "book" in user-visible strings (→ "Portfolio group").
The desk's momentum portfolio genuinely is a *book*, and renaming it "portfolio group" would be
wrong — a portfolio group is the reader's, and this is not the reader's. **The wording was changed
rather than the gate** ("the desk's portfolio", "its portfolio", "the exposure"), because the gate
file is not this leaf's to edit and the substituted wording is clearer to a retail reader anyway.
Zero offences remain in either PC5 file.

If the parent would rather the desk's book keep its name, the sanctioned route is an `ALLOWED`
entry in that test with a phrase and a reason — never a widened pattern.

---

## 5. Defects found and fixed while gating

1. **The heading changed with nothing to explain it.** An unrecognised tier (anything outside
   R1–R4) drops `current` to false, which flips the panel's heading to "Last recorded stance — not
   confirmed current" — with no notice saying why. It now raises its own `tier-unrecognised` notice
   with a next step. A silently changed heading is worse than a loud one.
2. **A tier change with an unrankable side was reported as "unchanged".** `movementOf` returned
   `"unchanged"` when either tier had no ordinal, which is the quieter of the two lies and still a
   lie — the tier *did* change and only the direction is unknown. There is now an `"unstated"`
   movement.
3. **The evaluation dates were a squashed meta line that vanished when empty.** "from 2026-09-04
   closing prices" simply disappeared if `signal_date` was null, so a reader could not tell a
   missing field from an omitted clause. Last evaluated / closing prices from / next evaluation are
   now three first-class `Metric` cells, each rendering its own reason when absent.
4. **Breadth's coverage caveat was only in a drawer.** Breadth is usable only when it covered
   enough of the universe, and the coverage is not in the response. The caveat now sits directly
   under the reading, where a reader meets the number, as well as in the declared-unavailable list.
5. **`key={reason}` on the reasons and evidence lists.** Two identical strings would collide. Keyed
   by index and content now.
6. **A test that asserted `not.toMatch(/—/)` over the whole panel.** Em dashes inside prose are not
   the defect; an element whose *entire* content is a dash is. The guard now walks every node in the
   rendered tree and asserts none of them is a bare `—`, `-`, `N/A`, `null` or `undefined` — a
   stronger test than the one it replaced, run against a payload with seven fields nulled.
7. **The advice scan could not be PC1's.** `attention-rail`'s `/\b(buy|sell)\b/i` would fail on the
   desk's own recorded sentence ("new entries blocked", "new buys"). The rule is not that the words
   are banned — it is that the panel must never **address the reader about their own money**. The
   scan is for second-person direction (`you should/must/may`), recommendation vocabulary
   (`we recommend`, `consider buying`), possessive framing (`your position`) and hype
   (`guaranteed`, `multibagger`, `target price`).

---

## 6. Gate ledger

| Gate | Result |
|---|---|
| G1 pure, every figure a `Metric` | met — grep returns 0; `every figure it emits is a Metric carrying a definition` |
| G2 gap in percentage points, direction in words | met — 12 tests |
| G3 R2 never an exit | met — 9 tests |
| G4 live cap from the API, ladder labelled | met — 5 tests |
| G5 five absent figures declared with reasons | met — 6 tests (six figures, not five) |
| G6 stale / manual / unreachable each explained | met — 8 tests |
| G7 reasons verbatim, nothing as advice | met — 4 tests |
| G8 lint + whole suite | met for PC5's files; three sibling failures attributed in `gates/pc5.md` |

**G8 attribution.** `pnpm run lint` is `tsc --noEmit && eslint . && check-shadowed-routes`. It
fails at `tsc` on five sibling files (PC2's `performance-workspace.test.tsx`; PC3's
`allocation-tab.tsx`, `overview-tab.tsx`; PC4's `rebalance-drawer.test.tsx`,
`rebalance-preview.test.ts`) and at `eslint` on PC3's `tab-strip.tsx`. `eslint` over PC5's four
files exits 0 and `tsc` reports nothing in them. The suite's one red file, `no-jargon.test.ts`,
carries 46 offences across PC2, PC3, PC4 and PC6 files and **none** in PC5's. No sibling file was
touched.
