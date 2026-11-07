"use client";

import { Row, Stat, StatRow, Table, Td, Th } from "@/components/desk/ui";
import { Banner, Card, Muted, useDeskPage } from "@/components/ops/shared";
import { useIsStaff } from "@/components/shell/staff-context";

/** `GET /desk/reconcile/data`: the latest plan against the broker's order book, stops and holdings. */
interface OrderRow {
  symbol: string;
  side: string;
  planned: number;
  local_status: string;
  local_filled: number;
  broker_status: string;
  broker_filled: number;
  state: string;
  why: string;
}

interface PositionRow {
  symbol: string;
  planned_final: number | null;
  held: number;
  diff: number;
  state: string;
  why: string;
}

interface StopFinding {
  kind: string;
  symbol: string;
  detail: string;
}

type Unavailable = { available: false; reason: string };

interface Available {
  available: true;
  plan_id: string | null;
  state: string;
  sections: { orders: string; stops: string; positions: string };
  orders: { rows: OrderRow[]; state: string };
  positions: { rows: PositionRow[]; state: string };
  stops: { coverage_pct?: number; findings: StopFinding[] };
  counts: { orders_wrong: number; orders_drift: number; positions_off: number; stop_findings: number };
}

const tone = (state: string) => (state === "ok" ? "text-positive" : state === "drift" ? "text-warning" : "text-negative");
const BAD_KINDS = new Set(["missing", "excess", "partial", "orphan"]);

export function BrokerReconcile() {
  const staff = useIsStaff();
  const { data, error } = useDeskPage<Available | Unavailable>("/reconcile/data");
  if (!staff) return null;
  if (error && !data) return <Banner tone="bad" tag="broker check unavailable">{error}</Banner>;
  if (!data) return <Muted>Reading the broker…</Muted>;
  if (!data.available) {
    return (
      <Banner tone="warn" tag="broker check unavailable">
        {data.reason} — log in to Kite on the desk and reload.
      </Banner>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <h2 className="text-lg font-semibold">Against the broker, right now</h2>
      <Banner tone={data.state === "ok" ? "ok" : data.state === "drift" ? "warn" : "bad"} tag={data.state}>
        {data.state === "ok"
          ? "The local record agrees with the broker on every order, every stop and every position."
          : data.state === "drift"
            ? "Nothing is wrong, but something is not yet settled."
            : "The local record disagrees with the broker."}
        {data.plan_id ? <span className="text-muted-foreground"> Plan {data.plan_id}.</span> : null}
      </Banner>

      <StatRow>
        <Stat
          label="Orders"
          value={data.sections.orders}
          tone={tone(data.sections.orders)}
          hint={`${data.counts.orders_wrong} disagree · ${data.counts.orders_drift} unsettled`}
        />
        <Stat
          label="Stops"
          value={data.sections.stops}
          tone={tone(data.sections.stops)}
          hint={`${data.counts.stop_findings} finding(s)${data.stops.coverage_pct !== undefined ? ` · ${data.stops.coverage_pct}% covered` : ""}`}
        />
        <Stat
          label="Positions"
          value={data.sections.positions}
          tone={tone(data.sections.positions)}
          hint={`${data.counts.positions_off} away from the plan`}
        />
      </StatRow>

      <Table
        caption="Orders — local record against the broker"
        head={
          <>
            <Th>Stock</Th>
            <Th>Side</Th>
            <Th align="right">Planned</Th>
            <Th>Local</Th>
            <Th align="right">Local filled</Th>
            <Th>Broker</Th>
            <Th align="right">Broker filled</Th>
            <Th>State</Th>
          </>
        }
      >
        {data.orders.rows.map((r) => (
          <Row key={`${r.side}-${r.symbol}`}>
            <Td>{r.symbol}</Td>
            <Td>{r.side}</Td>
            <Td align="right">{r.planned.toLocaleString("en-IN")}</Td>
            <Td>{r.local_status}</Td>
            <Td align="right">{r.local_filled}</Td>
            <Td>{r.broker_status || "—"}</Td>
            <Td align="right">{r.broker_filled}</Td>
            <Td tone={tone(r.state)}>
              {r.state} <span className="text-xs text-muted-foreground">{r.why}</span>
            </Td>
          </Row>
        ))}
      </Table>

      <Table
        caption="Positions — where the holdings landed against where the plan aimed"
        head={
          <>
            <Th>Stock</Th>
            <Th align="right">Planned final</Th>
            <Th align="right">Held</Th>
            <Th align="right">Diff</Th>
            <Th>Why</Th>
          </>
        }
      >
        {data.positions.rows.map((r) => (
          <Row key={r.symbol}>
            <Td>{r.symbol}</Td>
            <Td align="right">{r.planned_final ?? "—"}</Td>
            <Td align="right">{r.held.toLocaleString("en-IN")}</Td>
            <Td align="right" tone={tone(r.state)}>
              {r.diff > 0 ? `+${r.diff}` : r.diff}
            </Td>
            <Td>
              <span className="text-xs text-muted-foreground">{r.why}</span>
            </Td>
          </Row>
        ))}
      </Table>

      <Card title="Stops">
        {data.stops.findings.length ? (
          <ul className="grid gap-1.5 text-sm">
            {data.stops.findings.map((f, i) => (
              <li key={`${f.kind}-${f.symbol}-${i}`}>
                <b className={BAD_KINDS.has(f.kind) ? "text-negative" : ""}>{f.kind}</b> {f.symbol}{" "}
                <span className="text-muted-foreground">— {f.detail}</span>
              </li>
            ))}
          </ul>
        ) : (
          <Muted>Every tradeable position carries exactly the cover it should.</Muted>
        )}
      </Card>
      <Muted>
        Loading this section asks the desk to compare against the broker; it records any fills the broker reports,
        exactly as the desk console does on load. Fixing a stop stays on the desk.
      </Muted>
    </div>
  );
}
