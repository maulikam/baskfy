/**
 * Labels and formatting for the FO pages (FO5, `docs/fno/05` §2-§3). Pure: no fetch, no clock.
 *
 * The clock labels are exactly `05`'s: `As of close, Tue 22 Sep` for the scan, the proposed condor
 * and every column of the information table; `Marked at settle, 22 Sep` for the open structures;
 * `NIFTY live 13:14` for the underlying's level when the shared overlay is live.
 */

import { level, sessionDay, type Tone } from "@/lib/options/view";

import type { FnoEmptyReason } from "./types";

const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
] as const;

/** `As of close, Tue 22 Sep` — `05` §2 and §3. */
export function asOfClose(isoDate: string | null): string {
  return isoDate ? `As of close, ${sessionDay(isoDate)}` : "No close read yet";
}

/** `22 Sep` for a session date. */
export function dayMonth(isoDate: string | null): string {
  if (!isoDate) return "not yet";
  const parsed = new Date(`${isoDate}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return "not yet";
  return `${parsed.getUTCDate()} ${MONTHS[parsed.getUTCMonth()]}`;
}

/** `Marked at settle, 22 Sep` — `05` §2's open-structure clock. */
export function markedAtSettle(isoDate: string | null): string {
  return isoDate ? `Marked at settle, ${dayMonth(isoDate)}` : "Not marked yet";
}

/** `NIFTY live 13:14` — the only live number on the overnight page. */
export function liveLabel(underlying: string, hhmm: string): string {
  return `${underlying} live ${hhmm}`;
}

const STATE_TEXT: Record<string, { label: string; tone: Tone }> = {
  NOT_ENTRY_DAY: { label: "Not an entry day", tone: "neutral" },
  CANDIDATE: { label: "Candidate", tone: "accent" },
  SKIPPED_EVENT: { label: "Skipped: event", tone: "neutral" },
  PAUSED: { label: "Paused", tone: "warning" },
  OPEN_POSITION: { label: "Open position", tone: "neutral" },
  NO_DATA: { label: "No data", tone: "warning" },
  NO_SIGNAL: { label: "No signal", tone: "neutral" },
  BLOCKED_BAN: { label: "Blocked: ban list", tone: "neutral" },
  BLOCKED_REGIME: {
    label: "Blocked: NIFTY below its average",
    tone: "neutral",
  },
  BLOCKED_CAPACITY: { label: "Blocked: capacity", tone: "neutral" },
  REJECTED_SIZE: { label: "Rejected: size", tone: "neutral" },
  REJECTED_STRUCTURE: { label: "Rejected: structure", tone: "neutral" },
  REJECTED_LIQUIDITY: { label: "Rejected: liquidity", tone: "neutral" },
  REJECTED_COST: { label: "Rejected: cost", tone: "neutral" },
};

/** `04` §8's state, in words. Unknown states are shown as written, never hidden. */
export function stateText(state: string): { label: string; tone: Tone } {
  return STATE_TEXT[state] ?? { label: state, tone: "neutral" };
}

export function emptyReasonText(reason: FnoEmptyReason | null): string | null {
  switch (reason) {
    case "scan_off":
      return (
        "The overnight scan is switched off on this server, so no underlying has been scanned. " +
        "Nothing below is a statement about the market — the scan has not looked."
      );
    case "never_scanned":
      return (
        "The overnight scan is on but has not written a night yet. It runs after each " +
        "evening's F&O bhavcopy is ingested."
      );
    default:
      return null;
  }
}

function str(value: unknown): string | null {
  if (typeof value === "string" && value.trim() !== "") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

/** A value from a scan's `detail`, as the worker wrote it. */
export function detailStr(
  detail: Record<string, unknown>,
  key: string,
): string | null {
  return str(detail[key]);
}

/** A decimal fraction as a percent: `"0.101000"` → `10.1%`. */
export function volPct(value: unknown): string {
  const raw = str(value);
  if (raw === null) return "—";
  const n = Number(raw) * 100;
  return Number.isFinite(n) ? `${n.toFixed(1)}%` : "—";
}

/** A percent already in percent: `"3.25"` → `3.25%`. */
export function percent(value: unknown, decimals = 2): string {
  const raw = str(value);
  if (raw === null) return "—";
  const n = Number(raw);
  return Number.isFinite(n) ? `${n.toFixed(decimals)}%` : "—";
}

/** A plain ratio or an R figure, signed: `"0.060"` → `+0.06`. */
export function signed(value: unknown, decimals = 2): string {
  const raw = str(value);
  if (raw === null) return "—";
  const n = Number(raw);
  if (!Number.isFinite(n)) return "—";
  return `${n > 0 ? "+" : ""}${n.toFixed(decimals)}`;
}

export function ratio(value: unknown): string {
  const raw = str(value);
  if (raw === null) return "—";
  const n = Number(raw);
  return Number.isFinite(n) ? n.toFixed(2) : "—";
}

export function price(value: unknown): string {
  return str(value) === null ? "—" : level(value);
}

/** ₹ from a decimal string, grouped the Indian way. */
export function rupees(value: unknown): string {
  return str(value) === null ? "—" : `₹${level(value)}`;
}

/** Sleeve codes as a reader says them. */
export const SLEEVE_NAME: Record<string, string> = {
  F1N: "NIFTY condor",
  F1B: "BANKNIFTY condor",
  F2: "Stock-futures breakout",
};
