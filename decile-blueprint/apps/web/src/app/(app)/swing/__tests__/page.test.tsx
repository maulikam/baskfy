import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  SwingMarketDay,
  SwingSetup,
  SwingSetups,
  SwingWatchRow,
} from "@/lib/swing/fetch";

import SwingSetupsPage from "../page";

/**
 * SW11B, the setups page half — `docs/swing/STANDING-ANSWERS.md` A3 and `05` §2:
 *
 * - a candidate with a catalyst renders the headline as a **link** to the exchange's copy,
 *   opening in a new tab with `rel="noopener"`, and never any filing text;
 * - the earnings badge appears when the calendar names a result meeting and not otherwise;
 * - a candidate the feed has nothing for renders no link and no badge — an empty cell, not a
 *   broken one.
 */

vi.mock("@/lib/swing/fetch", () => ({
  fetchSetups: vi.fn(),
  fetchSectors: vi.fn(),
  fetchWatchlist: vi.fn(),
  fetchMarket: vi.fn(),
  fetchBars: vi.fn(),
}));

// The actions reach `next-auth` through the write helper; the page only binds them to forms.
vi.mock("../actions", () => ({
  watchAdd: vi.fn(),
  watchDismiss: vi.fn(),
  scanNow: vi.fn(),
}));

const refresh = vi.fn();
vi.mock("next/navigation", () => ({
  usePathname: () => "/swing",
  useRouter: () => ({ refresh }),
}));

const { fetchSetups, fetchSectors, fetchWatchlist, fetchMarket, fetchBars } =
  await import("@/lib/swing/fetch");

function marketDay(overrides: Partial<SwingMarketDay> = {}): SwingMarketDay {
  return {
    date: "2026-09-02",
    constituent_count: 41,
    pct_up_strong_1m: 5.8,
    pct_new_52w_high: 3.1,
    pct_above_ma_slow: 55,
    index_slug: "nifty-500",
    index_close: 20240,
    index_ma_fast: 20200,
    index_ma_slow: 20150,
    gate: "GREEN",
    exposure_level: 1,
    max_open_positions: 6,
    max_exposure_pct: 75,
    new_entries_allowed: true,
    parabolic_count: 1,
    drawdown_pct: 2.1,
    drawdown_locked: false,
    ...overrides,
  };
}

function watchRow(overrides: Partial<SwingWatchRow> = {}): SwingWatchRow {
  return {
    id: 7,
    instrument_id: 1,
    symbol: "FLAGCO",
    name: "FLAGCO LIMITED",
    setup: "FLAG",
    source: "DETECTOR",
    added_on: "2026-09-01",
    expires_on: "2026-09-15",
    trigger: 149.6,
    stop_ref: 141.86,
    distance_to_trigger_pct: 3.01,
    last_close: 145.1,
    note: null,
    catalyst: null,
    state: "WATCHING",
    focus: false,
    ...overrides,
  };
}

const FILING =
  "https://nsearchives.nseindia.com/corporate/FLAGCO_02092026084105_PR.pdf?x=1&y=2";

function setup(overrides: Partial<SwingSetup> = {}): SwingSetup {
  return {
    instrument_id: 1,
    symbol: "FLAGCO",
    name: "FLAGCO LIMITED",
    setup: "FLAG",
    status: "SETTING_UP",
    score: 62,
    close: 145.1,
    trigger: 149.6,
    stop_ref: 141.86,
    pivot_high: 149.6,
    stop_distance_pct: 5.17,
    adr_pct: 5.6,
    prior_move_pct: 42,
    base_depth_pct: 12,
    tightness_adr: 0.8,
    dryup_ratio: 0.6,
    rvol: null,
    gap_pct: null,
    turnover_avg: 120000000,
    base_bars: 14,
    up_streak: null,
    locked_upper_circuit: false,
    sector_slug: "nifty-it",
    listed_within_2y: false,
    catalyst_feed: null,
    ...overrides,
  };
}

function page(rows: SwingSetup[]): SwingSetups {
  return {
    as_of: "2026-09-02",
    gate: "GREEN",
    exposure_level: 2,
    max_open_positions: 6,
    max_exposure_pct: 75,
    new_entries_allowed: true,
    funnel: { instruments: 2500, liquid: 41 },
    data: rows,
  };
}

