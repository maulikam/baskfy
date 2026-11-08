"use client";

import { useState } from "react";
import type { Route } from "next";
import {
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  CircleSlash,
  Clock,
  Info,
  ShieldAlert,
} from "lucide-react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Metric } from "@/lib/portfolio/command-center";
import {
  regimeReading,
  type ExposureGap,
  type RegimeNotice,
  type RegimeOut,
  type RegimeReading,
  type SentinelFinding,
} from "@/lib/portfolio/regime";
import { cn } from "@/lib/utils";

/**
 * The momentum-regime panel — PC5 of `docs/PORTFOLIO-COMMAND-CENTER.md` §6.
 *
 * The one thing on the command centre that is not about the reader's own book. It is the desk's
 * weekly answer to "how defensive is the strategy being, and why", and the brief wants it to be
 * able to say, in one breath: *"Risk reduced to R2 because market breadth weakened and the
 * Smallcap index closed below its 50-DMA. Current exposure is 8 percentage points above target."*
 *
 * Both halves of that come from `lib/portfolio/regime.ts`, and neither is written here. The gap is
 * the desk's own subtraction; the causes are the desk's own sentences, printed as it wrote them,
 * under a heading that says whose words they are. This component decides layout and nothing else,
 * which is why there is no string in it that describes the market.
 *
 * THE FOUR RULES CARRIED IN THE MARKUP
 * ------------------------------------
 * **Never a bare dash.** Every figure is a `Metric`, and a `Metric` with no value renders its
 * reason in the figure's place. The six things `RegimeOut` does not carry are not omitted either:
 * they are listed by name with the column that holds each one on the desk.
 *
 * **Never colour alone.** The gap's direction is a word — above, below, on target — beside a
 * glyph, and the colour is third. The exposure meter has a text reading underneath it that says
 * the same thing the bar does.
 *
 * **Never an old tier as current.** A stale evaluation, a manual-action flag or an overdue weekly
 * run each change the panel's own heading, not just add a chip: it reads "Last recorded stance —
 * not confirmed current", and every notice carries one next step.
 *
 * **Never investment advice.** Baskfy is not registered to give it (D3). Nothing here addresses
 * the reader as an investor; the panel reports a decision somebody else already took about a
 * different book, and says so in as many words at the foot.
 *
 * R2 IS NOT AN EXIT
 * -----------------
 * Named in the brief and held by `regime.test.ts`. R2 is a lower cap with entries usually still
 * open at half size — `resolve_new_buys` gives R2 `HALF` unless the momentum sentinel vetoes or
 * the data is unusable. Reading it as "out of the market" would be a material misreading of the
 * desk's stance, so no copy in this component or its module says so.
 */

const REGIME_PAGE: Route = "/regime";

/** Tabular numerals, as everywhere else on this screen. */
const FIGURE = "tabular-nums tracking-tight";

const NOTICE_LEVEL = {
  critical: { word: "Critical", Icon: CircleAlert, className: "text-negative" },
  review: { word: "Review", Icon: Clock, className: "text-warning" },
  info: { word: "Informational", Icon: Info, className: "text-muted-foreground" },
} as const;

/** The four tiers as a scale, so R2 reads as "second of four" rather than as a code. */
const TIER_ORDER = ["R1", "R2", "R3", "R4"] as const;

function NoticeRow({ notice }: { notice: RegimeNotice }) {
  const level = NOTICE_LEVEL[notice.severity];
  const { Icon } = level;
  return (
    <li className="flex gap-2 px-4 py-3" data-testid={`regime-notice-${notice.id}`}>
      <Icon aria-hidden="true" className={cn("mt-0.5 size-4 shrink-0", level.className)} />
      <div className="min-w-0">
        <p className="text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
          {level.word}
        </p>
        <p className="mt-0.5 text-sm font-medium leading-snug">{notice.headline}</p>
        <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{notice.detail}</p>
        <p className="mt-1 text-xs leading-snug">
          <span className="font-medium">Next step: </span>
          <span className="text-muted-foreground">{notice.nextStep}</span>
        </p>
      </div>
    </li>
  );
}

/**
 * A figure with its definition, or the reason there is no figure — the same contract `MetricCell`
 * carries on the metric band, in the denser form this panel's grid needs.
 */
