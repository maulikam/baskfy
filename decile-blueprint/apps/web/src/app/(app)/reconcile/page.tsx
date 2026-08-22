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
  price,
  rupees,
  toneOf,
} from "@/components/desk/ui";
import { DeskUnavailable, fetchReconcile } from "@/lib/desk/fetch";

/**
 * `/reconcile` — M26. Did the last plan actually happen?
 *
 * One question, answered at the top, with the per-order detail underneath. The desk's console
 * answers the same question against live broker state; this answers it against the record,
 * which is what the database can honestly say (see `routers/desk.py`).
 *
 * **Read-only.** Nothing here re-sends an order.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Plan vs fills",
  description: "Whether the last rebalance did what it planned to, order by order.",
};

/** The desk's order statuses, said plainly. */
const STATUS: Record<string, string> = {
  COMPLETE: "filled",
  DRY_RUN: "simulated",
  REJECTED: "rejected",
  FAILED: "failed",
  CANCELLED: "cancelled",
  OPEN: "still working",
  RISK_BLOCKED: "blocked by a risk limit",
};

export default async function ReconcilePage() {
  let data;
  try {
    data = await fetchReconcile();
  } catch (error) {
    if (!(error instanceof DeskUnavailable)) throw error;
    return (
      <Empty title="Plan vs fills" body="The desk has not recorded a rebalance plan yet." />
    );
  }

  const simulated = data.rows.filter((row) => row.status === "DRY_RUN").length;
  const allSimulated = simulated === data.rows.length && data.rows.length > 0;

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Plan vs fills"
        lede="Whether the last rebalance did what it planned to, order by order."
        meta={`Plan ${data.plan_id} · ${data.created_at}${data.note ? ` · ${data.note}` : ""}`}
      />

      {allSimulated ? (
        <Notice>
          This plan was a <strong>dry run</strong>. Every order was simulated end to end and none
          of them reached a broker, which is why nothing below shows a fill.
        </Notice>
      ) : data.settled ? (
        <p className="rounded-md border border-emerald-500/40 bg-emerald-500/10 p-3 text-sm">
          <strong>Settled.</strong> All {data.planned_count} orders filled as planned.
        </p>
      ) : (
        <Notice>
          <strong>{data.partial_count + data.unfilled_count}</strong> of {data.planned_count} orders
          did not fill as planned. The rows below show which.
        </Notice>
      )}

      <StatRow>
        <Stat label="Orders" value={String(data.planned_count)} />
        <Stat label="Filled" value={String(data.complete_count)} />
        <Stat label="Partial" value={String(data.partial_count)} />
        <Stat label="Nothing filled" value={String(data.unfilled_count)} />
      </StatRow>

      <Table
        caption={`Orders in plan ${data.plan_id}`}
        head={
          <>
            <Th>Symbol</Th>
            <Th>Side</Th>
            <Th align="right">Planned</Th>
            <Th align="right">Filled</Th>
            <Th align="right">Ref price</Th>
            <Th align="right">Avg fill</Th>
            <Th align="right">Difference</Th>
            <Th>Status</Th>
          </>
        }
      >
        {data.rows.map((row) => (
          <Row key={`${row.side}-${row.symbol}`}>
            <Td>
              <span className="font-medium">{row.symbol}</span>
            </Td>
            <Td>{row.side}</Td>
            <Td align="right">{row.planned_qty.toLocaleString("en-IN")}</Td>
            <Td align="right">{row.filled_qty.toLocaleString("en-IN")}</Td>
            <Td align="right">{price(row.planned_ref_price)}</Td>
            <Td align="right">{price(row.avg_fill_price)}</Td>
            <Td align="right" tone={toneOf(row.slippage === null ? null : -row.slippage)}>
              {rupees(row.slippage, { sign: true })}
            </Td>
            <Td>
              <span className="text-xs text-muted-foreground">
                {row.status ? (STATUS[row.status] ?? row.status) : "–"}
              </span>
            </Td>
          </Row>
        ))}
      </Table>

      <p className="text-xs text-muted-foreground">
        Difference is what the fills cost against the price the plan was built at — negative is in
        your favour on a buy. It is only shown where both prices are known.
      </p>

      <DeskNav current="/reconcile" />
      <ReadOnlyFooter live="This compares the plan against the desk's own record of what filled. Comparing it against what the broker holds right now is done in the desk console, which is the only surface that talks to the broker." />
    </div>
  );
}
