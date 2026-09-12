import { percentOf } from "@/lib/portfolio/analytics";
import { metric, type Metric } from "@/lib/portfolio/command-center";
import {
  avgPriceReason,
  hasCostBasis,
  isBasketBacked,
  targetWeightOf,
  type DetailHoldingRow,
  type PortfolioSummary,
  type SourcePanel,
} from "@/lib/portfolio/detail-view";
import type {
  ActivityItem,
  DrawdownPoint,
  NavPoint,
  NavSeries,
  PortfolioDetail,
} from "@/lib/portfolio/overview";
import {
  addDecimalStrings,
  compareDecimalStrings,
  parseDecimal,
  roundDecimalString,
  toDecimalString,
} from "@/lib/portfolios/decimal";

/**
 * PC3 — the pure layer under the eight-tab portfolio detail workspace.
 *
 * `docs/PORTFOLIO-COMMAND-CENTER.md` §6.1 gives this leaf `lib/portfolio/detail-tabs.ts` and
 * `components/portfolio/detail/*`; `gates/pc3.md` is the definition of done. The brief asks for
 * Overview, Holdings, Performance, Allocation, Risk, Rebalance, Activity and Settings over the
 * payload `loadPortfolioDetail` already returns, and two of those tabs are mostly blocked by
 * §2.2. A blocked tab is not dropped and is not filled with dashes: it says what it will show
 * and what that needs.
 *
 * WHY THE WHOLE VIEW MODEL IS `Metric`
 * ------------------------------------
 * Every figure below goes through `metric()` from `command-center.ts`, which cannot hold a
 * missing value without also holding the reason it is missing. That makes the brief's hard rule
 * structural rather than a thing eight components have to remember: there is no shape in this
 * module a component could render as a bare "—", because there is no shape that carries a null
 * without a sentence beside it. `detail-tabs.test.ts` asserts it over a deliberately impoverished
 * payload, where every single figure is absent at once.
 *
 * MONEY IS NEVER A FLOAT, AND NEITHER IS A WEIGHT (house rule 9)
 * -------------------------------------------------------------
 * Rupees, weights, drifts and concentration scores are computed on `bigint` scaled integers via
 * `lib/portfolios/decimal` and `analytics.percentOf`. The only `number`s in this file are the
 * `raw` block on each holding row, which exists solely so a table can sort and filter, is never
 * rendered, and is documented as such at its declaration. Annualisation in
 * {@link returnBasis} is the one deliberate exception and it says so at the call site: a return
 * ratio raised to a fractional power has no exact decimal form.
 *
 * NOTHING HERE DERIVES A NUMBER THE PRODUCT HAS NOT MEASURED
 * ---------------------------------------------------------
 * Four derived figures appear below and each is exact algebra over fields the API actually sent,
 * not a model:
 *
 * * **Today's move as a percentage** for one holding is `todays_contribution / (value −
 *   todays_contribution)`. The server builds `todays_contribution` as `quantity × (latest −
 *   previous)` and `value` as `quantity × latest`, so the denominator is exactly
 *   `quantity × previous` — yesterday's close of that position, restated rather than guessed.
 * * **Unrealised P&L as a percentage** is `total_contribution / (value − total_contribution)`,
 *   and by the same server construction (`value − cost_basis`) the denominator is exactly the
 *   cost basis.
 * * **Contribution to the portfolio's return** divides the same numerator by `summary.invested`,
 *   the server's own cost figure, and is declared unavailable, carrying
 *   `invested_unavailable_reason`, whenever that figure is missing.
 * * **Monthly and rolling returns** are read off `NavSeriesOut.drawdown[].index`, the wealth
 *   index the NAV job publishes, never off the value line. Those differ whenever money moved:
 *   assigning ₹50,000 raises the value by ₹50,000 and the return by nothing.
 *
 * What is NOT derived here is the whole of the Risk tab's statistics. `daily_pnl[].pct` exists
 * and an annualised volatility is two lines of arithmetic away, but a volatility with no stated
 * window and no stated annualisation convention is exactly the "unstated model" §2.2 refuses,
 * and this product places live orders. {@link BLOCKED_RISK} is the list, and it is rendered.
 */

/* ------------------------------------------------------------------ *
 * Exact arithmetic the shared decimal module does not carry
 * ------------------------------------------------------------------ */

/** `"-12.50"` for `"12.50"`. Exact: it flips a sign on a scaled integer. */
function negateDecimal(value: string): string | null {
  const parsed = parseDecimal(value);
  return parsed === null ? null : toDecimalString({ units: -parsed.units, scale: parsed.scale });
}

/** `left − right`, exactly. `null` when either side is not a decimal string. */
export function subtractDecimals(
  left: string | null | undefined,
  right: string | null | undefined,
): string | null {
  if (left === null || left === undefined || right === null || right === undefined) return null;
  const negated = negateDecimal(right);
  if (negated === null) return null;
  return addDecimalStrings([left, negated]);
}

/** `left × right`, exactly. Scales add, which is why this cannot round on the way. */
export function multiplyDecimals(
  left: string | null | undefined,
  right: string | null | undefined,
): string | null {
  if (left === null || left === undefined || right === null || right === undefined) return null;
  const a = parseDecimal(left);
  const b = parseDecimal(right);
  if (a === null || b === null) return null;
  return toDecimalString({ units: a.units * b.units, scale: a.scale + b.scale });
}

/** A stored fraction (`"0.1200"`) as a percentage string (`"12.00"`), exact to two places. */
export function fractionToPercent(value: string | null | undefined): string | null {
  if (value === null || value === undefined || value === "") return null;
  return percentOf(value, "1");
}

