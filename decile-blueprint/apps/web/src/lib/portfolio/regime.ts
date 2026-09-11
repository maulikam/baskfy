import type { Schemas } from "@baskfy/api-client";

import { metric, type Metric } from "@/lib/portfolio/command-center";

/**
 * The momentum-regime module of the Portfolio Command Center — PC5.
 *
 * Brief: the panel must be able to write *"Risk reduced to R2 because market breadth weakened and
 * the Smallcap index closed below its 50-DMA. Current exposure is 8 percentage points above
 * target."* Both halves of that sentence have a rule here, and neither is composed by the browser:
 *
 * * **The gap is arithmetic over two real fields.** `actual_equity_pct - target_equity_cap_pct`,
 *   which is the desk's own convention — `regime_store.record_exposure` computes
 *   `gap = actual_equity_pct - target_equity_cap_pct` before it writes the row. The same
 *   subtraction, the same sign, so the panel and the desk can never disagree about which side of
 *   the cap the book is on.
 * * **The cause is quoted, never written.** `RegimeOut.reasons` are the sentences the desk itself
 *   produced at evaluation time (`baskfy_core.exposure.regime.describe` maps its stable reason
 *   codes through `DISPLAY_REASONS`). They are printed verbatim. This module neither paraphrases
 *   them nor assembles a narrative out of the numbers.
 *
 * WHAT THE PAYLOAD DOES NOT CARRY, AND WHY THAT IS THE INTERESTING PART
 * --------------------------------------------------------------------
 * The desk's engine computes far more than the API returns. `regime_evaluations` stores
 * `raw_candidate_tier`, `algorithm_version`, `config_hash` and `breadth_coverage_pct`;
 * `input_snapshot_json` stores `index_diagnostics`, which is every structural index against every
 * moving average with its confirming-close count; `regime_exposure` stores `pending_buy_pct`,
 * `pending_sell_pct` and `execution_status`. `GET /api/v1/desk/regime` selects none of them
 * (`services/api/src/baskfy_api/routers/desk.py`).
 *
 * So those figures are **not missing from the product — they are missing from the response**, and
 * {@link DECLARED_UNAVAILABLE} says exactly that, one entry at a time, with the column that holds
 * each one and the change that would surface it. That is a far more useful answer than "not
 * available", and it is checkable: anyone can open the router and see the select list.
 *
 * Nothing here invents a financial number. `docs/PORTFOLIO-COMMAND-CENTER.md` §6.2 rule 2.
 *
 * PURE
 * ----
 * No I/O, no clock, no randomness. "Today" is an argument, because a panel that asked the browser
 * what day it is would render differently in two time zones and could not be tested at all. The
 * caller passes the trading date in exchange time, or `null` — and `null` produces a stated
 * "the overdue check could not be made", never an assumption that the evaluation is fresh.
 */

export type RegimeOut = Schemas["RegimeOut"];

/* ------------------------------------------------------------------ the desk's own vocabulary */

/**
 * The four tiers, named.
 *
 * `stance` describes what the tier *is*, in the desk's terms. It never tells a reader what to do
 * with their own money — Baskfy is not a registered adviser (D3) — and, for R2 specifically, it
 * never says "exit". R2 is reduced exposure with a lower cap; whether new positions open, and at
 * what size, is `new_buys`, which the desk resolves separately and which the sentinel veto can
 * override. `resolve_new_buys` in `baskfy_core.exposure.regime` is the rule: R2 gets half size
 * *unless* the momentum sentinel is below its 50-DMA or the data is unusable.
 */
interface TierSpec {
  readonly label: string;
  /** 1 = most risk-on … 4 = most defensive, matching `RegimeTier.ordinal`. */
  readonly ordinal: number;
  readonly stance: string;
}

