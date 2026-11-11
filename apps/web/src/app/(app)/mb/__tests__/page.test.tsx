import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { MbToday } from "@/lib/mb/fetch";
import { fetchMe } from "@/lib/auth/me";
import { fetchMbToday } from "@/lib/mb/fetch";

import MbPage from "../page";

vi.mock("@/lib/mb/fetch", () => ({ fetchMbToday: vi.fn() }));
vi.mock("@/lib/auth/me", () => ({ fetchMe: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => "/mb", useRouter: () => ({ refresh: vi.fn() }) }));

const today: MbToday = {
  as_of: "2026-11-09",
  model: {
    id: 1, trained_on: "2025-11-04", label: "touch_2x_250", rows: 418077, positives: 58129,
    spearman_prev: null, published_at: "2026-11-11T11:26:36Z", published_by: "first model",
  },
  universe_n: 1215,
  investable_n: 996,
  names: [
    {
      rank: 1, instrument_id: 2242, symbol: "SALSTEEL", name: "S.A.L. Steel Limited", sector: "Metals & Mining",
      score: "0.691732", investable: true, hi63_trigger: false, hi63_trigger_date: null, close: "90.08",
      turnover20: "52454004.00", r252_fraction: "3.6892", dist_ath_fraction: "-0.1203", sessions_listed: 2729,
      ttm_rev_yoy_fraction: "-0.5123", ttm_pat_yoy_fraction: null,
    },
    {
      rank: 2, instrument_id: 9001, symbol: "IMAGICAA", name: null, sector: null, score: "0.600000",
      investable: false, hi63_trigger: false, hi63_trigger_date: null, close: "43.41",
      turnover20: "23500000.00", r252_fraction: "0.1000", dist_ath_fraction: "-0.5000", sessions_listed: 120,
      ttm_rev_yoy_fraction: null, ttm_pat_yoy_fraction: null,
    },
    {
      rank: 3, instrument_id: 1673, symbol: "MANAKCOAT", name: null, sector: null, score: "0.560223",
      investable: true, hi63_trigger: true, hi63_trigger_date: "2026-11-09", close: "163.20",
      turnover20: "87429032.00", r252_fraction: "0.0706", dist_ath_fraction: "-0.1070", sessions_listed: 2630,
      ttm_rev_yoy_fraction: "0.1270", ttm_pat_yoy_fraction: "0.1985",
    },
  ],
};

async function renderPage(payload: MbToday | null, staff = false) {
  vi.mocked(fetchMbToday).mockResolvedValue(payload);
  vi.mocked(fetchMe).mockResolvedValue((staff ? { is_staff: true } : null) as Awaited<ReturnType<typeof fetchMe>>);
  render(await MbPage());
}

describe("/mb", () => {
  it("lists the scored names with the session's closes, never as live", async () => {
    await renderPage(today);
    expect(screen.getByTestId("mb-as-of").textContent).toBe("Closes of 9 Nov 2026");
    const rows = screen.getAllByTestId("mb-row");
    expect(rows).toHaveLength(3);
    expect(within(rows[0] as HTMLElement).getByText("SALSTEEL")).toBeTruthy();
    expect(within(rows[0] as HTMLElement).getByText("369%")).toBeTruthy();
    expect(within(rows[2] as HTMLElement).getByTestId("mb-hi63").textContent).toBe("9 Nov 2026");
    expect(rows.map((r) => within(r).getByTestId("mb-rank").textContent)).toEqual(["1", "2", "3"]);
    expect(document.body.textContent?.toLowerCase()).not.toContain("live");
  });

  it("flags every name instead of hiding any, with a key above the list", async () => {
    await renderPage(today);
    const flags = screen.getAllByTestId("mb-flags").map((f) => f.textContent);
    expect(flags).toEqual(["●", "₹◌✱∅", "●▲"]);
    const key = screen.getByTestId("mb-flag-key").textContent ?? "";
    for (const word of ["Investable", "Low price", "Thinly traded", "63-day high", "New listing", "No results on file"]) {
      expect(key).toContain(word);
    }
  });

  it("states the honest numbers and the failures beside the list", async () => {
    await renderPage(today);
    expect(screen.getByTestId("mb-cagr").textContent).toBe("17.2% a year");
    expect(screen.getByTestId("mb-dd").textContent).toBe("-66.3%");
    expect(screen.getByTestId("mb-caveats").textContent).toContain("2025 showed no edge");
    expect(within(screen.getByTestId("mb-years")).getAllByRole("row")).toHaveLength(13);
    expect(screen.getByTestId("mb-model").textContent).toContain("4 Nov 2025");
  });

  it("says nothing has been scored rather than nothing qualified", async () => {
    await renderPage(null);
    expect(screen.getByTestId("mb-unscored")).toBeTruthy();
    expect(screen.queryByTestId("mb-as-of")).toBeNull();
    expect(screen.getByTestId("mb-what")).toBeTruthy();
  });

  it("gives staff the watchlist and a way to the manual desk, and nobody a plan", async () => {
    await renderPage(today);
    expect(screen.queryByTestId("mb-desk")).toBeNull();
    await renderPage(today, true);
    expect(within(screen.getByTestId("mb-desk-list")).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByTestId("mb-desk-link").getAttribute("href")).toBe("/rebalance");
  });
});
