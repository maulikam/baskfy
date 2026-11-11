import type { MbExplorerName } from "@/lib/mb/fetch";

/** PM11 explorer (Maulik, 12 Nov 2026): filter every scored stock on several measures and rank it by either model or both. */

export type RangeFilter = "any" | "both" | "doubling" | "swing";
export type SortKey =
  | "combined"
  | "doubling"
  | "swing"
  | "symbol"
  | "sector"
  | "close"
  | "turnover"
  | "r252"
  | "ath"
  | "age"
  | "rev"
  | "pat";

export interface ExplorerFilters {
  q: string;
  sector: string;
  /** Rupees. */
  priceMin: number | null;
  priceMax: number | null;
  /** ₹ crore a day, 20-day average. */
  turnoverMinCr: number | null;
  /** Percent. */
  r252Min: number | null;
  r252Max: number | null;
  /** Within this many percent of the all-time high. */
  athWithin: number | null;
  ageMinYears: number | null;
  /** Percent, year on year. */
  revMin: number | null;
  patMin: number | null;
  range: RangeFilter;
  investableOnly: boolean;
  hi63Only: boolean;
  picksOnly: boolean;
  doublingTop: number | null;
  swingTop: number | null;
}

export const DEFAULT_FILTERS: ExplorerFilters = {
  q: "",
  sector: "",
  priceMin: null,
  priceMax: null,
  turnoverMinCr: null,
  r252Min: null,
  r252Max: null,
  athWithin: null,
  ageMinYears: null,
  revMin: null,
  patMin: null,
  range: "any",
  investableOnly: false,
  hi63Only: false,
  picksOnly: false,
  doublingTop: null,
  swingTop: null,
};

const SESSIONS_A_YEAR = 250;

function num(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

const inRange = (v: { scope: string | null } | null) => v !== null && (v.scope ?? "universe") === "universe";

/** Lower is better: the mean of the two models' rank fractions; one model's alone when the other did not score it. */
export function combinedRank(n: MbExplorerName, doublingN: number, swingN: number): number | null {
  const a = n.doubling && doublingN > 0 ? n.doubling.rank / doublingN : null;
  const b = n.swing && swingN > 0 ? n.swing.rank / swingN : null;
  if (a === null && b === null) return null;
  if (a === null) return b;
  if (b === null) return a;
  return (a + b) / 2;
}

function atLeast(v: number | null, min: number | null): boolean {
  return min === null || (v !== null && v >= min);
}

function atMost(v: number | null, max: number | null): boolean {
  return max === null || (v !== null && v <= max);
}

export function matches(n: MbExplorerName, f: ExplorerFilters): boolean {
  const q = f.q.trim().toUpperCase();
  if (q && !n.symbol.toUpperCase().includes(q) && !(n.name ?? "").toUpperCase().includes(q)) return false;
  if (f.sector && n.sector !== f.sector) return false;
  const close = num(n.close);
  if (!atLeast(close, f.priceMin) || !atMost(close, f.priceMax)) return false;
  const turnover = num(n.turnover20);
  if (!atLeast(turnover === null ? null : turnover / 1e7, f.turnoverMinCr)) return false;
  const r252 = num(n.r252_fraction);
  const r = r252 === null ? null : r252 * 100;
  if (!atLeast(r, f.r252Min) || !atMost(r, f.r252Max)) return false;
  const ath = num(n.dist_ath_fraction);
  if (f.athWithin !== null && (ath === null || ath * 100 < -f.athWithin)) return false;
  const age = n.sessions_listed === null ? null : n.sessions_listed / SESSIONS_A_YEAR;
  if (!atLeast(age, f.ageMinYears)) return false;
  const rev = num(n.ttm_rev_yoy_fraction);
  if (!atLeast(rev === null ? null : rev * 100, f.revMin)) return false;
  const pat = num(n.ttm_pat_yoy_fraction);
  if (!atLeast(pat === null ? null : pat * 100, f.patMin)) return false;
  if (f.range === "both" && !(inRange(n.doubling) && inRange(n.swing))) return false;
  if (f.range === "doubling" && !inRange(n.doubling)) return false;
  if (f.range === "swing" && !inRange(n.swing)) return false;
  if (f.investableOnly && !(n.doubling?.investable || n.swing?.investable)) return false;
  if (f.hi63Only && !n.hi63_trigger) return false;
  if (f.picksOnly && !n.swing?.picked) return false;
  if (f.doublingTop !== null && !(n.doubling && n.doubling.rank <= f.doublingTop)) return false;
  if (f.swingTop !== null && !(n.swing && n.swing.rank <= f.swingTop)) return false;
  return true;
}

/** Sort value; null sorts last whichever way the column points. */
export function sortValue(n: MbExplorerName, key: SortKey, doublingN: number, swingN: number): number | string | null {
  switch (key) {
    case "combined":
      return combinedRank(n, doublingN, swingN);
    case "doubling":
      return n.doubling?.rank ?? null;
    case "swing":
      return n.swing?.rank ?? null;
    case "symbol":
      return n.symbol;
    case "sector":
      return n.sector;
    case "close":
      return num(n.close);
    case "turnover":
      return num(n.turnover20);
    case "r252":
      return num(n.r252_fraction);
    case "ath":
      return num(n.dist_ath_fraction);
    case "age":
      return n.sessions_listed;
    case "rev":
      return num(n.ttm_rev_yoy_fraction);
    case "pat":
      return num(n.ttm_pat_yoy_fraction);
  }
}

/** The natural first direction: ranks and names ascending (best first), measures descending (biggest first). */
export function defaultAscending(key: SortKey): boolean {
  return key === "combined" || key === "doubling" || key === "swing" || key === "symbol" || key === "sector";
}

export function explore(
  names: readonly MbExplorerName[],
  f: ExplorerFilters,
  key: SortKey,
  ascending: boolean,
): MbExplorerName[] {
  const doublingN = names.filter((n) => n.doubling).length;
  const swingN = names.filter((n) => n.swing).length;
  const kept = names.filter((n) => matches(n, f));
  const dir = ascending ? 1 : -1;
  return kept
    .map((n) => ({ n, v: sortValue(n, key, doublingN, swingN) }))
    .sort((a, b) => {
      if (a.v === null && b.v === null) return a.n.symbol.localeCompare(b.n.symbol);
      if (a.v === null) return 1;
      if (b.v === null) return -1;
      const c = typeof a.v === "string" ? a.v.localeCompare(String(b.v)) : a.v - Number(b.v);
      return c === 0 ? a.n.symbol.localeCompare(b.n.symbol) : c * dir;
    })
    .map((x) => x.n);
}
