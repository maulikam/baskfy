"use client";

import type { ReactNode } from "react";

import { Row, Table, Td, Th } from "@/components/desk/ui";
import { Banner, Card, Muted, StaffOnly, useDeskPage } from "@/components/ops/shared";
import { Badge } from "@/components/ui/badge";
import { useIsStaff } from "@/components/shell/staff-context";

import type { Bars, BacktestPage, Curves, Robustness, Scatter, Subperiods, Timeline, Variant } from "./types";

const GRID = "var(--border)";
const AXIS = "var(--muted-foreground)";
const INK = "var(--foreground)";

const n = (v: number | null | undefined, d: number) => (v === null || v === undefined ? "—" : v.toFixed(d));
const sgn = (v: number | null | undefined, d: number) =>
  v === null || v === undefined ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(d)}%`;
const inr = (v: number) => `₹${Math.round(v).toLocaleString("en-IN")}`;
const title = (k: string) => k.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

function Svg({ w, h, label, children }: { w: number; h: number; label: string; children: ReactNode }) {
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" style={{ maxWidth: w, height: "auto", display: "block" }} role="img" aria-label={label}>
      {children}
    </svg>
  );
}

function ScatterChart({ sc, caveat }: { sc: Scatter; caveat: string | null | undefined }) {
  return (
    <Card title="Which design earns its complexity">
      <Muted className="mb-2">
        Return against the drawdown you had to sit through to get it. <b>Up and to the left is better.</b> Colour is
        the design family, not the ranking.
      </Muted>
      <div className="mb-2 flex flex-wrap gap-3 text-xs text-muted-foreground">
        {sc.families.map((f) => (
          <span key={f.label} className="inline-flex items-center gap-1.5">
            <i className="inline-block size-2.5 rounded-full" style={{ background: f.color }} />
            {f.label}
          </span>
        ))}
        <span>ring: best return per unit of drawdown</span>
      </div>
      <div className="overflow-x-auto">
        <Svg w={sc.width} h={sc.height} label="Return against maximum drawdown for each backtested variant">
          {sc.yticks.map((t) => (
            <g key={`y${t.px}`}>
              <line x1={sc.pad_l} y1={t.px} x2={sc.width - sc.pad_r} y2={t.px} stroke={GRID} />
              <text x={sc.pad_l - 9} y={t.px + 4} textAnchor="end" fill={AXIS} fontSize={10.5}>
                {n(t.v, 0)}%
              </text>
            </g>
          ))}
          {sc.xticks.map((t) => (
            <text key={`x${t.px}`} x={t.px} y={sc.height - sc.pad_b + 18} textAnchor="middle" fill={AXIS} fontSize={10.5}>
              {n(t.v, 0)}%
            </text>
          ))}
          <text x={sc.pad_l + sc.plot_w / 2} y={sc.height - 6} textAnchor="middle" fill={AXIS} fontSize={10.5}>
            MAXIMUM DRAWDOWN →
          </text>
          <text
            x={14}
            y={sc.pad_t + sc.plot_h / 2}
            textAnchor="middle"
            fill={AXIS}
            fontSize={10.5}
            transform={`rotate(-90 14 ${sc.pad_t + sc.plot_h / 2})`}
          >
            ← CAGR
          </text>
          {sc.marks.map((m) => (
            <g key={m.key}>
              {m.leader ? (
                <line x1={m.x} y1={m.y} x2={m.anchor === "start" ? m.lx - 3 : m.lx + 3} y2={m.ly - 3} stroke={AXIS} opacity={0.5} />
              ) : null}
              {m.best ? <circle cx={m.x} cy={m.y} r={11} fill="none" stroke={m.color} strokeWidth={1.5} opacity={0.55} /> : null}
              <circle cx={m.x} cy={m.y} r={6.5} fill={m.color} stroke="var(--card)" strokeWidth={2} />
              <text x={m.lx} y={m.ly} textAnchor={m.anchor} fill={INK} fontSize={11.5}>
                {m.key}
                {m.proxy ? " *" : ""}
              </text>
              <text x={m.lx} y={m.ly + 12} textAnchor={m.anchor} fill={AXIS} fontSize={10}>
                {m.cagr}% / −{m.dd}%
              </text>
            </g>
          ))}
        </Svg>
      </div>
      {caveat ? (
        <Muted className="mt-2">
          <b>*</b> Breadth was {caveat.toLowerCase()}. The tiered variants were measured without the input they were
          designed around, so their marks understate what the design would do with real breadth history.
        </Muted>
      ) : null}
    </Card>
  );
}

function CurveCharts({ cv }: { cv: Curves }) {
  const xAxis = (h: number) =>
    cv.xticks.map((t) => (
      <text key={`x${t.px}`} x={t.px} y={h - 6} textAnchor="middle" fill={AXIS} fontSize={10}>
        {t.label}
      </text>
    ));
  const yGrid = (ticks: typeof cv.eq_ticks, fmt: (t: (typeof cv.eq_ticks)[number]) => string) =>
    ticks.map((t) => (
      <g key={`g${t.px}`}>
        <line x1={cv.pad_l} y1={t.px} x2={cv.width - cv.pad_r} y2={t.px} stroke={GRID} />
        <text x={cv.pad_l - 8} y={t.px + 4} textAnchor="end" fill={AXIS} fontSize={10}>
          {fmt(t)}
        </text>
      </g>
    ));
  return (
    <Card title="Growth of ₹100, and what it cost to hold">
      <Muted className="mb-2">
        {cv.start} to {cv.end}. The equity axis is <b>logarithmic</b>, so the early drawdowns that decide whether a
        design is survivable stay visible.
      </Muted>
      <div className="overflow-x-auto">
        <Svg w={cv.width} h={cv.eq_h} label="Growth of 100 rupees by variant, logarithmic scale">
          {yGrid(cv.eq_ticks, (t) => t.label ?? "")}
          {xAxis(cv.eq_h)}
          {cv.lines.map((l) => (
            <path key={l.key} d={l.equity} fill="none" stroke={l.color} strokeWidth={l.callout ? 2 : 1.2} opacity={l.callout ? 1 : 0.4} strokeLinejoin="round" />
          ))}
          {cv.lines
            .filter((l) => l.callout)
            .map((l) => (
              <g key={`c${l.key}`}>
                <text x={cv.width - cv.pad_r + 7} y={l.label_y} fill={l.color} fontSize={11}>
                  {l.key}
                </text>
                <text x={cv.width - cv.pad_r + 7} y={l.label_y + 11} fill={AXIS} fontSize={10}>
                  {inr(l.final)}
                </text>
              </g>
            ))}
        </Svg>
        <p className="eyebrow mb-1 mt-3">Drawdown from peak</p>
        <Svg w={cv.width} h={cv.dd_h} label="Drawdown from peak by variant">
          {yGrid(cv.dd_ticks, (t) => t.label ?? "")}
          {xAxis(cv.dd_h)}
          {cv.lines.map((l) => (
            <path key={l.key} d={l.drawdown} fill="none" stroke={l.color} strokeWidth={l.callout ? 1.8 : 1.1} opacity={l.callout ? 0.95 : 0.35} strokeLinejoin="round" />
          ))}
        </Svg>
        {cv.has_exposure ? (
          <>
            <p className="eyebrow mb-1 mt-3">
              Equity exposure, {cv.exposure_window}-week trailing mean · the rest was cash
            </p>
            <Svg w={cv.width} h={cv.ex_h} label="Equity exposure by variant; the remainder was held in cash">
              {yGrid(cv.ex_ticks, (t) => `${t.v ?? ""}%`)}
              {xAxis(cv.ex_h)}
              {cv.lines.map((l) =>
                l.exposure ? (
                  <path key={l.key} d={l.exposure} fill="none" stroke={l.color} strokeWidth={l.callout ? 1.8 : 1.1} opacity={l.callout ? 0.95 : 0.3} strokeLinejoin="round" />
                ) : null,
              )}
              {cv.lines
                .filter((l) => l.callout && l.avg_exposure !== null)
                .map((l, i) => (
                  <text key={`a${l.key}`} x={cv.width - cv.pad_r + 7} y={cv.pad_t + 12 + i * 13} fill={l.color} fontSize={10}>
                    {l.key} {l.avg_exposure}%
                  </text>
                ))}
            </Svg>
          </>
        ) : null}
      </div>
      <Muted className="mt-2">
        Drawdown is computed daily and bucketed weekly by its <b>worst</b> value. Exposure is bucketed by its mean and
        shown as a {cv.exposure_window}-week trailing average; the whipsaw count is reported separately.
      </Muted>
    </Card>
  );
}

function SubperiodTable({ sp }: { sp: Subperiods }) {
  return (
    <Card title="The trade, window by window">
      <Muted className="mb-2">The tiered model against no overlay in periods chosen because they hurt.</Muted>
      <div className="overflow-x-auto">
        <table className="w-full text-sm tabular-nums">
          <thead>
            <tr className="text-left text-xs text-muted-foreground">
              <th className="py-1.5 pr-3">Period</th>
              <th className="pr-3">Variant</th>
              <th className="pr-3">CAGR</th>
              <th />
              <th className="pr-3">Max drawdown</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {sp.rows.flatMap((row) =>
              row.bars.map((b, i) => (
                <tr key={`${row.label}-${b.key}`} className="border-t border-border/50">
                  <td className="py-1.5 pr-3">{i === 0 ? row.label : ""}</td>
                  <td className="pr-3">
                    <code className="text-xs">{b.key}</code>
                  </td>
                  <td className="pr-3">{b.cagr.toFixed(2)}%</td>
                  <td style={{ width: sp.bar_w + 20 }}>
                    <span className="block h-[9px] rounded-sm" style={{ width: b.cagr_w, background: b.color }} />
                  </td>
                  <td className="pr-3 text-negative">{b.dd.toFixed(2)}%</td>
                  <td style={{ width: sp.bar_w + 20 }}>
                    <span className="block h-[9px] rounded-sm opacity-55" style={{ width: b.dd_w, background: b.color }} />
                  </td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function Timelines({ items }: { items: Timeline[] }) {
  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-base font-semibold">Event timelines</h2>
      <div className="grid gap-3 md:grid-cols-2">
        {items.map((t) => (
          <Card key={t.label}>
            <h3 className="text-sm font-semibold">{t.label}</h3>
            <Muted className="mb-2">
              {t.start} to {t.end}
            </Muted>
            {t.unavailable ? (
              <Muted>{t.unavailable}</Muted>
            ) : (
              <>
                <Svg w={t.width} h={t.height} label={`Equity exposure through ${t.label}`}>
                  {t.ticks.map((tk) => (
                    <g key={tk.px}>
                      <line x1={t.pad_l} y1={tk.px} x2={t.width - 16} y2={tk.px} stroke={GRID} />
                      <text x={t.pad_l - 7} y={tk.px + 4} textAnchor="end" fill={AXIS} fontSize={9.5}>
                        {tk.v}%
                      </text>
                    </g>
                  ))}
                  <path d={t.area} fill="#7b6ad0" opacity={0.18} />
                  <path d={t.path} fill="none" stroke="#7b6ad0" strokeWidth={1.8} strokeLinejoin="round" />
                  {t.changes.map((c, i) => (
                    <line key={i} x1={c.x} y1={t.pad_t} x2={c.x} y2={t.height - 24} stroke={AXIS} strokeDasharray="2 3" opacity={0.7} />
                  ))}
                  <text x={t.pad_l} y={t.height - 8} fill={AXIS} fontSize={9.5}>
                    {t.first}
                  </text>
                  <text x={t.width - 16} y={t.height - 8} textAnchor="end" fill={AXIS} fontSize={9.5}>
                    {t.last}
                  </text>
                </Svg>
                <Muted className="mt-1">
                  Held to <b>{t.min_exposure.toFixed(0)}%</b> at its lowest, {t.changes.length} tier change
                  {t.changes.length === 1 ? "" : "s"}.
                </Muted>
              </>
            )}
          </Card>
        ))}
      </div>
      <Muted>Exposure is drawn as a step: it was constant between weekly evaluations. Dashed rules mark tier changes.</Muted>
    </section>
  );
}

function RobustnessGrid({ rb }: { rb: Robustness }) {
  return (
    <Card title="Parameter sensitivity">
      <Muted className="mb-2">
        CAGR of the tiered model across every combination of buffer, confirmation days and R4 exposure. Reported, not
        ranked.
      </Muted>
      <div className="mb-2 flex items-center gap-1 text-xs text-muted-foreground">
        <span>{rb.min}%</span>
        {rb.ramp.map((c) => (
          <i key={c} className="inline-block h-2.5 w-4" style={{ background: c }} />
        ))}
        <span>{rb.max}%</span>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        {rb.grids.map((g) => (
          <div key={g.r4} className="overflow-x-auto">
            <Muted className="mb-1">
              R4 exposure <b>{g.r4.toFixed(0)}%</b>
            </Muted>
            <table className="text-xs tabular-nums">
              <thead>
                <tr>
                  <th className="px-2 py-1 text-left">buffer \ confirm</th>
                  {rb.confirms.map((c) => (
                    <th key={c} className="px-2 py-1">
                      {c}d
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rb.buffers.map((b) => (
                  <tr key={b}>
                    <td className="px-2 py-1">{(b / 100).toFixed(1)}%</td>
                    {rb.confirms.map((c) => {
                      const cell = g.cells.find((x) => x.buffer === b && x.confirm === c);
                      return cell ? (
                        <td key={c} className="px-2 py-1 font-bold" style={{ background: cell.color, color: rb.ink }}>
                          {cell.value.toFixed(2)}%
                          <span className="block text-[10px] font-normal">
                            dd {cell.dd}% · {cell.whip}/yr
                          </span>
                        </td>
                      ) : (
                        <td key={c} className="px-2 py-1 text-muted-foreground">
                          —
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
      </div>
      <Muted className="mt-2">
        {rb.narrow ? `The whole grid spans ${rb.spread} percentage points of CAGR, so the result is not an artefact of where these knobs sit. ` : ""}
        {rb.r4_inert ? "Every R4 setting produces an identical grid: R4 was never reached in this window." : ""}
      </Muted>
    </Card>
  );
}

function BarChart({ title: heading, note, bars, digits, suffix, strip }: { title: string; note: string; bars: Bars; digits: number; suffix: string; strip?: boolean }) {
  return (
    <Card title={heading}>
      <Muted className="mb-2">{note}</Muted>
      <div className="overflow-x-auto">
        <Svg w={bars.width} h={bars.height} label={heading}>
          {bars.bars.map((b) => (
            <g key={b.key}>
              <text x={bars.pad_l - 10} y={b.y + 17} textAnchor="end" fill={INK} fontSize={11}>
                {strip ? (b.label ?? b.key).replace("Ablation: ", "") : b.key}
              </text>
              <rect x={b.x} y={b.y + 6} width={b.w} height={14} rx={3} fill={b.color} />
              <text x={b.x + b.w + 8} y={b.y + 17} fill={AXIS} fontSize={11}>
                {b.value.toFixed(digits)}
                {suffix}
              </text>
            </g>
          ))}
        </Svg>
      </div>
    </Card>
  );
}

function VariantCells({ r }: { r: Variant }) {
  return (
    <>
      <Td align="right">{n(r.CAGR, 2)}%</Td>
      <Td align="right" tone="text-negative">
        {n(r.max_drawdown_pct, 2)}%
      </Td>
      <Td align="right">{n(r.volatility_pct, 2)}%</Td>
      <Td align="right">{n(r.sharpe, 3)}</Td>
      <Td align="right">{n(r.sortino, 3)}</Td>
      <Td align="right">{n(r.calmar, 3)}</Td>
      <Td align="right">{n(r.avg_equity_exposure, 1)}%</Td>
      <Td align="right">{n(r.pct_time_in_cash, 1)}%</Td>
      <Td align="right">{Math.trunc(r.transitions)}</Td>
      <Td align="right">{n(r.whipsaws_per_year, 2)}</Td>
      <Td align="right">{n(r.annual_turnover_pct, 0)}%</Td>
      <Td align="right">{n(r.total_cost_drag_pct_yr, 3)}%</Td>
    </>
  );
}

export function RegimeBacktestView() {
  const staff = useIsStaff();
  const { data, error } = useDeskPage<BacktestPage>("/regime/backtest");
  if (!staff) return <StaffOnly what="The regime backtest" />;
  if (error && !data) return <Banner tone="bad" tag="unavailable">{error}</Banner>;
  if (!data) return <Muted>Loading the stored backtest…</Muted>;

  const { b, charts } = data;
  if (!b) {
    return (
      <Card title="No backtest results stored">
        <Muted>
          Run it and save the artifact, then reload: <code>python -m scripts.backtest_regime --run --start 2005-04-01 --save</code>.
          Expected at <code>{data.path}</code>.
        </Muted>
      </Card>
    );
  }
  const variants = Object.entries(b.variants);

  return (
    <div className="flex flex-col gap-5">
      <Banner tone="warn" tag="limitations">
        <b>Read the limitations before the numbers.</b>
        {Object.entries(b.limitations).map(([k, v]) => (
          <div key={k} className="mt-1">
            <b>{title(k)}:</b> {v}
          </div>
        ))}
        <div className="mt-1">
          Window {b.generated_for.start} → {b.generated_for.end}.
        </div>
      </Banner>

      {charts?.scatter ? <ScatterChart sc={charts.scatter} caveat={charts.breadth_caveat} /> : null}
      {charts?.curves ? <CurveCharts cv={charts.curves} /> : null}
      {charts?.subperiods ? <SubperiodTable sp={charts.subperiods} /> : null}
      {charts?.timelines?.length ? <Timelines items={charts.timelines} /> : null}
      {charts?.robustness ? <RobustnessGrid rb={charts.robustness} /> : null}
      {charts?.sharpe ? (
        <BarChart title="Risk-adjusted return, ranked" note="Sharpe ratio: return per unit of volatility." bars={charts.sharpe} digits={3} suffix="" />
      ) : null}
      {charts?.ablation ? (
        <BarChart title="What each input contributed" note="CAGR of the tiered model with only the named inputs active." bars={charts.ablation} digits={2} suffix="%" strip />
      ) : null}

      <section className="flex flex-col gap-2">
        <h2 className="text-base font-semibold">Rolling returns and turnover</h2>
        <Table
          caption="Rolling returns and turnover by variant"
          head={
            <>
              <Th>Variant</Th>
              <Th align="right">Worst 1Y</Th>
              <Th align="right">Median 3Y</Th>
              <Th align="right">Median 5Y</Th>
              <Th align="right">Turnover /yr</Th>
              <Th align="right">Transitions /yr</Th>
              <Th align="right">Whipsaws /yr</Th>
              <Th align="right">Cost drag /yr</Th>
            </>
          }
        >
          {variants
            .filter(([k, r]) => r.status === "ok" && !k.startsWith("abl_"))
            .map(([k, r]) => (
              <Row key={k}>
                <Td>
                  <code className="text-xs">{k}</code>
                </Td>
                <Td align="right" tone={r.roll_1Y_worst_pct !== null && r.roll_1Y_worst_pct < 0 ? "text-negative" : ""}>
                  {sgn(r.roll_1Y_worst_pct, 1)}
                </Td>
                <Td align="right">{sgn(r.roll_3Y_med_pct, 1)}</Td>
                <Td align="right">{sgn(r.roll_5Y_med_pct, 1)}</Td>
                <Td align="right">{n(r.annual_turnover_pct, 0)}%</Td>
                <Td align="right">{n(r.transitions_per_year, 1)}</Td>
                <Td align="right">{n(r.whipsaws_per_year, 2)}</Td>
                <Td align="right">{n(r.total_cost_drag_pct_yr, 3)}%</Td>
              </Row>
            ))}
        </Table>
        <Muted>
          Turnover here is <b>regime turnover only</b>; the selection turnover from weekly ranking is not included,
          because this proxy trades an index rather than the stock book.
        </Muted>
      </section>

      <section className="flex flex-col gap-2">
        <h2 className="text-base font-semibold">Strategy comparison</h2>
        <div className="overflow-hidden rounded-xl border border-border/70 bg-card">
          <div className="overflow-x-auto">
            <table className="w-full text-sm tabular-nums">
              <caption className="sr-only">Strategy comparison</caption>
              <thead>
                <tr className="border-b border-border bg-muted/50 text-left text-xs font-medium text-muted-foreground">
                  {["Variant", "Design", "CAGR", "Max DD", "Vol", "Sharpe", "Sortino", "Calmar", "Avg exp", "% cash", "Transitions", "Whip/yr", "Turnover", "Cost drag"].map((h) => (
                    <th key={h} scope="col" className="whitespace-nowrap py-2.5 pl-4 pr-3">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {variants.map(([k, r]) => (
                  <tr key={k} className="border-b border-border/60 last:border-0">
                    <td className="py-2.5 pl-4 pr-3">
                      <code className="text-xs">{k}</code>
                    </td>
                    <td className="min-w-[14rem] py-2.5 pl-4 pr-3">
                      {r.label}
                      {r.note ? (
                        <div className="text-xs text-muted-foreground">
                          <Badge variant="warning">proxy</Badge> {r.note}
                        </div>
                      ) : null}
                    </td>
                    {r.status === "NOT AVAILABLE" ? (
                      <td colSpan={12} className="py-2.5 pl-4 pr-3 text-xs text-muted-foreground">
                        <Badge variant="warning">not available</Badge> {r.reason}
                      </td>
                    ) : (
                      <VariantCells r={r} />
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        <Muted>Reported, not endorsed. Turnover is measured in NAV fractions at trade time; cost drag is per year.</Muted>
      </section>

      <div className="grid gap-3 md:grid-cols-2">
        <Card title="Tier distribution">
          <table className="w-full text-sm tabular-nums">
            <thead>
              <tr className="text-left text-xs text-muted-foreground">
                {["Variant", "R1", "R2", "R3", "R4"].map((h) => (
                  <th key={h} className="py-1 pr-3">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {Object.entries(b.tier_distribution).map(([k, d]) => (
                <tr key={k} className="border-t border-border/50">
                  <td className="py-1 pr-3">
                    <code className="text-xs">{k}</code>
                  </td>
                  {["R1", "R2", "R3", "R4"].map((t) => (
                    <td key={t} className="pr-3">
                      {d[t] ? `${d[t].toFixed(1)}%` : "—"}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          <Muted className="mt-2">Share of weekly evaluations spent in each tier.</Muted>
        </Card>
        <Card title="Cost model">
          <Muted className="mb-2">Charges on traded notional, shown for a ₹30 lakh move (R1→R2 on a ₹1 crore book).</Muted>
          <dl className="grid grid-cols-2 gap-1 text-sm">
            {Object.entries(b.cost_breakdown).map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-muted-foreground">{k}</dt>
                <dd className="text-right font-semibold tabular-nums">{inr(v)}</dd>
              </div>
            ))}
            <dt className="text-muted-foreground">One-way total</dt>
            <dd className="text-right font-semibold tabular-nums">{inr(b.cost_one_way)}</dd>
            <dt className="text-muted-foreground">Exit + re-entry</dt>
            <dd className="text-right font-semibold tabular-nums">{inr(b.cost_round_trip)}</dd>
          </dl>
        </Card>
        <Card title="Whipsaw sensitivity">
          <Muted className="mb-2">
            A whipsaw is a ≥20-point exposure cut with ≥80% restored inside the window, while the benchmark never fell
            5%.
          </Muted>
          <dl className="grid grid-cols-2 gap-1 text-sm">
            {Object.entries(b.whipsaw_sensitivity).map(([w, v]) => (
              <div key={w} className="contents">
                <dt className="text-muted-foreground">{w}-evaluation reversal window</dt>
                <dd className="text-right font-semibold tabular-nums">{v.toFixed(2)} / yr</dd>
              </div>
            ))}
          </dl>
        </Card>
        {b.unavailable && Object.keys(b.unavailable).length ? (
          <Card title="Not available">
            {Object.entries(b.unavailable).map(([k, reason]) => (
              <div key={k} className="border-b border-dotted border-border py-1.5">
                <Badge variant="warning">{k}</Badge>
                <Muted className="mt-1">{reason}</Muted>
              </div>
            ))}
            <Muted className="mt-2">Reported as unavailable rather than approximated.</Muted>
          </Card>
        ) : null}
      </div>
    </div>
  );
}