async function renderWith(
  rows: SwingSetup[],
  extra: {
    watch?: SwingWatchRow[];
    day?: SwingMarketDay | null;
    setups?: Partial<SwingSetups>;
    status?: string;
  } = {},
) {
  vi.mocked(fetchSetups).mockResolvedValue({ ...page(rows), ...extra.setups });
  vi.mocked(fetchSectors).mockResolvedValue({ as_of: "2026-09-02", data: [] });
  vi.mocked(fetchWatchlist).mockResolvedValue({ data: extra.watch ?? [] });
  vi.mocked(fetchMarket).mockResolvedValue(
    extra.day === null ? null : { data: [extra.day ?? marketDay()] },
  );
  vi.mocked(fetchBars).mockResolvedValue({
    data: [
      { date: "2026-09-01", close: 140, ma_fast: null, ma_slow: null },
      { date: "2026-09-02", close: 145.1, ma_fast: 142, ma_slow: 141 },
    ],
  });
  return render(
    await SwingSetupsPage({
      searchParams: Promise.resolve(extra.status ? { status: extra.status } : {}),
    }),
  );
}

describe("the setups page links out to the catalyst and never reproduces it", () => {
  it("renders the headline as a new-tab link to the exchange's filing", async () => {
    await renderWith([
      setup({
        catalyst_feed: {
          headline: "Press Release - FLAGCO wins a multi-year order",
          published_at: "2026-09-02T08:41:05+05:30",
          url: FILING,
          earnings_date: null,
        },
      }),
    ]);
    const link = screen.getByRole("link", {
      name: "Press Release - FLAGCO wins a multi-year order",
    });
    expect(link).toHaveAttribute("href", FILING);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(screen.queryByText(/earnings/i)).toBeNull();
  });

  it("shows the earnings badge when the calendar names a result meeting", async () => {
    await renderWith([
      setup({
        catalyst_feed: {
          headline: null,
          published_at: null,
          url: null,
          earnings_date: "2026-10-15",
        },
      }),
    ]);
    expect(screen.getByText(/earnings 15 Oct 2026/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /filing/i })).toBeNull();
  });

  it("renders an empty catalyst cell for a name the feed has nothing for", async () => {
    await renderWith([setup({ catalyst_feed: null })]);
    expect(screen.getByText("FLAGCO")).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: "Catalyst" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/nseindia/)).toBeNull();
    expect(screen.queryByText(/earnings/i)).toBeNull();
  });
});

/**
 * SW14 — the cells `05` §2 names that the page lacked: the gate with its two breadth numbers
 * and the index word, the rung or the lock-out in its place, the parabolic heading verbatim,
 * Watch and Dismiss on a row, the focus mark, the filter chips, the chart, and an empty state
 * written from the funnel.
 */
describe("the setups header says why the gate is what it is", () => {
  it("shows the two breadth numbers, the index word and the rung", async () => {
    await renderWith([setup()]);
    const detail = screen.getByTestId("gate-detail");
    expect(detail).toHaveTextContent("5.8% of names up 25% in a month");
    expect(detail).toHaveTextContent("3.1% at a year high");
    expect(detail).toHaveTextContent("10-day above 20-day");
    expect(screen.getByText(/Rung 2 of 4 · up to 6 positions · 75% of the allocation/)).toBeInTheDocument();
  });

  it("puts the lock-out line in place of the rung when the drawdown has locked the allocation", async () => {
    await renderWith([setup()], {
      day: marketDay({ drawdown_locked: true, drawdown_pct: 15.3, index_ma_fast: 20100 }),
    });
    expect(
      screen.getByText(/Locked out · allocation 15.3% below its peak · resumes inside 10%/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Rung 2 of 4/)).toBeNull();
    expect(screen.getByTestId("gate-detail")).toHaveTextContent("10-day below 20-day");
  });

  it("says no index when the averages are not there", async () => {
    await renderWith([setup()], { day: marketDay({ index_ma_fast: null, index_ma_slow: null }) });
    expect(screen.getByTestId("gate-detail")).toHaveTextContent("no index");
  });

  it("heads the parabolic list with the sentence, verbatim, and gives its rows no action", async () => {
    await renderWith([setup({ setup: "PARABOLIC_SHORT", symbol: "VERTCO", up_streak: 7 })]);
    expect(
      screen.getByRole("heading", {
        name: "Parabolic — for the record. Not tradeable on NSE delivery.",
      }),
    ).toBeInTheDocument();
    const row = screen.getByText("VERTCO").closest("tr");
    expect(row?.querySelector("button")).toBeNull();
    expect(row).toHaveTextContent("7 up");
  });
});

