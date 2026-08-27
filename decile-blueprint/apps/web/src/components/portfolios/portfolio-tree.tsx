import type { PortfolioForestOut, PortfolioRollupOut } from "@baskfy/api-client";
import { AlertTriangle, Building2, Layers } from "lucide-react";
import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import {
  attributionFor,
  brokerNamesFrom,
  describeOwnHoldings,
  forestRows,
  parentChoices,
  type BrokerAttribution,
  type PortfolioTreeRow,
} from "@/lib/portfolios/tree";
import { cn } from "@/lib/utils";

/**
 * The portfolio forest, drawn as a tree.
 *
 * Before migration 0019 this was a flat list, and it had to be: `portfolio` had four columns and
 * no parent. It now nests, so a child is drawn **under** its parent and indented from it, and
 * every row says two things the flat list could not:
 *
 * 1. **Which broker account it is attributed to.** `broker_account_id === null` is not "no
 *    broker" — it is a container that reaches across them, and it says "Connected to N brokers"
 *    rather than
 *    picking one of the N and printing its name. A false single attribution is the specific bug
 *    this row exists to prevent.
 * 2. **What its holdings count counts.** `holdings_count` is this portfolio's own rows. It was
 *    that before nesting and it is that now; a parent's figure does not swallow its children's,
 *    and the caption says so rather than leaving the reader to assume either way. Subtree totals
 *    come from the roll-up, which is a link away.
 *
 * Orphans — fragments whose parent is not the caller's — are rendered in their own block with the
 * damage said out loud. They are unreachable by walking down from a root, so a list that only
 * walked roots would drop them silently, and holdings disappearing from a page with nobody told
 * is worse than an ugly warning.
 *
 * The "Move to group" control is where the grouping is made, and it has to express two different
 * requests: choosing a parent sends that id, and choosing **Top level** sends an explicit `null`
 * that promotes the portfolio to a root. Omitting the field would mean "leave the parent alone",
 * which is a third thing. `onMove` therefore takes `number | null` and never `undefined`.
 *
 * Nothing here is the enforcement. The server refuses a cycle and the depth cap; the option list
 * merely leaves out this portfolio and its own descendants, because offering a move and then
 * reporting its refusal is a worse conversation than not offering it.
 */

function AttributionBadge({ attribution }: { attribution: BrokerAttribution }) {
  const spans = attribution.kind === "spans";
  return (
    <Badge
      variant={spans ? "neutral" : "outline"}
      data-testid="broker-attribution"
      data-attribution={attribution.kind}
      {...(spans && attribution.brokerCount !== null
        ? { "data-broker-count": String(attribution.brokerCount) }
        : {})}
    >
      {spans ? <Layers aria-hidden="true" className="size-3" /> : <Building2 aria-hidden="true" className="size-3" />}
      {attribution.label}
    </Badge>
  );
}

export interface MoveRequest {
  id: number;
  /** `null` promotes to a root. Never `undefined` — that would be a different request. */
  parentId: number | null;
}

