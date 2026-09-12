import { describe, expect, it } from "vitest";

import {
  commandCenter,
  commandCenterCsv,
  CSV_COLUMNS,
  metric,
  NOT_YET_MEASURED,
  VIEWS_NOTICE,
  viewsNotice,
  type Metric,
} from "@/lib/portfolio/command-center";
import type { AllocationSlice } from "@/lib/portfolio/analytics";
import type { Unallocated } from "@/lib/portfolio/organize";
import type { Overview, PortfolioRow } from "@/lib/portfolio/overview";

/**
 * The Command Center's arithmetic and its contract, asserted against the brief's own sentences.
 *
 * The gate these drive hardest is the brief's: **never display "—" without explaining why the
 * value is unavailable.** It is easy to state and very easy to break — one `?? null` in a
 * component and a dash reaches the screen with nothing behind it. So `Metric` is shaped to make
 * that unrepresentable, and `every metric is answerable` below is the test that keeps it so.
 */

/* Fixtures are schema-exact and carry NO `as` — `OverviewOut` and `PortfolioRowOut` are generated
   from the API's own OpenAPI document, so an assertion here would let a fixture describe a payload
   the server never sends and still go green. House rule 2: the test asserts the spec. */
function row(id: number, name: string, value: string, over: Partial<PortfolioRow> = {}): PortfolioRow {
  return {
    portfolio_id: id,
    name,
    kind: "CAPITAL",
    source: "HOLDING_GROUP",
    source_badge: "Grouped",
    started_on: "2026-01-01",
    value,
    cash: "0",
    counts_toward_total: true,
    status: "Synced",
    holdings_count: 3,
    broker_count: 1,
    pending_reconciliation: false,
    todays_pnl: { amount: "0", label: "flat" },
    headline_return: {
      kind: "SINCE_GROUPED",
      label: "Since grouped",
      since: "2026-01-01",
      is_model: false,
    },
    ...over,
  };
}

const NO_PILE: Overview["unallocated"] = {
  cash: "0",
  cta: "Organize into portfolios",
  holdings_count: 0,
  holdings_value: "0",
  total_value: "0",
  pending_reconciliation: false,
};

function overview(over: Partial<Overview> = {}): Overview {
  return {
    hero: {
      current_value: "3680509.37",
      invested: "3100000.00",
      holdings_without_cost_basis: 0,
      pending_reconciliation: false,
      todays_pnl: { amount: "72029.56", label: "today", pct: "0.019900" },
      total_pnl: { amount: "580509.37", label: "total" },
      twr: { label: "TWR since created", since: "2026-01-01", value: "0.187000" },
      xirr: { label: "XIRR", since: "2026-01-01", value: "0.214000" },
      secondary: {
        broker_count: 2,
        cash: "120000.00",
        dividends: "0",
        realised_pnl: { amount: "14000.00", label: "realised" },
        unrealised_pnl: { amount: "566509.37", label: "unrealised" },
      },
    },
    chart: {
      pending_reconciliation: false,
      range: "1Y",
      total_return: { label: "Total return", since: "2026-01-01", value: "0.187000" },
      max_drawdown: { drawdown: "-0.082000", peak_on: "2026-06-01", trough_on: "2026-07-10" },
      drawdown: [{ drawdown: "-2.1", index: "97.9", peak: "3750000.00", on: "2026-09-10" }],
    },
    prices_label: "Prices: close of 2026-09-10",
    prices_as_of: "2026-09-10",
    holdings_synced_label: "Holdings reconciled today at 09:18",
    holdings_synced_on: "2026-09-11",
    sync_summary: "Holdings synced: 2026-09-11",
    live_overlay: false,
    monitoring_excluded_note: "Views overlap and are excluded from total value.",
    open_reconciliation_count: 0,
    portfolios: [row(1, "Long term", "2000000"), row(2, "Swing Manual", "1500000")],
    monitoring_views: [row(9, "High momentum", "900000", { kind: "MONITORING", counts_toward_total: false })],
    sync_status: [],
    attention: [],
    unallocated: NO_PILE,
    ...over,
  };
}

const pile = (v: string): Unallocated => ({
  cash: "0",
  cta: "Sort it",
  holdings_count: 2,
  holdings_value: v,
  total_value: v,
  pending_reconciliation: false,
});

