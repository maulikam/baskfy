import type { Route } from "next";

import type { Schemas } from "@baskfy/api-client";

import { EMPTY_CELL, formatNumber, formatTradeDate } from "@/lib/format";

/**
 * The presentation layer for `PORTFOLIO_REDESIGN.md` §6's single screen.
 *
 * ## What this module is, and what it deliberately is not
 *
 * `GET /api/v1/portfolio/overview` already answers with every judgement the spec cares about
 * decided **on the server**: `counts_toward_total` rather than a kind the client has to map to
 * arithmetic (§4.1), `source_badge` already worded to §9, `label`/`since`/`unavailable_reason`
 * on every rate so criterion 3 cannot be reached without them, and `monitoring_excluded_note`
 * carrying §4.1's sentence verbatim. None of that is recomputed here. A second copy of the rules
 * in TypeScript is a second copy free to drift from the one the API's own tests assert.
 *
 * So this file holds only what is genuinely the browser's job:
 *
 * * **Units.** `baskfy_core.portfolio_nav` stores return ratios as fractions at six decimal
 *   places (`RETURN_PRECISION`); rupees arrive as `numeric` strings, never floats (house rule 9).
 *   {@link formatRate} is the one place a fraction becomes a percentage, matching the reasoning
 *   in `lib/format`'s `formatFraction`: exactly one multiplication by 100 in the whole system.
 * * **The two rate shapes, normalised.** `ReturnFigureOut` (a §5.2 headline metric, with a
 *   `MetricKind`) and `LabelledRateOut` (consolidated XIRR/TWR, which has no `MetricKind` yet)
 *   carry the same four display facts. {@link DisplayReturn} is those four facts, so one
 *   component can render both and neither can be rendered bare.
 * * **Tab filtering** over lists the API has already partitioned into `portfolios` (capital) and
 *   `monitoring_views`.
 * * **Deep links.** `AttentionKind` deliberately names no URL — core naming a route would be
 *   core knowing about the web app — so §6.4's "deep-links to its resolution flow" is resolved
 *   here, where the route table lives.
 */

export type Overview = Schemas["OverviewOut"];
export type PortfolioRow = Schemas["PortfolioRowOut"];
export type Hero = Schemas["HeroOut"];
export type MoneyMove = Schemas["MoneyMoveOut"];
export type ReturnFigure = Schemas["ReturnFigureOut"];
export type LabelledRate = Schemas["LabelledRateOut"];
export type NavSeries = Schemas["NavSeriesOut"];
export type NavPoint = Schemas["baskfy_api__routers__portfolio_overview__NavPointOut"];
export type DrawdownPoint =
  Schemas["baskfy_api__routers__portfolio_overview__DrawdownPointOut"];
export type Attention = Schemas["AttentionOut"];
export type SyncStatus = Schemas["SyncStatusOut"];
export type PortfolioDetail =
  Schemas["baskfy_api__routers__portfolio_overview__PortfolioDetailOut"];
export type DetailHolding = Schemas["DetailHoldingOut"];
export type ActivityItem = Schemas["ActivityItemOut"];
export type NavRange = Schemas["NavRange"];

/**
 * §4.1's sentence, as a fallback for the `monitoring_excluded_note` the API sends.
 *
 * The API is the source; this constant exists so a component still says the required sentence if
 * an older payload omits the optional field, rather than silently labelling nothing.
 */
export const MONITORING_NOTE =
  "Monitoring view — overlaps with other portfolios, excluded from totals.";

/* ------------------------------------------------------------------ *
 * Units
 * ------------------------------------------------------------------ */

/**
 * A stored return **ratio** as a signed percentage. `0.1234` → `"+12.34%"`.
 *
 * The multiplication by 100 happens here and nowhere else, for the reason `formatFraction`
 * records: one place in the system where a fraction becomes a percentage means the API, the
 * table and the chart axis cannot disagree about whether they were given 12.34 or 0.1234.
 */
export function formatRate(value: string | number | null | undefined, decimals = 2): string {
  if (value === null || value === undefined || value === "") return EMPTY_CELL;
  const numeric = typeof value === "string" ? Number(value) : value;
  if (Number.isNaN(numeric)) return EMPTY_CELL;
  return formatNumber(numeric * 100, { decimals, signed: true, suffix: "%" });
}

/** "₹12,34,567", the em dash when absent, dots when the reader has hidden amounts (§6.1). */
export function formatMoney(value: string | number | null | undefined, visible = true): string {
  if (value === null || value === undefined || value === "") return EMPTY_CELL;
  if (!visible) return "••••••";
  return `₹${formatNumber(value, { decimals: 0 })}`;
}

