import { describe, expect, it } from "vitest";

import { isSessionRevoked } from "@/lib/auth/session-epoch";

/**
 * The spec of the only comparison that can end a web session (`NEEDS-MAULIK.md` §22).
 *
 * Each case here is a decision that could reasonably have gone the other way, which is why they
 * are pinned: a later reader looking at `current > (stamped ?? 0)` sees an off-by-one waiting to
 * be "fixed" into `!==`, and the cost of that fix is either a userbase signed out on deploy or a
 * revoked session that keeps rendering.
 */
describe("isSessionRevoked", () => {
  it("lets a session through when it carries the generation the server expects", () => {
    expect(isSessionRevoked({ stamped: 4, current: 4 })).toBe(false);
  });

  it("revokes a session the server has moved past", () => {
    /* The case the whole mechanism exists for: a password change bumped the generation, and the
       cookie the attacker is holding was stamped before it. */
    expect(isSessionRevoked({ stamped: 4, current: 5 })).toBe(true);
  });

  it("revokes a session left behind by several bumps, not just the next one", () => {
    expect(isSessionRevoked({ stamped: 1, current: 9 })).toBe(true);
  });

  it("treats a session issued before the epoch shipped as generation zero", () => {
    /* Migration 0025 backfilled every existing account to 0. Reading a missing stamp as anything
       else would sign the entire userbase out on the deploy that adds this. */
    expect(isSessionRevoked({ stamped: undefined, current: 0 })).toBe(false);
  });

  it("still revokes an unstamped session once the account's generation has moved", () => {
    /* The other half of the grandfathering rule: old cookies are tolerated, not exempt. */
    expect(isSessionRevoked({ stamped: undefined, current: 1 })).toBe(true);
  });

  it("does not evict a session whose stamp is ahead of the server", () => {
    /* Only a rolled-back database or a lagging read replica produces this. Signing the user out
       would be indistinguishable, to them, from the bug this mechanism exists to fix. */
    expect(isSessionRevoked({ stamped: 7, current: 6 })).toBe(false);
  });
});
