import type { SleeveAllocationOut } from "@baskfy/api-client";

import { formatRupees, isPositiveDecimal, roundDecimalString } from "@/lib/portfolios/decimal";
import { priceCell, sleeveUnpriced, unitsCell } from "@/lib/portfolios/units";

/**
 * One sleeve's allocation — what it would hold, in rupees **and in shares**.
 *
 * Extracted from the planner so the rule it enforces can be tested on its own, because the rule is
 * about a single cell: **an unpriced row shows an em dash and the reason, never a `0`.** The API
 * sends `units: null` and `price: null` when it could not price a name, precisely so the UI can
 * tell "no price" apart from "no shares" — rendering both as `0` throws that distinction away and
 * tells the reader the allocator decided against a name it never managed to look at.
 *
 * A genuine zero is a different cell with a different reason: capital that does not stretch to one
 * share is an answer, and it says so.
 *
 * Money never passes through a `number` here (CLAUDE.md house rule 9); see `../lib/portfolios/decimal`.
 */

function money(value: string): string {
  return formatRupees(value, { decimals: 0 });
}

export function SleeveAllocationCard({ sleeve }: { sleeve: SleeveAllocationOut }) {
  const unpriced = sleeveUnpriced(sleeve);

  return (
    <div
      data-testid="sleeve-allocation"
      data-sleeve={sleeve.name}
      data-unpriced={unpriced.count}
      className="rounded-xl border border-border/70 bg-card p-4"
    >
      <div className="flex items-baseline justify-between">
        <div>
          <div className="text-sm font-medium">{sleeve.name}</div>
          <div className="text-xs text-muted-foreground">
            {sleeve.screen_name ?? sleeve.basket_name ?? "You run this one"}
          </div>
        </div>
        <div className="text-right text-sm tabular-nums">
          {money(sleeve.capital)}
          {isPositiveDecimal(sleeve.cash) && (
            <div className="text-xs text-muted-foreground">{money(sleeve.cash)} cash</div>
          )}
        </div>
      </div>

      {sleeve.source_note !== null && sleeve.source_note !== undefined && (
        <p className="mt-1 text-xs text-muted-foreground">{sleeve.source_note}</p>
      )}

      {sleeve.rows.length > 0 && (
        <table className="mt-2 w-full text-sm tabular-nums">
          <thead>
            <tr className="text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th scope="col" className="py-1 pr-3 font-normal">
                Name
              </th>
              <th scope="col" className="py-1 pr-3 text-right font-normal">
                Weight
              </th>
              <th scope="col" className="py-1 pr-3 text-right font-normal">
                Amount
              </th>
              <th scope="col" className="py-1 pr-3 text-right font-normal">
                Price
              </th>
              <th scope="col" className="py-1 text-right font-normal">
                Units
              </th>
            </tr>
          </thead>
          <tbody>
            {sleeve.rows.map((row) => {
              const price = priceCell(row);
              const cell = unitsCell(row, sleeve);
              return (
                <tr key={row.symbol} className="border-t" data-testid="allocation-row">
                  <td className="py-1 pr-3 font-medium">{row.symbol}</td>
                  <td className="py-1 pr-3 text-right text-muted-foreground">
                    {roundDecimalString(row.weight_pct, 2) ?? row.weight_pct}%
                  </td>
                  <td className="py-1 pr-3 text-right">{money(row.amount)}</td>
                  <td className="py-1 pr-3 text-right text-muted-foreground">{price.text}</td>
                  <td
                    className="py-1 text-right"
                    data-testid="units-cell"
                    data-symbol={row.symbol}
                    data-unpriced={cell.unpriced ? "true" : "false"}
                    {...(cell.reason === null ? {} : { title: cell.reason })}
                  >
                    {cell.text}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {unpriced.note !== null && (
        <p
          className="mt-2 text-xs leading-relaxed text-muted-foreground"
          data-testid="sleeve-unpriced-note"
        >
          {unpriced.note}
        </p>
      )}
    </div>
  );
}
