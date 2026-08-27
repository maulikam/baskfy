"use client";

import { Money, MoneyDelta } from "@/components/portfolio/amounts";
import { EMPTY_CELL, formatFraction, formatNumber, formatTradeDate } from "@/lib/format";
import {
  AVG_PRICE_SHORT,
  NO_SINCE_PURCHASE,
  NO_TARGET_WEIGHTS,
  avgPriceReason,
  driftOf,
  hasCostBasis,
  targetWeightOf,
  type DetailHoldingRow,
} from "@/lib/portfolio/detail-view";
import { toneFor } from "@/lib/portfolio/overview";
import { cn } from "@/lib/utils";

/**
 * §7's holdings block: *"qty, avg price (if known), current price, weight, today's contribution,
 * total contribution, broker. For basket-backed portfolios add target weight + drift."*
 *
 * ## "if known" is the whole design of the average-price column
 *
 * §5.2 forbids since-purchase P&L for a holding whose transaction history has not been imported,
 * and §5.3 says the CAS import is what unlocks it. The API honours that by sending `avg_price`
 * and `total_contribution` as `null` — never as zero — and this table renders the null as an em
 * dash with the reason attached rather than as a number.
 *
 * A zero would be the actively harmful rendering. `₹0.00` in an average-price column is
 * indistinguishable from a genuinely free share, and it makes the whole position read as pure
 * profit; `0.00%` in a contribution column is indistinguishable from a position that has gone
 * exactly nowhere. Both are claims, and neither is one we are in a position to make.
 *
 * The reason appears three ways, because a tooltip alone is invisible to anyone not holding a
 * mouse: a short marker in the cell, the full sentence on `title`, and a footnote under the table
 * naming every distinct reason present. `history_source` decides which sentence — "we never had
 * it", "your broker did not send it" and "your statement did not carry it" have different
 * remedies and the reader is the one who has to act on them.
 *
 * ## Target weight and drift
 *
 * Rendered for a basket-backed portfolio, from the payload's target weight where there is one.
 * There is no fallback that invents a target: an equal split across the names held would show
 * every portfolio as perfectly on target forever, which is the same lie as the zero above, one
 * column to the right. See `detail-view.ts` for the whole note.
 */

export interface DetailHoldingsProps {
  holdings: readonly DetailHoldingRow[] | null;
  /** True when this portfolio tracks a published model, so §7's two extra columns apply. */
  basketBacked?: boolean;
  /** The sentence to print instead of a table when the read failed. */
  unavailableReason?: string | null;
}

