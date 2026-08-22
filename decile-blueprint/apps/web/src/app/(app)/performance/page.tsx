import type { Metadata } from "next";

import {
  DeskNav,
  Empty,
  PageHeader,
  ReadOnlyFooter,
  Row,
  Stat,
  StatRow,
  Table,
  Td,
  Th,
  pct,
  rupees,
  toneOf,
} from "@/components/desk/ui";
import { DeskUnavailable, fetchPerformance } from "@/lib/desk/fetch";

/**
 * `/performance` — M26. What the portfolio is worth, and whether that beat the benchmark.
 *
 * The desk's own version of this page is a operator's instrument panel. This is the same
 * numbers asked the way a person asks them: what is it worth, is that up or down, and did it do
 * better than just buying the index.
 *
 * **Read-only.** Nothing on this page can move money.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Performance",
  description: "What the portfolio is worth, how it has moved, and how that compares to the benchmark.",
};

export default async function PerformancePage() {
  let data;
  try {
    data = await fetchPerformance();
  } catch (error) {
    if (!(error instanceof DeskUnavailable)) throw error;
    return (
      <Empty
        title="Performance"
        body="No end-of-day marks have been recorded yet. The desk writes one each evening it runs."
      />
    );
  }

  const beat = data.excess_pct !== null && data.excess_pct > 0;

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Performance"
        lede="What the portfolio is worth, and how that compares to buying the benchmark instead."
        meta={`As of ${data.as_of} · ${data.series.length} daily marks`}
      />

      <StatRow>
        <Stat label="Portfolio value" value={rupees(data.nav)} />
        <Stat
          label="Return"
          value={pct(data.return_pct, { sign: true })}
          tone={toneOf(data.return_pct)}
          hint="since the first mark"
        />
        <Stat
          label="Benchmark"
          value={pct(data.benchmark_return_pct, { sign: true })}
          tone={toneOf(data.benchmark_return_pct)}
          hint="NIFTY 500 Momentum 50"
        />
        <Stat
          label={beat ? "Ahead by" : "Behind by"}
          value={pct(data.excess_pct, { sign: true })}
          tone={toneOf(data.excess_pct)}
          hint="portfolio minus benchmark"
        />
      </StatRow>

      <StatRow>
        <Stat label="Invested" value={rupees(data.invested)} />
        <Stat label="Cash" value={rupees(data.cash)} />
      </StatRow>

      <Table
        caption={`Daily portfolio value from ${data.series[0]?.date} to ${data.as_of}`}
        head={
          <>
            <Th>Date</Th>
            <Th align="right">Value</Th>
            <Th align="right">Invested</Th>
            <Th align="right">Cash</Th>
            <Th align="right">Portfolio</Th>
            <Th align="right">Benchmark</Th>
          </>
        }
      >
        {[...data.series].reverse().map((point) => (
          <Row key={point.date}>
            <Td>{point.date}</Td>
            <Td align="right">{rupees(point.nav)}</Td>
            <Td align="right">{rupees(point.invested)}</Td>
            <Td align="right">{rupees(point.cash)}</Td>
            <Td align="right">{point.index_value === null ? "–" : point.index_value.toFixed(2)}</Td>
            <Td align="right">
              {point.benchmark_value === null ? "–" : point.benchmark_value.toFixed(2)}
            </Td>
          </Row>
        ))}
      </Table>

      <p className="text-xs text-muted-foreground">
        The last two columns are both rebased to 100 on the first day, so they can be read
        side by side. Instruments the strategy does not manage are excluded from the return.
      </p>

      <DeskNav current="/performance" />
      <ReadOnlyFooter />
    </div>
  );
}