function TreeRow({
  row,
  rollup,
  names,
  choices,
  onMove,
  moving,
}: {
  row: PortfolioTreeRow;
  rollup: PortfolioRollupOut | null;
  names: ReadonlyMap<number, { brokerId: string | null; label: string | null }>;
  choices: { id: number; label: string }[];
  onMove: ((request: MoveRequest) => void) | undefined;
  moving: boolean;
}) {
  const attribution = attributionFor(row.node, { rollup, names });
  return (
    <li
      data-testid="portfolio-tree-row"
      data-portfolio-id={row.node.id}
      data-depth={row.depth}
      data-orphan={row.orphan ? "true" : "false"}
      className={cn(
        "rounded-lg border border-border/70 bg-card px-3 py-2.5",
        row.depth > 0 && "border-l-2 border-l-accent/40",
      )}
      style={{ marginLeft: `${row.depth * 1.25}rem` }}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold">{row.node.name}</p>
          {row.ancestors.length > 0 ? (
            <p className="truncate text-xs text-muted-foreground">
              under {row.ancestors.join(" › ")}
            </p>
          ) : null}
          <p className="mt-0.5 text-xs text-muted-foreground">{describeOwnHoldings(row)}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <AttributionBadge attribution={attribution} />
          <Link
            href={`/portfolios/${row.node.id}/brokers` as never}
            className="text-xs underline underline-offset-4 hover:text-foreground"
          >
            Whose money is where
          </Link>
          {onMove ? (
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <span>Move to group</span>
              <select
                aria-label={`Move ${row.node.name} to a group`}
                data-testid="move-portfolio"
                data-portfolio-id={row.node.id}
                disabled={moving}
                value={row.node.parent_id ?? ""}
                onChange={(event) => {
                  const chosen = event.target.value;
                  onMove({ id: row.node.id, parentId: chosen === "" ? null : Number(chosen) });
                }}
                className="rounded-md border bg-background px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option value="">Top level</option>
                {choices.map((choice) => (
                  <option key={choice.id} value={choice.id}>
                    {choice.label}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
        </div>
      </div>
    </li>
  );
}

export interface PortfolioTreeProps {
  forest: PortfolioForestOut | null | undefined;
  /** Roll-ups already read, keyed by portfolio id. A missing one downgrades the wording, never the truth. */
  rollups?: ReadonlyMap<number, PortfolioRollupOut>;
  /** Omit to render the tree read-only — the control disappears rather than sitting there inert. */
  onMove?: (request: MoveRequest) => void;
  moving?: boolean;
}

export function PortfolioTree({ forest, rollups, onMove, moving = false }: PortfolioTreeProps) {
  const { rows, orphanRows, total, deepest } = forestRows(forest);
  if (total === 0) return null;

  const names = brokerNamesFrom([...(rollups?.values() ?? [])]);
  const everyRow = [...rows, ...orphanRows];

  return (
    <section aria-label="Your portfolios" data-testid="portfolio-tree" className="space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold">How your portfolios are grouped</h2>
        <p className="text-xs text-muted-foreground" data-testid="portfolio-tree-summary">
          {total} portfolio{total === 1 ? "" : "s"}
          {deepest > 0 ? `, ${deepest + 1} levels deep` : ", all at the top level"}
        </p>
      </div>

      <ul className="space-y-1.5">
        {rows.map((row) => (
          <TreeRow
            key={row.node.id}
            row={row}
            rollup={rollups?.get(row.node.id) ?? null}
            names={names}
            choices={parentChoices(everyRow, row.node)}
            onMove={onMove}
            moving={moving}
          />
        ))}
      </ul>

      {orphanRows.length > 0 ? (
        <div
          data-testid="portfolio-orphans"
          className="space-y-2 rounded-lg border border-warning/40 bg-warning-muted/40 p-3"
        >
          <p className="flex items-center gap-2 text-sm font-semibold">
            <AlertTriangle aria-hidden="true" className="size-4" />
            {orphanRows.length} portfolio{orphanRows.length === 1 ? "" : "s"} could not be placed
          </p>
          <p className="text-xs leading-relaxed text-muted-foreground">
            {orphanRows.length === 1 ? "This one names" : "These name"} a parent portfolio that is
            not yours. That cannot happen through this app, so it is a sign of damage rather than
            something you did — {orphanRows.length === 1 ? "it is" : "they are"} listed here rather
            than dropped, because holdings vanishing from a page without anyone being told is the
            worse failure.
          </p>
          <ul className="space-y-1.5">
            {orphanRows.map((row) => (
              <TreeRow
                key={row.node.id}
                row={row}
                rollup={rollups?.get(row.node.id) ?? null}
                names={names}
                choices={parentChoices(everyRow, row.node)}
                onMove={onMove}
                moving={moving}
              />
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
