# Discover audit — the brief's sixteen claims, checked against the code

Written before any change, against the working tree on 25 Aug 2026. Every verdict cites the file
that decides it. Four claims are wrong or only half right, and saying so is worth more than
building against them: the previous collections tree found the same thing and correcting the
brief was the most useful thing it did.

Verdicts: **TRUE** · **PARTLY** (real problem, wrong diagnosis) · **FALSE** (the code already
does the right thing).

| # | Claim | Verdict | What the code actually does |
|---|---|---|---|
| C1 | The right side of the desktop screen is largely unused | **TRUE** | `src/app/(app)/baskets/page.tsx:88` wraps everything in `max-w-5xl` (64rem) while the shell's own capsule is `max-w-[104rem]` (`src/components/shell/top-nav.tsx:57`). The page throws away 40rem of the width its own chrome claims. |
| C2 | The same six baskets repeat across multiple collections | **PARTLY** | The repetition was real and is now suppressed at render time (`src/lib/collections/select.ts`), but the *cause* is that the catalogue holds six or seven published baskets, all momentum, all run by the engine. Rendering can stop the repeat; only more baskets can make the shelves differ. |
| C3 | Basket cards look almost identical | **TRUE** | One component, one layout, three fields, no per-strategy signal: `src/components/basket/basket-card.tsx:45-108`. Nothing on the card varies with what the strategy *is*. |
| C4 | Initials such as "BM" and "LM" do not create memorable identities | **TRUE** | `src/components/basket/basket-card.tsx:7-12` — `monogram()` takes the first letter of the first two words. "Broad Market Sharpe" and "Liquid Momentum" become BM and LM. |
| C5 | There is no prominent action on a card | **TRUE** | `basket-card.tsx:52` makes the whole card one `<Link>`; the `actions` slot at line 107 exists and the catalog never passes it (`src/components/explore/basket-card.tsx:21`). |
| C6 | Users see returns before understanding risk | **TRUE** | `basket-card.tsx:82-104` renders Min. amount → headline return → risk, in that order. Risk is last and is one word. |
| C7 | "Swing: Low/Med/High" is unclear and potentially misleading | **TRUE** | `basket-card.tsx:101` labels `volatility_bucket` "Swing". The number behind it (`volatility_value`, annualised) is never shown, and the basis (`volatility_basis`, which can be `CONSTITUENT_WEIGHTED` rather than measured on the basket itself) is never disclosed. |
| C8 | Returns are shown as `53.30` instead of `53.30%` | **TRUE, and the mechanism matters** | `basket-card.tsx:93-95` appends `%` only when `typeof headlinePct === "number"`. The API types it `string \| number \| null` (`src/lib/explore/fetch.ts:33`) because it is a Pydantic `Decimal`, which serialises to a JSON **string**. So the `%` branch is dead for every real payload — the unit is missing always, not sometimes. |
| C9 | One-year performance is overemphasised | **FALSE** | `packages/core/src/baskfy_core/curated_metrics.py:730-769` picks the longest window the basket can actually support: 5Y CAGR, else 3Y CAGR, else 1Y, else 6M, else 1M, else nothing. It shows 1Y only when the basket is between one and three years old — which every basket in this catalogue currently is. The card is right; the catalogue is young. |
| C10 | There is no comparison feature | **TRUE** | Nothing under `src/app`, `src/components` or `src/lib` implements basket comparison. The only `Compare` in the tree is a benchmark toggle inside `src/components/explore/performance-chart.tsx`. |
| C11 | There is no watchlist/save control | **PARTLY** | The watchlist exists end to end: `GET/POST/DELETE /api/v1/watchlist` (`services/api/src/baskfy_api/routers/explore.py:496,521,567`), a `cb_watchlist_item` table, and a page at `/me/watchlist`. What is missing is a **save control on a card** — you can only add from the basket's own page. |
| C12 | "Create" under Baskets duplicates the global "Build" navigation | **TRUE** | `src/lib/nav.ts:144-148` — the `baskets` section tabs are Explore · Featured · **Create**, and `/create` is a screen-building surface that belongs to Build. |
| C13 | "Featured" does not explain why something is featured | **TRUE, and the name is wrong** | `src/app/(app)/baskets/featured/page.tsx:13` — it is not a curated selection at all. It renders the house momentum strategy as it would be constructed today. "Featured" implies editorial choice among many; the page is one strategy's live output. |
| C14 | "Minimum amount" is high for some baskets but not explained | **PARTLY** | The explanation exists, on the wrong surface: `src/components/basket/sizing-controls.tsx:63-66` says "₹X is the least that fills all N names". The card (`basket-card.tsx:84`) shows the number with no reason, and the reason is the one thing that makes a six-lakh minimum comprehensible. |
| C15 | The "December 2026 update" conflicts with the August 2026 data date | **FALSE as stated — but it hides a real bug** | `src/app/(app)/layout.tsx:66-68` announces work that is *"on the way"*: adjusted history, a longer backfill, backtests. December 2026 is four months in the future, so there is no conflict with an August 2026 data date. The real defect is one line up: the call to action reads "Read the December 2026 update" and points at **`/blog`**, while the update has its own route at `/december-2026-update` (`src/lib/marketing/routes.ts:39`). The link goes to the wrong place. |
| C16 | Disclosures are pushed to the bottom instead of being available where performance is shown | **TRUE** | `src/app/(app)/baskets/page.tsx:162-163` puts `ReturnConventionNote` and `DisclosureBlock` after every card. The cards each show a return; none of them carries or links the convention that return was computed under. |

## What the audit changes about the plan

- **C9 is dropped as a task.** De-emphasising 1Y would mean overriding a rule that is already
  correct and better than the brief's suggestion. What the card owes the reader instead is the
  *window label next to the number*, which it already has and which the redesign must keep.
- **C15 becomes a one-line link fix**, not a rewrite of the announcement.
- **C11 becomes "put the existing watchlist on the card"**, not "build a watchlist".
- **C2 becomes a data statement** that Discover must be honest about rather than hide.
- **C13 gains a rename**, because explaining "why featured" is impossible while the page is not
  a featured list.
