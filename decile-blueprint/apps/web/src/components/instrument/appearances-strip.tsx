import Link from "next/link";

import { formatTradeDate } from "@/lib/format";
import { appearanceHref, fetchAppearances, type Appearance } from "@/lib/instrument/appearances";

/**
 * "Where this stock appears" — above the factsheet (Maulik, 14 Sep 2026).
 *
 * Every chip is a stored result: a screen's newest `screen_run` or a strategy's latest session.
 * Nothing re-runs to draw it. When the stock is in none of them the strip still says what was
 * checked, because "not in any screen" and "never looked" are different answers.
 */

function label(item: Appearance): string {
  if (item.rank != null) {
    return `${item.name} · #${item.rank}${item.of != null ? ` of ${item.of}` : ""}`;
  }
  return item.detail ? `${item.name} · ${item.detail}` : item.name;
}

function kindWord(item: Appearance): string {
  switch (item.kind) {
    case "screen":
      return "Your screen";
    case "template":
      return "Template";
    default:
      return "Strategy scan";
  }
}

export async function AppearancesStrip({ symbol }: { symbol: string }) {
  const view = await fetchAppearances(symbol);
  if (view === null) return null;

  const items = view.appearances;
  const dates = [...new Set(items.map((item) => item.as_of))].sort();
  const checked = [
    `${view.screens_checked} ${view.screens_checked === 1 ? "screen" : "screens"}`,
    ...view.strategies_checked,
  ].join(", ");

  return (
    <section
      aria-label="Where this stock appears"
      data-testid="instrument-appearances"
      className="rounded-xl border border-border bg-card px-4 py-3"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-medium">
          {items.length === 0
            ? `${view.symbol} is not in any of your screens or strategy scans`
            : `${view.symbol} appears in ${items.length} ${items.length === 1 ? "result" : "results"}`}
        </h2>
        <p className="text-xs text-muted-foreground">
          Latest stored results
          {dates.length > 0 ? ` · ${dates.map((d) => formatTradeDate(d)).join(", ")}` : ""} · checked{" "}
          {checked}
        </p>
      </div>

      {items.length > 0 ? (
        <ul className="mt-2 flex flex-wrap gap-2" data-testid="instrument-appearances-list">
          {items.map((item) => (
            <li key={`${item.kind}-${item.ref}-${item.detail ?? ""}`}>
              <Link
                href={appearanceHref(item)}
                title={`${kindWord(item)} · as of ${formatTradeDate(item.as_of)}${
                  item.definition_changed
                    ? " · the screen was edited after this run; the rank is from the earlier version"
                    : ""
                }`}
                className="inline-flex items-center gap-1.5 rounded-full border border-border bg-background px-3 py-1 text-xs hover:bg-muted"
              >
                <span className="text-muted-foreground">{kindWord(item)}</span>
                <span className="font-medium">{label(item)}</span>
                {item.definition_changed ? <span aria-label="edited since this run">*</span> : null}
              </Link>
            </li>
          ))}
        </ul>
      ) : null}

      {view.screens_never_run.length > 0 ? (
        <p className="mt-2 text-xs text-muted-foreground">
          No stored result yet for {view.screens_never_run.join(", ")} — it is saved after the next
          nightly publish.
        </p>
      ) : null}
    </section>
  );
}
