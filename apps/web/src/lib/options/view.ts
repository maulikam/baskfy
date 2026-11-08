/**
 * The options tab's words and numbers — the one place that knows each sleeve's scan keys (OP5).
 *
 * Pure: no fetch, no clock of its own (the caller passes `now` where one is needed), so every
 * sentence the page can show is testable from a fixture.
 *
 * THE RULES THIS FILE KEEPS
 * -------------------------
 * * **No internal name reaches the screen.** A state or reason code (`WOULD_SKIP`,
 *   `RANGE_TOO_WIDE`) is translated, and a code this file has not met is sentence-cased rather
 *   than shown raw — `no-internals.test.tsx` bans SCREAMING_CASE and snake_case in rendered text.
 * * **Money stays a string.** Figures are formatted from the decimal strings the worker wrote;
 *   `Number()` is used only to *compare* a percentage against its threshold for a colour.
 * * **The clock is stated** (`docs/options/05` §2): `Live · 13:14` while the session is open and
 *   the rows are today's (amber `stale` beyond two minutes), `As of close, Mon 21 Sep · market
 *   closed` otherwise.
 */

import type {
  Candidate,
  CandidateLeg,
  EmptyReason,
  OptionsRole,
  OptionsScan,
  OptionsToday,
  SleeveCode,
  SleeveGroupCode,
} from "./types";

export const SLEEVE_SHORT: Record<SleeveCode, string> = {
  O1M: "O1-M",
  O1W: "O1-W",
  O2: "O2",
  O3A: "O3-A",
  O3B: "O3-B",
};

export const SLEEVE_NAME: Record<SleeveCode, string> = {
  O1M: "Premium selling · monthly",
  O1W: "Premium selling · weekly",
  O2: "Directional",
  O3A: "Expiry-day range break",
  O3B: "Expiry-day gap hold",
};

export const GROUP_NAME: Record<SleeveGroupCode, string> = {
  O1M: "Premium selling · monthly (O1-M)",
  O1W: "Premium selling · weekly (O1-W)",
  O2: "Directional (O2)",
  O3: "Expiry-day setups (O3)",
};

/** `02` §3.2's paper period, in its own words, for the progress bars. */
export const PAPER_UNIT: Record<SleeveGroupCode, string> = {
  O1M: "monthly expiries",
  O1W: "weekly expiries",
  O2: "sessions",
  O3: "expiry days",
};

export type Tone = "positive" | "negative" | "warning" | "accent" | "neutral";

const STATE_TEXT: Record<string, { label: string; tone: Tone }> = {
  NOT_TODAY: { label: "Not today", tone: "neutral" },
  OBSERVING: { label: "Watching the open", tone: "neutral" },
  BUILDING_RANGE: { label: "Building the range", tone: "neutral" },
  WOULD_TRADE: { label: "Would trade", tone: "positive" },
  WOULD_SKIP: { label: "Would skip", tone: "warning" },
  DAY_SKIPPED: { label: "Day skipped", tone: "warning" },
  SLOT_TAKEN: { label: "Slot taken", tone: "neutral" },
  ARMED: { label: "Armed", tone: "accent" },
  TRIGGERED: { label: "Triggered", tone: "positive" },
  PLANNED: { label: "Planned on the desk", tone: "accent" },
  DONE: { label: "Done for the day", tone: "neutral" },
  WINDOW_CLOSED: { label: "Window closed", tone: "neutral" },
  PAUSED: { label: "Paused", tone: "negative" },
};