const TIERS: Readonly<Record<string, TierSpec>> = {
  R1: {
    label: "Risk-on",
    ordinal: 1,
    stance:
      "The desk runs its portfolio at its full equity cap. Every condition the desk checks — long-term health, breadth and the momentum sentinel — was risk-on at this evaluation.",
  },
  R2: {
    label: "Cautious",
    ordinal: 2,
    stance:
      "Reduced exposure, not an exit. The desk's portfolio stays invested to a lower cap. Whether new positions open, and at what size, is the new-buy policy below — the desk's own rule allows half-sized entries at this tier unless the momentum sentinel vetoes them.",
  },
  R3: {
    label: "Defensive",
    ordinal: 3,
    stance:
      "Exposure is cut further and the tier itself stops new positions. Holdings above the cap are what the desk reconciles down on its next weekly plan.",
  },
  R4: {
    label: "Risk-off",
    ordinal: 4,
    stance: "The most defensive rung the desk has.",
  },
};

/** "full" | "half" | "blocked" — `NewBuyMode` in `baskfy_core.exposure.regime`. */
const NEW_BUYS: Readonly<Record<string, { label: string; detail: string }>> = {
  full: {
    label: "Full size",
    detail: "New positions open at the size the desk's sizing rule gives them.",
  },
  half: {
    label: "Half size",
    detail:
      "New positions open at half the size the sizing rule gives them. This is a smaller entry, not a stop on entries.",
  },
  blocked: {
    label: "Blocked",
    detail:
      "No new position is opened while this evaluation stands. Positions already open are managed under their own stops.",
  },
};

/**
 * `RegimeMode` — and it decides whether anything on this panel is being acted on at all.
 *
 * A stance in `observe` forces no selling. Showing a tier without showing this would let a reader
 * think the book had been moved to the cap when nothing had happened.
 */
const MODES: Readonly<Record<string, { label: string; detail: string }>> = {
  observe: {
    label: "Observing",
    detail:
      "The desk records this stance and acts on none of it: no selling is forced and no cap is reconciled while the mode is observe.",
  },
  propose: {
    label: "Proposing",
    detail:
      "The desk builds its weekly plan from this stance. A person still confirms every line before anything reaches a broker.",
  },
  enforce: {
    label: "Enforcing",
    detail:
      "The desk reconciles its portfolio towards this cap on its weekly plan. A person still confirms the plan before anything reaches a broker.",
  },
};

/**
 * The desk's configured exposure ladder, for CONTEXT ONLY.
 *
 * `kite-momentum-rebalancer/app/config.py` builds `tier_exposure_pct={"R1": 100.0, "R2": 70.0,
 * "R3": 40.0, "R4": REGIME_R4_EQUITY_PCT}` and `REGIME_R4_EQUITY_PCT` defaults to 10. Every one of
 * those four is an environment-overridable default, so **none of them is ever used as the cap in
 * force**: {@link RegimeReading.targetEquity} comes from `target_equity_cap_pct` on the response
 * and, when the response has no figure, says so rather than reaching for the rung.
 *
 * (`baskfy_core/sleeves.py:185` falls back to R4 = 0 with no caps table. That is a fallback in a
 * different module, not the desk's operating value, and it is a second reason not to guess.)
 */
export interface LadderRung {
  readonly tier: string;
  readonly label: string;
  readonly equityPct: number;
  /** True where the value is not a literal but an environment variable's default. */
  readonly fromEnv: string | null;
}

/** One name per tier across the whole module: the rung and the stance cannot drift apart. */
function tierLabel(code: string): string {
  return TIERS[code]?.label ?? code;
}

export const DESK_LADDER_DEFAULTS: readonly LadderRung[] = [
  { tier: "R1", label: tierLabel("R1"), equityPct: 100, fromEnv: null },
  { tier: "R2", label: tierLabel("R2"), equityPct: 70, fromEnv: null },
  { tier: "R3", label: tierLabel("R3"), equityPct: 40, fromEnv: null },
  { tier: "R4", label: tierLabel("R4"), equityPct: 10, fromEnv: "REGIME_R4_EQUITY_PCT" },
];

export const LADDER_CAVEAT =
  "The desk's configured defaults, not this evaluation's figures. app/config.py sets the ladder and REGIME_R4_EQUITY_PCT supplies the R4 rung, so every rung can be changed by an environment variable. The cap in force above is read from the evaluation itself and never from this table.";

/**
 * The structural indices the weekly regime reads, as `RegimeConfig` defaults them.
 *
 * Named here only so the panel can say precisely *which* distances it cannot show. These are the
 * WEEKLY desk's indices. They are not the swing book's market gate, which reads NIFTY MidSmallcap
 * 400 (SW17, `67df9b4`) — two different books asking two different questions, and conflating them
 * is how a wrong gate gets shipped.
 */
