/** The fields the flags read; a full ranking row and a Build-table lookup both have them. */
export interface MbVerdict {
  rank: number;
  score: string;
  investable: boolean;
  hi63_trigger: boolean;
  hi63_trigger_date: string | null;
  close: string | null;
  turnover20: string | null;
  sessions_listed: number | null;
  ttm_rev_yoy_fraction: string | null;
  ttm_pat_yoy_fraction: string | null;
}

/** PM7: what each flag on the ranking means. One symbol per fact; a name can carry several. */
export const MB_FLAGS = [
  {
    key: "investable",
    tone: "text-emerald-600 dark:text-emerald-400",
    symbol: "●",
    label: "Investable",
    meaning:
      "Close at least ₹50 and at least ₹5 cr traded a day. The paper record buys only these.",
  },
  {
    key: "low_price",
    tone: "text-red-600 dark:text-red-400",
    symbol: "₹",
    label: "Low price",
    meaning:
      "Close under ₹50. Below the backtest's floor; never bought on paper.",
  },
  {
    key: "thin",
    tone: "text-orange-500 dark:text-orange-400",
    symbol: "◑",
    label: "Thinly traded",
    meaning:
      "Under ₹5 cr traded a day (20-day average). Below the floor; hard to buy or sell in size.",
  },
  {
    key: "high63",
    tone: "text-sky-600 dark:text-sky-400",
    symbol: "▲",
    label: "63-day high",
    meaning:
      "Closed at a 63-session high in the last 21 sessions: the trigger line's entry signal.",
  },
  {
    key: "new",
    tone: "text-violet-600 dark:text-violet-400",
    symbol: "✱",
    label: "Young listing",
    meaning: "Under two years of trading history (the model ranks nothing with under about a year).",
  },
  {
    key: "no_results",
    tone: "text-amber-600 dark:text-amber-400",
    symbol: "∅",
    label: "No results on file",
    meaning:
      "No quarterly revenue or profit growth on file, so those columns are blank.",
  },
  {
    key: "not_scored",
    tone: "text-slate-400 dark:text-slate-500",
    symbol: "⊘",
    label: "Not ranked",
    meaning:
      "Outside the model's universe: under ₹20, under ₹2 cr traded a day, under 260 sessions listed, not an equity, or within 60 sessions of an unexplained price jump.",
  },
] as const;

export type MbFlagKey = (typeof MB_FLAGS)[number]["key"];

const YOUNG_SESSIONS = 500;
const MIN_CLOSE = 50;
const MIN_TURNOVER = 5e7;

export function flagsOf(n: MbVerdict | null | undefined): MbFlagKey[] {
  if (!n) return ["not_scored"];
  const out: MbFlagKey[] = [];
  if (n.investable) out.push("investable");
  if (n.close !== null && Number(n.close) < MIN_CLOSE) out.push("low_price");
  if (n.turnover20 !== null && Number(n.turnover20) < MIN_TURNOVER)
    out.push("thin");
  if (n.hi63_trigger) out.push("high63");
  if (n.sessions_listed !== null && n.sessions_listed < YOUNG_SESSIONS)
    out.push("new");
  if (n.ttm_rev_yoy_fraction === null && n.ttm_pat_yoy_fraction === null)
    out.push("no_results");
  return out;
}

export type SwingScope = "universe" | "quarantine" | "young" | "sme" | "cheap" | "thin";

/** PM11.1: why a swing score is outside the model's training universe (it is still scored, never picked). */
export const SWING_SCOPE: Record<Exclude<SwingScope, "universe">, { label: string; meaning: string }> = {
  quarantine: {
    label: "Recent price jump",
    meaning: "Within 60 sessions of a one-day move over 60% (usually a split or bonus). Scored, but less reliable and never picked.",
  },
  young: {
    label: "Young listing",
    meaning: "Under 260 sessions of history, so the one-year inputs are missing. Scored, but less reliable and never picked.",
  },
  sme: {
    label: "SME or special series",
    meaning: "Not in the EQ/BE/BZ series the model was trained on. Scored, but less reliable and never picked.",
  },
  cheap: {
    label: "Under ₹20",
    meaning: "Below the model's training floor of ₹20. Scored, but less reliable and never picked.",
  },
  thin: {
    label: "Thinly traded",
    meaning: "Under ₹2 cr traded a day, below the model's training floor. Scored, but less reliable and never picked.",
  },
};
