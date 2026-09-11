# PC6 — the Manage portfolios drawer

**Leaf of** `docs/PORTFOLIO-COMMAND-CENTER.md` §6. **Owns** `src/lib/portfolio/manage.ts`,
`src/components/portfolio/manage/*`, `src/lib/portfolio/__tests__/manage.test.ts` and this file.
**Gates:** `gates/pc6.md`, 9 of 9. **Nothing is mounted into a page** — the prop contract is §4.

---

## 1. What shipped

One drawer, `ManagePortfoliosDrawer`, with an index and eight panels. Every configuration
decision that had been scattered across an inline flow under the comparison table, a `<details>`
block, a separate `/portfolios/[id]/sleeves` route and (for delete) nowhere at all now lives in
one place, so the model's rules are stated once instead of four times.

| Panel | What it does | Endpoint |
|---|---|---|
| Create a portfolio | Kind choice, name, benchmark, holdings picker | `POST /api/v1/portfolio` |
| Rename | Name only; names the two identity fields that have no write | `PATCH /api/v1/portfolios/{portfolio_id}` |
| Assign holdings | Unallocated → a capital portfolio, exclusivity enforced in the preview | `POST /api/v1/portfolio/{portfolio_id}/holdings` |
| Move holdings | Capital → capital, **both sides previewed** | `POST /api/v1/portfolio/{portfolio_id}/holdings` |
| Monitoring view | Create or extend a lens, no ownership change, no quantity box | `POST /api/v1/portfolio` · `POST /api/v1/portfolio/{portfolio_id}/holdings` |
| Sub-portfolios | Adds one named allocation to a portfolio's split | `PUT /api/v1/portfolios/{portfolio_id}/sleeves` |
| Broker & reconciliation | Re-attribute the container; link out to connect and to the inbox | `PATCH /api/v1/portfolios/{portfolio_id}` · `GET /api/v1/portfolio/reconciliation` |
| Delete | Impact named, confirmed by typing the portfolio's name | `DELETE /api/v1/portfolios/{portfolio_id}` |

Endpoint paths are typed as `keyof paths` from the generated OpenAPI document, so **an action
that names a route the API does not serve is a compile error**, not a test somebody remembered to
write.

---

## 2. What §2.2 and §6.3 did not know — two new blocked capabilities

§6.3 said *"create, update, delete, add-holdings and replace-holdings endpoints all exist"*. True,
but "update" is much narrower than it sounds. `PortfolioPatchIn` carries `name`, `parent_id` and
`broker_account_id` and **nothing else** (`services/api/.../portfolios.py`, schema line 7357).

| Capability | Why it cannot be shown | What would unblock it |
|---|---|---|
| **Objective** | No `objective` column on `portfolio`, no field on `NewPortfolioIn` and none on `PortfolioPatchIn`. A grep for `objective` across `services/api/src` returns nothing. It is not a control that is hard to reach; there is nowhere to save it to | An `objective` column plus the field on both write bodies |
| **Benchmark, after creation** | `NewPortfolioIn.benchmark_index_id` sets it **once**, at create. Nothing changes it afterwards — there is no benchmark field on the patch body | `benchmark_index_id` on `PortfolioPatchIn`, and a decision about what happens to the comparison already drawn on the chart when the index changes under it |
| **Reconciliation *settings*** | There is an inbox (`GET /portfolio/reconciliation`) and a resolve route, and no settings of any kind — no tolerance, no auto-resolve rule | A settings model, and a product decision about whether an auto-resolved difference may enter a total unreviewed |

These sit alongside §2.2's existing "permissions, ownership, audit history" row. **Suggested §2.2
addition** (parent's file, not edited here):

> | Objective, and changing a benchmark after creation | `PortfolioPatchIn` carries only name, parent and broker account; no write body has an objective field at all | An objective column; `benchmark_index_id` on the patch body |

`ManageAvailability` therefore has three cases, not two: `available`, `create-only` (benchmark),
and `unavailable`. Collapsing `create-only` into either one would either draw a dead select on an
existing portfolio or remove a control that genuinely works on the create form.

---

## 3. Decisions and findings worth carrying

1. **Objective and benchmark are not drawn as controls on an existing portfolio.** G1's literal
   wording asks for both to be "reachable, each mapping to an endpoint that exists". They have no
   endpoint, so under the autonomy charter's precedence (Goal and the no-dead-control rule beat
   the literal wording of a criterion) they are *named* with a reason and an unblock — on the
   Rename form, where somebody would go looking for them, as well as in the index's list.
