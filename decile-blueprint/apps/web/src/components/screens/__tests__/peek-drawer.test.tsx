import type { RankExplanationOut, ScreenDefinition } from "@baskfy/api-client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PeekDrawer } from "@/components/screens/peek-drawer";
import type { ColumnMeta, ResultRow } from "@/components/screens/result-columns";
import { formatFraction } from "@/lib/format";
import { defaultDefinition } from "@/lib/screens/defaults";

/**
 * Tree 1.3.2: the peek is a mini factsheet of the row already on screen — not a second
 * fetch of the instrument page, and not a 1-year chart (preview rows have no history series).
 */

const api = vi.hoisted(() => ({ POST: vi.fn(), GET: vi.fn() }));
vi.mock("@/lib/api/browser", () => ({ browserApi: () => api }));

afterEach(cleanup);
beforeEach(() => {
  api.POST.mockReset();
  api.GET.mockReset();
});

/** CUPID on the seeded 2026-08-18 screen: top rank, reference-export return and vol. */
const CUPID: ResultRow = {
  rank: 1,
  symbol: "CUPID",
  name: "Cupid Limited",
  sorting_factor: 5.13,
  ret_12m: 726.63,
  vol_12m: 0.5793,
  close_raw: 284.03,
};

const COLUMNS = ["symbol", "name", "sorting_factor", "ret_12m", "vol_12m", "close_raw"] as const;

const META = new Map<string, ColumnMeta>(
  (
    [
      ["sorting_factor", "ratio"],
      ["ret_12m", "percent"],
      ["vol_12m", "fraction"],
      ["close_raw", "price"],
    ] as const
  ).map(([key, unit]) => [key, { key, label: key, unit }]),
);

function draw(row: ResultRow | null, onClose = vi.fn()) {
  return {
    onClose,
    ...render(
      <PeekDrawer
        row={row}
        columns={COLUMNS}
        meta={META}
        sortingFactorLabel="Avg. Sharpe 12/6/3/1"
        onClose={onClose}
      />,
    ),
  };
}

function peek() {
  return screen.queryByTestId("peek-drawer");
}

function isShown(node: HTMLElement | null): boolean {
  return node !== null && node.getAttribute("data-state") !== "closed";
}

describe("PeekDrawer", () => {
  it("opens on a CUPID row as a mini factsheet: symbol, encodings, All numbers, factsheet link", () => {
    draw(CUPID);

    expect(screen.getByTestId("peek-drawer")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "CUPID" })).toBeInTheDocument();
    expect(screen.getByTestId("peek-all-numbers")).toHaveTextContent("All numbers");
    expect(screen.getByTestId("score-bar-fill")).toBeInTheDocument();

    const factsheet = screen.getByRole("link", { name: /Open the full factsheet/i });
    expect(factsheet).toHaveAttribute("href", "/instruments/CUPID");
  });

  it("is closed when there is no row", () => {
    draw(null);

    const drawer = peek();
    expect(isShown(drawer)).toBe(false);
  });

  it("slides in 200ms when motion is allowed", () => {
    draw(CUPID);

    expect(screen.getByTestId("peek-drawer").className).toMatch(/motion-safe:duration-200/);
  });

  it("puts the bumpiness percent on a native title, so the dots are not the only reading", () => {
    draw(CUPID);

    expect(screen.getAllByTitle(formatFraction(CUPID.vol_12m as number)).length).toBeGreaterThan(0);
  });

  it("does not render a 1-year chart — preview rows carry no history series", () => {
    draw(CUPID);

    expect(screen.queryByRole("img", { name: /chart|sparkline|1.?year/i })).not.toBeInTheDocument();
    expect(document.querySelector("canvas,svg[aria-label*='year' i]")).toBeNull();
  });

  it("closes through the existing Dialog: Escape calls onClose", async () => {
    const user = userEvent.setup();
    const { onClose } = draw(CUPID);

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("renders desk A–F breakdown when explain columns are present", () => {
    const deskRow: ResultRow = {
      ...CUPID,
      sorting_factor: 72.5,
      desk_a_trend: 18,
      desk_b_momentum: 20,
      desk_c_sharpe: 15,
      desk_d_consistency: 12,
      desk_e_liquidity: 8,
      desk_f_penalty: 0,
      desk_reject: "",
      desk_eligible: true,
    };
    const deskColumns = [
      ...COLUMNS,
      "desk_a_trend",
      "desk_b_momentum",
      "desk_c_sharpe",
      "desk_d_consistency",
      "desk_e_liquidity",
      "desk_f_penalty",
      "desk_reject",
      "desk_eligible",
    ];
    render(
      <PeekDrawer
        row={deskRow}
        columns={deskColumns}
        meta={META}
        sortingFactorLabel="Desk Momentum Quality Score"
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByTestId("desk-score-breakdown")).toBeInTheDocument();
    expect(screen.getByTestId("desk_a_trend")).toHaveTextContent("18.0");
    expect(screen.getByTestId("desk-eligible")).toHaveTextContent("Eligible");
  });
});