/** Sorting and filtering only. Never rendered — see the note on {@link HoldingRow.raw}. */
function toNumber(value: string | null): number | null {
  if (value === null) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

const MS_PER_DAY = 86_400_000;

/**
 * The series, oldest first, whatever order it arrived in.
 *
 * Every derivation below reads a series by position: the last point is "today", the first is the
 * start of the window, and a month's return is its last session against the previous month's. All
 * of that is wrong if the array is not in date order. The API sends it ordered and this does not
 * trust that, because the cost of the sort is nothing and the cost of the assumption is a
 * drawdown episode running backwards.
 */
function inDateOrder<T extends { on: string }>(points: readonly T[]): readonly T[] {
  return [...points].sort((left, right) => left.on.localeCompare(right.on));
}

/** Whole days between two ISO dates, UTC, or `null` when either is unparseable. */
export function daysBetween(from: string | null, to: string | null): number | null {
  if (from === null || to === null) return null;
  const start = Date.parse(`${from}T00:00:00Z`);
  const end = Date.parse(`${to}T00:00:00Z`);
  if (Number.isNaN(start) || Number.isNaN(end)) return null;
  return Math.round((end - start) / MS_PER_DAY);
}

/** "2 yr 4 mo" over a year, "84 days" under it. Read at a glance, sorted on the day count. */
function describeSpan(days: number): string {
  if (days < 0) return `${Math.abs(days)} days from now`;
  if (days < 365) return `${days} days`;
  const years = Math.floor(days / 365);
  const months = Math.floor((days % 365) / 30);
  return months === 0 ? `${years} yr` : `${years} yr ${months} mo`;
}

/* ------------------------------------------------------------------ *
 * The eight tabs
 * ------------------------------------------------------------------ */

export type DetailTabId =
  | "overview"
  | "holdings"
  | "performance"
  | "allocation"
  | "risk"
  | "rebalance"
  | "activity"
  | "settings";

export interface DetailTab {
  readonly id: DetailTabId;
  /** The visible label, which is also the tab's accessible name. */
  readonly label: string;
  /** The question this tab answers, shown under the tab strip so a label carries its purpose. */
  readonly question: string;
}

/**
 * The brief's eight, in the brief's order.
 *
 * Ordered by how often a reader needs them rather than alphabetically or by how much data each
 * has: Overview and Holdings answer nine of every ten visits, and Settings is the one nobody
 * opens twice. Rebalance sits between Risk and Activity because it is where the previous five
 * tabs lead when the answer is "something should change".
 */
export const DETAIL_TABS: readonly DetailTab[] = [
  { id: "overview", label: "Overview", question: "What is this portfolio, and how is it doing?" },
  { id: "holdings", label: "Holdings", question: "What is in it, and what is each position doing?" },
  { id: "performance", label: "Performance", question: "How did it get here, and against what?" },
  { id: "allocation", label: "Allocation", question: "How is the money spread, and how concentrated is it?" },
  { id: "risk", label: "Risk", question: "What could hurt, and what do we not yet measure?" },
  { id: "rebalance", label: "Rebalance", question: "What would change, if anything changed?" },
  { id: "activity", label: "Activity", question: "What has actually happened here?" },
  { id: "settings", label: "Settings", question: "What is this portfolio configured as?" },
];

export function isDetailTabId(value: string): value is DetailTabId {
  return DETAIL_TABS.some((tab) => tab.id === value);
}

/* ------------------------------------------------------------------ *
 * §2.2 — what this workspace cannot show, and what each would take
 * ------------------------------------------------------------------ */

/**
 * One blocked metric. `why` is the fact about the payload; `unblockedBy` is the work.
 *
 * These are rendered, in the tab that would have shown them, rather than collected on a
 * single page nobody visits. A reader looking for a sector split should find out on the
 * Allocation tab that there is no sector map, not by reading a design document.
 */
export interface BlockedMetric {
  readonly name: string;
  readonly why: string;
  readonly unblockedBy: string;
}

/** Columns the brief names for a holdings table that `DetailHoldingOut` cannot fill. */
export const BLOCKED_COLUMNS: readonly BlockedMetric[] = [
  {
    name: "Exchange and instrument type",
    why: "the payload identifies an instrument by id, symbol and name only, so there is nothing that says NSE or BSE, equity or ETF",
    // Engineering: an `exchange` and `instrument_type` column on `instrument`, carried into
    // `InstrumentRefOut`. The reader gets what it would take, not the spelling of the column.
    unblockedBy: "the exchange and the instrument type carried through with each holding",
  },
  {
    name: "Available quantity, free against pledged and T1",
    why: "one quantity arrives per holding; the broker's free, T1 and collateral split is not carried into the portfolio payload",
    // Engineering: carry `quantity`, `t1_quantity` and `collateral_quantity` separately through
    // into `DetailHoldingOut`. The *total* is already correct — non-negotiable #2 sums all
    // three — so this is about showing the split, never about a different total.
    unblockedBy:
      "the broker's own three-way split carried through beside the total, not just the total",
  },
  {
    name: "Price age for one holding",
    why: "a single valuation date covers the whole portfolio, so every row here shares one clock",
    unblockedBy: "a per-instrument price timestamp on the holdings payload",
  },
  {
    name: "Realised P&L for one position",
    why: "the payload's since-purchase figure is market value minus cost, which is unrealised by construction; what has already been booked is a portfolio-level number",
    unblockedBy: "a realised-gain field per holding, computed off the trade ledger",
  },
  {
    name: "Distance from the 52-week high, moving-average status, momentum rank",
    why: "those exist only for the names the swing strategy watches, not for an arbitrary holding",
    unblockedBy: "extending the factor pass to every held instrument",
  },
  {
    name: "Liquidity and days to liquidate",
    why: "no traded-volume history is stored per instrument",
    unblockedBy: "a liquidity column on the daily bars",
  },
  {
    name: "Brokerage, taxes and charges on a position",
    // Engineering: the cost model lives in `packages/core`, wired to the weekly rebalancer.
    why: "Baskfy's brokerage and tax model was written for the weekly rebalancer's own trades, and has never been wired to an arbitrary portfolio",
    unblockedBy: "wiring the existing cost model through the portfolio path",
  },
];

/** What an Allocation tab would show with a richer instrument table. */
export const BLOCKED_ALLOCATION: readonly BlockedMetric[] = [
  {
    name: "Allocation by sector",
    why: "there is no sector column on instrument anywhere in Baskfy",
    unblockedBy: "an instrument sector map, sourced and kept current",
  },
  {
    name: "Allocation by industry",
    why: "the same missing map: industry is a finer cut of a classification that does not exist yet",
    unblockedBy: "the same instrument sector and industry map",
  },
  {
    name: "Allocation by market capitalisation",
    why: "market cap is not carried on the holdings payload, so large, mid and small cannot be told apart here",
    unblockedBy: "a market-cap column on instrument, carried into the holdings payload",
  },
  {
    name: "Allocation by geography",
    why: "Baskfy is India-only equities today, so a geography split would have exactly one slice",
    unblockedBy: "instruments listed outside India, and a country column to separate them",
  },
  {
    name: "Overlap with your other portfolios",
    why: "this page reads one portfolio; the holdings of the others are never fetched, and an overlap computed against a list that does not include them would understate itself",
    unblockedBy: "an overlap endpoint, or the parent fetching every portfolio's holdings for this screen",
  },
];

/**
 * The Risk tab's list, and the reason the tab exists in this shape at all.
 *
 * §2.2: of the brief's eighteen risk figures, two are real. Eighteen dashes would be worse than
 * nothing, because a dash implies the number exists and is merely missing today. So the tab
 * leads with what IS known in plain language and lists these underneath with what each needs.
 * No figure below is ever computed, not even where the arithmetic is available.
 */
export const BLOCKED_RISK: readonly BlockedMetric[] = [
  {
    name: "Beta against the benchmark",
    why: "no return-series statistics are computed anywhere in Baskfy",
    // Engineering: a statistics job over `portfolio_nav_daily`, joined to an index series on the
    // same sessions. The reader needs to know it takes a return series and a matching
    // benchmark; the table it would read is the business of whoever writes the job.
    unblockedBy:
      "a statistics pass over this portfolio's daily value history, and a benchmark series on the same sessions",
  },
  {
    name: "Annualised volatility",
    why: "the daily series exists but nothing computes a standard deviation from it, and a volatility with no stated window and no stated annualisation convention is a number nobody can check",
    unblockedBy: "the same statistics job, with its window and convention written down",
  },
  {
    name: "Sharpe ratio",
    why: "it needs the volatility above and a stated risk-free rate, and Baskfy stores neither",
    unblockedBy: "the statistics job plus a risk-free series",
  },
  {
    name: "Sortino ratio and downside deviation",
    why: "same statistics job, plus a stated minimum acceptable return to measure the downside against",
    unblockedBy: "the statistics job and a stated target return",
  },
  {
    name: "Value at Risk",
    why: "a VaR is only as good as the model behind it, and there is no model here to name",
    unblockedBy: "the statistics job and a documented model: historical, parametric or Monte Carlo, with its confidence level and horizon",
  },
  {
    name: "Conditional VaR, the expected loss beyond VaR",
    why: "it is a statistic of the VaR distribution, so it cannot exist before the VaR does",
    unblockedBy: "everything Value at Risk needs, first",
  },
  {
    name: "Correlation between your holdings",
    why: "it needs a return series per instrument, aligned on dates; Baskfy stores prices, not per-instrument returns",
    unblockedBy: "a factor and returns store covering every held instrument",
  },
  {
    name: "Stress tests at minus 5, 10 and 20 percent",
    why: "a stress test is a model of how each holding responds to a market move, and that model is the beta work above",
    unblockedBy: "beta per holding, and a stated scenario definition",
  },
  {
    name: "Risk contribution by holding",
    why: "it is a covariance calculation, so it sits behind every statistic above",
    unblockedBy: "the covariance work above",
  },
  {
    name: "Days to liquidate these positions",
    why: "no traded-volume history is stored, so there is nothing to measure a position against",
    unblockedBy: "a liquidity column on the daily bars",
  },
];

/** What a Performance tab would add with data Baskfy does not keep. */
export const BLOCKED_PERFORMANCE: readonly BlockedMetric[] = [
  {
    name: "Today, and this week, as chart ranges",
    why: "the valuation series is end-of-day; there is no intraday NAV to plot",
    unblockedBy: "an intraday valuation job",
  },
  {
    name: "Performance against a benchmark you pick here",
    why: "one benchmark is stored per portfolio, and re-measuring against a different index needs that index's history joined to this portfolio's series",
    unblockedBy: "a benchmark selector on the portfolio, and a re-run of the NAV job against it",
  },
  {
    name: "Returns after brokerage and taxes",
    why: "no cost model runs over an arbitrary portfolio's trades",
    unblockedBy: "wiring the existing cost model through the portfolio path",
  },
];

/** Facts the brief asks an Overview to state that no field in this payload carries. */
export const BLOCKED_OVERVIEW: readonly BlockedMetric[] = [
  {
    name: "This portfolio's stated objective",
    why: "a portfolio has a name, a source and a benchmark, but no objective or description field",
    unblockedBy: "an objective on the portfolio record, set when it is created",
  },
  {
    name: "When it was last rebalanced",
    why: "the activity feed records buys, sells, cash moves, dividends, corporate actions and reconciliations; a rebalance is not one of its kinds, so there is no event to date",
    unblockedBy: "a rebalance event written to the activity feed when a plan is applied",
  },
  {
    name: "When it is next due for review",
    why: "no review cadence is stored against a portfolio",
    unblockedBy: "a review schedule on the portfolio record",
  },
  {
    name: "A target equity exposure for this portfolio",
    why: "the equity cap is a regime setting that applies across everything the swing strategy runs, not a target held against any one portfolio",
    unblockedBy: "a target allocation model per portfolio, which is a product decision rather than a UI one",
  },
];

/* ------------------------------------------------------------------ *
 * Holdings — the row model
 * ------------------------------------------------------------------ */

export type HoldingColumnId =
  | "security"
  | "broker"
  | "quantity"
  | "avgPrice"
  | "price"
  | "marketValue"
  | "weight"
  | "targetWeight"
  | "drift"
  | "todaysPnl"
  | "todaysPct"
  | "unrealisedPnl"
  | "unrealisedPct"
  | "contribution"
  | "heldFor"
  | "pricedOn";

export interface HoldingColumn {
  readonly id: HoldingColumnId;
  readonly label: string;
  /** One sentence on what the column is, carried to the header's title and the column picker. */
  readonly definition: string;
  readonly align: "left" | "right";
  /** False for columns a reader can switch off. The security column is always present. */
  readonly fixed: boolean;
  /** On by default. The rest are opt-in so the first view is readable rather than exhaustive. */
  readonly defaultOn: boolean;
  /** True for the two columns that exist only where a basket publishes target weights. */
  readonly basketOnly: boolean;
}

export const HOLDING_COLUMNS: readonly HoldingColumn[] = [
  {
    id: "security",
    label: "Security",
    definition: "The instrument, by symbol and name, with anything flagged against the row.",
    align: "left",
    fixed: true,
    defaultOn: true,
    basketOnly: false,
  },
  {
    id: "quantity",
    label: "Qty",
    definition: "Shares held in this portfolio at this broker. The free, T1 and pledged split is not in this payload.",
    align: "right",
    fixed: false,
    defaultOn: true,
    basketOnly: false,
  },
  {
    id: "avgPrice",
    label: "Avg price",
    definition: "What these shares cost, per share, as your broker or your imported statement reported it.",
    align: "right",
    fixed: false,
    defaultOn: true,
    basketOnly: false,
  },
  {
    id: "price",
    label: "Price",
    definition: "The latest close we have on record for this instrument.",
    align: "right",
    fixed: false,
    defaultOn: true,
    basketOnly: false,
  },
  {
    id: "marketValue",
    label: "Market value",
    definition: "Quantity at the latest close.",
    align: "right",
    fixed: false,
    defaultOn: true,
    basketOnly: false,
  },
  {
    id: "weight",
    label: "Weight",
    definition: "This position's share of what the portfolio's shares are worth. Cash is not in the denominator.",
    align: "right",
    fixed: false,
    defaultOn: true,
    basketOnly: false,
  },
  {
    id: "targetWeight",
    label: "Target",
    definition: "The share the published model intends this name to have. Only a basket-backed portfolio has one.",
    align: "right",
    fixed: false,
    defaultOn: true,
    basketOnly: true,
  },
  {
    id: "drift",
    label: "Drift",
    definition: "Weight minus target, in percentage points. Positive is overweight against the model.",
    align: "right",
    fixed: false,
    defaultOn: true,
    basketOnly: true,
  },
  {
    id: "todaysPnl",
    label: "Today",
    definition: "Quantity times the move from the previous close to the latest one.",
    align: "right",
    fixed: false,
    defaultOn: true,
    basketOnly: false,
  },
  {
    id: "todaysPct",
    label: "Today %",
    definition: "The same move as a percentage of what this position was worth at the previous close.",
    align: "right",
    fixed: false,
    defaultOn: false,
    basketOnly: false,
  },
  {
    id: "unrealisedPnl",
    label: "Unrealised",
    definition: "Market value minus what these shares cost. Nothing here has been sold, so nothing here is booked.",
    align: "right",
    fixed: false,
    defaultOn: true,
    basketOnly: false,
  },
  {
    id: "unrealisedPct",
    label: "Unrealised %",
    definition: "The same figure as a percentage of this position's own cost.",
    align: "right",
    fixed: false,
    defaultOn: false,
    basketOnly: false,
  },
  {
    id: "contribution",
    label: "Contribution",
    definition: "This position's unrealised gain as a share of what the whole portfolio cost, so the column adds to the portfolio's own return.",
    align: "right",
    fixed: false,
    defaultOn: false,
    basketOnly: false,
  },
  {
    id: "heldFor",
    label: "Held for",
    definition: "How long these shares have been held, measured from the first purchase we have a date for.",
    align: "right",
    fixed: false,
    defaultOn: false,
    basketOnly: false,
  },
  {
    id: "broker",
    label: "Broker",
    definition: "The account this line sits in. A name held at two brokers is two rows.",
    align: "left",
    fixed: false,
    defaultOn: true,
    basketOnly: false,
  },
  {
    id: "pricedOn",
    label: "Priced",
    definition: "The valuation date behind the price column. One date covers the whole portfolio, so every row shows the same one.",
    align: "left",
    fixed: false,
    defaultOn: false,
    basketOnly: false,
  },
];

export type HoldingFlagId = "no-cost-basis" | "no-price" | "reconciliation";

export interface HoldingFlag {
  readonly id: HoldingFlagId;
  /** Two or three words in the cell. */
  readonly short: string;
  /** The whole sentence, for the row's detail and for the footnote under the table. */
  readonly detail: string;
  readonly severity: "critical" | "review";
}

export interface HoldingRow {
  /** Instrument and broker together: the same name at two brokers is two rows. */
  readonly key: string;
  readonly instrumentId: number;
  readonly symbol: string;
  readonly name: string;
  readonly brokerLabel: string;
  readonly brokerAccountId: number;

  readonly quantity: Metric;
  readonly avgPrice: Metric;
  readonly price: Metric;
  readonly marketValue: Metric;
  readonly weight: Metric;
  readonly targetWeight: Metric;
  readonly drift: Metric;
  readonly todaysPnl: Metric;
  readonly todaysPct: Metric;
  readonly unrealisedPnl: Metric;
  readonly unrealisedPct: Metric;
  readonly contribution: Metric;
  readonly heldFor: Metric;
  readonly broker: Metric;
  readonly pricedOn: Metric;

  readonly flags: readonly HoldingFlag[];

  /**
   * Comparison keys for sorting and filtering. **Never rendered.**
   *
   * These are the one place in this module where a money figure becomes a `number`, and they are
   * safe precisely because nothing displays them: a float that is wrong in the sixteenth digit
   * still sorts correctly, and no rupee reaches a reader through this block. Every displayed
   * figure above is an exact decimal string.
   */
  readonly raw: {
    readonly marketValue: number | null;
    readonly weight: number | null;
    readonly drift: number | null;
    readonly todaysPnl: number | null;
    readonly unrealisedPnl: number | null;
    readonly contribution: number | null;
    readonly heldForDays: number | null;
    readonly hasCostBasis: boolean;
    readonly hasPrice: boolean;
    readonly pendingReconciliation: boolean;
  };
}

const NO_PRICE_REASON =
  "No closing price is on record for this instrument, so it cannot be valued today.";
const NO_QUANTITY_REASON = "The payload carried no quantity for this line.";
const NO_TARGET_ROW_REASON =
  "The model has not published a target weight against this holding yet.";
const NOT_BASKET_BACKED_REASON =
  "This portfolio is grouped by hand rather than tracking a published model, so there is no target weight to measure a drift against.";
const NO_FIRST_BOUGHT_REASON =
  "No purchase date is on record for these shares, so how long they have been held is unknown.";
const NO_AS_OF_REASON =
  "This portfolio has never been valued, so there is no date to measure a holding period to.";

export interface HoldingsContext {
  /** True when the source panel names a basket, so target weights apply at all. */
  readonly basketBacked: boolean;
  /** The portfolio's cost, the denominator for the contribution column. */
  readonly invested: string | null;
  readonly investedUnavailableReason: string;
  /** The valuation date the price column is measured at. */
  readonly pricesAsOf: string | null;
}

/**
 * One holding as sixteen explained figures.
 *
 * Every cell is a `Metric`, so a cell with no value carries its own sentence. The four reasons
 * `history_source` distinguishes travel on the average-price cell rather than being flattened
 * into one "unknown": "we never had it" and "your broker did not send it" have different
 * remedies, and the reader is the one who has to act on them.
 */
export function holdingRow(holding: DetailHoldingRow, context: HoldingsContext): HoldingRow {
  const value = holding.value ?? null;
  const known = hasCostBasis(holding);
  const costReason = avgPriceReason(holding);

  const todays = holding.todays_contribution ?? null;
  const unrealised = holding.total_contribution ?? null;

  /* Exact restatements, not estimates. The server builds `value` as quantity × latest and
     `todays_contribution` as quantity × (latest − previous), so their difference is exactly
     quantity × previous; it builds `total_contribution` as value − cost_basis, so the same
     subtraction recovers the cost basis to the last paisa. */
  const previousValue = subtractDecimals(value, todays);
  const costOfPosition = subtractDecimals(value, unrealised);

  const weightPct = fractionToPercent(holding.weight);
  const targetRaw = context.basketBacked ? targetWeightOf(holding) : null;
  const targetPct = fractionToPercent(targetRaw);
  const driftPct =
    weightPct === null || targetPct === null ? null : subtractDecimals(weightPct, targetPct);

  const heldDays = daysBetween(holding.first_bought_on ?? null, context.pricesAsOf);

  const flags: HoldingFlag[] = [];
  if (!known) {
    flags.push({
      id: "no-cost-basis",
      short: "No cost basis",
      detail: costReason,
      severity: "review",
    });
  }
  if (holding.price === null || holding.price === undefined || holding.price === "") {
    flags.push({
      id: "no-price",
      short: "Not priced",
      detail: NO_PRICE_REASON,
      severity: "review",
    });
  }
  if (holding.pending_reconciliation) {
    flags.push({
      id: "reconciliation",
      short: "Not reconciled",
      detail:
        "Something changed at your broker that we could not attribute on our own. Until you answer it, this holding is held out of the portfolio's return series rather than guessed at.",
      severity: "critical",
    });
  }

  return {
    key: `${holding.instrument.instrument_id}-${holding.broker.broker_account_id}`,
    instrumentId: holding.instrument.instrument_id,
    symbol: holding.instrument.symbol,
    name: holding.instrument.name,
    brokerLabel: holding.broker.label,
    brokerAccountId: holding.broker.broker_account_id,

    quantity: metric("Qty", columnDefinition("quantity"), holding.quantity, NO_QUANTITY_REASON),
    avgPrice: metric("Avg price", columnDefinition("avgPrice"), known ? holding.avg_price : null, costReason),
    price: metric("Price", columnDefinition("price"), holding.price, NO_PRICE_REASON),
    marketValue: metric("Market value", columnDefinition("marketValue"), value, NO_PRICE_REASON),
    weight: metric(
      "Weight",
      columnDefinition("weight"),
      weightPct,
      "This holding could not be valued, so it has no share of the portfolio to state.",
    ),
    targetWeight: metric(
      "Target",
      columnDefinition("targetWeight"),
      targetPct,
      context.basketBacked ? NO_TARGET_ROW_REASON : NOT_BASKET_BACKED_REASON,
    ),
    drift: metric(
      "Drift",
      columnDefinition("drift"),
      driftPct,
      context.basketBacked ? NO_TARGET_ROW_REASON : NOT_BASKET_BACKED_REASON,
    ),
    todaysPnl: metric(
      "Today",
      columnDefinition("todaysPnl"),
      todays,
      "Today's move needs both the latest close and the one before it, and one of them is missing for this instrument.",
    ),
    todaysPct: metric(
      "Today %",
      columnDefinition("todaysPct"),
      percentOf(todays, previousValue),
      "Today's move as a percentage needs what this position was worth at the previous close, which is not available for this instrument.",
    ),
    unrealisedPnl: metric("Unrealised", columnDefinition("unrealisedPnl"), unrealised, costReason),
    unrealisedPct: metric(
      "Unrealised %",
      columnDefinition("unrealisedPct"),
      percentOf(unrealised, costOfPosition),
      costReason,
    ),
    contribution: metric(
      "Contribution",
      columnDefinition("contribution"),
      percentOf(unrealised, context.invested),
      known ? context.investedUnavailableReason : costReason,
    ),
    heldFor: metric(
      "Held for",
      columnDefinition("heldFor"),
      heldDays === null ? null : describeSpan(heldDays),
      context.pricesAsOf === null ? NO_AS_OF_REASON : NO_FIRST_BOUGHT_REASON,
      /* The date it is measured from travels with the span, so the column answers both "how
         long" and "since when" without needing a second column for the second question. */
      { since: holding.first_bought_on ?? null },
    ),
    broker: metric(
      "Broker",
      columnDefinition("broker"),
      holding.broker.label,
      "This line is not attached to a broker account.",
    ),
    pricedOn: metric(
      "Priced",
      columnDefinition("pricedOn"),
      context.pricesAsOf,
      "No end-of-day valuation has been recorded for this portfolio yet.",
    ),

    flags,
    raw: {
      marketValue: toNumber(value),
      weight: toNumber(weightPct),
      drift: toNumber(driftPct),
      todaysPnl: toNumber(todays),
      unrealisedPnl: toNumber(unrealised),
      contribution: toNumber(percentOf(unrealised, context.invested)),
      heldForDays: heldDays,
      hasCostBasis: known,
      hasPrice: holding.price !== null && holding.price !== undefined && holding.price !== "",
      pendingReconciliation: holding.pending_reconciliation,
    },
  };
}

function columnDefinition(id: HoldingColumnId): string {
  return HOLDING_COLUMNS.find((column) => column.id === id)?.definition ?? "";
}

export function holdingsContext(detail: PortfolioDetail): HoldingsContext {
  const summary = detail.summary;
  return {
    basketBacked: isBasketBacked(detail.source_panel),
    invested: summary.invested ?? null,
    investedUnavailableReason:
      summary.invested_unavailable_reason ??
      "What this portfolio cost is not known, so a share of it cannot be stated.",
    pricesAsOf: summary.prices_as_of ?? null,
  };
}

export function holdingRows(detail: PortfolioDetail): readonly HoldingRow[] {
  const context = holdingsContext(detail);
  return (detail.holdings ?? []).map((holding) => holdingRow(holding, context));
}

/* ------------------------------------------------------------------ *
 * Holdings — sorting
 * ------------------------------------------------------------------ */

export type SortDirection = 1 | -1;

/**
 * Sort, with rows that have no figure always last.
 *
 * A row with nothing in the column being sorted has no place in an ordering by that column, and
 * putting it at one end or the other by treating its absence as a very large or very small number
 * would read as a ranking. So absence sinks, in both directions, and the reader can see that the
 * bottom of the list is the part the data does not cover.
 */
export function sortHoldings(
  rows: readonly HoldingRow[],
  column: HoldingColumnId,
  direction: SortDirection,
): readonly HoldingRow[] {
  const text = (row: HoldingRow): string | null => {
    if (column === "security") return row.symbol;
    if (column === "broker") return row.brokerLabel;
    if (column === "avgPrice") return row.avgPrice.value;
    if (column === "price") return row.price.value;
    if (column === "quantity") return row.quantity.value;
    if (column === "targetWeight") return row.targetWeight.value;
    if (column === "todaysPct") return row.todaysPct.value;
    if (column === "unrealisedPct") return row.unrealisedPct.value;
    if (column === "pricedOn") return row.pricedOn.value;
    return null;
  };
  const numeric = (row: HoldingRow): number | null => {
    switch (column) {
      case "marketValue":
        return row.raw.marketValue;
      case "weight":
        return row.raw.weight;
      case "drift":
        return row.raw.drift;
      case "todaysPnl":
        return row.raw.todaysPnl;
      case "unrealisedPnl":
        return row.raw.unrealisedPnl;
      case "contribution":
        return row.raw.contribution;
      case "heldFor":
        return row.raw.heldForDays;
      case "quantity":
      case "avgPrice":
      case "price":
      case "targetWeight":
      case "todaysPct":
      case "unrealisedPct":
        return toNumber(text(row));
      default:
        return null;
    }
  };

  const alphabetical = column === "security" || column === "broker" || column === "pricedOn";

  return [...rows].sort((left, right) => {
    if (alphabetical) {
      const a = text(left) ?? "";
      const b = text(right) ?? "";
      return direction * a.localeCompare(b);
    }
    const a = numeric(left);
    const b = numeric(right);
    if (a === null && b === null) return left.symbol.localeCompare(right.symbol);
    if (a === null) return 1;
    if (b === null) return -1;
    if (a === b) return left.symbol.localeCompare(right.symbol);
    return direction * (a - b);
  });
}

/* ------------------------------------------------------------------ *
 * Holdings — quick filters
 * ------------------------------------------------------------------ */

export type HoldingFilterId =
  | "all"
  | "winners"
  | "losers"
  | "up-today"
  | "down-today"
  | "above-target"
  | "below-target"
  | "no-cost-basis"
  | "no-price"
  | "reconciliation";

export interface HoldingFilter {
  readonly id: HoldingFilterId;
  readonly label: string;
  /** What the filter selects, in one sentence. */
  readonly definition: string;
  readonly matches: (row: HoldingRow) => boolean;
}

/**
 * The brief's quick filters, restricted to the ones with real data behind them.
 *
 * A filter is offered only where the payload can answer it. The two target filters are the clear
 * case: on a hand-grouped portfolio there is no target weight at all, so "above target" is not a
 * filter that returns nothing, it is a question the product cannot ask. {@link availableFilters}
 * returns it with the reason attached so the control renders disabled and explains itself rather
 * than disappearing.
 */
export const HOLDING_FILTERS: readonly HoldingFilter[] = [
  {
    id: "all",
    label: "All",
    definition: "Every holding in this portfolio.",
    matches: () => true,
  },
  {
    id: "winners",
    label: "Winners",
    definition: "Positions worth more than they cost.",
    matches: (row) => row.raw.unrealisedPnl !== null && row.raw.unrealisedPnl > 0,
  },
  {
    id: "losers",
    label: "Losers",
    definition: "Positions worth less than they cost.",
    matches: (row) => row.raw.unrealisedPnl !== null && row.raw.unrealisedPnl < 0,
  },
  {
    id: "up-today",
    label: "Up today",
    definition: "Positions that gained since the previous close.",
    matches: (row) => row.raw.todaysPnl !== null && row.raw.todaysPnl > 0,
  },
  {
    id: "down-today",
    label: "Down today",
    definition: "Positions that lost since the previous close.",
    matches: (row) => row.raw.todaysPnl !== null && row.raw.todaysPnl < 0,
  },
  {
    id: "above-target",
    label: "Above target",
    definition: "Positions holding a larger share than the model intends.",
    matches: (row) => row.raw.drift !== null && row.raw.drift > 0,
  },
  {
    id: "below-target",
    label: "Below target",
    definition: "Positions holding a smaller share than the model intends.",
    matches: (row) => row.raw.drift !== null && row.raw.drift < 0,
  },
  {
    id: "no-cost-basis",
    label: "Missing cost basis",
    definition: "Positions with no purchase price on record, whose profit since purchase cannot be shown.",
    matches: (row) => !row.raw.hasCostBasis,
  },
  {
    id: "no-price",
    label: "Not priced",
    definition: "Positions with no closing price on record, which enter no total on this page.",
    matches: (row) => !row.raw.hasPrice,
  },
  {
    id: "reconciliation",
    label: "Needs reconciliation",
    definition: "Positions frozen out of the return series until a broker difference is answered.",
    matches: (row) => row.raw.pendingReconciliation,
  },
];

export interface OfferedFilter {
  readonly filter: HoldingFilter;
  /** How many rows it would select. */
  readonly count: number;
  /** Non-null when the filter cannot be asked of this portfolio at all. */
  readonly unavailable: string | null;
}

/**
 * Every filter, with its count, and the reason where the question does not apply.
 *
 * A filter that matches nothing today is still a legitimate question, so it is offered with a
 * count of zero. A filter whose underlying column does not exist for this portfolio is a
 * different thing entirely and says so.
 */
export function availableFilters(
  rows: readonly HoldingRow[],
  context: Pick<HoldingsContext, "basketBacked">,
): readonly OfferedFilter[] {
  const anyTarget = rows.some((row) => row.targetWeight.value !== null);
  return HOLDING_FILTERS.map((filter) => {
    const targeted = filter.id === "above-target" || filter.id === "below-target";
    const unavailable = !targeted
      ? null
      : !context.basketBacked
        ? NOT_BASKET_BACKED_REASON
        : anyTarget
          ? null
          : "This portfolio tracks a model, but no target weights have been published against it yet, so there is nothing to be above or below.";
    return {
      filter,
      count: unavailable === null ? rows.filter((row) => filter.matches(row)).length : 0,
      unavailable,
    };
  });
}

export function applyFilter(
  rows: readonly HoldingRow[],
  id: HoldingFilterId,
): readonly HoldingRow[] {
  const filter = HOLDING_FILTERS.find((candidate) => candidate.id === id);
  return filter === undefined ? rows : rows.filter((row) => filter.matches(row));
}

/* ------------------------------------------------------------------ *
 * Holdings — what a selection adds up to
 * ------------------------------------------------------------------ */

export interface SelectionSummary {
  readonly count: number;
  readonly marketValue: Metric;
  readonly weight: Metric;
  readonly todaysPnl: Metric;
  readonly unrealisedPnl: Metric;
  /** Rows in the selection that could not be valued, and are therefore in none of the sums. */
  readonly excluded: number;
}

const PARTIAL_SELECTION =
  "None of the selected rows carries this figure, so there is nothing to add up.";

/**
 * What the selected rows come to, added exactly, with the unpriced ones named rather than zeroed.
 *
 * "Absent is not zero" is load-bearing here: treating an unvalued holding as ₹0 would make a
 * selection of five look like a selection of four that happens to add up, which is the failure
 * that looks most like success.
 */
export function summariseSelection(rows: readonly HoldingRow[]): SelectionSummary {
  const values = rows.map((row) => row.marketValue.value);
  const priced = values.filter((value): value is string => value !== null);
  const weights = rows.map((row) => row.weight.value).filter((v): v is string => v !== null);
  const todays = rows.map((row) => row.todaysPnl.value).filter((v): v is string => v !== null);
  const unrealised = rows
    .map((row) => row.unrealisedPnl.value)
    .filter((v): v is string => v !== null);

  return {
    count: rows.length,
    marketValue: metric(
      "Selected value",
      "The selected positions at the latest close, added exactly. Positions with no price are left out and counted separately.",
      priced.length === 0 ? null : addDecimalStrings(priced),
      PARTIAL_SELECTION,
    ),
    weight: metric(
      "Share of the portfolio",
      "What the selected positions come to as a share of the portfolio's shares.",
      weights.length === 0 ? null : addDecimalStrings(weights),
      PARTIAL_SELECTION,
    ),
    todaysPnl: metric(
      "Selected move today",
      "The selected positions' change since the previous close, added exactly.",
      todays.length === 0 ? null : addDecimalStrings(todays),
      PARTIAL_SELECTION,
    ),
    unrealisedPnl: metric(
      "Selected unrealised",
      "The selected positions' market value minus their cost, added exactly.",
      unrealised.length === 0 ? null : addDecimalStrings(unrealised),
      PARTIAL_SELECTION,
    ),
    excluded: values.length - priced.length,
  };
}

/* ------------------------------------------------------------------ *
 * Allocation
 * ------------------------------------------------------------------ */

export interface AllocationBucket {
  readonly key: string;
  readonly label: string;
  /**
   * Where this slice leads, when it leads anywhere.
   *
   * Brief: *"Drill down from every chart into the relevant holdings."* A security slice answers
   * "how much of this portfolio is RELIANCE"; the question it raises is "what IS RELIANCE doing",
   * and that is the instrument's own page. A broker or cash slice has no such destination and
   * carries no link rather than a dead one.
   */
  readonly href?: string;
  readonly secondary: string | null;
  readonly value: Metric;
  readonly weight: Metric;
  readonly count: number;
}

export interface AllocationView {
  readonly bySecurity: readonly AllocationBucket[];
  readonly byBroker: readonly AllocationBucket[];
  readonly holdingsValue: Metric;
  readonly cash: Metric;
  readonly cashShare: Metric;
  readonly deployedShare: Metric;
  readonly top1: Metric;
  readonly top3: Metric;
  readonly top5: Metric;
  readonly top10: Metric;
  readonly herfindahl: Metric;
  /** The concentration score in words, so the number is never the only signal. */
  readonly herfindahlVerdict: string;
  readonly effectiveHoldings: Metric;
  readonly pricedCount: number;
  readonly unpricedCount: number;
  readonly blocked: readonly BlockedMetric[];
}

const NOTHING_PRICED =
  "Nothing in this portfolio could be valued, so there is no total to take a share of.";

/** Herfindahl thresholds, on the 0 to 10,000 scale competition authorities use. */
const HHI_CONCENTRATED = 2500;
const HHI_MODERATE = 1500;

function hhiVerdict(score: string | null, count: number): string {
  if (score === null) {
    return "A concentration score needs at least one valued holding.";
  }
  const value = Number(score);
  if (!Number.isFinite(value)) {
    return "The concentration score could not be read.";
  }
  const evenly = count === 0 ? "" : ` ${count} equally weighted holdings would score about ${Math.round(10_000 / count)}.`;
  if (value >= HHI_CONCENTRATED) {
    return `Concentrated: a score at or above ${HHI_CONCENTRATED} means a few names carry most of this portfolio.${evenly}`;
  }
  if (value >= HHI_MODERATE) {
    return `Moderately concentrated: between ${HHI_MODERATE} and ${HHI_CONCENTRATED}.${evenly}`;
  }
  return `Spread: below ${HHI_MODERATE}, no single name dominates.${evenly}`;
}

function topShare(sortedPercents: readonly string[], n: number): string | null {
  const slice = sortedPercents.slice(0, n);
  return slice.length === 0 ? null : addDecimalStrings(slice);
}

/**
 * Allocation by the three cuts that are real, plus concentration.
 *
 * Weights are shares of what the **shares** are worth, with cash reported beside them rather than
 * folded into the denominator: idle rupees shrinking every position's weight is a change in the
 * arithmetic that has nothing to do with the positions. Cash gets its own figure, which is the
 * question "how much of this is not invested" answered directly.
 */
export function allocationView(detail: PortfolioDetail): AllocationView {
  const rows = holdingRows(detail);
  const priced = rows.filter((row) => row.marketValue.value !== null);
  const total =
    priced.length === 0 ? null : addDecimalStrings(priced.map((row) => row.marketValue.value));

  const bySecurity: AllocationBucket[] = rows
    .map((row) => ({
      key: row.key,
      label: row.symbol,
      href: `/instruments/${encodeURIComponent(row.symbol)}`,
      secondary: row.name,
      value: row.marketValue,
      weight: metric(
        "Weight",
        "This position's share of what the portfolio's shares are worth.",
        percentOf(row.marketValue.value, total),
        row.marketValue.value === null ? NO_PRICE_REASON : NOTHING_PRICED,
      ),
      count: 1,
    }))
    .sort((left, right) => {
      const a = left.value.value;
      const b = right.value.value;
      if (a === null && b === null) return left.label.localeCompare(right.label);
      if (a === null) return 1;
      if (b === null) return -1;
      return compareDecimalStrings(b, a);
    });

  const brokerTotals = new Map<number, { label: string; values: string[]; count: number }>();
  for (const row of rows) {
    const bucket = brokerTotals.get(row.brokerAccountId) ?? {
      label: row.brokerLabel,
      values: [],
      count: 0,
    };
    bucket.count += 1;
    if (row.marketValue.value !== null) bucket.values.push(row.marketValue.value);
    brokerTotals.set(row.brokerAccountId, bucket);
  }

  const byBroker: AllocationBucket[] = [...brokerTotals.entries()]
    .map(([accountId, bucket]) => {
      const value = bucket.values.length === 0 ? null : addDecimalStrings(bucket.values);
      return {
        key: String(accountId),
        label: bucket.label,
        secondary: `${bucket.count} holding${bucket.count === 1 ? "" : "s"}`,
        value: metric(
          "Value at this broker",
          "Every holding in this portfolio that sits in this broker account, added exactly.",
          value,
          "None of this account's holdings in this portfolio could be valued.",
        ),
        weight: metric(
          "Share",
          "This broker account's share of what the portfolio's shares are worth.",
          percentOf(value, total),
          NOTHING_PRICED,
        ),
        count: bucket.count,
      };
    })
    .sort((left, right) => {
      const a = left.value.value;
      const b = right.value.value;
      if (a === null && b === null) return left.label.localeCompare(right.label);
      if (a === null) return 1;
      if (b === null) return -1;
      return compareDecimalStrings(b, a);
    });

  /* The percentages are already rounded to two places, which is the contract the column adds up
     under; squaring them there rather than squaring the unrounded ratio keeps the score and the
     column telling the same story. */
  const percents = bySecurity
    .map((bucket) => bucket.weight.value)
    .filter((value): value is string => value !== null);
  const squares = percents.map((pct) => multiplyDecimals(pct, pct));
  const hhi = squares.length === 0 ? null : addDecimalStrings(squares);
  const hhiRounded = hhi === null ? null : roundDecimalString(hhi, 0);

  const cash = detail.summary.cash;
  const investedAndCash = addDecimalStrings([total, cash]);

  const effective =
    hhi === null || Number(hhi) <= 0 ? null : (10_000 / Number(hhi)).toFixed(1);

  return {
    bySecurity,
    byBroker,
    holdingsValue: metric(
      "Value of the shares",
      "Every priced holding in this portfolio, added exactly. Cash is not in it.",
      total,
      NOTHING_PRICED,
    ),
    cash: metric(
      "Cash in this portfolio",
      "Money assigned to this portfolio that is not in a share.",
      cash,
      "No cash balance is recorded against this portfolio.",
    ),
    cashShare: metric(
      "Cash share",
      "Cash as a share of the shares plus the cash.",
      percentOf(cash, investedAndCash),
      "Neither the shares nor the cash could be valued, so there is no split to state.",
    ),
    deployedShare: metric(
      "Deployed",
      "The shares as a share of the shares plus the cash. This portfolio has no stored target exposure to compare it against.",
      percentOf(total, investedAndCash),
      NOTHING_PRICED,
    ),
    top1: metric(
      "Largest holding",
      "The biggest single position's share of the shares.",
      topShare(percents, 1),
      NOTHING_PRICED,
    ),
    top3: metric("Top 3", "The three largest positions together.", topShare(percents, 3), NOTHING_PRICED),
    top5: metric("Top 5", "The five largest positions together.", topShare(percents, 5), NOTHING_PRICED),
    top10: metric("Top 10", "The ten largest positions together.", topShare(percents, 10), NOTHING_PRICED),
    herfindahl: metric(
      "Concentration score",
      "The Herfindahl index: every holding's percentage weight squared, added up. 10,000 is one holding; twenty equally weighted names score 500.",
      hhiRounded,
      NOTHING_PRICED,
    ),
    herfindahlVerdict: hhiVerdict(hhiRounded, percents.length),
    effectiveHoldings: metric(
      "Equivalent equal holdings",
      "How many equally weighted names would give the same concentration score. Thirty holdings where one name is a third of the value behave like a far smaller portfolio.",
      effective,
      NOTHING_PRICED,
    ),
    pricedCount: priced.length,
    unpricedCount: rows.length - priced.length,
    blocked: BLOCKED_ALLOCATION,
  };
}

/* ------------------------------------------------------------------ *
 * Risk — what IS known, in plain language
 * ------------------------------------------------------------------ */

export interface RiskReading {
  readonly id: string;
  readonly figure: Metric;
  /** The sentence that comes before the number, in the reader's own terms. */
  readonly plain: string;
}

export interface RiskView {
  readonly known: readonly RiskReading[];
  readonly notMeasured: readonly BlockedMetric[];
  /** The sentence that heads the tab, so a reader knows what they are looking at. */
  readonly opening: string;
}

const RISK_OPENING =
  "Baskfy measures three kinds of risk today and states them plainly. Everything the brief asks for beyond these needs a statistics job that has not been built, and none of it is estimated here: a risk number with no model behind it is worse than an absent one, because somebody may size a position against it.";

/**
 * The Risk tab, which is mostly a list of what is not measured, and says so.
 *
 * §2.2's design consequence, verbatim: *"of its eighteen requested figures, two exist. It is
 * therefore scoped as one honest panel ... rather than eighteen dashes."* This function returns
 * the honest panel. Note what it does NOT do: `nav.daily_pnl[].pct` is right there and a standard
 * deviation over it is trivial, and it is still not computed, because the window and the
 * annualisation convention would be invented here rather than stated anywhere.
 */
export function riskView(detail: PortfolioDetail, nav: NavSeries | null): RiskView {
  const allocation = allocationView(detail);
  const rows = holdingRows(detail);
  const summary = detail.summary;

  const drawdownPoints = inDateOrder(nav?.drawdown ?? []);
  const current = drawdownPoints[drawdownPoints.length - 1] ?? null;
  const maxDrawdown = nav?.max_drawdown ?? null;

  const total = allocation.holdingsValue.value;
  const frozen = rows.filter((row) => row.raw.pendingReconciliation);
  const frozenValues = frozen
    .map((row) => row.marketValue.value)
    .filter((value): value is string => value !== null);
  const frozenValue = frozenValues.length === 0 ? null : addDecimalStrings(frozenValues);

  const blind = rows.filter((row) => !row.raw.hasCostBasis);
  const blindValues = blind
    .map((row) => row.marketValue.value)
    .filter((value): value is string => value !== null);
  const blindValue = blindValues.length === 0 ? null : addDecimalStrings(blindValues);

  const known: RiskReading[] = [
    {
      id: "max-drawdown",
      plain:
        "The deepest fall from a high this portfolio has been through, measured on its own value series. It is what has happened, not what could.",
      figure: metric(
        "Deepest fall from a high",
        "The largest peak to trough fall in the end-of-day valuation series, with the dates it ran between.",
        fractionToPercent(maxDrawdown?.drawdown ?? null),
        "No end-of-day valuation history has been recorded for this portfolio, so there is no peak to measure a fall from.",
        maxDrawdown === null
          ? {}
          : { since: `${maxDrawdown.peak_on} to ${maxDrawdown.trough_on}` },
      ),
    },
    {
      id: "current-drawdown",
      plain: "Where it stands against its own high water mark right now. Zero means it is at a high.",
      figure: metric(
        "Below its high today",
        "The latest point on the drawdown series: how far under its own peak the portfolio sits.",
        fractionToPercent(current?.drawdown ?? null),
        "No end-of-day valuation history has been recorded, so there is no high water mark to stand against.",
        current === null ? {} : { since: current.on },
      ),
    },
    {
      id: "largest-holding",
      plain:
        "How much of this portfolio rides on its single biggest name. One name going wrong costs roughly this much of the portfolio.",
      figure: allocation.top1,
    },
    {
      id: "top-three",
      plain: "The same question for the three biggest together.",
      figure: allocation.top3,
    },
    {
      id: "concentration",
      plain: allocation.herfindahlVerdict,
      figure: allocation.herfindahl,
    },
    {
      id: "reconciliation-exposure",
      plain:
        frozen.length === 0
          ? "Nothing in this portfolio is waiting on a reconciliation answer, so every holding is inside the return series."
          : `${frozen.length} holding${frozen.length === 1 ? " is" : "s are"} frozen out of the return series until a broker difference is answered. The returns on this page are measured over less than everything you hold.`,
      figure: metric(
        "Value waiting on reconciliation",
        "What the frozen holdings are worth, as a share of the portfolio's shares.",
        percentOf(frozenValue, total),
        frozen.length === 0
          ? "Nothing is waiting on a reconciliation answer."
          : "The frozen holdings could not be valued, so their share cannot be stated.",
      ),
    },
    {
      id: "cost-basis-exposure",
      plain:
        blind.length === 0
          ? "Every holding here has a purchase price on record, so the profit since purchase covers the whole portfolio."
          : `${blind.length} holding${blind.length === 1 ? " has" : "s have"} no purchase price on record. Their profit since purchase is unknown, so the portfolio's own is understated by whatever they have done.`,
      figure: metric(
        "Value with no cost basis",
        "What the holdings with no purchase price are worth, as a share of the portfolio's shares.",
        percentOf(blindValue, total),
        blind.length === 0
          ? "Every holding has a purchase price on record."
          : "The holdings with no cost basis could not be valued either, so their share cannot be stated.",
      ),
    },
    {
      id: "unpriced-exposure",
      plain:
        allocation.unpricedCount === 0
          ? "Every holding here has a closing price on record, so the value on this page covers the whole portfolio."
          : `${allocation.unpricedCount} holding${allocation.unpricedCount === 1 ? " has" : "s have"} no closing price, so ${allocation.unpricedCount === 1 ? "it is" : "they are"} in none of the totals on this page.`,
      figure: metric(
        "Holdings with no price",
        "How many holdings enter no total on this page.",
        String(allocation.unpricedCount),
        "The holdings could not be counted.",
      ),
    },
  ];

  if (summary.benchmark === null || summary.benchmark === undefined) {
    known.push({
      id: "no-benchmark",
      plain:
        "No benchmark is set on this portfolio, so nothing here can say whether its falls were the market's or its own.",
      figure: metric(
        "Benchmark",
        "The index this portfolio is measured against.",
        null,
        "No benchmark has been chosen for this portfolio.",
      ),
    });
  }

  return { known, notMeasured: BLOCKED_RISK, opening: RISK_OPENING };
}

/* ------------------------------------------------------------------ *
 * Performance
 * ------------------------------------------------------------------ */

/** A month of the wealth index, from its last session to the previous month's last session. */
export interface MonthlyReturn {
  /** "2026-08". */
  readonly key: string;
  readonly year: number;
  /** 1 to 12. */
  readonly month: number;
  readonly label: string;
  readonly figure: Metric;
  /** Sorting and heatmap shading only. Never rendered. */
  readonly raw: number | null;
}

const MONTH_NAMES = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
] as const;

