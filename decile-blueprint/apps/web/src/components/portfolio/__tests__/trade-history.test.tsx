import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { importTradebookAction, syncTodaysTradesAction } from "@/app/actions/trades";
import { TradeHistory } from "@/components/portfolio/trade-history";

vi.mock("@/app/actions/trades", () => ({
  importTradebookAction: vi.fn(),
  syncTodaysTradesAction: vi.fn(),
}));

afterEach(() => {
  cleanup();
  vi.mocked(importTradebookAction).mockReset();
  vi.mocked(syncTodaysTradesAction).mockReset();
});

/**
 * NEEDS-MAULIK §32, 14 Sep 2026: "sync all the trades made by me from kite api or let me upload
 * csv". The screen must say that the API cannot supply history, offer both paths, and report what
 * an import did — including which holdings it could not date, and why.
 */
describe("trade history", () => {
  it("says where history comes from, and lists what is on record", () => {
    render(
      <TradeHistory
        trades={{
          total: 1,
          first_trade_on: "2025-02-03",
          last_trade_on: "2025-02-03",
          rows: [
            {
              trade_date: "2025-02-03",
              symbol: "PWL",
              exchange: "NSE",
              side: "BUY",
              quantity: "25",
              price: "412.35",
              value: "10308.75",
              trade_id: "T1",
              source: "CONSOLE_CSV",
            },
          ],
        }}
      />,
    );
    expect(screen.getByTestId("trade-history")).toHaveTextContent("only returns today's trades");
    expect(screen.getByTestId("trade-history")).toHaveTextContent("Reports → Tradebook");
    const table = screen.getByTestId("trade-history-table");
    expect(table).toHaveTextContent("PWL");
    expect(table).toHaveTextContent("412.35");
    expect(table).toHaveTextContent("₹10,309");
    expect(table).toHaveTextContent("Tradebook");
  });

  it("reports what a capture stored, and names the holdings it could not date", async () => {
    vi.mocked(syncTodaysTradesAction).mockResolvedValue({
      ok: true,
      report: {
        received: 3,
        inserted: 2,
        already_present: 1,
        skipped_non_equity: 0,
        unresolved_symbols: [],
        dated_holdings: 4,
        undated_holdings: [
          { symbol: "WABAG", held: "92", traded_net: "46", reason: "a bonus changes quantity" },
        ],
        note: "2 new of 3 trades read from Kite",
      },
    });
    render(<TradeHistory trades={null} />);
    expect(screen.getByTestId("trade-history-span")).toHaveTextContent("No trades on record yet");

    fireEvent.click(screen.getByTestId("trades-sync-today"));

    const report = await screen.findByTestId("trade-import-report");
    expect(report).toHaveTextContent("2 new trades stored, 1 already on record");
    expect(report).toHaveTextContent("4 holdings now have their purchase date");
    expect(report).toHaveTextContent("WABAG: holds 92, trades net to 46");
  });

  it("shows the API's refusal as it was given", async () => {
    vi.mocked(syncTodaysTradesAction).mockResolvedValue({
      ok: false,
      error: "no Kite session has been stored yet",
    });
    render(<TradeHistory trades={null} />);
    fireEvent.click(screen.getByTestId("trades-sync-today"));
    await waitFor(() =>
      expect(screen.getByTestId("trade-import-error")).toHaveTextContent(
        "no Kite session has been stored yet",
      ),
    );
  });
});