/** A signed rupee move — "+₹4,120" / "−₹910". */
export function formatMoneyMove(
  value: string | number | null | undefined,
  visible = true,
): string {
  if (value === null || value === undefined || value === "") return EMPTY_CELL;
  if (!visible) return "••••";
  const numeric = typeof value === "string" ? Number(value) : value;
  if (Number.isNaN(numeric)) return EMPTY_CELL;
  const sign = numeric > 0 ? "+" : numeric < 0 ? "−" : "";
  return `${sign}₹${formatNumber(Math.abs(numeric), { decimals: 0 })}`;
}

/** −1 / 0 / +1 for a stored numeric string, so colour is chosen once. */
export function signOf(value: string | number | null | undefined): -1 | 0 | 1 {
  if (value === null || value === undefined || value === "") return 0;
  const numeric = typeof value === "string" ? Number(value) : value;
  if (Number.isNaN(numeric) || numeric === 0) return 0;
  return numeric > 0 ? 1 : -1;
}

export function toneFor(value: string | number | null | undefined): string {
  const sign = signOf(value);
  if (value === null || value === undefined || value === "") return "text-muted-foreground";
  return sign > 0 ? "text-positive" : sign < 0 ? "text-negative" : "text-foreground";
}

/* ------------------------------------------------------------------ *
 * Criterion 3 — a return carries its label and its start date
 * ------------------------------------------------------------------ */

/**
 * The four facts every displayed rate needs, whichever payload shape it arrived in.
 *
 * §11 criterion 3: *"Every displayed return number carries a label stating what it is (TWR /
 * XIRR / since-grouped) and its start date on hover."* `label` is the server's own wording, not
 * a second vocabulary assembled in the browser.
 */
export interface DisplayReturn {
  readonly label: string;
  /** The stored ratio, as a `numeric` string. `null` when it cannot be computed honestly. */
  readonly value: string | null;
  readonly since: string | null;
  readonly unavailableReason: string | null;
  /** §11 criterion 5: a publisher's record, never blended with the user's own. */
  readonly isModel: boolean;
}

export function fromFigure(figure: ReturnFigure): DisplayReturn {
  return {
    label: figure.label,
    value: figure.value ?? null,
    since: figure.since,
    unavailableReason: figure.unavailable_reason ?? null,
    isModel: figure.is_model === true,
  };
}

export function fromRate(rate: LabelledRate): DisplayReturn {
  return {
    label: rate.label,
    value: rate.value ?? null,
    since: rate.since ?? null,
    unavailableReason: rate.unavailable_reason ?? null,
    isModel: false,
  };
}

/** A day's or a lifetime's money move, which carries a percentage and therefore a label too. */
export function fromMove(move: MoneyMove): DisplayReturn {
  return {
    label: move.label,
    value: move.pct ?? null,
    since: move.since ?? null,
    unavailableReason: move.unavailable_reason ?? null,
    isModel: false,
  };
}

/**
 * The hover text: what this number is, and when its clock started.
 *
 * Rendered into `title`, which is what "on hover" means for a mouse and what a keyboard or
 * screen-reader user gets as well. The start date is always present in this string — that is the
 * half of criterion 3 a visible label alone does not satisfy.
 */
export function describeReturn(entry: DisplayReturn): string {
  const window =
    entry.since === null
      ? "Start date not recorded."
      : `Measured since ${formatTradeDate(entry.since)}.`;
  const model = entry.isModel
    ? " This is the published model's own record, not your money and not your return."
    : "";
  const missing =
    entry.value === null && entry.unavailableReason !== null
      ? ` Not shown: ${entry.unavailableReason}`
      : "";
  return `${entry.label}. ${window}${model}${missing}`;
}

/* ------------------------------------------------------------------ *
 * §6.5 — grouping tabs
 * ------------------------------------------------------------------ */

export type GroupingTab =
  | "ALL"
  | "SUBSCRIBED"
  | "MY_STRATEGIES"
  | "MY_SCREENS"
  | "HOLDING_GROUPS"
  | "MONITORING";

export const GROUPING_TABS: readonly { key: GroupingTab; label: string }[] = [
  { key: "ALL", label: "All" },
  { key: "SUBSCRIBED", label: "Subscribed" },
  { key: "MY_STRATEGIES", label: "My strategies" },
  { key: "MY_SCREENS", label: "My screens" },
  { key: "HOLDING_GROUPS", label: "Holding groups" },
  { key: "MONITORING", label: "Monitoring views" },
];

