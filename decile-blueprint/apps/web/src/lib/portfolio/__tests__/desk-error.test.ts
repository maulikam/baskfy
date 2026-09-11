import { describe, expect, it } from "vitest";

import { DeskUnavailable } from "@/lib/desk/fetch";
import { readerSafeDeskError } from "@/lib/portfolio/desk-error";

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

  it("copy: an error of any other kind still gets a sentence, never an empty string", () => {
    expect(readerSafeDeskError(new TypeError("fetch failed"))).toBe(
      "The desk could not be reached.",
    );
    expect(readerSafeDeskError(undefined)).toBe("The desk could not be reached.");
  });
});
