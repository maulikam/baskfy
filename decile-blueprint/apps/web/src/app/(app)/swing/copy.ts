import type { SwingMarketDay, SwingScanRun } from "@/lib/swing/fetch";

/**
 * The sentences the Setups and Market headers share — `docs/swing/05` §2 and `04` §8.
 *
 * One record for both pages, for the reason `journal/copy.ts` exists: a gate said two ways is
 * two gates. The two numbers below are `04` §8.5's drawdown containment (`max_drawdown_pct`
 * [15], `resume_drawdown_pct` [10]); the page shows them so a locked-out reader knows what ends
 * it, the same way the Market page names §8.3's breadth thresholds. They are display copy: the
 * decision is `sw_market_daily.drawdown_locked`, written by the evening, never taken here.
 */
export const RESUME_DRAWDOWN_PCT = 10;

export const RUNGS = 4;

const GATE_COPY: Record<string, string> = {
  GREEN: "breakouts are working — the allocation may press",
  AMBER: "mixed — entries allowed, at the size the rung permits",
  RED: "no new entries; manage what is open",
};

export function gateCopy(gate: string): string {
  return GATE_COPY[gate] ?? "read the market tab";
}

/** `04` §8.2's index rule, as a word (SW9.5): the 10-day against the 20-day, or no index. */
export function indexWord(day: Pick<SwingMarketDay, "index_ma_fast" | "index_ma_slow">): string {
  if (day.index_ma_fast === null || day.index_ma_slow === null) return "no index";
  if (day.index_ma_fast > day.index_ma_slow) return "10-day above 20-day";
  if (day.index_ma_fast < day.index_ma_slow) return "10-day below 20-day";
  return "10-day on the 20-day";
}

function pct(value: number | null): string {
  return value === null ? "—" : `${value.toFixed(1)}%`;
}

/** "5.8% of names up 25% in a month, 3.1% at a year high · 10-day above 20-day". */
export function breadthLine(
  day: Pick<SwingMarketDay, "pct_up_strong_1m" | "pct_new_52w_high" | "index_ma_fast" | "index_ma_slow">,
): string {
  return `${pct(day.pct_up_strong_1m)} of names up 25% in a month, ${pct(day.pct_new_52w_high)} at a year high · ${indexWord(day)}`;
}

/**
 * The tier, or the lock-out in its place — `05` §2: "Rung 2 of 4 · up to 6 positions · 75% of
 * sleeve"; when `drawdown_locked`, "Locked out · sleeve 15.3% below its peak · resumes inside
 * 10%". The pages say "allocation" for the money (DECISIONS-SW SW4.2).
 */
export function tierLine(
  day: Pick<
    SwingMarketDay,
    "exposure_level" | "max_open_positions" | "max_exposure_pct" | "drawdown_pct" | "drawdown_locked"
  >,
): string {
  if (day.drawdown_locked) {
    return `Locked out · allocation ${day.drawdown_pct.toFixed(1)}% below its peak · resumes inside ${RESUME_DRAWDOWN_PCT}%`;
  }
  return `Rung ${day.exposure_level + 1} of ${RUNGS} · up to ${day.max_open_positions} positions · ${day.max_exposure_pct.toFixed(0)}% of the allocation`;
}

/** "13:42 IST" — the wall clock a person compares a scan against; never the browser's zone. */
export function timeIST(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return null;
  const rendered = new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Kolkata",
  }).format(parsed);
  return `${rendered} IST`;
}

/**
 * SW15's header line. Provisional rows say so first, and say from what: "provisional — scanned
 * 13:42 IST from live quotes". A day re-scanned on demand outside the session says
 * "re-scanned 18:02 IST from published bars"; a day the nightly wrote says nothing.
 */
export function scanLine(provisional: boolean, scannedAt: string | null | undefined): string {
  const when = timeIST(scannedAt);
  if (provisional) {
    return when ? `provisional — scanned ${when} from live quotes` : "provisional — from live quotes";
  }
  return when ? `re-scanned ${when} from published bars` : "";
}

/** The last run beside the button: "scanning…", "scanned 13:42 IST · 41 liquid, 2 flags", "failed: …". */
export function scanRunLine(run: SwingScanRun | null | undefined): string {
  if (!run) return "";
  if (run.status === "QUEUED") return "Scan queued…";
  if (run.status === "RUNNING") return "Scanning…";
  if (run.status === "FAILED") {
    return `The last scan failed${run.error ? `: ${run.error}` : "."}`;
  }
  const when = timeIST(run.finished_at) ?? timeIST(run.requested_at);
  const liquid = run.funnel?.liquid;
  const candidates = run.funnel?.candidates ?? {};
  const found = Object.values(candidates).reduce((sum, count) => sum + count, 0);
  const counts =
    liquid === undefined ? "" : ` · ${liquid.toLocaleString("en-IN")} liquid, ${found} flagged`;
  const what = run.provisional ? " from live quotes" : " from published bars";
  return `Last scan ${when ?? "done"}${what}${counts}`;
}