/* Spread first, deliberately. `Object.values` on an interface picks the `{}` overload and hands
   back `any[]`, which would let a non-Metric slip through the very test meant to catch it; the
   spread is an anonymous object type, so the `Metric` overload applies and the array stays typed.
   Spreading also keeps this exhaustive — a metric added to the snapshot is covered on sight. */
const allMetrics = (c: NonNullable<ReturnType<typeof commandCenter>>): Metric[] =>
  Object.values<Metric>({ ...c.snapshot });

describe("the executive snapshot", () => {
  it("snapshot: reports net worth, invested, cash, P&L, XIRR, TWR, drawdown and peak", () => {
    const c = commandCenter(overview(), pile("180509.37"));

    expect(c).not.toBeNull();
    expect(c!.snapshot.netWorth.value).toBe("3680509.37");
    expect(c!.snapshot.invested.value).toBe("3100000.00");
    expect(c!.snapshot.cash.value).toBe("120000.00");
    expect(c!.snapshot.todaysPnl.value).toBe("72029.56");
    /* The server stores FRACTIONS and the snapshot converts once, here, so a renderer formats
       and never scales. `0.019900` in, `1.99` out. */
    expect(c!.snapshot.todaysPnl.pct).toBe("1.99");
    expect(c!.snapshot.unrealisedPnl.value).toBe("566509.37");
    expect(c!.snapshot.realisedPnl.value).toBe("14000.00");
    expect(c!.snapshot.xirr.value).toBe("21.40");
    expect(c!.snapshot.twr.value).toBe("18.70");
    expect(c!.snapshot.drawdown.value).toBe("-8.20");
    expect(c!.snapshot.peak.value).toBe("3750000.00");
  });

  it("snapshot: net worth is capital plus unallocated — and never includes a monitoring view", () => {
    /* The view in the fixture is worth 900000. If it ever leaked into the total, this is the test
       that catches it: 2,000,000 + 1,500,000 + 180,509.37 and not a rupee more. */
    const c = commandCenter(overview(), pile("180509.37"));

    expect(c!.allocation.totalValue).toBe("3680509.37");
    expect(c!.snapshot.netWorth.value).toBe("3680509.37");
  });

  it("snapshot: every metric carries its own label and a definition", () => {
    const c = commandCenter(overview(), pile("1"));

    for (const m of allMetrics(c!)) {
      expect(m.label.length, `${m.label} has no label`).toBeGreaterThan(0);
      expect(m.definition.length, `${m.label} has no definition`).toBeGreaterThan(20);
    }
  });
});

describe("the rule against a bare dash", () => {
  it("unavailable: every metric is either a value or a stated reason — never neither", () => {
    /* The brief's own hard rule. Run over a payload with almost nothing in it, which is the
       shape that produces dashes if anything is going to. */
    const bare = overview({
      hero: {
        current_value: "0",
        holdings_without_cost_basis: 4,
        pending_reconciliation: false,
        todays_pnl: { label: "today" },
        total_pnl: { label: "total" },
        twr: { label: "TWR since created", since: "2026-01-01" },
        xirr: { label: "XIRR", since: "2026-01-01" },
        secondary: {
          broker_count: 0,
          cash: "0",
          dividends: "0",
          realised_pnl: { label: "realised" },
          unrealised_pnl: { label: "unrealised" },
        },
      },
      chart: {
        pending_reconciliation: false,
        range: "1Y",
        total_return: { label: "Total return", since: "2026-01-01" },
      },
      portfolios: [],
      monitoring_views: [],
    });

    const c = commandCenter(bare, null);

    for (const m of allMetrics(c!)) {
      const answerable = m.value !== null || (m.unavailable !== null && m.unavailable.length > 10);
      expect(answerable, `"${m.label}" would render a bare dash`).toBe(true);
    }
  });

  it("unavailable: a metric built with NO reason still answers, rather than showing a dash", () => {
    /* The seam the rule actually lives at. An earlier version of this suite only exercised
       `commandCenter()`, where every call site happens to pass a reason — so deleting the
       fallback left all seventeen tests green. That is a guard that cannot fail, which is worse
       than no guard: it reports safety it is not providing. */
    const missing = metric("Beta", "A definition long enough to be useful.", null, undefined);

    expect(missing.value).toBeNull();
    expect(missing.unavailable).not.toBeNull();
    expect(missing.unavailable!.length).toBeGreaterThan(10);
  });

  it("unavailable: an empty string is treated as absent, not as a value", () => {
    const blank = metric("Cash", "A definition long enough to be useful.", "", "No broker yet.");

    expect(blank.value).toBeNull();
    expect(blank.unavailable).toBe("No broker yet.");
  });

  it("unavailable: the server's own reason is preferred over a generic one", () => {
    const c = commandCenter(
      overview({
        hero: {
          ...overview().hero,
          invested: null,
          invested_unavailable_reason: "Cost basis is missing for 4 holdings.",
        },
      }),
      null,
    );

    expect(c!.snapshot.invested.value).toBeNull();
    expect(c!.snapshot.invested.unavailable).toBe("Cost basis is missing for 4 holdings.");
  });

  it("unavailable: a metric never has both a value and a reason", () => {
    const c = commandCenter(overview(), pile("1"));

    for (const m of allMetrics(c!)) {
      expect(
        m.value === null ? m.unavailable !== null : m.unavailable === null,
        `"${m.label}" has both a value and an unavailable reason`,
      ).toBe(true);
    }
  });
});

