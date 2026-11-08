import { ScreenDefinitionSchema, type ScreenDefinition } from "@baskfy/api-client";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { RankingSection } from "@/components/screens/ranking-section";
import { defaultDefinition } from "@/lib/screens/defaults";
import type { RankingFactorMeta } from "@/lib/screens/ranking";

/**
 * gates/ranking-2.H-web.md G1 — the Ranking section, against docs/ranking/PLAN.md C3.
 *
 * The harness keeps the definition in state and, like the editor's URL state, only accepts a
 * patch the schema accepts. Every test then asserts `rejected` is empty: a control that emitted an
 * invalid definition would be silently dropped in the real editor, which is the failure this
 * section exists to prevent.
 */

afterEach(cleanup);

beforeEach(() => {
  // cmdk needs these; jsdom has neither (same stub as filter-chip-bar.test.tsx).
  if (typeof window.ResizeObserver === "undefined") {
    Object.defineProperty(window, "ResizeObserver", {
      writable: true,
      configurable: true,
      value: class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    });
  }
  if (typeof Element.prototype.scrollIntoView !== "function") {
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      writable: true,
      configurable: true,
      value: () => {},
    });
  }
});

const FACTORS: RankingFactorMeta[] = [
  {
    key: "avg_sharpe_12_6_3_1",
    label: "AVERAGE SHARPE RETURN 12 6 3 1 MONTHS",
    family: "sharpe_return",
    unit: "ratio",
    higher_is_better: true,
    preference: "higher",
    rankable: true,
    weight_family: "momentum",
    validation_status: "legacy",
    definition: "Mean of the 12 6 3 1-month sharpe returns, NULL if any is NULL (docs/05 §4).",
  },
  {
    key: "vol_12m",
    label: "VOLATILITY 1 YEAR",
    family: "non_momentum",
    unit: "fraction",
    higher_is_better: false,
    preference: "lower",
    rankable: true,
    weight_family: "risk_execution",
    validation_status: "legacy",
    definition: "Annualised population standard deviation of the 1-year window's daily returns, as a fraction (docs/05 §2).",
  },
  {
    key: "atr_ext_20",
    label: "ATR EXTENSION 20",
    family: "trend_structure",
    unit: "ratio",
    higher_is_better: false,
    preference: "target_range",
    rankable: true,
    weight_family: "trend_structure",
    validation_status: "research",
    definition: "How many 14-session ATRs the close sits above its 20-session moving average.",
  },
  {
    key: "excess_ret_12m",
    label: "EXCESS RETURN 1 YEAR VS NIFTY 500",
    family: "momentum_quality",
    unit: "percent",
    higher_is_better: true,
    preference: "eligibility",
    rankable: false,
    weight_family: "momentum",
    validation_status: "research",
    definition: "The 1-year return minus NIFTY 500's over the same window; a filter, not a rank key.",
  },
  {
    // The registry's spec value for atr_14 (FactorOut carries `rankable` since 2.G).
    key: "atr_14",
    label: "ATR 14",
    family: "trend_structure",
    unit: "price",
    higher_is_better: false,
    preference: "eligibility",
    rankable: false,
    weight_family: "risk_execution",
    validation_status: "research",
    definition: "Wilder's 14-session average true range of the adjusted bars, seeded by a simple mean.",
  },
];

function Harness({
  initial,
  rejected,
  onChange,
}: {
  initial: ScreenDefinition;
  rejected: string[];
  onChange?: (definition: ScreenDefinition) => void;
}) {
  const [definition, setDefinition] = useState(initial);
  return (
    <RankingSection
      definition={definition}
      factors={FACTORS}
      patch={(partial) => {
        const parsed = ScreenDefinitionSchema.safeParse({ ...definition, ...partial });
        if (!parsed.success) {
          rejected.push(parsed.error.issues[0]?.message ?? "invalid");
          return;
        }
        setDefinition(parsed.data);
        onChange?.(parsed.data);
      }}
    />
  );
}

function setup(initial: Partial<ScreenDefinition> = {}) {
  const rejected: string[] = [];
  const state: { current: ScreenDefinition } = {
    current: { ...defaultDefinition(), ...initial },
  };
  const user = userEvent.setup();
  render(
    <Harness
      initial={state.current}
      rejected={rejected}
      onChange={(definition) => {
        state.current = definition;
      }}
    />,
  );
  return { user, rejected, state };
}

describe("new screens", () => {
  it("default to the fixed universe, while a saved screen without a scope stays filtered", () => {
    expect(defaultDefinition().ranking_scope).toBe("fixed_universe");
    const legacy: Record<string, unknown> = { ...defaultDefinition() };
    delete legacy.ranking_scope;
    expect(ScreenDefinitionSchema.parse(legacy).ranking_scope).toBe("filtered_results");
  });

  it("show Fixed universe selected with its explanation", () => {
    setup();
    const scope = screen.getByLabelText("Scope");
    expect(scope).toHaveValue("fixed_universe");
    expect(scope).toHaveAccessibleDescription(/before your filters/);
  });
});

