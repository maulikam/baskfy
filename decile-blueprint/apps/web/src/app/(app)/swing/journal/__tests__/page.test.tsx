import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  SwingJournal,
  SwingJournalCard,
  SwingJournalTrade,
  SwingLadder,
} from "@/lib/swing/fetch";

import SwingJournalPage from "../page";

/**
 * SW8, the page half — `docs/swing/05` §2's Journal tab, asserted against the spec:
 *
 * - real and simulated are **two separate cards**, and a simulated close never appears in the
 *   real one (`04` §10: "summarised separately");
 * - the six R buckets render in the API's order, and read at zero trades;
 * - "N of 20 paper sessions logged" (`02` §3.2);
 * - the backtest card carries the caveats **verbatim** when a run exists and says "not run yet"
 *   when it does not (`02` §3.3, `04` §11);
 * - the ladder sentence says what the next close does at each rung and gate (`04` §8.4).
 *
 * The fetch is mocked at the module the page imports, so the page renders exactly what contract
 * C2 puts on the wire and nothing else.
 */

vi.mock("@/lib/swing/fetch", () => ({
  fetchJournal: vi.fn(),
}));

// `SectionTabs` reads `usePathname`, which is null outside a router.
vi.mock("next/navigation", () => ({
  usePathname: () => "/swing/journal",
}));

const { fetchJournal } = await import("@/lib/swing/fetch");
const mocked = vi.mocked(fetchJournal);

const BUCKETS = ["<-1", "-1..0", "0..1", "1..2", "2..3", ">3"] as const;

function histogram(counts: number[]): SwingJournalCard["histogram"] {
  return BUCKETS.map((bucket, index) => ({ bucket, count: counts[index] ?? 0 }));
}

function trade(overrides: Partial<SwingJournalTrade> = {}): SwingJournalTrade {
  return {
    symbol: "REALCO",
    setup: "FLAG",
    entry_date: "2026-08-03",
    exit_date: "2026-08-12",
    entry: 149.6,
    initial_stop: 142.0,
    exit_avg: 164.8,
    quantity: 100,
    r_multiple: 2.0,
    pnl_inr: 1520,
    close_reason: "TRAIL_BREAK",
    ...overrides,
  };
}

function emptyCard(): SwingJournalCard {
  return {
    stats: {
      trades: 0,
      win_rate_pct: 0,
      avg_win_r: 0,
      avg_loss_r: 0,
      expectancy_r: 0,
      profit_factor: null,
      net_r: 0,
      largest_win_r: 0,
      largest_loss_r: 0,
      current_loss_streak: 0,
    },
    histogram: histogram([0, 0, 0, 0, 0, 0]),
    by_setup: [],
    by_month: [],
    trades: [],
  };
}

/** The five-close record `test_api_swing_journal.py` works by hand: 60%, +2.00 / -0.75, 0.90. */
function fiveCloseCard(symbol: string): SwingJournalCard {
  return {
    stats: {
      trades: 5,
      win_rate_pct: 60,
      avg_win_r: 2.0,
      avg_loss_r: -0.75,
      expectancy_r: 0.9,
      profit_factor: 4.0,
      net_r: 4.5,
      largest_win_r: 3.0,
      largest_loss_r: -1.0,
      current_loss_streak: 1,
    },
    histogram: histogram([0, 2, 0, 1, 2, 0]),
    by_setup: [
      { setup: "EP", trades: 2, net_r: 2.5, expectancy_r: 1.25 },
      { setup: "FLAG", trades: 3, net_r: 2.0, expectancy_r: 0.67 },
    ],
    by_month: [
      { month: "2026-07", trades: 2, net_r: 1.5 },
      { month: "2026-08", trades: 3, net_r: 3.0 },
    ],
    trades: [
      trade({ symbol, r_multiple: -0.5, pnl_inr: -380, exit_date: "2026-08-20", close_reason: "STOPPED_OUT" }),
      trade({ symbol, r_multiple: 3.0, pnl_inr: 2280, exit_date: "2026-08-14" }),
      trade({ symbol, r_multiple: 2.0, pnl_inr: 1520, exit_date: "2026-08-12" }),
      trade({ symbol, r_multiple: -1.0, pnl_inr: -760, exit_date: "2026-07-22", close_reason: "STOPPED_OUT" }),
      trade({ symbol, r_multiple: 1.0, pnl_inr: 760, exit_date: "2026-07-10", setup: "EP" }),
    ],
  };
}

