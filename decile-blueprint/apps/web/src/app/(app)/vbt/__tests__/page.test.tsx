import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  VbtBacktest,
  VbtBook,
  VbtBreadth,
  VbtCandidate,
  VbtToday,
} from "@/lib/vbt/fetch";

import VbtBacktestPage from "../backtest/page";
import VbtBookPage from "../book/page";
import VbtTodayPage from "../page";

/**
 * VB8's rendered-DOM acceptance (`docs/vbt/06` VB8): the four things `05` §2 says a reader must
 * see whatever the data does.
 *
 * - **the funnel line at zero candidates**, because an empty list and a detector that never ran
 *   render identically without it, and those need opposite responses;
 * - **the gate badge in both states**, with the SHUT copy saying what the book does anyway —
 *   "SHUT" must not read as "sell everything";
 * - **the caveats above the numbers**, as a component (house rule 9) — asserted by document
 *   order, not by presence, because their placement is the whole point;
 * - **the fill-rate line**, on the page from the first fill and honest before it.
 */

vi.mock("@/lib/vbt/fetch", () => ({
  fetchToday: vi.fn(),
  fetchBreadth: vi.fn(),
  fetchBook: vi.fn(),
  fetchBacktest: vi.fn(),
  fetchBars: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/vbt",
  useRouter: () => ({ refresh: vi.fn() }),
}));

const { fetchToday, fetchBreadth, fetchBook, fetchBacktest, fetchBars } =
  await import("@/lib/vbt/fetch");

function candidate(overrides: Partial<VbtCandidate> = {}): VbtCandidate {
  return {
    instrument_id: 1,
    symbol: "VBTCO",
    name: "VBTCO LIMITED",
    state: "SIGNAL",
    failed_filters: [],
    close: 149.6,
    limit_price: 149.6,
    stop_price: 131.65,
    change_pct: 8.4,
    rvol: 3.2,
    close_position: 0.91,
    ret_20_pct: 12.4,
    turnover_avg_20: 340_000_000,
    sma_200: 100,
    ema_21: 138.2,
    high_20_prior: 146.0,
    pct_above_dma: 49.6,
    locked_upper_circuit: false,
    rank_key: 340_000_000,
    ...overrides,
  };
}

function today(overrides: Partial<VbtToday> = {}): VbtToday {
  return {
    as_of: "2026-09-08",
    gate: "OPEN",
    pct_above_dma: 62.4,
    above_count: 881,
    measured_count: 1412,
    gate_threshold_pct: 40,
    thin_session: false,
    funnel: {
      funnel: {
        universe: 4186,
        with_bar: 1412,
        with_dma: 1412,
        scan_hits: 37,
        signals: 4,
      },
    },
    shut_sessions_recent: 3,
    shut_window: 60,
    candidates: [candidate()],
    rejects: [],
    ...overrides,
  };
}

const NO_BREADTH: VbtBreadth = { threshold_pct: 40, data: [] };

function book(overrides: Partial<VbtBook> = {}): VbtBook {
  return {
    working: [],
    open_positions: [],
    closed_positions: [],
    fill_rate: { filled: 0, resolved: 0, rate_pct: null, modelled_pct: 83.8 },
    ...overrides,
  };
}

const BACKTEST: VbtBacktest = {
  published: {
    start: "2017-10-16",
    end: "2026-09-09",
    years: 8.9,
    cagr_pct: 18.23,
    max_drawdown_pct: -27.94,
    trades: 761,
    win_rate_pct: 37.8,
    profit_factor: 1.55,
    avg_hold_sessions: 18.3,
    exposure_pct: 63.2,
    sharpe: 0.97,
    in_sample_cagr_pct: 12.6,
    in_sample_dd_pct: -27.9,
    out_of_sample_cagr_pct: 26.0,
    out_of_sample_dd_pct: -19.8,
    modelled_fill_rate_pct: 83.8,
  },
  runs: [],
};

