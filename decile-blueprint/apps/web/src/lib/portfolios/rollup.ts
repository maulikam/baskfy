/**
 * The consolidated view: whose money is where, and whose money is *not in this figure*.
 *
 * `GET /portfolios/{id}/holdings` answers with a {@link PortfolioRollupOut} — every holding in
 * the subtree, split per broker account, plus an `unattributed` line and a `total`. Its stated
 * wire invariant is `total == sum(by_broker) + unattributed`, exact to the last digit, so this
 * module **checks** it rather than trusting it: a total that does not reconcile is shown as not
 * reconciling. Every sum is done in `bigint` (see `./decimal`), never in a float.
 *
 * The second job is harder and is the one a consolidated screen usually gets wrong. Only Zerodha
 * has a wired holdings adapter; the other nine brokers in the catalog report `holdings_sync
 * "planned"`. A figure captioned "your holdings" that silently omits nine brokers is a wrong
 * number wearing a right label, so {@link describeCoverage} names both sides out loud: which
 * accounts are in the figure, and which brokers cannot put anything in it yet.
 *
 * `cost` is money put in, never a valuation — the endpoint reads no quote and is structurally
 * incapable of marking anything to market. The copy says so where the number is shown.
 */

import type { PortfolioRollupOut, Schemas } from "@baskfy/api-client";

import { addDecimalStrings, compareDecimalStrings, decimalStringsEqual } from "@/lib/portfolios/decimal";

type BrokerLineOut = Schemas["BrokerLineOut"];
type TotalsOut = Schemas["TotalsOut"];
type BrokerOut = Schemas["BrokerOut"];
export type BrokerListOut = Schemas["BrokerListOut"];

/** The capability value that means "this broker can actually fetch holdings today". */
export const HOLDINGS_SYNC_READY = "ready";

export interface ConsolidatedLine {
  key: string;
  /** `null` on the unattributed line — holdings with no account, not an account called null. */
  brokerAccountId: number | null;
  brokerId: string | null;
  label: string;
  cost: string;
  quantity: string;
  holdings: number;
  unattributed: boolean;
}

export interface CoverageBroker {
  id: string;
  name: string;
  /** The catalog's own word: `ready`, `planned`, `partner`. */
  holdingsSync: string;
  connected: boolean;
}

export interface BrokerCoverage {
  /** Accounts that put something into this total. */
  contributing: { brokerAccountId: number; label: string }[];
  canSync: CoverageBroker[];
  cannotSync: CoverageBroker[];
  /** True when the catalog was not available, so coverage is unknown rather than complete. */
  unknown: boolean;
  sentence: string;
}

export interface TotalsInvariant {
  holds: boolean;
  /** `sum(by_broker) + unattributed`, added exactly. `null` when a figure was unparseable. */
  sum: string | null;
  total: string;
}

export interface ConsolidatedHoldings {
  portfolioId: number;
  lines: ConsolidatedLine[];
  total: TotalsOut;
  unattributed: TotalsOut;
  spansBrokers: boolean;
  declarationConflicts: boolean;
  declaredBrokerAccountId: number | null;
  subtreeSize: number;
  invariant: TotalsInvariant;
  coverage: BrokerCoverage;
}

function lineLabel(line: BrokerLineOut): string {
  if (line.label) return line.label;
  if (line.broker_id) return line.broker_id;
  return `Broker account ${line.broker_account_id}`;
}

/** Biggest cost first, so the reader meets the money in the order it matters. */
function byCostDescending(left: ConsolidatedLine, right: ConsolidatedLine): number {
  const comparison = compareDecimalStrings(right.cost, left.cost);
  return Number.isNaN(comparison) ? 0 : comparison;
}

export function checkTotals(rollup: PortfolioRollupOut): TotalsInvariant {
  const sum = addDecimalStrings([
    ...rollup.by_broker.map((line) => line.totals.cost),
    rollup.unattributed.cost,
  ]);
  const holds = sum !== null && decimalStringsEqual(sum, rollup.total.cost);
  return { holds, sum, total: rollup.total.cost };
}

