import { InstrumentLink } from "@/components/instrument/instrument-link";
import { FlagTip } from "@/components/mb/flag-tip";
import { SWING_SCOPE } from "@/lib/mb/scores";
import {
  EMPTY_CELL,
  formatFraction,
  formatNumber,
  formatTradeDate,
} from "@/lib/format";
import type {
  MbSwing,
  MbSwingBacktest,
  MbSwingName,
  MbSwingPick,
} from "@/lib/mb/fetch";

/** PM11's rules, as the scorer runs them (rr_task.py). */
export const SWING_RULES = {
  stop: 0.07,
  trail: 0.12,
  horizon: 60,
  topN: 5,
  cost: 0.005,
} as const;

const TH =
  "border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground";
const TD = "py-1.5 pr-4";

function rupees(v: string | number | null | undefined): string {
  if (v === null || v === undefined) return EMPTY_CELL;
  return formatNumber(v, { decimals: 2, suffix: "" });
}

function tone(v: string | number | null | undefined): string {
  if (v === null || v === undefined) return "";
  const n = Number(v);
  return n > 0
    ? "text-emerald-600 dark:text-emerald-400"
    : n < 0
      ? "text-red-600 dark:text-red-400"
      : "";
}

function Head({ cols }: { cols: readonly string[] }) {
  return (
    <thead>
      <tr className={TH}>
        {cols.map((h) => (
          <th key={h} scope="col" className="py-1.5 pr-4 font-medium">
            {h}
          </th>
        ))}
      </tr>
    </thead>
  );
}

function Section({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section aria-labelledby={id} className="space-y-3">
      <h2
        id={id}
        className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
      >
        {title}
      </h2>
      {children}
    </section>
  );
}

function SwitchCard({ data }: { data: MbSwing }) {
  const s = data.switch;
  if (s === null)
    return (
      <p className="text-sm text-muted-foreground" data-testid="swing-switch-none">
        The market switch has not been computed yet; it appears after the
        first nightly run.
      </p>
    );
  const on = s.is_on;
  const history = data.switch_history;
  const onShare =
    history.length === 0
      ? null
      : history.filter(([, v]) => v).length / history.length;
  return (
    <div
      className={`space-y-3 rounded-md border p-4 ${
        on
          ? "border-emerald-500/50 bg-emerald-500/10"
          : "border-slate-400/50 bg-slate-500/10"
      }`}
      data-testid="swing-switch"
      data-on={on ? "true" : "false"}
    >
      <p className="text-base font-semibold">
        <span
          aria-hidden
          className={`mr-2 inline-block h-3 w-3 rounded-full ${on ? "bg-emerald-500" : "bg-slate-400"}`}
        />
        {on
          ? "Switch ON: new picks are allowed"
          : "Switch OFF: no new picks, sit in cash"}
        <span className="ml-2 text-sm font-normal text-muted-foreground">
          at the close of {formatTradeDate(s.date)}
        </span>
      </p>
      <ul className="grid gap-1 text-sm tabular-nums sm:grid-cols-2">
        <li>
          <span className={s.above_200 ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}>
            {s.above_200 ? "✓" : "✗"}
          </span>{" "}
          NIFTY500 Equal Weight {rupees(s.ew_close)}{" "}
          {s.above_200 ? "above" : "below"} its 200-day average {rupees(s.ma200)}
        </li>
        <li>
          <span className={s.up_10_20 ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}>
            {s.up_10_20 ? "✓" : "✗"}
          </span>{" "}
          10-day average {rupees(s.ma10)} {s.up_10_20 ? "above" : "below"}{" "}
          the 20-day {rupees(s.ma20)}
        </li>
      </ul>
      {history.length > 0 ? (
        <div className="space-y-1">
          <div
            className="flex h-4 w-full overflow-hidden rounded-sm"
            role="img"
            aria-label={`Switch over the last ${history.length} sessions: on ${formatFraction(onShare, 0)} of them`}
            data-testid="swing-switch-strip"
          >
            {history.map(([d, v]) => (
              <span
                key={d}
                title={`${formatTradeDate(d)}: ${v ? "on" : "off"}`}
                className={`h-full flex-1 ${v ? "bg-emerald-500" : "bg-slate-300 dark:bg-slate-600"}`}
              />
            ))}
          </div>
          <p className="text-xs text-muted-foreground">
            Last {history.length} sessions, oldest on the left: on{" "}
            {formatFraction(onShare, 0)} of them.
          </p>
        </div>
      ) : null}
    </div>
  );
}