function Figure({
  metric,
  testId,
  suffix = "%",
  emphasis = "normal",
}: {
  metric: Metric;
  testId: string;
  suffix?: string;
  emphasis?: "strong" | "normal";
}) {
  const unavailable = metric.value === null;
  return (
    <div className="min-w-0 px-4 py-3" data-testid={testId} data-available={unavailable ? "false" : "true"}>
      <Tooltip>
        <TooltipTrigger asChild>
          <p className="flex cursor-help items-center gap-1 text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
            {metric.label}
            <Info aria-hidden="true" className="size-3 opacity-60" />
          </p>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs text-xs leading-relaxed">
          {metric.definition}
        </TooltipContent>
      </Tooltip>
      {unavailable ? (
        <div className="mt-1">
          <p className="flex items-center gap-1 text-sm font-medium text-muted-foreground">
            <CircleAlert aria-hidden="true" className="size-3.5 shrink-0 text-warning" />
            Not available
          </p>
          <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{metric.unavailable}</p>
        </div>
      ) : (
        <p
          className={cn(
            "mt-1 font-semibold",
            FIGURE,
            emphasis === "strong" ? "text-xl" : "text-base",
          )}
        >
          {metric.value}
          <span className="text-sm font-medium opacity-70">{suffix}</span>
        </p>
      )}
    </div>
  );
}

/** The coverage caveat, taken from the declared-unavailable list so there is one wording. */
function coverageNote(reading: RegimeReading): string | null {
  return reading.unavailable.find((item) => item.id === "breadth-coverage")?.reason ?? null;
}

const GAP_GLYPH = { above: "▲", below: "▼", "on-target": "●" } as const;
const GAP_WORD = { above: "above target", below: "below target", "on-target": "on target" } as const;

/**
 * The gap, with its direction as a word first and a colour last.
 *
 * "Above target" is the direction that matters — it is the one that means the book is carrying
 * more risk than the tier allows — so it is the one that gets the warning tone. It is deliberately
 * not red: nothing here has gone wrong, and the desk closes the gap on its next weekly plan.
 */
function GapReading({ gap }: { gap: ExposureGap }) {
  if (gap.direction === null) {
    return (
      <div className="px-4 py-3" data-testid="regime-gap" data-direction="unavailable">
        <p className="text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
          {gap.metric.label}
        </p>
        <p className="mt-1 flex items-center gap-1 text-sm font-medium text-muted-foreground">
          <CircleAlert aria-hidden="true" className="size-3.5 shrink-0 text-warning" />
          Not available
        </p>
        <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{gap.metric.unavailable}</p>
      </div>
    );
  }

  return (
    <div className="px-4 py-3" data-testid="regime-gap" data-direction={gap.direction}>
      <Tooltip>
        <TooltipTrigger asChild>
          <p className="flex cursor-help items-center gap-1 text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
            {gap.metric.label}
            <Info aria-hidden="true" className="size-3 opacity-60" />
          </p>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs text-xs leading-relaxed">
          {gap.metric.definition}
        </TooltipContent>
      </Tooltip>
      <p
        className={cn(
          "mt-1 flex flex-wrap items-baseline gap-x-1.5 font-semibold",
          FIGURE,
          gap.direction === "above" && "text-warning",
        )}
      >
        <span className="text-xl">
          <span aria-hidden="true" className="mr-0.5 text-[0.8em]">
            {GAP_GLYPH[gap.direction]}
          </span>
          {gap.magnitude}
          <span className="text-sm font-medium opacity-70">pp</span>
        </span>
        <span className="text-sm font-medium">{GAP_WORD[gap.direction]}</span>
      </p>
      <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{gap.sentence}</p>
    </div>
  );
}

/**
 * The exposure meter — the one drawing on this panel, and it draws only real numbers.
 *
 * A filled track for what is actually in shares and a marked line for the cap this tier allows.
 * It renders only when both figures exist; where either is missing, the two `Figure` cells above
 * have already said which one and why, and a bar with one end invented would be the exact defect
 * §1 of the plan forbids. `role="img"` with a spelled-out label, because a bar that only means
 * something visually means nothing to half the ways this screen is read.
 */
