import type { ScreenOut, ScreenRunResponse } from "@baskfy/api-client";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CreateFromScreen } from "@/components/create/create-from-screen";
import { CreateWorkspace } from "@/components/create/create-workspace";
import { createBasketFromScreen } from "@/lib/create/fetch";
import { defaultDefinition } from "@/lib/screens/defaults";
import { usePreview } from "@/lib/screens/queries";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("@/lib/screens/queries", () => ({
  usePreview: vi.fn(),
}));

vi.mock("@/lib/create/fetch", () => ({
  CreateBasketError: class CreateBasketError extends Error {
    readonly status: number;
    constructor(message: string, status: number) {
      super(message);
      this.status = status;
      this.name = "CreateBasketError";
    }
  },
  createBasketFromScreen: vi.fn(),
  createPrivateBasket: vi.fn(),
}));

function screenOut(
  publicId: string,
  name: string,
  isExample: boolean,
): ScreenOut {
  return {
    public_id: publicId,
    name,
    is_example: isExample,
    editable: !isExample,
    columns: ["symbol", "name", "sorting_factor", "close_raw"],
    created_at: "2026-08-18T00:00:00Z",
    updated_at: "2026-08-18T00:00:00Z",
    definition: defaultDefinition() as ScreenOut["definition"],
  };
}

const TEMPLATES = [
  screenOut("exmpl0000001", "Investing 001", true),
  screenOut("exmpl0000002", "Trend Stack", true),
];

const MINE = [screenOut("usr000000001", "My momentum", false)];

function runResponse(rowCount: number): ScreenRunResponse {
  return {
    as_of: "2026-08-18",
    columns: ["symbol", "name", "sorting_factor", "close_raw"],
    data_version: 41,
    result_count: rowCount,
    sorting_factor: { key: "sharpe_12m", label: "Sharpe 12M" },
    rows: Array.from({ length: rowCount }, (_, i) => ({
      rank: i + 1,
      symbol: `SYM${i + 1}`,
      name: `Company ${i + 1}`,
      sorting_factor: 5.13 - i * 0.01,
      close_raw: 1_000 + i,
      ret_12m: 10 + i,
      vol_12m: 0.2 + i * 0.001,
      marketcap_cr: 800 + i * 10,
      beta_12m: 0.9 + i * 0.01,
      sharpe_12m: 1.2,
      median_vol_12m: 20_000_000 + i * 1_000,
    })),
  };
}

function mockPreview(rows = 40) {
  vi.mocked(usePreview).mockReturnValue({
    data: runResponse(rows),
    isPending: false,
    isFetching: false,
    error: null,
    refetch: vi.fn(),
  } as never);
}

function fillAmount(value: number) {
  fireEvent.change(screen.getByRole("spinbutton", { name: "Amount to invest" }), {
    target: { value: String(value) },
  });
}

afterEach(() => {
  cleanup();
  vi.mocked(createBasketFromScreen).mockReset();
});

beforeEach(() => {
  mockPreview();
});

describe("CreateFromScreen — first login already has screens", () => {
  it("lists the templates in the dropdown when the investor has saved none", () => {
    render(
      <CreateFromScreen screens={TEMPLATES} initialScreenId="exmpl0000001" />,
    );

    const select = screen.getByTestId("create-screen-select");
    expect(select).toHaveValue("exmpl0000001");
    expect(screen.getByRole("option", { name: "Investing 001" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Trend Stack" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Templates — ready on first login" })).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Your screens" })).not.toBeInTheDocument();
  });

  it("keeps the investor's own screens in a separate group", () => {
    render(
      <CreateFromScreen
        screens={[...TEMPLATES, ...MINE]}
        initialScreenId="exmpl0000001"
      />,
    );

    expect(screen.getByRole("group", { name: "Your screens" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "My momentum" })).toBeInTheDocument();
  });
});

