/**
 * How much two baskets are the same basket.
 *
 * This is the question the brief calls "portfolio overlap", and it is the most useful thing a
 * comparison can say that a table of returns cannot: two momentum baskets that share twenty of
 * twenty-five holdings are one bet wearing two names, and a reader about to buy both is
 * concentrating rather than diversifying. Every basket in this catalogue is momentum, so it is
 * not a hypothetical here.
 *
 * Symbols, not instrument ids: the reader can check the answer against the holdings list on each
 * basket's own page, and a comparison nobody can check is a comparison nobody should trust.
 */

/** One basket's holdings, or an honest `null` when they could not be read. */
export interface HoldingSet {
  slug: string;
  name: string;
  /** `null` means "we could not read this basket's holdings", never "it holds nothing". */
  symbols: readonly string[] | null;
}

export interface OverlapPair {
  a: HoldingSet;
  b: HoldingSet;
  shared: string[];
  /** Shared count, and each basket's own size, so "14 of 25" can be said from either side. */
  sharedCount: number;
  countA: number;
  countB: number;
  /** Shared ÷ union. 1 means identical holdings, 0 means nothing in common. */
  jaccard: number;
  available: boolean;
  /** The sentence a reader sees. Always present, including when nothing could be computed. */
  summary: string;
}

function unique(symbols: readonly string[]): string[] {
  return [...new Set(symbols.map((symbol) => symbol.trim().toUpperCase()).filter(Boolean))];
}

/**
 * Overlap between exactly two baskets.
 *
 * An empty holdings list and an unreadable one are different answers and are kept different: a
 * basket that genuinely holds nothing shares nothing, while a basket whose holdings could not be
 * read supports no conclusion at all. Collapsing the second into "0% overlap" would be inventing
 * a reassuring number, which is the one thing a comparison must never do.
 */
export function overlapOf(a: HoldingSet, b: HoldingSet): OverlapPair {
  if (a.symbols === null || b.symbols === null) {
    const unread = [a, b].filter((set) => set.symbols === null).map((set) => set.name);
    return {
      a,
      b,
      shared: [],
      sharedCount: 0,
      countA: 0,
      countB: 0,
      jaccard: 0,
      available: false,
      summary: `Holdings for ${unread.join(" and ")} could not be read, so overlap cannot be computed. This is not the same as "no overlap".`,
    };
  }

  const left = unique(a.symbols);
  const right = unique(b.symbols);
  const rightSet = new Set(right);
  const shared = left.filter((symbol) => rightSet.has(symbol)).sort();
  const union = new Set([...left, ...right]).size;

  return {
    a,
    b,
    shared,
    sharedCount: shared.length,
    countA: left.length,
    countB: right.length,
    jaccard: union === 0 ? 0 : shared.length / union,
    available: true,
    summary: overlapSummary(a.name, b.name, shared.length, left.length, right.length),
  };
}

/**
 * The sentence, worded so the number cannot be misread.
 *
 * "They share 14 stocks" is ambiguous when the two baskets are different sizes — fourteen of
 * twenty-five is very different from fourteen of fifteen — so both denominators are always said.
 */
export function overlapSummary(
  nameA: string,
  nameB: string,
  shared: number,
  countA: number,
  countB: number,
): string {
  if (countA === 0 || countB === 0) {
    const empty = countA === 0 ? nameA : nameB;
    return `${empty} has no holdings recorded, so there is nothing to overlap.`;
  }
  if (shared === 0) {
    return `${nameA} and ${nameB} share no holdings — ${countA} and ${countB} stocks, none in common.`;
  }
  if (shared === countA && shared === countB) {
    return `${nameA} and ${nameB} hold exactly the same ${shared} stocks. Holding both concentrates rather than diversifies.`;
  }
  return `${nameA} and ${nameB} share ${shared} stocks — ${shared} of ${nameA}'s ${countA} and ${shared} of ${nameB}'s ${countB}.`;
}

/** Every pair among two or three baskets, in the order they were selected. */
export function overlapMatrix(sets: readonly HoldingSet[]): OverlapPair[] {
  const pairs: OverlapPair[] = [];
  for (let i = 0; i < sets.length; i += 1) {
    for (let j = i + 1; j < sets.length; j += 1) {
      pairs.push(overlapOf(sets[i]!, sets[j]!));
    }
  }
  return pairs;
}

/**
 * The strongest overlap among the selected baskets, which is the one worth leading with.
 *
 * Returns `null` when nothing could be computed — the caller then says why rather than showing
 * a zero.
 */
export function strongestOverlap(pairs: readonly OverlapPair[]): OverlapPair | null {
  const usable = pairs.filter((pair) => pair.available && pair.sharedCount > 0);
  if (usable.length === 0) return null;
  return usable.reduce((best, pair) => (pair.jaccard > best.jaccard ? pair : best));
}
