"use client";

import type { ReactNode } from "react";
import { ArrowRight, CircleAlert } from "lucide-react";

import { MetricValue, Panel } from "@/components/portfolio/detail/primitives";
import { Button } from "@/components/ui/button";
import type { HoldingRow } from "@/lib/portfolio/detail-tabs";

/**
 * The Rebalance tab: an entry point, and an honest account of what is behind it.
 *
 * **This tab builds no drawer.** The rebalance preview is PC4's leaf
 * (`components/portfolio/rebalance/*`, `lib/portfolio/rebalance-preview.ts`), and
 * `docs/PORTFOLIO-COMMAND-CENTER.md` §6.1 is explicit that no leaf edits another leaf's file.
 * So this renders the entry point and the parent passes the drawer in. The prop contract is in
 * `docs/pc-findings/pc3.md`:
 *
 * * `rebalanceSlot?: ReactNode` — PC4's drawer, or its trigger, rendered in place of the
 *   placeholder below.
 * * `onOpenRebalance?: () => void` — when the parent would rather own the open state than the
 *   markup. With neither, the button is disabled beside the reason, which is the treatment PC1
 *   arrived at after shipping a primary action wired to a callback nobody passed.
 *
 * ## What the reader is told while it is not wired
 *
 * Not "coming soon". The two facts that matter about a Baskfy rebalance are that it produces a
 * plan rather than an order, and that the plan carries weights rather than quantities — PC4's own
 * finding, recorded in §6.3: `RebalanceOut` has entries, exits, holds and target weights, and no
 * quantities, no cash delta, no turnover and no costs. A quantity derived from weight times value
 * over price looks exactly like an instruction, and this product places live orders.
 *
 * ## Drift is here because drift is a fact about the holdings
 *
 * A basket-backed portfolio already knows how far each name sits from its target, and that is a
 * measurement rather than a proposal. It is shown. A hand-grouped portfolio has no target at all
 * and the tab says which of the two this is rather than showing an empty table.
 */

export interface RebalanceTabProps {
  basketBacked: boolean;
  basketName: string | null;
  /** Status the summary reports, e.g. "Rebalance due". */
  status: string;
  rows: readonly HoldingRow[];
  executionNote: string;
  /** PC4's drawer, passed in by the parent. */
  rebalanceSlot?: ReactNode;
  onOpenRebalance?: (() => void) | undefined;
}

export function RebalanceTab({
  basketBacked,
  basketName,
  status,
  rows,
  executionNote,
  rebalanceSlot,
  onOpenRebalance,
}: RebalanceTabProps) {
  const drifted = rows.filter((row) => row.drift.value !== null);

  return (
    <>
      <Panel
        title="Preparing a rebalance"
        blurb={executionNote}
        testId="rebalance-entry"
        actions={
          onOpenRebalance ? (
            <Button variant="primary" size="sm" onClick={onOpenRebalance} data-testid="rebalance-open">
              Review the plan
              <ArrowRight aria-hidden="true" />
            </Button>
          ) : rebalanceSlot === undefined ? (
            <Button
              variant="primary"
              size="sm"
              disabled
              title="The rebalance preview is not wired into this page yet."
              data-testid="rebalance-open"
            >
              Review the plan
            </Button>
          ) : null
        }
      >
        {rebalanceSlot ?? (
          <div className="space-y-2 px-4 py-4 text-sm" data-testid="rebalance-placeholder">
            <p className="flex items-start gap-2 text-muted-foreground">
              <CircleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
              <span className="max-w-[76ch] leading-snug">
                The rebalance preview is a separate surface and is not attached to this page yet.
                Nothing is hidden behind the button above: until the drawer is passed in, there is
                no plan for it to open.
              </span>
            </p>
            <p className="max-w-[76ch] text-xs leading-snug text-muted-foreground">
              When it is attached, it will show the names to enter, the names to exit, the names to
              hold, and the target weights behind each. It will <strong>not</strong> show
              quantities, a cash delta, turnover or costs: the rebalance payload carries none of
              them, and a quantity worked out from weight times value over price would look exactly
              like an instruction to trade a number of shares that nobody calculated.
            </p>
            <p className="max-w-[76ch] text-xs leading-snug text-muted-foreground">
              This portfolio&rsquo;s status is currently: {status}.
            </p>
          </div>
        )}
      </Panel>

      <Panel
        title={basketBacked ? "How far each name sits from its target" : "This portfolio has no targets to drift from"}
        blurb={
          basketBacked
            ? `Measured against ${basketName ?? "the published model"}. This is a measurement of what you hold, not a proposal to change it.`
            : "A rebalance is computed against a model or a screen. A portfolio you grouped by hand has neither, so there is nothing to be off."
        }
        testId="rebalance-drift"
      >
        {!basketBacked ? (
          <p className="max-w-[76ch] px-4 py-4 text-sm leading-snug text-muted-foreground">
            Baskfy has no notion of a target weight for a hand-grouped portfolio. Inventing one, by
            splitting equally across the names held, would show every portfolio as perfectly on
            target forever, which is the same lie as a zero average price and would look exactly
            like success. Linking this portfolio to a published model, or building it from a
            screen, is what gives it targets.
          </p>
        ) : drifted.length === 0 ? (
          <p className="max-w-[76ch] px-4 py-4 text-sm leading-snug text-muted-foreground">
            This portfolio tracks {basketName ?? "a published model"}, but no target weights have
            been published against it yet, so there is nothing to measure the drift against. The
            columns appear here as soon as the model publishes a version against this portfolio.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[32rem] text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-muted-foreground">
                  <th scope="col" className="px-4 py-2 text-left font-medium">
                    Security
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    Weight
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    Target
                  </th>
                  <th scope="col" className="px-4 py-2 text-right font-medium">
                    Drift
                  </th>
                </tr>
              </thead>
              <tbody>
                {[...drifted]
                  .sort(
                    (left, right) =>
                      Math.abs(right.raw.drift ?? 0) - Math.abs(left.raw.drift ?? 0),
                  )
                  .map((row) => (
                    <tr
                      key={row.key}
                      data-testid={`rebalance-drift-${row.symbol}`}
                      className="border-b border-border/50 last:border-0"
                    >
                      <th scope="row" className="px-4 py-2 text-left font-normal">
                        <span className="block text-sm font-medium">{row.symbol}</span>
                        <span className="block text-xs text-muted-foreground">{row.name}</span>
                      </th>
                      <td className="px-3 py-2 text-right">
                        <MetricValue
                          metric={row.weight}
                          kind="percent"
                          compact={row.weight.value === null}
                          short="not priced"
                        />
                      </td>
                      <td className="px-3 py-2 text-right">
                        <MetricValue
                          metric={row.targetWeight}
                          kind="percent"
                          compact={row.targetWeight.value === null}
                          short="no target"
                        />
                      </td>
                      <td className="px-4 py-2 text-right">
                        <MetricValue
                          metric={row.drift}
                          kind="points"
                          signed
                          compact={row.drift.value === null}
                          short="no target"
                        />
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="border-t border-border px-4 py-2.5 text-xs leading-snug text-muted-foreground">
          Drift is weight minus target in percentage points. It describes what you hold. It is not
          a recommendation to buy or sell anything, and nothing on this page can place an order.
        </p>
      </Panel>
    </>
  );
}
