import { describe, expect, it } from "vitest";

import { mergeMbScores, parseMbScores } from "@/lib/mb/scores";

const part = (symbol: string, asOf: string | null) =>
  parseMbScores({
    as_of: asOf,
    universe_n: asOf ? 1221 : null,
    names: [],
    swing_as_of: asOf,
    swing_universe_n: 3107,
    swing_switch_on: true,
    swing: [{ symbol, rank: 1, score: "0.2", investable: true, picked: false }],
  });

describe("a table bigger than one lookup (Maulik, 12 Nov 2026: names past the 500th were ⊘)", () => {
  it("keeps every batch's names, not only the first 500", () => {
    const merged = mergeMbScores([part("CUPID", "2026-11-11"), part("SBC", null)]);
    expect(Object.keys(merged.swing.bySymbol).sort()).toEqual(["CUPID", "SBC"]);
    expect(merged.asOf).toBe("2026-11-11");
    expect(merged.loaded).toBe(true);
  });
});
