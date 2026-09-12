import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Agent C's audit, the web half: **a sleeve page cannot tell "this book is not yours" from
 * "this book is empty".**
 *
 * Every sleeve's `fetch.ts` wraps its reads in the same `readOrNull`, and every non-OK
 * response — a
 * 404 from `scoped_sole_user_id` refusing a second account, a 503 when the sole tenant is not
 * configured, a 500 — becomes `null`, and every page renders `null` as its empty state. The
 * swing hub's is "No flags today"; the TWT hub's is the sentence this whole audit started from,
 * *"Nothing has been read for this strategy yet."*
 *
 * That is the same silent blindness as the TWT read-path bug, arriving through the **user** axis
 * rather than the date axis. The writer wrote — 157 `sw_setup_daily` rows for user 1 on the box —
 * and a signed-in second account (user 6 has a portfolio there) is told the strategy has never
 * run. Nothing errors, nothing is logged on the page's side, and the reader believes it.
 *
 * This test asserts the contract that makes the two cases separable: a **refusal** must not
 * arrive at the page as the same value that means **"nothing has been written yet"**. It fails
 * today, and that is the point — see `gates/sleeve-read-contract.md` C7.
 */

const ORIGIN = "http://api.test";

vi.mock("@/lib/auth", () => ({
  auth: () => Promise.resolve({ accessToken: "test-token" }),
}));

vi.mock("@/lib/api/config", () => ({
  serverApiOrigin: () => ORIGIN,
}));

describe("the swing hub's reads distinguish a refusal from an empty book", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("answers null when the detector has genuinely written nothing", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(Response.json({ as_of: null, data: [], funnel: null }, { status: 200 })),
      ),
    );
    const { fetchSetups } = await import("@/lib/swing/fetch");
    const page = await fetchSetups({});
    expect(page).not.toBeNull();
    expect(page?.as_of).toBeNull();
  });

  it("does not answer the same thing when the API refuses the caller", async () => {
    /* AGENT F, 12 Sep 2026 — THE BODY BELOW CHANGED AND THE ASSERTION DID NOT.
     *
     * Agent C wrote this stub as the refusal looked on 12 Sep: `No watchlist with id '6'.`, a
     * bare `not-found`. Two things about that were the bug, and both are fixed in the same pass
     * as this test: `curated_tenant.scoped_sole_user_id` named the watchlist whatever surface it
     * was guarding (C7's first half), and the refusal carried **nothing a reader could key on**.
     *
     * It cannot key on the status. The refusal is deliberately a 404 rather than a 403 — M43.4,
     * "a 403 would confirm that the surface holds somebody's data" — and 404 is also the honest
     * answer to `/swing/bars?instrument_id=…` for an instrument that does not exist. A rule of
     * "every 404 is visible" would turn those into a broken page, which is a different lie.
     *
     * So the API marks the refusal with an RFC 9457 extension member,
     * `baskfy_api.curated_tenant.SOLE_TENANT_REFUSED`, and this stub is that response. The
     * assertion below is Agent C's, unchanged and un-weakened; `a_plain_404_is_still_nothing`
     * underneath is the control that says this is precision rather than a loosened net.
     */
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          Response.json(
            {
              type: "not-found",
              title: "Not found",
              status: 404,
              detail:
                "This deployment serves a single account and this caller (user 6) is not it.",
              reason: "not-the-sole-tenant",
            },
            { status: 404 },
          ),
        ),
      ),
    );
    const { fetchSetups } = await import("@/lib/swing/fetch");

    /* A refusal must be visible to the caller: either it throws, or it comes back as something
       the page can render as "this is not your book" / "the sleeve is unavailable". `null` is
       already spoken for — it is how the page says the detector has not run. */
    let refusalWasVisible: boolean;
    try {
      const page = await fetchSetups({});
      refusalWasVisible = page !== null;
    } catch {
      refusalWasVisible = true;
    }

    expect(
      refusalWasVisible,
      "a 404 from scoped_sole_user_id arrives at the page as null — exactly the value that " +
        "means 'the detector has never run'. So a signed-in account that is not the sole " +
        "tenant is shown an empty-state page over a database holding the session, and nothing " +
        "anywhere says it was refused.",
    ).toBe(true);
  });

  it("does not answer the same thing when the sole tenant is not configured", async () => {
    /* `resolve_sole_user_id` answers 503 `pipeline-degraded` on a deployment with no
       BASKFY_SOLE_USER_ID and no seeded e2e account. That is an operator's alarm, and the page
       currently renders it as "no flags today". */
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          Response.json(
            { type: "pipeline-degraded", status: 503, detail: "sole tenant not configured" },
            { status: 503 },
          ),
        ),
      ),
    );
    const { fetchSetups } = await import("@/lib/swing/fetch");

    let degradationWasVisible: boolean;
    try {
      const page = await fetchSetups({});
      degradationWasVisible = page !== null;
    } catch {
      degradationWasVisible = true;
    }

    expect(
      degradationWasVisible,
      "a 503 'the sole tenant is not configured' is rendered as an ordinary quiet day",
    ).toBe(true);
  });

  it("still answers null for a plain 404 — an instrument that does not exist", async () => {
    /* The control for the change above. `/swing/setups/{id}/bars` for an instrument nobody
     * detected is a 404 that genuinely means "there is nothing here", and it must keep answering
     * null: a page that threw on it would turn an ordinary absence into a broken screen. Only
     * the marked refusal is visible. */
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          Response.json(
            {
              type: "not-found",
              title: "Not found",
              status: 404,
              detail: "No instrument with id '9'.",
            },
            { status: 404 },
          ),
        ),
      ),
    );
    const { fetchBars } = await import("@/lib/swing/fetch");
    await expect(fetchBars(9)).resolves.toBeNull();
  });
});
