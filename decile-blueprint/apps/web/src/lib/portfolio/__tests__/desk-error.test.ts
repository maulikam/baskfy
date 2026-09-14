import { describe, expect, it, vi } from "vitest";

/* `lib/desk/fetch` reads the session for its bearer token; the class under test needs none. */
vi.mock("@/lib/auth", () => ({ auth: () => Promise.resolve(null) }));

import { DeskUnavailable } from "@/lib/desk/fetch";
import { readerSafeDeskError } from "@/lib/portfolio/desk-error";
import { DESK_NO_EVALUATION, regimeReading } from "@/lib/portfolio/regime";

describe("what a reader is told when the desk is down", () => {
  it("copy: the transport's own message never becomes the reader's", () => {
    /* The exact string that shipped to the screen, from a real 404 against the local API. */
    const raw = new DeskUnavailable(
      "http://127.0.0.1:8100/api/v1/desk/regime responded 404",
    );
    const shown = readerSafeDeskError(raw);

    expect(shown).toBe("The desk did not answer.");
    expect(shown).not.toMatch(/https?:\/\//);
    expect(shown).not.toMatch(/\/api\/v\d/);
    expect(shown).not.toMatch(/\d{4}/);
  });

  it("copy: the API's 404 means no evaluation was ever recorded, not an outage", () => {
    expect(
      readerSafeDeskError(
        new DeskUnavailable("http://api/desk/regime responded 404", 404),
      ),
    ).toBe(DESK_NO_EVALUATION);
    expect(
      regimeReading({
        regime: null,
        today: null,
        unavailableReason: DESK_NO_EVALUATION,
      }),
    ).toMatchObject({
      kind: "unavailable",
      notice: { id: "desk-no-evaluation", severity: "info" },
    });
    expect(
      regimeReading({
        regime: null,
        today: null,
        unavailableReason: "The desk did not answer.",
      }),
    ).toMatchObject({ notice: { severity: "critical" } });
  });

  it("copy: an error of any other kind still gets a sentence, never an empty string", () => {
    expect(readerSafeDeskError(new TypeError("fetch failed"))).toBe(
      "The desk could not be reached.",
    );
    expect(readerSafeDeskError(undefined)).toBe(
      "The desk could not be reached.",
    );
  });
});
