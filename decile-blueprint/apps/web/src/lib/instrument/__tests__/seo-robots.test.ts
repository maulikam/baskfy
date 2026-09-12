import { describe, expect, it } from "vitest";

import { instrumentMetadata } from "@/lib/instrument/seo";
import type { FactsheetOut } from "@baskfy/api-client";

/**
 * AUDIT 4.8 — instrument pages redirect crawlers to /login, so they must not claim index: true.
 */
function sheet(): FactsheetOut {
  return {
    symbol: "RELIANCE",
    as_of: "2026-09-11",
    header: { name: "Reliance", exchange: "NSE", isin: null, sector: null, industry: null },
    returns: [{ key: "ret_12m", label: "1Y", value: "10" }],
    sharpe_returns: [],
    volatility: [],
    rsi: [],
    index_memberships: [],
  } as unknown as FactsheetOut;
}

describe("instrumentMetadata robots", () => {
  it("sets index: false because the page is behind login", () => {
    const meta = instrumentMetadata(sheet());
    expect(meta.robots).toEqual({ index: false, follow: false });
  });
});
