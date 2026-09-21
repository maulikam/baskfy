import type { ScreenProvenanceOut, ScreenRunResponse } from "@baskfy/api-client";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProvenanceHeader } from "@/components/screens/provenance-header";
import { ResultsPanel } from "@/components/screens/results-panel";
import { TooltipProvider } from "@/components/ui/tooltip";
import { formatTradeDate } from "@/lib/format";
import type * as LiveMarks from "@/lib/screens/live-marks";

vi.mock("@/lib/screens/live-marks", async (importOriginal) => {
  const actual = await importOriginal<typeof LiveMarks>();
  return { ...actual, useLiveMarks: () => actual.EMPTY_LIVE_MARKS };
});

/**
 * gates/ranking-2.H-web.md G3 — docs/ranking/PLAN.md C6.
 *
 * The spec: the results header always shows universe, as-of, data version, the ranking engine and
 * desk score versions, scope and mode, and every one comes from the run response's `provenance`.
 * A definition without ranking terms reports `ranking_engine_version="legacy-sql"`, and
 * `desk_score_version` is null unless the desk score decides the order — both shown plainly,
 * never hidden.
 */

afterEach(cleanup);

const RANKED: ScreenProvenanceOut = {
  universe: "nifty-mid-small-400",
  universe_label: "NIFTY MidSmallcap 400",
  as_of: "2026-09-11",
  data_version: 57,
  ranking_engine_version: "rank-2.0",
  desk_score_version: "desk-1.2",
  scope: "within_sector",
  mode: "sequential",
};

const LEGACY: ScreenProvenanceOut = {
  universe: "nifty-500",
  universe_label: "NIFTY 500",
  as_of: "2026-09-10",
  data_version: 56,
  ranking_engine_version: "legacy-sql",
  desk_score_version: null,
  scope: "filtered_results",
  mode: "composite",
};

function item(key: string): HTMLElement {
  return within(screen.getByTestId("results-provenance")).getByTestId(`provenance-${key}`);
}

describe("ProvenanceHeader", () => {
  it("shows all seven items from provenance for a ranked run", () => {
    render(<ProvenanceHeader provenance={RANKED} />);

    expect(item("universe")).toHaveTextContent("NIFTY MidSmallcap 400");
    expect(item("as_of")).toHaveTextContent(formatTradeDate("2026-09-11"));
    expect(item("data_version")).toHaveTextContent("#57");
    expect(item("engine")).toHaveTextContent("rank-2.0");
    expect(item("desk_score")).toHaveTextContent("desk-1.2");
    expect(item("scope")).toHaveTextContent("Within sector");
    expect(item("mode")).toHaveTextContent("Sequential");
  });

  it("shows legacy-sql and an unused desk score plainly instead of hiding them", () => {
    render(<ProvenanceHeader provenance={LEGACY} />);

    expect(item("engine")).toHaveTextContent("legacy-sql");
    expect(item("engine")).toHaveAttribute("title", expect.stringMatching(/original SQL ranking/));
    expect(item("desk_score")).toHaveTextContent("Not used");
    expect(item("desk_score")).toHaveAttribute(
      "title",
      "The desk score does not decide this order.",
    );
    expect(item("scope")).toHaveTextContent("Filtered results");
    expect(item("mode")).toHaveTextContent("Composite");
  });

  it("falls back to the universe slug when the label is empty", () => {
    render(<ProvenanceHeader provenance={{ ...LEGACY, universe_label: "" }} />);
    expect(item("universe")).toHaveTextContent("nifty-500");
  });

  it("covers every scope and mode the schema allows", () => {
    for (const scope of ["fixed_universe", "filtered_results", "within_sector"] as const) {
      for (const mode of ["single", "sequential", "composite"] as const) {
        cleanup();
        render(<ProvenanceHeader provenance={{ ...RANKED, scope, mode }} />);
        expect(item("scope").textContent).not.toMatch(/undefined|_/);
        expect(item("mode").textContent).not.toMatch(/undefined/);
      }
    }
  });
});

function run(provenance: ScreenProvenanceOut, overrides: Partial<ScreenRunResponse> = {}) {
  return {
    as_of: "2026-09-12",
    columns: ["symbol", "name", "sorting_factor"],
    data_version: 99,
    result_count: 0,
    sorting_factor: { key: "sharpe_12m", label: "Sharpe 12M" },
    provenance,
    rows: [],
    ...overrides,
  } satisfies ScreenRunResponse;
}

function renderPanel(result: ScreenRunResponse | undefined, isPending = false) {
  return render(
    <TooltipProvider>
      <ResultsPanel
        result={result}
        columnMeta={new Map()}
        sortingFactorUnit="ratio"
        isPending={isPending}
        isFetching={false}
        error={undefined}
        onRetry={vi.fn()}
        onLoosenFilters={vi.fn()}
      />
    </TooltipProvider>,
  );
}

describe("ResultsPanel header", () => {
  it("reads provenance, not the response's top-level as_of and data_version", () => {
    renderPanel(run(RANKED));

    // The top-level fields say 2026-09-12 / 99; the header must show provenance's own values.
    expect(item("as_of")).toHaveTextContent(formatTradeDate("2026-09-11"));
    expect(item("data_version")).toHaveTextContent("#57");
    expect(item("universe")).toHaveTextContent("NIFTY MidSmallcap 400");
  });

  it("still shows the header when nothing survived the filters", () => {
    renderPanel(run(LEGACY));

    expect(screen.getByText("Nothing survived your filters")).toBeInTheDocument();
    expect(item("engine")).toHaveTextContent("legacy-sql");
    expect(item("desk_score")).toHaveTextContent("Not used");
  });

  it("has no header before the first result arrives", () => {
    renderPanel(undefined, true);
    expect(screen.queryByTestId("results-provenance")).toBeNull();
  });
});
