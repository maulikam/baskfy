import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PortfolioOverviewScreen } from "@/components/portfolio/overview-screen";
import type { InspectorData } from "@/lib/portfolio/inspector";
import { totalOfRows } from "@/lib/portfolio/overview";

import { EMPTY_OVERVIEW, MONITORING_ROW, OVERVIEW } from "./overview-fixture";

/**
 * `PORTFOLIO_REDESIGN.md` §6, asserted as the spec states it — never as the code happens to
 * behave (CLAUDE.md house rule 2). Each `describe` names the clause it defends.
 *
 * `next/navigation` is mocked with spies rather than stubbed away, because one of these tests is
 * *about* navigation not happening: §6.5's drawer opens over the page, and the only way to prove
 * that is to watch the router and see nothing.
 */

const push = vi.fn();
const replace = vi.fn();
const refresh = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace, refresh, back: vi.fn(), forward: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/portfolio/overview",
  useSearchParams: () => new URLSearchParams(),
}));

const NO_INSPECTOR_DATA: InspectorData = {
  detail: null,
  nav: null,
  activity: null,
  failures: { detail: null, nav: null, activity: null },
};

beforeEach(() => {
  push.mockClear();
  replace.mockClear();
  refresh.mockClear();
});

afterEach(cleanup);

function draw(overview = OVERVIEW, inspector: InspectorData = NO_INSPECTOR_DATA) {
  const load = vi.fn().mockResolvedValue(inspector);
  const utils = render(
    <PortfolioOverviewScreen
      overview={overview}
      loadChart={vi.fn().mockResolvedValue(null)}
      loadInspectorData={load}
    />,
  );
  return { ...utils, load };
}

describe("§6.1 — the header states two timestamps, and never merges them", () => {
  it("prints the price close date and the holdings sync date as separate facts", () => {
    draw();
    const prices = screen.getByTestId("prices-as-of");
    const synced = screen.getByTestId("holdings-synced");

    expect(prices).not.toBe(synced);
    expect(prices).toHaveTextContent("Prices: close of 21 Aug 2026");
    expect(synced).toHaveTextContent("Holdings synced: 25 Aug 2026");
    // The dates really are different days: merging them would have to lose one of them.
    expect(prices.textContent).not.toEqual(synced.textContent);
  });

  it("says which fact is missing rather than borrowing the other one's date", () => {
    draw(EMPTY_OVERVIEW);
    expect(screen.getByTestId("prices-as-of")).toHaveTextContent("No closing prices yet");
    expect(screen.getByTestId("holdings-synced")).toHaveTextContent("Holdings not synced yet");
  });

  it("carries the title and the subtitle §6.1 specifies", () => {
    draw();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("My Portfolio");
    expect(
      screen.getByText("Your complete investment picture across baskets and brokers."),
    ).toBeInTheDocument();
  });

  it("hides every amount when the reader asks, and shows them again", async () => {
    const user = userEvent.setup();
    draw();
    const value = screen.getByTestId("hero-current-value");
    expect(value).toHaveTextContent("₹7,02,500");

    await user.click(screen.getByTestId("toggle-amounts"));
    expect(value).not.toHaveTextContent("7,02,500");
    expect(value).toHaveTextContent("••••••");

    await user.click(screen.getByTestId("toggle-amounts"));
    expect(value).toHaveTextContent("₹7,02,500");
  });
});