export const SENTINEL_INDEX = "NIFTY 500 MOMENTUM 50";
export const STRUCTURAL_INDICES = ["NIFTY 50", "NIFTY MIDCAP 150", "NIFTY SMLCAP 250"] as const;
export const MA_LENGTHS = [20, 50, 200] as const;

/* ---------------------------------------------------------- what the response does not include */

export interface DeclaredUnavailable {
  readonly id: string;
  readonly name: string;
  /** Why there is no figure. Always names where the value does live. */
  readonly reason: string;
  /** The change that would put it on this panel. */
  readonly unblockedBy: string;
}

/**
 * Six figures the brief asks for that `RegimeOut` does not carry, each with its real home.
 *
 * Every one of these exists on the desk. None of them is computed here from something else, and
 * none is rendered as a dash: the panel prints the reason in the figure's place.
 */
export const DECLARED_UNAVAILABLE: readonly DeclaredUnavailable[] = [
  {
    id: "candidate-tier",
    name: "Candidate tier",
    reason:
      "The desk records the tier its reading pointed at before the one-rung-a-week limit is applied, but does not yet publish it. Until it does, this panel can show where the policy landed and not where the reading pointed.",
    unblockedBy: "Add raw_candidate_tier and transition_limited to RegimeOut.",
  },
  {
    id: "index-dma",
    name: `Distance from the 20-, 50- and 200-DMA for ${STRUCTURAL_INDICES.join(", ")}`,
    reason:
      "The evaluation measures each of the three structural indices against each of its three moving averages and keeps the result in input_snapshot_json.index_diagnostics. The response carries no index field at all, so there is no honest way to state a distance here.",
    unblockedBy: "Expose index_diagnostics on RegimeOut.",
  },
  {
    id: "confirmation-progress",
    name: "Confirmation progress",
    reason:
      "An index/average pair only changes side after confirm_days closes clear of a 150 basis-point buffer, and the count so far is MaSignal.confirming_closes inside the same index_diagnostics snapshot. It is not in the response, so the panel cannot say how far through a confirmation any pair is.",
    unblockedBy: "Expose index_diagnostics on RegimeOut.",
  },
  {
    id: "breadth-coverage",
    name: "Breadth coverage",
    reason:
      "Breadth is only usable when it was measured over enough of the universe — regime_evaluations.breadth_coverage_pct against the configured minimum. The percentage is returned; the coverage behind it is not, so the panel shows the reading without being able to say how much of the universe it covers.",
    unblockedBy: "Add breadth_coverage_pct to RegimeOut.",
  },
  {
    id: "pending-execution",
    name: "Pending execution against the cap",
    reason:
      "regime_exposure carries pending_buy_pct, pending_sell_pct and execution_status beside the two exposure figures, which is what distinguishes a gap nothing has been done about from one a plan is already closing. The route selects only actual_equity_pct and target_equity_cap_pct.",
    unblockedBy: "Add the three execution columns to RegimeOut.",
  },
  {
    id: "algorithm-version",
    name: "Algorithm version and configuration hash",
    reason:
      "Every evaluation is stamped with regime_evaluations.algorithm_version and config_hash — the audit pair that says which rules and which thresholds produced this tier. Neither is returned, so a reader cannot tell whether two evaluations were made by the same model.",
    unblockedBy: "Add algorithm_version and config_hash to RegimeOut.",
  },
];

/* ---------------------------------------------------------------- the momentum sentinel's veto */

/**
 * The desk's own sentences, copied exactly from `DISPLAY_REASONS` in
 * `baskfy_core/exposure/regime.py`, keyed by the stable reason code that produces each.
 *
 * WHY MATCH ON SENTENCES AND NOT ON CODES
 * ---------------------------------------
 * Because the response only has sentences. The evaluation writes both — `reason_codes_json` and
 * `reasons_json` — and `/desk/regime` returns the second. Reason codes are a documented audit
 * contract ("never rename; add new ones instead"); the rendered text is not. So this table is
 * pinned to the code's *current* wording and the failure mode is deliberately one-sided: a
 * rewording makes the veto read "not stated", never "clear". The panel can under-report a veto it
 * cannot see. It can never invent one, and it can never claim the sentinel is clear on silence.
 */
