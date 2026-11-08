import { EMPTY_CELL, formatNumber, formatTradeDate } from "@/lib/format";
import type {
  DataStatus,
  OptionLeg,
  ScanProgress,
  SwingCounts,
  SwingOpportunity,
} from "@/lib/opportunities/types";

export function formatInr(value: number | null | undefined, decimals = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY_CELL;
  const body = formatNumber(Math.abs(value), { decimals });
  return value < 0 ? `-₹${body}` : `₹${body}`;
}

export function formatInrCrore(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY_CELL;
  return `₹${formatNumber(value, { decimals: 1 })} cr`;
}

export function formatMaxProfit(value: number | null): string {
  return value === null ? "Unlimited" : formatInr(value);
}

const IST_TIME = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
  timeZone: "Asia/Kolkata",
});

const IST_DATE_TIME = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
  timeZone: "Asia/Kolkata",
});

export function formatIstDateTime(iso: string | null | undefined): string {
  if (!iso) return EMPTY_CELL;
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return EMPTY_CELL;
  return `${IST_DATE_TIME.format(parsed)} IST`;
}

export function formatIstTime(iso: string | null | undefined): string {
  if (!iso) return EMPTY_CELL;
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return EMPTY_CELL;
  return `${IST_TIME.format(parsed)} IST`;
}

export function formatSessionDate(date: string | null | undefined): string {
  return formatTradeDate(date);
}

export function formatPct(value: number | null | undefined, decimals = 2, signed = false): string {
  return formatNumber(value, { decimals, signed, suffix: "%" });
}

export function isActionable(opportunity: Pick<SwingOpportunity, "actionable">, status: DataStatus): boolean {
  return opportunity.actionable && status === "FRESH";
}

export function actionabilityLabel(
  opportunity: Pick<SwingOpportunity, "actionable">,
  status: DataStatus,
): { actionable: boolean; label: string } {
  if (isActionable(opportunity, status)) return { actionable: true, label: "Actionable" };
  if (status !== "FRESH") return { actionable: false, label: "Not actionable — data stale/incomplete" };
  return { actionable: false, label: "Not actionable" };
}

export const SETUP_LABEL: Record<string, string> = {
  BREAKOUT: "Breakout",
  PULLBACK: "Pullback",
  RELATIVE_STRENGTH: "Relative strength",
  VCP: "VCP",
  THREE_WEEKS_TIGHT: "Three weeks tight",
  EPISODIC_PIVOT: "Episodic pivot",
  FLAG: "Flag / high tight flag",
  INSIDE_BAR_NR7: "Inside bar / NR7",
  HIGH_52W_BREAKOUT: "52-week high",
  CUP_WITH_HANDLE: "Cup with handle",
  MA_BOUNCE: "EMA bounce",
  VBT: "VBT",
};

export const STRATEGY_LABEL: Record<string, string> = {
  LONG_CALL: "Long call",
  LONG_PUT: "Long put",
  BULL_CALL_SPREAD: "Bull call spread",
  BEAR_PUT_SPREAD: "Bear put spread",
};

export function label(map: Record<string, string>, key: string): string {
  return map[key] ?? key.replaceAll("_", " ").toLowerCase();
}

export function statusLabel(status: string): string {
  return status === "TRIGGERED" ? "Triggered" : status === "PENDING" ? "Pending" : status;
}

export function legsSummary(legs: readonly OptionLeg[]): string {
  if (legs.length === 0) return EMPTY_CELL;
  return legs
    .map((leg) => `${leg.side} ${formatNumber(leg.strike)} ${leg.option_type}`)
    .join(" / ");
}

export function progressPercent(progress: ScanProgress | null): number {
  if (!progress || progress.total <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((progress.done / progress.total) * 100)));
}

export function funnelSteps(counts: SwingCounts): { label: string; value: number }[] {
  return [
    { label: "In universe", value: counts.universe },
    { label: "Evaluated", value: counts.evaluated },
    { label: "Too little history", value: counts.insufficient_history },
    { label: "Qualified", value: counts.qualified },
    { label: "Pending", value: counts.pending },
    { label: "Triggered", value: counts.triggered },
  ];
}

export function emptyReason(counts: SwingCounts): string {
  const n = (v: number) => v.toLocaleString("en-IN");
  if (counts.universe === 0) return "The universe had no names to scan.";
  if (counts.evaluated === 0) {
    return `None of the ${n(counts.universe)} names could be evaluated — ${n(counts.insufficient_history)} had too little history.`;
  }
  return `${n(counts.universe)} names in the universe, ${n(counts.evaluated)} evaluated, ${n(counts.qualified)} met every rule. Loosen a filter to see more.`;
}

export function evidenceValue(value: number | null, unit: string): string {
  if (value === null) return EMPTY_CELL;
  const body = formatNumber(value, { decimals: 2 });
  if (unit === "%") return `${body}%`;
  if (unit === "INR" || unit === "₹") return formatInr(value);
  if (unit === "cr" || unit === "₹cr") return formatInrCrore(value);
  return unit ? `${body} ${unit}` : body;
}

export function optionsHref(symbol: string, direction: "BULLISH" | "BEARISH" = "BULLISH"): string {
  const params = new URLSearchParams({ symbol, direction });
  return `/opportunities/options?${params.toString()}`;
}