function nameList(names: string[]): string {
  if (names.length === 0) return "";
  if (names.length === 1) return names[0] ?? "";
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1] ?? ""}`;
}

function coverageBroker(broker: BrokerOut): CoverageBroker {
  return {
    id: broker.id,
    name: broker.name,
    holdingsSync: broker.capabilities.holdings_sync,
    connected: broker.connected,
  };
}

/**
 * Say, in one paragraph, what the total covers and what it cannot.
 *
 * The sentence is built rather than written so it cannot drift from the catalog: the day a tenth
 * broker gets an adapter, the copy stops naming it without anyone editing a string.
 */
export function describeCoverage(
  rollup: PortfolioRollupOut,
  catalog: BrokerListOut | null | undefined,
): BrokerCoverage {
  const contributing = rollup.by_broker.map((line) => ({
    brokerAccountId: line.broker_account_id,
    label: lineLabel(line),
  }));

  if (!catalog) {
    return {
      contributing,
      canSync: [],
      cannotSync: [],
      unknown: true,
      sentence:
        "The broker catalog could not be read, so this figure cannot say which brokers it leaves out. Treat it as covering only what is filed here.",
    };
  }

  const brokers = catalog.brokers.map(coverageBroker);
  const canSync = brokers.filter((broker) => broker.holdingsSync === HOLDINGS_SYNC_READY);
  const cannotSync = brokers.filter((broker) => broker.holdingsSync !== HOLDINGS_SYNC_READY);

  const filed =
    contributing.length === 0
      ? "No broker account has holdings filed under this portfolio yet."
      : `Filed under ${contributing.length} broker account${contributing.length === 1 ? "" : "s"}: ${nameList(contributing.map((line) => line.label))}.`;

  const syncing =
    canSync.length === 0
      ? "No broker in the catalog can sync holdings yet."
      : `${nameList(canSync.map((broker) => broker.name))} ${canSync.length === 1 ? "is the only broker that can" : "are the only brokers that can"} sync holdings into Baskfy.`;

  const missing =
    cannotSync.length === 0
      ? ""
      : ` ${nameList(cannotSync.map((broker) => broker.name))} cannot — ${cannotSync.length === 1 ? "it holds" : "anything you hold there"} nothing in this figure unless you filed it by hand.`;

  return {
    contributing,
    canSync,
    cannotSync,
    unknown: false,
    sentence: `${filed} ${syncing}${missing}`,
  };
}

export function consolidate(input: {
  rollup: PortfolioRollupOut;
  catalog?: BrokerListOut | null | undefined;
}): ConsolidatedHoldings {
  const { rollup } = input;
  const lines: ConsolidatedLine[] = rollup.by_broker
    .map((line) => ({
      key: `account-${line.broker_account_id}`,
      brokerAccountId: line.broker_account_id,
      brokerId: line.broker_id ?? null,
      label: lineLabel(line),
      cost: line.totals.cost,
      quantity: line.totals.quantity,
      holdings: line.totals.holdings,
      unattributed: false,
    }))
    .sort(byCostDescending);

  if (rollup.unattributed.holdings > 0) {
    lines.push({
      key: "unattributed",
      brokerAccountId: null,
      brokerId: null,
      label: "Not attributed to a broker",
      cost: rollup.unattributed.cost,
      quantity: rollup.unattributed.quantity,
      holdings: rollup.unattributed.holdings,
      unattributed: true,
    });
  }

  return {
    portfolioId: rollup.portfolio_id,
    lines,
    total: rollup.total,
    unattributed: rollup.unattributed,
    spansBrokers: rollup.spans_brokers,
    declarationConflicts: rollup.declaration_conflicts,
    declaredBrokerAccountId: rollup.declared_broker_account_id ?? null,
    subtreeSize: rollup.subtree_portfolio_ids.length,
    invariant: checkTotals(rollup),
    coverage: describeCoverage(rollup, input.catalog ?? null),
  };
}