const SENTINEL_VETO_SENTENCES: readonly string[] = [
  /* SENTINEL_BELOW_50DMA_VETO */ "Momentum sentinel below its 50-DMA — new entries vetoed.",
  /* SENTINEL_FLOOR_R2 */ "Sentinel 50-DMA veto floors the candidate tier at R2.",
  /* NEW_BUYS_BLOCKED_SENTINEL */ "New entries blocked by the momentum sentinel veto.",
];

const SENTINEL_FLOOR_SENTENCES: readonly string[] = [
  /* R3_SENTINEL_BELOW_200DMA */ "Momentum sentinel confirmed below its 200-DMA.",
  /* SENTINEL_FLOOR_R3 */ "Sentinel 200-DMA break floors the candidate tier at R3.",
  /* R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH */
  "Momentum sentinel below its 200-DMA with weak long-term health and breadth.",
];

const SENTINEL_UNCONFIRMED_SENTENCE = "Momentum sentinel state is not yet confirmed.";

/** The only sentence that is positive evidence the sentinel was risk-on. */
const SENTINEL_CLEAR_SENTENCE = "Health, breadth and the momentum sentinel all risk-on.";

export type SentinelState = "in-force" | "reported-clear" | "unconfirmed" | "not-stated";

export interface SentinelFinding {
  readonly id: "veto-50dma" | "floor-200dma";
  readonly label: string;
  readonly state: SentinelState;
  /** What the state means, in the desk's terms. */
  readonly summary: string;
  /** The desk's sentences that decided it, verbatim and in the order it recorded them. */
  readonly evidence: readonly string[];
}

export interface SentinelReading {
  readonly indexName: string;
  readonly veto: SentinelFinding;
  readonly floor: SentinelFinding;
  /** Why silence here is not the same as "clear". Always shown. */
  readonly note: string;
}

const SENTINEL_SILENCE_NOTE =
  "The desk notes the momentum sentinel only when it changes something, and publishes those notes as sentences rather than as a state. So where the desk says nothing, this panel says nothing was stated — it never reads silence as an all-clear.";

function findingFor(
  id: SentinelFinding["id"],
  label: string,
  reasons: readonly string[],
  triggers: readonly string[],
  inForceSummary: string,
): SentinelFinding {
  const hits = reasons.filter((r) => triggers.includes(r.trim()));
  if (hits.length > 0) {
    return { id, label, state: "in-force", summary: inForceSummary, evidence: hits };
  }
  const unconfirmed = reasons.filter((r) => r.trim() === SENTINEL_UNCONFIRMED_SENTENCE);
  if (unconfirmed.length > 0) {
    return {
      id,
      label,
      state: "unconfirmed",
      summary:
        "The desk could not confirm which side of its averages the sentinel is on, so it imposed no floor from it this evaluation.",
      evidence: unconfirmed,
    };
  }
  const clear = reasons.filter((r) => r.trim() === SENTINEL_CLEAR_SENTENCE);
  if (clear.length > 0) {
    return {
      id,
      label,
      state: "reported-clear",
      summary: "The evaluation recorded the sentinel as risk-on.",
      evidence: clear,
    };
  }
  return {
    id,
    label,
    state: "not-stated",
    summary: SENTINEL_SILENCE_NOTE,
    evidence: [],
  };
}

export function sentinelReading(reasons: readonly string[]): SentinelReading {
  return {
    indexName: SENTINEL_INDEX,
    veto: findingFor(
      "veto-50dma",
      "50-DMA veto on new entries",
      reasons,
      SENTINEL_VETO_SENTENCES,
      "The sentinel is below its 50-DMA. That vetoes new entries outright and floors the tier at R2, whatever the rest of the model said.",
    ),
    floor: findingFor(
      "floor-200dma",
      "200-DMA floor on the tier",
      reasons,
      SENTINEL_FLOOR_SENTENCES,
      "The sentinel is confirmed below its 200-DMA, which floors the tier at R3. The sentinel can only make the stance more defensive, never less.",
    ),
    note: SENTINEL_SILENCE_NOTE,
  };
}

