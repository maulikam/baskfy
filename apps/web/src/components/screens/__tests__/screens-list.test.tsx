import type { ScreenOut } from "@baskfy/api-client";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ScreensList } from "@/components/screens/screens-list";
import { defaultDefinition } from "@/lib/screens/defaults";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => "/build",
}));

vi.mock("@/components/shell/section-tabs", () => ({
  SectionTabs: () => <nav aria-label="Build">tabs</nav>,
}));

const saveMutate = vi.fn();

vi.mock("@/lib/screens/queries", () => ({
  useFactors: () => ({ data: [] }),
  useUniverses: () => ({ data: [] }),
  useDuplicateScreen: () => ({ mutateAsync: vi.fn(), isPending: false, error: null }),
  useDeleteScreen: () => ({ mutateAsync: vi.fn(), isPending: false, error: null }),
  useCreateScreen: () => ({ mutateAsync: vi.fn(), isPending: false, error: null }),
  useSaveScreen: () => ({ mutateAsync: saveMutate, isPending: false, error: null }),
}));

function makeScreen(publicId: string, name: string, isExample: boolean): ScreenOut {
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

describe("ScreensList", () => {
  it("offers rename and delete on an owned screen, not on a template", async () => {
    const user = userEvent.setup();
    saveMutate.mockResolvedValue(makeScreen("usr000000001", "Midcap momentum", false));
    render(
      <ScreensList
        initial={[
          makeScreen("usr000000001", "Untitled screen 1", false),
          makeScreen("exmpl0000001", "Investing 001", true),
        ]}
        error={null}
      />,
    );

    const mine = screen.getByTestId("your-screens");
    const templates = screen.getByTestId("example-screens");
    expect(mine.querySelector('[data-testid="rename-screen"]')).not.toBeNull();
    expect(mine.querySelector('[data-testid="delete-screen"]')).not.toBeNull();
    expect(templates.querySelector('[data-testid="rename-screen"]')).toBeNull();
    expect(templates.querySelector('[data-testid="delete-screen"]')).toBeNull();

    await user.click(screen.getByTestId("rename-screen"));
    const field = screen.getByTestId("rename-screen-input");
    await user.clear(field);
    await user.type(field, "Midcap momentum");
    await user.click(screen.getByRole("button", { name: "Save name" }));
    expect(saveMutate).toHaveBeenCalledWith({
      publicId: "usr000000001",
      name: "Midcap momentum",
    });
  });
});