function ExposureMeter({ reading }: { reading: RegimeReading }) {
  const actual = reading.actualEquity.value;
  const target = reading.targetEquity.value;
  if (actual === null || target === null) return null;

  /* Clamped for the GEOMETRY only — a bar cannot be 105% wide. The figures above, the reading
     below and the accessible name all carry the unclamped values, so nothing a reader is given
     as a number has been altered to fit a box. */
  const actualPct = Math.max(0, Math.min(100, Number(actual)));
  const targetPct = Math.max(0, Math.min(100, Number(target)));
  const over = actualPct > targetPct;

  return (
    <div className="px-4 pb-3" data-testid="regime-meter">
      <div
        role="img"
        aria-label={`${actual}% of capital in shares against a cap of ${target}%. ${reading.gap.sentence}`}
        className="relative h-3 w-full overflow-hidden rounded-full border border-border bg-muted"
      >
        <div
          className={cn("h-full rounded-l-full", over ? "bg-warning" : "bg-brand-strong")}
          style={{ width: `${actualPct}%` }}
        />
        {/* The cap, as a hard line rather than a second fill: a cap is a boundary, not a quantity. */}
        <div
          aria-hidden="true"
          className="absolute inset-y-0 w-0.5 bg-foreground"
          style={{ left: `calc(${targetPct}% - 1px)` }}
        />
      </div>
      <p className="mt-1.5 text-xs text-muted-foreground">
        <span className="font-medium text-foreground tabular-nums">{actual}%</span> in shares ·
        cap at <span className="font-medium text-foreground tabular-nums">{target}%</span> ·{" "}
        {reading.gap.sentence}
      </p>
    </div>
  );
}

const SENTINEL_STATE = {
  "in-force": { word: "In force", Icon: ShieldAlert, className: "text-warning" },
  "reported-clear": { word: "Reported clear", Icon: CheckCircle2, className: "text-positive" },
  unconfirmed: { word: "Unconfirmed", Icon: CircleSlash, className: "text-muted-foreground" },
  "not-stated": { word: "Not stated", Icon: Info, className: "text-muted-foreground" },
} as const;

/**
 * One sentinel rule, and the desk's own sentences under it.
 *
 * Four states, and the fourth is the point: **not stated** is not **clear**. The response carries
 * the desk's rendered sentences and not its `reason_codes`, and a sentence about the sentinel is
 * only written when the sentinel changed something — so silence is genuinely ambiguous, and this
 * says so rather than resolving it in the reassuring direction.
 */
