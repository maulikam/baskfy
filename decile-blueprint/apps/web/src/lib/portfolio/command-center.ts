import { allocationAnalytics, percentOf, type AllocationAnalytics } from "@/lib/portfolio/analytics";
import type { Unallocated } from "@/lib/portfolio/organize";
import type { Overview, PortfolioRow } from "@/lib/portfolio/overview";

/**
 * Everything the Portfolio Command Center reads, derived once from the payload it already fetches.
 *
 * Brief, 11 Sep 2026 (Maulik): *"Redesign Basqfy's Portfolios screen completely ... a premium,
 * modern financial-intelligence workspace ... It should feel like an intelligent portfolio
 * operating system."* The plan, the decomposition and — most importantly — the survey of which
 * metrics Baskfy can and cannot compute is `docs/PORTFOLIO-COMMAND-CENTER.md`.
 *
 * THE RULE THIS MODULE ENFORCES
 * -----------------------------
 * The brief supplies it: **never display "—" without explaining why the value is unavailable.**
 * So no figure here is ever a bare `null` handed to a component to render as a dash. Every metric
 * is a {@link Metric}: a value, or a *reason there is no value*. A component cannot render one
 * without also having the explanation to hand, which is what makes the rule structural rather
 * than a thing to remember.
 *
 * That matters more here than on a marketing page. Baskfy places real orders with real money —
 * auto-execute has been live on the box since 7 Sep — so a plausible-looking number with nothing
 * behind it is not a cosmetic bug, it is a number somebody may size a position against. Nothing
 * in this file invents a financial figure, and `command-center.test.ts` asserts that the metrics
 * the product genuinely cannot compute (beta, VaR, Sharpe, sector, target weight) never acquire
 * one by accident.
 */

/** A number the screen may show, or the reason it cannot. Never both, never neither. */
export interface Metric {
  /** Decimal string, exactly as the API sent it. `null` only ever means "see `unavailable`". */
  readonly value: string | null;
  /** Why there is no value. Non-null whenever `value` is null — the type is the contract. */
  readonly unavailable: string | null;
  /** The name of the figure, always shown beside it (criterion 3). */
  readonly label: string;
  /** One sentence on how it is calculated, for the tooltip. */
  readonly definition: string;
  /** Percent form where the API supplies one, e.g. today's move. */
  readonly pct?: string | null;
  /** What the figure is measured from, when it has a start date. */
  readonly since?: string | null;
}

/**
 * Exported so its contract can be tested directly, which turned out to matter.
 *
 * The integration test over `commandCenter()` could not reach the fallback below — every call
 * site there passes a reason — so removing the fallback left all 17 tests green. A test that
 * cannot fail is not a guard. This is the seam where the rule actually lives, so this is where it
 * is asserted.
 */
export function metric(
  label: string,
  definition: string,
  value: string | null | undefined,
  unavailableReason: string | null | undefined,
  extra: { pct?: string | null; since?: string | null } = {},
): Metric {
  const has = value !== null && value !== undefined && value !== "";
  return {
    label,
    definition,
    value: has ? value : null,
    // A missing value with no reason is the exact failure the brief forbids, so a generic
    // fallback is supplied rather than allowing a bare dash to reach the screen. It says what is
    // true — we do not know why — instead of inventing a cause.
    unavailable: has ? null : (unavailableReason ?? "Not available for this period."),
    ...extra,
  };
}

/** The operational strip under the header: is the data underneath this screen trustworthy? */
export interface DataHealth {
  readonly portfolioCount: number;
  readonly viewCount: number;
  readonly holdingsCount: number;
  readonly brokerCount: number;
  readonly pricesLabel: string;
  readonly pricesAsOf: string | null;
  readonly holdingsSyncedLabel: string;
  readonly holdingsSyncedOn: string | null;
  /** Named problems, never a generic warning — each says exactly what is affected. */
  readonly problems: readonly HealthProblem[];
  readonly status: "live" | "stale" | "action-required";
}

export interface HealthProblem {
  readonly id: string;
  readonly severity: "action-required" | "stale";
  /** What is wrong, naming the subject. */
  readonly headline: string;
  /** Why it matters and what to do; never phrased as investment advice. */
  readonly detail: string;
  /**
   * When this was detected — the brief requires every alert to say so.
   *
   * Baskfy has no alert table, so there is no moment at which a problem was "raised". What it
   * does have is the pass that would have seen it: the last holdings reconcile. That is the
   * honest answer, and when even that is undated the sentence says so rather than omitting the
   * field and leaving a reader to assume the alert is fresh.
   */
  readonly since: string;
  readonly href?: string;
}

/** The reconcile pass is when a problem with the book would have been noticed. */
function detectedAt(overview: Overview): string {
  const on = overview.holdings_synced_on;
  return on ? `detected at the reconcile on ${on}` : "detected at the last reconcile, which is undated";
}

