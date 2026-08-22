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
import { PAGES } from "@/lib/vocabulary";

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
  title: PAGES["/performance"].title,
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
        title={PAGES["/performance"].title}
        body="No end-of-day marks have been recorded yet. The desk writes one each evening it runs."
      />
    );
  }

  const beat = data.excess_pct !== null && data.excess_pct > 0;

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title={PAGES["/performance"].title}
        lede={PAGES["/performance"].blurb}
        meta={`As of ${data.as_of} · ${data.series.length} days recorded`}
      />

      <StatRow>
        <Stat label="What it is all worth" value={rupees(data.nav)} />
        <Stat
          label="Change since we started"
          value={pct(data.return_pct, { sign: true })}
          tone={toneOf(data.return_pct)}
          hint="since the first day recorded"
        />
        <Stat
          label="What the index did"
          value={pct(data.benchmark_return_pct, { sign: true })}
          tone={toneOf(data.benchmark_return_pct)}
          hint="NIFTY 500 Momentum 50"
        />
        <Stat
          label={beat ? "Ahead of the index by" : "Behind the index by"}
          value={pct(data.excess_pct, { sign: true })}
          tone={toneOf(data.excess_pct)}
          hint="the portfolio, minus what the index did"
        />
      </StatRow>

      <StatRow>
        <Stat label="Put to work" value={rupees(data.invested)} />
        <Stat label="Sitting in cash" value={rupees(data.cash)} />
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
