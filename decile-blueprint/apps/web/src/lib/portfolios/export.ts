import type { RebalanceNameOut } from "@decile/api-client";

/**
 * Turning one of the three lists into something a user can paste into a broker or a spreadsheet —
 * docs/08 §"Rebalance tracker": "each with copy-to-clipboard and CSV".
 *
 * Two shapes, deliberately different:
 *
 * * **Clipboard** is symbols, one per line, and nothing else. It is pasted into a basket order or
 *   a watchlist import, both of which want bare symbols; a header line would be pasted as a
 *   position called "SYMBOL".
 * * **CSV** carries the rank and, on an exit, the reason, because a file is read later by someone
 *   who no longer has the screen in front of them.
 *
 * `\n` line endings and the same quoting rule as the screener's export (`docs/09a` §1), so the two
 * downloads in this product agree with each other.
 */

export const EXIT_REASON_LABELS: Record<string, string> = {
  rank_outside_buffer: "Ranked outside the buffer",
  not_in_screen: "No longer in the screen",
  delisted: "Delisted",
};

export function clipboardText(rows: readonly RebalanceNameOut[]): string {
  return rows.map((row) => row.symbol).join("\n");
}

export function csvText(rows: readonly RebalanceNameOut[]): string {
  const header = "symbol,name,rank,reason\n";
  const body = rows
    .map((row) =>
      [
        row.symbol,
        quote(row.name),
        row.rank === null || row.rank === undefined ? "" : String(row.rank),
        row.reason ? (EXIT_REASON_LABELS[row.reason] ?? row.reason) : "",
      ].join(","),
    )
    .join("\n");
  return rows.length === 0 ? header : `${header}${body}\n`;
}

/** Only `name` can contain a comma, which is the rule the 93-column export follows too. */
function quote(value: string): string {
  return `"${value.replaceAll('"', '""')}"`;
}

export function downloadCsv(filename: string, text: string): void {
  const blob = new Blob([text], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

/** `decile-exits-2026-08-18.csv` — the list and the date it was computed for. */
export function filenameFor(list: string, asOf: string): string {
  return `decile-${list}-${asOf}.csv`;
}
