import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SentinelNumberInput, isSentinel } from "@/components/data/sentinel-number-input";

/**
 * docs/08 §"Screen editor": "Sentinel values are explained inline exactly as the reference does
 * ('Keep value as 100 if you want to ignore…'), and the field renders visually 'off' when at its
 * sentinel." Prompt 8 adds: "explains it via aria-describedby".
 *
 * The `above` mode matters as much as the equality one: docs/01 §2.6 makes the circuits sentinel a
 * *threshold* (`> 250`), and a component that only understood equality would leave that filter
 * silently applied at 999.
 */
const HINT = "Keep value as 100 if you want to ignore this filter.";

function setup(value: number, mode: "equals" | "above" = "equals") {
  const onChange = vi.fn();
  render(
    <SentinelNumberInput
      label="Within % of all-time high"
      value={value}
      onChange={onChange}
      sentinel={mode === "above" ? 250 : 100}
      mode={mode}
      sentinelHint={HINT}
    />,
  );
  return { onChange };
}

describe("isSentinel", () => {
  it("compares by equality for the away-from-high and beta sentinels", () => {
    // docs/01 §2.4 and §2.10: 100 = ignore.
    expect(isSentinel(100, 100, "equals")).toBe(true);
    expect(isSentinel(99, 100, "equals")).toBe(false);
  });

  it("compares by threshold for circuits", () => {
    // docs/01 §2.6: "> 250 = ignore", so 250 itself is an active cap.
    expect(isSentinel(999, 250, "above")).toBe(true);
    expect(isSentinel(251, 250, "above")).toBe(true);
    expect(isSentinel(250, 250, "above")).toBe(false);
  });
});

describe("SentinelNumberInput", () => {
  it("describes the sentinel through aria-describedby", () => {
    setup(100);
    const input = screen.getByLabelText("Within % of all-time high");
    const describedBy = input.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy as string)?.textContent).toContain(HINT);
  });

  it("says the filter is off when it is at its sentinel", () => {
    setup(100);
    const input = screen.getByLabelText("Within % of all-time high");
    const hint = document.getElementById(input.getAttribute("aria-describedby") as string);
    expect(hint?.textContent).toContain("This filter is currently off.");
  });

  it("does not say so when the filter is doing something", () => {
    setup(25);
    const input = screen.getByLabelText("Within % of all-time high");
    const hint = document.getElementById(input.getAttribute("aria-describedby") as string);
    expect(hint?.textContent).not.toContain("currently off");
  });

  it("renders the off state visually as well as in the description", () => {
    setup(100);
    // `aria-hidden`, because the description already says it — announcing it twice is noise.
    const pill = screen.getByText("Off");
    expect(pill).toHaveAttribute("aria-hidden", "true");
  });

  it("has a real label bound to the control", () => {
    // docs/08 §"Accessibility & quality bar": "Every form field has a real `<label>`."
    setup(50);
    expect(screen.getByLabelText("Within % of all-time high")).toBeInTheDocument();
  });

  it("reports edits as numbers, not strings", async () => {
    const { onChange } = setup(50);
    const input = screen.getByLabelText("Within % of all-time high");
    await userEvent.clear(input);
    await userEvent.type(input, "25");
    expect(onChange).toHaveBeenCalled();
    for (const call of onChange.mock.calls) {
      expect(typeof call[0]).toBe("number");
    }
  });
});