function ladder(overrides: Partial<SwingLadder> = {}): SwingLadder {
  return {
    level: 1,
    gate: "GREEN",
    max_open_positions: 4,
    max_exposure_pct: 50,
    new_entries_allowed: true,
    last_r: [1.0, -1.0, 2.0, 3.0, -0.5],
    reads: "SIMULATED",
    ...overrides,
  };
}

function journal(overrides: Partial<SwingJournal> = {}): SwingJournal {
  return {
    real: emptyCard(),
    simulated: fiveCloseCard("PAPERCO"),
    sessions: { logged: 14, required: 20 },
    ladder: ladder(),
    backtest: null,
    ...overrides,
  };
}

async function renderPage(payload: SwingJournal | null) {
  mocked.mockResolvedValue(payload);
  return render(await SwingJournalPage());
}

describe("the two cards", () => {
  it("renders real and simulated as separate cards, and a simulated close never enters the real one", async () => {
    await renderPage(
      journal({
        real: { ...fiveCloseCard("REALCO"), trades: [trade({ symbol: "REALCO" })] },
        simulated: fiveCloseCard("PAPERCO"),
      }),
    );
    const real = screen.getByTestId("journal-card-real");
    const simulated = screen.getByTestId("journal-card-simulated");
    expect(real).not.toBe(simulated);
    expect(within(real).getAllByText("REALCO").length).toBeGreaterThan(0);
    expect(within(real).queryByText("PAPERCO")).toBeNull();
    expect(within(simulated).getAllByText("PAPERCO").length).toBeGreaterThan(0);
    expect(within(simulated).queryByText("REALCO")).toBeNull();
  });

  it("never sums the two records: each card shows its own trade count", async () => {
    await renderPage(journal({ real: emptyCard(), simulated: fiveCloseCard("PAPERCO") }));
    const real = screen.getByTestId("journal-card-real");
    const simulated = screen.getByTestId("journal-card-simulated");
    expect(within(real).getByText("Closed trades").nextSibling).toHaveTextContent("0");
    expect(within(simulated).getByText("Closed trades").nextSibling).toHaveTextContent("5");
    expect(within(real).queryAllByTestId("real-trade")).toHaveLength(0);
  });

  it("renders the six histogram buckets in the API's order with their counts", async () => {
    await renderPage(journal());
    const simulated = screen.getByTestId("journal-card-simulated");
    const items = within(simulated).getByLabelText("Trades by R bucket").querySelectorAll("li");
    expect(Array.from(items).map((item) => item.getAttribute("aria-label"))).toEqual([
      "<-1: 0 trades",
      "-1..0: 2 trades",
      "0..1: 0 trades",
      "1..2: 1 trade",
      "2..3: 2 trades",
      ">3: 0 trades",
    ]);
  });

  it("keeps the six buckets readable at zero trades", async () => {
    await renderPage(journal());
    const real = screen.getByTestId("journal-card-real");
    const items = within(real).getByLabelText("Trades by R bucket").querySelectorAll("li");
    expect(items).toHaveLength(6);
    for (const bucket of BUCKETS) {
      expect(within(real).getByTestId(`real-bucket-${bucket}`)).toHaveTextContent("0");
    }
    expect(within(real).getByText(/every bar is empty/)).toBeInTheDocument();
  });

  it("shows the two groupings, with the month named and R signed", async () => {
    await renderPage(journal());
    const simulated = screen.getByTestId("journal-card-simulated");
    expect(within(simulated).getByText("Aug 2026")).toBeInTheDocument();
    expect(within(simulated).getByText("Jul 2026")).toBeInTheDocument();
    expect(within(simulated).getAllByText("EP").length).toBeGreaterThan(0);
    expect(within(simulated).getAllByText("+3.00R").length).toBeGreaterThan(0);
  });

  it("says why there is no profit factor rather than showing a zero", async () => {
    const card = fiveCloseCard("PAPERCO");
    card.stats.profit_factor = null;
    await renderPage(journal({ simulated: card }));
    const simulated = screen.getByTestId("journal-card-simulated");
    expect(within(simulated).getByText("Profit factor").nextSibling).toHaveTextContent("—");
    expect(within(simulated).getByText(/no loss to measure against/)).toBeInTheDocument();
  });

  it("shows an empty by-month grouping and a close with no reason without failing", async () => {
    const card = fiveCloseCard("PAPERCO");
    card.by_month = [];
    card.trades = [trade({ symbol: "PAPERCO", close_reason: null })];
    await renderPage(journal({ simulated: card }));
    const simulated = screen.getByTestId("journal-card-simulated");
    expect(within(simulated).getByText("No month has a closed trade yet.")).toBeInTheDocument();
    const row = within(simulated).getByTestId("simulated-trade");
    expect(within(row).getByText("—")).toBeInTheDocument();
  });

  it("renders a 200-row trade list and says the statistics cover more than the list", async () => {
    const card = fiveCloseCard("PAPERCO");
    card.stats.trades = 240;
    card.trades = Array.from({ length: 200 }, (_, index) =>
      trade({ symbol: "PAPERCO", exit_date: `2026-0${1 + (index % 9)}-1${index % 9}` }),
    );
    await renderPage(journal({ simulated: card }));
    const simulated = screen.getByTestId("journal-card-simulated");
    expect(within(simulated).getAllByTestId("simulated-trade")).toHaveLength(200);
    expect(within(simulated).getByText(/the latest 200 of 240/)).toBeInTheDocument();
  });

  it("formats the stored numbers with their two places and a sign", async () => {
    await renderPage(journal());
    const simulated = screen.getByTestId("journal-card-simulated");
    const stats = within(simulated).getByTestId("simulated-stats");
    expect(within(stats).getByText("Net R").nextSibling).toHaveTextContent("+4.50R");
    expect(within(stats).getByText("Expectancy").nextSibling).toHaveTextContent("+0.90R a trade");
    expect(within(stats).getByText("Average loss").nextSibling).toHaveTextContent("-0.75R");
    expect(within(stats).getByText("Win rate").nextSibling).toHaveTextContent("60%");
    expect(within(stats).getByText("Profit factor").nextSibling).toHaveTextContent("4.00");
    expect(within(stats).getByText("Loss streak").nextSibling).toHaveTextContent("1 in a row");
    expect(within(simulated).getAllByText("-₹380").length).toBeGreaterThan(0);
  });
});

