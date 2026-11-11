import type { MbName } from "@/lib/mb/fetch";

/** PM7: what each flag on the ranking means. One symbol per fact; a name can carry several. */
export const MB_FLAGS = [
  {
    key: "investable",
    symbol: "●",
    label: "Investable",
    meaning:
      "Close at least ₹50 and at least ₹5 cr traded a day. The paper book buys only these.",
  },
  {
    key: "low_price",
    symbol: "₹",
    label: "Low price",
    meaning:
      "Close under ₹50. Below the backtest's floor; never bought by the book.",
  },
  {
    key: "thin",
    symbol: "◌",
    label: "Thinly traded",
    meaning:
      "Under ₹5 cr traded a day (20-day average). Below the floor; hard to buy or sell in size.",
  },
  {
    key: "high63",
    symbol: "▲",
    label: "63-day high",
    meaning:
      "Closed at a 63-session high in the last 21 sessions: the trigger line's entry signal.",
  },
  {
    key: "new",
    symbol: "✱",
    label: "New listing",
    meaning: "Under a year of trading history.",
  },
  {
    key: "no_results",
    symbol: "∅",
    label: "No results on file",
    meaning:
      "No quarterly revenue or profit growth on file, so those columns are blank.",
  },
] as const;

export type MbFlagKey = (typeof MB_FLAGS)[number]["key"];

const SESSIONS_A_YEAR = 250;
const MIN_CLOSE = 50;
const MIN_TURNOVER = 5e7;

export function flagsOf(n: MbName): MbFlagKey[] {
  const out: MbFlagKey[] = [];
  if (n.investable) out.push("investable");
  if (n.close !== null && Number(n.close) < MIN_CLOSE) out.push("low_price");
  if (n.turnover20 !== null && Number(n.turnover20) < MIN_TURNOVER)
    out.push("thin");
  if (n.hi63_trigger) out.push("high63");
  if (n.sessions_listed !== null && n.sessions_listed < SESSIONS_A_YEAR)
    out.push("new");
  if (n.ttm_rev_yoy_fraction === null && n.ttm_pat_yoy_fraction === null)
    out.push("no_results");
  return out;
}
