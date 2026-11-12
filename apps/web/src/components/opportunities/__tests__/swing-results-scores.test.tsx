import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SwingResultsTable } from "@/components/opportunities/swing-results";
import type { SwingOpportunity, SwingScanResponse } from "@/lib/opportunities/types";

// Maulik, 12 Nov 2026: every Model/Swing cell on Opportunities read "·". The page fired one
// live-marks request per row, the burst drew 429s, the lookup was 429'd too, and nothing retried.
vi.mock("@/lib/api/browser", () => ({ accessToken: () => Promise.resolve("tok") }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

const SYMBOLS = ["SBC", "SSWL", "RATNAVEER", "ATHERENERG", "ONWARDTEC"];

function row(symbol: string, i: number): SwingOpportunity {
  return {
    id: `r${i}`,
    instrument_id: i + 1,
    symbol,
    name: symbol,
    exchange: "NSE",
    sector: null,
    setup: "BREAKOUT",
    status: "PENDING",
    actionable: true,
    close: 100,
    evidence: [],
    trigger: { condition: "", level: 101, status: "PENDING", triggered_on: null },
    entry: { basis: "", price: 101 },
    invalidation: 95,
    targets: [],
    risk_per_share: 6,
    reward_per_share: 12,
    risk_reward: 2,
    horizon: { min_sessions: 1, max_sessions: 20 },
    liquidity: { avg_turnover_cr_20d: 10, avg_volume_20d: 1000 },
    volatility: { atr_14: 2, atr_pct: 2, hv_20_pct: null },
    relative_strength: { benchmark: "NIFTY", excess_return_pct: 1, lookback: 63 },
    score: { total: 80 - i, components: [] },
    data_as_of: "2026-11-11",
    scanned_at: "2026-11-12T10:00:00+05:30",
    limitations: [],
    series: symbol === "ONWARDTEC" ? "BE" : "EQ",
  } as unknown as SwingOpportunity;
}

const response = {
  scan_id: "s",
  scanned_at: "2026-11-12T10:00:00+05:30",
  data_as_of: "2026-11-11",
  data_status: "FRESH",
  stale_reason: null,
  universe: "nifty-500",
  counts: { universe: 5, evaluated: 5, insufficient_history: 0, qualified: 5, pending: 5, triggered: 0 },
  warnings: [],
  opportunities: SYMBOLS.map(row),
} as unknown as SwingScanResponse;

const lookupBody = {
  as_of: "2026-11-11",
  universe_n: 1221,
  names: SYMBOLS.map((symbol, i) => ({
    symbol,
    rank: i + 1,
    score: "0.5",
    investable: true,
    hi63_trigger: false,
    hi63_trigger_date: null,
    scope: "universe",
  })),
  swing_as_of: "2026-11-11",
  swing_universe_n: 3107,
  swing_switch_on: true,
  swing: SYMBOLS.map((symbol, i) => ({
    symbol,
    rank: i + 1,
    score: "0.25",
    investable: true,
    picked: false,
    scope: "universe",
  })),
};

type Reply = { status: number; body?: unknown; headers?: Record<string, string> };
let lookupReplies: Reply[] = [];
const fetchMock = vi.fn((input: URL | string) => {
  const url = new URL(String(input));
  if (url.pathname === "/api/v1/mb/lookup") {
    const r = lookupReplies.shift() ?? { status: 200, body: lookupBody };
    return Promise.resolve(
      new Response(r.body === undefined ? "" : JSON.stringify(r.body), {
        status: r.status,
        headers: r.headers ?? {},
      }),
    );
  }
  return Promise.resolve(
    new Response(JSON.stringify({ live: false, reason: "market_closed", marks: {} }), { status: 200 }),
  );
});

function calls(path: string) {
  return fetchMock.mock.calls.filter(([u]) => new URL(String(u)).pathname === path);
}

function renderTable() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SwingResultsTable response={response} horizonMax={20} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  lookupReplies = [];
  fetchMock.mockClear();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("Opportunities swing table: Model and Swing columns", () => {
  it("renders both models' scores for every row", async () => {
    renderTable();
    await waitFor(() => expect(screen.getAllByTestId("mb-score-cell")).toHaveLength(SYMBOLS.length));
    expect(screen.getAllByTestId("swing-score-cell")).toHaveLength(SYMBOLS.length);
    expect(screen.getAllByText("0.500")).toHaveLength(SYMBOLS.length);
    expect(screen.getAllByText("0.250")).toHaveLength(SYMBOLS.length);
  });

  it("asks live-marks once for the whole table, not once per row", async () => {
    renderTable();
    await waitFor(() => expect(calls("/api/v1/meta/live-marks").length).toBeGreaterThan(0));
    const live = calls("/api/v1/meta/live-marks");
    expect(live).toHaveLength(1);
    const asked = new URL(String(live[0]![0])).searchParams.get("symbols")!.split(",");
    expect(asked.sort()).toEqual([...SYMBOLS].sort());
    expect(calls("/api/v1/mb/lookup")).toHaveLength(1);
  });

  it("retries a 429'd lookup and then shows the scores", async () => {
    lookupReplies = [{ status: 429, headers: { "Retry-After": "0" } }];
    renderTable();
    await waitFor(() => expect(screen.getAllByText("0.500")).toHaveLength(SYMBOLS.length), {
      timeout: 4000,
    });
    expect(calls("/api/v1/mb/lookup")).toHaveLength(2);
  });

  it("reads 'unavailable' after every retry fails, never an endless dot", async () => {
    lookupReplies = [{ status: 429 }, { status: 429 }, { status: 503 }];
    renderTable();
    await waitFor(
      () => expect(screen.getAllByTestId("mb-score-cell-unavailable")).toHaveLength(SYMBOLS.length),
      { timeout: 8000 },
    );
    expect(screen.getAllByTestId("swing-score-cell-unavailable")).toHaveLength(SYMBOLS.length);
    expect(screen.getAllByTestId("mb-score-cell-unavailable")[0]).toHaveAttribute(
      "title",
      expect.stringContaining("HTTP 503"),
    );
  }, 10000);
});
