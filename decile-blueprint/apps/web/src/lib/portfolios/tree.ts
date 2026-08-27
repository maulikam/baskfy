/**
 * A portfolio is a node in a forest, not a row in a list.
 *
 * Migration 0019 gave `portfolio` a `parent_id` and a `broker_account_id`, and `GET /portfolios`
 * answers with a {@link PortfolioForestOut}: `data` holds the **roots**, each carrying its
 * `children`, and `orphans` holds fragments whose parent is not the caller's. This module turns
 * that shape into rows a list can render, and answers the two questions the flat list could not:
 *
 * 1. **Where does this sit?** — `depth`, and the ancestors above it.
 * 2. **Whose money is in it?** — `broker_account_id`, where **`null` means "spans brokers"** and
 *    must never be rendered as a broker. A container that spans two accounts shown as one
 *    account is a false statement about where the shares are.
 *
 * Two counts are deliberately kept apart. `holdings_count` counts **this** portfolio's own rows —
 * the API says so in as many words, and it did not change meaning when nesting arrived. A
 * subtree's numbers come from the roll-up (`GET /portfolios/{id}/holdings`), which is the only
 * thing that has them. Nothing here adds child counts into a parent's total and calls it one
 * figure.
 */

import type { PortfolioForestOut, PortfolioNodeOut, PortfolioRollupOut } from "@baskfy/api-client";

export interface PortfolioTreeRow {
  node: PortfolioNodeOut;
  /** Indentation level in the rendered forest. An orphan renders at 0 — see {@link orphan}. */
  depth: number;
  /** Names above this one, outermost first. Empty for a root. */
  ancestors: string[];
  hasChildren: boolean;
  /**
   * The parent this row names is not in the caller's set, so the row is unreachable by walking
   * down from a root. Reported, never dropped: a dropped fragment is holdings vanishing from a
   * list with nobody told.
   */
  orphan: boolean;
}

function walk(
  nodes: readonly PortfolioNodeOut[],
  depth: number,
  ancestors: string[],
  orphan: boolean,
  out: PortfolioTreeRow[],
): PortfolioTreeRow[] {
  for (const node of nodes) {
    const children = node.children ?? [];
    out.push({ node, depth, ancestors, hasChildren: children.length > 0, orphan });
    walk(children, depth + 1, [...ancestors, node.name], orphan, out);
  }
  return out;
}

/** Depth-first, parents before children — the order a nested list is read in. */
export function flattenNodes(nodes: readonly PortfolioNodeOut[], orphan = false): PortfolioTreeRow[] {
  return walk(nodes, 0, [], orphan, []);
}

export interface ForestRows {
  rows: PortfolioTreeRow[];
  orphanRows: PortfolioTreeRow[];
  /** Every portfolio the forest names, roots, children and orphans alike. */
  total: number;
  deepest: number;
}

export function forestRows(forest: PortfolioForestOut | null | undefined): ForestRows {
  const rows = flattenNodes(forest?.data ?? []);
  const orphanRows = flattenNodes(forest?.orphans ?? [], true);
  const deepest = [...rows, ...orphanRows].reduce((max, row) => Math.max(max, row.depth), 0);
  return { rows, orphanRows, total: rows.length + orphanRows.length, deepest };
}

/** Every node in the forest, flat — for the places that need ids rather than shape. */
export function forestNodes(forest: PortfolioForestOut | null | undefined): PortfolioNodeOut[] {
  const { rows, orphanRows } = forestRows(forest);
  return [...rows, ...orphanRows].map((row) => row.node);
}

/** The ids whose broker split is worth asking the server for: the containers that span. */
export function spanningNodeIds(forest: PortfolioForestOut | null | undefined): number[] {
  return forestNodes(forest)
    .filter((node) => node.broker_account_id === null || node.broker_account_id === undefined)
    .map((node) => node.id);
}

export type BrokerAttribution =
  | {
      kind: "account";
      brokerAccountId: number;
      brokerId: string | null;
      /** What to put on the row. Never a guess: falls back to the account number. */
      label: string;
    }
  | {
      kind: "spans";
      /** How many broker accounts the subtree actually holds money at. `null` = not looked up. */
      brokerCount: number | null;
      label: string;
    };

