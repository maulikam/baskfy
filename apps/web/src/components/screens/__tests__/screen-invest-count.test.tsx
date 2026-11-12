import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ScreenInvestActions } from "@/components/screens/screen-invest";
import { StockCountControl } from "@/components/screens/stock-count";
import type * as InvestModule from "@/lib/screens/invest";
import type { ScreenInvestPreview, ScreenInvestRequest } from "@/lib/screens/invest";
import { checkInvestCount, defaultInvestCount } from "@/lib/screens/invest";

vi.mock("@/lib/portfolios/queries", () => ({
  usePortfolios: () => ({ data: { data: [], orphans: [] }, isError: false, refetch: vi.fn() }),
}));

const calls = vi.hoisted((): { preview: ScreenInvestRequest[]; confirm: ScreenInvestRequest[] } => ({
  preview: [],
  confirm: [],
}));

function plan(input: ScreenInvestRequest): ScreenInvestPreview {
  const n = input.top_n ?? 20;
  return {
    as_of: "2026-11-11",
    cash_outlay: String(input.amount),
    exposure: "0",
    leverage: "1",
    mtf: false,
    min_investment: "100",
    leftover: "0",
    reserve: "0",
    costs: "0",
    contribution: "0",
    funding: "0",
    gross_target: "0",
    top_n: n,
    available: 12,
    plan_id: `plan-${n}`,
    allocation_version: "v",
    quoted_at: "q",
    constraint: null,
    empty_reason: null,
    holdings: Array.from({ length: Math.min(n, 12) }, (_, i) => ({
      symbol: `S${i + 1}`,
      name: `S${i + 1}`,
      rank: i + 1,
      quantity: 1,
      amount: "100",
      weight: "0.1",
      price: "100",
      leverage: "1",
      product: "CNC" as const,
      contribution: "100",
      funding: "0",
      deviation: "0",
    })),
    skipped: [],
    report: null,
  };
}

vi.mock("@/lib/screens/invest", async (importOriginal) => {
  const actual = await importOriginal<typeof InvestModule>();
  return {
    ...actual,
    previewScreenInvest: (_id: string, input: ScreenInvestRequest) => {
      calls.preview.push(input);
      return Promise.resolve(plan(input));
    },
    confirmScreenInvest: (_id: string, input: ScreenInvestRequest) => {
      calls.confirm.push(input);
      return Promise.resolve({ ...plan(input), report: null, empty_reason: "stub" });
    },
  };
});

function open(props: { investCount?: number | null; available?: number | null } = {}) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ScreenInvestActions screenName="Momo" screenPublicId="abc" {...props} />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByTestId("screen-invest"));
}

describe("number of stocks on invest (12 Nov 2026)", () => {
  beforeEach(() => {
    calls.preview.length = 0;
    calls.confirm.length = 0;
  });

  it("defaults to the screen's saved count and sends it as top_n", async () => {
    open({ investCount: 5, available: 12 });
    expect(screen.getByTestId("screen-invest-count")).toHaveValue(5);
    await waitFor(() => expect(calls.preview.at(-1)?.top_n).toBe(5));
    await waitFor(() =>
      expect(screen.getByTestId("screen-invest-count-help")).toHaveTextContent("Top 5 ranked stocks of 12"),
    );
  });

  it("defaults to 20, capped at what the screen ranks", async () => {
    open({ available: 12 });
    expect(screen.getByTestId("screen-invest-count")).toHaveValue(12);
    await waitFor(() => expect(calls.preview.at(-1)?.top_n).toBe(12));
  });

  it("re-sizes on an edit and confirms with the same count", async () => {
    open({ investCount: 5, available: 12 });
    fireEvent.change(screen.getByTestId("screen-invest-count"), { target: { value: "8" } });
    await waitFor(() => expect(calls.preview.at(-1)?.top_n).toBe(8));
    await waitFor(() => expect(screen.getByTestId("screen-invest-confirm")).toBeEnabled());
    fireEvent.click(screen.getByTestId("screen-invest-confirm"));
    await waitFor(() => expect(calls.confirm.at(-1)?.top_n).toBe(8));
    expect(calls.confirm.at(-1)?.plan_id).toBe("plan-8");
  });

  it("refuses zero and more than the screen ranks", async () => {
    open({ investCount: 5, available: 12 });
    await waitFor(() => expect(screen.getByTestId("screen-invest-confirm")).toBeEnabled());
    fireEvent.change(screen.getByTestId("screen-invest-count"), { target: { value: "0" } });
    expect(screen.getByTestId("screen-invest-count-help")).toHaveTextContent("at least 1");
    expect(screen.getByTestId("screen-invest-confirm")).toBeDisabled();
    fireEvent.change(screen.getByTestId("screen-invest-count"), { target: { value: "13" } });
    await waitFor(() =>
      expect(screen.getByTestId("screen-invest-count-help")).toHaveTextContent("ranks only 12"),
    );
    expect(screen.getByTestId("screen-invest-confirm")).toBeDisabled();
  });
});

describe("invest count helpers", () => {
  it("checks bounds", () => {
    expect(checkInvestCount(null, null)).not.toBeNull();
    expect(checkInvestCount(1.5, null)).not.toBeNull();
    expect(checkInvestCount(101, null)).not.toBeNull();
    expect(checkInvestCount(30, 20)).not.toBeNull();
    expect(checkInvestCount(20, 20)).toBeNull();
    expect(checkInvestCount(30, null)).toBeNull();
  });

  it("defaults", () => {
    expect(defaultInvestCount(null, null)).toBe(20);
    expect(defaultInvestCount(null, 7)).toBe(7);
    expect(defaultInvestCount(9, 30)).toBe(9);
  });
});

describe("Stocks to show / invest", () => {
  it("saves a whole count and clears with blank", () => {
    const onSave = vi.fn();
    const { rerender } = render(
      <StockCountControl value={null} available={40} canEdit saving={false} onSave={onSave} />,
    );
    expect(screen.getByText("Stocks to show / invest")).toBeInTheDocument();
    expect(screen.getByTestId("stock-count-save")).toBeDisabled();
    fireEvent.change(screen.getByTestId("stock-count-input"), { target: { value: "10" } });
    fireEvent.click(screen.getByTestId("stock-count-save"));
    expect(onSave).toHaveBeenLastCalledWith(10);
    rerender(<StockCountControl value={10} available={40} canEdit saving={false} onSave={onSave} />);
    fireEvent.change(screen.getByTestId("stock-count-input"), { target: { value: "" } });
    fireEvent.click(screen.getByTestId("stock-count-save"));
    expect(onSave).toHaveBeenLastCalledWith(null);
  });

  it("is read-only on a screen you cannot edit", () => {
    render(<StockCountControl value={5} available={40} canEdit={false} saving={false} onSave={vi.fn()} />);
    expect(screen.getByTestId("stock-count-input")).toBeDisabled();
    expect(screen.queryByTestId("stock-count-save")).toBeNull();
  });
});