describe("§6.2 — five hero metrics, and not a sixth", () => {
  it("shows exactly the five §6.2 names", () => {
    draw();
    const hero = screen.getByTestId("hero-metrics");
    // Five cards, counted as children rather than by name: a sixth metric added later fails here
    // whatever it is called.
    expect(hero.children).toHaveLength(5);

    expect(screen.getByTestId("hero-current-value")).toHaveTextContent("₹7,02,500");
    expect(screen.getByTestId("hero-todays-pnl")).toHaveTextContent("+₹2,280");
    expect(screen.getByTestId("hero-total-pnl")).toHaveTextContent("+₹94,300");
    expect(screen.getByTestId("hero-xirr")).toHaveTextContent("+17.12%");
    expect(screen.getByTestId("hero-invested")).toHaveTextContent("₹6,08,200");
  });

  it("shows today's move in rupees and in percent, as §6.2 asks", () => {
    draw();
    const today = screen.getByTestId("todays-pnl-figure");
    expect(today).toHaveTextContent("+₹2,280");
    expect(today).toHaveTextContent("+0.33%");
  });

  it("keeps cash, the realised/unrealised split, dividends and brokers in a collapsed row", async () => {
    const user = userEvent.setup();
    draw();
    expect(screen.queryByTestId("hero-secondary")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("hero-secondary-toggle"));
    const secondary = screen.getByTestId("hero-secondary");
    expect(within(secondary).getByText("Cash")).toBeInTheDocument();
    expect(within(secondary).getByText("Realised")).toBeInTheDocument();
    expect(within(secondary).getByText("Unrealised")).toBeInTheDocument();
    expect(within(secondary).getByText("Dividends")).toBeInTheDocument();
    expect(within(secondary).getByText("Brokers")).toBeInTheDocument();
  });

  it("prints an em dash and a reason where a figure cannot be computed, never a zero", () => {
    // The empty account still renders its hero row; every unavailable number says why.
    render(
      <PortfolioOverviewScreen
        overview={{ ...EMPTY_OVERVIEW, sync_status: OVERVIEW.sync_status ?? [] }}
        loadChart={vi.fn().mockResolvedValue(null)}
        loadInspectorData={vi.fn().mockResolvedValue(NO_INSPECTOR_DATA)}
      />,
    );
    const invested = screen.getByTestId("hero-invested");
    expect(invested).toHaveTextContent("—");
    expect(invested).toHaveTextContent("No purchases have been recorded.");
    expect(invested).not.toHaveTextContent("₹0");

    const xirr = screen.getByTestId("hero-xirr");
    expect(xirr).toHaveTextContent("—");
    expect(xirr).toHaveTextContent("We need at least one cash flow to compute this.");
  });
});

describe("§11 criterion 3 — every return says what it is and when it started", () => {
  it("gives each return a visible label and its start date on hover", () => {
    draw();
    const returns = screen.getAllByTestId("return-value");
    expect(returns.length).toBeGreaterThan(0);

    for (const node of returns) {
      const title = node.getAttribute("title") ?? "";
      // Criterion 3's two halves: what it is, and since when.
      expect(title).not.toBe("");
      expect(title).toMatch(/Measured since \d|Start date not recorded/);
      // And the label is on the screen, not only in the tooltip.
      expect(node.textContent ?? "").toMatch(/[A-Za-z]{3}/);
    }
  });

  it("labels the consolidated XIRR by name and dates it", () => {
    draw();
    const xirr = screen.getByTestId("hero-xirr-value");
    expect(xirr).toHaveTextContent("XIRR since your first purchase");
    expect(xirr.getAttribute("title")).toContain("Measured since 1 Apr 2025");
  });

  it("labels a holding group's return 'Since grouped' rather than borrowing XIRR's name", () => {
    draw();
    const table = screen.getByTestId("portfolio-table");
    const row = within(table).getByText("Long-term core").closest("tr");
    expect(row).not.toBeNull();
    const figure = within(row as HTMLElement).getByTestId("return-value");
    expect(figure).toHaveTextContent("Since grouped");
    expect(figure).toHaveTextContent("—");
    expect(figure.getAttribute("title")).toContain("Import your account statement");
  });

  it("never renders a bare percentage — every one is inside a labelled figure", () => {
    const { container } = draw();
    const bare = Array.from(container.querySelectorAll("td, th, dd"))
      .filter((cell) => /-?\d+\.\d\d%/.test(cell.textContent ?? ""))
      .filter((cell) => cell.querySelector("[data-testid='return-value']") === null);
    expect(bare.map((cell) => cell.textContent)).toEqual([]);
  });
});

