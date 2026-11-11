import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { MbPaper } from "@/lib/mb/fetch";
import { fetchMbPaper } from "@/lib/mb/fetch";

import MbPaperPage from "../paper/page";

vi.mock("@/lib/mb/fetch", () => ({ fetchMbPaper: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => "/mb/paper", useRouter: () => ({ refresh: vi.fn() }) }));

const book = (source: "live" | "backtest", b: "book" | "hi63_20" | "hi63_16" | "book_overlay", growth: string) => ({
  source, book: b, start: "2014-01-02", end: "2026-11-09", growth_fraction: growth, cagr_fraction: source === "live" ? null : "0.1870",
  max_dd_fraction: "-0.6050", invested_fraction: b === "book_overlay" ? "1.0000" : "0.1682",
});

const paper: MbPaper = {
  live_from_month: "2026-12",
  books: [book("backtest", "book", "8.0734"), book("backtest", "hi63_20", "6.4676"), book("backtest", "book_overlay", "6.9982")],
  outcomes: [{ source: "backtest", line: "book", matured: true, lines: 2860, touched_2x_fraction: "0.2038", halved_fraction: "0.0545", median_return_fraction: "0.0790" }],
  tranches: [],
};

describe("/mb/paper", () => {
  it("says when the live record starts and labels the backtest as one", async () => {
    vi.mocked(fetchMbPaper).mockResolvedValue(paper);
    render(await MbPaperPage());
    expect(screen.getByTestId("mb-paper-unstarted").textContent).toContain("2026-12");
    const back = screen.getByTestId("mb-paper-backtest");
    expect(within(back).getByText("Backtest, not live")).toBeTruthy();
    expect(within(screen.getByTestId("mb-paper-back")).getAllByRole("row")).toHaveLength(4);
    expect(within(screen.getByTestId("mb-paper-back-outcomes")).getByText("20.4%")).toBeTruthy();
    expect(screen.getByTestId("mb-paper-expect").textContent).toContain("20–23%");
  });

  it("renders live books when the record has started", async () => {
    vi.mocked(fetchMbPaper).mockResolvedValue({
      ...paper,
      books: [...paper.books, book("live", "book", "0.0075")],
      tranches: [{ signal_date: "2026-12-01", model_id: 1, sessions_held: 5, hi63_lines: 3, lines: 20, touched_2x_fraction: "0", halved_fraction: "0", median_return_fraction: "0.0276" }],
    });
    render(await MbPaperPage());
    expect(within(screen.getByTestId("mb-paper-live")).getAllByRole("row")).toHaveLength(2);
    expect(screen.getByTestId("mb-paper-tranches").textContent).toContain("1 Dec 2026: 20 names");
  });
});
