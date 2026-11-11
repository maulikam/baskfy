import { EMPTY_CELL, formatFraction, formatTradeDate } from "@/lib/format";
import { MB_BOOK_NAMES, MB_PAPER_EXPECT } from "@/lib/mb/copy";
import type { MbPaper, MbPaperBook, MbPaperOutcome } from "@/lib/mb/fetch";

const ORDER = ["book", "hi63_20", "hi63_16", "book_overlay"] as const;

function Books({
  books,
  testId,
}: {
  books: readonly MbPaperBook[];
  testId: string;
}) {
  const rows = ORDER.map((b) => books.find((x) => x.book === b)).filter(
    (x): x is MbPaperBook => x !== undefined,
  );
  return (
    <div className="overflow-x-auto">
      <table
        className="min-w-[44rem] border-collapse text-sm"
        data-testid={testId}
      >
        <thead>
          <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
            {[
              "Line",
              "Marked",
              "Growth",
              "A year",
              "Worst fall",
              "Invested now",
            ].map((h) => (
              <th key={h} scope="col" className="py-1.5 pr-4 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((b) => (
            <tr key={b.book} className="border-b border-border/40 tabular-nums">
              <td className="py-1.5 pr-4">{MB_BOOK_NAMES[b.book]}</td>
              <td className="py-1.5 pr-4">
                {formatTradeDate(b.start)} – {formatTradeDate(b.end)}
              </td>
              <td className="py-1.5 pr-4">
                {formatFraction(b.growth_fraction, 1)}
              </td>
              <td className="py-1.5 pr-4">
                {b.cagr_fraction === null
                  ? EMPTY_CELL
                  : formatFraction(b.cagr_fraction, 1)}
              </td>
              <td className="py-1.5 pr-4">
                {formatFraction(b.max_dd_fraction, 1)}
              </td>
              <td className="py-1.5 pr-4">
                {b.book === "book_overlay"
                  ? b.invested_fraction === "1.0000"
                    ? "in"
                    : "out"
                  : formatFraction(b.invested_fraction, 0)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Outcomes({
  rows,
  testId,
}: {
  rows: readonly MbPaperOutcome[];
  testId: string;
}) {
  return (
    <div className="overflow-x-auto">
      <table
        className="min-w-[40rem] border-collapse text-sm"
        data-testid={testId}
      >
        <thead>
          <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
            {[
              "Lines",
              "",
              "Count",
              "Touched 2×",
              "Halved",
              "Median return",
            ].map((h, k) => (
              <th key={k} scope="col" className="py-1.5 pr-4 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((o) => (
            <tr
              key={`${o.line}-${o.matured}`}
              className="border-b border-border/40 tabular-nums"
            >
              <td className="py-1.5 pr-4">
                {o.line === "book" ? "Monthly picks" : "63-day trigger"}
              </td>
              <td className="py-1.5 pr-4 text-muted-foreground">
                {o.matured ? "held 12 months" : "so far"}
              </td>
              <td className="py-1.5 pr-4">{o.lines}</td>
              <td className="py-1.5 pr-4">
                {formatFraction(o.touched_2x_fraction, 1)}
              </td>
              <td className="py-1.5 pr-4">
                {formatFraction(o.halved_fraction, 1)}
              </td>
              <td className="py-1.5 pr-4">
                {formatFraction(o.median_return_fraction, 1)}
              </td>
            </tr>
          ))}
          <tr className="text-muted-foreground" data-testid="mb-paper-expect">
            <td className="py-1.5 pr-4">Expected, monthly picks</td>
            <td className="py-1.5 pr-4">held 12 months</td>
            <td className="py-1.5 pr-4">{EMPTY_CELL}</td>
            <td className="py-1.5 pr-4">{MB_PAPER_EXPECT.touched2x}</td>
            <td className="py-1.5 pr-4">{MB_PAPER_EXPECT.halved}</td>
            <td className="py-1.5 pr-4">{MB_PAPER_EXPECT.median12m}</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

/** PM8's `/mb/paper` panel. Paper only: no line here was ever an order. The backtest is labelled as one. */
export function MbPaperPanel({ paper }: { paper: MbPaper | null }) {
  const live = paper?.books.filter((b) => b.source === "live") ?? [];
  const back = paper?.books.filter((b) => b.source === "backtest") ?? [];
  const liveOut = paper?.outcomes.filter((o) => o.source === "live") ?? [];
  const backOut = paper?.outcomes.filter((o) => o.source === "backtest") ?? [];
  return (
    <div className="space-y-8">
      <section aria-labelledby="mb-live-heading" className="space-y-3">
        <h2
          id="mb-live-heading"
          className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
        >
          Live paper record
        </h2>
        {live.length === 0 ? (
          <p
            className="max-w-[80ch] text-sm text-muted-foreground"
            data-testid="mb-paper-unstarted"
          >
            {paper?.live_from_month
              ? `The live record starts with the first tranche of ${paper.live_from_month}, entered at the next session's open.`
              : "The live record starts with the first monthly tranche after the scorer's first night."}{" "}
            Nothing has been marked live yet.
          </p>
        ) : (
          <>
            <Books books={live} testId="mb-paper-live" />
            <Outcomes rows={liveOut} testId="mb-paper-live-outcomes" />
            <ul className="text-sm" data-testid="mb-paper-tranches">
              {paper?.tranches.map((t) => (
                <li key={t.signal_date} className="tabular-nums">
                  {formatTradeDate(t.signal_date)}: {t.lines} names,{" "}
                  {t.sessions_held ?? 0} sessions held, touched 2×{" "}
                  {formatFraction(t.touched_2x_fraction, 0)}, median{" "}
                  {formatFraction(t.median_return_fraction, 1)}; {t.hi63_lines}{" "}
                  trigger lines
                </li>
              ))}
            </ul>
          </>
        )}
        <p className="max-w-[80ch] text-xs text-muted-foreground">
          The test is twelve months of this record at 70% of the expected hit
          rate or better, and Maulik&rsquo;s decision after that. The trigger
          line is expected to fall {MB_PAPER_EXPECT.triggerDrawdown} at worst.
        </p>
      </section>
      <section
        aria-labelledby="mb-back-heading"
        className="space-y-3 rounded-md border border-dashed border-border p-4"
        data-testid="mb-paper-backtest"
      >
        <h2
          id="mb-back-heading"
          className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
        >
          Backtest, not live
        </h2>
        <p className="max-w-[80ch] text-sm text-muted-foreground">
          The same rules run over the walk-forward scores from 2014, with
          delisted names written off. These lines were never live; they are here
          so the record above has something to be compared with.
        </p>
        {back.length === 0 ? (
          <p className="text-sm text-muted-foreground">Not seeded yet.</p>
        ) : (
          <Books books={back} testId="mb-paper-back" />
        )}
        {backOut.length === 0 ? null : (
          <Outcomes rows={backOut} testId="mb-paper-back-outcomes" />
        )}
      </section>
    </div>
  );
}