function HowTo() {
  const { stop, trail, horizon, topN } = SWING_RULES;
  return (
    <div
      className="max-w-[90ch] space-y-2 rounded-md border border-border p-4 text-sm"
      data-testid="swing-howto"
    >
      <p className="font-medium">How to use it</p>
      <ol className="list-decimal space-y-1 pl-5">
        <li>
          Each evening after the close the model scores every liquid stock on
          how likely it is to make at least +{formatFraction(stop * 2, 0)}{" "}
          under the exit rule below.
        </li>
        <li>
          Only while the switch above is <strong>ON</strong>, the top {topN}{" "}
          investable names you don&rsquo;t already hold become picks, to buy at
          the next session&rsquo;s open.
        </li>
        <li>
          Exit: a stop {formatFraction(stop, 0)} under the entry; once the price
          rises, the stop trails {formatFraction(trail, 0)} under the highest
          high since entry (it never moves down). Sell at the stop, or after{" "}
          {horizon} sessions, whichever comes first.
        </li>
        <li>
          Size every pick the same, and small. In the backtest about two
          trades in three lost; the winners paid for them.
        </li>
        <li>
          When the switch turns OFF, open picks keep running to their own exit;
          only new picks stop.
        </li>
      </ol>
      <p className="text-xs text-muted-foreground">
        Paper only. Nothing on this page places an order. Prices are the last
        close, in today&rsquo;s split-adjusted terms.
      </p>
    </div>
  );
}