describe("mode", () => {
  it("offers Single, Sequential and Composite, and the copy states the difference", async () => {
    const { user, rejected, state } = setup();
    expect(screen.getByRole("radio", { name: "Composite" })).toBeChecked();
    expect(screen.getByTestId("ranking-mode-copy")).toHaveTextContent(/adds the scores by weight/);

    await user.click(screen.getByRole("radio", { name: "Sequential" }));
    expect(state.current.ranking_mode).toBe("sequential");
    expect(screen.getByTestId("ranking-mode-copy")).toHaveTextContent(/only breaks exact ties/);

    await user.click(screen.getByRole("radio", { name: "Single" }));
    expect(state.current.ranking_mode).toBe("single");
    expect(screen.getByTestId("ranking-mode-copy")).toHaveTextContent(/one factor/);
    expect(rejected).toEqual([]);
  });

  it("Single keeps only the first term and drops composite weights", async () => {
    const { user, rejected, state } = setup({
      sort_by: "avg_sharpe_12_6_3_1",
      ranking_terms: [
        { factor: "avg_sharpe_12_6_3_1", preference: "higher", weight: 2, target_min: null, target_max: null },
        { factor: "vol_12m", preference: "lower", weight: 1, target_min: null, target_max: null },
      ],
      family_weights: { momentum: 2, path_quality: null, trend_structure: null, participation: null, risk_execution: 1 },
    });
    await user.click(screen.getByRole("radio", { name: "Single" }));
    expect(rejected).toEqual([]);
    expect(state.current.ranking_terms.map((term) => term.factor)).toEqual(["avg_sharpe_12_6_3_1"]);
    expect(state.current.ranking_terms[0]?.weight).toBe(1);
    expect(state.current.family_weights).toBeNull();
    expect(screen.queryByRole("button", { name: /Add a ranking term/ })).not.toBeInTheDocument();
  });
});

