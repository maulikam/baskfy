import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DeskMenu } from "@/components/shell/desk-menu";
import { StaffProvider } from "@/components/shell/staff-context";

vi.mock("next/navigation", () => ({
  usePathname: () => "/regime/backtest",
}));

function renderMenu() {
  return render(
    <StaffProvider value={false}>
      <DeskMenu />
    </StaffProvider>,
  );
}

describe("the Desk menu", () => {
  it("lists the desk pages in the web app and opens Regime's backtest", async () => {
    const user = userEvent.setup();
    renderMenu();

    const trigger = screen.getByRole("button", { name: "Desk" });
    expect(trigger).toHaveAttribute("aria-current", "page");
    await user.click(trigger);

    expect(screen.getByRole("menuitem", { name: "Performance" })).toHaveAttribute(
      "href",
      "/performance",
    );
    expect(screen.getByRole("menuitem", { name: "Swing" })).toHaveAttribute("href", "/swing");

    await user.hover(screen.getByRole("menuitem", { name: "Regime" }));
    expect(await screen.findByRole("menuitem", { name: "Backtest" })).toHaveAttribute(
      "href",
      "/regime/backtest",
    );
    expect(screen.getByRole("menuitem", { name: "Backtest" })).toHaveAttribute("aria-current", "page");
  });
});