export function DetailHoldings({
  holdings,
  basketBacked = false,
  unavailableReason = null,
}: DetailHoldingsProps) {
  if (holdings === null) {
    return (
      <Block>
        <p data-testid="detail-holdings-unavailable" className="text-sm text-muted-foreground">
          {unavailableReason ?? "The holdings for this portfolio did not load."}
        </p>
      </Block>
    );
  }

  if (holdings.length === 0) {
    return (
      <Block>
        <p data-testid="detail-holdings-empty" className="text-sm text-muted-foreground">
          Nothing is allocated to this portfolio yet. Allocating a holding here records which
          portfolio it belongs to and nothing else.
        </p>
      </Block>
    );
  }

  const missingCost = holdings.filter((holding) => !hasCostBasis(holding));
  const reasons = [...new Set(missingCost.map((holding) => avgPriceReason(holding)))];
  const anyTarget = holdings.some((holding) => targetWeightOf(holding) !== null);

  return (
    <Block>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[54rem] text-sm" data-testid="detail-holdings">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th scope="col" className="py-2 pr-3 font-normal">
                Stock
              </th>
              <th scope="col" className="py-2 pr-3 text-right font-normal">
                Qty
              </th>
              <th scope="col" className="py-2 pr-3 text-right font-normal">
                Avg price
              </th>
              <th scope="col" className="py-2 pr-3 text-right font-normal">
                Price
              </th>
              <th scope="col" className="py-2 pr-3 text-right font-normal">
                Value
              </th>
              <th scope="col" className="py-2 pr-3 text-right font-normal">
                Weight
              </th>
              {basketBacked ? (
                <>
                  <th scope="col" className="py-2 pr-3 text-right font-normal">
                    Target weight
                  </th>
                  <th scope="col" className="py-2 pr-3 text-right font-normal">
                    Drift
                  </th>
                </>
              ) : null}
              <th scope="col" className="py-2 pr-3 text-right font-normal">
                Today
              </th>
              <th scope="col" className="py-2 pr-3 text-right font-normal">
                Since purchase
              </th>
              <th scope="col" className="py-2 text-left font-normal">
                Broker
              </th>
            </tr>
          </thead>
          <tbody>
            {holdings.map((holding) => {
              const known = hasCostBasis(holding);
              const reason = avgPriceReason(holding);
              const target = targetWeightOf(holding);
              const drift = driftOf(holding);
              return (
                <tr
                  key={`${holding.instrument.instrument_id}-${holding.broker.broker_account_id}`}
                  data-testid={`detail-holding-${holding.instrument.symbol}`}
                  className="border-b border-border/50 last:border-0"
                >
                  <td className="py-2 pr-3">
                    <span className="font-medium">{holding.instrument.symbol}</span>
                    <span className="block text-xs text-muted-foreground">
                      {holding.instrument.name}
                    </span>
                    {holding.pending_reconciliation ? (
                      <span className="block text-xs text-warning">Pending reconciliation</span>
                    ) : null}
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    {formatNumber(holding.quantity, { decimals: 0 })}
                  </td>
                  <td
                    className="py-2 pr-3 text-right tabular-nums"
                    data-testid={`detail-avg-price-${holding.instrument.symbol}`}
                  >
                    {known ? (
                      <>
                        <span>₹{formatNumber(holding.avg_price, { decimals: 2 })}</span>
                        {holding.first_bought_on ? (
                          <span className="block text-xs text-muted-foreground">
                            from {formatTradeDate(holding.first_bought_on)}
                          </span>
                        ) : null}
                      </>
                    ) : (
                      <span title={reason}>
                        <span aria-hidden="true">{EMPTY_CELL}</span>
                        <span className="sr-only">{reason}</span>
                        <span className="block text-xs text-muted-foreground">
                          {AVG_PRICE_SHORT}
                        </span>
                      </span>
                    )}
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    {holding.price === null || holding.price === undefined
                      ? EMPTY_CELL
                      : `₹${formatNumber(holding.price, { decimals: 2 })}`}
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    <Money value={holding.value ?? null} />
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    {holding.weight === null || holding.weight === undefined
                      ? EMPTY_CELL
                      : formatFraction(holding.weight)}
                  </td>
                  {basketBacked ? (
                    <>
                      <td
                        className="py-2 pr-3 text-right tabular-nums"
                        data-testid={`detail-target-${holding.instrument.symbol}`}
                      >
                        {target === null ? EMPTY_CELL : formatFraction(target)}
                      </td>
                      <td
                        className={cn("py-2 pr-3 text-right tabular-nums", toneFor(drift))}
                        data-testid={`detail-drift-${holding.instrument.symbol}`}
                      >
                        {drift === null
                          ? EMPTY_CELL
                          : formatNumber(drift * 100, {
                              decimals: 2,
                              signed: true,
                              suffix: "%",
                            })}
                      </td>
                    </>
                  ) : null}
                  <td
                    className={cn(
                      "py-2 pr-3 text-right tabular-nums",
                      toneFor(holding.todays_contribution ?? null),
                    )}
                  >
                    <MoneyDelta value={holding.todays_contribution ?? null} />
                  </td>
                  <td
                    className={cn(
                      "py-2 pr-3 text-right tabular-nums",
                      toneFor(holding.total_contribution ?? null),
                    )}
                    data-testid={`detail-total-contribution-${holding.instrument.symbol}`}
                  >
                    {holding.total_contribution === null ||
                    holding.total_contribution === undefined ? (
                      <span title={NO_SINCE_PURCHASE}>
                        <span aria-hidden="true">{EMPTY_CELL}</span>
                        <span className="sr-only">{NO_SINCE_PURCHASE}</span>
                      </span>
                    ) : (
                      <MoneyDelta value={holding.total_contribution} />
                    )}
                  </td>
                  <td className="py-2 text-muted-foreground">{holding.broker.label}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {reasons.length > 0 ? (
        <div data-testid="detail-holdings-footnote" className="space-y-1 text-xs text-muted-foreground">
          <p>An em dash under Avg price is never a zero. It means one of these:</p>
          <ul className="list-disc space-y-0.5 pl-5">
            {reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
          <p>{NO_SINCE_PURCHASE}</p>
        </div>
      ) : null}

      {basketBacked && !anyTarget ? (
        <p data-testid="detail-no-targets" className="text-xs text-muted-foreground">
          {NO_TARGET_WEIGHTS}
        </p>
      ) : null}
    </Block>
  );
}

function Block({ children }: { children: React.ReactNode }) {
  return (
    <section
      aria-label="Holdings"
      className="flex flex-col gap-3 rounded-xl border border-border/70 bg-card p-4"
    >
      <h2 className="text-sm font-semibold">What this portfolio holds</h2>
      {children}
    </section>
  );
}
