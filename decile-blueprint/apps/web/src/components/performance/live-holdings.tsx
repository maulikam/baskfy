"use client";

import { type ReactNode, useEffect, useState } from "react";

import { Notice, Row, Stat, StatRow, Td, price, rupees, toneOf } from "@/components/desk/ui";
import { deskGet } from "@/lib/desk-actions/client";
import { cn } from "@/lib/utils";

import { Badge, Card } from "./parts";

// Shape of the desk's `GET /desk/performance/holdings` (performance_view::holdings_block).
interface LiveRow {
  symbol: string;
  quantity: number;
  pledged_qty: number;
  t1_quantity: number;
  avg_price: number;
  last_price: number;
  invested: number;
  value: number;
  pnl: number;
  pnl_pct: number | null;
  day_change_pct: number | null;
  day_pnl: number | null;
  cap_band: string | null;
  stop: string;
  first_bought?: string;
  held_days?: number;
  term?: "long" | "short";
  weight_pct: number;
}

interface LiveSummary {
  positions: number;
  invested: number;
  value: number;
  pnl: number;
  pnl_pct: number | null;
  day_pnl: number | null;
  day_pnl_pct: number | null;
  cash: number;
  cash_pct: number | null;
  top5_weight: number;
  largest: string | null;
  largest_weight: number | null;
}

interface LiveBlock {
  available: boolean;
  reason?: string;
  rows?: LiveRow[];
  summary?: LiveSummary;
  allocation?: { rows: { band: string; value: number; pct: number }[]; total: number };
  warnings?: { level: string; symbol: string | null; text: string }[];
  scan_context?: boolean;
  gaps: [string, string][];
  as_of?: string;
}

type Filter = "all" | "win" | "lose" | "pledged" | "unprotected" | "long" | "short";
type Key =
  | "symbol" | "quantity" | "avg_price" | "last_price" | "invested" | "value" | "pnl" | "pnl_pct"
  | "weight_pct" | "day_change_pct" | "day_pnl" | "cap_band" | "held_days" | "stop";

const COLUMNS: [Key, string, boolean][] = [
  ["symbol", "Symbol", false],
  ["quantity", "Qty", true],
  ["avg_price", "Avg", true],
  ["last_price", "LTP", true],
  ["invested", "Invested", true],
  ["value", "Value", true],
  ["pnl", "P&L", true],
  ["pnl_pct", "P&L %", true],
  ["weight_pct", "Weight", true],
  ["day_change_pct", "Day %", true],
  ["day_pnl", "Day P&L", true],
  ["cap_band", "Band", false],
  ["held_days", "Held", true],
  ["stop", "Stop", false],
];

const STOP_TONE: Record<string, "ok" | "warn" | "bad"> = {
  ok: "ok",
  too_close: "warn",
  too_far: "warn",
  partial: "bad",
  missing: "bad",
  excess: "bad",
};