describe("§4.1 / criterion 2 — a monitoring view never reaches a total", () => {
  it("keeps monitoring views out of the All tab", () => {
    draw();
    const table = screen.getByTestId("portfolio-table");
    expect(within(table).queryByText("Defence stocks")).not.toBeInTheDocument();
    expect(within(table).getByText("Momentum 30")).toBeInTheDocument();
  });

  it("sums only the capital portfolios under the table", () => {
    draw();
    // 4,80,000 + 2,20,000. The monitoring view's 1,50,000 is not in it.
    expect(screen.getByTestId("table-total")).toHaveTextContent("₹7,00,000");
    expect(totalOfRows([MONITORING_ROW])).toBeNull();
  });

  it("shows §4.1's sentence instead of a subtotal on the Monitoring views tab", async () => {
    const user = userEvent.setup();
    draw();
    await user.click(screen.getByTestId("grouping-tab-MONITORING"));

    expect(screen.getByTestId("monitoring-note")).toHaveTextContent(
      "Monitoring view — overlaps with other portfolios, excluded from totals.",
    );
    expect(screen.getByTestId("table-total")).toHaveTextContent("—");
    expect(screen.getByTestId("table-total")).not.toHaveTextContent("1,50,000");
  });

  it("styles a monitoring row more quietly than a capital one", async () => {
    const user = userEvent.setup();
    draw();
    await user.click(screen.getByTestId("grouping-tab-MONITORING"));

    const row = screen.getByTestId("portfolio-row");
    expect(row).toHaveAttribute("data-counts", "false");
    expect(row.className).toContain("text-muted-foreground");
    expect(row).toHaveTextContent(
      "Monitoring view — overlaps with other portfolios, excluded from totals.",
    );
  });
});

describe("§9 — third-party content is a subscribed model, never a managed one", () => {
  it("names the publisher in the permitted wording", () => {
    draw();
    expect(screen.getAllByTestId("source-badge")[0]).toHaveTextContent(
      "Subscribed model by Bramha Research",
    );
  });

  it("uses none of the forbidden words anywhere on the screen", () => {
    const { container } = draw();
    const text = container.textContent ?? "";
    for (const word of ["managed", "advisory", "PMS", "portfolio management service"]) {
      expect(text.toLowerCase()).not.toContain(word.toLowerCase());
    }
  });
});

describe("§11 criterion 5 — model and actual are never one figure", () => {
  it("shows the publisher's record as a second, separately labelled number", async () => {
    const user = userEvent.setup();
    draw();
    await user.click(within(screen.getByTestId("portfolio-table")).getByText("Momentum 30"));

    const mine = await screen.findByTestId("drawer-headline-return");
    const model = screen.getByTestId("drawer-model-return");
    expect(mine).not.toBe(model);
    expect(mine).toHaveAttribute("data-model", "false");
    expect(model).toHaveAttribute("data-model", "true");
    expect(mine).toHaveTextContent("+18.42%");
    expect(model).toHaveTextContent("+21.30%");
    expect(model).toHaveTextContent("model, not yours");
  });
});

describe("§6.5 — the row opens an inspector drawer, without leaving the page", () => {
  it("opens the drawer and calls no router method", async () => {
    const user = userEvent.setup();
    const { load } = draw();
    expect(screen.queryByTestId("inspector-drawer")).not.toBeInTheDocument();

    await user.click(within(screen.getByTestId("portfolio-table")).getByText("Momentum 30"));

    expect(await screen.findByTestId("inspector-drawer")).toBeInTheDocument();
    expect(load).toHaveBeenCalledWith(1);
    // The table is still mounted behind it: nothing was unmounted, nothing was routed.
    expect(screen.getByTestId("portfolio-table")).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe("/");
  });

  it("carries the three tabs §6.5 names, and a way to the full page", async () => {
    const user = userEvent.setup();
    draw();
    await user.click(within(screen.getByTestId("portfolio-table")).getByText("Momentum 30"));
    await screen.findByTestId("inspector-drawer");

    expect(screen.getByTestId("drawer-tab-PERFORMANCE")).toBeInTheDocument();
    expect(screen.getByTestId("drawer-tab-HOLDINGS")).toBeInTheDocument();
    expect(screen.getByTestId("drawer-tab-ACTIVITY")).toBeInTheDocument();
    expect(screen.getByTestId("drawer-full-page")).toHaveAttribute("href", "/portfolio/1");
  });

  it("closes without navigating either", async () => {
    const user = userEvent.setup();
    draw();
    await user.click(within(screen.getByTestId("portfolio-table")).getByText("Momentum 30"));
    await screen.findByTestId("inspector-drawer");

    await user.click(screen.getByLabelText("Close details"));
    expect(screen.queryByTestId("inspector-drawer")).not.toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });
});

