import { formatTradeDate } from "@/lib/format";
import { MB_BOOK, MB_CAVEATS, MB_YEARS } from "@/lib/mb/copy";
import type { MbModel } from "@/lib/mb/fetch";

/** PM7's "what this is" panel: the from-2014 book, the twelve years, the failures, and the model. */
export function MbWhatThisIs({ model }: { model: MbModel | null }) {
  return (
    <section
      aria-labelledby="mb-what-heading"
      className="space-y-4 rounded-md border border-border p-4"
      data-testid="mb-what"
    >
      <h2
        id="mb-what-heading"
        className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
      >
        What this is
      </h2>
      <p className="max-w-[80ch] text-sm">
        A ranking of liquid NSE names by how likely the model thinks each is to
        touch twice its price within about a year. Backtested from{" "}
        {MB_BOOK.from} with delisted and merged names included, a monthly set of
        the top 20 investable names returned{" "}
        <strong data-testid="mb-cagr">{MB_BOOK.cagrPct}% a year</strong> with a
        worst fall of{" "}
        <strong data-testid="mb-dd">{MB_BOOK.maxDrawdownPct}%</strong> (Sharpe{" "}
        {MB_BOOK.sharpe}), against {MB_BOOK.equalWeightCagrPct}% /{" "}
        {MB_BOOK.equalWeightDrawdownPct}% for every investable name held equally
        and {MB_BOOK.smallcapCagrPct}% / {MB_BOOK.smallcapDrawdownPct}% for the
        Smallcap 250. A little more return than the index, and a deeper
        drawdown.
      </p>
      <ul
        className="max-w-[80ch] list-disc space-y-1 pl-5 text-sm text-muted-foreground"
        data-testid="mb-caveats"
      >
        {MB_CAVEATS.map((c) => (
          <li key={c}>{c}</li>
        ))}
      </ul>
      <div className="overflow-x-auto">
        <table
          className="min-w-[36rem] border-collapse text-sm"
          data-testid="mb-years"
        >
          <caption className="mb-1 text-left text-xs text-muted-foreground">
            Twelve walk-forward years: share of names that touched 2× within 12
            months, and the mean 12-month return
          </caption>
          <thead>
            <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
              {[
                "Signal year",
                "All names hit",
                "Top 20 hit",
                "Top 20, 12 months",
                "All names, 12 months",
              ].map((h) => (
                <th key={h} scope="col" className="py-1.5 pr-4 font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {MB_YEARS.map((y) => (
              <tr
                key={y.year}
                className="border-b border-border/40 tabular-nums"
              >
                <td className="py-1.5 pr-4">{y.year}</td>
                <td className="py-1.5 pr-4">{y.baseHitPct}%</td>
                <td className="py-1.5 pr-4">{y.top20HitPct}%</td>
                <td className="py-1.5 pr-4">{y.top20MeanPct}%</td>
                <td className="py-1.5 pr-4">{y.universeMeanPct}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {model ? (
        <p className="text-xs text-muted-foreground" data-testid="mb-model">
          Model {model.id}, trained on {model.rows.toLocaleString("en-IN")} rows
          with labels through {formatTradeDate(model.trained_on)}; retrained the
          first Saturday of each quarter.
        </p>
      ) : null}
    </section>
  );
}