export interface ExecutiveSnapshot {
  readonly netWorth: Metric;
  readonly invested: Metric;
  readonly cash: Metric;
  readonly unallocated: Metric;
  readonly todaysPnl: Metric;
  readonly unrealisedPnl: Metric;
  readonly realisedPnl: Metric;
  readonly xirr: Metric;
  readonly twr: Metric;
  readonly drawdown: Metric;
  readonly peak: Metric;
}

export interface CommandCenter {
  readonly health: DataHealth;
  readonly snapshot: ExecutiveSnapshot;
  readonly allocation: AllocationAnalytics;
  readonly capital: readonly PortfolioRow[];
  readonly views: readonly PortfolioRow[];
}

/**
 * The two modes the brief insists must never be mixed.
 *
 * Capital portfolios own their holdings exclusively and sum to net worth. Monitoring views are
 * lenses that overlap freely and enter no total (§4.1). The switch defaults to capital, and
 * `viewsNotice` is rendered wherever views are shown so the distinction is stated, not implied.
 */
export type CommandMode = "capital" | "views";

/** Fallback only. The server sends its own wording as `overview.monitoring_excluded_note`, and
 *  {@link viewsNotice} prefers it — one sentence, defined once, rather than two that can drift. */
export const VIEWS_NOTICE =
  "Monitoring views may contain overlapping holdings and are excluded from total portfolio value.";

export function viewsNotice(overview: Overview | null): string {
  const fromServer = overview?.monitoring_excluded_note?.trim();
  return fromServer ? fromServer : VIEWS_NOTICE;
}

/**
 * Metrics this product genuinely cannot compute, and what each would take.
 *
 * Kept as data rather than prose so the screen can *say so on the surface that would have shown
 * them*, and so a test can assert that none of them ever renders as a figure. The full reasoning
 * is in `docs/PORTFOLIO-COMMAND-CENTER.md` §2.2.
 */
export const NOT_YET_MEASURED: ReadonlyArray<{ name: string; blockedBy: string }> = [
  { name: "Sector and industry mix", blockedBy: "instruments carry no sector map yet" },
  { name: "Beta, volatility, Sharpe", blockedBy: "no return-series statistics are computed yet" },
  { name: "Value at Risk and stress tests", blockedBy: "needs a stated model, not just the maths" },
  { name: "Target weights and drift", blockedBy: "a grouped portfolio has no target allocation yet" },
  { name: "Risk contribution by holding", blockedBy: "needs the covariance work above" },
  { name: "Liquidity and days to liquidate", blockedBy: "no traded-volume history is stored yet" },
];

function countProblems(overview: Overview, unallocated: Unallocated | null): HealthProblem[] {
  const problems: HealthProblem[] = [];
  const detected = detectedAt(overview);

  const open = overview.open_reconciliation_count ?? 0;
  if (open > 0) {
    problems.push({
      id: "reconciliation-inbox",
      severity: "action-required",
      headline: `${open} unanswered reconciliation question${open === 1 ? "" : "s"}`,
      detail:
        "Each one freezes its holding's contribution to performance until it is answered, so the returns below are measured over less than everything you hold.",
      since: detected,
      href: "/portfolio/activity",
    });
  }

  for (const row of [...(overview.portfolios ?? []), ...(overview.monitoring_views ?? [])]) {
    if (row.pending_reconciliation) {
      problems.push({
        id: `reconcile-${row.portfolio_id}`,
        severity: "action-required",
        headline: `${row.name} does not reconcile with the broker`,
        detail:
          "Its performance is frozen until the difference is resolved, so the return shown for it is the last one we could stand behind.",
        since: detected,
        href: `/portfolio/${row.portfolio_id}`,
      });
    }
  }

  const loose = unallocated?.holdings_count ?? 0;
  if (loose > 0) {
    problems.push({
      id: "unallocated",
      severity: "action-required",
      headline: `${loose} holding${loose === 1 ? "" : "s"} in no portfolio`,
      detail:
        "They count towards net worth but belong to no strategy, so nothing on this page can tell you how they are doing.",
      since: detected,
      href: "/portfolio/holdings",
    });
  }

  for (const sync of overview.sync_status ?? []) {
    if (!sync.synced_on) {
      problems.push({
        id: `sync-${sync.broker.broker_account_id}`,
        severity: "action-required",
        headline: `${sync.broker.label} has never synced`,
        detail: `${sync.label} Holdings from this account are missing from every figure here.`,
        /* Not the reconcile pass: this account has never been in one. */
        since: "no sync has ever completed for this account",
        href: "/portfolio/holdings",
      });
    }
  }

  return problems;
}