describe("term editor", () => {
  it("adds a first term from Sort By, with the factor's own preference", async () => {
    const { user, rejected, state } = setup({ sort_by: "vol_12m", sort_direction: "asc" });
    await user.click(screen.getByRole("button", { name: /Add a ranking term/ }));
    expect(rejected).toEqual([]);
    expect(state.current.ranking_terms).toEqual([
      { factor: "vol_12m", preference: "lower", weight: 1, target_min: null, target_max: null },
    ]);
    const term = screen.getByTestId("ranking-term-1");
    expect(within(term).getByLabelText("Preference")).toHaveValue("lower");
  });

  it("offers only rankable factors, and a new factor brings its preference and becomes Sort By", async () => {
    const { user, rejected, state } = setup();
    await user.click(screen.getByRole("button", { name: /Add a ranking term/ }));
    const term = screen.getByTestId("ranking-term-1");

    await user.click(within(term).getByRole("combobox", { name: "Factor" }));
    const options = within(term).getAllByRole("option").map((option) => option.textContent ?? "");
    expect(options.some((label) => /VOLATILITY 1 YEAR/i.test(label))).toBe(true);
    expect(options.some((label) => /EXCESS RETURN/i.test(label))).toBe(false);
    expect(options.some((label) => /ATR 14/i.test(label))).toBe(false);

    const vol = within(term)
      .getAllByRole("option")
      .find((option) => /VOLATILITY 1 YEAR/i.test(option.textContent ?? ""));
    await user.click(vol!);
    expect(rejected).toEqual([]);
    expect(state.current.ranking_terms[0]?.factor).toBe("vol_12m");
    expect(state.current.ranking_terms[0]?.preference).toBe("lower");
    expect(state.current.sort_by).toBe("vol_12m");
  });

  it("a target range asks for bounds and applies once one is typed", async () => {
    const { user, rejected, state } = setup();
    await user.click(screen.getByRole("button", { name: /Add a ranking term/ }));
    const term = screen.getByTestId("ranking-term-1");

    await user.selectOptions(within(term).getByLabelText("Preference"), "target_range");
    // Not yet valid, so not yet applied.
    expect(state.current.ranking_terms[0]?.preference).toBe("higher");
    expect(term).toHaveTextContent(/Not applied until you enter a minimum or a maximum/);

    await user.type(within(term).getByLabelText("Term 1 target minimum"), "0");
    await user.type(within(term).getByLabelText("Term 1 target maximum"), "3");
    expect(rejected).toEqual([]);
    expect(state.current.ranking_terms[0]).toMatchObject({
      preference: "target_range",
      target_min: 0,
      target_max: 3,
    });
  });

  it("shows weight and family weights in Composite only", async () => {
    const { user, rejected, state } = setup();
    await user.click(screen.getByRole("button", { name: /Add a ranking term/ }));
    const term = screen.getByTestId("ranking-term-1");
    const weight = within(term).getByLabelText("Weight");
    await user.clear(weight);
    await user.type(weight, "3");
    expect(state.current.ranking_terms[0]?.weight).toBe(3);

    const families = screen.getByTestId("family-weights");
    await user.type(within(families).getByLabelText("Momentum"), "2");
    expect(state.current.family_weights).toEqual({
      momentum: 2,
      path_quality: null,
      trend_structure: null,
      participation: null,
      risk_execution: null,
    });

    await user.click(screen.getByRole("radio", { name: "Sequential" }));
    expect(rejected).toEqual([]);
    expect(within(screen.getByTestId("ranking-term-1")).queryByLabelText("Weight")).toBeNull();
    expect(screen.queryByTestId("family-weights")).not.toBeInTheDocument();
    expect(state.current.ranking_terms[0]?.weight).toBe(1);
    expect(state.current.family_weights).toBeNull();
  });

  it("refuses a duplicate factor with a plain reason instead of dropping it", async () => {
    const { user, rejected, state } = setup();
    await user.click(screen.getByRole("button", { name: /Add a ranking term/ }));
    await user.click(screen.getByRole("button", { name: /Add a ranking term/ }));
    expect(state.current.ranking_terms).toHaveLength(2);

    const second = screen.getByTestId("ranking-term-2");
    await user.click(within(second).getByRole("combobox", { name: "Factor" }));
    const first = within(second)
      .getAllByRole("option")
      .find((option) => /AVERAGE SHARPE/i.test(option.textContent ?? ""));
    await user.click(first!);

    expect(screen.getByRole("alert")).toHaveTextContent(/already a term/);
    expect(state.current.ranking_terms[1]?.factor).not.toBe("avg_sharpe_12_6_3_1");
    expect(rejected).toEqual([]);
  });

  it("removing the last term restores the defaults terms depend on", async () => {
    const { user, rejected, state } = setup();
    await user.click(screen.getByRole("button", { name: /Add a ranking term/ }));
    await user.selectOptions(screen.getByLabelText("Scope"), "within_sector");
    await user.selectOptions(screen.getByLabelText("Missing data"), "exclude");
    expect(state.current.ranking_scope).toBe("within_sector");

    await user.click(screen.getByRole("button", { name: "Remove term 1" }));
    expect(rejected).toEqual([]);
    expect(state.current.ranking_terms).toEqual([]);
    expect(state.current.missing_data).toBe("penalize");
    expect(state.current.ranking_scope).toBe("fixed_universe");
  });
});

describe("missing data and scope", () => {
  it("missing data waits for terms, then offers penalize, neutral and exclude", async () => {
    const { user, rejected, state } = setup();
    const select = screen.getByLabelText("Missing data");
    expect(select).toBeDisabled();

    await user.click(screen.getByRole("button", { name: /Add a ranking term/ }));
    expect(screen.getByLabelText("Missing data")).toBeEnabled();
    const labels = within(screen.getByLabelText("Missing data"))
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(labels).toEqual([
      "Score it as the worst (0)",
      "Score it as the middle (0.5)",
      "Leave the stock out of the results",
    ]);
    await user.selectOptions(screen.getByLabelText("Missing data"), "neutral");
    expect(state.current.missing_data).toBe("neutral");
    expect(rejected).toEqual([]);
  });

  it("offers the three scopes, explains the chosen one, and gates Within sector", async () => {
    const { user, rejected, state } = setup();
    const scope = screen.getByLabelText("Scope");
    const options = within(scope).getAllByRole("option");
    expect(options.map((option) => option.getAttribute("value"))).toEqual([
      "fixed_universe",
      "filtered_results",
      "within_sector",
    ]);
    const sector = options.find((option) => option.getAttribute("value") === "within_sector");
    expect(sector).toBeDisabled();

    await user.selectOptions(scope, "filtered_results");
    expect(state.current.ranking_scope).toBe("filtered_results");
    expect(screen.getByLabelText("Scope")).toHaveAccessibleDescription(
      /only against the stocks that pass your filters/,
    );

    await user.click(screen.getByRole("button", { name: /Add a ranking term/ }));
    const enabled = within(screen.getByLabelText("Scope"))
      .getAllByRole("option")
      .find((option) => option.getAttribute("value") === "within_sector");
    expect(enabled).toBeEnabled();
    await user.selectOptions(screen.getByLabelText("Scope"), "within_sector");
    expect(state.current.ranking_scope).toBe("within_sector");
    expect(screen.getByLabelText("Scope")).toHaveAccessibleDescription(/in its sector/);
    expect(rejected).toEqual([]);
  });
});
