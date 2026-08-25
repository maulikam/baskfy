"use client";

import type { ScreenOut, SleeveIn, SleeveListOut } from "@baskfy/api-client";
import { Plus, Trash2 } from "lucide-react";
import { parseAsBoolean, useQueryState } from "nuqs";
import { useState } from "react";

import { ErrorState } from "@/components/data/error-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SleeveAllocationCard } from "@/components/portfolios/sleeve-allocation-card";
import { addDecimalStrings, formatRupees } from "@/lib/portfolios/decimal";
import { useAllocation, useSaveSleeves, useSleeves } from "@/lib/portfolios/queries";
import { allocationUnits } from "@/lib/portfolios/units";

/**
 * Dividing a portfolio across screens, with a slice you run yourself — M34.
 *
 * The Rebalance Tracker answers "which symbols changed". This answers the other question: **how
 * much goes where**. Each sleeve carries its own capital and its own source, and a `manual` sleeve
 * is money you run yourself — counted so the totals are honest, never allocated.
 *
 * **Amounts, weights and unit counts — and still no control that could place anything.** The
 * allocation now carries `units` and `price` per row, because "₹5,00,000 of CUPID" is not a thing
 * a reader can check against a demat statement and "1,757 shares" is. A count is not an
 * instruction: there is no order affordance on this page and there is none anywhere in this app.
 * Execution lives in the desk console.
 *
 * When a price is unavailable the row's `units` and `price` arrive as **`null`, never `0`**, and
 * the cell renders as an em dash with the server's own reason beside it. A silent zero would read
 * as "buy none of this", which is a different and false statement — preventing it is the entire
 * point of the units feature.
 *
 * The market stance is shown as a fact — *"R1 · the strategy caps equity at 100% under R1"* — with
 * a control to size the sleeves to that cap. Applying it is the reader's decision; the page never
 * urges it.
 *
 * (`sleeve-planner-contract.test.ts` scans this file for the phrasings of advice, which is why
 * they are described here rather than written. Third time this collision has been recorded —
 * M22.4, M34.5, and now.)
 */

/** Monotonic ids for rows the reader adds, so a new row keeps its identity while being typed in. */
let keySeed = 0;
function nextKey(): number {
  keySeed += 1;
  return keySeed;
}

/**
 * Rupees, formatted without ever becoming a `number` — CLAUDE.md house rule 9.
 *
 * This used to be `Intl.NumberFormat().format(Number(value))`. A crore is 1e7 and a decade of
 * contributions is more; the double was exact at these sizes today and would not have said so on
 * the day it stopped being.
 */
function money(value: string | number): string {
  return formatRupees(typeof value === "number" ? String(value) : value, { decimals: 0 });
}

interface Row {
  /** Stable across edits and removals. An index key rebinds row 3's state to row 4 the moment
   *  row 2 is deleted, which shows the wrong capital against the wrong sleeve. */
  key: string;
  name: string;
  kind: "screen" | "manual";
  capital: string;
  screen_public_id: string | null;
  top_n: number;
}

/**
 * `"4000000.00"` → `"4000000"`.
 *
 * `numeric(18,2)` serialises with its paise and a number input renders that verbatim, so the field
 * reads like an accounting entry. Whole rupees is the precision the allocator uses. Done on the
 * digits rather than through `Math.round(Number(...))`, so it is a truncation of a decimal string
 * and not a trip through a float.
 */
function wholeRupees(capital: string | null | undefined): string {
  const text = (capital ?? "0").trim();
  const [whole = "0"] = text.split(".");
  return whole === "" || whole === "-" ? "0" : whole;
}

function toRows(listing: SleeveListOut | undefined): Row[] {
  return (listing?.sleeves ?? []).map((sleeve) => ({
    key: `saved-${sleeve.id}`,
    name: sleeve.name,
    kind: sleeve.kind === "manual" ? "manual" : "screen",
    capital: wholeRupees(sleeve.capital),
    screen_public_id: sleeve.screen_public_id ?? null,
    top_n: sleeve.top_n ?? 15,
  }));
}