/** `04`'s reason and rejection codes, as a reader should meet them. */
const REASON_TEXT: Record<string, string> = {
  NOT_TRADING_DAY: "Not a trading day",
  EVENT_DAY: "An event day — no strategy trades it",
  NOT_EXPIRY: "Not an expiry day",
  NOT_MONTHLY: "Not the monthly expiry",
  MONTHLY_EXPIRY: "The monthly expiry belongs to the monthly strategy",
  SETUP_DISABLED: "This setup is switched off",
  GAP_TOO_BIG: "The opening gap is too big",
  GAP_TOO_SMALL: "The opening gap is too small",
  RANGE_TOO_WIDE: "The morning range is too wide",
  NOT_CONTAINED: "The price left the opening range",
  ER_TOO_HIGH: "The morning is trending, not ranging",
  HOLD_BROKEN: "The price traded through half the gap",
  TREND_FLAT: "No trend to trade with",
  TREND_UNKNOWN: "The trend is not known yet",
  VIX_TOO_HIGH: "India VIX is too high",
  VIX_UNKNOWN: "India VIX is not known yet",
  INCOMPLETE_OBSERVATION: "Too few minute bars to judge the morning",
  NO_PREV_CLOSE: "No previous close to measure the gap from",
  O3B_HOLDS: "The gap-hold setup holds today's slot",
  REJECTED_NO_SHORT_CALL:
    "No call strike fits the delta band outside the range",
  REJECTED_NO_SHORT_PUT: "No put strike fits the delta band outside the range",
  REJECTED_NO_WING: "No protective wing strike is quoted",
  REJECTED_CREDIT: "The credit is below the floor",
  REJECTED_DELTA: "No strike fits the delta band",
  REJECTED_ILLIQUID: "The quotes are too thin to fill",
  REJECTED_DEBIT: "The debit is above the cap",
  REJECTED_COST: "Costs would take too much of the expected gain",
  REJECTED_PREMIUM_CAP: "The premium is above the cap",
  REJECTED_NO_LOT_SIZE: "The lot size is not in the contract list",
  REJECTED_NO_SLEEVE_CAPITAL: "No capital is set for this strategy",
  REJECTED_BUDGET: "The risk budget cannot cover one lot",
  REJECTED_SLOT_TAKEN: "Another strategy holds today's expiry slot",
  REJECTED_PAUSED: "This strategy is paused",
  REJECTED_NO_CONTRACT: "No contract to trade in the list",
  REJECTED_NO_CHAIN: "No option chain was collected for this minute",
  RESERVE_EXCEEDED: "The risk budget cannot cover one lot",
  HARD_EXIT: "Flat at the hard exit time",
  STRIKE_TOUCH: "A short strike was touched",
  STOP: "Stopped out",
  PROFIT: "Profit taken",
  TARGET: "Target reached",
  TIME_STOP: "Time stop",
  INVALIDATED: "The setup was invalidated",
  MANUAL: "Closed by hand",
};

function sentenceCase(code: string): string {
  const words = code.toLowerCase().replace(/[_]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "Unspecified";
}

export function stateText(state: string): { label: string; tone: Tone } {
  return STATE_TEXT[state] ?? { label: sentenceCase(state), tone: "neutral" };
}

export function reasonText(code: string): string {
  return REASON_TEXT[code] ?? sentenceCase(code);
}

// --- the clock ----------------------------------------------------------------------------------

const IST_TIME = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZone: "Asia/Kolkata",
});

const IST_TIME_SECONDS = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
  timeZone: "Asia/Kolkata",
});

/* Spelled out rather than `Intl`'s en-GB, whose short September is "Sept" — `05` §2 writes
   "Mon 21 Sep", and a label that changes with the ICU build is not a label a test can hold. */
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"] as const;
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

export function istTime(iso: string | null, { seconds = false } = {}): string {
  if (!iso) return "not yet";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return "not yet";
  return (seconds ? IST_TIME_SECONDS : IST_TIME).format(parsed);
}