describe("the sessions counter", () => {
  it('renders "N of 20 paper sessions logged"', async () => {
    await renderPage(journal({ sessions: { logged: 14, required: 20 } }));
    expect(screen.getAllByText("14 of 20 paper sessions logged").length).toBeGreaterThan(0);
    expect(screen.getByRole("progressbar", { name: "14 of 20 paper sessions logged" })).toHaveAttribute(
      "aria-valuenow",
      "14",
    );
  });

  it("does not overflow the bar past the gate", async () => {
    await renderPage(journal({ sessions: { logged: 27, required: 20 } }));
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "20");
  });
});

describe("the ladder and the loss streak", () => {
  const sentence = () => screen.getByTestId("journal-ladder-sentence").textContent ?? "";

  it("at rung 1 with a RED gate says no close moves it until the gate turns", async () => {
    await renderPage(
      journal({
        ladder: ladder({
          level: 0,
          gate: "RED",
          max_open_positions: 2,
          max_exposure_pct: 25,
          new_entries_allowed: false,
          last_r: [2.0, 1.0, 1.5, 0.5, 1.0],
        }),
      }),
    );
    expect(sentence()).toMatch(/RED/);
    expect(sentence()).toMatch(/rung 1 of 4/);
    expect(sentence()).toMatch(/no new entries whatever the next close says/);
    const card = screen.getByTestId("journal-ladder");
    expect(within(card).getByText("Rung").nextSibling).toHaveTextContent("1 of 4");
    expect(within(card).getByText("New entries").nextSibling).toHaveTextContent("not allowed");
  });

  it("with no market row says the gate has not been measured", async () => {
    await renderPage(
      journal({
        ladder: ladder({ level: 0, gate: "UNKNOWN", new_entries_allowed: false, last_r: [] }),
      }),
    );
    expect(sentence()).toMatch(/not been measured/);
    expect(screen.getByText("Gate").nextSibling).toHaveTextContent("not measured");
    expect(screen.getByText(/has no closed trade yet/)).toBeInTheDocument();
  });

  it("two losses in a row: one more steps it down from rung 3 to 2", async () => {
    await renderPage(
      journal({ ladder: ladder({ level: 2, gate: "GREEN", last_r: [2.0, 1.0, 3.0, -1.0, -1.0] }) }),
    );
    expect(sentence()).toMatch(/^2 losses in a row: one more losing close makes 3/);
    expect(sentence()).toMatch(/from rung 3 of 4 to 2; otherwise the last 5 closes are net \+4\.00R in a GREEN tape, so it climbs to rung 4/);
  });

  it("two losses in a row at the bottom: it cannot fall further", async () => {
    await renderPage(
      journal({ ladder: ladder({ level: 0, gate: "AMBER", last_r: [1.0, -1.0, -0.5] }) }),
    );
    expect(sentence()).toMatch(/^2 losses in a row: one more losing close would call for a step down, but the ladder is already at rung 1 of 4; otherwise it cannot climb until 5 trades have closed/);
  });

  it("three losses in a row: it falls a rung each evening the streak stands", async () => {
    await renderPage(
      journal({ ladder: ladder({ level: 3, gate: "GREEN", last_r: [4.0, 2.0, -1.0, -1.0, -0.5] }) }),
    );
    expect(sentence()).toMatch(/^3 losses in a row: the ladder falls a rung each evening/);
    expect(sentence()).toMatch(/rung 4 of 4 becomes 3/);
  });

  it("five losses in a row at the bottom: another loss keeps it there", async () => {
    await renderPage(
      journal({ ladder: ladder({ level: 0, gate: "AMBER", last_r: [-1, -1, -1, -1, -1] }) }),
    );
    expect(sentence()).toMatch(/^5 losses in a row, and the ladder is already at rung 1 of 4/);
    expect(sentence()).toMatch(/Another losing close keeps it there/);
  });

  it("five closes net positive in a GREEN tape: it climbs a rung", async () => {
    await renderPage(
      journal({ ladder: ladder({ level: 1, gate: "GREEN", last_r: [1.0, -1.0, 2.0, 3.0, -0.5] }) }),
    );
    expect(sentence()).toMatch(/^1 loss in a row: 2 losing closes in a row would step the ladder down from rung 2 of 4/);
    expect(sentence()).toMatch(/otherwise the last 5 closes are net \+4\.50R in a GREEN tape, so it climbs to rung 3 at the next settlement/);
  });

  it("at the top rung in a GREEN tape it says so rather than promising rung 5", async () => {
    await renderPage(
      journal({ ladder: ladder({ level: 3, gate: "GREEN", last_r: [1.0, 1.0, 1.0, 1.0, 1.0] }) }),
    );
    expect(sentence()).toMatch(/^No losing streak/);
    expect(sentence()).toMatch(/at the top, rung 4 of 4/);
    expect(sentence()).not.toMatch(/to 5/);
  });

  it("with fewer than five closes says how many it still needs", async () => {
    await renderPage(journal({ ladder: ladder({ level: 0, gate: "GREEN", last_r: [2.0, 1.0] }) }));
    expect(sentence()).toMatch(/would call for a step down, but the ladder is already at rung 1 of 4/);
    expect(sentence()).toMatch(/cannot climb until 5 trades have closed/);
    expect(sentence()).toMatch(/2 closes so far/);
  });

  it("with nothing closed says none so far and that there is nowhere lower to go", async () => {
    await renderPage(journal({ ladder: ladder({ level: 0, gate: "GREEN", last_r: [] }) }));
    expect(sentence()).toBe(
      "No losing streak: 3 losing closes in a row would call for a step down, but the ladder is already at rung 1 of 4; otherwise it cannot climb until 5 trades have closed net positive in a GREEN tape (none so far).",
    );
  });

  it("in an AMBER tape says the rung holds with entries allowed", async () => {
    await renderPage(
      journal({ ladder: ladder({ level: 1, gate: "AMBER", last_r: [1.0, 1.0, 1.0, 1.0, 1.0] }) }),
    );
    expect(sentence()).toMatch(/^No losing streak: 3 losing closes in a row would step the ladder down from rung 2 of 4; otherwise AMBER holds rung 2 of 4 with entries allowed/);
  });

  it("with five closes net negative in a GREEN tape says the rung holds", async () => {
    await renderPage(
      journal({ ladder: ladder({ level: 1, gate: "GREEN", last_r: [-1.0, 0.5, -1.0, 0.5, 0.25] }) }),
    );
    expect(sentence()).toMatch(/net -0\.75R, so rung 2 of 4 holds until they are net positive/);
  });

  it("names the record the ladder reads and lists the closes it read, oldest first", async () => {
    await renderPage(journal({ ladder: ladder({ reads: "SIMULATED" }) }));
    expect(screen.getByText(/The ladder reads the simulated record/)).toBeInTheDocument();
    expect(screen.getByTestId("journal-ladder-last-r")).toHaveTextContent(
      "+1.00R · -1.00R · +2.00R · +3.00R · -0.50R",
    );
    expect(screen.getByText("Allows").nextSibling).toHaveTextContent("4 positions · 50% of the allocation");
  });
});