export interface BrokerAccountName {
  brokerId: string | null;
  label: string | null;
}

/** `broker_account_id → (broker id, the user's own label)`, gathered from roll-ups already read. */
export function brokerNamesFrom(
  rollups: ReadonlyArray<PortfolioRollupOut | null | undefined>,
): Map<number, BrokerAccountName> {
  const names = new Map<number, BrokerAccountName>();
  for (const rollup of rollups) {
    for (const line of rollup?.by_broker ?? []) {
      names.set(line.broker_account_id, {
        brokerId: line.broker_id ?? null,
        label: line.label ?? null,
      });
    }
  }
  return names;
}

export function brokerAccountLabel(
  brokerAccountId: number,
  names?: ReadonlyMap<number, BrokerAccountName>,
): string {
  const known = names?.get(brokerAccountId);
  if (known?.label) return known.label;
  if (known?.brokerId) return known.brokerId;
  return `Broker account ${brokerAccountId}`;
}

/**
 * What this row may say about its broker.
 *
 * `broker_account_id === null` is a **roll-up container**: it declares nothing about where the
 * shares are, and the money underneath it may sit at several accounts. It gets "Connected to N
 * brokers" when a roll-up has been read for it and "Connected to your brokers" when one has not —
 * never the name of
 * one broker, which is the false single attribution this whole leaf exists to remove.
 */
export function attributionFor(
  node: PortfolioNodeOut,
  options: {
    rollup?: PortfolioRollupOut | null | undefined;
    names?: ReadonlyMap<number, BrokerAccountName> | undefined;
  } = {},
): BrokerAttribution {
  const accountId = node.broker_account_id;
  if (accountId !== null && accountId !== undefined) {
    const known = options.names?.get(accountId);
    return {
      kind: "account",
      brokerAccountId: accountId,
      brokerId: known?.brokerId ?? null,
      label: brokerAccountLabel(accountId, options.names),
    };
  }
  const lines = options.rollup?.by_broker;
  if (lines === undefined) {
    return { kind: "spans", brokerCount: null, label: "Connected to your brokers" };
  }
  const count = lines.length;
  if (count === 0) {
    return { kind: "spans", brokerCount: 0, label: "Connected to your brokers · nothing here yet" };
  }
  return {
    kind: "spans",
    brokerCount: count,
    label: `Connected to ${count} broker${count === 1 ? "" : "s"}`,
  };
}

/**
 * The row's own holdings count, said in words that cannot be mistaken for a subtree total.
 *
 * `holdings_count` counts this portfolio's rows and nothing below it. A parent that reads
 * "40 holdings" while its children hold another 60 is not wrong — it is answering a different
 * question — so the sentence names which question it answered.
 */
export function describeOwnHoldings(row: PortfolioTreeRow): string {
  const count = row.node.holdings_count;
  const noun = `${count} holding${count === 1 ? "" : "s"}`;
  if (!row.hasChildren) return `${noun} filed here`;
  return `${noun} filed here · portfolios inside this one are counted separately`;
}

/** Every id at or below this node — the set a move may not land inside. */
export function subtreeIds(node: PortfolioNodeOut): number[] {
  return [node.id, ...(node.children ?? []).flatMap(subtreeIds)];
}

export interface ParentChoice {
  id: number;
  /** Indented by depth so the option list reads as the tree it is choosing a place in. */
  label: string;
}

/**
 * Where this portfolio could legally be filed.
 *
 * Itself and its own descendants are excluded, because re-parenting under your own child is the
 * cycle the server refuses — offering it and then reporting the refusal is a worse conversation
 * than not offering it. Everything else is offered: the server still owns the depth cap and the
 * ownership check, and this list is a convenience, never the enforcement.
 */
export function parentChoices(
  rows: readonly PortfolioTreeRow[],
  node: PortfolioNodeOut,
): ParentChoice[] {
  const forbidden = new Set(subtreeIds(node));
  return rows
    .filter((row) => !forbidden.has(row.node.id))
    .map((row) => ({
      id: row.node.id,
      label: `${"— ".repeat(row.depth)}${row.node.name}`,
    }));
}