describe("CreateFromScreen — the profile suggests, the investor decides", () => {
  it("opens on the balanced suggestion of 20 names", () => {
    render(
      <CreateFromScreen screens={TEMPLATES} initialScreenId="exmpl0000001" />,
    );

    expect(screen.getByRole("spinbutton", { name: "Amount to invest" })).toHaveValue(null);
    expect(screen.getByRole("spinbutton", { name: "Number of stocks" })).toHaveValue(20);
    expect(screen.getByText(/Suggested for balanced/i)).toBeInTheDocument();
    expect(screen.getByTestId("profile-balanced")).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("basket-health")).toBeInTheDocument();
    expect(screen.getByText(/Largest name/)).toBeInTheDocument();
  });

  it("moves the suggestion when the investor picks a more concentrated profile", async () => {
    const user = userEvent.setup();
    render(
      <CreateFromScreen screens={TEMPLATES} initialScreenId="exmpl0000001" />,
    );

    await user.click(screen.getByTestId("profile-aggressive"));
    expect(screen.getByRole("spinbutton", { name: "Number of stocks" })).toHaveValue(12);
    expect(screen.getByText(/Suggested for concentrated/i)).toBeInTheDocument();
  });

  it("lets an explicit count beat the suggestion, and records that on save", async () => {
    const user = userEvent.setup();
    vi.mocked(createBasketFromScreen).mockResolvedValue({
      slug: "my-cut",
      name: "Investing 001",
    } as never);

    render(
      <CreateFromScreen screens={TEMPLATES} initialScreenId="exmpl0000001" />,
    );

    const count = screen.getByRole("spinbutton", { name: "Number of stocks" });
    fireEvent.change(count, { target: { value: "8" } });
    expect(screen.getByText(/Yours, not the suggested 20/)).toBeInTheDocument();

    fillAmount(100_000);
    await user.click(screen.getByTestId("save-from-screen"));
    expect(createBasketFromScreen).toHaveBeenCalledWith(
      expect.objectContaining({
        screen_public_id: "exmpl0000001",
        holdings: 8,
        profile: null,
      }),
    );
  });

  it("sends the profile, not a count, when the investor kept the suggestion", async () => {
    const user = userEvent.setup();
    vi.mocked(createBasketFromScreen).mockResolvedValue({
      slug: "my-cut",
      name: "Investing 001",
    } as never);

    render(
      <CreateFromScreen screens={TEMPLATES} initialScreenId="exmpl0000001" />,
    );

    fillAmount(100_000);
    await user.click(screen.getByTestId("save-from-screen"));
    expect(createBasketFromScreen).toHaveBeenCalledWith(
      expect.objectContaining({
        screen_public_id: "exmpl0000001",
        profile: "BALANCED",
        holdings: null,
        amount: 100_000,
        method: "EQUAL",
      }),
    );
    expect(createBasketFromScreen).toHaveBeenCalledWith(
      // vitest types its matchers as `any`; narrowing to `unknown` keeps the assertion and
      // stops the `any` from spreading into the object literal.
      expect.not.objectContaining({ custom_weights: expect.anything() as unknown }),
    );
  });
});

describe("CreateFromScreen — cash sleeve (SB7)", () => {
  it("suggests a cash sleeve by default and lets the investor opt out", async () => {
    const user = userEvent.setup();
    render(
      <CreateFromScreen screens={TEMPLATES} initialScreenId="exmpl0000001" />,
    );

    expect(screen.getByTestId("cash-sleeve-keep")).toBeChecked();
    expect(screen.getByText(/Suggested 5%/)).toBeInTheDocument();

    fillAmount(100_000);
    await user.click(screen.getByTestId("cash-sleeve-none"));
    expect(screen.getByTestId("cash-sleeve-none")).toBeChecked();
    expect(screen.getByText(/rounding remainder stays uninvested/i)).toBeInTheDocument();
  });

  it("sends zero cash when the investor opts out", async () => {
    const user = userEvent.setup();
    vi.mocked(createBasketFromScreen).mockResolvedValue({
      slug: "my-cut",
      name: "Investing 001",
    } as never);

    render(
      <CreateFromScreen screens={TEMPLATES} initialScreenId="exmpl0000001" />,
    );

    fillAmount(100_000);
    await user.click(screen.getByTestId("cash-sleeve-none"));
    await user.click(screen.getByTestId("save-from-screen"));

    expect(createBasketFromScreen).toHaveBeenCalledWith(
      expect.objectContaining({ cash_pct: 0 }),
    );
  });
});