describe("monitoring views are never capital", () => {
  it("monitoring: views are kept in their own list, apart from capital portfolios", () => {
    const c = commandCenter(overview(), null);

    expect(c!.capital.map((r) => r.name)).toEqual(["Long term", "Swing Manual"]);
    expect(c!.views.map((r) => r.name)).toEqual(["High momentum"]);
  });

  it("monitoring: the notice prefers the server's wording", () => {
    expect(viewsNotice(overview())).toBe("Views overlap and are excluded from total value.");
  });

  it("monitoring: there is still a notice when the server sends none", () => {
    expect(viewsNotice(overview({ monitoring_excluded_note: "" }))).toBe(
      VIEWS_NOTICE,
    );
    expect(viewsNotice(null)).toContain("excluded from total portfolio value");
  });
});

describe("the data-health strip", () => {
  it("health: counts portfolios, views, holdings and brokers", () => {
    const c = commandCenter(overview(), pile("1"));

    expect(c!.health.portfolioCount).toBe(2);
    expect(c!.health.viewCount).toBe(1);
    expect(c!.health.holdingsCount).toBe(3 + 3 + 2); // two portfolios of 3, plus 2 unallocated
    expect(c!.health.brokerCount).toBe(2);
  });

  it("health: a clean book reports live, with no problems", () => {
    const c = commandCenter(overview(), null);

    expect(c!.health.status).toBe("live");
    expect(c!.health.problems).toEqual([]);
  });

  it("health: every problem NAMES what is affected — never a generic warning", () => {
    const c = commandCenter(
      overview({
        open_reconciliation_count: 2,
        portfolios: [row(1, "Long term", "1", { pending_reconciliation: true })],
        sync_status: [
          { broker: { broker_account_id: 5, broker_id: "zerodha", label: "Zerodha" }, label: "Never synced." },
        ],
      }),
      pile("240000"),
    );

    const headlines = c!.health.problems.map((p) => p.headline);
    expect(headlines).toContain("Long term does not reconcile with the broker");
    expect(headlines).toContain("Zerodha has never synced");
    expect(headlines.some((h) => h.includes("2 unanswered reconciliation questions"))).toBe(true);
    expect(headlines.some((h) => h.includes("2 holdings in no portfolio"))).toBe(true);
    expect(c!.health.status).toBe("action-required");

    for (const p of c!.health.problems) {
      expect(p.detail.length, `${p.headline} has no explanation`).toBeGreaterThan(30);
    }
  });

  it("health: a problem explains why it matters, and never gives investment advice", () => {
    const c = commandCenter(overview({ open_reconciliation_count: 1 }), null);
    const banned = /\b(buy|sell|should invest|recommend|hot|guaranteed)\b/i;

    for (const p of c!.health.problems) {
      expect(banned.test(p.detail), `"${p.detail}" reads as advice`).toBe(false);
    }
  });
});

