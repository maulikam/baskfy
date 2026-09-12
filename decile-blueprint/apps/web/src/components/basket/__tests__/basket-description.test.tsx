import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { BasketDescription } from "@/components/basket/basket-description";

describe("BasketDescription", () => {
  it("offers Read more when the thesis is truncated mid-flow (audit §1.10)", async () => {
    const user = userEvent.setup();
    const long =
      "Momentum names that move in lurches. Liquidity is thin outside the top of the book, " +
      "and the screen re-ranks monthly so the composition can change. " +
      "This sentence exists only to push the copy past the preview limit so the control appears.";
    render(<BasketDescription markdown={long} />);
    expect(screen.getByRole("button", { name: /read more/i })).toBeInTheDocument();
    expect(screen.getByText(/…$/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /read more/i }));
    expect(screen.getByText(/composition can change/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /show less/i })).toBeInTheDocument();
  });

  it("does not offer Read more for a short thesis", () => {
    render(<BasketDescription markdown="A short basket." />);
    expect(screen.queryByRole("button", { name: /read more/i })).toBeNull();
  });
});