/**
 * The rows behind one tab.
 *
 * `All` is every **capital** portfolio and no monitoring view. The API has already put the two in
 * separate arrays, and this keeps them separate: a monitoring view double-counts the holdings it
 * lenses, so a list mixing the two is a list whose values do not add up — which §11 criterion 2
 * forbids for any total, including a visible subtotal under a table.
 */
export function rowsForTab(overview: Overview, tab: GroupingTab): PortfolioRow[] {
  if (tab === "MONITORING") return [...(overview.monitoring_views ?? [])];
  const capital = [...(overview.portfolios ?? [])];
  if (tab === "ALL") return capital;
  if (tab === "SUBSCRIBED") return capital.filter((row) => row.source === "SUBSCRIBED");
  if (tab === "MY_STRATEGIES") return capital.filter((row) => row.source === "MY_STRATEGY");
  if (tab === "MY_SCREENS") return capital.filter((row) => row.source === "MY_SCREEN");
  return capital.filter((row) => row.source === "HOLDING_GROUP");
}

/**
 * The sum under a table, from the rows that say they belong in one.
 *
 * `counts_toward_total` is read rather than `kind`, because the API states the answer and a
 * client that re-derives it is a client that can derive it wrongly. A monitoring view is dropped
 * before the sum, not zeroed inside it: §11 criterion 2 is about contribution of any kind.
 */
export function totalOfRows(rows: readonly PortfolioRow[]): number | null {
  const values = rows
    .filter((row) => row.counts_toward_total)
    .map((row) => Number(row.value))
    .filter((value) => Number.isFinite(value));
  return values.length === 0 ? null : values.reduce((sum, value) => sum + value, 0);
}

/** True when nothing in the list may be added up — the Monitoring views tab. */
export function tabExcludedFromTotals(rows: readonly PortfolioRow[]): boolean {
  return rows.length > 0 && rows.every((row) => !row.counts_toward_total);
}

/** §8: "Spans brokers" is gone; this is what replaces it. */
export function describeBrokers(row: PortfolioRow): string {
  const names = (row.brokers ?? []).map((broker) => broker.label);
  const count = row.broker_count ?? names.length;
  if (count === 0) return "No broker connected";
  if (count === 1) return names[0] ?? "1 broker";
  return `Connected to ${count} brokers`;
}

/* ------------------------------------------------------------------ *
 * §6.4 — where each attention item resolves
 * ------------------------------------------------------------------ */

export interface AttentionLink {
  readonly href: Route;
  readonly action: string;
}

/**
 * `AttentionKind` names no URL on purpose (`reconciliation.py`: "core naming a URL would be core
 * knowing about the web app"), so the mapping lives here, beside the route table.
 */
const ATTENTION_LINKS: Readonly<Record<string, AttentionLink>> = {
  BROKER_CONNECTION_EXPIRED: { href: "/brokers", action: "Reconnect" },
  RECONCILIATION_PENDING: { href: "/reconcile", action: "Resolve" },
  STALE_PRICE_DATA: { href: "/portfolio/holdings", action: "See which prices are old" },
  HOLDINGS_UNALLOCATED: { href: "/portfolio/holdings", action: "Organize into portfolios" },
  REBALANCE_AVAILABLE: { href: "/portfolio/portfolios", action: "Review the plan" },
};

const ATTENTION_FALLBACK: AttentionLink = { href: "/portfolio/holdings", action: "Open" };

export function attentionLink(kind: string): AttentionLink {
  return ATTENTION_LINKS[kind] ?? ATTENTION_FALLBACK;
}

/* ------------------------------------------------------------------ *
 * §6.1 — the two timestamps, formatted but never merged
 * ------------------------------------------------------------------ */

/** "Prices: close of 21 Aug 2026", or the API's own sentence when there is no date. */
export function pricesLine(overview: Pick<Overview, "prices_as_of" | "prices_label">): string {
  const date = overview.prices_as_of;
  return date ? `Prices: close of ${formatTradeDate(date)}` : overview.prices_label;
}

/** "Holdings synced: 25 Aug 2026", or the API's own sentence when nothing has synced. */
export function syncedLine(
  overview: Pick<Overview, "holdings_synced_on" | "holdings_synced_label">,
): string {
  const date = overview.holdings_synced_on;
  return date ? `Holdings synced: ${formatTradeDate(date)}` : overview.holdings_synced_label;
}
