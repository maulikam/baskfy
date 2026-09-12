import { REASONS, watchOnlyReason } from "@/lib/twt/copy";
import type {
  TwtBacktest,
  TwtBacktestRun,
  TwtOpenPosition,
  TwtTightName,
  TwtToday,
} from "@/lib/twt/fetch";
import {
  asPercent,
  distanceToTriggerPct,
  figure,
  noFigure,
  percent,
  quantity,
  ratioAsUpliftPct,
  rupees,
  toCrore,
  type Figure,
} from "@/lib/twt/numbers";

/**
 * Everything `/twt` renders, derived once from the payload — TW8, `docs/twt/05` §1.
 *
 * The components are thin over this module for the reason the portfolio tree is thin over
 * `command-center.ts`: every figure below is checkable against a fixture rather than against a
 * screenshot, and **a rate changes unit here or nowhere**. A component that multiplied by a
 * hundred is how a 1.99% day came to be displayed as 0.0199% on 11 Sep 2026.
 *
 * Every value is a {@link Figure}: a formatted string, or the reason there is none. There is no
 * path through this file that yields a bare dash, and none that yields a plausible zero standing
 * in for an unknown — on a page whose subject is a resting stop, a zero distance to the trigger
 * is not a cosmetic bug, it is a number somebody may act on.
 */

export interface GateView {
  readonly state: "OPEN" | "SHUT" | "UNKNOWN";
  readonly session: string | null;
  readonly thinSession: boolean;
}

export function gateView(today: TwtToday | null): GateView {
  const gate = today?.gate;
  return {
    state: gate?.gate ?? "UNKNOWN",
    session: gate?.date ?? today?.as_of ?? null,
    thinSession: gate?.thin_session ?? false,
  };
}

/** One row of `05` §1.2's table, formatted. */
export interface TightNameView {
  readonly instrumentId: number;
  readonly symbol: string;
  readonly name: string;
  readonly close: Figure;
  readonly weekCloses: readonly Figure[];
  readonly weekRange: Figure;
  readonly aboveMonthLow: Figure;
  readonly sessionsInState: Figure;
  readonly turnoverCrore: Figure;
  /** `SIGNAL` becomes an entry badge; `SCAN_ONLY` becomes a greyed row with its reason. */
  readonly entry: "ENTRY" | "WATCH_ONLY" | "NONE";
  /** Why it is watch-only, in a reader's words. Empty for the other two states. */
  readonly watchReason: string;
  readonly lockedUpperCircuit: boolean;
}

export function tightNameView(row: TwtTightName): TightNameView {
  const uplift = ratioAsUpliftPct(row.month_low_ratio);
  return {
    instrumentId: row.instrument_id,
    symbol: row.symbol,
    name: row.name,
    close: figure(row.close_raw === null ? null : rupees(row.close_raw), REASONS.notComputed),
    weekCloses: [row.week_close_0, row.week_close_1, row.week_close_2].map((value) =>
      figure(value === null ? null : rupees(value), REASONS.notComputed),
    ),
    weekRange: figure(
      row.week_range_pct === null ? null : percent(row.week_range_pct, 2),
      REASONS.notComputed,
    ),
    aboveMonthLow: figure(uplift === null ? null : percent(uplift, 0), REASONS.notComputed),
    sessionsInState: figure(
      row.sessions_in_state === null ? null : String(row.sessions_in_state),
      REASONS.notComputed,
    ),
    turnoverCrore: (() => {
      /* `turnover_avg_20` is an integer rupee count on the wire; `toCrore` wants a decimal string. */
      const crore = toCrore(
        row.turnover_avg_20 === null ? null : String(row.turnover_avg_20),
      );
      return figure(crore === null ? null : `₹${crore} cr`, REASONS.notComputed);
    })(),
    entry:
      row.signal_state === "SIGNAL"
        ? "ENTRY"
        : row.signal_state === "SCAN_ONLY"
          ? "WATCH_ONLY"
          : "NONE",
    watchReason: row.signal_state === "SCAN_ONLY" ? watchOnlyReason(row.failed_filters) : "",
    lockedUpperCircuit: row.locked_upper_circuit,
  };
}