/** Build everything the screen needs. Pure: same payload in, same screen out. */
export function commandCenter(
  overview: Overview | null,
  unallocated: Unallocated | null,
): CommandCenter | null {
  if (overview === null) return null;

  const capital = overview.portfolios ?? [];
  const views = overview.monitoring_views ?? [];
  const hero = overview.hero;
  const allocation = allocationAnalytics(capital, unallocated);
  const problems = countProblems(overview, unallocated);
  const health: DataHealth = {
    portfolioCount: capital.length,
    viewCount: views.length,
    holdingsCount: capital.reduce((n, row) => n + (row.holdings_count ?? 0), 0) +
      (unallocated?.holdings_count ?? 0),
    brokerCount: hero?.secondary?.broker_count ?? 0,
    pricesLabel: overview.prices_label || "Prices not loaded",
    pricesAsOf: overview.prices_as_of ?? null,
    holdingsSyncedLabel: overview.holdings_synced_label || "Holdings not synced yet",
    holdingsSyncedOn: overview.holdings_synced_on ?? null,
    problems,
    status: problems.some((p) => p.severity === "action-required")
      ? "action-required"
      : problems.length > 0
        ? "stale"
        : "live",
  };

  const snapshot: ExecutiveSnapshot = {
    netWorth: metric(
      "Net worth",
      "Every capital portfolio plus unallocated holdings and cash, at the latest close we have. Monitoring views are excluded — they overlap, so adding them would count the same shares twice.",
      allocation.totalValue ?? hero?.current_value,
      "No holdings have been valued yet.",
    ),
    invested: metric(
      "Invested",
      "What the shares you hold cost you, from the average price your broker reports.",
      hero?.invested,
      hero?.invested_unavailable_reason ??
        "Cost basis is missing for some holdings, so the total would understate what you put in.",
    ),
    cash: metric(
      "Cash",
      "Balances your connected brokers report, across every account.",
      hero?.secondary?.cash,
      "No broker has reported a cash balance.",
    ),
    unallocated: metric(
      "Unallocated",
      "Shares that count towards net worth but sit in no portfolio yet.",
      allocation.unallocatedValue ?? unallocated?.holdings_value,
      "Everything you hold is filed into a portfolio.",
    ),
    todaysPnl: metric(
      "Today",
      "The change in the value of what you hold since the previous close. Deposits and withdrawals are not part of it.",
      hero?.todays_pnl?.amount,
      hero?.todays_pnl?.unavailable_reason ?? "No previous close to compare against yet.",
      { pct: hero?.todays_pnl?.pct ?? null },
    ),
    unrealisedPnl: metric(
      "Unrealised",
      "What you would gain or lose if everything you hold were sold at today's price, before costs and taxes.",
      hero?.secondary?.unrealised_pnl?.amount,
      hero?.secondary?.unrealised_pnl?.unavailable_reason ??
        "Needs a cost basis for every holding.",
    ),
    realisedPnl: metric(
      "Realised",
      "Gains and losses already booked on positions you have closed.",
      hero?.secondary?.realised_pnl?.amount,
      hero?.secondary?.realised_pnl?.unavailable_reason ?? "Nothing has been sold yet.",
    ),
    xirr: metric(
      hero?.xirr?.label ?? "XIRR",
      "The annualised return that accounts for when money went in and came out — the honest one to compare against a fixed deposit.",
      hero?.xirr?.value,
      hero?.xirr?.unavailable_reason ?? "Needs dated deposits and withdrawals to solve for.",
      { since: hero?.xirr?.since ?? null },
    ),
    twr: metric(
      hero?.twr?.label ?? "Time-weighted return",
      "Return with the effect of deposits and withdrawals stripped out — the one to compare against an index.",
      hero?.twr?.value,
      hero?.twr?.unavailable_reason ?? "Needs a daily value series to compute.",
      { since: hero?.twr?.since ?? null },
    ),
    drawdown: metric(
      "Drawdown",
      "How far below its own peak the portfolio is now. Zero means it is at a high.",
      overview.chart?.max_drawdown?.drawdown,
      "No value history yet, so there is no peak to measure from.",
    ),
    peak: metric(
      "Peak value",
      "The highest the portfolio has been worth, and when.",
      peakValue(overview),
      "No value history yet.",
      { since: overview.chart?.max_drawdown?.peak_on ?? null },
    ),
  };

  return { health, snapshot, allocation, capital, views };
}

function peakValue(overview: Overview): string | null {
  const points = overview.chart?.drawdown ?? [];
  if (points.length === 0) return null;
  // The peak the drawdown series is measured against — the last one is the running maximum, which
  // is the figure the brief means by "peak portfolio value".
  return points[points.length - 1]?.peak ?? null;
}

/** A portfolio's share of capital, for the comparison table. Re-exported so the table has one
 *  source for the arithmetic it shows beside the bar. */
export { percentOf };
