import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InstrumentLink, instrumentHref } from "@/components/instrument/instrument-link";

describe("InstrumentLink", () => {
  it("opens the factsheet for that symbol", () => {
    expect(instrumentHref("shadowfax")).toBe("/instruments/SHADOWFAX");
    render(<InstrumentLink symbol="shadowfax" />);
    expect(screen.getByRole("link", { name: "SHADOWFAX" })).toHaveAttribute(
      "href",
      "/instruments/SHADOWFAX",
    );
  });

  it("does not invent a page for a missing symbol", () => {
    render(<InstrumentLink symbol="—" />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