/** One open line of `05` §1.3, formatted, with its distance to the trigger. */
export interface PositionView {
  readonly id: number;
  readonly symbol: string;
  readonly name: string;
  readonly entryDate: string;
  readonly entryPrice: Figure;
  readonly quantity: Figure;
  readonly highSince: Figure;
  readonly highSinceDate: string | null;
  /** The trigger resting at the exchange. Absent when the line is naked, which is its own state. */
  readonly stopInForce: Figure;
  /** `01` §8's number: how far the price has to fall before the stop sells. */
  readonly distanceToTrigger: Figure;
  readonly unrealised: Figure;
  readonly unrealisedPct: Figure;
  readonly lastPrice: Figure;
  readonly hold: Figure;
  readonly halfSize: boolean;
  readonly simulated: boolean;
  /** Shares open with nothing resting at the exchange — the one state the method forbids. */
  readonly naked: boolean;
  /** `05` §1.3: "ratchet due tomorrow: ₹X → ₹Y". The web page shows it; only the desk can act. */
  readonly ratchetDue: { readonly from: string; readonly to: string } | null;
}

export function positionView(row: TwtOpenPosition): PositionView {
  const naked = row.gtt_id === null;
  const distance = distanceToTriggerPct(row.last_price, row.gtt_trigger);
  const unrealisedPct = asPercent(row.unrealised_fraction);
  /* The ratchet line is only shown when the evening computed one for a session AND it beats the
     trigger that is actually resting. `03` §5: a plan never reads a trigger computed for another
     session, and a page that showed one would be promising a move the desk will not make. */
  const ratchetDue =
    row.next_trigger !== null && row.gtt_trigger !== null && row.next_trigger_for !== null
      ? { from: rupees(row.gtt_trigger), to: rupees(row.next_trigger) }
      : null;
  return {
    id: row.id,
    symbol: row.symbol,
    name: row.name,
    entryDate: row.entry_date,
    entryPrice: figure(
      row.entry_avg === null ? null : rupees(row.entry_avg),
      REASONS.notComputed,
    ),
    quantity: figure(
      row.quantity_open === null ? null : quantity(String(row.quantity_open)),
      REASONS.notComputed,
    ),
    highSince: figure(
      row.high_since === null ? null : rupees(row.high_since),
      REASONS.notComputed,
    ),
    highSinceDate: row.high_since_date,
    stopInForce: naked
      ? noFigure(REASONS.noRestingStop)
      : figure(
          row.gtt_trigger === null ? null : rupees(row.gtt_trigger),
          REASONS.noRestingStop,
        ),
    distanceToTrigger: naked
      ? noFigure(REASONS.noRestingStop)
      : figure(distance === null ? null : percent(distance, 1), REASONS.noQuote),
    unrealised: figure(
      row.unrealised_inr === null ? null : rupees(row.unrealised_inr, 0),
      REASONS.noQuote,
    ),
    unrealisedPct: figure(
      unrealisedPct === null ? null : percent(unrealisedPct, 1, { sign: true }),
      REASONS.noQuote,
    ),
    lastPrice: figure(
      row.last_price === null ? null : rupees(row.last_price),
      REASONS.noQuote,
    ),
    hold: figure(
      row.hold_sessions === null ? null : `${row.hold_sessions}`,
      REASONS.notComputed,
    ),
    halfSize: row.half_size,
    simulated: row.simulated,
    naked,
    ratchetDue,
  };
}

/** The signed percentage behind `unrealisedPct`, so a component can colour it without re-deriving. */
export function unrealisedSignal(row: TwtOpenPosition): string | null {
  return asPercent(row.unrealised_fraction);
}

export interface TodayView {
  readonly gate: GateView;
  readonly tight: readonly TightNameView[];
  readonly positions: readonly PositionView[];
  readonly entriesToday: number;
  readonly watchOnlyToday: number;
  readonly nakedCount: number;
}

/**
 * `05` §1.2 sorts by 20-day turnover, descending — the liquidity the strategy can actually use.
 *
 * The sort is here rather than in the API for the same reason the arithmetic is: it is part of
 * what the page claims, and a claim that lives in a query string is a claim no test can reach.
 * Exact, on decimal strings, so two names a rupee apart never swap between renders.
 */
