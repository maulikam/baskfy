import {
  ScreenDefinitionSchema,
  type FactorOut,
  type RankingPresetOut,
  type ScreenDefinition,
} from "@baskfy/api-client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FactorList } from "@/components/data/factor-combobox";
import { PresetsMenu, PresetsMenuView } from "@/components/screens/presets-menu";
import { defaultDefinition } from "@/lib/screens/defaults";
import { applyPreset, presetChange, validatePreset } from "@/lib/screens/presets";

/**
 * gates/ranking-2.H-web.md G5, parts 1 and 2 — docs/ranking/PLAN.md §1.5, C1, C6.
 *
 * The spec: a presets menu fed by `GET /meta/ranking-presets` with a status badge per preset;
 * applying one patches the current definition; a preset is validated as a `ScreenDefinition` patch
 * over the default definition, so one that is not a valid screen is never applied. And the factor
 * list shows each factor's registry `validation_status`.
 */

const api = vi.hoisted(() => ({ POST: vi.fn(), GET: vi.fn() }));
vi.mock("@/lib/api/browser", () => ({ browserApi: () => api, accessToken: () => undefined }));

afterEach(cleanup);
beforeEach(() => {
  api.GET.mockReset();
  api.POST.mockReset();
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

/** The shapes `baskfy_core.ranking_presets.PRESET_SPECS` serves. */
const DESK_QUALITY: RankingPresetOut = {
  key: "desk_quality",
  label: "Desk quality",
  description: "The desk's quality score, best first.",
  sort_by: "desk_score",
  status: "ready",
  patch: {
    sort_by: "desk_score",
    sort_direction: "desc",
    ranking_mode: "single",
    ranking_scope: "filtered_results",
    factor_two: { enabled: false, sort_by: null, sort_direction: "desc" },
    factor_three: { enabled: false, sort_by: null, sort_direction: "desc" },
  },
};

const PATH_QUALITY: RankingPresetOut = {
  key: "path_quality",
  label: "Path quality",
  description: "Positive days paired with drawdown, downside volatility and jump dependence.",
  sort_by: "pos_days_6m",
  status: "research",
  patch: {
    sort_by: "pos_days_6m",
    sort_direction: "desc",
    ranking_mode: "composite",
    ranking_scope: "filtered_results",
    ranking_terms: [
      { factor: "pos_days_6m", preference: "higher" },
      { factor: "max_dd_12m", preference: "lower" },
      { factor: "downside_vol_12m", preference: "lower" },
      { factor: "ret_ex_top3_12m", preference: "higher" },
    ],
    family_weights: null,
    missing_data: "penalize",
    factor_two: { enabled: false, sort_by: null, sort_direction: "desc" },
    factor_three: { enabled: false, sort_by: null, sort_direction: "desc" },
  },
};

/** PLAN C7 / correction 8: NSE's score, on NSE's universe, under C7's exact label. */
const NSE_MOMENTUM: RankingPresetOut = {
  key: "nse_momentum",
  label: "NIFTY200 Momentum 30 score (NSE methodology)",
  description: "NSE's NIFTY200 Momentum 30 score and eligibility on NIFTY 200.",
  sort_by: "nse_momentum_score",
  status: "research",
  patch: {
    sort_by: "nse_momentum_score",
    sort_direction: "desc",
    ranking_mode: "single",
    ranking_scope: "filtered_results",
    ranking_terms: [{ factor: "nse_momentum_score", preference: "higher" }],
    family_weights: null,
    missing_data: "penalize",
    factor_two: { enabled: false, sort_by: null, sort_direction: "desc" },
    factor_three: { enabled: false, sort_by: null, sort_direction: "desc" },
    index: "nifty-200",
  },
};

const TREND_STRUCTURE: RankingPresetOut = {
  key: "trend_structure",
  label: "Trend structure",
  description: "MA stack and MA 50 slope, with ATR extension scored as a 0-3 target range.",
  sort_by: "ma_stack_score",
  status: "research",
  patch: {
    sort_by: "ma_stack_score",
    sort_direction: "desc",
    ranking_mode: "composite",
    ranking_scope: "filtered_results",
    ranking_terms: [
      { factor: "ma_stack_score", preference: "higher" },
      { factor: "ma50_slope_20", preference: "higher" },
      { factor: "atr_ext_20", preference: "target_range", target_min: 0, target_max: 3 },
    ],
    family_weights: null,
    missing_data: "penalize",
    factor_two: { enabled: false, sort_by: null, sort_direction: "desc" },
    factor_three: { enabled: false, sort_by: null, sort_direction: "desc" },
  },
};

/**
 * A single Sort By with no terms that names neither factor two nor three: the Phase-1 shape an
 * older API served, kept so the menu's own resets are exercised.
 */
const SINGLE_SORT: RankingPresetOut = {
  key: "single_sort",
  label: "Single sort",
  description: "Rank by the share of positive days alone.",
  sort_by: "pos_days_6m",
  status: "research",
  patch: {
    sort_by: "pos_days_6m",
    sort_direction: "desc",
    ranking_mode: "single",
    ranking_scope: "filtered_results",
    moving_average: {
      enabled: true,
      above_200: true,
      above_100: true,
      above_50: true,
      above_20: true,
      below_200: false,
      below_100: false,
      below_50: false,
      below_20: false,
    },
  },
};

/** Not a valid screen on its own: Within sector needs ranking terms. */
const BROKEN: RankingPresetOut = {
  key: "broken_scope",
  label: "Broken scope",
  description: "A patch the schema refuses.",
  sort_by: "ret_6m",
  status: "research",
  patch: { sort_by: "ret_6m", ranking_scope: "within_sector" },
};

/** Names a field the definition does not have. */
const UNKNOWN_FIELD: RankingPresetOut = {
  key: "unknown_field",
  label: "Unknown field",
  description: "A patch with a field the definition does not have.",
  sort_by: "ret_6m",
  status: "ready",
  patch: { sort_by: "ret_6m", not_a_field: true },
};

function merged(definition: ScreenDefinition, change: Partial<ScreenDefinition>) {
  return ScreenDefinitionSchema.safeParse({ ...definition, ...change });
}

/** A composite screen ranked by explicit terms with family weights and a missing-data rule. */
function termsDefinition(): ScreenDefinition {
  return {
    ...defaultDefinition(),
    sort_by: "ret_6m",
    ranking_mode: "composite",
    ranking_scope: "within_sector",
    ranking_terms: [
      { factor: "ret_6m", preference: "higher", weight: 2, target_min: null, target_max: null },
      { factor: "vol_12m", preference: "lower", weight: 1, target_min: null, target_max: null },
    ],
    family_weights: {
      momentum: 2,
      path_quality: null,
      trend_structure: null,
      participation: null,
      risk_execution: 1,
    },
    missing_data: "neutral",
  };
}

describe("validatePreset", () => {
  it("accepts every preset shape core serves, as a patch over the default definition", () => {
    for (const preset of [DESK_QUALITY, PATH_QUALITY, TREND_STRUCTURE, NSE_MOMENTUM, SINGLE_SORT]) {
      const checked = validatePreset(preset);
      expect(checked.ok, preset.key).toBe(true);
    }
  });

  it("fills nested defaults and carries only the keys the preset names", () => {
    const checked = validatePreset(SINGLE_SORT);
    if (!checked.ok) throw new Error(checked.reason);
    expect(Object.keys(checked.patch).sort()).toEqual(
      ["moving_average", "ranking_mode", "ranking_scope", "sort_by", "sort_direction"].sort(),
    );
    expect(checked.patch.moving_average?.above_200).toBe(true);
  });

  it("fills a ranking term's defaults", () => {
    const checked = validatePreset(TREND_STRUCTURE);
    if (!checked.ok) throw new Error(checked.reason);
    expect(checked.patch.ranking_terms?.[0]).toEqual({
      factor: "ma_stack_score",
      preference: "higher",
      weight: 1,
      target_min: null,
      target_max: null,
    });
    expect(checked.patch.ranking_terms?.[2]?.target_max).toBe(3);
  });

  it("refuses a patch that is not a valid screen over the default", () => {
    expect(validatePreset(BROKEN).ok).toBe(false);
    expect(validatePreset(UNKNOWN_FIELD).ok).toBe(false);
  });
});

describe("applying a preset", () => {
  it("patches the current definition and keeps the filters the preset does not name", () => {
    const current: ScreenDefinition = {
      ...defaultDefinition(),
      index: "nifty-mid-small-400",
      price: { from: "50", to: null },
    };
    const applied = applyPreset(current, DESK_QUALITY);
    if (!applied.ok) throw new Error(applied.reason);

    expect(applied.change).not.toHaveProperty("index");
    expect(applied.change).not.toHaveProperty("price");
    const next = merged(current, applied.change);
    expect(next.success).toBe(true);
    expect(next.data?.sort_by).toBe("desk_score");
    expect(next.data?.ranking_mode).toBe("single");
    expect(next.data?.index).toBe("nifty-mid-small-400");
    expect(next.data?.price.from).toBe("50");
  });

  it("clears ranking terms and what depends on them, so the result is a valid screen", () => {
    const current = termsDefinition();
    const applied = applyPreset(current, SINGLE_SORT);
    if (!applied.ok) throw new Error(applied.reason);

    const next = merged(current, applied.change);
    expect(next.success).toBe(true);
    expect(next.data?.ranking_terms).toEqual([]);
    expect(next.data?.family_weights).toBeNull();
    expect(next.data?.missing_data).toBe("penalize");
    expect(next.data?.ranking_scope).toBe("filtered_results");
    expect(next.data?.sort_by).toBe("pos_days_6m");
  });

  it("replaces the current terms and their settings with a composite preset's", () => {
    const current = termsDefinition();
    const applied = applyPreset(current, PATH_QUALITY);
    if (!applied.ok) throw new Error(applied.reason);

    const next = merged(current, applied.change);
    expect(next.success).toBe(true);
    expect(next.data?.ranking_terms.map((term) => term.factor)).toEqual([
      "pos_days_6m",
      "max_dd_12m",
      "downside_vol_12m",
      "ret_ex_top3_12m",
    ]);
    expect(next.data?.family_weights).toBeNull();
    expect(next.data?.missing_data).toBe("penalize");
    expect(next.data?.ranking_scope).toBe("filtered_results");
  });

  it("applies nse_momentum on NIFTY 200 over a screen with factor two and three on", () => {
    const current: ScreenDefinition = {
      ...defaultDefinition(),
      ranking_mode: "sequential",
      factor_two: { enabled: true, sort_by: "ret_3m", sort_direction: "desc" },
      factor_three: { enabled: true, sort_by: "ret_1m", sort_direction: "desc" },
    };
    const applied = applyPreset(current, NSE_MOMENTUM);
    if (!applied.ok) throw new Error(applied.reason);

    const next = merged(current, applied.change);
    expect(next.success).toBe(true);
    expect(next.data?.index).toBe("nifty-200");
    expect(next.data?.sort_by).toBe("nse_momentum_score");
    expect(next.data?.ranking_mode).toBe("single");
    expect(next.data?.factor_two.enabled).toBe(false);
  });

  it("turns factor two and three off when the preset ranks in Single mode", () => {
    const current: ScreenDefinition = {
      ...defaultDefinition(),
      ranking_mode: "sequential",
      factor_two: { enabled: true, sort_by: "ret_3m", sort_direction: "desc" },
      factor_three: { enabled: true, sort_by: "ret_1m", sort_direction: "desc" },
    };
    const checked = validatePreset(SINGLE_SORT);
    if (!checked.ok) throw new Error(checked.reason);
    const change = presetChange(current, checked.patch);

    expect(change.factor_two?.enabled).toBe(false);
    expect(change.factor_three?.enabled).toBe(false);
    expect(merged(current, change).success).toBe(true);
  });
});

describe("PresetsMenuView", () => {
  it("lists each preset with its status badge", async () => {
    const user = userEvent.setup();
    render(
      <PresetsMenuView
        presets={[DESK_QUALITY, PATH_QUALITY, NSE_MOMENTUM]}
        definition={defaultDefinition()}
        patch={vi.fn()}
      />,
    );
    await user.click(screen.getByTestId("presets-menu-trigger"));

    const menu = await screen.findByTestId("presets-menu");
    expect(within(menu).getByTestId("preset-desk_quality")).toHaveTextContent("Desk quality");
    expect(within(menu).getByTestId("preset-nse_momentum")).toHaveTextContent(
      "NIFTY200 Momentum 30 score (NSE methodology)",
    );
    expect(within(menu).getByTestId("preset-status-nse_momentum")).toHaveTextContent("Research");
    expect(within(menu).getByTestId("preset-status-desk_quality")).toHaveTextContent("Ready");
    expect(within(menu).getByTestId("preset-status-path_quality")).toHaveTextContent("Research");
    expect(within(menu).getByText("The desk's quality score, best first.")).toBeInTheDocument();
  });

  it("applies a chosen preset through patch", async () => {
    const user = userEvent.setup();
    const patch = vi.fn();
    const current = termsDefinition();
    render(<PresetsMenuView presets={[DESK_QUALITY]} definition={current} patch={patch} />);

    await user.click(screen.getByTestId("presets-menu-trigger"));
    await user.click(await screen.findByTestId("preset-desk_quality"));

    expect(patch).toHaveBeenCalledTimes(1);
    const change = patch.mock.calls[0]?.[0] as Partial<ScreenDefinition>;
    expect(change.sort_by).toBe("desk_score");
    expect(change.ranking_terms).toEqual([]);
    expect(merged(current, change).success).toBe(true);
  });

  it("shows a preset that is not a valid screen as unavailable and never applies it", async () => {
    const user = userEvent.setup();
    const patch = vi.fn();
    render(
      <PresetsMenuView
        presets={[BROKEN, UNKNOWN_FIELD]}
        definition={defaultDefinition()}
        patch={patch}
      />,
    );
    await user.click(screen.getByTestId("presets-menu-trigger"));

    const broken = await screen.findByTestId("preset-broken_scope");
    expect(broken).toBeDisabled();
    expect(screen.getByTestId("preset-invalid-broken_scope")).toHaveTextContent(
      "Cannot be applied",
    );
    expect(screen.getByTestId("preset-unknown_field")).toBeDisabled();
    await user.click(broken);
    expect(patch).not.toHaveBeenCalled();
  });
});

describe("PresetsMenu", () => {
  it("reads the presets from GET /meta/ranking-presets", async () => {
    api.GET.mockResolvedValue({ data: [PATH_QUALITY], error: undefined });
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <PresetsMenu definition={defaultDefinition()} patch={vi.fn()} />
      </QueryClientProvider>,
    );

    await user.click(screen.getByTestId("presets-menu-trigger"));
    expect(await screen.findByTestId("preset-status-path_quality")).toHaveTextContent("Research");
    expect(api.GET).toHaveBeenCalledWith("/api/v1/meta/ranking-presets");
  });
});

describe("FactorList validation status", () => {
  const base = {
    family: "absolute_return",
    unit: "percent",
    higher_is_better: true,
    preference: "higher",
    rankable: true,
    weight_family: "momentum",
    definition: "",
  } as const;
  const FACTORS: FactorOut[] = [
    { ...base, key: "ret_12m", label: "1Y Return", validation_status: "validated" },
    { ...base, key: "ret_6m", label: "6M Return", validation_status: "research" },
    { ...base, key: "ret_3m", label: "3M Return", validation_status: "rejected" },
    { ...base, key: "ret_1m", label: "1M Return", validation_status: "legacy" },
  ];

  it("shows each factor's validation status", () => {
    render(<FactorList factors={FACTORS} value={null} onChange={vi.fn()} />);

    expect(screen.getByTestId("factor-validation-ret_12m")).toHaveTextContent("Validated");
    expect(screen.getByTestId("factor-validation-ret_6m")).toHaveTextContent("Research");
    expect(screen.getByTestId("factor-validation-ret_3m")).toHaveTextContent("Rejected");
    expect(screen.getByTestId("factor-validation-ret_1m")).toHaveTextContent("Legacy");
  });
});