describe("the backtest card", () => {
  it('says "not run yet" under its own heading when there is no run', async () => {
    await renderPage(journal({ backtest: null }));
    const card = screen.getByTestId("journal-backtest");
    expect(within(card).getByRole("heading", { name: "Backtest, EOD approximation" })).toBeInTheDocument();
    expect(within(card).getByTestId("journal-backtest-empty")).toHaveTextContent(/^Not run yet\./);
  });

  it("shows the caveats verbatim when a run exists", async () => {
    const caveats = [
      "No intraday data, so no opening-range filter: real entries are more selective than these.",
      "No circuit history before 2020; a locked name may have been entered that could not have been.",
      "Survivorship is handled by instrument.delisted_on, nothing more.",
    ];
    await renderPage(
      journal({
        backtest: {
          run_id: 7,
          params: { start: "2017-01-02", end: "2026-08-29", sleeve_inr: 1000000, cost_pct_per_side: 0.13 },
          started_at: "2026-09-01T15:30:00+05:30",
          finished_at: "2026-09-01T15:52:10+05:30",
          stats: {
            trades: 412,
            win_rate_pct: 41.5,
            expectancy_r: 0.31,
            profit_factor: null,
            by_setup: { FLAG: { trades: 300, net_r: 90.5 }, EP: { trades: 112, net_r: 37.25 } },
            equity_curve: [["2017-01-02", 1000000], ["2017-01-03", 1000000]],
          },
          caveats,
        },
      }),
    );
    const list = screen.getByTestId("journal-backtest-caveats");
    const items = Array.from(list.querySelectorAll("li")).map((item) => item.textContent);
    expect(items).toEqual(caveats);
    expect(screen.queryByTestId("journal-backtest-empty")).toBeNull();
    expect(screen.getByText(/^Run 7, started 1 Sept? 2026, 15:30 IST, finished 1 Sept? 2026, 15:52 IST\.$/)).toBeInTheDocument();
    expect(screen.getByText("expectancy r").nextSibling).toHaveTextContent("0.31");
    expect(screen.getByText("profit factor").nextSibling).toHaveTextContent("—");
    // Nested records are opened up two levels; a series is a count, never a dump.
    expect(screen.getByText("by setup · FLAG · trades").nextSibling).toHaveTextContent("300");
    expect(screen.getByText("by setup · EP · net r").nextSibling).toHaveTextContent("37.25");
    expect(screen.getByText("equity curve").nextSibling).toHaveTextContent("2 entries");
    // The schema's word stays in the JSON; the reader sees the product's (SW4.2).
    expect(screen.getByText("allocation inr").nextSibling).toHaveTextContent("1000000");
    expect(screen.queryByText(/sleeve/)).toBeNull();
  });
});

describe("the top of the page", () => {
  it("leads with the record the ladder reads", async () => {
    await renderPage(journal());
    expect(screen.getByLabelText("In short")).toHaveTextContent(
      "The simulated record stands at +4.50R over 5 trades, +0.90R a trade, with 1 loss in a row behind it.",
    );
  });

  it("with no closed trade says so and counts the sessions", async () => {
    await renderPage(journal({ simulated: emptyCard(), sessions: { logged: 3, required: 20 } }));
    expect(screen.getByLabelText("In short")).toHaveTextContent(
      "No simulated trade has closed yet — 3 of 20 paper sessions logged, and the ladder is reading an empty record.",
    );
  });

  it("renders an empty state, not a crash, when the API answers null", async () => {
    await renderPage(null);
    expect(screen.getByTestId("journal-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("journal-card-real")).toBeNull();
    expect(screen.queryByTestId("journal-backtest")).toBeNull();
  });

  it("says on the page that nothing here can trade", async () => {
    await renderPage(journal());
    expect(screen.getByLabelText("In short")).toHaveTextContent("can place an order");
  });
});