2. **`aria-modal` is never set by Radix Dialog 1.1.23.** It relies on `RemoveScroll` and on
   hiding siblings. Correct behaviour, but not the same as telling assistive tech the rest of the
   page is inert. `manage-drawer.tsx` sets it explicitly. Any other Radix dialog in this app
   (`inspector-drawer.tsx`, `ui/dialog.tsx`) has the same gap — **worth a separate fix**.
3. **Focus is not returned to the trigger** for a Radix dialog driven by an external `open` prop
   with no `DialogTrigger`. The drawer captures `document.activeElement` during the render that
   opens it — before the commit that moves focus inside — and restores it in `onCloseAutoFocus`.
   Held in state, not a ref, because a ref read during render is the stale-UI hazard the React
   lint rule exists for.
4. **The sleeves write is destructive by design, so the read is a precondition.**
   `PUT /portfolios/{id}/sleeves` replaces the whole set. A form that adds "Momentum" without
   having read the three allocations already there does not add one; it deletes three. The save
   button is therefore unreachable until the current split has been read back, and a *failed*
   read says so rather than enabling a button that would destroy something.
5. **A portfolio's declared broker account is not on the overview payload.** `PortfolioRowOut`
   carries the brokers whose holdings are *in* a portfolio, not the `broker_account_id` the row
   is declared against. A select that defaulted to any option would let one click silently change
   a declaration nobody looked at, so the box starts on "Choose an account", the button is off
   until a choice is made, and the panel says why.
6. **Two ceilings on a move, and they differ.** The shipped picker's "of N available" is the
   *route's* ceiling (`_apply_allocation` takes free shares first, then the smallest other slice).
   A move out of a named portfolio can only take what that portfolio holds. Both are true; the
   panel says which is which, because otherwise the two numbers read as a bug.
7. **Deleting destroys more than the grouping.** `portfolio_holding`, `portfolio_nav_daily` **and**
   `portfolio_cash_flow` all cascade on `portfolio.id`. §5.1 is explicit that the NAV series is
   stored rather than recomputed, so the chart, the drawdown and the dated flows XIRR is solved
   from are gone for good — recreating the portfolio tomorrow starts its history at tomorrow. The
   shares are untouched (they are in the demat; these rows are bookkeeping) and the panel leads
   with that, because it is the fear a person brings to a red button.
8. **§8's jargon scanner caught this leaf, twice.** `src/lib/__tests__/no-jargon.test.ts` bans
   "sleeve", "divide", "book" and "nest" from user-visible strings. The first draft of the
   sub-portfolio panel used three of them, and a later edit reintroduced one stray "the box" in
   the broker hint. Both are fixed and the scanner now reports zero offences anywhere.
   User-facing copy reads *allocation* and *split*; the schema names survive in comments and
   identifiers, which is exactly what the scanner allows. **Worth knowing for the other leaves:**
   this scanner runs over the whole of `src`, so it fails for everyone when any one leaf trips
   it, and a `tsc` error in a sibling's file blocks `pnpm run lint` before eslint ever runs.

---

## 4. The prop contract — what the parent must fetch and pass

```tsx
import { ManagePortfoliosDrawer } from "@/components/portfolio/manage/manage-drawer";

<ManagePortfoliosDrawer
  open={open}                         // boolean — the host owns the trigger and the state
  onOpenChange={setOpen}              // (open: boolean) => void
  capital={overview.portfolios ?? []}          // PortfolioRow[]  — OverviewOut.portfolios
  views={overview.monitoring_views ?? []}      // PortfolioRow[]  — OverviewOut.monitoring_views
  rows={holdings.holdings ?? []}               // AggregatedHolding[] — HoldingsOut.holdings
  brokers={brokersIn(holdings.holdings ?? [])} // BrokerRef[]  (optional, default [])
  sectors={sectors}                            // Record<string,string> (optional)
  benchmarks={benchmarkNames}                  // string[] (optional; falls back to DEFAULT_BENCHMARKS)
  openReconciliationCount={overview.open_reconciliation_count ?? 0}  // number (optional, default 0)
  initialAction={null}                         // ManageActionId | null (optional) — which panel to open on
  handlers={handlers}                          // ManageHandlers (optional; anything missing disables its button beside a reason)
  onChanged={(message) => router.refresh()}    // (message: string) => void — fires ONLY on a write that succeeded
/>
```