describe("§6.4 — the ribbon deep-links, and can be dismissed", () => {
  it("links each item to the flow that resolves it", () => {
    draw();
    const items = screen.getAllByTestId("attention-item");
    expect(items).toHaveLength(2);
    const links = screen.getAllByTestId("attention-link");
    expect(links[0]).toHaveAttribute("href", "/portfolio/holdings");
    expect(links[1]).toHaveAttribute("href", "/brokers");
  });

  it("dismisses an item and says how to bring it back", async () => {
    const user = userEvent.setup();
    draw();
    await user.click(screen.getAllByTestId("attention-dismiss")[0] as HTMLElement);
    expect(screen.getAllByTestId("attention-item")).toHaveLength(1);

    await user.click(screen.getByTestId("attention-restore"));
    expect(screen.getAllByTestId("attention-item")).toHaveLength(2);
  });
});

describe("§6.3 — the chart offers the ranges the data supports and no others", () => {
  it("offers 1M, 3M, 1Y, 3Y and All, and never 1D or 1W", () => {
    draw();
    for (const range of ["1M", "3M", "1Y", "3Y", "ALL"]) {
      expect(screen.getByTestId(`chart-range-${range}`)).toBeInTheDocument();
    }
    expect(screen.queryByTestId("chart-range-1D")).not.toBeInTheDocument();
    expect(screen.queryByTestId("chart-range-1W")).not.toBeInTheDocument();
  });

  it("has a value/return switch, a benchmark overlay and a drawdown overlay", () => {
    draw();
    expect(screen.getByTestId("chart-mode-value")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("chart-mode-return")).toBeInTheDocument();
    expect(screen.getByTestId("chart-benchmark-toggle")).toBeChecked();
    expect(screen.getByTestId("chart-drawdown-toggle")).not.toBeChecked();
    expect(screen.getAllByText(/Nifty 500/).length).toBeGreaterThan(0);
  });

  it("draws the falls-from-peak panel only when asked", async () => {
    const user = userEvent.setup();
    draw();
    expect(screen.queryByTestId("drawdown-panel")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("chart-drawdown-toggle"));
    expect(screen.getByTestId("drawdown-panel")).toBeInTheDocument();
  });
});

describe("§11 criterion 8 — the empty state leads to a broker, not the catalog", () => {
  it("offers 'Connect your broker' as the one primary action", () => {
    draw(EMPTY_OVERVIEW);
    const empty = screen.getByTestId("overview-empty");
    expect(within(empty).getByTestId("empty-connect-broker")).toHaveAttribute("href", "/brokers");
    expect(empty).toHaveTextContent("Start with the shares you already own");
  });

  it("does not fill the screen with metrics that all read as nothing", () => {
    draw(EMPTY_OVERVIEW);
    expect(screen.queryByTestId("hero-metrics")).not.toBeInTheDocument();
    expect(screen.queryByTestId("portfolio-table")).not.toBeInTheDocument();
    expect(screen.queryByTestId("combined-chart")).not.toBeInTheDocument();
  });

  it("still shows the header, so the reader knows where they are", () => {
    draw(EMPTY_OVERVIEW);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("My Portfolio");
    expect(screen.getByTestId("prices-as-of")).toBeInTheDocument();
  });
});
