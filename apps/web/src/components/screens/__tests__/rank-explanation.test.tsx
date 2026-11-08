import type { RankExplanationOut, RankPointOut, ScreenDefinition } from "@baskfy/api-client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  RankExplanation,
  RankExplanationView,
  deskComponentDetail,
  deskInputText,
  previousRank,
  rankChange,
  resolvePreviousRank,
} from "@/components/screens/rank-explanation";
import { formatTradeDate } from "@/lib/format";
import { defaultDefinition } from "@/lib/screens/defaults";

/**
 * gates/ranking-2.H-web.md G4 — the peek's explanation, against docs/ranking/PLAN.md C6.
 *
 * `POST /screens/explain` answers with core's `RankExplanation` plus `as_of`/`data_version`. The
 * spec: total and a per-term table (raw, score, weight, contribution); positives and deductions;
 * eligibility failures; data quality; the stored desk A–F block with the raw inputs behind each
 * grade; provenance; and rank today / previous / change. "Previous" is the response's
 * `rank_history` (the same definition on the session before); saved runs are only a fallback when
 * that is null, and it is empty, not guessed, when neither has one.
 */

const api = vi.hoisted(() => ({ POST: vi.fn(), GET: vi.fn() }));
vi.mock("@/lib/api/browser", () => ({ browserApi: () => api }));

afterEach(cleanup);
beforeEach(() => {
  api.POST.mockReset();
  api.GET.mockReset();
});

const PROVENANCE: RankExplanationOut["provenance"] = {
  universe: "nifty-500",
  as_of: "2026-09-11",
  data_version: 42,
  desk_score_version: "desk-1.2",
  mode: "composite",
  nse_momentum_version: "nse-mom-1",
  ranking_engine_version: "rank-2.0",
  scope: "fixed_universe",
};

const CLEAN_QUALITY: RankExplanationOut["data_quality"] = {
  missing_factors: [],
  insufficient_history: false,
  stale_price: false,
  recent_corporate_action: false,
};

/** A composite-ranked row that passes every filter. */
const PASSING: RankExplanationOut = {
  as_of: "2026-09-11",
  data_version: 42,
  instrument_id: 101,
  symbol: "CUPID",
  rank: 3,
  total: 72.4,
  terms: [
    {
      factor: "ret_6m",
      label: "6-month return",
      weight_family: "momentum",
      preference: "higher",
      raw: 0.4213,
      transformed: 0.91,
      effective_weight: 0.6,
      contribution: 54.6,
      missing: false,
    },
    {
      factor: "vol_12m",
      label: "12-month volatility",
      weight_family: "risk_execution",
      preference: "lower",
      raw: 0.3312,
      transformed: 0.445,
      effective_weight: 0.4,
      contribution: 17.8,
      missing: false,
    },
  ],
  positives: ["Top 20% on 6-month return within nifty-500"],
  deductions: [],
  eligibility: { passed: true, failures: [] },
  data_quality: CLEAN_QUALITY,
  desk: null,
  provenance: PROVENANCE,
  // The API's previous session disagrees with the saved runs (#5) on purpose: the API wins.
  rank_history: { today: 3, previous: 4, previous_as_of: "2026-09-10", change: 1 },
};

/** In the universe, out of the results: a filter removed it, so rank is null. */
const FAILING: RankExplanationOut = {
  ...PASSING,
  symbol: "IDEA",
  rank: null,
  positives: [],
  eligibility: {
    passed: false,
    failures: [
      { filter: "marketcap", detail: "Market cap below ₹1,000 Cr" },
      { filter: "price", detail: "Price below ₹20" },
    ],
  },
  rank_history: { today: null, previous: 2, previous_as_of: "2026-09-10", change: null },
};

const EXTENSION = {
  name: "ext_over_20dma",
  label: "Extension over the 20-day moving average, %",
  value: 19.4,
};

