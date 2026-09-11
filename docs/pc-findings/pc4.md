# PC4 — the rebalance preview drawer

**Leaf of** `docs/PORTFOLIO-COMMAND-CENTER.md` §6. **Gates:** `gates/pc4.md`.
**Owns:** `apps/web/src/lib/portfolio/rebalance-preview.ts`,
`apps/web/src/components/portfolio/rebalance/*`, and the two test files.
**Mounted nowhere.** The parent wires it; the contract is §3 below.

---

## 1. What the payload actually supports, and what it does not

`RebalanceOut` carries four lists of names — `entries`, `exits`, `inside_wrh`, `holds` — and
`target_weights`. `baskfy_core.rank_buffer._target_weights` builds the last as an **equal weight
over the post-rebalance portfolio, summing to exactly 1**, with the last name absorbing the
rounding remainder. There is no quantity in the response, no cash figure, no turnover and no cost.
`RebalanceOrderOut.planned_qty` exists in the schema but belongs to the desk's weekly plan, a
different object on a different route.

Against that, the brief's list for this drawer splits cleanly:

| Brief asks for | Status here |
|---|---|
| Current weight vs target weight, per name | **Real.** `DetailHoldingOut.weight` and `TargetWeightOut.weight` are both *fractions of the portfolio*, quantized server-side (`RETURN_PRECISION` and `WEIGHT_STEP`). Comparing them needs no price at all |
| The four lists, named and counted, with the rule behind each | **Real** |
| Which account holds each name | **Real** for anything held — `DetailHoldingOut.broker`. Not real for an entry, and the drawer says so rather than guessing |
| Concentration before and after (largest name, top five) | **Real** — arithmetic over the weights above |
| Name churn: entering, exiting, staying | **Real** |
| Delisted, unpriced, unreconciled, stale-`as_of`, short screen, holdings drift | **Real**, each named by symbol or date |
| Quantity, buy/sell values, cash, turnover, brokerage/STT, tax lots, liquidity, beta/vol/VaR, sector, exposure-after | **Not produced.** Listed by name on the Impact step and in the plan text, each with the reason and where the figure genuinely comes from |

### The three decisions a reviewer should check first

1. **No quantity is derived.** `weight x portfolio value / last price` is four lines and it would
   be wrong invisibly — it assumes the last close is the fill, assumes the whole portfolio is free
   to redeploy, assumes no charges and no lot constraints, and then prints a share count beside a
   symbol. A quantity on a screen reads as an instruction. `NOT_PRODUCED_HERE` is the list; nothing
   in the tree computes one. **There is no rupee sign anywhere in the drawer**, and a test asserts
   it on every step including the generated plan — the cheapest possible proof that no money figure
   was invented.
2. **An exclusion is never re-spread.** Taking an entry out of the plan leaves its slice
   *unassigned*; the names that remain keep the weights the server computed. Re-normalising them
   back to 100% would be this preview inventing an allocation the screen never proposed — the same
   class of mistake as inventing a quantity. The panel shows `Target coverage` and `Left
   unassigned`, and says the honest fix is to re-run the diff with a smaller top N.
3. **Keeping a name the screen exits makes the after-figures indeterminate, by name.** The target
   set does not contain that name, so the portfolio after would hold it *plus* a set that already
   adds to 100%. `largestAfter` and `topFiveAfter` render the reason, naming the symbols that did
   it, rather than a plausible number.

### Two smaller judgements

* **Two zeros are deliberate and are not the forbidden zero.** An *entry*'s current weight is
  `0.00%` — the server has just established you do not hold it — and an *exit*'s target weight is
  `0.00%`, because the target set is an equal weight over the names that stay. Both are measured
  zeros. An **unpriced** holding is the opposite: `weight` is null, and every figure that depends
  on it renders its reason (`"No price today, so it has no weight."`).
* **Fractions are only converted to percentages once, at the very end.** Six equal weights of
  `0.166666` displayed at two decimals add to `100.02`. Coverage, the top-five sums and every delta
  are computed on the fractions with the scaled-integer helpers in `lib/portfolios/decimal`, then
  converted. House rule 9, applied to weights rather than to rupees.

---

## 2. Nothing here can place an order

* No fetch, no mutation, no route reference of any kind lives in this tree. The drawer is a pure
  function of its props plus local UI state; the only callback out is `onAnalyse`, which asks the
  parent to run `POST /portfolios/{id}/rebalance` — a **read** that computes a diff.
* Step 5 produces `planText(...)`: a document, rendered in a `<pre>` with a copy button. There is
  no send control, and a test walks every button in the open drawer asserting none of them is
  named send / submit / place / execute.
