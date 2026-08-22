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
  price,
  rupees,
  toneOf,
} from "@/components/desk/ui";
import { DeskUnavailable, fetchTradebook } from "@/lib/desk/fetch";

/**
 * `/tradebook` — M26. Every trade the strategy has taken.
 *
 * The record, in the order it happened. Open positions first because they are the ones still
 * capable of changing.
 *
 * **Read-only.** A trade shown here has already happened.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Trades",
  description: "Every trade the strategy has taken, what it made or lost, and why it was closed.",
};

/** The desk's exit codes, in words a person would use. */
const REASONS: Record<string, string> = {
  rank: "fell out of the ranking",
  stop: "stop was hit",
  regime: "market stance turned defensive",
  rebalance: "rebalanced out",
  manual: "closed by hand",
};

function reasonOf(code: string | null): string {
  if (!code) return "–";
  return REASONS[code.toLowerCase()] ?? code;
}

export default async function TradebookPage() {
  let data;
  try {
    data = await fetchTradebook();
  } catch (error) {
    if (!(error instanceof DeskUnavailable)) throw error;
    return <Empty title="Trades" body="The desk has recorded no trades yet." />;
  }

  const decided = data.winners + data.losers;
  const hitRate = decided ? (data.winners / decided) * 100 : null;

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Trades"
        lede="Every trade the strategy has taken, and what each one made or lost."
        meta={`${data.total.toLocaleString("en-IN")} trades · showing the ${data.rows.length} most recent`}
      />

      <StatRow>
        <Stat label="Still open" value={String(data.open_count)} />
        <Stat label="Closed" value={data.closed_count.toLocaleString("en-IN")} />
        <Stat
          label="Realised"
          value={rupees(data.realised_pnl, { sign: true })}
          tone={toneOf(data.realised_pnl)}
          hint="closed trades, after costs"
        />
        <Stat
          label="Won"
          value={hitRate === null ? "–" : `${hitRate.toFixed(0)}%`}
          hint={`${data.winners} up, ${data.losers} down`}
        />
      </StatRow>

      <Table
        caption="Trades, most recently closed or opened first"
        head={
          <>
            <Th>Symbol</Th>
            <Th align="right">Qty</Th>
            <Th>Bought</Th>
            <Th align="right">At</Th>
            <Th>Sold</Th>
            <Th align="right">At</Th>
            <Th align="right">Gain / loss</Th>
            <Th align="right">%</Th>
            <Th>Why closed</Th>
          </>
        }
      >
        {data.rows.map((trade, index) => (
          <Row key={`${trade.symbol}-${trade.entry_date}-${index}`}>
            <Td>
              <span className="font-medium">{trade.symbol}</span>
              {trade.open && <span className="ml-2 text-xs text-muted-foreground">open</span>}
            </Td>
            <Td align="right">{trade.quantity.toLocaleString("en-IN")}</Td>
            <Td>{trade.entry_date ?? "–"}</Td>
            <Td align="right">{price(trade.entry_price)}</Td>
            <Td>{trade.exit_date ?? "–"}</Td>
            <Td align="right">{price(trade.exit_price)}</Td>
            <Td align="right" tone={toneOf(trade.pnl)}>
              {rupees(trade.pnl, { sign: true })}
            </Td>
            <Td align="right" tone={toneOf(trade.pnl_pct)}>
              {pct(trade.pnl_pct, { sign: true })}
            </Td>
            <Td>
              <span className="text-xs text-muted-foreground">{reasonOf(trade.exit_reason)}</span>
            </Td>
          </Row>
        ))}
      </Table>

      <p className="text-xs text-muted-foreground">
        An open trade has no gain or loss here — it has not been realised. What it is worth today
        is on the holdings page.
      </p>

      <DeskNav current="/tradebook" />
      <ReadOnlyFooter />
    </div>
  );
}