describe("the Today page answers the gate before it lists anything", () => {
  it("shows the gate OPEN with the number that decided it", async () => {
    vi.mocked(fetchToday).mockResolvedValue(today());
    vi.mocked(fetchBreadth).mockResolvedValue(NO_BREADTH);
    vi.mocked(fetchBars).mockResolvedValue(null);

    render(await VbtTodayPage());

    expect(screen.getByTestId("gate-badge")).toHaveTextContent("OPEN");
    expect(
      screen.getByText(/new limits may be placed/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/62\.4% of 1,412 names/)).toBeInTheDocument();
    expect(screen.getByText(/the gate opens above 40%/)).toBeInTheDocument();
  });

  it("shows the gate SHUT and says the book is still managed", async () => {
    vi.mocked(fetchToday).mockResolvedValue(
      today({ gate: "SHUT", pct_above_dma: 31.2, candidates: [] }),
    );
    vi.mocked(fetchBreadth).mockResolvedValue(NO_BREADTH);
    vi.mocked(fetchBars).mockResolvedValue(null);

    render(await VbtTodayPage());

    expect(screen.getByTestId("gate-badge")).toHaveTextContent("SHUT");
    expect(screen.getByText(/no new limits are placed/i)).toBeInTheDocument();
    expect(
      screen.getByText(/stops stay, exits still fire/i),
    ).toBeInTheDocument();
  });

  it("renders the funnel line even with zero candidates", async () => {
    vi.mocked(fetchToday).mockResolvedValue(today({ candidates: [] }));
    vi.mocked(fetchBreadth).mockResolvedValue(NO_BREADTH);
    vi.mocked(fetchBars).mockResolvedValue(null);

    render(await VbtTodayPage());

    const funnel = screen.getByTestId("funnel-line");
    expect(funnel).toHaveTextContent("4,186 names");
    expect(funnel).toHaveTextContent("37 met the volume scan");
    expect(funnel).toHaveTextContent("4 are signals");
    expect(funnel).toHaveTextContent("shut on 3 of the last 60 sessions");
  });

  it("tells a quiet session apart from a detector that never ran", async () => {
    vi.mocked(fetchToday).mockResolvedValue(null);
    vi.mocked(fetchBreadth).mockResolvedValue(null);
    vi.mocked(fetchBars).mockResolvedValue(null);

    render(await VbtTodayPage());

    expect(screen.getByText(/No detection has run yet/i)).toBeInTheDocument();
    expect(screen.queryByTestId("gate-badge")).not.toBeInTheDocument();
  });

  it("lists what the filters rejected, with the letters that failed", async () => {
    vi.mocked(fetchToday).mockResolvedValue(
      today({
        candidates: [],
        rejects: [
          candidate({
            state: "SCAN_ONLY",
            symbol: "AHCL",
            failed_filters: ["B", "F"],
          }),
        ],
      }),
    );
    vi.mocked(fetchBreadth).mockResolvedValue(NO_BREADTH);
    vi.mocked(fetchBars).mockResolvedValue(null);

    render(await VbtTodayPage());

    expect(screen.getByTestId("rejects")).toHaveTextContent("AHCL — B, F");
  });

  it("flags a name locked at its upper circuit", async () => {
    vi.mocked(fetchToday).mockResolvedValue(
      today({ candidates: [candidate({ locked_upper_circuit: true })] }),
    );
    vi.mocked(fetchBreadth).mockResolvedValue(NO_BREADTH);
    vi.mocked(fetchBars).mockResolvedValue(null);

    render(await VbtTodayPage());

    expect(screen.getByTestId("locked-warning")).toBeInTheDocument();
  });
});

describe("the Book page", () => {
  it("says a limit cancels tonight when it is in its third session", async () => {
    vi.mocked(fetchBook).mockResolvedValue(
      book({
        working: [
          {
            id: 1,
            instrument_id: 1,
            symbol: "VBTCO",
            name: "VBTCO LIMITED",
            limit_price: 149.6,
            stop_price: 131.65,
            quantity: 100,
            value_inr: 14960,
            state: "SENT",
            signal_date: "2026-09-03",
            working_from: "2026-09-04",
            expires_after_session: "2026-09-08",
            sessions_worked: 3,
            sessions_allowed: 3,
            expires_tonight: true,
            broker_order_id: null,
            filled_quantity: 0,
            simulated: true,
          },
        ],
      }),
    );

    render(await VbtBookPage());

    expect(screen.getByText("3 of 3 · cancels tonight")).toBeInTheDocument();
    expect(screen.getByTestId("simulated-tag")).toBeInTheDocument();
  });

  it("renders the fill-rate line before anything has resolved, without claiming 0%", async () => {
    vi.mocked(fetchBook).mockResolvedValue(book());

    render(await VbtBookPage());

    const line = screen.getByTestId("fill-rate-line");
    expect(line).toHaveTextContent("No limit has resolved yet");
    expect(line).toHaveTextContent("83.8%");
    expect(line).not.toHaveTextContent("0.0%");
  });

  it("renders this book's rate beside the study's once orders have resolved", async () => {
    vi.mocked(fetchBook).mockResolvedValue(
      book({
        fill_rate: {
          filled: 14,
          resolved: 17,
          rate_pct: 82.4,
          modelled_pct: 83.8,
        },
      }),
    );

    render(await VbtBookPage());

    const line = screen.getByTestId("fill-rate-line");
    expect(line).toHaveTextContent("14");
    expect(line).toHaveTextContent("17");
    expect(line).toHaveTextContent("82.4%");
    expect(line).toHaveTextContent("83.8%");
  });

  it("marks a position with no resting stop in red, and counts it in the answer", async () => {
    vi.mocked(fetchBook).mockResolvedValue(
      book({
        open_positions: [
          {
            id: 3,
            instrument_id: 1,
            symbol: "VBTCO",
            name: "VBTCO LIMITED",
            entry_date: "2026-09-01",
            entry_avg: 100,
            quantity_open: 100,
            initial_stop: 88,
            stop_price: 88,
            gtt_id: null,
            naked: true,
            last_close: 110,
            ema_21: 104,
            distance_to_ema_pct: 5.4,
            return_pct: 10,
            r_multiple: 0.83,
            sessions_held: 5,
            exit_queued_for: null,
            exit_reason_queued: null,
            simulated: true,
          },
        ],
      }),
    );

    render(await VbtBookPage());

    expect(screen.getByTestId("naked-cell")).toHaveTextContent("naked");
    expect(screen.getByTestId("naked-warning")).toHaveTextContent(
      "1 without a resting stop",
    );
  });
});