function TodayPicks({ data }: { data: MbSwing }) {
  const today = data.picks.filter((p) => p.signal_date === data.as_of);
  if (today.length === 0)
    return (
      <p className="text-sm text-muted-foreground" data-testid="swing-today-none">
        {data.switch?.is_on === false
          ? "No new picks tonight: the switch is off."
          : "No new picks for this session."}
      </p>
    );
  const { stop } = SWING_RULES;
  return (
    <div className="overflow-x-auto">
      <table className="min-w-[44rem] border-collapse text-sm" data-testid="swing-today">
        <Head cols={["#", "Stock", "Score", "Close", "Buy", `Stop (−${formatFraction(stop, 0)}), approx.`]} />
        <tbody>
          {today.map((p) => {
            const close = data.names.find((n) => n.instrument_id === p.instrument_id)?.close ?? null;
            return (
              <tr key={p.instrument_id} className="border-b border-border/40 tabular-nums">
                <td className={TD}>{p.pick_rank}</td>
                <td className={TD}>
                  <InstrumentLink symbol={p.symbol} flag={false} />
                  {p.name ? <span className="ml-2 text-xs text-muted-foreground">{p.name}</span> : null}
                </td>
                <td className={`${TD} font-medium text-emerald-700 dark:text-emerald-400`}>{Number(p.score).toFixed(3)}</td>
                <td className={TD}>{rupees(close)}</td>
                <td className={TD}>next open</td>
                <td className={TD}>{close === null ? EMPTY_CELL : rupees(Number(close) * (1 - stop))}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="mt-1 text-xs text-muted-foreground">
        The stop is set from the actual entry price; the figure here uses
        tonight&rsquo;s close as a guide.
      </p>
    </div>
  );
}

function OpenPicks({ picks }: { picks: readonly MbSwingPick[] }) {
  const open = picks.filter((p) => p.status !== "closed" && p.signal_date !== null);
  if (open.length === 0)
    return <p className="text-sm text-muted-foreground">No open picks.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="min-w-[52rem] border-collapse text-sm" data-testid="swing-open">
        <Head cols={["Stock", "Picked", "Entry", "Last", "Return", "Stop now", "Held"]} />
        <tbody>
          {open.map((p) => (
            <tr key={`${p.signal_date}-${p.instrument_id}`} className="border-b border-border/40 tabular-nums">
              <td className={TD}>
                <InstrumentLink symbol={p.symbol} flag={false} />
              </td>
              <td className={TD}>{formatTradeDate(p.signal_date)}</td>
              <td className={TD}>
                {p.status === "waiting" ? "next open" : `${rupees(p.entry_px)} · ${formatTradeDate(p.entry_date)}`}
              </td>
              <td className={TD}>{rupees(p.last_px)}</td>
              <td className={`${TD} font-medium ${tone(p.return_fraction)}`}>
                {formatFraction(p.return_fraction, 1)}
              </td>
              <td className={TD}>{rupees(p.stop_px)}</td>
              <td className={TD}>
                {p.sessions_held ?? 0} / {SWING_RULES.horizon}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const REASON: Record<string, string> = {
  stop: "initial stop",
  trail: "trailing stop",
  time: `${SWING_RULES.horizon} sessions`,
};

function ClosedPicks({ picks }: { picks: readonly MbSwingPick[] }) {
  const closed = picks.filter((p) => p.status === "closed");
  if (closed.length === 0)
    return (
      <p className="text-sm text-muted-foreground" data-testid="swing-closed-none">
        No pick has closed yet. The live record starts with the first night the
        switch is on.
      </p>
    );
  const net = closed.map((p) => Number(p.return_fraction ?? 0) - SWING_RULES.cost);
  const wins = net.filter((r) => r > 0).length;
  const mean = net.reduce((a, b) => a + b, 0) / net.length;
  return (
    <div className="space-y-2">
      <p className="text-sm tabular-nums" data-testid="swing-live-stats">
        {closed.length} closed · won {formatFraction(wins / closed.length, 0)} ·
        average{" "}
        <span className={tone(mean)}>{formatFraction(mean, 2)}</span> a trade
        after {formatFraction(SWING_RULES.cost, 1)} costs
      </p>
      <div className="overflow-x-auto">
        <table className="min-w-[52rem] border-collapse text-sm" data-testid="swing-closed">
          <Head cols={["Stock", "Entry", "Exit", "Return", "Why", "Held"]} />
          <tbody>
            {closed.map((p) => (
              <tr key={`${p.signal_date}-${p.instrument_id}`} className="border-b border-border/40 tabular-nums">
                <td className={TD}>
                  <InstrumentLink symbol={p.symbol} flag={false} />
                </td>
                <td className={TD}>
                  {rupees(p.entry_px)} · {formatTradeDate(p.entry_date)}
                </td>
                <td className={TD}>
                  {rupees(p.exit_px)} · {formatTradeDate(p.exit_date)}
                </td>
                <td className={`${TD} font-medium ${tone(p.return_fraction)}`}>
                  {formatFraction(p.return_fraction, 1)}
                </td>
                <td className={TD}>{p.exit_reason ? REASON[p.exit_reason] : EMPTY_CELL}</td>
                <td className={TD}>{p.sessions_held ?? EMPTY_CELL}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Backtest({ rows }: { rows: readonly MbSwingBacktest[] }) {
  const all = rows.find((r) => r.period === "all");
  const years = rows.filter((r) => r.period !== "all");
  if (!all) return <p className="text-sm text-muted-foreground">Not seeded yet.</p>;
  const positive = years.filter((y) => Number(y.net_fraction) > 0).length;
  return (
    <div className="space-y-3">
      <ul className="grid gap-2 text-sm tabular-nums sm:grid-cols-3 lg:grid-cols-6" data-testid="swing-bt-summary">
        {[
          ["Trades", formatNumber(all.trades)],
          ["Average a trade", formatFraction(all.net_fraction, 2)],
          ["Won", formatFraction(all.win_rate_fraction, 0)],
          ["Gains ÷ losses", all.profit_factor === null ? EMPTY_CELL : Number(all.profit_factor).toFixed(2)],
          ["Worst trade", formatFraction(all.worst_fraction, 0)],
          ["Profitable years", `${positive} of ${years.length}`],
        ].map(([k, v]) => (
          <li key={k} className="rounded-md border border-border p-2">
            <div className="text-xs text-muted-foreground">{k}</div>
            <div className="font-medium">{v}</div>
          </li>
        ))}
      </ul>
      <div className="overflow-x-auto">
        <table className="min-w-[32rem] border-collapse text-sm" data-testid="swing-bt-years">
          <Head cols={["Year", "Trades", "Average a trade", "Switch on"]} />
          <tbody>
            {years.map((y) => (
              <tr key={y.period} className="border-b border-border/40 tabular-nums">
                <td className={TD}>{y.period}</td>
                <td className={TD}>{formatNumber(y.trades)}</td>
                <td className={`${TD} font-medium ${tone(y.net_fraction)}`}>{formatFraction(y.net_fraction, 2)}</td>
                <td className={TD}>{formatFraction(y.on_share_fraction, 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="max-w-[90ch] text-xs text-muted-foreground">
        Walk-forward 2016–2026: each year scored by a model trained only on the
        years before it, top {SWING_RULES.topN} a day, after{" "}
        {formatFraction(SWING_RULES.cost, 1)} costs. It did not pass the bar
        set before the test (8 profitable years of 11): it makes its money in
        strong years and loses in 2018, 2019, 2022 and 2024. Kept by
        Maulik&rsquo;s decision on 11 Nov 2026, to be watched on paper.
      </p>
    </div>
  );
}

function Ranking({ names }: { names: readonly MbSwingName[] }) {
  if (names.length === 0) return <p className="text-sm text-muted-foreground">Nothing scored yet.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="min-w-[40rem] border-collapse text-sm" data-testid="swing-ranking">
        <Head cols={["Rank", "Stock", "Score", "Close", "Sector", "Can be picked"]} />
        <tbody>
          {names.map((n) => (
            <tr key={n.instrument_id} className="border-b border-border/40 tabular-nums">
              <td className={TD}>{n.rank}</td>
              <td className={TD}>
                <InstrumentLink symbol={n.symbol} flag={false} />
              </td>
              <td className={`${TD} font-medium`}>{Number(n.score).toFixed(3)}</td>
              <td className={TD}>{rupees(n.close)}</td>
              <td className={`${TD} text-muted-foreground`}>{n.sector ?? EMPTY_CELL}</td>
              <td className={TD}>
                {n.scope !== "universe" ? (
                  <FlagTip label={SWING_SCOPE[n.scope].label} meaning={SWING_SCOPE[n.scope].meaning} className="text-amber-600 dark:text-amber-400">◌</FlagTip>
                ) : n.investable ? (
                  <FlagTip label="Investable" meaning="Close at least ₹50 and at least ₹5 cr traded a day: can become a pick." className="text-emerald-600 dark:text-emerald-400">●</FlagTip>
                ) : (
                  <FlagTip label="Not investable" meaning="Close under ₹50 or under ₹5 cr traded a day: scored, but never picked." className="text-red-600 dark:text-red-400">✗</FlagTip>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** `/mb/swing` (PM11). Paper only: no line here was ever an order. */
export function MbSwingPanel({ data }: { data: MbSwing | null }) {
  if (data === null)
    return (
      <p className="text-sm text-muted-foreground" data-testid="swing-unavailable">
        The swing-picks read is unavailable right now.
      </p>
    );
  return (
    <div className="space-y-8">
      <SwitchCard data={data} />
      <HowTo />
      <Section id="swing-today-h" title={`Tonight's picks · ${formatTradeDate(data.as_of)}`}>
        <TodayPicks data={data} />
      </Section>
      <Section id="swing-open-h" title="Open picks (paper)">
        <OpenPicks picks={data.picks} />
      </Section>
      <Section id="swing-closed-h" title="Closed picks (paper)">
        <ClosedPicks picks={data.picks} />
      </Section>
      <section
        aria-labelledby="swing-bt-h"
        className="space-y-3 rounded-md border border-dashed border-border p-4"
      >
        <h2 id="swing-bt-h" className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
          Backtest, not live
        </h2>
        <Backtest rows={data.backtest} />
      </section>
      <Section
        id="swing-rank-h"
        title={`The model's top ${data.names.length} · ${data.investable_n} can be picked of ${data.universe_n} scored`}
      >
        <Ranking names={data.names} />
      </Section>
      {data.model ? (
        <p className="text-xs text-muted-foreground" data-testid="swing-model">
          Model {data.model.id}, trained on {formatNumber(data.model.rows)} stock-days
          ({formatNumber(data.model.positives)} made +14%) with labels through{" "}
          {formatTradeDate(data.model.trained_on)}. Retrained each quarter.
        </p>
      ) : null}
    </div>
  );
}