function monthName(month: number): string {
  return MONTH_NAMES[month - 1] ?? String(month);
}

const NO_INDEX_SERIES =
  "The end-of-day valuation series has not been built for this portfolio, so there is no return series to cut up.";

/**
 * Month-end to month-end returns, read off the wealth index rather than the value line.
 *
 * The index is flow-adjusted; the value line is not. A month in which ₹50,000 was assigned would
 * show as a large gain on the value line and as whatever the market did on the index, and only
 * the second is a return. `CombinedChart` makes the same choice for the same reason.
 *
 * The first month in the window is deliberately absent rather than measured from its own first
 * session: a part-month is not the month, and printing one in a grid of whole months invites the
 * comparison the grid exists to make.
 */
export function monthlyReturns(nav: NavSeries | null): readonly MonthlyReturn[] {
  const points = inDateOrder(nav?.drawdown ?? []);
  if (points.length < 2) return [];

  const lastOfMonth = new Map<string, DrawdownPoint>();
  for (const point of points) {
    const key = point.on.slice(0, 7);
    lastOfMonth.set(key, point);
  }

  const keys = [...lastOfMonth.keys()].sort();
  const out: MonthlyReturn[] = [];
  for (let i = 1; i < keys.length; i += 1) {
    const key = keys[i];
    const previousKey = keys[i - 1];
    if (key === undefined || previousKey === undefined) continue;
    const end = lastOfMonth.get(key);
    const start = lastOfMonth.get(previousKey);
    if (end === undefined || start === undefined) continue;

    const growth = percentOf(subtractDecimals(end.index, start.index), start.index);
    const [yearText = "0", monthText = "1"] = key.split("-");
    const year = Number(yearText);
    const month = Number(monthText);
    out.push({
      key,
      year,
      month,
      label: `${monthName(month)} ${year}`,
      figure: metric(
        `${monthName(month)} ${year}`,
        "The wealth index at this month's last session against the previous month's last session, so deposits and withdrawals are not in it.",
        growth,
        "This month has no closing index value to measure from.",
      ),
      raw: toNumber(growth),
    });
  }
  return out;
}

