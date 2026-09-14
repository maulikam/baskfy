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

/** How many screen columns the overlap matrix shows. Three is the floor the page promises. */
export const MAX_SCREEN_COLUMNS = 3;

export interface ScreenOption {
  publicId: string;
  name: string;
  isExample: boolean;
}

/**
 * Resolve screen ids from the URL, filling up to {@link MAX_SCREEN_COLUMNS} from the list so
 * the dropdowns always have three columns when three screens exist.
 *
 * Unknown ids are ignored. When the URL leaves slots open, the person's own screens fill them
 * first — in the list's order, which the API sorts most recently updated first — then Investing
 * 001, then the other templates. Someone who built a screen came to see it against the scans,
 * not a seeded example.
 */
export function pickScreenIds(
  requested: readonly string[] | undefined,
  screens: readonly ScreenOption[],
  max: number = MAX_SCREEN_COLUMNS,
): string[] {
  const available = screens.map((screen) => screen.publicId);
  const chosen: string[] = [];
  for (const id of requested ?? []) {
    if (available.includes(id) && !chosen.includes(id)) {
      chosen.push(id);
    }
    if (chosen.length >= max) return chosen;
  }
  const mine = screens.filter((screen) => !screen.isExample).map((screen) => screen.publicId);
  const templates = screens.filter((screen) => screen.isExample).map((screen) => screen.publicId);
  const preferred = [
    ...mine,
    ...templates.filter((id) => id === DEFAULT_SCREEN_ID),
    ...templates.filter((id) => id !== DEFAULT_SCREEN_ID),
  ];
  for (const id of preferred) {
    if (!chosen.includes(id)) chosen.push(id);
    if (chosen.length >= max) break;
  }
  return chosen;
}

export interface SymbolSet {
  /** Stable key for tests and React lists — `vbt`, `twt`, `swing`, a screen public id. */
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

export interface MembershipColumn {
  key: string;
  label: string;
  available: boolean;
  size: number;
}

export interface MembershipRow {
  symbol: string;
  sourceKeys: readonly string[];
  count: number;
}

export interface Membership {
  columns: readonly MembershipColumn[];
  rows: MembershipRow[];
  /** At least one source answered. An unread source is omitted from the columns that count. */
  available: boolean;
  summary: string;
}

export type MembershipView = "shared" | "three" | "screen" | "all";

/** The three strategy columns. They stay first in the overlap table, always, in this order. */
export const SLEEVE_COLUMN_KEYS = ["vbt", "twt", "swing"] as const;
export type SleeveColumnKey = (typeof SLEEVE_COLUMN_KEYS)[number];

export function isSleeveColumnKey(key: string): key is SleeveColumnKey {
  return (SLEEVE_COLUMN_KEYS as readonly string[]).includes(key);
}

/**
 * Volume, Tight, Swing first — even when a sleeve could not be read, and even when the caller
 * passed screen columns ahead of them. Unread screens still follow, so a failed sleeve cannot
 * let a screen steal the first slot.
 */
export function overlapTableColumns(
  columns: readonly MembershipColumn[],
): MembershipColumn[] {
  const byKey = new Map(columns.map((column) => [column.key, column]));
  const sleeves: MembershipColumn[] = [];
  for (const key of SLEEVE_COLUMN_KEYS) {
    const column = byKey.get(key);
    if (column) sleeves.push(column);
  }
  const rest = columns.filter((column) => !isSleeveColumnKey(column.key));
  return [...sleeves, ...rest];
}

function unique(symbols: readonly string[]): string[] {
  return [...new Set(symbols.map((symbol) => symbol.trim().toUpperCase()).filter(Boolean))].sort();
}

function joinLabels(labels: readonly string[]): string {
  if (labels.length === 0) return "";
  if (labels.length === 1) return labels[0]!;
  if (labels.length === 2) return `${labels[0]} and ${labels[1]}`;
  return `${labels.slice(0, -1).join(", ")}, and ${labels[labels.length - 1]}`;
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
 * Every name on any readable source, tagged with which sources named it.
 *
 * An unread source is not a zero: it is omitted from the columns, and the summary says so.
 * That is the table the overlap page needs so a screen stock can sit next to Swing / Volume /
 * Tight instead of only appearing in one hard-coded triple.
 */
export function membershipOf(sources: readonly SymbolSet[]): Membership {
  const columns: MembershipColumn[] = sources.map((set) => ({
    key: set.key,
    label: set.label,
    available: set.symbols !== null,
    size: set.symbols === null ? 0 : unique(set.symbols).length,
  }));

  const readable = sources.filter((set) => set.symbols !== null);
  if (readable.length === 0) {
    return {
      columns,
      rows: [],
      available: false,
      summary: membershipSummary(columns, []),
    };
  }

  const bySymbol = new Map<string, Set<string>>();
  for (const set of readable) {
    for (const symbol of unique(set.symbols ?? [])) {
      const keys = bySymbol.get(symbol) ?? new Set<string>();
      keys.add(set.key);
      bySymbol.set(symbol, keys);
    }
  }

  const rows: MembershipRow[] = [...bySymbol.entries()]
    .map(([symbol, keys]) => {
      const sourceKeys = [...keys].sort();
      return { symbol, sourceKeys, count: sourceKeys.length };
    })
    .sort((a, b) => b.count - a.count || a.symbol.localeCompare(b.symbol));

  return {
    columns,
    rows,
    available: true,
    summary: membershipSummary(columns, rows),
  };
}

export function filterMembershipRows(
  rows: readonly MembershipRow[],
  view: MembershipView,
  screenKeys: readonly string[],
): MembershipRow[] {
  switch (view) {
    case "all":
      return [...rows];
    case "shared":
      return rows.filter((row) => row.count >= 2);
    case "three":
      return rows.filter((row) => row.count >= 3);
    case "screen":
      if (screenKeys.length === 0) return [];
      return rows.filter((row) => screenKeys.some((key) => row.sourceKeys.includes(key)));
  }
}

export function membershipSummary(
  columns: readonly MembershipColumn[],
  rows: readonly MembershipRow[],
): string {
  const unread = columns.filter((column) => !column.available);
  const readable = columns.filter((column) => column.available);
  const unreadNote =
    unread.length === 0
      ? ""
      : ` ${joinLabels(unread.map((column) => column.label))} could not be read, so those columns are omitted.`;

  if (readable.length === 0) {
    return `${joinLabels(unread.map((column) => column.label))} could not be read, so overlap cannot be computed. This is not the same as "no overlap".`;
  }
  if (rows.length === 0) {
    return `The readable scans named no stocks on this session.${unreadNote}`;
  }

  const shared = rows.filter((row) => row.count >= 2).length;
  const named = joinLabels(readable.map((column) => column.label));
  if (shared === 0) {
    return `${rows.length} ${rows.length === 1 ? "name" : "names"} across ${named}, none on more than one.${unreadNote}`;
  }
  return `${shared} ${shared === 1 ? "name sits" : "names sit"} on at least two of ${named} (${rows.length} named in all).${unreadNote}`;
}

/**
 * The sentence, worded so a zero cannot be misread as a failed read.
 */
export function intersectionSummary(
  labels: readonly string[],
  shared: number,
  sizes: readonly number[],
): string {
  const joined = joinLabels(labels);

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
