import type { Metadata } from "next";

import {
  DeskNav,
  Empty,
  Notice,
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
import { DeskUnavailable, fetchHoldings } from "@/lib/desk/fetch";

/**
 * `/holdings` — M26. What is actually owned, with what it has made or lost.
 *
 * This is the page the desk's `/stops` and `/reconcile` consoles both open with, minus the
 * live broker calls neither of them can make from here (see `routers/desk.py`).
 *
 * **Read-only.** Positions are shown, never changed.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Holdings",
  description: "Every position currently held, with its cost, its value and what it has made.",
};

export default async function HoldingsPage() {
  let data;
  try {
    data = await fetchHoldings();
  } catch (error) {
    if (!(error instanceof DeskUnavailable)) throw error;
    return (
      <Empty
        title="Holdings"
        body="No end-of-day snapshot has been recorded yet, so there is nothing to show."
      />
    );
  }

  const managed = data.rows.filter((row) => !row.excluded);
  const unrealised = managed.reduce((sum, row) => sum + (row.unrealised ?? 0), 0);
  const pledged = data.rows.filter((row) => row.pledged_qty > 0).length;

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Holdings"
        lede="Every position currently held, what it cost, and what it is worth now."
        meta={`As of ${data.as_of}`}
      />

      <StatRow>
        <Stat label="Positions" value={String(managed.length)} hint="managed by the strategy" />
        <Stat label="Value" value={rupees(data.total_value - data.excluded_value)} />
        <Stat
          label="Unrealised"
          value={rupees(unrealised, { sign: true })}
          tone={toneOf(unrealised)}
        />
        <Stat label="Pledged" value={String(pledged)} hint="held as collateral" />
      </StatRow>

      {data.excluded_value > 0 && (
        <Notice>
          <strong>{rupees(data.excluded_value)}</strong> is held in instruments the strategy does
          not manage — sovereign gold bonds and government securities. They are shown below, marked,
          and they are left out of every figure above and out of the return on the performance page.
        </Notice>
      )}

      <Table
        caption={`Positions held on ${data.as_of}`}
        head={
          <>
            <Th>Symbol</Th>
            <Th align="right">Qty</Th>
            <Th align="right">Avg cost</Th>
            <Th align="right">Price</Th>
            <Th align="right">Value</Th>
            <Th align="right">Gain / loss</Th>
            <Th align="right">%</Th>
          </>
        }
      >
        {data.rows.map((row) => (
          <Row key={row.symbol}>
            <Td>
              <span className="font-medium">{row.symbol}</span>
              {row.excluded && (
                <span className="ml-2 text-xs text-muted-foreground">not managed</span>
              )}
              {row.pledged_qty > 0 && (
                <span className="ml-2 text-xs text-muted-foreground">
                  {row.pledged_qty} pledged
                </span>
              )}
            </Td>
            <Td align="right">{row.quantity.toLocaleString("en-IN")}</Td>
            <Td align="right">{price(row.average_price)}</Td>
            <Td align="right">{price(row.price)}</Td>
            <Td align="right">{rupees(row.value)}</Td>
            <Td align="right" tone={toneOf(row.unrealised)}>
              {rupees(row.unrealised, { sign: true })}
            </Td>
            <Td align="right" tone={toneOf(row.unrealised_pct)}>
              {pct(row.unrealised_pct, { sign: true })}
            </Td>
          </Row>
        ))}
      </Table>

      <p className="text-xs text-muted-foreground">
        Quantity includes shares pledged for collateral — they are still owned and still sell
        directly. Prices are from the most recent end-of-day snapshot, not live.
      </p>

      <DeskNav current="/holdings" />
      <ReadOnlyFooter live="Each position carries a protective stop placed the same session it was bought. Whether a given stop is resting at the broker right now is confirmed in the desk console, which is the only surface that talks to the broker." />
    </div>
  );
}