/* ------------------------------------------------------------------------- the exposure figures */

export type GapDirection = "above" | "below" | "on-target";

export interface ExposureGap {
  readonly metric: Metric;
  /** `null` when either side of the subtraction is missing. */
  readonly direction: GapDirection | null;
  /** Magnitude, one decimal place, unsigned — for the reading that names the direction in words. */
  readonly magnitude: string | null;
  /** The brief's second sentence, or the reason it cannot be written. */
  readonly sentence: string;
}

/** One decimal place, and the direction is taken from the ROUNDED figure. */
function round1(value: number): number {
  return Math.round(value * 10) / 10;
}

const GAP_DEFINITION =
  "Actual equity exposure minus the cap this tier targets, in percentage points. The same subtraction and the same sign the desk uses when it writes regime_exposure.exposure_gap_pct, so this panel and the desk can never disagree about which side of the cap the exposure is on.";

export function exposureGap(regime: RegimeOut): ExposureGap {
  const actual = regime.actual_equity_pct;
  const target = regime.target_equity_cap_pct;

  if (actual === null || actual === undefined || target === null || target === undefined) {
    const which =
      actual === null || actual === undefined
        ? target === null || target === undefined
          ? "Neither the actual exposure nor the cap"
          : "The actual exposure"
        : "The cap";
    const reason = `${which} was recorded for this evaluation, so there is no gap to state. Both figures come from regime_exposure, which is written when the desk observes its portfolio against the tier.`;
    return {
      metric: metric("Exposure gap", GAP_DEFINITION, null, reason),
      direction: null,
      magnitude: null,
      sentence: reason,
    };
  }

  const gap = round1(actual - target);
  const direction: GapDirection = gap > 0 ? "above" : gap < 0 ? "below" : "on-target";
  const magnitude = Math.abs(gap).toFixed(1);
  const points = magnitude === "1.0" ? "percentage point" : "percentage points";
  const sentence =
    direction === "on-target"
      ? "Current exposure is on target — actual and cap agree to within half a tenth of a percentage point."
      : `Current exposure is ${magnitude} ${points} ${direction} target.`;

  return {
    /* Signed, because the sign IS the direction and a component that only had the magnitude
       could put the wrong word beside it. The word is carried separately all the same. */
    metric: metric("Exposure gap", GAP_DEFINITION, gap.toFixed(1), null),
    direction,
    magnitude,
    sentence,
  };
}

/* --------------------------------------------------------------------- notices and currentness */

export interface RegimeNotice {
  readonly id: string;
  readonly severity: "critical" | "review" | "info";
  readonly headline: string;
  readonly detail: string;
  /** Exactly one, and never an instruction about a position. */
  readonly nextStep: string;
}

/* ----------------------------------------------------------------------------- the whole reading */

export interface TierReading {
  readonly code: string;
  readonly label: string;
  readonly ordinal: number | null;
  readonly stance: string;
  /** False when the response used a tier string this build does not know. */
  readonly recognised: boolean;
}

export interface NewBuyPolicy {
  readonly code: string | null;
  readonly label: string;
  readonly detail: string;
  readonly recognised: boolean;
}

export interface ModeReading {
  readonly code: string | null;
  readonly label: string;
  readonly detail: string;
  readonly recognised: boolean;
}

export type TierMovement = "reduced" | "raised" | "unchanged" | "unstated" | "first";

export interface RegimeReading {
  readonly applied: TierReading;
  readonly previous: TierReading | null;
  readonly movement: TierMovement;
  /** "Risk reduced to R2 from R1." — the first half of the brief's sentence. */
  readonly movementSentence: string;
  /** False whenever a notice says the stance may not be the one in force. */
  readonly current: boolean;
  /** The heading the panel must use. Never "in force" when {@link current} is false. */
  readonly standingHeadline: string;
  readonly evaluatedAt: string;
  readonly evaluatedOn: string;
  /** The same date as {@link evaluatedOn}, as a figure, so a panel row can render it beside the
   *  two that can be missing without one of the three being rendered a different way. */
  readonly evaluated: Metric;
  readonly signalDate: Metric;
  readonly nextEvaluation: Metric;
  readonly breadth: Metric;
  readonly actualEquity: Metric;
  readonly targetEquity: Metric;
  readonly gap: ExposureGap;
  readonly newBuys: NewBuyPolicy;
  readonly mode: ModeReading;
  readonly sentinel: SentinelReading;
  /** The desk's sentences, verbatim and in its order. */
  readonly reasons: readonly string[];
  /** Set when it recorded none, so the panel says so instead of showing an empty list. */
  readonly reasonsNote: string | null;
  readonly notices: readonly RegimeNotice[];
  readonly unavailable: readonly DeclaredUnavailable[];
  readonly ladder: readonly LadderRung[];
  readonly ladderCaveat: string;
}

