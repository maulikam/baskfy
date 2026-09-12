import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TightNames } from "@/components/twt/tight-names";

/**
 * Two different absences, which the panel stated as one until 12 Sep 2026.
 *
 * On the day TWT first reached the box, `tw_breadth_daily`, `tw_signal_daily` and `tw_state_daily`
 * were all empty — the detector had never run there. The page rendered *"No name held the pattern
 * on this session"*, which asserts a session was read and nothing matched. Nothing had been read.
 * Two panels above, the gate card correctly said "Not read yet".
 *
 * An empty table is not evidence about the market unless something looked at the market. These two
 * tests are what stop the branch collapsing back into one sentence.
 */
describe("the quiet-for-three-weeks panel tells the two absences apart", () => {
  it("says nothing has looked yet when no session has been read", () => {
    render(<TightNames rows={[]} session={null} />);

    const unread = screen.getByTestId("twt-tight-unread");
    expect(unread).toBeInTheDocument();
    expect(unread.textContent).toMatch(/no session has been read/i);
    // It must NOT claim anything about what the pattern did, because nothing looked.
    expect(unread.textContent).not.toMatch(/held the pattern on this session/i);
    expect(screen.queryByTestId("twt-tight-empty")).not.toBeInTheDocument();
  });

  it("says no name qualified when a session was read and none did", () => {
    render(<TightNames rows={[]} session="2026-09-10" />);

    const empty = screen.getByTestId("twt-tight-empty");
    expect(empty).toBeInTheDocument();
    expect(empty.textContent).toMatch(/no name held the pattern on this session/i);
    expect(screen.queryByTestId("twt-tight-unread")).not.toBeInTheDocument();
  });

  it("the two states never render together", () => {
    for (const session of [null, "2026-09-10"] as const) {
      const { unmount } = render(<TightNames rows={[]} session={session} />);
      const shown = [
        screen.queryByTestId("twt-tight-unread"),
        screen.queryByTestId("twt-tight-empty"),
      ].filter(Boolean);
      expect(shown).toHaveLength(1);
      unmount();
    }
  });
});
