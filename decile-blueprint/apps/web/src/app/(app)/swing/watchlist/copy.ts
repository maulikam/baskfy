import type { SwingSignal, SwingWatchRow } from "@/lib/swing/fetch";

/**
 * The watchlist's derived cells — SW14, `docs/swing/05` §2. Pure, and separately tested: the
 * "will skip" mark and the signal sentence are the two things on the page a person acts on at
 * 21:30, and both are arithmetic or wording over stored numbers rather than new facts.
 */

function money(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toFixed(2);
}

/**
 * `05` §2: "stop distance as a share of the ADR (a name whose stop is wider than one ADR is
 * shown as one the plan will skip — `04` §6.1)". The share is the two stored numbers divided,
 * so the page cannot disagree with the plan about which side of one it falls.
 */
export function stopShareOfAdr(row: SwingWatchRow): number | null {
  if (row.trigger === null || row.stop_ref === null || row.trigger <= 0) return null;
  const adr = row.adr_pct ?? null;
  if (adr === null || adr <= 0) return null;
  const stopPct = ((row.trigger - row.stop_ref) / row.trigger) * 100;
  return stopPct / adr;
}

const IST_TIME = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZone: "Asia/Kolkata",
});

function clock(iso: string): string {
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? "—" : IST_TIME.format(parsed);
}

function range(signal: SwingSignal): string {
  const window = signal.or_window_minutes === null ? "opening" : `${signal.or_window_minutes}-min`;
  if (signal.range_low === null || signal.range_high === null) return `${window} range not set`;
  return `${window} range ${money(signal.range_low)}–${money(signal.range_high)}`;
}

/** "fired 09:23, 5-min range 412.30–418.90" — and the other verdicts, each as what it was. */
export function signalSentence(signal: SwingSignal): string {
  const at = clock(signal.raised_at);
  switch (signal.state) {
    case "TRIGGERED": {
      const levels =
        signal.entry !== null && signal.stop !== null
          ? ` · entry ${money(signal.entry)}, stop ${money(signal.stop)}`
          : "";
      const line = signal.plan_line_id !== null ? " · a plan line was made" : "";
      return `fired ${at}, ${range(signal)}${levels}${line}`;
    }
    case "BELOW_PIVOT":
      return `${at}: broke the range but not the pivot, ${range(signal)}`;
    case "LOCKED_UPPER_CIRCUIT":
      return `${at}: locked at its upper circuit — no seller, no fill`;
    case "WAITING":
      return `${at}: waiting, ${range(signal)}`;
    case "RANGE_INCOMPLETE":
      return `${at}: the opening range was not complete`;
    case "SESSION_OVER":
      return `${at}: the window closed without a trigger`;
    default:
      return `${at}: ${signal.state.replaceAll("_", " ").toLowerCase()}`;
  }
}