/** Ranked on the desk score, which rejected the name. */
const DESK_REJECTED: RankExplanationOut = {
  ...PASSING,
  symbol: "YESBANK",
  rank: null,
  total: null,
  deductions: ["Desk reject: below_200dma", "Desk reject: low_liquidity"],
  eligibility: {
    passed: false,
    failures: [{ filter: "desk_score", detail: "the desk rejected this name" }],
  },
  desk: {
    score: 38.5,
    rank: 412,
    a_trend: 4,
    b_momentum: 9.5,
    c_sharpe: 11,
    d_consistency: 8,
    e_liquidity: 6,
    f_penalty: -3,
    reject: "below_200dma;low_liquidity",
    eligible: false,
    ext_over_20dma: 19.4,
    score_version: "desk-1.2",
    components: [
      { grade: "A", key: "a_trend", label: "Trend", points: 4, min_points: 0, max_points: 25, inputs: [EXTENSION] },
      { grade: "B", key: "b_momentum", label: "Momentum", points: 9.5, min_points: 0, max_points: 25, inputs: [] },
      { grade: "C", key: "c_sharpe", label: "Sharpe", points: 11, min_points: 0, max_points: 20, inputs: [] },
      { grade: "D", key: "d_consistency", label: "Consistency", points: 8, min_points: 0, max_points: 10, inputs: [] },
      { grade: "E", key: "e_liquidity", label: "Liquidity", points: 6, min_points: 0, max_points: 10, inputs: [] },
      { grade: "F", key: "f_penalty", label: "Penalty", points: -3, min_points: -10, max_points: 0, inputs: [EXTENSION] },
    ],
  },
  rank_history: { today: null, previous: null, previous_as_of: "2026-09-10", change: null },
};

/** A term with no value: missing flags, a deduction and data-quality notes. */
const MISSING: RankExplanationOut = {
  ...PASSING,
  symbol: "NEWLIST",
  rank: 88,
  total: 27.3,
  terms: [
    PASSING.terms[0] as RankExplanationOut["terms"][number],
    {
      factor: "vol_12m",
      label: "12-month volatility",
      weight_family: "risk_execution",
      preference: "lower",
      raw: null,
      transformed: 0,
      effective_weight: 0.4,
      contribution: 0,
      missing: true,
    },
  ],
  positives: [],
  deductions: ["Missing 12-month volatility (missing_data=penalize)"],
  data_quality: {
    missing_factors: ["12-month volatility"],
    insufficient_history: true,
    stale_price: true,
    recent_corporate_action: true,
  },
  rank_history: { today: 88, previous: null, previous_as_of: null, change: null },
};

const HISTORY: RankPointOut[] = [
  { as_of: "2026-09-04", rank: 7, result_count: 50 },
  { as_of: "2026-09-10", rank: 5, result_count: 50 },
  // Same day as the explanation: the run being explained, never "previous".
  { as_of: "2026-09-11", rank: 3, result_count: 50 },
];

const RANKED_DEFINITION: ScreenDefinition = {
  ...defaultDefinition(),
  ranking_mode: "composite",
  ranking_terms: [
    { factor: "ret_6m", preference: "higher", weight: 3, target_min: null, target_max: null },
    { factor: "vol_12m", preference: "lower", weight: 2, target_min: null, target_max: null },
  ],
};

