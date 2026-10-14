import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RecutFromScreen } from "@/components/basket/recut-from-screen";
import { recutBasketFromScreen, type RecutResponse } from "@/lib/create/fetch";

/**
 * Maulik, 14 Oct 2026: re-run the screen, see what was added and removed, and decide. So the
 * first click must only preview, and a version is saved only by the second.
 */

const refresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh }),
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
  recutBasketFromScreen: vi.fn(),
}));

function response(overrides: Partial<RecutResponse> = {}): RecutResponse {
  return {
    slug: "maulik",
    screen_public_id: "scr_momo",
    as_of: "2026-10-14",
    method: "EQUAL",
    method_fell_back: false,
    current_version_no: 1,
    added: ["EEE"],
    removed: ["CCC"],
    kept: ["AAA", "BBB"],
    changed: true,
    written: false,
    version_no: null,
    holdings: [],
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(recutBasketFromScreen).mockReset();
  refresh.mockReset();
});
afterEach(cleanup);

describe("RecutFromScreen", () => {
  it("previews first, and writes nothing until asked", async () => {
    vi.mocked(recutBasketFromScreen).mockResolvedValueOnce(response());
    const user = userEvent.setup();
    render(<RecutFromScreen slug="maulik" />);

    await user.click(screen.getByRole("button", { name: "Re-cut from screen" }));

    expect(recutBasketFromScreen).toHaveBeenCalledExactlyOnceWith("maulik", false);
    expect(screen.getByText(/EEE/)).toBeTruthy();
    expect(screen.getByText(/CCC/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Save as version 2" })).toBeTruthy();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("saves the next version on the second click", async () => {
    vi.mocked(recutBasketFromScreen)
      .mockResolvedValueOnce(response())
      .mockResolvedValueOnce(response({ written: true, version_no: 2 }));
    const user = userEvent.setup();
    render(<RecutFromScreen slug="maulik" />);

    await user.click(screen.getByRole("button", { name: "Re-cut from screen" }));
    await user.click(screen.getByRole("button", { name: "Save as version 2" }));

    expect(recutBasketFromScreen).toHaveBeenLastCalledWith("maulik", true);
    expect(screen.getByTestId("recut-saved").textContent).toContain("Saved as version 2");
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("offers no save when the screen picks the same names", async () => {
    vi.mocked(recutBasketFromScreen).mockResolvedValueOnce(
      response({ changed: false, added: [], removed: [] }),
    );
    const user = userEvent.setup();
    render(<RecutFromScreen slug="maulik" />);

    await user.click(screen.getByRole("button", { name: "Re-cut from screen" }));

    expect(screen.getByTestId("recut-unchanged")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Save as version/ })).toBeNull();
  });
});