export type RegimePanelState =
  | { readonly kind: "ready"; readonly reading: RegimeReading }
  | { readonly kind: "unavailable"; readonly notice: RegimeNotice };

export interface RegimeInput {
  /** `GET /api/v1/desk/regime`, or `null` when the call did not answer. */
  readonly regime: RegimeOut | null;
  /** Today's date in exchange time, `YYYY-MM-DD`, or `null` when the caller has none. */
  readonly today: string | null;
  /** What went wrong, when `regime` is null. */
  readonly unavailableReason?: string | null | undefined;
}

function tierReading(code: string | null | undefined, targetCapPct: number | null): TierReading | null {
  if (code === null || code === undefined || code.trim() === "") return null;
  const key = code.trim().toUpperCase();
  const spec = TIERS[key];
  if (spec === undefined) {
    return {
      code: code.trim(),
      label: code.trim(),
      ordinal: null,
      stance: `The desk recorded the tier "${code.trim()}", which this screen does not have a description for. Read the desk's own reasons below rather than this line.`,
      recognised: false,
    };
  }
  return { code: key, label: spec.label, ordinal: spec.ordinal, stance: stanceFor(key, spec, targetCapPct), recognised: true };
}

/**
 * R4's meaning depends on a number, so it is read rather than assumed.
 *
 * `app/config.py` is explicit about this: "R4 residual policy must be EXPLICIT. If you set max
 * residual names to 0, R4 becomes full cash — which is the honest configuration for 'exit
 * everything', rather than describing a 10% residual as a full exit." The panel obeys the same
 * rule, from the cap the evaluation actually recorded.
 */
function stanceFor(key: string, spec: TierSpec, targetCapPct: number | null): string {
  if (key !== "R4") return spec.stance;
  if (targetCapPct === null) {
    return `${spec.stance} Whether that is a full exit or a residual holding depends on the cap, which this evaluation did not record.`;
  }
  if (round1(targetCapPct) === 0) {
    return `${spec.stance} The cap it recorded is 0%, which is a full exit.`;
  }
  return `${spec.stance} The cap it recorded is ${round1(targetCapPct).toFixed(1)}%, so this is a residual holding and not a full exit.`;
}

function newBuyPolicy(code: string | null | undefined): NewBuyPolicy {
  const key = (code ?? "").trim().toLowerCase();
  const spec = NEW_BUYS[key];
  if (spec !== undefined) {
    return { code: key, label: spec.label, detail: spec.detail, recognised: true };
  }
  if (key === "") {
    return {
      code: null,
      label: "Not recorded",
      detail:
        "This evaluation recorded no new-buy policy. The panel will not guess one from the tier: the desk's rule lets the momentum sentinel and unusable data block entries at any tier, so the tier alone does not determine it.",
      recognised: false,
    };
  }
  return {
    code: key,
    label: `Recorded as "${key}"`,
    detail:
      "The desk recorded a new-buy policy this screen does not have a description for. It is printed as written rather than mapped to a guess.",
    recognised: false,
  };
}

function modeReading(code: string | null | undefined): ModeReading {
  const key = (code ?? "").trim().toLowerCase();
  const spec = MODES[key];
  if (spec !== undefined) {
    return { code: key, label: spec.label, detail: spec.detail, recognised: true };
  }
  if (key === "") {
    return {
      code: null,
      label: "Not recorded",
      detail:
        "This evaluation did not record which mode the desk was in, so the panel cannot say whether the stance is only being observed or is being reconciled to.",
      recognised: false,
    };
  }
  return {
    code: key,
    label: `Recorded as "${key}"`,
    detail: "The desk recorded a mode this screen does not have a description for.",
    recognised: false,
  };
}