describe("the row actions are Watch and Dismiss, and nothing else", () => {
  it("offers Watch, carrying the row's levels, for a candidate not yet on the list", async () => {
    await renderWith([setup()]);
    const row = screen.getByText("FLAGCO").closest("tr");
    const button = within(row as HTMLElement).getByRole("button", { name: "Watch" });
    const form = button.closest("form");
    expect(form?.querySelector('input[name="instrument_id"]')).toHaveValue("1");
    expect(form?.querySelector('input[name="trigger"]')).toHaveValue("149.60");
    expect(form?.querySelector('input[name="stop_ref"]')).toHaveValue("141.86");
    expect(form?.querySelector('input[name="setup"]')).toHaveValue("FLAG");
    expect(within(row as HTMLElement).queryByRole("button", { name: "Dismiss" })).toBeNull();
  });

  it("offers Dismiss, carrying the watch row's id, for a candidate already watched", async () => {
    await renderWith([setup()], { watch: [watchRow({ id: 42 })] });
    const row = screen.getByText("FLAGCO").closest("tr");
    const button = within(row as HTMLElement).getByRole("button", { name: "Dismiss" });
    expect(button.closest("form")?.querySelector('input[name="id"]')).toHaveValue("42");
    expect(within(row as HTMLElement).queryByRole("button", { name: "Watch" })).toBeNull();
    expect(row).toHaveTextContent("watching");
  });

  it("renders no other button on the page but Scan now", async () => {
    await renderWith([setup()], { watch: [watchRow()] });
    const names = screen.getAllByRole("button").map((button) => button.textContent?.trim());
    expect(new Set(names)).toEqual(new Set(["Dismiss", "Scan now"]));
  });
});

