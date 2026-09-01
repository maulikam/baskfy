/**
 * Human display names for screen result columns (§1.3).
 *
 * The API and factor registry keep the technical labels; this map is the UI contract for what
 * appears in headers and tooltips on the screen editor.
 */
export interface ColumnDisplay {
  readonly label: string;
  readonly tooltip: string;
}

export const COLUMN_DISPLAY: Readonly<Record<string, ColumnDisplay>> = {
  avg_sharpe_12_6_3_1: {
    label: "Consistency score",
    tooltip: "Avg. Sharpe of 12/6/3/1-month returns",
  },
  sorting_factor: {
    label: "Consistency score",
    tooltip: "The factor this screen ranks on",
  },
  ret_12m: {
    label: "1-yr return",
    tooltip: "Absolute price return, 365d",
  },
  sharpe_12m: {
    label: "Return vs risk (1 yr)",
    tooltip: "1-yr Sharpe ratio",
  },
  vol_12m: {
    label: "Bumpiness",
    tooltip: "Annualized volatility, 1 yr",
  },
  close_raw: {
    label: "Price",
    tooltip: "Last close, unadjusted",
  },
  ma_200: {
    label: "200-day average",
    tooltip: "Simple MA, 200 sessions",
  },
  beta_12m: {
    label: "Moves with market",
    tooltip: "1-yr beta vs index",
  },
  marketcap_cr: {
    label: "Market cap",
    tooltip: "Market capitalisation in ₹ crore",
  },
  series: {
    label: "Series",
    tooltip: "Exchange series (EQ, BE, …)",
  },
};

/** Default saved columns for new screens — §2.2 column diet. */
export const DEFAULT_VISIBLE_FACTOR_COLUMNS: readonly string[] = ["ret_12m", "vol_12m", "close_raw"];

/** Legacy default factor columns — hidden in the table unless the user added a custom column. */
export const LEGACY_DEFAULT_FACTOR_COLUMNS: ReadonlySet<string> = new Set([
  "close_raw",
  "series",
  "marketcap_cr",
  "ret_12m",
  "sharpe_12m",
  "vol_12m",
  "beta_12m",
  "ma_200",
]);

/** Factor columns dropped from the default diet (§2.2). */
export const LEGACY_HIDDEN_FACTOR_COLUMNS: ReadonlySet<string> = new Set([
  "series",
  "marketcap_cr",
  "ma_200",
  "beta_12m",
  "sharpe_12m",
]);

/**
 * Columns that identify the row rather than describe it.
 *
 * These are never hidden for lack of data: a rank or a symbol column that came back empty is a
 * broken payload the operator needs to see, not a column worth tidying away.
 */
export const IDENTITY_COLUMNS: ReadonlySet<string> = new Set([
  "rank",
  "symbol",
  "name",
  "sorting_factor",
]);

export function columnDisplayLabel(key: string, fallback: string): string {
  return COLUMN_DISPLAY[key]?.label ?? fallback;
}

export function columnDisplayTooltip(key: string): string | undefined {
  return COLUMN_DISPLAY[key]?.tooltip;
}

function shouldShowFactorColumn(key: string): boolean {
  if (DEFAULT_VISIBLE_FACTOR_COLUMNS.includes(key)) return true;
  if (LEGACY_HIDDEN_FACTOR_COLUMNS.has(key)) return false;
  if (!LEGACY_DEFAULT_FACTOR_COLUMNS.has(key)) return true;
  return false;
}

/**
 * The column diet (§2.2), applied to a column list without reference to any data.
 *
 * Identity columns stay. Legacy hidden defaults (marketcap, series, …) are dropped. Custom
 * columns the user added via Edit Columns (anything outside the legacy default set) stay visible.
 */
function dietedColumns(columns: readonly string[]): string[] {
  const out: string[] = [];

  for (const key of columns) {
    if (key === "name") {
      if (!columns.includes("symbol")) out.push(key);
      continue;
    }
    if (key === "symbol" || key === "sorting_factor") {
      out.push(key);
      continue;
    }
    if (!shouldShowFactorColumn(key)) continue;
    out.push(key);
  }

  return out;
}