function SentinelRow({ finding }: { finding: SentinelFinding }) {
  const state = SENTINEL_STATE[finding.state];
  const { Icon } = state;
  return (
    <li className="flex gap-2 py-2.5" data-testid={`regime-sentinel-${finding.id}`} data-state={finding.state}>
      <Icon aria-hidden="true" className={cn("mt-0.5 size-4 shrink-0", state.className)} />
      <div className="min-w-0">
        <p className="text-sm font-medium">
          {finding.label}
          <span className="ml-2 text-xs font-normal text-muted-foreground">{state.word}</span>
        </p>
        <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{finding.summary}</p>
        {finding.evidence.length > 0 ? (
          <ul className="mt-1 space-y-0.5">
            {finding.evidence.map((sentence, index) => (
              <li
                key={`${index}-${sentence}`}
                className="text-xs italic leading-snug text-muted-foreground"
              >
                “{sentence}”
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </li>
  );
}

/** A disclosure, matching the health strip's: a chip that expands, never a dialog. */
function Disclosure({
  id,
  summary,
  count,
  children,
}: {
  id: string;
  summary: string;
  count?: number | undefined;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-t border-border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        data-testid={`regime-disclosure-${id}`}
        className="flex w-full items-center gap-1.5 px-4 py-2.5 text-left text-xs font-medium text-muted-foreground transition-colors duration-150 hover:text-foreground"
      >
        {summary}
        {count === undefined ? null : <span className="tabular-nums">· {count}</span>}
        <ChevronDown
          aria-hidden="true"
          className={cn("ml-auto size-3.5 transition-transform duration-150", open && "rotate-180")}
        />
      </button>
      {open ? <div className="px-4 pb-4">{children}</div> : null}
    </div>
  );
}

export interface RegimePanelProps {
  /** `GET /api/v1/desk/regime`. `null` when the call failed or the desk has evaluated nothing. */
  regime: RegimeOut | null;
  /** Today in exchange time, `YYYY-MM-DD`. `null` is honest and produces a stated caveat. */
  today: string | null;
  /** What went wrong, when `regime` is null. */
  unavailableReason?: string | null | undefined;
  className?: string | undefined;
}

export function RegimePanel({ regime, today, unavailableReason, className }: RegimePanelProps) {
  const state = regimeReading({ regime, today, unavailableReason: unavailableReason ?? null });

  if (state.kind === "unavailable") {
    const { notice } = state;
    return (
      <section
        aria-label="Momentum regime"
        data-testid="regime-panel"
        data-state="unavailable"
        className={cn("rounded-xl border border-border bg-card", className)}
      >
        <Header />
        <ul className="border-t border-border">
          <NoticeRow notice={notice} />
        </ul>
        <Footnote />
      </section>
    );
  }

  const r = state.reading;
  /* `findIndex` rather than `indexOf`, so an unrecognised tier needs no cast to be looked up. */
  const tierIndex = TIER_ORDER.findIndex((tier) => tier === r.applied.code);

  return (
    <section
      aria-label="Momentum regime"
      data-testid="regime-panel"
      data-state="ready"
      data-tier={r.applied.code}
      data-current={r.current ? "true" : "false"}
      className={cn("rounded-xl border border-border bg-card", className)}
    >
      <Header />

      {/* ---------------------------------------------------------------- the stance itself */}
      <div className="border-t border-border px-4 py-3.5">
        <p className="text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
          {r.standingHeadline}
        </p>
        <div className="mt-1 flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
          <span className={cn("text-2xl font-semibold", FIGURE)}>{r.applied.label}</span>
          <span className="rounded-md border border-border px-1.5 py-0.5 text-xs font-medium tabular-nums">
            {r.applied.code}
            {tierIndex >= 0 ? (
              <span className="font-normal text-muted-foreground">
                {" "}
                · {tierIndex + 1} of {TIER_ORDER.length}
              </span>
            ) : null}
          </span>
          <span
            className="rounded-md bg-muted px-1.5 py-0.5 text-xs font-medium text-muted-foreground"
            data-testid="regime-mode"
          >
            {r.mode.label}
          </span>
        </div>
        <p className="mt-1.5 text-sm font-medium leading-snug" data-testid="regime-movement">
          {r.movementSentence}
        </p>
        <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{r.applied.stance}</p>
        <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{r.mode.detail}</p>
      </div>

      {/* --------------------------------------------------- what may not be what is in force */}
      {r.notices.length > 0 ? (
        <ul className="divide-y divide-border/60 border-t border-border" data-testid="regime-notices">
          {r.notices.map((notice) => (
            <NoticeRow key={notice.id} notice={notice} />
          ))}
        </ul>
      ) : null}

      {/* ------------------------------------------------------ when, and from which session */}
      <div className="flex flex-wrap divide-x divide-border border-t border-border">
        <div className="min-w-[11rem] flex-1">
          <Figure metric={r.evaluated} testId="regime-evaluated" suffix="" />
        </div>
        <div className="min-w-[11rem] flex-1">
          <Figure metric={r.signalDate} testId="regime-signal-date" suffix="" />
        </div>
        <div className="min-w-[11rem] flex-1">
          <Figure metric={r.nextEvaluation} testId="regime-next-evaluation" suffix="" />
        </div>
      </div>

      {/* ------------------------------------------------------------------------- exposure */}
      <div className="border-t border-border">
        <div className="flex flex-wrap divide-x divide-border">
          <div className="min-w-[9.5rem] flex-1">
            <Figure metric={r.actualEquity} testId="regime-actual" emphasis="strong" />
          </div>
          <div className="min-w-[9.5rem] flex-1">
            <Figure metric={r.targetEquity} testId="regime-target" emphasis="strong" />
          </div>
          <div className="min-w-[13rem] flex-[1.4]">
            <GapReading gap={r.gap} />
          </div>
        </div>
        <ExposureMeter reading={r} />
      </div>

      {/* ---------------------------------------------------- new buys, breadth, the sentinel */}
      <div className="border-t border-border px-4 py-3.5">
        <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2">
          <div className="min-w-0 flex-1">
            <p className="text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
              New buys
            </p>
            <p className="mt-1 text-base font-semibold" data-testid="regime-new-buys">
              {r.newBuys.label}
            </p>
            <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{r.newBuys.detail}</p>
          </div>
          <div className="min-w-[13rem]">
            <Figure metric={r.breadth} testId="regime-breadth" />
            {/* Breadth is only usable when it covered enough of the universe, and the coverage is
                not in the response. Saying so HERE rather than only in the disclosure below is the
                difference between a caveat a reader meets and one they have to go looking for. */}
            {coverageNote(r) === null ? null : (
              <p className="px-4 pb-1 text-xs leading-snug text-muted-foreground">
                {coverageNote(r)}
              </p>
            )}
          </div>
        </div>

        <div className="mt-3 border-t border-border/60 pt-2">
          <p className="text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
            Momentum sentinel · {r.sentinel.indexName}
          </p>
          <ul className="divide-y divide-border/60">
            <SentinelRow finding={r.sentinel.veto} />
            <SentinelRow finding={r.sentinel.floor} />
          </ul>
        </div>
      </div>

      {/* ------------------------------------------------------------- the desk's own reasons */}
      <div className="border-t border-border px-4 py-3.5">
        <h3 className="text-sm font-semibold">Why the desk set it there</h3>
        <p className="mt-0.5 text-xs text-muted-foreground">
          The sentences below are the desk&rsquo;s own, written when it evaluated and quoted here
          unchanged.
        </p>
        {r.reasonsNote === null ? (
          <ul className="mt-2 space-y-1.5" data-testid="regime-reasons">
            {r.reasons.map((reason, index) => (
              <li key={`${index}-${reason}`} className="flex gap-2 text-sm leading-snug">
                <span aria-hidden="true" className="text-muted-foreground">
                  ·
                </span>
                <span>{reason}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-sm text-muted-foreground" data-testid="regime-reasons">
            {r.reasonsNote}
          </p>
        )}
      </div>

      {/* ------------------------------------------------------------------------ disclosures */}
      <Disclosure
        id="unavailable"
        summary="What this panel cannot show, and where each figure lives"
        count={r.unavailable.length}
      >
        <ul className="space-y-2.5" data-testid="regime-unavailable">
          {r.unavailable.map((item) => (
            <li key={item.id} data-testid={`regime-unavailable-${item.id}`}>
              <p className="flex items-start gap-1.5 text-sm font-medium leading-snug">
                <CircleAlert aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-warning" />
                {item.name}
              </p>
              <p className="mt-0.5 pl-5 text-xs leading-snug text-muted-foreground">{item.reason}</p>
              <p className="mt-0.5 pl-5 text-xs leading-snug text-muted-foreground">
                <span className="font-medium">Would need: </span>
                {item.unblockedBy}
              </p>
            </li>
          ))}
        </ul>
      </Disclosure>

      <Disclosure id="ladder" summary="The desk's configured exposure ladder">
        <table className="w-full text-xs" data-testid="regime-ladder">
          <caption className="sr-only">
            The desk&rsquo;s configured default equity cap for each tier
          </caption>
          <thead>
            <tr className="text-left text-muted-foreground">
              <th scope="col" className="py-1 font-medium">
                Tier
              </th>
              <th scope="col" className="py-1 font-medium">
                Stance
              </th>
              <th scope="col" className="py-1 text-right font-medium">
                Default cap
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/60">
            {r.ladder.map((rung) => (
              <tr key={rung.tier} data-tier={rung.tier}>
                <th scope="row" className="py-1 text-left font-medium tabular-nums">
                  {rung.tier}
                </th>
                <td className="py-1 text-muted-foreground">{rung.label}</td>
                <td className={cn("py-1 text-right", FIGURE)}>
                  {rung.equityPct}%
                  {rung.fromEnv ? (
                    <span className="ml-1 font-normal text-muted-foreground">
                      ({rung.fromEnv})
                    </span>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mt-2 text-xs leading-snug text-muted-foreground">{r.ladderCaveat}</p>
      </Disclosure>

      <Footnote />
    </section>
  );
}

function Header() {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 px-4 py-2.5">
      <h2 className="text-sm font-semibold">Momentum regime</h2>
      <p className="text-xs text-muted-foreground">
        The desk&rsquo;s weekly stance on how much risk the strategy is carrying
      </p>
    </div>
  );
}

/**
 * The standing sentence, and it is not decoration.
 *
 * This panel reports a decision taken for a different book on a schedule the reader does not
 * control. Saying so is what keeps a tier from reading as a suggestion — and Baskfy is not
 * registered to make one (D3, `docs/PORTFOLIO-COMMAND-CENTER.md` §6.2 rule 4).
 */
function Footnote() {
  return (
    <p className="border-t border-border px-4 py-2.5 text-xs leading-snug text-muted-foreground">
      A record of what the desk decided for its own portfolio, not a recommendation about yours.
      The full evaluation, with every reason it recorded, is on{" "}
      <a href={REGIME_PAGE} className="font-medium text-brand-strong underline-offset-4 hover:underline">
        the stance page
      </a>
      .
    </p>
  );
}