describe("Scan now, and the header that says the rows are provisional (SW15)", () => {
  const scanned = "2026-09-02T08:12:00+00:00"; // 13:42 IST

  it("says provisional — scanned 13:42 IST from live quotes when the rows are from live quotes", async () => {
    await renderWith([setup()], {
      setups: { as_of_provisional: true, scanned_at: scanned },
    });
    expect(screen.getByTestId("scan-label")).toHaveTextContent(
      "provisional — scanned 13:42 IST from live quotes",
    );
    expect(screen.getByText(/built from live quotes — provisional until the nightly/)).toBeInTheDocument();
  });

  it("says nothing of the kind for a day the nightly wrote", async () => {
    await renderWith([setup()]);
    expect(screen.queryByTestId("scan-label")).toBeNull();
    expect(screen.getByText(/Detected from published end-of-day bars/)).toBeInTheDocument();
  });

  it("labels an on-demand re-scan of a published day as such, not as provisional", async () => {
    await renderWith([setup()], {
      setups: { as_of_provisional: false, scanned_at: "2026-09-02T12:32:00+00:00" },
    });
    expect(screen.getByTestId("scan-label")).toHaveTextContent(
      "re-scanned 18:02 IST from published bars",
    );
  });

  it("offers Scan now bound to the action, with the last run's result beside it", async () => {
    await renderWith([setup()], {
      setups: {
        last_scan: {
          run_id: 9,
          status: "DONE",
          requested_at: "2026-09-02T08:11:00+00:00",
          started_at: "2026-09-02T08:11:30+00:00",
          finished_at: scanned,
          session_date: "2026-09-02",
          provisional: true,
          funnel: { instruments: 1812, liquid: 41, candidates: { FLAG: 2, EP: 0 } },
          detail: null,
          error: null,
        },
      },
    });
    const control = screen.getByTestId("scan-now");
    const button = within(control).getByRole("button", { name: "Scan now" });
    expect(button).toBeEnabled();
    expect(control.tagName).toBe("FORM");
    expect(control).not.toHaveAttribute("method");
    // When it ran, that it finished, and what it found — the three facts the gap of 12 Sep 2026
    // was missing. Past a day the age gives way to the IST wall clock; `scan-now.test.tsx` in
    // the sibling sleeves asserts the recent half.
    expect(within(control).getByRole("status")).toHaveTextContent(
      "Last scanned at 2 Sept 2026, 13:42 IST from live quotes — 41 liquid, 2 setups.",
    );
    expect(refresh).not.toHaveBeenCalled();
  });

  it("disables the button and says scanning while a run is in flight", async () => {
    await renderWith([setup()], {
      setups: {
        last_scan: {
          run_id: 10,
          status: "RUNNING",
          requested_at: "2026-09-02T08:11:00+00:00",
          started_at: "2026-09-02T08:11:30+00:00",
          finished_at: null,
          session_date: null,
          provisional: false,
          funnel: null,
          detail: null,
          error: null,
        },
      },
    });
    const control = screen.getByTestId("scan-now");
    expect(within(control).getByRole("button", { name: "Scanning…" })).toBeDisabled();
    expect(within(control).getByRole("status")).toHaveTextContent(
      "Scanning now. This page updates when it finishes.",
    );
  });

  /**
   * THE REASON IS NOT THE READER'S. This test asserted the opposite until 12 Sep 2026: it pinned
   * `run.error` onto a customer-facing page, which is the defect of 11 Sep 2026 said again — that
   * sentence names jobs, quote sources and tables and is written for whoever can fix it. `/vbt`
   * and `/twt` already refused to render it; the book now agrees with them. The reader gets the
   * three facts that are theirs: when it stopped, that nothing changed, that they may press again.
   */
  it("says a run did not finish without repeating the reason it gives itself", async () => {
    await renderWith([setup()], {
      setups: {
        last_scan: {
          run_id: 11,
          status: "FAILED",
          requested_at: "2026-09-02T08:11:00+00:00",
          started_at: "2026-09-02T08:11:30+00:00",
          finished_at: "2026-09-02T08:11:31+00:00",
          session_date: "2026-09-02",
          provisional: true,
          funnel: null,
          detail: null,
          error: "ScanNotRunnable: the market is open and there is no Kite quote source",
        },
      },
    });
    const status = within(screen.getByTestId("scan-now")).getByRole("status");
    expect(status).toHaveTextContent(
      "The last scan did not finish, so nothing changed. It stopped at 2 Sept 2026, 13:41 IST. " +
        "You can start another.",
    );
    expect(status.textContent).not.toContain("ScanNotRunnable");
    expect(status.textContent).not.toContain("Kite");
    expect(screen.getByRole("button", { name: "Scan now" })).toBeEnabled();
  });
});

describe("focus, filters, the chart and the empty state", () => {
  it("marks a focus row and counts it in the header", async () => {
    await renderWith([setup()], { watch: [watchRow({ focus: true })] });
    const row = screen.getByText("FLAGCO").closest("tr");
    expect(row).toHaveAttribute("data-focus", "true");
    expect(within(row as HTMLElement).getByLabelText("focus")).toBeInTheDocument();
    expect(screen.getByTestId("gate-detail")).toHaveTextContent("marks the 1 in today’s focus");
  });

  it("passes the status chip through to the read and marks the chip current", async () => {
    await renderWith([setup({ status: "TRIGGERED" })], { status: "TRIGGERED" });
    expect(fetchSetups).toHaveBeenLastCalledWith({ status: "TRIGGERED" });
    expect(screen.getByRole("link", { name: "Triggered" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Every status" })).not.toHaveAttribute("aria-current");
  });

  it("draws the mini chart from the row's bars", async () => {
    await renderWith([setup()]);
    expect(fetchBars).toHaveBeenCalledWith(1, "2026-09-02");
    expect(screen.getByRole("img", { name: /FLAGCO: the last 2 closes/ })).toBeInTheDocument();
  });

  it("writes the empty state from the funnel", async () => {
    await renderWith([], { setups: { funnel: { instruments: 2500, liquid: 41, candidates: { FLAG: 0 } } } });
    expect(
      screen.getByText(/No flags today — 2,500 names had a bar, 41 of them were liquid enough, and 0 met the rules\./),
    ).toBeInTheDocument();
  });

  it("says that no scan has run when there is no funnel at all", async () => {
    await renderWith([], { setups: { funnel: null, gate: null, as_of: null }, day: null });
    expect(screen.getAllByText(/no scan has run yet/i).length).toBeGreaterThan(0);
  });
});