export interface CalendarYear {
  readonly year: number;
  readonly figure: Metric;
  readonly months: readonly MonthlyReturn[];
  /** True when the window does not cover the whole year, so the figure is a part-year. */
  readonly partial: boolean;
}

/**
 * Calendar returns, compounded from the months rather than taken from the year's endpoints.
 *
 * They are the same number when every month is present and a different one when the window
 * starts mid-year, and compounding the months we actually have is the version whose caveat is
 * legible: the year is marked partial and says which months it covers.
 */
export function calendarYears(nav: NavSeries | null): readonly CalendarYear[] {
  const months = monthlyReturns(nav);
  const byYear = new Map<number, MonthlyReturn[]>();
  for (const month of months) {
    const list = byYear.get(month.year) ?? [];
    list.push(month);
    byYear.set(month.year, list);
  }

  return [...byYear.entries()]
    .sort(([left], [right]) => right - left)
    .map(([year, list]) => {
      const usable = list.filter((month) => month.raw !== null);
      /* Compounding is a product of ratios, which has no exact decimal form once there are more
         than a handful of them; the inputs are already rounded percentages, so this is a
         presentation figure to two places and is labelled as compounded from the months shown. */
      const compounded =
        usable.length === 0
          ? null
          : (
              (usable.reduce((product, month) => product * (1 + (month.raw ?? 0) / 100), 1) - 1) *
              100
            ).toFixed(2);
      return {
        year,
        partial: list.length < 12,
        months: list.sort((left, right) => left.month - right.month),
        figure: metric(
          String(year),
          "The months shown, compounded. A year the window does not fully cover is marked as partial and covers only those months.",
          compounded,
          "No month in this year has a closing index value to measure from.",
        ),
      };
    });
}