/** "Mon 21 Sep" for a session date. */
export function sessionDay(isoDate: string | null): string {
  if (!isoDate) return "no session yet";
  const parsed = new Date(`${isoDate}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return "no session yet";
  return `${WEEKDAYS[parsed.getUTCDay()]} ${parsed.getUTCDate()} ${MONTHS[parsed.getUTCMonth()]}`;
}

export interface ClockLabel {
  text: string;
  tone: Tone;
  live: boolean;
}

/** `05` §2's clock table, for the scan states, candidates and chain. */
export function clockLabel(today: OptionsToday): ClockLabel {
  if (today.live) {
    const minute = istTime(today.as_of_minute);
    return today.stale
      ? { text: `Live · ${minute} · stale`, tone: "warning", live: true }
      : { text: `Live · ${minute}`, tone: "positive", live: true };
  }
  if (today.scan_date) {
    const suffix = today.market_open ? "market open" : "market closed";
    return {
      text: `As of close, ${sessionDay(today.scan_date)} · ${suffix}`,
      tone: "neutral",
      live: false,
    };
  }
  return { text: "No scan yet", tone: "neutral", live: false };
}

/** Why there is nothing (or nothing from today) to show — never a blank, never an error. */
export function emptyReasonText(reason: EmptyReason | null): string | null {
  switch (reason) {
    case "collector_off":
      return (
        "The options collector is off, so there is no option chain for a scan to read. " +
        "Nothing below is a statement about today's market — the strategies have not looked."
      );
    case "scan_off":
      return (
        "The options collector is on but the scan is switched off, so no strategy has been " +
        "scanned today."
      );
    case "no_scan_yet_today":
      return (
        "No scan yet today. The first one runs a minute after the session opens at 09:15, " +
        "and then every minute until the close."
      );
    case "never_scanned":
      return "No options scan has ever run on this account.";
    default:
      return null;
  }
}

// --- the header ---------------------------------------------------------------------------------

export function roleLine(role: OptionsRole): string {
  const name = SLEEVE_SHORT[role.sleeve];
  if (role.today) return `${name}: today`;
  if (role.next_date) return `${name}: next ${sessionDay(role.next_date)}`;
  return `${name}: ${reasonText(role.reason).toLowerCase()}`;
}

// --- numbers ------------------------------------------------------------------------------------

function str(value: unknown): string | null {
  if (typeof value === "string" && value.trim() !== "") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

/** A PERCENTAGE string as the page shows it: `"0.9400"` → `0.94%`. */
export function pct(value: unknown, decimals = 2): string {
  const raw = str(value);
  if (raw === null) return "not measured yet";
  const n = Number(raw);
  return Number.isFinite(n) ? `${n.toFixed(decimals)}%` : "not measured yet";
}

/** A FRACTION string as a percentage: `"0.0164"` → `1.64%`. */
export function fractionPct(value: unknown): string {
  const raw = str(value);
  if (raw === null) return "not priced";
  const n = Number(raw);
  return Number.isFinite(n) ? `${(n * 100).toFixed(2)}%` : "not priced";
}

/** A price or level string, with Indian grouping: `"25162.55"` → `25,162.55`. */
export function level(value: unknown): string {
  const raw = str(value);
  if (raw === null) return "not yet";
  const n = Number(raw);
  if (!Number.isFinite(n)) return "not yet";
  const [whole, frac] = raw.split(".");
  const grouped = Number(whole).toLocaleString("en-IN");
  return frac ? `${grouped}.${frac.slice(0, 2).padEnd(2, "0")}` : grouped;
}

/** ₹ from a decimal string, whole rupees unless paise matter. */
export function inr(value: unknown): string {
  const raw = str(value);
  if (raw === null) return "not priced";
  return `₹${level(raw)}`;
}

export interface Check {
  label: string;
  value: string;
  limit: string | null;
  ok: boolean | null;
}

function under(
  value: unknown,
  max: unknown,
  { absolute = false } = {},
): boolean | null {
  const v = str(value);
  const m = str(max);
  if (v === null || m === null) return null;
  const n = Number(v);
  return (absolute ? Math.abs(n) : n) <= Number(m);
}

function over(value: unknown, min: unknown): boolean | null {
  const v = str(value);
  const m = str(min);
  if (v === null || m === null) return null;
  return Number(v) >= Number(m);
}

/** Each sleeve's filters against their thresholds (`05` §2's green/red), from the scan's numbers. */
export function checks(scan: OptionsScan): Check[] {
  const n = scan.numbers;
  switch (scan.sleeve) {
    case "O1M":
    case "O1W":
      if (!("gap_pct" in n)) return [];
      return [
        {
          label: "Opening gap",
          value: pct(n.gap_pct),
          limit: `at most ${pct(n.gap_max_pct)}`,
          ok: under(n.gap_pct, n.gap_max_pct, { absolute: true }),
        },
        {
          label: "Morning range",
          value: pct(n.range_pct),
          limit: `at most ${pct(n.range_max_pct)}`,
          ok: under(n.range_pct, n.range_max_pct),
        },
        {
          label: "Still inside the opening range",
          value:
            n.contained === true
              ? "yes"
              : n.contained === false
                ? "no"
                : "not yet",
          limit: "must be yes",
          ok: typeof n.contained === "boolean" ? n.contained : null,
        },
        {
          label: "Efficiency ratio",
          value: str(n.er) ?? "not measured yet",
          limit: `at most ${str(n.er_max) ?? "the limit"}`,
          ok: under(n.er, n.er_max),
        },
      ];
    case "O2":
      if (!("gap_pct" in n) && !("trend" in n)) return [];
      return [
        {
          label: "Trend (NIFTY 50 close against its average)",
          value:
            n.trend === "UP" ? "up" : n.trend === "DOWN" ? "down" : "not known",
          limit: `close ${level(n.prev_close)} · average ${level(n.ema)}`,
          ok: n.trend === "UP" || n.trend === "DOWN" ? true : null,
        },
        {
          label: "Opening gap",
          value: pct(n.gap_pct),
          limit: `at most ${pct(n.gap_max_pct)}`,
          ok: under(n.gap_pct, n.gap_max_pct, { absolute: true }),
        },
        {
          label: "Opening range",
          value: pct(n.or_pct),
          limit: `at most ${pct(n.or_max_pct)}`,
          ok: under(n.or_pct, n.or_max_pct),
        },
        {
          label: "India VIX",
          value: level(n.vix),
          limit: `at most ${level(n.vix_max)}`,
          ok: under(n.vix, n.vix_max),
        },
      ];
    case "O3A":
      if (!("range_pct" in n)) return [];
      return [
        {
          label: "Morning range",
          value: pct(n.range_pct),
          limit: `at most ${pct(n.range_max_pct)}`,
          ok: under(n.range_pct, n.range_max_pct),
        },
        {
          label: "Break levels",
          value: `${level(n.level_down)} / ${level(n.level_up)}`,
          limit: `efficiency at least ${str(n.er_min) ?? "the floor"}`,
          ok: null,
        },
      ];
    case "O3B":
      if (!("gap_pct" in n)) return [];
      return [
        {
          label: "Opening gap",
          value: pct(n.gap_pct),
          limit: `between ${pct(n.gap_min_pct)} and ${pct(n.gap_max_pct)}`,
          ok:
            n.gap_pct === undefined
              ? null
              : Boolean(
                  over(Math.abs(Number(n.gap_pct)), n.gap_min_pct) &&
                  under(n.gap_pct, n.gap_max_pct, { absolute: true }),
                ),
        },
        {
          label: "Half-gap level",
          value: level(n.half_gap),
          limit: n.held_so_far === false ? "traded through" : "held so far",
          ok: typeof n.held_so_far === "boolean" ? n.held_so_far : null,
        },
      ];
    default:
      return [];
  }
}

/** O2's trigger and the distance to it (`05` §2), when the scan has one. */
export function trigger(
  scan: OptionsScan,
): { level: string; points: string; pct: string } | null {
  const n = scan.numbers;
  if (scan.sleeve !== "O2" || str(n.trigger_level) === null) return null;
  return {
    level: level(n.trigger_level),
    points: str(n.distance_points) ?? "reached",
    pct: str(n.distance_pct) === null ? "reached" : pct(n.distance_pct),
  };
}

export interface SeenBreak {
  time: string;
  close: string;
  direction: string;
}

/** O2's counter-trend breaks — "seen, not traded". */
export function counterTrendBreaks(scan: OptionsScan): SeenBreak[] {
  const raw = scan.numbers.counter_trend_breaks;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((item): SeenBreak[] => {
    if (item === null || typeof item !== "object") return [];
    const row = item as Record<string, unknown>;
    return [
      {
        time: str(row.close_time)?.slice(0, 5) ?? "",
        close: level(row.close),
        direction: row.direction === "UP" ? "up" : "down",
      },
    ];
  });
}

/** A not-today row's next date, from the scan (O1/O3) — O2 trades every session. */
export function nextDate(scan: OptionsScan): string | null {
  return str(scan.numbers.next_date);
}

function isLeg(value: unknown): value is CandidateLeg {
  return (
    value !== null &&
    typeof value === "object" &&
    "strike" in value &&
    "option_type" in value
  );
}

/** The scan's candidates, typed — anything that is not a candidate object is dropped. */
export function candidates(scan: OptionsScan): Candidate[] {
  return scan.candidates.flatMap((item): Candidate[] => {
    if (item === null || typeof item !== "object") return [];
    const row = item as Record<string, unknown>;
    if (!Array.isArray(row.legs)) return [];
    return [{ ...(row as Candidate), legs: row.legs.filter(isLeg) }];
  });
}

export function structureName(structure: string): string {
  switch (structure) {
    case "IRON_CONDOR":
      return "Iron condor";
    case "IRON_FLY":
      return "Iron fly";
    case "LONG_OPTION":
      return "Long option";
    case "DEBIT_SPREAD":
      return "Debit spread";
    default:
      return sentenceCase(structure);
  }
}

export function legLine(leg: CandidateLeg): string {
  const side = leg.side === "SELL" ? "Sell" : "Buy";
  return `${side} ${level(leg.strike).replace(".00", "")} ${leg.option_type}`;
}

/** `sizing_mode` in words — paper one lot is the default for the whole paper period. */
export function sizingText(mode: string | null): string {
  if (mode === "PAPER_ONE_LOT") return "paper · one lot";
  if (mode === "BUDGET") return "sized from the risk budget";
  return "not sized";
}