describe("the Backtest page puts the caveats above the numbers", () => {
  it("renders the caveats component before any result in document order", async () => {
    vi.mocked(fetchBacktest).mockResolvedValue(BACKTEST);

    const { container } = render(await VbtBacktestPage());

    const caveats = screen.getByTestId("vbt-caveats");
    const headline = screen.getByText(/18\.2% a year/);
    // House rule 9: a component, above the numbers, never a footer. Placement is the claim, so
    // presence alone is not enough — the caveats must come first in the document.
    expect(
      caveats.compareDocumentPosition(headline) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(container.textContent).toContain("not a true walk-forward");
  });

  it("shows the two halves rather than only their average", async () => {
    vi.mocked(fetchBacktest).mockResolvedValue(BACKTEST);

    render(await VbtBacktestPage());

    expect(
      screen.getByText(/12\.6% a year, worst drawdown -?27\.9%/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/26\.0% a year, worst drawdown -?19\.8%/),
    ).toBeInTheDocument();
  });

  it("says so plainly when this box has not re-run the backtest", async () => {
    vi.mocked(fetchBacktest).mockResolvedValue(BACKTEST);

    render(await VbtBacktestPage());

    expect(screen.getByTestId("no-runs")).toBeInTheDocument();
    expect(screen.queryByTestId("drift-banner")).not.toBeInTheDocument();
  });

  it("raises the drift banner when a run is more than a CAGR point out", async () => {
    vi.mocked(fetchBacktest).mockResolvedValue({
      ...BACKTEST,
      runs: [
        {
          id: 9,
          source: "PLANT",
          started_at: "2026-09-09T15:00:00Z",
          finished_at: "2026-09-09T15:20:00Z",
          params: {},
          stats: {
            full: { cagr_pct: 14.8, max_drawdown_pct: -31.2, trades: 742 },
            gate_off: { cagr_pct: 15.1, max_drawdown_pct: -48.8, trades: 980 },
            raw_scan: { cagr_pct: 0.8, max_drawdown_pct: -47.9, trades: 3100 },
          },
          drift: { flagged: true, cagr_pct_delta: -3.4, threshold_cagr_points: 1 },
          error: null,
        },
      ],
    });

    render(await VbtBacktestPage());

    const banner = screen.getByTestId("drift-banner");
    expect(banner).toHaveTextContent(
      "3.4 points away from the published number",
    );
    expect(banner).toHaveTextContent(
      "Do not use the published number until this is explained",
    );
  });

  it("draws the equity curve and the yearly table when the run carries them", async () => {
    vi.mocked(fetchBacktest).mockResolvedValue({
      ...BACKTEST,
      runs: [
        {
          id: 11,
          source: "PLANT",
          started_at: "2026-09-09T15:00:00Z",
          finished_at: "2026-09-09T15:20:00Z",
          params: {},
          stats: {
            full: {
              cagr_pct: 18.2,
              max_drawdown_pct: -27.9,
              trades: 761,
              // House rule 9: money reaches the page as a string of its exact decimal.
              equity_curve: [
                { date: "2024-01-01", equity_inr: "1000000.00" },
                { date: "2024-01-02", equity_inr: "1100000.00" },
                { date: "2024-01-03", equity_inr: "900000.00" },
              ],
              yearly: [
                { year: 2024, return_pct: 31.4, trades: 92, win_rate_pct: 38.0 },
                { year: 2025, return_pct: -8.1, trades: 77, win_rate_pct: 31.2 },
              ],
            },
          },
          drift: { flagged: false, cagr_pct_delta: -0.03 },
          error: null,
        },
      ],
    });

    render(await VbtBacktestPage());

    expect(screen.getByTestId("equity-curve")).toBeInTheDocument();
    // The deepest fall below the running peak is 1.1m -> 0.9m, or -18.2%.
    expect(screen.getByTestId("equity-curve-trough")).toBeInTheDocument();
    const table = screen.getByTestId("yearly-table");
    expect(table).toHaveTextContent("2024");
    expect(table).toHaveTextContent("31.4%");
    expect(table).toHaveTextContent("-8.1%");
    expect(screen.queryByTestId("drift-banner")).not.toBeInTheDocument();
  });
});