export interface RollingWindow {
  readonly id: string;
  readonly label: string;
  /** Sessions, not calendar days: the index has a point per trading session. */
  readonly sessions: number;
  readonly latest: Metric;
  readonly best: Metric;
  readonly worst: Metric;
  /** How many windows of this length the series actually contains. */
  readonly observations: number;
}

const ROLLING_WINDOWS: readonly { id: string; label: string; sessions: number }[] = [
  { id: "1m", label: "About a month", sessions: 21 },
  { id: "3m", label: "About a quarter", sessions: 63 },
  { id: "6m", label: "About six months", sessions: 126 },
  { id: "12m", label: "About a year", sessions: 252 },
];

/**
 * Rolling returns over the wealth index.
 *
 * A window longer than the series is reported as having no observations, with the count of
 * sessions we do have, rather than being computed from whatever is there. "Your one-year rolling
 * return" measured over four months is not a one-year return, and a reader has no way to tell
 * from the number alone.
 */
export function rollingReturns(nav: NavSeries | null): readonly RollingWindow[] {
  const points = inDateOrder(nav?.drawdown ?? []);
  return ROLLING_WINDOWS.map((window) => {
    const returns: { on: string; pct: string }[] = [];
    for (let i = window.sessions; i < points.length; i += 1) {
      const end = points[i];
      const start = points[i - window.sessions];
      if (end === undefined || start === undefined) continue;
      const growth = percentOf(subtractDecimals(end.index, start.index), start.index);
      if (growth !== null) returns.push({ on: end.on, pct: growth });
    }

    const shortfall =
      points.length === 0
        ? NO_INDEX_SERIES
        : `The series holds ${points.length} session${points.length === 1 ? "" : "s"}, and this window needs ${window.sessions + 1}.`;

    const sorted = [...returns].sort((left, right) => compareDecimalStrings(left.pct, right.pct));
    const best = sorted[sorted.length - 1] ?? null;
    const worst = sorted[0] ?? null;
    const latest = returns[returns.length - 1] ?? null;

    return {
      id: window.id,
      label: window.label,
      sessions: window.sessions,
      observations: returns.length,
      latest: metric(
        `${window.label}, latest`,
        `The wealth index over the most recent ${window.sessions} sessions.`,
        latest?.pct ?? null,
        shortfall,
        latest === null ? {} : { since: latest.on },
      ),
      best: metric(
        `${window.label}, best`,
        `The best any ${window.sessions} session stretch in this window has done.`,
        best?.pct ?? null,
        shortfall,
        best === null ? {} : { since: best.on },
      ),
      worst: metric(
        `${window.label}, worst`,
        `The worst any ${window.sessions} session stretch in this window has done.`,
        worst?.pct ?? null,
        shortfall,
        worst === null ? {} : { since: worst.on },
      ),
    };
  });
}