describe("no fabricated financial metrics", () => {
  it("fabricated: the metrics Baskfy cannot compute are declared, with what blocks each", () => {
    expect(NOT_YET_MEASURED.length).toBeGreaterThanOrEqual(6);
    for (const item of NOT_YET_MEASURED) {
      expect(item.blockedBy.length, `${item.name} does not say what blocks it`).toBeGreaterThan(15);
    }
  });

  it("fabricated: no snapshot metric is one of the blocked names", () => {
    /* The failure this guards: somebody adds `beta: metric("Beta", ..., "1.02", null)` to fill a
       gap in the band. Baskfy has no beta. A number here would be one a person could size a
       position against, and auto-execute is live. */
    const c = commandCenter(overview(), pile("1"));
    const blocked = /\b(beta|value at risk|var\b|sharpe|sortino|sector|target weight|momentum score)\b/i;

    for (const m of allMetrics(c!)) {
      expect(blocked.test(m.label), `"${m.label}" claims a figure Baskfy cannot compute`).toBe(
        false,
      );
    }
  });
});

describe("a missing payload", () => {
  it("state: no overview yields no command centre, rather than a screen of zeroes", () => {
    expect(commandCenter(null, null)).toBeNull();
  });

  it("state: an empty book still answers every metric with a reason", () => {
    const c = commandCenter(
      overview({ portfolios: [], monitoring_views: [] }),
      null,
    );

    expect(c!.capital).toEqual([]);
    expect(c!.health.portfolioCount).toBe(0);
    for (const m of allMetrics(c!)) {
      expect(m.value !== null || m.unavailable !== null).toBe(true);
    }
  });
});

/* ------------------------------------------------------------------ *
 * Exporting the current view — `gates/pc-integration.md` I14
 * ------------------------------------------------------------------ */

function slice(over: Partial<AllocationSlice> = {}): AllocationSlice {
  return {
    portfolioId: 1,
    name: "Swing Manual",
    value: "1250000.00",
    weightPct: "34.00",
    todaysPnl: "12500.00",
    holdingsCount: 7,
    cash: "40000.00",
    returnLabel: "Since grouped",
    returnPct: "12.50",
    ...over,
  };
}

describe("exporting the view a person is looking at", () => {
  it("interaction: the CSV carries every column the comparison table shows", () => {
    const csv = commandCenterCsv([slice()], "capital");
    const lines = csv.split("\n");
    const header = lines.find((line) => line.startsWith("Portfolio,"));

    expect(header).toBe(CSV_COLUMNS.join(","));
    expect(lines.at(-1)).toBe(
      "Swing Manual,1250000.00,34.00,12500.00,Since grouped,12.50,40000.00,7",
    );
  });

  it("unavailable: a figure with no value exports the WORD, never an empty cell", () => {
    /* An empty CSV cell is read as zero by every spreadsheet there is. A portfolio that could
       not be priced would then be worth nothing, which is the same lie the screen's
       no-bare-dash rule exists to prevent — just laundered through a file. */
    const csv = commandCenterCsv(
      [slice({ value: null, weightPct: null, todaysPnl: null, returnLabel: null, returnPct: null })],
      "capital",
    );
    const row = csv.split("\n").at(-1)!;

    expect(row).toBe("Swing Manual,not available,not available,not available,not available,not available,40000.00,7");
    expect(row).not.toMatch(/,,/);
  });

  it("monitoring: a views export says on its own first lines that it does not sum to net worth", () => {
    const capital = commandCenterCsv([slice()], "capital");
    const views = commandCenterCsv([slice({ name: "High Momentum" })], "views");

    expect(views).toMatch(/^# Baskfy — monitoring views/);
    expect(views).toMatch(/do not sum to net worth/);
    expect(capital).toMatch(/sum to net worth/);
    expect(capital).not.toMatch(/overlapping/);
  });

  it("interaction: a name containing a comma or a quote cannot break the row apart", () => {
    const csv = commandCenterCsv([slice({ name: 'Long-term, "core"' })], "capital");
    const row = csv.split("\n").at(-1)!;

    expect(row.startsWith('"Long-term, ""core""",')).toBe(true);
    // Eight columns survive the punctuation: seven commas outside the quoted field.
    expect(row.slice(row.indexOf('",') + 2).split(",")).toHaveLength(7);
  });
});