function movementOf(
  applied: TierReading,
  previous: TierReading | null,
): { movement: TierMovement; sentence: string } {
  if (previous === null) {
    return {
      movement: "first",
      sentence: `The desk recorded no previous tier against this evaluation, so ${applied.code} is shown without a change.`,
    };
  }
  if (previous.code === applied.code) {
    return { movement: "unchanged", sentence: `Held at ${applied.code}; unchanged from the previous evaluation.` };
  }
  if (applied.ordinal === null || previous.ordinal === null) {
    return {
      /* Not "unchanged" — the tier DID change and the direction is what is unknown. Calling an
         unranked move "unchanged" would be the quieter of the two lies and still a lie. */
      movement: "unstated",
      sentence: `Changed to ${applied.code} from ${previous.code}. One of the two is a tier this screen does not rank, so the direction is not stated.`,
    };
  }
  return applied.ordinal > previous.ordinal
    ? { movement: "reduced", sentence: `Risk reduced to ${applied.code} from ${previous.code}.` }
    : { movement: "raised", sentence: `Risk raised to ${applied.code} from ${previous.code}.` };
}

/** `YYYY-MM-DD` sorts lexicographically, which is why no clock is needed to compare two of them. */
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function noticesFor(regime: RegimeOut, today: string | null): RegimeNotice[] {
  const notices: RegimeNotice[] = [];

  if (regime.data_stale) {
    notices.push({
      id: "data-stale",
      severity: "critical",
      headline: "This evaluation ran on data the desk judged stale",
      detail:
        "When index data is stale the desk holds the previous tier and blocks new buys rather than acting on what it cannot see. The tier below is therefore the last one it could stand behind, not a fresh reading of this week's market.",
      nextStep: "Re-run the weekly evaluation on the desk console once the index history has caught up.",
    });
  }

  if (regime.manual_action_required) {
    notices.push({
      id: "manual-action",
      severity: "critical",
      headline: "This evaluation is flagged for a person to look at",
      detail:
        "The desk sets this when it reached a state it will not resolve on its own — most often a first evaluation whose exposure is already outside the cap, which it refuses to close by forced selling from no prior history.",
      nextStep: "Open the desk console and clear the flag before treating this stance as settled.",
    });
  }

  const due = regime.next_evaluation_date;
  if (due !== null && due !== undefined && ISO_DATE.test(due)) {
    if (today === null) {
      notices.push({
        id: "overdue-unknown",
        severity: "info",
        headline: "Whether the next evaluation is overdue could not be checked",
        detail: `The desk scheduled its next evaluation for ${due}. This panel was rendered without a trading date to compare that against, so it is not claiming the stance is current.`,
        nextStep: `Compare ${due} against today before reading the tier as this week's.`,
      });
    } else if (ISO_DATE.test(today) && today > due) {
      notices.push({
        id: "overdue",
        severity: "review",
        headline: `The weekly evaluation was due on ${due} and no newer one has been recorded`,
        detail:
          "The desk evaluates on a weekly schedule. When the newest evaluation is older than the date it set for itself, the stance shown here belongs to an earlier week and the market has had sessions the desk has not read.",
        nextStep: "Run the weekly evaluation on the desk console, then reload this panel.",
      });
    }
  }

  return notices;
}