export interface DrawdownEpisode {
  readonly key: string;
  /**
   * The last session at the old high, which is the session BEFORE the fall began.
   *
   * `null` when the window opens already below a high: the peak is then outside the range we
   * were given, and dating it to the first session in the window would invent a high that
   * session did not set. The tab says so rather than printing a date.
   */
  readonly peakOn: string | null;
  readonly troughOn: string;
  /** The session the index regained its old peak, or `null` while it has not. */
  readonly recoveredOn: string | null;
  readonly depth: Metric;
  /** Days from the high to the bottom, or `null` when the high is outside the window. */
  readonly toTrough: number | null;
  readonly toRecovery: number | null;
  readonly ongoing: boolean;
}

/**
 * Every peak to trough episode in the window, with whether it recovered and how long it took.
 *
 * The peak is the last session AT the high, not the first session below it. Those are one session
 * apart and the difference is the whole meaning of "how long was it down": dating the fall from
 * its first down day understates every episode by a session and disagrees with the server's own
 * `max_drawdown.peak_on`, which the test pins the deepest episode against.
 *
 * An episode that has not recovered is reported as ongoing rather than closed at the last session.
 * Closing it would date a recovery that has not happened.
 */
export function drawdownEpisodes(nav: NavSeries | null): readonly DrawdownEpisode[] {
  const points = inDateOrder(nav?.drawdown ?? []);
  const episodes: DrawdownEpisode[] = [];

  let lastClear: string | null = null;
  let peakOn: string | null = null;
  let troughOn: string | null = null;
  let deepest: string | null = null;
  let open = false;

  for (const point of points) {
    const inDrawdown = parseDecimal(point.drawdown)?.units !== 0n;
    if (inDrawdown) {
      if (!open) {
        open = true;
        peakOn = lastClear;
        troughOn = point.on;
        deepest = point.drawdown;
      } else if (deepest !== null && compareDecimalStrings(point.drawdown, deepest) < 0) {
        deepest = point.drawdown;
        troughOn = point.on;
      }
      continue;
    }
    if (open && troughOn !== null && deepest !== null) {
      episodes.push(episode(peakOn, troughOn, point.on, deepest));
    }
    lastClear = point.on;
    open = false;
    peakOn = null;
    troughOn = null;
    deepest = null;
  }

  if (open && troughOn !== null && deepest !== null) {
    episodes.push(episode(peakOn, troughOn, null, deepest));
  }

  return episodes.sort((left, right) => {
    const a = toNumber(left.depth.value) ?? 0;
    const b = toNumber(right.depth.value) ?? 0;
    return a - b;
  });
}

function episode(
  peakOn: string | null,
  troughOn: string,
  recoveredOn: string | null,
  depth: string,
): DrawdownEpisode {
  return {
    key: `${peakOn ?? "before-the-window"}-${troughOn}`,
    peakOn,
    troughOn,
    recoveredOn,
    depth: metric(
      "Depth",
      "How far under the previous high the index fell before it turned.",
      fractionToPercent(depth),
      "The depth of this fall could not be read from the series.",
      { since: peakOn === null ? `up to ${troughOn}` : `${peakOn} to ${troughOn}` },
    ),
    toTrough: daysBetween(peakOn, troughOn),
    toRecovery: peakOn === null ? null : daysBetween(peakOn, recoveredOn),
    ongoing: recoveredOn === null,
  };
}

export interface BestWorst {
  readonly bestDay: Metric;
  readonly worstDay: Metric;
  readonly bestMonth: Metric;
  readonly worstMonth: Metric;
}

const NO_DAILY_SERIES =
  "No daily profit and loss series has been recorded for this portfolio, so there is no best or worst day to name.";
const NO_MONTHLY_SERIES =
  "The valuation series does not span two month ends yet, so there is no best or worst month to name.";

export function bestAndWorst(nav: NavSeries | null): BestWorst {
  const days = (nav?.daily_pnl ?? []).filter((day) => day.pct !== null && day.pct !== undefined);
  const sortedDays = [...days].sort((left, right) =>
    compareDecimalStrings(left.pct ?? "0", right.pct ?? "0"),
  );
  const bestDay = sortedDays[sortedDays.length - 1] ?? null;
  const worstDay = sortedDays[0] ?? null;

  const months = monthlyReturns(nav).filter((month) => month.figure.value !== null);
  const sortedMonths = [...months].sort((left, right) =>
    compareDecimalStrings(left.figure.value ?? "0", right.figure.value ?? "0"),
  );
  const bestMonth = sortedMonths[sortedMonths.length - 1] ?? null;
  const worstMonth = sortedMonths[0] ?? null;

  return {
    bestDay: metric(
      "Best day",
      "The largest one day gain in the window, as a percentage of the portfolio.",
      fractionToPercent(bestDay?.pct ?? null),
      NO_DAILY_SERIES,
      bestDay === null ? {} : { since: bestDay.on },
    ),
    worstDay: metric(
      "Worst day",
      "The largest one day fall in the window.",
      fractionToPercent(worstDay?.pct ?? null),
      NO_DAILY_SERIES,
      worstDay === null ? {} : { since: worstDay.on },
    ),
    bestMonth: metric(
      "Best month",
      "The best whole month in the window, month end to month end on the wealth index.",
      bestMonth?.figure.value ?? null,
      NO_MONTHLY_SERIES,
      bestMonth === null ? {} : { since: bestMonth.label },
    ),
    worstMonth: metric(
      "Worst month",
      "The worst whole month in the window.",
      worstMonth?.figure.value ?? null,
      NO_MONTHLY_SERIES,
      worstMonth === null ? {} : { since: worstMonth.label },
    ),
  };
}

export interface FlowSummary {
  readonly deposits: Metric;
  readonly withdrawals: Metric;
  readonly net: Metric;
  readonly valueChange: Metric;
  /** The sentence that keeps the two halves apart, which is the whole point of the panel. */
  readonly note: string;
}

const FLOW_NOTE =
  "Money you moved in or out is not performance. The value change below is the two added together; the return figures on this tab are measured on the wealth index, which takes your deposits and withdrawals out by construction.";

/**
 * Deposits and withdrawals, separated from performance the only honest way.
 *
 * The obvious way to separate them is to subtract the net flow from the value change, and that is
 * only right when every flow lands at the start of the window. The wealth index already does the
 * separation properly, session by session, so the panel reports the flows as rupees and points at
 * the index for the return rather than doing the subtraction and hoping.
 */