* Step 5 is gated on an explicit acknowledgement on step 4 ("I will enter these myself at my
  broker"), and **any exclusion withdraws an acknowledgement already given** — an acknowledgement
  of a different plan is not an acknowledgement of this one.
* `gates/pc4.md` G4 greps the whole directory for an order route and expects zero hits.

---

## 3. The prop contract — what the parent must fetch and pass

```tsx
import { RebalanceDrawer } from "@/components/portfolio/rebalance/rebalance-drawer";

<RebalanceDrawer
  portfolioName={detail.summary.name}   // string, required
  detail={detail}                       // PortfolioDetail | null   (required prop, nullable value)
  screens={screens}                     // readonly ScreenRef[]     (required; [] is the blocked state)
  rebalance={result}                    // RebalanceOut | null      (required; null is "not analysed")
  onAnalyse={run}                       // ((input: AnalyseInput) => void) | undefined
  analysing={mutation.isPending}        // boolean | undefined      (default false)
  analyseError={message}                // string | null | undefined
  defaultTopN={20}                      // number | undefined       (default 20)
  defaultHoldBuffer={10}                // number | undefined       (default 10)
  trigger={<Button …/>}                 // ReactNode | undefined
  open={open}                           // boolean | undefined      (controlled; omit for uncontrolled)
  onOpenChange={setOpen}                // ((open: boolean) => void) | undefined
/>;
```

| Prop | Type | Where the parent gets it |
|---|---|---|
| `portfolioName` | `string` | `PortfolioSummaryOut.name`, or the row's `name` |
| `detail` | `PortfolioDetail \| null` (`Schemas["baskfy_api__routers__portfolio_overview__PortfolioDetailOut"]`, exported as `PortfolioDetail` from `lib/portfolio/overview`) | `GET /api/v1/portfolio/{id}` — the same payload PC3's detail workspace already fetches. **`null` is handled**: every current-side figure renders "Holdings not loaded, so there is no weight to compare." and the name count reports *unknown*, never 0 |
| `screens` | `readonly { public_id: string; name: string }[]` | `useScreens()` in `lib/portfolios/queries` (`ScreenOut[]` is structurally assignable) |
| `rebalance` | `RebalanceOut \| null` | `useRebalance().mutateAsync({ portfolioId, screenPublicId, topN, holdBuffer })` |
| `onAnalyse` | `(input: { screenPublicId: string; topN: number; holdBuffer: number }) => void` | Wrap the mutation above. **Omit it and the primary control is disabled with a visible sentence saying why** — never a dead button |
| `analysing` / `analyseError` | `boolean` / `string \| null` | `mutation.isPending` / `ApiError.message`, so the server's own words reach the reader |
| `trigger` | `ReactNode` | Must be a **single element that accepts a ref** — it is rendered through `DialogTrigger asChild`. Omit it for the built-in `Review rebalance` button (`data-testid="open-rebalance-drawer"`) |
| `open` / `onOpenChange` | `boolean` / `(open: boolean) => void` | Optional. Uncontrolled by default; pass both to drive it from the page |

`AnalyseInput`, `RebalanceDrawerProps` and `ScreenRef` are all exported.

**Type imports:** `ScreenRef`, `RebalancePreview`, `NOT_PRODUCED_HERE`, `WORKFLOW`,
`rebalancePreview`, `planText`, `stepBlockedReason` from `@/lib/portfolio/rebalance-preview`;
`RebalanceDrawer`, `RebalanceDrawerProps`, `AnalyseInput` from
`@/components/portfolio/rebalance/rebalance-drawer`.

**State the drawer owns and the parent must not duplicate:** the current step, the exclusion set,
the per-name notes and the acknowledgement. All four reset automatically when `rebalance.id`
changes, because a fresh diff is a fresh set of instrument ids and an exclusion pinned to the old
ones is an answer to a question nobody asked.

**Nothing else is required.** No provider, no `TooltipProvider`, no query client — the drawer
fetches nothing.

---

## 4. Accessibility and layout

* Radix `Dialog`: `role="dialog"`, an explicit `aria-modal="true"` (Radix marks the rest of the
  tree `aria-hidden` but does not set the attribute), an accessible name from `DialogTitle` and a
  description from `DialogDescription`. Focus is trapped, Escape closes, focus returns to the
  trigger — all four asserted in jsdom rather than assumed.
* One row renderer, two layouts. Below `md` each comparison row stacks with every figure carrying
  a **visible** label; at `md` and up the same markup lays out under a header row and the labels
  become `sr-only`. There is nothing separate for a phone to drift away from — PC1's second defect,
  not repeated.
* Never colour alone: every side carries an icon and a word (Entry / Exit / Band / Hold), every
  warning carries "Critical" or "Review" in words, and every direction carries a sign.
* On a phone the drawer is a bottom sheet; from 900px it is a right-hand panel at 68vw.

---

## 5. What the parent should know

1. **`docs/PORTFOLIO-COMMAND-CENTER.md` §2.2 needs one correction and one addition** (PC4 does not
   edit that file — the parent owns it):
   * §2.2 says *"Target weights, allocation drift ... Baskfy has no notion of a target weight for a
     manually grouped portfolio"*. That is true of a **stored** target and false of a **computed**
     one: a rebalance against a screen produces a real target weight for every name, and this
     drawer shows it. PC3 already recorded the basket-backed half of the same correction.
   * Addition, for §2.2's list: **turnover** is blocked for a reason worth writing down — it is a
     share of *value* changing hands, and counting names instead would be a different number under
     the same label. The name-level churn is shown; it is not called turnover.
2. **The existing `/portfolios/[id]/rebalance` page is untouched.** `RebalanceWizard` still owns
   that route. This drawer is the Command Center's version of the same diff and can replace the
   wizard's result columns later; nothing was removed in case the parent wants both for a while.
3. **`/build` is the "no screen" next action** — that is where `ScreensList` renders. There is no
   `/screens` route.
4. **No rupee figure anywhere is a load-bearing invariant, not a style.** If a future change adds
   one (a portfolio value for context, say), the G2 test fails — which is the intended behaviour,
   not a brittle test. Read this section before relaxing it.