/**
 * gates/ranking-2.H-web.md G4: a screen with ``ranking_terms`` explains the peeked row from
 * ``POST /screens/explain``; every other screen keeps the legacy peek and makes no explain call.
 */
describe("PeekDrawer explanation (ranked screens only)", () => {
  const RANKED: ScreenDefinition = {
    ...defaultDefinition(),
    ranking_mode: "composite",
    ranking_terms: [
      { factor: "ret_6m", preference: "higher", weight: 1, target_min: null, target_max: null },
    ],
  };

  const EXPLANATION: RankExplanationOut = {
    as_of: "2026-09-11",
    data_version: 42,
    instrument_id: 101,
    symbol: "CUPID",
    rank: 1,
    total: 91.2,
    terms: [
      {
        factor: "ret_6m",
        label: "6-month return",
        weight_family: "momentum",
        preference: "higher",
        raw: 0.61,
        transformed: 0.912,
        effective_weight: 1,
        contribution: 91.2,
        missing: false,
      },
    ],
    positives: [],
    deductions: [],
    eligibility: { passed: true, failures: [] },
    data_quality: {
      missing_factors: [],
      insufficient_history: false,
      stale_price: false,
      recent_corporate_action: false,
    },
    desk: {
      score: 80.5,
      rank: 2,
      a_trend: 18,
      b_momentum: 20,
      c_sharpe: 15,
      d_consistency: 12,
      e_liquidity: 8,
      f_penalty: 0,
      reject: "",
      eligible: true,
      ext_over_20dma: 6.1,
      score_version: "desk-1.2",
      components: [],
    },
    provenance: {
      universe: "nifty-500",
      as_of: "2026-09-11",
      data_version: 42,
      desk_score_version: "desk-1.2",
      mode: "composite",
      nse_momentum_version: "nse-mom-1",
      ranking_engine_version: "rank-2.0",
      scope: "fixed_universe",
    },
    rank_history: { today: 1, previous: null, previous_as_of: "2026-09-10", change: null },
  };

  const DESK_COLUMNS = [...COLUMNS, "desk_a_trend", "desk_eligible"];

  function drawWith(definition: ScreenDefinition, screenPublicId?: string) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={client}>
        <PeekDrawer
          row={{ ...CUPID, desk_a_trend: 18, desk_eligible: true }}
          columns={DESK_COLUMNS}
          meta={META}
          sortingFactorLabel="Composite score"
          onClose={vi.fn()}
          definition={definition}
          asOf="2026-09-11"
          dataVersion={42}
          screenPublicId={screenPublicId}
        />
      </QueryClientProvider>,
    );
  }

  it("calls /screens/explain for the peeked symbol and shows the explanation", async () => {
    api.POST.mockResolvedValue({ data: EXPLANATION });
    api.GET.mockResolvedValue({
      data: { data: [], screen_name: "Mine", screen_public_id: "scr_1", symbol: "CUPID" },
    });
    drawWith(RANKED, "scr_1");

    expect(await screen.findByTestId("rank-explanation")).toBeInTheDocument();
    expect(api.POST).toHaveBeenCalledTimes(1);
    expect(api.POST).toHaveBeenCalledWith("/api/v1/screens/explain", {
      body: { definition: RANKED, symbol: "CUPID", as_of: "2026-09-11", data_version: 42 },
    });
    expect(screen.getByTestId("explain-total-value")).toHaveTextContent("91.2");
    expect(screen.getByTestId("explain-desk")).toBeInTheDocument();
    // One desk breakdown — the explanation's stored block — not a second one from the row.
    expect(screen.getAllByTestId("desk-score-breakdown")).toHaveLength(1);
    expect(screen.queryByTestId("rank-history")).not.toBeInTheDocument();
    expect(
      await screen.findByText(/No earlier saved run to compare with\./),
    ).toBeInTheDocument();
  });

  it("keeps the legacy peek, with no explain call, when the screen has no ranking terms", () => {
    drawWith(defaultDefinition());

    expect(api.POST).not.toHaveBeenCalled();
    expect(screen.queryByTestId("rank-explanation")).not.toBeInTheDocument();
    expect(screen.queryByTestId("rank-explanation-loading")).not.toBeInTheDocument();
    expect(screen.getByTestId("desk-score-breakdown")).toBeInTheDocument();
    expect(screen.getByTestId("peek-all-numbers")).toBeInTheDocument();
  });
});