export function flowSummary(nav: NavSeries | null): FlowSummary {
  const points: readonly NavPoint[] = inDateOrder(nav?.points ?? []);
  const inflows: string[] = [];
  const outflows: string[] = [];
  for (const point of points) {
    const parsed = parseDecimal(point.net_flow);
    if (parsed === null || parsed.units === 0n) continue;
    if (parsed.units > 0n) inflows.push(point.net_flow);
    else outflows.push(point.net_flow);
  }

  const first = points[0] ?? null;
  const last = points[points.length - 1] ?? null;
  const change =
    first === null || last === null || points.length < 2
      ? null
      : subtractDecimals(last.value, first.value);

  const noFlows =
    points.length === 0
      ? "The end-of-day valuation series has not been built for this portfolio, so its cash movements cannot be listed."
      : "No money moved in or out of this portfolio over this window.";

  return {
    deposits: metric(
      "Money in",
      "Cash assigned to this portfolio over the window, added exactly.",
      inflows.length === 0 ? null : addDecimalStrings(inflows),
      noFlows,
    ),
    withdrawals: metric(
      "Money out",
      "Cash released from this portfolio over the window, added exactly.",
      outflows.length === 0 ? null : addDecimalStrings(outflows),
      noFlows,
    ),
    net: metric(
      "Net movement",
      "Money in minus money out.",
      inflows.length === 0 && outflows.length === 0 ? null : addDecimalStrings([...inflows, ...outflows]),
      noFlows,
    ),
    valueChange: metric(
      "Value change over the window",
      "What the portfolio was worth at the end of the window minus what it was worth at the start. It contains both the market's work and your own deposits.",
      change,
      "The window holds fewer than two valuations, so there is no change to measure.",
    ),
    note: FLOW_NOTE,
  };
}

export interface ReturnBasis {
  readonly id: string;
  readonly figure: Metric;
  /** How it is calculated, in a sentence a reader can check the number against. */
  readonly how: string;
  /** What question it is the right answer to. */
  readonly useFor: string;
}

const DAYS_IN_YEAR = 365.25;

/**
 * XIRR, TWR and CAGR, each with its calculation stated.
 *
 * CAGR is the one that needs a guard. Annualising a stretch shorter than a year means raising a
 * part-year return to a power greater than one, which turns a good quarter into a spectacular
 * year that never happened. So it is computed only over a window of at least a year, and below
 * that it says how much series there is and what it would take. The exponent is the one floating
 * point operation in this module that reaches a reader, and a rate raised to a fractional power
 * has no exact decimal form; it is a rate rather than a rupee, displayed to two places, which is
 * the same precision `formatRate` gives every other rate on the screen.
 */
export function returnBasis(summary: PortfolioSummary, nav: NavSeries | null): readonly ReturnBasis[] {
  const twr = nav?.total_return ?? null;
  const from = nav?.from_on ?? null;
  const to = nav?.to_on ?? null;
  const span = daysBetween(from, to);
  const twrValue = twr?.value ?? null;

  const cagr =
    twrValue === null || span === null || span < DAYS_IN_YEAR
      ? null
      : (((1 + Number(twrValue)) ** (DAYS_IN_YEAR / span) - 1) * 100).toFixed(2);

  const cagrReason =
    twrValue === null
      ? "A time-weighted return over this window is needed first, and it is not available."
      : span === null
        ? "The valuation window has no start and end date, so the return cannot be spread over a year."
        : `This window covers ${span} day${span === 1 ? "" : "s"}. Annualising a stretch shorter than a year would state a rate the portfolio has never actually run for.`;

  return [
    {
      id: "twr",
      figure: metric(
        twr?.label ?? "Time-weighted return",
        "The wealth index at the end of the window against its start. Every deposit and withdrawal resets the index's base on the day it lands, so what the money did is separated from when it arrived.",
        twrValue === null ? null : fractionToPercent(twrValue),
        twr?.unavailable_reason ?? NO_INDEX_SERIES,
        twr?.since === null || twr?.since === undefined ? {} : { since: twr.since },
      ),
      how: "The wealth index, end against start, with each cash movement resetting the base on the day it lands.",
      useFor: "Comparing this portfolio against an index, or against another portfolio you fund differently.",
    },
    {
      id: "xirr",
      figure: metric(
        summary.xirr.label,
        "The single annual rate that, applied to every cash movement on the date it happened, would produce the portfolio you have now. It answers what your money earned, not what the strategy did.",
        summary.xirr.value === null || summary.xirr.value === undefined
          ? null
          : fractionToPercent(summary.xirr.value),
        summary.xirr.unavailable_reason ?? "Dated deposits and withdrawals are needed to solve for it.",
        summary.xirr.since === null || summary.xirr.since === undefined
          ? {}
          : { since: summary.xirr.since },
      ),
      how: "The rate that discounts every dated cash movement back to the portfolio's current value.",
      useFor: "Comparing against a fixed deposit, where the timing of your money is part of the answer.",
    },
    {
      id: "cagr",
      figure: metric(
        "Compound annual rate",
        "The time-weighted return spread evenly over a year, so a window longer than a year can be read as an annual rate. It is a restatement of the figure above, not a second measurement.",
        cagr,
        cagrReason,
        from === null || to === null ? {} : { since: `${from} to ${to}` },
      ),
      how: "The time-weighted return above, raised to the power of a year divided by the window's length.",
      useFor: "Reading a multi-year window as one annual number. It states nothing the time-weighted return does not.",
    },
  ];
}

/* ------------------------------------------------------------------ *
 * Overview — contributors, detractors and alerts
 * ------------------------------------------------------------------ */

export interface Mover {
  readonly row: HoldingRow;
  readonly figure: Metric;
}

export interface Movers {
  readonly contributors: readonly Mover[];
  readonly detractors: readonly Mover[];
  /** Non-null when there is nothing to rank, and says why. */
  readonly unavailable: string | null;
}

const NO_MOVERS =
  "No holding here has a purchase price on record, so nothing can be ranked by what it has added or taken away.";

/**
 * The names that have helped and the names that have hurt, ranked on unrealised profit.
 *
 * Ranked on rupees rather than on percentages: a 40% gain on a position worth ₹8,000 has not
 * moved this portfolio, and putting it at the top of a "top contributors" list is a claim that it
 * did. The percentage rides along on each row so the reader gets both.
 */
export function movers(detail: PortfolioDetail, count = 5): Movers {
  const rows = holdingRows(detail).filter((row) => row.raw.unrealisedPnl !== null);
  if (rows.length === 0) return { contributors: [], detractors: [], unavailable: NO_MOVERS };

  const sorted = [...rows].sort(
    (left, right) => (right.raw.unrealisedPnl ?? 0) - (left.raw.unrealisedPnl ?? 0),
  );
  const positive = sorted.filter((row) => (row.raw.unrealisedPnl ?? 0) > 0).slice(0, count);
  const negative = sorted
    .filter((row) => (row.raw.unrealisedPnl ?? 0) < 0)
    .slice(-count)
    .reverse();

  return {
    contributors: positive.map((row) => ({ row, figure: row.unrealisedPnl })),
    detractors: negative.map((row) => ({ row, figure: row.unrealisedPnl })),
    unavailable:
      positive.length === 0 && negative.length === 0
        ? "Every holding here is worth exactly what it cost, so there is nothing to rank."
        : null,
  };
}

/* ------------------------------------------------------------------ *
 * G9 — the states the brief names, each with exactly one next action
 * ------------------------------------------------------------------ */

export type WorkspaceStateId =
  | "empty"
  | "no-broker"
  | "partial-sync"
  | "stale-prices"
  | "reconciliation"
  | "missing-cost-basis"
  | "no-history";

export interface WorkspaceAction {
  readonly label: string;
  /** A route inside the app. Nothing here places an order. */
  readonly href: string;
}

export interface WorkspaceState {
  readonly id: WorkspaceStateId;
  readonly severity: "critical" | "review" | "info";
  /** What is true, naming its subject. */
  readonly headline: string;
  /** What it costs the reader, in terms of what this screen can no longer tell them. */
  readonly detail: string;
  /** Exactly one. A state with two next steps is a state nobody acts on. */
  readonly action: WorkspaceAction;
  /** When this would have been noticed. */
  readonly since: string;
}

/**
 * Every state the brief names, detected from the payload, each with one action.
 *
 * *Exactly* one action is the design rule and it is asserted. Two buttons on a problem is a
 * decision handed back to the person who came here to be told what to do, and the second button
 * is always the one that does nothing.
 *
 * None of these is phrased as advice about a position. They are all statements about the data:
 * what is inconsistent, what is unmeasured, what is unreconciled. Baskfy is not registered to say
 * anything else (D3).
 */
export function workspaceStates(
  detail: PortfolioDetail,
  nav: NavSeries | null,
): readonly WorkspaceState[] {
  const summary = detail.summary;
  const rows = holdingRows(detail);
  const brokers = detail.brokers ?? [];
  const id = summary.portfolio_id;
  const states: WorkspaceState[] = [];

  const synced = summary.holdings_synced_on ?? null;
  const detected = synced === null ? "noticed at the last sync, which is undated" : `noticed at the sync on ${synced}`;

  if (rows.length === 0) {
    states.push({
      id: "empty",
      severity: "info",
      headline: "Nothing is allocated to this portfolio yet",
      detail:
        "It exists and it has a name, but no holding has been filed into it, so every figure on this page has nothing to measure.",
      action: { label: "Choose holdings for it", href: "/portfolio/holdings" },
      since: detected,
    });
  }

  if (brokers.length === 0) {
    states.push({
      id: "no-broker",
      severity: rows.length === 0 ? "info" : "critical",
      headline: "No broker account is attached to this portfolio",
      detail:
        "Nothing here can be checked against a live account, so the quantities on this page are whatever was last recorded rather than whatever you hold.",
      action: { label: "Connect a broker", href: "/brokers" },
      since: detected,
    });
  }

  const unpriced = rows.filter((row) => !row.raw.hasPrice);
  if (unpriced.length > 0 && unpriced.length < rows.length) {
    states.push({
      id: "partial-sync",
      severity: "review",
      headline: `${unpriced.length} of ${rows.length} holdings could not be priced`,
      detail:
        "They enter no total on this page, so the value, the weights and the concentration below are measured over the rest. Absent is not zero, and it is not being treated as zero.",
      action: { label: "See which holdings have no price", href: "/portfolio/holdings" },
      since: detected,
    });
  }

  const pricedOn = summary.prices_as_of ?? null;
  if (pricedOn !== null && synced !== null && pricedOn < synced) {
    states.push({
      id: "stale-prices",
      severity: "review",
      headline: `Prices are from ${pricedOn}, and your positions from ${synced}`,
      detail:
        "The prices are older than the positions they are pricing, so the value on this page is what these positions were worth at the last close we have, not at the last one there was.",
      action: { label: "See how old each price is", href: "/portfolio/holdings" },
      since: detected,
    });
  }

  const frozen = rows.filter((row) => row.raw.pendingReconciliation);
  if (summary.pending_reconciliation || frozen.length > 0) {
    const count = Math.max(frozen.length, summary.pending_reconciliation ? 1 : 0);
    states.push({
      id: "reconciliation",
      severity: "critical",
      headline: `${count} holding${count === 1 ? "" : "s"} does not reconcile with the broker`,
      detail:
        "Something changed at your broker that we could not attribute on our own. The affected holdings are held out of the return series until you answer it, so the returns here are measured over less than everything you hold.",
      action: { label: "Answer the difference", href: "/reconcile" },
      since: detected,
    });
  }

  const blind = rows.filter((row) => !row.raw.hasCostBasis);
  if (blind.length > 0) {
    states.push({
      id: "missing-cost-basis",
      severity: "review",
      headline: `${blind.length} holding${blind.length === 1 ? " has" : "s have"} no purchase price on record`,
      detail:
        "Profit since purchase needs a purchase price. Those rows show the reason rather than a zero, because a zero average price is indistinguishable from a free share and would make the whole position read as pure profit.",
      action: { label: "Import your account statement", href: "/portfolio/activity" },
      since: detected,
    });
  }

  const points = nav?.points ?? [];
  if (nav === null || points.length < 2) {
    states.push({
      id: "no-history",
      severity: "info",
      headline: "This portfolio has no valuation history yet",
      detail:
        "Performance, drawdown and the monthly grid all read from an end-of-day valuation series, and this one holds fewer than two days. They will fill in as the series is built.",
      action: { label: "See what has happened here so far", href: `/portfolio/${id}` },
      since: detected,
    });
  }

  return states;
}

