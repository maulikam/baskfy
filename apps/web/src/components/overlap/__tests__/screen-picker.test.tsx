import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ScreenPicker } from "@/components/overlap/screen-picker";

const push = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

const screens = [
  { publicId: "exmpl0000001", name: "Investing 001", isExample: true },
  { publicId: "exmpl0000002", name: "Trend Stack", isExample: true },
  { publicId: "exmpl0000003", name: "Top Baskfy Liquid Momentum", isExample: true },
  { publicId: "exmpl0000004", name: "Momentum, Low Volatility", isExample: true },
  { publicId: "user00000001", name: "My screen", isExample: false },
];

describe("overlap screen picker", () => {
  it("offers three dropdowns, each listing at least three screens", () => {
    render(
      <ScreenPicker
        screens={screens}
        selected={["exmpl0000001", "exmpl0000002", "exmpl0000003"]}
      />,
    );

    const selects = [
      screen.getByTestId("overlap-screen-picker-0"),
      screen.getByTestId("overlap-screen-picker-1"),
      screen.getByTestId("overlap-screen-picker-2"),
    ];
    expect(selects).toHaveLength(3);
    for (const select of selects) {
      expect(within(select).getAllByRole("option").length).toBeGreaterThanOrEqual(3);
      expect(within(select).getByRole("option", { name: "Investing 001" })).toBeInTheDocument();
      expect(within(select).getByRole("option", { name: "Trend Stack" })).toBeInTheDocument();
      expect(within(select).getByRole("option", { name: "Top Baskfy Liquid Momentum" })).toBeInTheDocument();
    }
  });

  it("keeps the other two screens when one dropdown changes", async () => {
    const user = userEvent.setup();
    render(
      <ScreenPicker
        screens={screens}
        selected={["exmpl0000001", "exmpl0000002", "exmpl0000003"]}
      />,
    );

    await user.selectOptions(screen.getByTestId("overlap-screen-picker-1"), "exmpl0000004");

    expect(push).toHaveBeenCalledWith(
      "/build/overlap?screen=exmpl0000001&screen=exmpl0000004&screen=exmpl0000003",
    );
  });
});