describe("CreateFromScreen — how the money is split", () => {
  it("opens on equal weight and offers the suggested methods plus custom", () => {
    render(
      <CreateFromScreen screens={TEMPLATES} initialScreenId="exmpl0000001" />,
    );

    expect(screen.getByTestId("method-equal")).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("method-rank")).toBeInTheDocument();
    expect(screen.getByTestId("method-score")).toBeInTheDocument();
    expect(screen.getByTestId("method-inv_vol")).toBeInTheDocument();
    expect(screen.getByTestId("method-custom")).toBeInTheDocument();
    expect(screen.queryByTestId("custom-weights")).not.toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Market cap" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Bumpiness" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Liquidity" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Sector" })).not.toBeInTheDocument();
  });

  it("sends rank when that method is picked", async () => {
    const user = userEvent.setup();
    vi.mocked(createBasketFromScreen).mockResolvedValue({
      slug: "my-cut",
      name: "Investing 001",
    } as never);

    render(
      <CreateFromScreen screens={TEMPLATES} initialScreenId="exmpl0000001" />,
    );

    await user.click(screen.getByTestId("method-rank"));
    fillAmount(250_000);
    await user.click(screen.getByTestId("save-from-screen"));
    expect(createBasketFromScreen).toHaveBeenCalledWith(
      expect.objectContaining({ method: "RANK", amount: 250_000 }),
    );
  });

  it("lets the investor type their own numbers, only for the screen's names", async () => {
    const user = userEvent.setup();
    vi.mocked(createBasketFromScreen).mockResolvedValue({
      slug: "my-cut",
      name: "Investing 001",
    } as never);

    render(
      <CreateFromScreen screens={TEMPLATES} initialScreenId="exmpl0000001" />,
    );

    await user.click(screen.getByTestId("method-custom"));
    expect(screen.getByTestId("custom-weights")).toBeInTheDocument();
    expect(screen.getByLabelText("SYM1 weight")).toBeInTheDocument();
    const custom = screen.getByTestId("custom-weights");
    expect(custom).toHaveTextContent("Market cap");
    expect(custom).toHaveTextContent("Bumpiness");
    expect(custom).toHaveTextContent("Liquidity");
    expect(custom).not.toHaveTextContent("Sector");
    expect(screen.queryByLabelText("NOT-ON-SCREEN weight")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("SYM1 weight"), { target: { value: "40" } });
    fillAmount(100_000);
    await user.click(screen.getByTestId("save-from-screen"));
    expect(createBasketFromScreen).toHaveBeenCalledWith(
      expect.objectContaining({
        method: "CUSTOM",
        custom_weights: expect.arrayContaining([
          expect.objectContaining({ symbol: "SYM1", weight: 40 }),
        ]) as unknown,
      }),
    );
    const posted = vi.mocked(createBasketFromScreen).mock.calls[0]?.[0];
    expect(posted?.custom_weights).toHaveLength(20);
  });
});

describe("CreateWorkspace", () => {
  it("opens on the screen path, and the manual form is one tap away", async () => {
    const user = userEvent.setup();
    render(
      <CreateWorkspace screens={TEMPLATES} initialScreenId="exmpl0000001" />,
    );

    expect(screen.getByTestId("create-mode-screen")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("create-screen-select")).toBeInTheDocument();

    await user.click(screen.getByTestId("create-mode-manual"));
    expect(screen.getByTestId("create-mode-manual")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Name")).toBeInTheDocument();
    expect(screen.queryByTestId("create-screen-select")).not.toBeInTheDocument();
  });
});