/**
 * Whether a cell carries data.
 *
 * Deliberately the same test the table's own renderer applies before it substitutes an em dash
 * (`renderValue` in `components/screens/result-columns.tsx`), because the rule this predicate
 * serves is "hide a column only when every one of its cells would render as an em dash". Keeping
 * the two in step is what makes the rule sound rather than approximate.
 *
 * `0`, `false` and `NaN` are data. A screen on which every stock returned exactly 0% has a
 * perfectly informative return column, and a NaN is a computation that ran and failed — both are
 * facts about the instrument, and neither renders as an em dash.
 */
function hasData(value: unknown): boolean {
  return value !== null && value !== undefined && value !== "";
}

/**
 * The subset of `candidateColumns` for which no row in `rows` carries data.
 *
 * One pass over the rows, narrowing the candidate set as evidence arrives and stopping the moment
 * nothing is left to prove — so the common case (every column populated in the first row) costs
 * one row, and the pathological case (a column empty in all of them) is the single full scan that
 * proving a negative requires.
 */
function columnsWithoutData(
  candidateColumns: readonly string[],
  rows: readonly Record<string, unknown>[],
): ReadonlySet<string> {
  const candidates = new Set<string>();
  for (const key of candidateColumns) {
    if (!IDENTITY_COLUMNS.has(key)) candidates.add(key);
  }

  for (const row of rows) {
    for (const key of candidates) {
      // Deleting the entry the iterator is currently on is well-defined for a Set.
      if (Object.hasOwn(row, key) && hasData(row[key])) candidates.delete(key);
    }
    if (candidates.size === 0) break;
  }

  return candidates;
}

interface ColumnPolicy {
  readonly visible: string[];
  readonly suppressed: string[];
}

/**
 * The single decision both exported functions read, so the visible set and the disclosed set can
 * never drift apart: `visible` and `suppressed` always partition the dieted column list.
 */
function columnPolicy(
  columns: readonly string[],
  rows: readonly Record<string, unknown>[] | undefined,
): ColumnPolicy {
  const dieted = dietedColumns(columns);

  // No rows means no evidence, not evidence of absence — an empty result set must not strip the
  // table down to its identity columns on its way to rendering an empty state.
  if (rows === undefined || rows.length === 0) {
    return { visible: dieted, suppressed: [] };
  }

  const dead = columnsWithoutData(dieted, rows);
  if (dead.size === 0) {
    return { visible: dieted, suppressed: [] };
  }

  const visible: string[] = [];
  const suppressed: string[] = [];
  const disclosed = new Set<string>();

  for (const key of dieted) {
    if (!dead.has(key)) {
      visible.push(key);
      continue;
    }
    if (!disclosed.has(key)) {
      disclosed.add(key);
      suppressed.push(key);
    }
  }

  return { visible, suppressed };
}

/**
 * Columns the table should render.
 *
 * Without `rows`, this is the column diet (§2.2) and nothing else, so existing callers are
 * unaffected. With `rows`, it additionally drops any column that holds no data in any row — the
 * class of fault the brief reported as a MARKETCAP column of em dashes and which is live on Price
 * (`close_raw` is NULL for every seeded row). Identity columns are exempt.
 *
 * Whatever this drops for lack of data, `suppressedResultColumns` names, so the page can disclose
 * the omission instead of quietly serving a narrower table than the user configured.
 */
export function visibleResultColumns(
  columns: readonly string[],
  rows?: readonly Record<string, unknown>[],
): string[] {
  return columnPolicy(columns, rows).visible;
}

/**
 * The columns `visibleResultColumns(columns, rows)` dropped **for lack of data**.
 *
 * Columns the diet hides are not in here — the user did not ask for those, so there is nothing to
 * apologise for. These are columns the user did ask for and we cannot fill.
 */
export function suppressedResultColumns(
  columns: readonly string[],
  rows: readonly Record<string, unknown>[],
): string[] {
  return columnPolicy(columns, rows).suppressed;
}
