import { describe, expect, it } from "vitest";

import {
  ACCEPT_ALL,
  CONSENT_VERSION,
  ESSENTIAL_ONLY,
  effectiveConsent,
  isAllowed,
  parseConsent,
  serialiseConsent,
} from "@/lib/consent/state";

/**
 * Prompt 18 deliverable 5 — "a cookie/consent banner that **defaults to essential-only**".
 *
 * The whole of this module is one rule: nothing outside `essential` is true unless a visitor said
 * so. The tests below are mostly about the ways that rule could quietly stop holding — a corrupt
 * cookie, a cookie from an older notice, a truthy-but-not-`true` value — because each of those
 * would fail *open* if the parsing were naive, and a consent record that fails open is worse than
 * no consent record at all.
 */
describe("no stored choice means essential-only", () => {
  it.each([undefined, null, "", "not json", "%7Bbroken", "[]", '"a string"', "null"])(
    "reads %s as no choice",
    (raw) => {
      expect(parseConsent(raw)).toBeNull();
      expect(effectiveConsent(raw)).toEqual(ESSENTIAL_ONLY);
    },
  );

  it("treats a record from an older notice as no choice, so the visitor is asked again", () => {
    const stale = encodeURIComponent(
      JSON.stringify({ version: CONSENT_VERSION - 1, essential: true, analytics: true }),
    );
    expect(parseConsent(stale)).toBeNull();
    expect(isAllowed(stale, "analytics")).toBe(false);
  });

  it("never infers consent from a truthy value that is not `true`", () => {
    /* `"yes"`, `1` and `"false"` are all truthy in JavaScript. A `=== true` check is the only
       reading that cannot be talked into a permission. */
    for (const value of ["yes", 1, "false", {}, []]) {
      const raw = encodeURIComponent(
        JSON.stringify({ version: CONSENT_VERSION, essential: true, analytics: value }),
      );
      expect(isAllowed(raw, "analytics"), JSON.stringify(value)).toBe(false);
    }
  });
});

describe("a stored choice is honoured", () => {
  it("round-trips essential-only", () => {
    expect(parseConsent(serialiseConsent(ESSENTIAL_ONLY))).toEqual(ESSENTIAL_ONLY);
  });

  it("round-trips accept-all", () => {
    expect(parseConsent(serialiseConsent(ACCEPT_ALL))).toEqual(ACCEPT_ALL);
  });

  it("grants only what was granted", () => {
    const raw = serialiseConsent({ ...ESSENTIAL_ONLY, preferences: true });
    expect(isAllowed(raw, "preferences")).toBe(true);
    expect(isAllowed(raw, "analytics")).toBe(false);
  });
});

describe("essential can never be refused", () => {
  it("is true even for a visitor who has chosen nothing", () => {
    expect(isAllowed(undefined, "essential")).toBe(true);
    expect(ESSENTIAL_ONLY.essential).toBe(true);
  });

  it("is true even if a stored record tried to say otherwise", () => {
    const raw = encodeURIComponent(
      JSON.stringify({ version: CONSENT_VERSION, essential: false, analytics: false }),
    );
    expect(isAllowed(raw, "essential")).toBe(true);
    expect(parseConsent(raw)?.essential).toBe(true);
  });
});
