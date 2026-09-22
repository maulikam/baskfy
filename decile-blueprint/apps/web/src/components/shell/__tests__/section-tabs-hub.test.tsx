import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SectionTabs } from "@/components/shell/section-tabs";
import { StaffProvider } from "@/components/shell/staff-context";

/**
 * Staff on a sleeve used to lose Swing / Volume / Tight, because those tabs only existed on
 * `section="build"`. Switching Volume → Swing meant clicking Build (Screens) and picking again.
 * The hub row has to stay on the sleeve pages themselves.
 */

let pathname = "/swing";

vi.mock("next/navigation", () => ({
  usePathname: () => pathname,
}));

describe("staff can switch strategies without a hop through Screens", () => {
  it("keeps the Build hub row on a sleeve, with Volume linking to /vbt", () => {
    pathname = "/swing";
    render(
      <StaffProvider value={true}>
        <SectionTabs section="swing" />
      </StaffProvider>,
    );

    const hub = screen.getByRole("navigation", { name: "Build" });
    expect(within(hub).getByRole("link", { name: "Swing" })).toHaveAttribute("href", "/swing");
    expect(within(hub).getByRole("link", { name: "Volume" })).toHaveAttribute("href", "/vbt");
    expect(within(hub).getByRole("link", { name: "Tight" })).toHaveAttribute("href", "/twt");
    expect(within(hub).getByRole("link", { name: "Options" })).toHaveAttribute("href", "/options");
    expect(within(hub).getByRole("link", { name: "Screens" })).toHaveAttribute("href", "/build");
    expect(within(hub).getByRole("link", { name: "Swing" })).toHaveAttribute("aria-current", "page");

    const local = screen.getByRole("navigation", { name: "In this strategy" });
    expect(within(local).getByRole("link", { name: "Setups" })).toHaveAttribute("aria-current", "page");
    expect(within(local).getByRole("link", { name: "Watchlist" })).toHaveAttribute(
      "href",
      "/swing/watchlist",
    );
  });

  it("lights Volume on the hub and Positions on the sleeve when the book is open", () => {
    pathname = "/vbt/book";
    render(
      <StaffProvider value={true}>
        <SectionTabs section="vbt" />
      </StaffProvider>,
    );

    const hub = screen.getByRole("navigation", { name: "Build" });
    expect(within(hub).getByRole("link", { name: "Volume" })).toHaveAttribute("aria-current", "page");
    expect(within(hub).getByRole("link", { name: "Swing" })).not.toHaveAttribute("aria-current");

    const local = screen.getByRole("navigation", { name: "In this strategy" });
    expect(within(local).getByRole("link", { name: "Positions" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(local).getByRole("link", { name: "Today" })).not.toHaveAttribute("aria-current");
  });

  it("does not show the hub row to civilians, or mix sleeve tabs into Build", () => {
    pathname = "/swing";
    render(<SectionTabs section="swing" />);

    expect(screen.queryByRole("navigation", { name: "Build" })).toBeNull();
    expect(screen.getByRole("navigation", { name: "Section" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Volume" })).toBeNull();
    expect(screen.getByRole("link", { name: "Setups" })).toBeInTheDocument();
  });
});