/* ------------------------------------------------------------------ *
 * Settings — what this portfolio is configured as
 * ------------------------------------------------------------------ */

export interface SettingRow {
  readonly id: string;
  readonly label: string;
  readonly figure: Metric;
  /** Where the value comes from, so a reader knows whether they can change it. */
  readonly owner: "Set when created" | "From your broker" | "From the published model" | "Not stored yet";
}

/**
 * The configuration this workspace can state, read-only.
 *
 * Every one of these is a fact the payload carries. Changing any of them is PC6's management
 * drawer, which this tab hands off to rather than reimplementing: two places that can rename a
 * portfolio is one place too many, and the second is the one that forgets a validation rule.
 * Nothing on this tab writes.
 */
export function settingRows(detail: PortfolioDetail): readonly SettingRow[] {
  const summary = detail.summary;
  const panel: SourcePanel = detail.source_panel;
  const brokers = detail.brokers ?? [];

  return [
    {
      id: "name",
      label: "Name",
      owner: "Set when created",
      figure: metric("Name", "What this portfolio is called on every screen.", summary.name, "This portfolio has no name recorded."),
    },
    {
      id: "kind",
      label: "Counts toward your net worth",
      owner: "Set when created",
      figure: metric(
        "Counts toward your net worth",
        "A capital portfolio owns its holdings and enters the total. A monitoring view is a lens over holdings that belong elsewhere, and enters no total.",
        summary.counts_toward_total ? "Yes, it is a capital portfolio" : "No, it is a monitoring view",
        "Whether this portfolio enters your totals is not recorded.",
      ),
    },
    {
      id: "source",
      label: "Where it comes from",
      owner: "Set when created",
      figure: metric("Where it comes from", "Whether this tracks a published model, your own screen, your own strategy, or a group you made by hand.", summary.source_badge, "The source of this portfolio is not recorded."),
    },
    {
      id: "model",
      label: "Published model",
      owner: "From the published model",
      figure: metric(
        "Published model",
        "The basket whose weights this portfolio is measured against. Only a basket-backed portfolio has one.",
        panel.basket_name ?? panel.basket_slug ?? null,
        "This portfolio does not track a published model, so it has no target weights and no model record of its own.",
      ),
    },
    {
      id: "publisher",
      label: "Published by",
      owner: "From the published model",
      figure: metric("Published by", "Who publishes the model behind this portfolio.", summary.publisher ?? panel.publisher ?? null, "Nobody publishes this portfolio; you made it."),
    },
    {
      id: "benchmark",
      label: "Benchmark",
      owner: "Set when created",
      figure: metric(
        "Benchmark",
        "The index this portfolio's return is compared against.",
        summary.benchmark?.name ?? null,
        "No benchmark has been chosen for this portfolio, so nothing here can say how it did against the market.",
      ),
    },
    {
      id: "started",
      label: "Measured from",
      owner: "Set when created",
      figure: metric("Measured from", "The date this portfolio's return series begins.", summary.started_on, "The date this portfolio started is not recorded."),
    },
    {
      id: "brokers",
      label: "Broker accounts",
      owner: "From your broker",
      figure: metric(
        "Broker accounts",
        "The accounts the holdings in this portfolio sit in.",
        brokers.length === 0 ? null : brokers.map((broker) => broker.label).join(", "),
        "No broker account is attached to this portfolio.",
      ),
    },
    {
      id: "objective",
      label: "Objective",
      owner: "Not stored yet",
      figure: metric(
        "Objective",
        "What this portfolio is for, in your own words.",
        null,
        "A portfolio has a name, a source and a benchmark, but no objective field. Writing one down would need a change to the portfolio record itself.",
      ),
    },
  ];
}

/** The execution sentence, printed on the Settings and Rebalance tabs alike. */
export function executionNote(detail: PortfolioDetail): string {
  return detail.source_panel.execution_note;
}

/* ------------------------------------------------------------------ *
 * Activity
 * ------------------------------------------------------------------ */

export interface ActivityCount {
  readonly kind: string;
  readonly label: string;
  readonly count: number;
}

/** The kind of each activity row in the reader's words, shared by every surface that lists one. */
export const ACTIVITY_LABELS: Readonly<Record<string, string>> = {
  BUY: "Buys",
  SELL: "Sells",
  DIVIDEND: "Dividends",
  ASSIGN: "Cash assigned",
  RELEASE: "Cash released",
  EXTERNAL_DEPOSIT: "Deposits",
  EXTERNAL_WITHDRAWAL: "Withdrawals",
  CORPORATE_ACTION: "Corporate actions",
  RECONCILIATION: "Reconciliations",
};

/** What is in the feed, by kind, so a reader can see the shape before reading the rows. */
export function activityCounts(items: readonly ActivityItem[] | null): readonly ActivityCount[] {
  if (items === null) return [];
  const counts = new Map<string, number>();
  for (const item of items) counts.set(item.kind, (counts.get(item.kind) ?? 0) + 1);
  return [...counts.entries()]
    .map(([kind, count]) => ({ kind, label: ACTIVITY_LABELS[kind] ?? kind, count }))
    .sort((left, right) => right.count - left.count);
}

/* ------------------------------------------------------------------ *
 * Overview — the snapshot band
 * ------------------------------------------------------------------ */

export interface DetailSnapshot {
  readonly value: Metric;
  readonly invested: Metric;
  readonly cash: Metric;
  readonly todaysPnl: Metric;
  readonly totalPnl: Metric;
  readonly headlineReturn: Metric;
  readonly modelReturn: Metric;
  readonly xirr: Metric;
  readonly benchmarkReturn: Metric;
  readonly benchmarkGap: Metric;
  readonly drawdown: Metric;
  readonly deployed: Metric;
  readonly cashShare: Metric;
  readonly holdingsCount: Metric;
}

/**
 * The figures the Overview tab leads with.
 *
 * `headline_return` and `model_return` stay two separate metrics and are rendered in two separate
 * places: §11 criterion 5 forbids blending a publisher's record with the reader's own, and the
 * strongest reading of "separately" a layout can express is a border between them. Neither is
 * ever subtracted from the other here.
 */
export function detailSnapshot(detail: PortfolioDetail, nav: NavSeries | null): DetailSnapshot {
  const summary = detail.summary;
  const allocation = allocationView(detail);
  const benchmark = summary.benchmark ?? null;
  const maxDrawdown = nav?.max_drawdown ?? null;
  const holdings = detail.holdings ?? [];

  return {
    value: metric(
      "Value",
      "What this portfolio's shares are worth at the latest close we have, cash excluded.",
      summary.value,
      "Nothing in this portfolio could be valued.",
    ),
    invested: metric(
      "Invested",
      "What these shares cost, from the average price your broker or your statement reported.",
      summary.invested,
      summary.invested_unavailable_reason ??
        "Cost basis is missing for some holdings, so the total would understate what you put in.",
    ),
    cash: metric(
      "Cash",
      "Money assigned to this portfolio that is not in a share.",
      summary.cash,
      "No cash balance is recorded against this portfolio.",
    ),
    todaysPnl: metric(
      summary.todays_pnl.label,
      "The change in what these shares are worth since the previous close. Money you moved is not part of it.",
      summary.todays_pnl.amount,
      summary.todays_pnl.unavailable_reason ?? "No previous close to compare against yet.",
      /* `pct` is a stored FRACTION on the wire: `portfolio_overview.py` computes `move / base`
         and quantizes it, so a 0.06% day arrives as `0.000578`. It is converted here, at the one
         seam that builds the metric, because a component that renders `${pct}%` on a raw fraction
         prints "0.000578%" and looks like a rounding bug rather than a units bug. PC2 found the
         same fault in PC1's metric band on 11 Sep; this is the same mistake refused twice. */
      {
        pct: fractionToPercent(summary.todays_pnl.pct ?? null),
        since: summary.todays_pnl.since ?? null,
      },
    ),
    totalPnl: metric(
      summary.total_pnl.label,
      "What these shares are worth now against what they cost. Nothing here has been sold, so nothing here is booked.",
      summary.total_pnl.amount,
      summary.total_pnl.unavailable_reason ??
        "Profit since purchase needs a purchase price for every holding.",
      {
        pct: fractionToPercent(summary.total_pnl.pct ?? null),
        since: summary.total_pnl.since ?? null,
      },
    ),
    headlineReturn: metric(
      summary.headline_return.label,
      "Your return on your own money in this portfolio, measured the way its source calls for.",
      fractionToPercent(summary.headline_return.value ?? null),
      summary.headline_return.unavailable_reason ?? "Not enough history to measure a return yet.",
      { since: summary.headline_return.since },
    ),
    modelReturn: metric(
      summary.model_return?.label ?? "The model's own record",
      "What the publisher reports for the model itself. It is measured on the model, not on your account, and the two are never added, averaged or subtracted from one another.",
      fractionToPercent(summary.model_return?.value ?? null),
      summary.model_return === null || summary.model_return === undefined
        ? "This portfolio does not track a published model, so there is no publisher's record to show beside yours."
        : (summary.model_return.unavailable_reason ??
          "The publisher has not published a record for this model yet."),
      summary.model_return === null || summary.model_return === undefined
        ? {}
        : { since: summary.model_return.since },
    ),
    xirr: metric(
      summary.xirr.label,
      "The annualised rate that accounts for when your money went in and came out.",
      fractionToPercent(summary.xirr.value ?? null),
      summary.xirr.unavailable_reason ?? "Needs dated deposits and withdrawals to solve for.",
      { since: summary.xirr.since ?? null },
    ),
    benchmarkReturn: metric(
      benchmark === null ? "Benchmark" : benchmark.benchmark.label,
      "What the index this portfolio is measured against did over the same stretch.",
      fractionToPercent(benchmark?.benchmark.value ?? null),
      benchmark === null
        ? "No benchmark has been chosen for this portfolio, so there is nothing to measure it against."
        : (benchmark.benchmark.unavailable_reason ??
          "The index has no print over this stretch."),
      benchmark === null ? {} : { since: benchmark.benchmark.since },
    ),
    benchmarkGap: metric(
      benchmark === null ? "Against the benchmark" : `Against ${benchmark.name}`,
      "Your return minus the index's, over the same stretch. Both sides are stated above so the difference can be checked.",
      fractionToPercent(benchmark?.difference ?? null),
      benchmark === null
        ? "No benchmark has been chosen for this portfolio."
        : `There is not enough overlap with ${benchmark.name} to compare the two yet.`,
      benchmark === null ? {} : { since: benchmark.benchmark.since },
    ),
    drawdown: metric(
      "Deepest fall from a high",
      "The largest peak to trough fall in this portfolio's end-of-day valuation series.",
      fractionToPercent(maxDrawdown?.drawdown ?? null),
      "No end-of-day valuation history has been recorded for this portfolio yet, so there is no peak to measure a fall from.",
      maxDrawdown === null ? {} : { since: `${maxDrawdown.peak_on} to ${maxDrawdown.trough_on}` },
    ),
    deployed: allocation.deployedShare,
    cashShare: allocation.cashShare,
    holdingsCount: metric(
      "Holdings",
      "How many positions are filed into this portfolio. One name at two brokers is two of them.",
      String(holdings.length),
      "The holdings for this portfolio did not load.",
    ),
  };
}
