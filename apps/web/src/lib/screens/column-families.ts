import type { ColumnOut } from "@baskfy/api-client";

/**
 * The column picker's families — docs/08 §"Columns editor": "34 toggles in the reference's order,
 * **grouped by family**".
 *
 * `/meta/columns` carries `key`, `label`, `unit` and `is_factor` but no family: the factor
 * registry's `FactorFamily` describes *ranking* families (absolute / sharpe / RSI / beta-adjusted
 * / skip-month / other), and thirteen of the thirty-six picker columns are not ranking factors at
 * all — `series`, the four moving averages, the five circuit counts, the two highs and the median
 * volume. Grouping the picker by the ranking families would therefore put a third of it under
 * "Other".
 *
 * These groups follow docs/01 §4's own enumeration order instead, which is how the reference
 * product lists them, and each column is matched by an explicit predicate rather than by a name
 * prefix — `vol_12m` and `vol_avg_12m` differ by one segment and mean entirely different things.
 */
export interface ColumnFamily {
  id: string;
  label: string;
  keys: readonly string[];
}

export const COLUMN_FAMILIES: readonly ColumnFamily[] = [
  { id: "identity", label: "Identity & fundamentals", keys: ["series", "marketcap_cr", "pe"] },
  {
    id: "absolute-return",
    label: "Absolute return",
    keys: ["ret_12m", "ret_9m", "ret_6m", "ret_3m", "ret_1m"],
  },
  {
    id: "sharpe-return",
    label: "Sharpe return",
    keys: ["sharpe_12m", "sharpe_9m", "sharpe_6m", "sharpe_3m", "sharpe_1m"],
  },
  { id: "rsi", label: "RSI", keys: ["rsi_12m", "rsi_9m", "rsi_6m", "rsi_3m", "rsi_1m"] },
  { id: "risk", label: "Risk", keys: ["beta_12m", "vol_12m"] },
  {
    id: "highs",
    label: "Highs",
    keys: ["high_1y", "high_ath", "away_high_1y", "away_high_ath"],
  },
  { id: "moving-averages", label: "Moving averages", keys: ["ma_200", "ma_100", "ma_50", "ma_20"] },
  { id: "liquidity", label: "Liquidity", keys: ["median_vol_12m"] },
  { id: "price", label: "Price", keys: ["close", "close_raw"] },
  {
    id: "circuits",
    label: "Circuits",
    keys: ["circuits_12m", "circuits_9m", "circuits_6m", "circuits_3m", "circuits_1m"],
  },
];

export interface GroupedColumns {
  family: ColumnFamily;
  columns: ColumnOut[];
}

/**
 * Group what the API actually returned.
 *
 * A column the API serves but no family claims is appended under "Other" rather than dropped: the
 * registry is allowed to grow, and a picker that silently hides a new column is worse than one
 * with an untidy last group.
 */
export function groupColumns(columns: readonly ColumnOut[]): GroupedColumns[] {
  const byKey = new Map(columns.map((column) => [column.key, column]));
  const claimed = new Set<string>();

  const groups = COLUMN_FAMILIES.map((family) => {
    const members = family.keys
      .map((key) => {
        const column = byKey.get(key);
        if (column) claimed.add(key);
        return column;
      })
      .filter((column): column is ColumnOut => column !== undefined);
    return { family, columns: members };
  }).filter((group) => group.columns.length > 0);

  const orphans = columns.filter((column) => !claimed.has(column.key));
  if (orphans.length > 0) {
    groups.push({ family: { id: "other", label: "Other", keys: [] }, columns: orphans });
  }
  return groups;
}
