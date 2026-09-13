import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ScreenIdentity } from "@/components/screens/screen-identity";

describe("ScreenIdentity", () => {
  it("lets an owned screen rename and delete", async () => {
    const onNameChange = vi.fn();
    const onDelete = vi.fn();
    const user = userEvent.setup();
    render(
      <ScreenIdentity
        name="Untitled screen 1"
        onNameChange={onNameChange}
        canEdit
        canDelete
        onDelete={onDelete}
      />,
    );

    const field = screen.getByTestId("screen-name");
    await user.clear(field);
    await user.type(field, "Midcap momentum");
    expect(onNameChange).toHaveBeenCalled();

    await user.click(screen.getByTestId("delete-screen"));
    expect(onDelete).toHaveBeenCalledTimes(1);
  });

  it("lets a template rename in place, but not delete", () => {
    render(
      <ScreenIdentity
        name="Investing 001"
        onNameChange={vi.fn()}
        canEdit
        canDelete={false}
      />,
    );

    expect(screen.getByTestId("screen-name")).toHaveValue("Investing 001");
    expect(screen.queryByTestId("delete-screen")).toBeNull();
  });
});