const sgn = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}`;

function cellText(r: LiveRow, key: Key): string {
  const v = r[key];
  if (key === "held_days") return r.held_days === undefined ? "—" : `${r.held_days}d ${(r.term ?? "").slice(0, 2).toUpperCase()}`;
  if (v === null || v === undefined) return "—";
  return String(v);
}

function Table({ rows, summary }: { rows: LiveRow[]; summary: LiveSummary }) {
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [sort, setSort] = useState<{ key: Key; dir: 1 | -1 } | null>(null);

  const needle = q.trim().toUpperCase();
  const shown = rows.filter((r) => {
    if (needle && !r.symbol.toUpperCase().includes(needle)) return false;
    if (filter === "win") return r.pnl > 0;
    if (filter === "lose") return r.pnl <= 0;
    if (filter === "pledged") return r.pledged_qty > 0;
    if (filter === "unprotected") return r.stop === "missing" || r.stop === "partial";
    if (filter === "long" || filter === "short") return r.term === filter;
    return true;
  });
  if (sort) {
    shown.sort((a, b) => {
      const av = a[sort.key];
      const bv = b[sort.key];
      // A missing value sorts last whichever way the column points.
      if (av === null || av === undefined) return bv === null || bv === undefined ? 0 : 1;
      if (bv === null || bv === undefined) return -1;
      if (typeof av === "number" && typeof bv === "number") return (av - bv) * sort.dir;
      return String(av).localeCompare(String(bv)) * sort.dir;
    });
  }

  const exportCsv = () => {
    const lines = [COLUMNS.map(([, l]) => l).join(",")];
    for (const r of shown) lines.push(COLUMNS.map(([k]) => `"${cellText(r, k).replace(/"/g, '""')}"`).join(","));
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
    a.download = "holdings.csv";
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <>
      <div className="flex flex-wrap items-center gap-2 border-t border-border/60 px-4 py-3 text-sm">
        <input
          type="search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search symbol"
          aria-label="Search holdings"
          className="h-8 w-48 rounded-md border border-border bg-background px-2 text-sm"
        />
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value as Filter)}
          aria-label="Filter holdings"
          className="h-8 rounded-md border border-border bg-background px-2 text-sm"
        >
          <option value="all">All positions</option>
          <option value="win">In profit</option>
          <option value="lose">At a loss</option>
          <option value="pledged">Pledged</option>
          <option value="unprotected">No full stop</option>
          <option value="long">Long term</option>
          <option value="short">Short term</option>
        </select>
        <span className="text-xs text-muted-foreground">
          {shown.length === rows.length ? `${rows.length} positions` : `${shown.length} of ${rows.length} positions`}
        </span>
        <a href="/desk/stops" className="ml-auto rounded-md border border-border/70 px-2.5 py-1 text-xs font-medium hover:bg-muted">
          Stops
        </a>
        <button
          type="button"
          onClick={exportCsv}
          className="rounded-md border border-border/70 px-2.5 py-1 text-xs font-medium hover:bg-muted"
        >
          Export CSV
        </button>
      </div>
      <div className="overflow-x-auto border-t border-border/60">
        <table className="w-full text-sm tabular-nums">
          <caption className="sr-only">Holdings, {summary.positions} positions</caption>
          <thead>
            <tr className="border-b border-border bg-muted/50 text-left text-xs font-medium text-muted-foreground">
              {COLUMNS.map(([key, label, right]) => (
                <th
                  key={key}
                  scope="col"
                  aria-sort={sort?.key === key ? (sort.dir === -1 ? "descending" : "ascending") : "none"}
                  className={cn("whitespace-nowrap py-2.5 pl-4 pr-3", right && "text-right")}
                >
                  <button
                    type="button"
                    title={`Sort by ${label}`}
                    className="font-medium"
                    onClick={() => setSort((s) => ({ key, dir: s?.key === key ? ((-s.dir) as 1 | -1) : -1 }))}
                  >
                    {label}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <Row key={r.symbol}>
                <Td>
                  <span className="inline-flex items-center gap-1.5 font-medium">
                    {r.symbol}
                    {r.pledged_qty ? <span title={`${r.pledged_qty} pledged`}><Badge>P</Badge></span> : null}
                    {r.t1_quantity ? <span title={`${r.t1_quantity} settling (T+1)`}><Badge tone="info">T1</Badge></span> : null}
                  </span>
                </Td>
                <Td align="right">{r.quantity}</Td>
                <Td align="right">{price(r.avg_price)}</Td>
                <Td align="right">{price(r.last_price)}</Td>
                <Td align="right">{rupees(r.invested)}</Td>
                <Td align="right">{rupees(r.value)}</Td>
                <Td align="right" tone={toneOf(r.pnl)}>{rupees(r.pnl)}</Td>
                <Td align="right" tone={toneOf(r.pnl_pct)}>{sgn(r.pnl_pct)}</Td>
                <Td align="right">{r.weight_pct}%</Td>
                <Td align="right" tone={toneOf(r.day_change_pct)}>{sgn(r.day_change_pct)}</Td>
                <Td align="right" tone={toneOf(r.day_pnl)}>{rupees(r.day_pnl)}</Td>
                <Td tone="text-muted-foreground">{r.cap_band ?? "—"}</Td>
                <Td align="right">
                  <span title={r.first_bought ? `first bought ${r.first_bought}` : "no fill history"}>
                    {r.held_days === undefined ? (
                      "—"
                    ) : (
                      <span className="inline-flex items-center gap-1">
                        {r.held_days}d
                        <Badge {...(r.term === "long" ? { tone: "ok" as const } : {})}>
                          {(r.term ?? "").slice(0, 2).toUpperCase()}
                        </Badge>
                      </span>
                    )}
                  </span>
                </Td>
                <Td>
                  <Badge {...(STOP_TONE[r.stop] ? { tone: STOP_TONE[r.stop] } : {})}>{r.stop}</Badge>
                </Td>
              </Row>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function Gaps({ gaps }: { gaps: [string, string][] }) {
  return (
    <div className="border-t border-border/60 p-4">
      <h3 className="text-sm font-semibold">Not shown, and why</h3>
      <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
        {gaps.map(([name, why]) => (
          <li key={name}>
            <b className="text-foreground">{name}</b> — {why}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** The desk's live holdings block; on any failure renders `fallback` with the reason. */
export function LiveHoldings({ fallback }: { fallback: (reason: string) => ReactNode }) {
  const [block, setBlock] = useState<LiveBlock | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    deskGet<LiveBlock>("/performance/holdings")
      .then((b) => live && setBlock(b))
      .catch((e: unknown) => live && setError(e instanceof Error ? e.message : "the desk did not answer"));
    return () => {
      live = false;
    };
  }, []);

  if (error) return fallback(error);
  if (!block) return <p className="text-sm text-muted-foreground">Loading holdings…</p>;
  if (!block.available || !block.rows || !block.summary || !block.allocation) {
    return fallback(block.reason ?? "no broker session");
  }

  const s = block.summary;
  return (
    <Card title="Holdings" meta={`${s.positions} positions · priced ${block.as_of ?? ""}`}>
      <div className="flex flex-col gap-4 p-4">
        <StatRow>
          <Stat label="Invested" value={rupees(s.invested)} />
          <Stat label="Current value" value={rupees(s.value)} />
          <Stat
            label="Unrealised P&L"
            value={rupees(s.pnl)}
            tone={toneOf(s.pnl)}
            {...(s.pnl_pct !== null ? { hint: `${sgn(s.pnl_pct)}% on cost` } : {})}
          />
          <Stat
            label="Today"
            value={s.day_pnl === null ? "—" : rupees(s.day_pnl)}
            tone={toneOf(s.day_pnl)}
            {...(s.day_pnl_pct !== null ? { hint: `${sgn(s.day_pnl_pct)}%` } : {})}
          />
          <Stat label="Cash" value={rupees(s.cash)} hint={`${s.cash_pct ?? "—"}% of NAV`} />
          <Stat
            label="Concentration"
            value={`${s.top5_weight}%`}
            hint={`top five · largest ${s.largest ?? "—"} at ${s.largest_weight ?? "—"}%`}
          />
        </StatRow>

        {block.warnings && block.warnings.length > 0 ? (
          <div className="flex flex-col gap-2">
            {block.warnings.map((w) => (
              <p
                key={`${w.symbol ?? ""}:${w.text}`}
                role="status"
                className={cn(
                  "rounded-xl border p-3 text-sm leading-relaxed",
                  w.level === "bad" ? "border-negative/35 bg-negative-muted" : "border-warning/35 bg-warning-muted",
                )}
              >
                {w.symbol ? <b>{w.symbol} — </b> : null}
                {w.text}
              </p>
            ))}
          </div>
        ) : null}

        <div>
          <h3 className="mb-2 text-sm font-semibold">Allocation</h3>
          <div className="flex flex-col gap-1.5">
            {block.allocation.rows.map((a) => (
              <div key={a.band} className="grid grid-cols-[7rem_1fr_3.5rem] items-center gap-3 text-sm">
                <span className="truncate text-muted-foreground">{a.band}</span>
                <span className="h-2 overflow-hidden rounded-full bg-muted">
                  <span className="block h-full rounded-full bg-accent" style={{ width: `${Math.min(a.pct, 100)}%` }} />
                </span>
                <span className="text-right tabular-nums">{a.pct}%</span>
              </div>
            ))}
          </div>
          {!block.scan_context ? (
            <p className="mt-2 text-xs text-muted-foreground">
              Bands come from the most recent uploaded scan. Names absent from it are shown as
              unclassified rather than guessed into a band.
            </p>
          ) : null}
        </div>
        {s.positions === 0 ? <Notice>No positions held.</Notice> : null}
      </div>
      <Table rows={block.rows} summary={s} />
      <Gaps gaps={block.gaps} />
    </Card>
  );
}
