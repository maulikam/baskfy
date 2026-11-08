import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * The two things the Market Health page has to say out loud (M33).
 *
 * Both exist because a reader drew the wrong conclusion from a page that was telling the truth:
 *
 * 1. The range selector offers 5Y whether or not five years exist. Showing "Data available from
 *    1 Aug 2025" in the header and then drawing a shorter line than the button implies reads as
 *    a broken filter — the filter was fine.
 * 2. Historical breadth is computed over today's constituents carried backwards, because NSE
 *    publishes constituents for today only and Kite has no constituents endpoint at all. That
 *    biases the past upward, and it must be on the page rather than only in `source='derived'`.
 *
 * Asserted over the source because both are prose: a refactor that drops a paragraph passes every
 * behavioural test there is.
 *
 * **M36 changed the wording of both, and this file changed with them.** The page was rewritten for
 * a reader who does not have the vocabulary, so "The selected range starts before the data does"
 * became "You asked for more history than exists" and the bias notice leads with what it means
 * ("The past here looks better than it was") before naming itself. The obligation is unchanged and
 * so is the strength of these assertions: each still pins an exact sentence, so dropping the
 * paragraph still fails. Only the sentence being pinned is new.
 */
const PAGE = readFileSync(
  // Tree 6 moved this page to the Market hub; `/market-health` is now a redirect stub.
  join(__dirname, "..", "..", "..", "app", "(app)", "market", "mood", "page.tsx"),
  "utf8",
);

describe("the Market Health page discloses what it cannot show", () => {
  it("warns when the selected range starts before the data does", () => {
    expect(PAGE).toContain("truncated");
    expect(PAGE).toMatch(/asked for more history than exists/i);
  });

  it("derives that warning from the data rather than hard-coding a date", () => {
    // A constant would start lying the day the backfill goes further back.
    expect(PAGE).toContain("health.data_available_from");
    expect(PAGE).not.toMatch(/1 Aug 2025|2025-08-01/);
  });

  it("states the survivorship bias in the reader's words, not the schema's", () => {
    // The plain sentence leads and the technical name follows it, which is M36's rule everywhere:
    // the term is never dropped, only moved out of first position.
    expect(PAGE).toMatch(/past here looks better than it was/i);
    expect(PAGE).toMatch(/survivorship bias/i);
    expect(PAGE).toMatch(/constituents for today only/i);
    expect(PAGE).toMatch(/carried\s+backwards/i);
  });

  it("says the live gauges are not affected", () => {
    // Only the history is biased. A reader who distrusts the whole page over this would be
    // discarding a number that is exactly right.
    expect(PAGE).toMatch(/gauges\s+are unaffected/i);
  });

  it("keeps both notices inside the History section", () => {
    const history = PAGE.slice(PAGE.indexOf('id="history-heading"'));
    expect(history).toMatch(/survivorship bias/i);
    expect(history).toMatch(/more history than exists/i);
  });
});