export function regimeReading(input: RegimeInput): RegimePanelState {
  const { regime, today } = input;

  if (regime === null) {
    const because = (input.unavailableReason ?? "").trim();
    return {
      kind: "unavailable",
      notice: {
        id: "desk-unreachable",
        severity: "critical",
        headline: "The desk's market stance could not be read",
        detail: because
          ? `${because} No tier is shown, because the last one this screen saw would be a claim about a market it cannot currently see.`
          : "The desk did not answer, and gave no reason. No tier is shown, because the last one this screen saw would be a claim about a market it cannot currently see.",
        nextStep: "Open the desk's own stance page once the desk is reachable.",
      },
    };
  }

  const target = regime.target_equity_cap_pct ?? null;
  const applied =
    tierReading(regime.tier, target) ??
    /* `tier` is required by the schema, so this is only reachable for an empty string. */
    ({
      code: "unknown",
      label: "Not recorded",
      ordinal: null,
      stance:
        "This evaluation carries no tier. The desk's reasons below are still its own; the stance itself is not stated.",
      recognised: false,
    } satisfies TierReading);
  const previous = tierReading(regime.previous_tier, null);
  const { movement, sentence: movementSentence } = movementOf(applied, previous);

  const notices = noticesFor(regime, today);
  if (!applied.recognised) {
    /* `current` drops for an unrecognised tier, so the heading changes. A heading that changes
       with nothing to explain it is the defect this closes. */
    notices.unshift({
      id: "tier-unrecognised",
      severity: "review",
      headline: `The desk recorded the tier "${applied.code}", which this screen does not know`,
      detail:
        "Baskfy knows R1 to R4. A tier outside that set means the desk's ladder has gained a rung this build has never seen, so the panel prints it as written and describes none of it.",
      nextStep: "Read the desk's own reasons below, and its stance page, rather than this summary.",
    });
  }
  const current = notices.every((n) => n.severity === "info") && applied.recognised;

  const reasons = regime.reasons.filter((r) => r.trim() !== "");

  return {
    kind: "ready",
    reading: {
      applied,
      previous,
      movement,
      movementSentence,
      current,
      standingHeadline: current ? "Stance in force" : "Last recorded stance — not confirmed current",
      evaluatedAt: regime.evaluated_at,
      evaluatedOn: regime.evaluated_at.slice(0, 10),
      evaluated: metric(
        "Last evaluated",
        "When the desk wrote this evaluation. The desk evaluates weekly, so this is normally the most recent Friday's run rather than today.",
        regime.evaluated_at.slice(0, 10) || null,
        "This evaluation carries no timestamp, which should not happen — the desk stamps every row it writes.",
      ),
      signalDate: metric(
        "Closing prices from",
        "The session whose closes the evaluation read. A weekly evaluation is made after a session ends, so this is a closed day and not a running one.",
        regime.signal_date,
        "This evaluation did not record which session's closes it read.",
      ),
      nextEvaluation: metric(
        "Next evaluation",
        "The date the desk scheduled its next weekly evaluation for when it wrote this one.",
        regime.next_evaluation_date,
        "This evaluation did not record when the next one is due.",
      ),
      breadth: metric(
        "Market breadth",
        "The share of the desk's universe trading above its own 20-day average at this evaluation. It is one of the three inputs to the tier, alongside long-term health and the momentum sentinel.",
        numberOrNull(regime.breadth_pct),
        "No breadth reading was supplied to this evaluation, so the desk's breadth rules did not fire.",
      ),
      actualEquity: metric(
        "Actual equity exposure",
        "How much of the desk's capital was in shares when it observed its portfolio against this tier.",
        numberOrNull(regime.actual_equity_pct),
        "The desk recorded no exposure observation against this evaluation, so how much is in shares is not stated here.",
      ),
      targetEquity: metric(
        "Target equity cap",
        "The most this tier allows in shares. Read from this evaluation, never from the configured ladder — the ladder's rungs are environment-overridable defaults.",
        numberOrNull(target),
        "The desk recorded no cap against this evaluation. The panel will not fill it in from the configured ladder, because a rung is a default and not evidence of what was in force.",
      ),
      gap: exposureGap(regime),
      newBuys: newBuyPolicy(regime.new_buys),
      mode: modeReading(regime.mode),
      sentinel: sentinelReading(reasons),
      reasons,
      reasonsNote:
        reasons.length === 0
          ? "This evaluation recorded no reasons. The tier above is what it wrote; the panel has nothing of the desk's own to quote for it."
          : null,
      notices,
      unavailable: DECLARED_UNAVAILABLE,
      ladder: DESK_LADDER_DEFAULTS,
      ladderCaveat: LADDER_CAVEAT,
    },
  };
}

/** Percentages arrive as JSON numbers; `metric()` speaks in decimal strings. One decimal place. */
function numberOrNull(value: number | null | undefined): string | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return round1(value).toFixed(1);
}