describe("RankExplanationView", () => {
  it("a passing row: total, the component table, positives, filters passed, provenance, rank change", () => {
    render(<RankExplanationView explanation={PASSING} history={HISTORY} />);

    expect(screen.getByTestId("explain-rank")).toHaveTextContent("#3");
    expect(screen.getByTestId("explain-total-value")).toHaveTextContent("72.4");

    const table = screen.getByTestId("explain-terms");
    for (const header of ["Raw value", "Score", "Weight", "Contribution"]) {
      expect(within(table).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    const ret = within(screen.getByTestId("explain-term-ret_6m")).getAllByRole("cell");
    expect(ret.map((cell) => cell.textContent)).toEqual(["0.4213", "0.91", "0.60", "54.6"]);
    expect(screen.getByTestId("explain-term-vol_12m")).toHaveTextContent("12-month volatility");

    expect(screen.getByTestId("explain-positives")).toHaveTextContent(
      "Top 20% on 6-month return within nifty-500",
    );
    expect(screen.queryByTestId("explain-deductions")).not.toBeInTheDocument();
    expect(screen.getByTestId("explain-eligibility")).toHaveTextContent("Passes every filter.");
    expect(screen.getByTestId("explain-data-quality")).toHaveTextContent("No data problems found.");
    expect(screen.queryByTestId("explain-desk")).not.toBeInTheDocument();

    const provenance = screen.getByTestId("explain-provenance");
    expect(provenance).toHaveTextContent("nifty-500");
    expect(provenance).toHaveTextContent(formatTradeDate("2026-09-11"));
    expect(provenance).toHaveTextContent("#42");
    expect(provenance).toHaveTextContent("Composite");
    expect(provenance).toHaveTextContent("Fixed universe");
    expect(provenance).toHaveTextContent("rank-2.0");
    expect(provenance).toHaveTextContent("desk-1.2");
    expect(provenance).toHaveTextContent("nse-mom-1");

    expect(screen.getByTestId("explain-rank-today")).toHaveTextContent("#3");
    // The API's previous session (#4), not the saved run on the same day (#5).
    expect(screen.getByTestId("explain-rank-previous")).toHaveTextContent("#4");
    expect(screen.getByTestId("explain-rank-change")).toHaveTextContent("Up 1 place");
    expect(screen.getByTestId("explain-rank-previous-note")).toHaveTextContent(
      `Previous is this screen's rank on the session before, ${formatTradeDate("2026-09-10")}.`,
    );
  });

  it("a row that failed a filter: not in the results, each failure named, no change shown", () => {
    render(<RankExplanationView explanation={FAILING} history={HISTORY} />);

    expect(screen.getByTestId("explain-rank")).toHaveTextContent("Not in the results");
    const eligibility = screen.getByTestId("explain-eligibility");
    expect(eligibility).toHaveTextContent("Left out of the results.");
    const failures = within(screen.getByTestId("explain-eligibility-failures")).getAllByRole(
      "listitem",
    );
    expect(failures.map((item) => item.textContent)).toEqual([
      "Market cap below ₹1,000 Cr",
      "Price below ₹20",
    ]);
    expect(screen.getByTestId("explain-rank-today")).toHaveTextContent("—");
    // Ranked on the session before, out today: previous is shown, but no change is claimed.
    expect(screen.getByTestId("explain-rank-previous")).toHaveTextContent("#2");
    expect(screen.getByTestId("explain-rank-change")).toHaveTextContent("—");
  });

  it("a desk-rejected row: the stored A–F parts, score, desk rank and each reject reason", () => {
    render(<RankExplanationView explanation={DESK_REJECTED} history={[]} />);

    const desk = screen.getByTestId("explain-desk");
    expect(within(desk).getByTestId("explain-desk-score")).toHaveTextContent("38.5 · desk rank #412");
    expect(within(desk).getByTestId("desk_a_trend")).toHaveTextContent("4.0");
    expect(within(desk).getByTestId("desk_b_momentum")).toHaveTextContent("9.5");
    expect(within(desk).getByTestId("desk_c_sharpe")).toHaveTextContent("11.0");
    expect(within(desk).getByTestId("desk_d_consistency")).toHaveTextContent("8.0");
    expect(within(desk).getByTestId("desk_e_liquidity")).toHaveTextContent("6.0");
    expect(within(desk).getByTestId("desk_f_penalty")).toHaveTextContent("-3.0");
    expect(within(desk).getByTestId("desk-eligible")).toHaveTextContent(
      "below_200dma, low_liquidity",
    );
    // The raw inputs under each grade: A and F read the stored 20-day extension; B–E store none.
    expect(within(desk).getByTestId("desk_a_trend-inputs")).toHaveTextContent(
      "Out of 25 · 19.4% above its 20-day average",
    );
    expect(within(desk).getByTestId("desk_b_momentum-inputs")).toHaveTextContent("Out of 25");
    expect(within(desk).getByTestId("desk_c_sharpe-inputs")).toHaveTextContent("Out of 20");
    expect(within(desk).getByTestId("desk_d_consistency-inputs")).toHaveTextContent("Out of 10");
    expect(within(desk).getByTestId("desk_e_liquidity-inputs")).toHaveTextContent("Out of 10");
    expect(within(desk).getByTestId("desk_f_penalty-inputs")).toHaveTextContent(
      "Down to -10 · 19.4% above its 20-day average",
    );

    expect(screen.getByTestId("explain-deductions")).toHaveTextContent("Desk reject: below_200dma");
    expect(screen.getByTestId("explain-eligibility-failures")).toHaveTextContent(
      "the desk rejected this name",
    );
    expect(screen.getByTestId("explain-total-value")).toHaveTextContent("—");
    expect(screen.getByTestId("explain-rank-previous")).toHaveTextContent("—");
    expect(screen.getByTestId("explain-rank-previous-note")).toHaveTextContent(
      `Not ranked in this screen on ${formatTradeDate("2026-09-10")}. No earlier saved run to compare with.`,
    );
  });

  it("a row with missing data: the term reads No data and every data-quality flag is spelled out", () => {
    render(<RankExplanationView explanation={MISSING} />);

    const vol = within(screen.getByTestId("explain-term-vol_12m")).getAllByRole("cell");
    expect(vol[0]).toHaveTextContent("No data");
    expect(screen.getByTestId("explain-deductions")).toHaveTextContent("Missing 12-month volatility");

    const notes = within(screen.getByTestId("explain-data-quality-notes")).getAllByRole("listitem");
    expect(notes.map((item) => item.textContent)).toEqual([
      "No value for 12-month volatility",
      "Too little price history for some measures",
      "The last price is out of date",
      "A recent split, bonus or similar event changed past prices",
    ]);

    // An unsaved screen has no runs: previous is empty and says why, rather than guessing.
    expect(screen.getByTestId("explain-rank-previous")).toHaveTextContent("—");
    expect(screen.getByTestId("explain-rank-change")).toHaveTextContent("—");
    expect(screen.getByTestId("explain-rank-previous-note")).toHaveTextContent(
      "Save this screen to keep a rank history.",
    );
  });
});

describe("rank history helpers", () => {
  it("previous is the last saved run strictly before the explained date", () => {
    expect(previousRank("2026-09-11", HISTORY)?.rank).toBe(5);
    expect(previousRank("2026-09-04", HISTORY)).toBeNull();
    expect(previousRank("2026-09-11", undefined)).toBeNull();
  });

  it("the API's previous-session rank wins; saved runs only fill in when it is null", () => {
    expect(resolvePreviousRank(PASSING, HISTORY)).toEqual({
      rank: 4,
      as_of: "2026-09-10",
      source: "session",
    });
    expect(resolvePreviousRank(DESK_REJECTED, HISTORY)).toEqual({
      rank: 5,
      as_of: "2026-09-10",
      source: "saved",
    });
    expect(resolvePreviousRank(MISSING, undefined)).toBeNull();
  });

  it("rank 1 is best: a smaller number is a move up", () => {
    expect(rankChange(3, 5)).toBe("Up 2 places");
    expect(rankChange(6, 5)).toBe("Down 1 place");
    expect(rankChange(5, 5)).toBe("No change");
    expect(rankChange(null, 5)).toBe("—");
    expect(rankChange(3, null)).toBe("—");
  });
});

describe("desk input helpers", () => {
  it("reads the 20-day extension in words, above, below or missing", () => {
    expect(deskInputText(EXTENSION)).toBe("19.4% above its 20-day average");
    expect(deskInputText({ ...EXTENSION, value: -3.25 })).toBe("3.3% below its 20-day average");
    expect(deskInputText({ ...EXTENSION, value: null })).toBe(
      "Distance from the 20-day average not recorded",
    );
  });

  it("a grade line is its range, then each stored input", () => {
    const [a, b] = DESK_REJECTED.desk?.components ?? [];
    expect(a && deskComponentDetail(a)).toBe("Out of 25 · 19.4% above its 20-day average");
    expect(b && deskComponentDetail(b)).toBe("Out of 25");
  });
});

describe("RankExplanation (fetching)", () => {
  function renderFetching(screenPublicId?: string) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={client}>
        <RankExplanation
          definition={RANKED_DEFINITION}
          symbol="CUPID"
          asOf="2026-09-11"
          dataVersion={42}
          screenPublicId={screenPublicId}
        />
      </QueryClientProvider>,
    );
  }

  it("posts {definition, symbol, as_of, data_version} to /screens/explain and renders the answer", async () => {
    api.POST.mockResolvedValue({ data: PASSING });
    api.GET.mockResolvedValue({
      data: { data: HISTORY, screen_name: "Mine", screen_public_id: "scr_1", symbol: "CUPID" },
    });
    renderFetching("scr_1");

    expect(await screen.findByTestId("rank-explanation")).toBeInTheDocument();
    expect(api.POST).toHaveBeenCalledWith("/api/v1/screens/explain", {
      body: {
        definition: RANKED_DEFINITION,
        symbol: "CUPID",
        as_of: "2026-09-11",
        data_version: 42,
      },
    });
    expect(await screen.findByText("Up 1 place")).toBeInTheDocument();
    // The API carried a previous rank, so the saved runs are never fetched.
    expect(api.GET).not.toHaveBeenCalled();
  });

  it("falls back to the saved runs only when the API has no previous rank", async () => {
    api.POST.mockResolvedValue({
      data: {
        ...PASSING,
        rank_history: { today: 3, previous: null, previous_as_of: "2026-09-10", change: null },
      },
    });
    api.GET.mockResolvedValue({
      data: { data: HISTORY, screen_name: "Mine", screen_public_id: "scr_1", symbol: "CUPID" },
    });
    renderFetching("scr_1");

    expect(await screen.findByText("Up 2 places")).toBeInTheDocument();
    expect(api.GET).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("explain-rank-previous")).toHaveTextContent("#5");
    expect(screen.getByTestId("explain-rank-previous-note")).toHaveTextContent(
      `Previous is the last saved run, on ${formatTradeDate("2026-09-10")}.`,
    );
  });

  it("shows the server's problem when the explanation fails", async () => {
    api.POST.mockResolvedValue({
      data: undefined,
      error: {
        type: "stale-data-version",
        title: "Data has been updated",
        status: 409,
        detail: "Run the screen again to see the latest ranks.",
      },
    });
    renderFetching();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Run the screen again to see the latest ranks.",
    );
    expect(api.GET).not.toHaveBeenCalled();
  });
});