### `ManageHandlers` — all optional, all server actions

| Field | Signature | Wire |
|---|---|---|
| `create` | `(draft: PortfolioDraft) => Promise<ManageOutcome>` | `POST /api/v1/portfolio`. **`lib/portfolio/create.ts`'s `createPortfolio` already matches** — `CreateResult` is structurally a `ManageOutcome` |
| `rename` | `(portfolioId: number, name: string) => Promise<ManageOutcome>` | `PATCH /api/v1/portfolios/{id}` with `{ name }` |
| `transfer` | `(request: TransferRequest) => Promise<ManageOutcome>` | `POST /api/v1/portfolio/{request.destinationPortfolioId}/holdings` with `{ holdings: request.holdings.map(({instrument_id, broker_account_id, quantity}) => quantity === null ? {instrument_id, broker_account_id} : {instrument_id, broker_account_id, quantity}) }`. **Omit the key when `quantity` is null** — the API reads a missing quantity as "all of it"; `request.sourcePortfolioId` is for the journal, not the wire |
| `loadSleeves` | `(portfolioId: number) => Promise<readonly SleeveRow[]>` | `GET /api/v1/portfolios/{id}/sleeves`, returning `data.sleeves`. **Required before any sleeve save** |
| `saveSleeves` | `(portfolioId: number, sleeves: readonly SleeveDraft[]) => Promise<ManageOutcome>` | `PUT /api/v1/portfolios/{id}/sleeves` with `{ sleeves }` |
| `reattribute` | `(portfolioId: number, brokerAccountId: number \| null) => Promise<ManageOutcome>` | `PATCH /api/v1/portfolios/{id}` with `{ broker_account_id }` |
| `remove` | `(portfolioId: number) => Promise<ManageOutcome>` | `DELETE /api/v1/portfolios/{id}` |

`ManageOutcome` is `{ ok: true; portfolioId: number; message?: string } | { ok: false; reason: string }`.
**Pass the server's own `detail` through as `reason`** — `create.ts`'s `refusalReason()` is the
pattern: "HDFC Bank is already in Long term" is worth more than anything this layer could invent.
A handler that *throws* is also handled; it becomes a sentence, never a stack trace.

### Two things the parent should know

- **A missing handler is not a dead button.** Its control is disabled with the reason beside it,
  and the failure path in `useWrite` still produces a sentence if one is somehow reached.
- **`onChanged` fires only on a genuine success**, so it is safe to wire straight to
  `router.refresh()`. A refusal and a thrown request both leave the drawer open on the form with
  everything the person entered still in it.

### Where to put the trigger

The index is the natural home for the brief's **overflow menu** (§3, "Four header controls the
brief names"): PC1 deferred import/export/settings/reconciliation to this drawer, and the drawer
now holds them. The trigger belongs on `command-header.tsx`; focus restore targets whatever
element was focused when `open` became true, so any button works.

---

## 5. Not done, and deliberately

- **Nothing is mounted.** No `page.tsx`, `command-center-screen.tsx` or `fetch.ts` was touched;
  `gates/pc-integration.md` owns the wiring.
- **Screen-driven allocations** are not offered in the drawer — they need the screen catalogue, a
  rank buffer and an allocation preview, which is the full planner at `/portfolios/[id]/sleeves`.
  The drawer adds a manual slice and links to the planner.
- **Removing holdings from a portfolio back to Unallocated** has no route. The API's own
  docstring says so: *"'add these shares to Unallocated' is a removal, which is a different verb
  and deserves its own route"*. Deleting the portfolio is today's only path, and the delete panel
  says what that costs.
- **An e2e pass** (axe in both themes, the drawer at phone width) is not written. `e2e/` belongs
  to the parent per §6.1.
