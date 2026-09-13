import { ScreenDefinitionSchema, type ScreenDefinition } from "@baskfy/api-client";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { FactorRangesFilter, RegimeFilter } from "@/components/screens/factor-ranges";
import type { SectionProps } from "@/components/screens/filter-sections";
import { defaultDefinition } from "@/lib/screens/defaults";
import { FILTER_GROUPS } from "@/lib/screens/groups";
import type { RankingFactorMeta } from "@/lib/screens/ranking";

/**
 * gates/ranking-2.H-web.md G2 — factor ranges (with "beat NIFTY 500 by ≥ X pp") and the regime
 * filter, against docs/ranking/PLAN.md C3. As in ranking-section.test.tsx, the harness accepts
 * only patches the schema accepts and each test asserts nothing was refused.
 */

afterEach(cleanup);

beforeEach(() => {
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
    key: "max_dd_12m",
    label: "MAX DRAWDOWN 1 YEAR",
    family: "path_quality",
    unit: "percent",
    higher_is_better: false,
    preference: "lower",
    rankable: true,
    weight_family: "path_quality",
    validation_status: "research",
    definition: "Deepest percent fall of the close from its running high inside the 1-year window.",
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
];

type Section = (props: SectionProps & { factors: readonly RankingFactorMeta[] }) => ReactElement;

function setup(Component: Section, initial: Partial<ScreenDefinition> = {}) {
  const rejected: string[] = [];
  const state: { current: ScreenDefinition } = {
    current: { ...defaultDefinition(), ...initial },
  };
  function Harness() {
    const [definition, setDefinition] = useState(state.current);
    return (
      <Component
        definition={definition}
        factors={FACTORS}
        patch={(partial) => {
          const parsed = ScreenDefinitionSchema.safeParse({ ...definition, ...partial });
          if (!parsed.success) {
            rejected.push(parsed.error.issues[0]?.message ?? "invalid");
            return;
          }
          state.current = parsed.data;
          setDefinition(parsed.data);
        }}
      />
    );
  }
  const user = userEvent.setup();
  render(<Harness />);
  return { user, rejected, state };
}

describe("beat NIFTY 500", () => {
  it("writes an excess_ret minimum in percentage points, and blank removes it", async () => {
    const { user, rejected, state } = setup(FactorRangesFilter);
    const beat = screen.getByTestId("beat-benchmark");
    expect(within(beat).getByLabelText("Over")).toHaveValue("excess_ret_12m");

    await user.type(within(beat).getByLabelText("By at least (pp)"), "5");
    expect(state.current.factor_ranges).toEqual([
      { enabled: true, factor: "excess_ret_12m", min: 5, max: null },
    ]);

    await user.selectOptions(within(beat).getByLabelText("Over"), "excess_ret_6m");
    expect(state.current.factor_ranges).toEqual([
      { enabled: true, factor: "excess_ret_6m", min: 5, max: null },
    ]);

    await user.clear(within(beat).getByLabelText("By at least (pp)"));
    expect(state.current.factor_ranges).toEqual([]);
    expect(rejected).toEqual([]);
  });

  it("reads an existing beat range back and does not list it twice", () => {
    setup(FactorRangesFilter, {
      factor_ranges: [{ enabled: true, factor: "excess_ret_3m", min: 0, max: null }],
    });
    const beat = screen.getByTestId("beat-benchmark");
    expect(within(beat).getByLabelText("Over")).toHaveValue("excess_ret_3m");
    expect(within(beat).getByLabelText("By at least (pp)")).toHaveValue(0);
    expect(screen.queryByTestId("factor-range-1")).not.toBeInTheDocument();
  });
});

describe("factor ranges", () => {
  it("adds a range only once a bound is typed, and edits it in place", async () => {
    const { user, rejected, state } = setup(FactorRangesFilter);
    await user.click(screen.getByRole("button", { name: /Add a factor range \(0 of 10\)/ }));
    const draft = screen.getByTestId("factor-range-1");
    expect(draft).toHaveTextContent(/Not applied until you enter a minimum or a maximum/);
    expect(state.current.factor_ranges).toEqual([]);

    await user.type(within(draft).getByLabelText("Range 1 maximum"), "30");
    expect(state.current.factor_ranges).toEqual([
      { enabled: true, factor: "max_dd_12m", min: null, max: 30 },
    ]);

    const row = screen.getByTestId("factor-range-1");
    expect(row).toHaveTextContent(/In percent: 5 means 5%/);
    await user.click(within(row).getByRole("switch"));
    expect(state.current.factor_ranges[0]?.enabled).toBe(false);
    expect(rejected).toEqual([]);
  });

  it("offers non-rankable factors, because a range is where they belong", async () => {
    const { user } = setup(FactorRangesFilter);
    await user.click(screen.getByRole("button", { name: /Add a factor range/ }));
    const draft = screen.getByTestId("factor-range-1");
    await user.click(within(draft).getByRole("combobox", { name: "Factor" }));
    const labels = within(draft).getAllByRole("option").map((option) => option.textContent ?? "");
    expect(labels.some((label) => /EXCESS RETURN 1 YEAR/i.test(label))).toBe(true);
  });

  it("refuses an inverted range with a reason instead of dropping it", async () => {
    const { user, rejected, state } = setup(FactorRangesFilter, {
      factor_ranges: [{ enabled: true, factor: "max_dd_12m", min: 10, max: 30 }],
    });
    const row = screen.getByTestId("factor-range-1");
    const min = within(row).getByLabelText("Range 1 minimum");
    await user.clear(min);
    await user.type(min, "50");
    expect(screen.getByRole("alert")).toHaveTextContent(/minimum is above the maximum/);
    expect(state.current.factor_ranges[0]).toMatchObject({ min: 5, max: 30 });
    expect(rejected).toEqual([]);
  });

  it("counts enabled ranges on the group badge", () => {
    const group = FILTER_GROUPS.find((entry) => entry.id === "factor-ranges");
    expect(
      group?.activeCount({
        ...defaultDefinition(),
        factor_ranges: [
          { enabled: true, factor: "max_dd_12m", min: null, max: 30 },
          { enabled: false, factor: "excess_ret_12m", min: 0, max: null },
        ],
      }),
    ).toBe(1);
  });
});

describe("market regime", () => {
  it("is off by default and writes the switched-on labels, back to null when none", async () => {
    const { user, rejected, state } = setup(RegimeFilter);
    expect(state.current.regime_in).toBeNull();
    for (const label of ["Bull", "Neutral", "Bear"]) {
      expect(screen.getByRole("switch", { name: label })).not.toBeChecked();
    }

    await user.click(screen.getByRole("switch", { name: "Bull" }));
    await user.click(screen.getByRole("switch", { name: "Neutral" }));
    expect(state.current.regime_in).toEqual(["BULL", "NEUTRAL"]);

    await user.click(screen.getByRole("switch", { name: "Bull" }));
    await user.click(screen.getByRole("switch", { name: "Neutral" }));
    expect(state.current.regime_in).toBeNull();
    expect(rejected).toEqual([]);
    expect(screen.getByTestId("regime-filter")).toHaveTextContent(/Leave all off to ignore/);
  });
});
