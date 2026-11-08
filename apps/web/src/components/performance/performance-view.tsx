"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { Empty, Notice, Row, Stat, StatRow, Table, Td, Th, pct, price, rupees, toneOf } from "@/components/desk/ui";
import type { Holding, Holdings } from "@/lib/desk/fetch";
import { cn } from "@/lib/utils";

import { LiveHoldings } from "./live-holdings";
import { Badge, Card } from "./parts";
import type { PerfMetric, PerformanceData } from "./types";

// Read-only port of the desk's /performance (templates/performance.html). Nothing here can move money.

const SERIES_STROKES = ["stroke-accent", "stroke-info", "stroke-muted-foreground", "stroke-warning"];
const SERIES_FILLS = ["bg-accent", "bg-info", "bg-muted-foreground", "bg-warning"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const f2 = (v: number) => v.toFixed(2);
const signed = (v: number, dp = 2) => `${v > 0 ? "+" : ""}${v.toFixed(dp)}`;
const num = (v: number | null | undefined, dp: number) =>
  v === null || v === undefined || !Number.isFinite(v) ? "—" : v.toFixed(dp);

function MetricValue({ m }: { m: PerfMetric }) {
  if (m.value === null) return <span className="text-muted-foreground">—</span>;
  if (m.polarity === "up") {
    return <span className={toneOf(m.value)}>{m.unit === "%" ? signed(m.value) : f2(m.value)}{m.unit}</span>;
  }
  if (m.polarity === "loss") {
    const v = m.value < 0 ? m.value : -m.value;
    return (
      <span className={Math.abs(m.value) > 0.005 ? "text-negative" : "text-muted-foreground"}>
        {f2(v)}{m.unit}
      </span>
    );
  }
  return <span>{f2(m.value)}{m.unit}</span>;
}

function monthStyle(v: number) {
  const a = Math.round(Math.min(Math.abs(v) / 10, 0.85) * 100);
  const token = v > 0 ? "--positive" : "--negative";
  return { background: `color-mix(in srgb, var(${token}) ${a}%, transparent)` };
}

type HFilter = "all" | "win" | "lose" | "pledged" | "excluded";
type HKey = "symbol" | "quantity" | "average_price" | "price" | "invested" | "value" | "unrealised" | "unrealised_pct" | "weight";
interface HRow extends Holding {
  invested: number | null;
  weight: number | null;
}

function HoldingsBlock({
  holdings,
  cash,
  reason,
  liveError,
}: {
  holdings: Holdings | null;
  cash: number;
  reason: string | null;
  liveError: string | null;
}) {
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<HFilter>("all");
  const [sort, setSort] = useState<{ key: HKey; dir: 1 | -1 } | null>(null);

  const rows = useMemo<HRow[]>(() => {
    if (!holdings) return [];
    const nav = holdings.total_value + cash;
    return holdings.rows.map((r) => ({
      ...r,
      invested: r.average_price === null ? null : r.average_price * r.quantity,
      weight: r.value === null || nav <= 0 ? null : (r.value / nav) * 100,
    }));
  }, [holdings, cash]);

  if (!holdings) {
    return (
      <Card title="Holdings">
        <p className="p-4 text-sm text-muted-foreground">
          Not available: {reason ?? "no snapshot"}. The record below is stored and does not need one.
        </p>
      </Card>
    );
  }

  const invested = rows.reduce((s, r) => s + (r.invested ?? 0), 0);
  const value = rows.reduce((s, r) => s + (r.value ?? 0), 0);
  const pnl = value - invested;
  const nav = value + cash;
  const byWeight = [...rows].sort((a, b) => (b.weight ?? 0) - (a.weight ?? 0));
  const top5 = byWeight.slice(0, 5).reduce((s, r) => s + (r.weight ?? 0), 0);
  const largest = byWeight[0];

  const needle = q.trim().toUpperCase();
  const shown = rows
    .filter((r) => !needle || r.symbol.toUpperCase().includes(needle))
    .filter((r) => {
      if (filter === "win") return (r.unrealised ?? 0) > 0;
      if (filter === "lose") return (r.unrealised ?? 0) <= 0;
      if (filter === "pledged") return r.pledged_qty > 0;
      if (filter === "excluded") return r.excluded;
      return true;
    });
  if (sort) {
    shown.sort((a, b) => {
      const av = a[sort.key];
      const bv = b[sort.key];
      if (typeof av === "string" && typeof bv === "string") return av.localeCompare(bv) * sort.dir;
      // A missing value sorts last whichever way the column points.
      if (av === null) return 1;
      if (bv === null) return -1;
      return ((av as number) - (bv as number)) * sort.dir;
    });
  }

  const head: [HKey, string, boolean][] = [
    ["symbol", "Symbol", false],
    ["quantity", "Qty", true],
    ["average_price", "Avg", true],
    ["price", "Price", true],
    ["invested", "Invested", true],
    ["value", "Value", true],
    ["unrealised", "P&L", true],
    ["unrealised_pct", "P&L %", true],
    ["weight", "Weight", true],
  ];
  const onSort = (key: HKey) =>
    setSort((s) => ({ key, dir: s?.key === key ? ((-s.dir) as 1 | -1) : -1 }));

  const exportCsv = () => {
    const lines = [head.map(([, l]) => l).join(",")];
    for (const r of shown) {
      lines.push(
        [r.symbol, r.quantity, r.average_price, r.price, r.invested, r.value, r.unrealised, r.unrealised_pct, r.weight]
          .map((v) => `"${v === null ? "" : typeof v === "number" ? String(Math.round(v * 100) / 100) : v}"`)
          .join(","),
      );
    }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
    a.download = "holdings.csv";
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <Card title="Holdings" meta={`${rows.length} positions · snapshot ${holdings.as_of}`}>
      <div className="flex flex-col gap-4 p-4">
        <StatRow>
          <Stat label="Invested" value={rupees(invested)} />
          <Stat label="Current value" value={rupees(value)} />
          <Stat
            label="Unrealised P&L"
            value={rupees(pnl)}
            tone={toneOf(pnl)}
            {...(invested > 0 ? { hint: `${signed((pnl / invested) * 100)}% on cost` } : {})}
          />
          <Stat label="Cash" value={rupees(cash)} hint={`${nav > 0 ? ((cash / nav) * 100).toFixed(1) : "0.0"}% of NAV`} />
          <Stat
            label="Concentration"
            value={`${top5.toFixed(1)}%`}
            {...(largest ? { hint: `top five · largest ${largest.symbol} at ${num(largest.weight, 1)}%` } : {})}
          />
        </StatRow>
        {holdings.excluded_value > 0 ? (
          <Notice>{rupees(holdings.excluded_value)} sits in instruments the strategy does not manage.</Notice>
        ) : null}
      </div>
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
          onChange={(e) => setFilter(e.target.value as HFilter)}
          aria-label="Filter holdings"
          className="h-8 rounded-md border border-border bg-background px-2 text-sm"
        >
          <option value="all">All positions</option>
          <option value="win">In profit</option>
          <option value="lose">At a loss</option>
          <option value="pledged">Pledged</option>
          <option value="excluded">Excluded</option>
        </select>
        <span className="text-xs text-muted-foreground">
          {shown.length === rows.length ? `${rows.length} positions` : `${shown.length} of ${rows.length} positions`}
        </span>
        <button
          type="button"
          onClick={exportCsv}
          className="ml-auto rounded-md border border-border/70 px-2.5 py-1 text-xs font-medium hover:bg-muted"
        >
          Export CSV
        </button>
      </div>
      <div className="overflow-x-auto border-t border-border/60">
        <table className="w-full text-sm tabular-nums">
          <caption className="sr-only">Holdings</caption>
          <thead>
            <tr className="border-b border-border bg-muted/50 text-left text-xs font-medium text-muted-foreground">
              {head.map(([key, label, right]) => (
                <th
                  key={key}
                  scope="col"
                  aria-sort={sort?.key === key ? (sort.dir === -1 ? "descending" : "ascending") : "none"}
                  className={cn("py-2.5 pl-4 pr-3", right && "text-right")}
                >
                  <button type="button" onClick={() => onSort(key)} title={`Sort by ${label}`} className="font-medium">
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
                    {r.pledged_qty ? <Badge>P</Badge> : null}
                    {r.excluded ? <Badge tone="warn">excluded</Badge> : null}
                  </span>
                </Td>
                <Td align="right">{r.quantity}</Td>
                <Td align="right">{price(r.average_price)}</Td>
                <Td align="right">{price(r.price)}</Td>
                <Td align="right">{rupees(r.invested)}</Td>
                <Td align="right">{rupees(r.value)}</Td>
                <Td align="right" tone={toneOf(r.unrealised)}>{rupees(r.unrealised)}</Td>
                <Td align="right" tone={toneOf(r.unrealised_pct)}>{pct(r.unrealised_pct, { sign: true })}</Td>
                <Td align="right">{r.weight === null ? "–" : `${r.weight.toFixed(1)}%`}</Td>
              </Row>
            ))}
          </tbody>
        </table>
      </div>
      {liveError ? (
        <p className="border-t border-border/60 px-4 py-3 text-xs text-muted-foreground">
          Live holdings unavailable ({liveError}); showing the latest end-of-day snapshot.
        </p>
      ) : null}
    </Card>
  );
}

export function PerformanceView({
  title,
  holdings,
  holdingsReason,
}: {
  title: string;
  holdings: Holdings | null;
  holdingsReason: string | null;
}) {
  const [p, setP] = useState<PerformanceData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    fetch("/desk/performance/data", { credentials: "same-origin", cache: "no-store" })
      .then(async (r) => {
        if (!r.ok) throw new Error(`the desk answered ${r.status}`);
        return (await r.json()) as PerformanceData;
      })
      .then((d) => live && setP(d))
      .catch((e: unknown) => live && setError(e instanceof Error ? e.message : "the desk did not answer"));
    return () => {
      live = false;
    };
  }, []);

  if (error) return <Empty title={title} body={`Performance is unavailable: ${error}.`} />;
  if (!p) return <p className="text-sm text-muted-foreground">Loading the record…</p>;
  if (!p.available) {
    return (
      <Empty
        title={title}
        body={`${p.message ?? "No track record yet."} Performance is measured from stored end-of-day snapshots, so the record starts the first time one is taken and grows daily.`}
      />
    );
  }

  const thin = p.observations < 63;
  const tr = p.index_value - 100;
  const priceReturn = Object.values(p.series_used).includes("PRI");

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-center gap-4">
        <div className="grid size-14 place-items-center rounded-xl border border-border/70 bg-card text-xl font-semibold tabular-nums">
          {p.index_value.toFixed(0)}
        </div>
        <div className="min-w-0 flex-1">
          <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <span>{p.inception} → {p.as_of}</span>
            <span aria-hidden="true">·</span>
            <span>{p.observations} sessions</span>
            <Badge tone={thin ? "warn" : "ok"}>{thin ? "short history" : "measured"}</Badge>
          </div>
        </div>
        <Link
          href="/tradebook"
          className="rounded-md border border-border/70 bg-card px-3 py-1.5 text-sm font-medium hover:bg-muted"
        >
          Tradebook
        </Link>
      </header>

      <LiveHoldings
        fallback={(liveError) => (
          <HoldingsBlock holdings={holdings} cash={p.cash} reason={holdingsReason} liveError={liveError} />
        )}
      />

      <div className="grid gap-5 lg:grid-cols-3">
        <div className="flex min-w-0 flex-col gap-5 lg:col-span-2">
          {thin ? (
            <Notice>
              <b>{p.observations} sessions since {p.inception}.</b> Ratios below are arithmetic, not
              evidence: an annualised figure from a few weeks extrapolates noise. Figures drawn from too
              little data are marked. Volatility needs about 21 sessions, risk-adjusted ratios about 63.
            </Notice>
          ) : null}

          <Card title="Performance" meta={`as of ${p.as_of}`}>
            <div className="flex flex-col gap-3 p-4">
              <StatRow>
                <Stat label="Current value" value={rupees(p.nav)} hint="Net asset value, including cash" />
                <Stat label="Total return" value={`${signed(tr)}%`} tone={toneOf(tr)} hint={`Time-weighted, since ${p.inception}`} />
                <Stat label="Index value" value={f2(p.index_value)} hint="Rebased to 100 at inception" />
              </StatRow>
              <StatRow>
                <Stat label="Positions" value={String(p.positions)} />
                <Stat label="Cash" value={rupees(p.cash)} hint={`${p.cash_pct.toFixed(1)}% of NAV`} />
                <Stat
                  label="Cashflows recorded"
                  value={p.has_cashflows ? "Yes" : "None"}
                  {...(p.has_cashflows ? {} : { tone: "text-muted-foreground" })}
                  hint="XIRR needs these"
                />
              </StatRow>
            </div>
          </Card>

          <Card title="Headline">
            <dl className="grid grid-cols-[repeat(auto-fit,minmax(11rem,1fr))] gap-3 p-4">
              {p.metrics.map((m) => (
                <div key={m.label} className="rounded-xl border border-border/70 p-3">
                  <dt className="eyebrow">{m.label}</dt>
                  <dd className="mt-1 text-xl font-semibold tabular-nums">
                    <MetricValue m={m} />
                  </dd>
                  <div className="mt-1 text-xs leading-relaxed text-muted-foreground">{m.note}</div>
                  {!m.flag.meaningful ? (
                    <div className="mt-1 text-[11px] font-medium text-warning">
                      {m.flag.n} of {m.flag.need} sessions needed
                    </div>
                  ) : null}
                </div>
              ))}
            </dl>
          </Card>

          <Card title="Equity curve" meta="rebased to 100 at inception">
            <div className="p-4">
              {p.chart.series[0]?.points ? (
                <>
                  <svg viewBox="0 -10 1000 290" className="w-full" role="img" aria-label="Portfolio index against benchmarks, rebased to 100 at inception">
                    {p.chart.series.map((s, i) => (
                      <polyline
                        key={s.name}
                        points={s.points}
                        fill="none"
                        className={SERIES_STROKES[i % 4]}
                        strokeWidth={i === 0 ? 2.5 : 1.6}
                        strokeLinejoin="round"
                        strokeLinecap="round"
                        {...(i > 0 ? { strokeDasharray: "4 3" } : {})}
                      />
                    ))}
                  </svg>
                  <div className="mt-2 flex justify-between text-xs text-muted-foreground tabular-nums">
                    <span>{p.chart.start}</span>
                    <span>{p.chart.lo.toFixed(1)} – {p.chart.hi.toFixed(1)}</span>
                    <span>{p.chart.end}</span>
                  </div>
                  <div className="mt-4 flex flex-wrap gap-x-5 gap-y-2 text-sm">
                    {p.chart.series.map((s, i) => (
                      <span key={s.name} className="inline-flex items-center gap-2">
                        <i className={cn("inline-block size-2.5 rounded-sm", SERIES_FILLS[i % 4])} />
                        <span className="text-muted-foreground">{s.name}</span>
                        <b className="tabular-nums">{s.last.toFixed(1)}</b>
                        <Badge>{s.type}</Badge>
                      </span>
                    ))}
                  </div>
                  {priceReturn ? (
                    <p className="mt-4 text-xs text-muted-foreground">
                      <Badge tone="warn">price return</Badge> A benchmark shown as PRI excludes dividends, so
                      it understates the index by roughly its dividend yield.
                    </p>
                  ) : null}
                  {p.benchmark_notes.map((n) => (
                    <p key={n} className="mt-2 text-xs text-muted-foreground">{n}</p>
                  ))}
                </>
              ) : (
                <p className="text-sm text-muted-foreground">At least two snapshots are needed to draw a curve.</p>
              )}
            </div>
          </Card>

          <div className="grid gap-5 sm:grid-cols-2">
            <Card title="Underwater">
              <div className="p-4">
                {p.underwater.points ? (
                  <>
                    <svg viewBox="0 0 1000 100" className="w-full" role="img" aria-label={`Drawdown from the running peak, worst ${f2(p.underwater.worst)} percent`}>
                      <line x1={0} y1={0} x2={1000} y2={0} className="stroke-border" strokeWidth={1} />
                      <polyline points={p.underwater.points} fill="none" className="stroke-negative" strokeWidth={2} strokeLinejoin="round" />
                    </svg>
                    <div className="mt-2 flex justify-between text-xs text-muted-foreground">
                      <span>0%</span>
                      <span>worst {f2(p.underwater.worst)}%</span>
                    </div>
                  </>
                ) : (
                  <p className="text-sm text-muted-foreground">Not enough history to plot.</p>
                )}
              </div>
            </Card>
            <Card title="Deepest fall">
              <table className="w-full text-sm tabular-nums">
                <tbody>
                  <Row><Td>Depth</Td><Td align="right" tone="text-negative">{f2(p.drawdown.depth_pct)}%</Td></Row>
                  <Row><Td>Peak</Td><Td align="right">{p.drawdown.peak ?? "—"}</Td></Row>
                  <Row><Td>Trough</Td><Td align="right">{p.drawdown.trough ?? "—"}</Td></Row>
                  <Row>
                    <Td>Recovered</Td>
                    <Td align="right" tone={p.drawdown.recovered ? "text-positive" : "text-negative"}>
                      {p.drawdown.recovered ? p.drawdown.recovery : "still under water"}
                    </Td>
                  </Row>
                  <Row><Td>Duration</Td><Td align="right">{p.drawdown.duration_days} days</Td></Row>
                  <Row>
                    <Td>Current</Td>
                    <Td align="right" tone={p.drawdown.current_pct < -0.005 ? "text-negative" : "text-muted-foreground"}>
                      {f2(p.drawdown.current_pct)}%
                    </Td>
                  </Row>
                </tbody>
              </table>
            </Card>
          </div>

          {p.relative ? (
            <Card title={<>Against {p.relative.name} <Badge>{p.relative.series_type}</Badge></>}>
              {!p.relative_flag.meaningful ? (
                <div className="px-4 pt-4">
                  <Notice>
                    <b>{p.relative_flag.n} of {p.relative_flag.need} sessions.</b> Beta and tracking error
                    need a meaningful sample before they describe anything.
                  </Notice>
                </div>
              ) : null}
              <Table
                caption={`Against ${p.relative.name}`}
                head={<><Th>Measure</Th><Th align="right">Value</Th><Th>Reading</Th></>}
              >
                <Row><Td>Beta</Td><Td align="right">{num(p.relative.beta, 3)}</Td><Td tone="text-muted-foreground">Above 1 moves more than the index</Td></Row>
                <Row>
                  <Td>Alpha (annualised)</Td>
                  <Td align="right" tone={(p.relative.alpha ?? 0) > 0 ? "text-positive" : "text-negative"}>
                    {p.relative.alpha === null ? "—" : `${signed(p.relative.alpha * 100)}%`}
                  </Td>
                  <Td tone="text-muted-foreground">Return beyond what beta explains</Td>
                </Row>
                <Row>
                  <Td>Tracking error</Td>
                  <Td align="right">{p.relative.tracking_error === null ? "—" : `${f2(p.relative.tracking_error * 100)}%`}</Td>
                  <Td tone="text-muted-foreground">How far the paths diverge</Td>
                </Row>
                <Row><Td>Information ratio</Td><Td align="right">{num(p.relative.information_ratio, 3)}</Td><Td tone="text-muted-foreground">Excess return per unit of divergence</Td></Row>
                <Row><Td>Up capture</Td><Td align="right">{num(p.relative.up_capture, 2)}</Td><Td tone="text-muted-foreground">Share of the index&apos;s up months captured</Td></Row>
                <Row><Td>Down capture</Td><Td align="right">{num(p.relative.down_capture, 2)}</Td><Td tone="text-muted-foreground">Below 1 is good: you fell less</Td></Row>
              </Table>
            </Card>
          ) : null}

          <Card title="Monthly returns">
            {p.monthly && p.monthly_flag.meaningful ? (
              <div className="overflow-x-auto">
                <table className="w-full text-sm tabular-nums">
                  <caption className="sr-only">Monthly returns, percent</caption>
                  <thead>
                    <tr className="border-b border-border bg-muted/50 text-xs font-medium text-muted-foreground">
                      <th scope="col" className="py-2.5 pl-4 pr-3 text-left">Year</th>
                      {MONTHS.map((m) => (
                        <th key={m} scope="col" className="px-2 py-2.5 text-right">{m}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {p.monthly.years.map((y, yi) => (
                      <tr key={y} className="border-b border-border/60 last:border-0">
                        <td className="py-2.5 pl-4 pr-3 font-medium">{y}</td>
                        {(p.monthly?.rows[yi] ?? []).map((v, mi) =>
                          v === null ? (
                            <td key={MONTHS[mi]} className="px-2 py-2.5 text-right text-muted-foreground">·</td>
                          ) : (
                            <td key={MONTHS[mi]} className="px-2 py-2.5 text-right font-medium" style={monthStyle(v)}>
                              {signed(v, 1)}
                            </td>
                          ),
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="p-4 text-sm text-muted-foreground">
                A month-by-month grid needs at least {p.monthly_flag.need} completed months; there{" "}
                {p.monthly_flag.n === 1 ? "is" : "are"} {p.monthly_flag.n}. It fills in as the record grows.
              </p>
            )}
          </Card>

          <Card title="Closed trades" {...(p.trades ? { meta: `${p.trades.closed} round trips` } : {})}>
            {p.trades ? (
              <>
                <div className="p-4">
                  <StatRow>
                    <Stat label="Win rate" value={`${f2(p.trades.win_rate)}%`} tone={toneOf(p.trades.win_rate)} />
                    <Stat label="Avg win" value={rupees(p.trades.avg_win)} tone={toneOf(p.trades.avg_win)} />
                    <Stat label="Avg loss" value={rupees(p.trades.avg_loss)} tone={toneOf(p.trades.avg_loss)} />
                    <Stat label="Payoff" value={`${f2(p.trades.payoff)}x`} tone={toneOf(p.trades.payoff)} />
                    <Stat label="Profit factor" value={`${f2(p.trades.profit_factor)}x`} tone={toneOf(p.trades.profit_factor)} />
                    <Stat label="Expectancy" value={rupees(p.trades.expectancy)} tone={toneOf(p.trades.expectancy)} />
                    <Stat label="Avg hold" value={`${f2(p.trades.avg_hold_days)}d`} />
                  </StatRow>
                </div>
                <Table
                  caption="Realised P&L by stock"
                  head={<><Th>Stock</Th><Th align="right">Trades</Th><Th align="right">P&amp;L</Th><Th align="right">Share</Th></>}
                >
                  {p.trades.by_symbol.map((r) => (
                    <Row key={r.symbol}>
                      <Td>{r.symbol}</Td>
                      <Td align="right">{r.trades}</Td>
                      <Td align="right" tone={r.pnl >= 0 ? "text-positive" : "text-negative"}>{rupees(r.pnl)}</Td>
                      <Td align="right" tone="text-muted-foreground">{r.share_pct.toFixed(1)}%</Td>
                    </Row>
                  ))}
                </Table>
              </>
            ) : (
              <p className="p-4 text-sm text-muted-foreground">
                No closed trades recorded. Realised P&amp;L, win rate and holding periods come from the
                tax-lot history — import a Console tradebook on the desk and this section fills in.
              </p>
            )}
          </Card>
        </div>

        <aside className="flex flex-col gap-5 lg:sticky lg:top-20 lg:self-start">
          <div className="flex flex-col gap-2 rounded-xl border border-border/70 bg-card p-4">
            <a href="/desk/" className="rounded-md bg-accent px-3 py-2 text-center text-sm font-medium text-accent-foreground hover:opacity-90">
              Go to the desk
            </a>
            <a href="/desk/ops" className="rounded-md border border-border/70 px-3 py-2 text-center text-sm font-medium hover:bg-muted">
              Run collection
            </a>
          </div>
          <div>
            <h3 className="mb-2 px-1 text-sm font-semibold">Where it stands</h3>
            <div className="overflow-hidden rounded-xl border border-border/70 bg-card">
              <table className="w-full text-sm tabular-nums">
                <tbody>
                  <Row><Td>NAV</Td><Td align="right">{rupees(p.nav)}</Td></Row>
                  <Row><Td>Index value</Td><Td align="right">{p.index_value.toFixed(4)}</Td></Row>
                  <Row><Td>Positions</Td><Td align="right">{p.positions}</Td></Row>
                  <Row><Td>Cash</Td><Td align="right">{rupees(p.cash)}</Td></Row>
                  <Row><Td>Sessions</Td><Td align="right">{p.observations}</Td></Row>
                </tbody>
              </table>
            </div>
          </div>
          <div className="rounded-xl border border-border/70 bg-card p-4 text-sm">
            <h3 className="font-semibold">How this is measured</h3>
            <p className="mt-2 text-muted-foreground">
              The index is a time-weighted chain, so deposits and withdrawals never register as performance.
            </p>
            {!p.has_cashflows ? (
              <>
                <p className="mt-3 text-muted-foreground">Without recorded cashflows XIRR cannot be computed:</p>
                <code className="mt-2 block overflow-x-auto rounded-lg bg-muted px-3 py-2 text-xs">
                  python -m app.analytics.snapshot --cashflow -100000
                </code>
              </>
            ) : null}
          </div>
        </aside>
      </div>
    </div>
  );
}
