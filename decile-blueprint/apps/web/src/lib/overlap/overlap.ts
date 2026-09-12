/**
 * Names that land on more than one Build scan on the same session.
 *
 * Symbols, not instrument ids: a reader can check each list against `/vbt`, `/twt` and the
 * screen editor, and an intersection nobody can check is not worth showing.
 *
 * An unread source and an empty source are kept different. Collapsing "the volume-breakout
 * sleeve has never run" into "0 names in common" would invent a reassuring number.
 */

/** The seeded example screen — same id the marketing sample and `/build/exmpl0000001` use. */
export const DEFAULT_SCREEN_ID = "exmpl0000001";

export interface ScreenOption {
  publicId: string;
  name: string;
}

/**
 * Resolve the screen id from the URL, falling back to the seeded example when the query is
 * absent or names a screen that is not on the list.
 */
export function pickScreenId(
  requested: string | undefined,
  screens: readonly ScreenOption[],
): string {
  if (requested && screens.some((screen) => screen.publicId === requested)) {
    return requested;
  }
  if (screens.some((screen) => screen.publicId === DEFAULT_SCREEN_ID)) {
    return DEFAULT_SCREEN_ID;
  }
  return screens[0]?.publicId ?? DEFAULT_SCREEN_ID;
}

export interface SymbolSet {
  /** Stable key for tests and React lists — `vbt`, `twt`, a screen public id. */
  key: string;
  /** What the reader sees above the count. */
  label: string;
  /**
   * `null` means the source could not be read. An empty array means it was read and named
   * nothing — those are different answers.
   */
  symbols: readonly string[] | null;
}

export interface Intersection {
  sources: readonly SymbolSet[];
  shared: string[];
  sharedCount: number;
  /** Every source answered; an unread one makes this false. */
  available: boolean;
  /** The sentence a reader sees. Always present. */
  summary: string;
}

function unique(symbols: readonly string[]): string[] {
  return [...new Set(symbols.map((symbol) => symbol.trim().toUpperCase()).filter(Boolean))].sort();
}

/**
 * Names present in every set.
 *
 * Order of `sources` is preserved in the summary so "Volume breakout and Three weeks tight"
 * reads the same way the page titles the section.
 */
export function intersectionOf(sources: readonly SymbolSet[]): Intersection {
  if (sources.length === 0) {
    return {
      sources,
      shared: [],
      sharedCount: 0,
      available: false,
      summary: "No sources were named, so there is nothing to intersect.",
    };
  }

  const unread = sources.filter((set) => set.symbols === null);
  if (unread.length > 0) {
    const names = unread.map((set) => set.label).join(", ");
    return {
      sources,
      shared: [],
      sharedCount: 0,
      available: false,
      summary:
        unread.length === sources.length
          ? `${names} could not be read, so overlap cannot be computed. This is not the same as "no overlap".`
          : `${names} could not be read, so overlap across these sources cannot be computed. This is not the same as "no overlap".`,
    };
  }

  const normalised = sources.map((set) => ({
    ...set,
    symbols: unique(set.symbols ?? []),
  }));
  const [first, ...rest] = normalised;
  if (!first) {
    return {
      sources,
      shared: [],
      sharedCount: 0,
      available: false,
      summary: "No sources were named, so there is nothing to intersect.",
    };
  }

  let shared = first.symbols;
  for (const set of rest) {
    const keep = new Set(set.symbols);
    shared = shared.filter((symbol) => keep.has(symbol));
  }

  const labels = sources.map((set) => set.label);
  const sizes = normalised.map((set) => set.symbols.length);

  return {
    sources,
    shared,
    sharedCount: shared.length,
    available: true,
    summary: intersectionSummary(labels, shared.length, sizes),
  };
}

/**
 * The sentence, worded so a zero cannot be misread as a failed read.
 */
export function intersectionSummary(
  labels: readonly string[],
  shared: number,
  sizes: readonly number[],
): string {
  const joined =
    labels.length === 1
      ? labels[0]!
      : labels.length === 2
        ? `${labels[0]} and ${labels[1]}`
        : `${labels.slice(0, -1).join(", ")}, and ${labels[labels.length - 1]}`;

  if (sizes.some((size) => size === 0)) {
    const empty = labels.filter((_, index) => sizes[index] === 0);
    return `${empty.join(" and ")} named no stocks on this session, so there is nothing to overlap.`;
  }
  if (shared === 0) {
    const sizeLine = labels
      .map((label, index) => `${sizes[index]} on ${label}`)
      .join(", ");
    return `${joined} share no stocks — ${sizeLine}, none in common.`;
  }
  if (sizes.every((size) => size === shared)) {
    return `${joined} name exactly the same ${shared} stocks.`;
  }
  return `${joined} share ${shared} stocks.`;
}
