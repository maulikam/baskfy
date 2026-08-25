import type { FactorOut, ScreenDefinition, ScreenRunResponse, UniverseOut } from "@baskfy/api-client";

import { columnDisplayLabel } from "@/lib/screens/column-display";
import { formatFraction, formatPercent, formatTradeDate } from "@/lib/format";

/** Plain-language universe phrases for the story strip (§1.2). */
const UNIVERSE_PHRASE: Readonly<Record<string, string>> = {
  "nifty-50": "Nifty 50",
  "nifty-100": "Nifty 100",
  "nifty-200": "Nifty 200",
  "nifty-500": "Nifty 500",
  "nifty-midcap-150": "Nifty Midcap 150",
  "nifty-smallcap-250": "Nifty Smallcap 250",
  "nifty-microcap-250": "Nifty Microcap 250",
  "nifty-total-market": "the whole market",
  "nifty-allcap": "the all-cap market",
};

/** Ranking phrases keyed by common sort_by factors. */
const RANK_PHRASE: Readonly<Record<string, string>> = {
  avg_sharpe_12_6_3_1:
    "how steadily they've beaten their own risk over the last year",
  sharpe_12m: "how much return they've earned per unit of bumpiness over a year",
  ret_12m: "their absolute 1-year price return",
  vol_12m: "how bumpy their last year of trading was",
};

export function universePhrase(slug: string, universes: readonly UniverseOut[]): string {
  const known = UNIVERSE_PHRASE[slug];
  if (known) return known;
  const match = universes.find((u) => u.slug === slug);
  if (match) return match.name.replace(/^NIFTY\s+/i, "Nifty ");
  return slug;
}

export function rankingPhrase(sortBy: string, factors: readonly FactorOut[]): string {
  const known = RANK_PHRASE[sortBy];
  if (known) return known;
  const factor = factors.find((f) => f.key === sortBy);
  const label = columnDisplayLabel(sortBy, factor?.label ?? sortBy).toLowerCase();
  return `their ${label}`;
}

/**
 * One-line plain-language sentence from the current config + result count (§1.2).
 */
export function buildStorySentence(options: {
  definition: ScreenDefinition;
  resultCount: number | null | undefined;
  universes: readonly UniverseOut[];
  factors: readonly FactorOut[];
}): string {
  const { definition, resultCount, universes, factors } = options;
  const count = resultCount ?? 0;
  const noun = count === 1 ? "stock" : "stocks";
  const universe = universePhrase(definition.index, universes);
  const ranking = rankingPhrase(definition.sort_by, factors);
  const direction =
    definition.sort_direction === "asc" ? "from lowest to highest" : "ranked by";

  return `${count} ${noun} from ${universe}, ${direction} ${ranking}.`;
}

export interface StoryStats {
  topPickSymbol: string | null;
  topPickScore: string | null;
  bestReturnText: string | null;
  medianVolText: string | null;
  asOfText: string;
}

function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 0) {
    return ((sorted[mid - 1] as number) + (sorted[mid] as number)) / 2;
  }
  return sorted[mid] as number;
}

function numericCell(row: Record<string, unknown>, key: string): number | null {
  const raw = row[key];
  if (typeof raw === "number" && Number.isFinite(raw)) return raw;
  if (typeof raw === "string" && raw !== "") {
    const n = Number(raw);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

/** Derive 3–4 story tiles from an existing preview payload — no new API. */
export function computeStoryStats(result: ScreenRunResponse | undefined): StoryStats {
  const asOfText = formatTradeDate(result?.as_of ?? null);
  if (!result || result.rows.length === 0) {
    return {
      topPickSymbol: null,
      topPickScore: null,
      bestReturnText: null,
      medianVolText: null,
      asOfText,
    };
  }

  const top = result.rows[0] as Record<string, unknown>;
  const topSymbol = typeof top.symbol === "string" ? top.symbol : null;
  const scoreRaw = numericCell(top, "sorting_factor");
  const topPickScore =
    scoreRaw === null ? null : scoreRaw.toLocaleString("en-IN", { maximumFractionDigits: 2 });

  let bestReturn: number | null = null;
  const vols: number[] = [];
  for (const row of result.rows) {
    const record = row as Record<string, unknown>;
    const ret = numericCell(record, "ret_12m");
    if (ret !== null && (bestReturn === null || ret > bestReturn)) bestReturn = ret;
    const vol = numericCell(record, "vol_12m");
    if (vol !== null) vols.push(vol);
  }

  const medVol = median(vols);

  return {
    topPickSymbol: topSymbol,
    topPickScore,
    bestReturnText: bestReturn === null ? null : formatPercent(bestReturn),
    medianVolText: medVol === null ? null : formatFraction(medVol),
    asOfText,
  };
}