export function todayView(today: TwtToday | null): TodayView {
  const tight = [...(today?.tight ?? [])]
    .sort((left, right) => compareTurnover(right.turnover_avg_20, left.turnover_avg_20))
    .map(tightNameView);
  const positions = (today?.positions ?? []).map(positionView);
  return {
    gate: gateView(today),
    tight,
    positions,
    entriesToday: tight.filter((row) => row.entry === "ENTRY").length,
    watchOnlyToday: tight.filter((row) => row.entry === "WATCH_ONLY").length,
    nakedCount: positions.filter((row) => row.naked).length,
  };
}

function compareTurnover(left: number | null, right: number | null): number {
  if (left === null && right === null) return 0;
  /* A name whose turnover is unknown sorts last rather than first. Treating the unknown as zero
     would be the same mistake in the other direction, but at least it is the safe one: an
     unranked row at the bottom is visibly unranked. */
  if (left === null) return -1;
  if (right === null) return 1;
  /* Integer rupees from the API — compare as BigInt so two names a rupee apart never swap, and
     so a value past `Number.MAX_SAFE_INTEGER` (unlikely, but turnover is a bigint in Postgres)
     still ranks correctly. Do not call `.split`: that is a decimal-string habit and is what
     turned a 200 from `/twt/today` into "Three weeks tight could not be read". */
  const leftValue = BigInt(left);
  const rightValue = BigInt(right);
  return leftValue === rightValue ? 0 : leftValue > rightValue ? 1 : -1;
}

/** `05` §3: the latest FINISHED run per source — never the latest started. */
export function latestFinished(
  backtest: TwtBacktest | null,
  source: "PLANT" | "RESEARCH_EXPORT",
): TwtBacktestRun | null {
  const finished = (backtest?.runs ?? []).filter(
    (run) => run.source === source && run.finished_at !== null && run.stats !== null,
  );
  if (finished.length === 0) return null;
  return finished.reduce((best, run) =>
    (run.finished_at ?? "") > (best.finished_at ?? "") ? run : best,
  );
}

export interface BacktestFigure {
  readonly label: string;
  readonly figure: Figure;
}

/** `05` §3's metric list, in the order it names them. */
export function backtestFigures(run: TwtBacktestRun | null): readonly BacktestFigure[] {
  const stats = run?.stats ?? null;
  const pct = (value: string | undefined): Figure =>
    figure(value === undefined ? null : percent(value, 1), REASONS.noFinishedRun);
  const plain = (value: string | number | undefined, suffix = ""): Figure =>
    figure(value === undefined ? null : `${value}${suffix}`, REASONS.noFinishedRun);
  return [
    { label: "Annual return", figure: pct(stats?.cagr_pct) },
    { label: "Worst fall from a peak", figure: pct(stats?.max_drawdown_pct) },
    { label: "Return per unit of that fall", figure: plain(stats?.calmar) },
    { label: "Sharpe", figure: plain(stats?.sharpe) },
    { label: "Trades", figure: plain(stats?.trades) },
    { label: "Winners", figure: pct(stats?.win_rate_pct) },
    { label: "Profit factor", figure: plain(stats?.profit_factor) },
    { label: "Average hold", figure: plain(stats?.avg_hold_sessions, " sessions") },
    { label: "Time invested", figure: pct(stats?.exposure_pct) },
    { label: "First half", figure: pct(stats?.in_sample_cagr_pct) },
    { label: "Second half", figure: pct(stats?.out_of_sample_cagr_pct) },
  ];
}

/**
 * `05` §3's gate comparison — the one number that justifies the gate.
 *
 * Both sides, or neither: a comparison with one side missing is not a comparison, and rendering
 * the half we have beside an empty cell invites the reader to supply the other half themselves.
 */
export interface GateComparison {
  readonly withGate: Figure;
  readonly withoutGate: Figure;
}

export function gateComparison(run: TwtBacktestRun | null): GateComparison {
  const stats = run?.stats ?? null;
  return {
    withGate: figure(
      stats?.cagr_pct === undefined ? null : percent(stats.cagr_pct, 1),
      REASONS.noFinishedRun,
    ),
    withoutGate: figure(
      stats?.gate_off_cagr_pct === undefined ? null : percent(stats.gate_off_cagr_pct, 1),
      REASONS.noFinishedRun,
    ),
  };
}
