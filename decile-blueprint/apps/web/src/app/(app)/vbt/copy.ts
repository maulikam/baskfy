import type { VbtToday } from "@/lib/vbt/fetch";

/**
 * The sentences `/vbt` says about a gate, a funnel and an empty session — in one file, because
 * three pages say versions of the same thing and a copy that drifts between them is a page that
 * contradicts itself.
 *
 * Every number here comes from the payload. Nothing is hard-coded: `04` §4.2's forty is served
 * as `gate_threshold_pct` precisely so a page cannot disagree with the detector about what the
 * gate is.
 */

/** `05` §2: "62.4% of 1,412 names are above their 200-day average · the gate opens above 40%". */
export function breadthLine(today: VbtToday): string {
  const pct = today.pct_above_dma;
  const measured = today.measured_count;
  if (pct === null || measured === null)
    return "the breadth of the traded universe is unknown";
  return (
    `${pct.toFixed(1)}% of ${measured.toLocaleString("en-IN")} names are above their ` +
    `200-day average · the gate opens above ${today.gate_threshold_pct.toFixed(0)}%`
  );
}

/**
 * What the book does under each gate. The SHUT sentence matters more than the OPEN one: a person
 * reading "SHUT" needs to know it does not mean "sell everything" (`04` §4.3).
 */
export function gateCopy(gate: string): string {
  if (gate === "OPEN") return "new limits may be placed";
  return "no new limits are placed. Positions are managed as usual — stops stay, exits still fire";
}

/**
 * `05` §2's funnel line, always rendered, even at zero:
 * "4,186 names → 1,412 with a bar and a 200-day average → 37 met the volume scan → 4 are signals".
 *
 * Without it an empty list and a detector that never ran render identically, and those two need
 * opposite responses from whoever is reading.
 */
export function funnelLine(today: VbtToday): string {
  const f = today.funnel?.funnel;
  if (!f) {
    return "No scan has run for this session, so the empty list below is not a statement about the market.";
  }
  const n = (value: number | undefined) => (value ?? 0).toLocaleString("en-IN");
  return (
    `${n(f.universe)} names → ${n(f.with_dma ?? f.with_bar)} with a bar and a 200-day average → ` +
    `${n(f.scan_hits)} met the volume scan → ${n(f.signals)} ${
      (f.signals ?? 0) === 1 ? "is a signal" : "are signals"
    }`
  );
}

/** How often the gate has been shut lately — the context a single day's reading does not carry. */
export function shutLine(today: VbtToday): string {
  const shut = today.shut_sessions_recent;
  if (shut === 0)
    return `The gate has been open every one of the last ${today.shut_window} sessions.`;
  return `The gate has been shut on ${shut} of the last ${today.shut_window} sessions.`;
}

/**
 * `04` §7.2's window, as a person reads it on a working order. The third session's close cancels
 * it, so "3 of 3" and "cancels tonight" are the same fact said twice — deliberately, because the
 * first is a number in a column and the second is the thing to act on.
 */
export function sessionsLine(
  worked: number,
  allowed: number,
  expiring: boolean,
): string {
  return expiring
    ? `${worked} of ${allowed} · cancels tonight`
    : `${worked} of ${allowed}`;
}