export function SleevePlanner({
  portfolioId,
  screens,
  initial,
}: {
  portfolioId: number;
  screens: ScreenOut[];
  initial?: SleeveListOut;
}) {
  const sleeves = useSleeves(portfolioId, initial);
  const save = useSaveSleeves(portfolioId);
  // In the URL, not component state: the capped and uncapped views are genuinely different
  // answers about somebody's money, and a link to one of them should open that one.
  const [applyCap, setApplyCap] = useQueryState(
    "cap",
    parseAsBoolean.withDefault(false).withOptions({ shallow: false, history: "push" }),
  );
  const allocation = useAllocation(portfolioId, applyCap);

  const [rows, setRows] = useState<Row[] | null>(null);
  // Removing a sleeve throws away a capital figure somebody chose. An undo window is cheaper than
  // a confirmation dialog for something this reversible, and the guidelines allow either -- what
  // they refuse is an immediate, silent delete.
  const [undo, setUndo] = useState<{ row: Row; at: number } | null>(null);
  const editing = rows ?? toRows(sleeves.data);
  const total = addDecimalStrings(editing.map((row) => row.capital)) ?? "0";

  function update(index: number, patch: Partial<Row>) {
    setRows(editing.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  }

  const stance = allocation.data?.stance;
  const units = allocation.data
    ? allocationUnits(allocation.data)
    : { pricedAsOf: null, unpriced: [], note: null };

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold tracking-tight">Sleeves</h1>
        <p className="text-sm text-muted-foreground">
          Divide the portfolio across screens, and keep a slice you run yourself. Each sleeve gets
          its own capital; the names come from its screen.
        </p>
      </header>

      {/*
        The stance, stated. Not a recommendation: the tier is what the strategy is doing, the cap
        is what that tier implies, and applying it to your own sleeves is a tick you make.
      */}
      {stance && (
        <section className="rounded-xl border border-border/70 bg-card p-4">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="text-muted-foreground">Market stance:</span>
            <Badge variant="neutral">{stance.tier}</Badge>
            <span className="font-medium">{stance.label}</span>
            {stance.equity_cap_pct !== null && stance.equity_cap_pct !== undefined && (
              <span className="text-muted-foreground">
                — under {stance.tier} the strategy caps equity at {Number(stance.equity_cap_pct)}%
              </span>
            )}
          </div>
          <label className="mt-2 flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={applyCap}
              // `void`: nuqs's setter returns a Promise the change handler has no use for, and
              // awaiting a URL update inside an onChange would only delay the tick.
              onChange={(event) => void setApplyCap(event.target.checked)}
              className="size-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            Size my screen sleeves to this cap
          </label>
          {stance.reasons.length > 0 && (
            <ul className="mt-2 flex flex-col gap-1 text-xs text-muted-foreground">
              {stance.reasons.slice(0, 3).map((reason) => (
                <li key={reason}>• {reason}</li>
              ))}
            </ul>
          )}
        </section>
      )}

      <section className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold">How the portfolio is divided</h2>
          <span className="text-sm tabular-nums text-muted-foreground">
            Total {money(total)}
          </span>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b text-left text-xs uppercase tracking-wide text-muted-foreground">
                <th className="py-2 pr-3">Name</th>
                <th className="py-2 pr-3">Source</th>
                <th className="py-2 pr-3">Names</th>
                <th className="py-2 pr-3 text-right">Capital</th>
                <th className="py-2" />
              </tr>
            </thead>
            <tbody>
              {editing.length === 0 && (
                <tr>
                  <td colSpan={5} className="py-6 text-center text-sm text-muted-foreground">
                    No sleeves yet. Add one to divide this portfolio.
                  </td>
                </tr>
              )}
              {editing.map((row, index) => (
                <tr key={row.key} className="border-b last:border-0">
                  <td className="py-2 pr-3">
                    <input
                      aria-label="Sleeve name"
                      value={row.name}
                      onChange={(event) => update(index, { name: event.target.value })}
                      autoComplete="off"
                      spellCheck={false}
                      className="w-40 min-w-0 truncate rounded-md border bg-background px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    />
                  </td>
                  <td className="py-2 pr-3">
                    <select
                      aria-label="Sleeve source"
                      value={row.screen_public_id ?? "manual"}
                      onChange={(event) => {
                        const value = event.target.value;
                        update(
                          index,
                          value === "manual"
                            ? { kind: "manual", screen_public_id: null }
                            : { kind: "screen", screen_public_id: value },
                        );
                      }}
                      className="w-56 min-w-0 truncate rounded-md border bg-background px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      <option value="manual">I run this myself</option>
                      {screens.map((screen) => (
                        <option key={screen.public_id} value={screen.public_id}>
                          {screen.name}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="py-2 pr-3">
                    {row.kind === "screen" ? (
                      <input
                        aria-label="How many names"
                        type="number"
                        min={1}
                        max={100}
                        value={row.top_n}
                        onChange={(event) =>
                          update(index, { top_n: Number(event.target.value) || 1 })
                        }
                        inputMode="numeric"
                        autoComplete="off"
                        className="w-20 rounded-md border bg-background px-2 py-1 tabular-nums focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      />
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </td>
                  <td className="py-2 pr-3 text-right">
                    <input
                      aria-label="Capital"
                      type="number"
                      min={0}
                      step={1000}
                      value={row.capital}
                      onChange={(event) => update(index, { capital: event.target.value })}
                      inputMode="numeric"
                      autoComplete="off"
                      className="w-36 rounded-md border bg-background px-2 py-1 text-right tabular-nums focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    />
                  </td>
                  <td className="py-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`Remove ${row.name}`}
                      onClick={() => {
                        setUndo({ row, at: index });
                        setRows(editing.filter((_, i) => i !== index));
                      }}
                    >
                      <Trash2 className="size-4" />
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              setRows([
                ...editing,
                {
                  key: `new-${nextKey()}`,
                  name: `Sleeve ${editing.length + 1}`,
                  kind: "manual",
                  capital: "0",
                  screen_public_id: null,
                  top_n: 15,
                },
              ])
            }
          >
            <Plus className="mr-1 size-4" /> Add Sleeve
          </Button>
          <Button
            size="sm"
            disabled={save.isPending}
            onClick={() => {
              const body: SleeveIn[] = editing.map((row) => ({
                name: row.name,
                kind: row.kind,
                capital: row.capital,
                screen_public_id: row.kind === "screen" ? row.screen_public_id : null,
                top_n: row.top_n,
              }));
              save.mutate(body, {
                onSuccess: () => {
                  setRows(null);
                  setUndo(null);
                },
              });
            }}
          >
            {save.isPending ? "Saving…" : "Save Sleeves"}
          </Button>
        </div>
        {/*
          Announced, not just drawn: a reader using a screen reader gets no signal from a row
          vanishing. `role="status"` carries an implicit aria-live="polite".
        */}
        <div role="status" aria-live="polite" className="min-h-6 text-sm">
          {undo && (
            <span className="flex items-center gap-2">
              <span className="text-muted-foreground">Removed {undo.row.name}.</span>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  const restored = [...editing];
                  restored.splice(undo.at, 0, undo.row);
                  setRows(restored);
                  setUndo(null);
                }}
              >
                Undo
              </Button>
            </span>
          )}
          {save.isSuccess && !undo && <span className="text-muted-foreground">Sleeves saved.</span>}
        </div>
        {save.isError && <ErrorState error={save.error} />}
      </section>

      {allocation.data && (
        <section className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold">What each sleeve would hold</h2>
            <span className="text-sm tabular-nums text-muted-foreground">
              {money(allocation.data.deployed)} deployed · {money(allocation.data.cash)} cash
            </span>
          </div>
          {allocation.data.applied_regime_cap && (
            <p role="status" className="rounded-md border bg-muted/40 p-2.5 text-xs">
              Sized to the {stance?.tier} cap. The portfolio is still{" "}
              {money(allocation.data.capital)} — the difference is held as cash, not removed.
            </p>
          )}

          {/*
            Units come from a price map, so the reader is told which day's prices produced them and
            which names had none. A count with no date attached is a count nobody can check.
          */}
          <p className="text-xs text-muted-foreground" data-testid="allocation-pricing">
            {units.pricedAsOf === null
              ? "No prices were available, so every row below shows an amount and no unit count."
              : `Unit counts are whole shares at closing prices from ${units.pricedAsOf}. They are what the amount buys, not an instruction to buy it.`}
          </p>
          {units.note !== null && (
            <p
              className="rounded-md border border-warning/40 bg-warning-muted/40 p-2.5 text-xs leading-relaxed"
              data-testid="allocation-unpriced"
            >
              {units.note}
            </p>
          )}

          <div className="grid gap-3 xl:grid-cols-2">
            {allocation.data.sleeves.map((sleeve) => (
              <SleeveAllocationCard key={sleeve.name} sleeve={sleeve} />
            ))}
          </div>
        </section>
      )}

      <p className="text-xs text-muted-foreground">
        Amounts, target weights and the whole shares they buy — nothing here places an order, and a
        name with no price shows a blank rather than a zero.
      </p>
    </div>
  );
}
