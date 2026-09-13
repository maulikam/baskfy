"use client";

import type {
  DataQualityOut,
  DeskBlockOut,
  DeskComponentOut,
  DeskInputOut,
  RankExplanationOut,
  RankPointOut,
  RankProvenanceOut,
  ScreenDefinition,
} from "@baskfy/api-client";

import { ErrorState } from "@/components/data/error-state";
import {
  DeskScoreBreakdown,
  type DeskExplainKey,
  isDeskExplainKey,
} from "@/components/screens/desk-score-breakdown";
import { EMPTY_CELL, formatNumber, formatTradeDate } from "@/lib/format";
import { useRankExplanation, useRankHistory } from "@/lib/screens/queries";
import { cn } from "@/lib/utils";

/**
 * Why one stock ranks where it does — `POST /screens/explain` (docs/ranking/PLAN.md C6,
 * gates/ranking-2.H-web.md G4).
 *
 * Everything shown comes from the response: core's `RankExplanation` plus `as_of` and
 * `data_version`. "Previous" is the response's `rank_history` — the same definition ranked on the
 * session before, from that day's data. Only when the API has no previous rank (the stock was not
 * ranked that session) does it fall back to the saved runs (`/instruments/{symbol}/rank-history`,
 * from `screen_run`), and it says which one it shows. Nothing is guessed.
 */

const MODE_LABEL: Record<RankProvenanceOut["mode"], string> = {
  single: "Single",
  sequential: "Sequential",
  composite: "Composite",
};

const SCOPE_LABEL: Record<RankProvenanceOut["scope"], string> = {
  fixed_universe: "Fixed universe",
  filtered_results: "Filtered results",
  within_sector: "Within sector",
};

/** Four significant figures: raw values run from 0.0012 to 12,500 and share one column. */
function rawValue(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return EMPTY_CELL;
  return formatNumber(Number(value.toPrecision(4)));
}

function fixed(value: number | null, decimals: number): string {
  if (value === null || !Number.isFinite(value)) return EMPTY_CELL;
  return formatNumber(value, { decimals });
}

/** The last saved run strictly before the explanation's date — never the run being explained. */
export function previousRank(
  asOf: string,
  history: readonly RankPointOut[] | undefined,
): RankPointOut | null {
  if (!history) return null;
  const earlier = history.filter((point) => point.as_of < asOf);
  return earlier.length > 0 ? (earlier[earlier.length - 1] ?? null) : null;
}

export interface PreviousRank {
  rank: number;
  as_of: string;
  /** `session`: the API's previous-session run. `saved`: the last saved run of this screen. */
  source: "session" | "saved";
}

/** The API's previous-session rank; the saved runs only when the API has none. */
export function resolvePreviousRank(
  explanation: RankExplanationOut,
  history: readonly RankPointOut[] | undefined,
): PreviousRank | null {
  const { previous, previous_as_of } = explanation.rank_history;
  if (previous !== null && previous_as_of !== null) {
    return { rank: previous, as_of: previous_as_of, source: "session" };
  }
  const saved = previousRank(explanation.as_of, history);
  return saved ? { rank: saved.rank, as_of: saved.as_of, source: "saved" } : null;
}

/** Rank 1 is best, so a smaller number today is a move up. */
export function rankChange(today: number | null, previous: number | null): string {
  if (today === null || previous === null) return EMPTY_CELL;
  const delta = previous - today;
  if (delta === 0) return "No change";
  const places = Math.abs(delta) === 1 ? "place" : "places";
  return delta > 0 ? `Up ${Math.abs(delta)} ${places}` : `Down ${Math.abs(delta)} ${places}`;
}

function signed(value: number): string {
  return formatNumber(Math.abs(value), { decimals: 1 });
}

/** The stored raw input in words: `ext_over_20dma` is a percent above (or below) the 20-day average. */
export function deskInputText(input: DeskInputOut): string {
  if (input.name === "ext_over_20dma") {
    if (input.value === null || !Number.isFinite(input.value)) {
      return "Distance from the 20-day average not recorded";
    }
    if (input.value === 0) return "At its 20-day average";
    return `${signed(input.value)}% ${input.value > 0 ? "above" : "below"} its 20-day average`;
  }
  return `${input.label} ${input.value === null ? EMPTY_CELL : rawValue(input.value)}`;
}

/** One line under a grade: its range, then each stored input behind it. */
export function deskComponentDetail(component: DeskComponentOut): string {
  const range =
    component.max_points > 0
      ? `Out of ${formatNumber(component.max_points)}`
      : `Down to ${formatNumber(component.min_points)}`;
  return [range, ...component.inputs.map(deskInputText)].join(" · ");
}

function deskDetails(desk: DeskBlockOut): Partial<Record<DeskExplainKey, string>> {
  const details: Partial<Record<DeskExplainKey, string>> = {};
  for (const component of desk.components) {
    const key = `desk_${component.key}`;
    if (isDeskExplainKey(key)) details[key] = deskComponentDetail(component);
  }
  return details;
}

function previousNote(
  previous: PreviousRank | null,
  explanation: RankExplanationOut,
  history: readonly RankPointOut[] | undefined,
  historyPending: boolean,
): string {
  if (previous?.source === "session") {
    return `Previous is this screen's rank on the session before, ${formatTradeDate(previous.as_of)}.`;
  }
  if (historyPending) return "Loading saved runs…";
  const sessionDate = explanation.rank_history.previous_as_of;
  const notRanked = sessionDate
    ? `Not ranked in this screen on ${formatTradeDate(sessionDate)}. `
    : "";
  if (previous) {
    return `${notRanked}Previous is the last saved run, on ${formatTradeDate(previous.as_of)}.`;
  }
  if (history === undefined) return `${notRanked}Save this screen to keep a rank history.`.trim();
  return `${notRanked}No earlier saved run to compare with.`.trim();
}

function dataQualityNotes(quality: DataQualityOut): string[] {
  const notes: string[] = [];
  if (quality.missing_factors.length > 0) {
    notes.push(`No value for ${quality.missing_factors.join(", ")}`);
  }
  if (quality.insufficient_history) notes.push("Too little price history for some measures");
  if (quality.stale_price) notes.push("The last price is out of date");
  if (quality.recent_corporate_action) {
    notes.push("A recent split, bonus or similar event changed past prices");
  }
  return notes;
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <p className="text-xs font-medium text-muted-foreground">{children}</p>;
}

function NoteList({ items, testId, tone }: { items: readonly string[]; testId: string; tone?: "negative" }) {
  return (
    <ul data-testid={testId} className="space-y-1">
      {items.map((item) => (
        <li key={item} className={cn("text-sm", tone === "negative" && "text-negative")}>
          {item}
        </li>
      ))}
    </ul>
  );
}

export interface RankExplanationViewProps {
  explanation: RankExplanationOut;
  /** Saved runs of this screen for the stock; `undefined` when the screen has never been saved. */
  history?: readonly RankPointOut[] | undefined;
  /** The saved runs are still loading, so "no earlier run" would be premature. */
  historyPending?: boolean | undefined;
}

export function RankExplanationView({
  explanation,
  history,
  historyPending = false,
}: RankExplanationViewProps) {
  const { terms, eligibility, desk, provenance } = explanation;
  const quality = dataQualityNotes(explanation.data_quality);
  const previous = resolvePreviousRank(explanation, history);

  return (
    <div data-testid="rank-explanation" className="space-y-4">
      <div className="vaaya-stat flex items-end justify-between gap-3 p-4" data-testid="explain-total">
        <div>
          <SectionTitle>Rank in this screen</SectionTitle>
          <p className="text-2xl font-semibold tabular-nums" data-testid="explain-rank">
            {explanation.rank === null ? "Not in the results" : `#${explanation.rank}`}
          </p>
        </div>
        <div className="text-right">
          <SectionTitle>Total score</SectionTitle>
          <p className="text-2xl font-semibold tabular-nums" data-testid="explain-total-value">
            {fixed(explanation.total, 1)}
          </p>
        </div>
      </div>

      {terms.length > 0 ? (
        <div className="space-y-2">
          <SectionTitle>How the score adds up</SectionTitle>
          <div className="overflow-x-auto">
            <table data-testid="explain-terms" className="w-full text-xs tabular-nums">
              <thead>
                <tr className="text-muted-foreground">
                  <th scope="col" className="py-1 pr-2 text-left font-medium">Measure</th>
                  <th scope="col" className="px-1 py-1 text-right font-medium">Raw value</th>
                  <th scope="col" className="px-1 py-1 text-right font-medium">Score</th>
                  <th scope="col" className="px-1 py-1 text-right font-medium">Weight</th>
                  <th scope="col" className="py-1 pl-1 text-right font-medium">Contribution</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {terms.map((term) => (
                  <tr key={term.factor} data-testid={`explain-term-${term.factor}`}>
                    <th scope="row" className="py-1.5 pr-2 text-left font-normal">
                      {term.label}
                    </th>
                    <td className={cn("px-1 py-1.5 text-right", term.missing && "text-negative")}>
                      {term.missing ? "No data" : rawValue(term.raw)}
                    </td>
                    <td className="px-1 py-1.5 text-right">{fixed(term.transformed, 2)}</td>
                    <td className="px-1 py-1.5 text-right">{fixed(term.effective_weight, 2)}</td>
                    <td className="py-1.5 pl-1 text-right">{fixed(term.contribution, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {explanation.positives.length > 0 ? (
        <div className="space-y-1">
          <SectionTitle>What helps</SectionTitle>
          <NoteList items={explanation.positives} testId="explain-positives" />
        </div>
      ) : null}

      {explanation.deductions.length > 0 ? (
        <div className="space-y-1">
          <SectionTitle>What holds it back</SectionTitle>
          <NoteList items={explanation.deductions} testId="explain-deductions" />
        </div>
      ) : null}

      <div className="space-y-1" data-testid="explain-eligibility">
        <SectionTitle>Filters</SectionTitle>
        {eligibility.passed ? (
          <p className="text-sm">Passes every filter.</p>
        ) : (
          <>
            <p className="text-sm text-negative">Left out of the results.</p>
            {eligibility.failures.length > 0 ? (
              <NoteList
                items={eligibility.failures.map((failure) => failure.detail || failure.filter)}
                testId="explain-eligibility-failures"
                tone="negative"
              />
            ) : null}
          </>
        )}
      </div>

      <div className="space-y-1" data-testid="explain-data-quality">
        <SectionTitle>Data checks</SectionTitle>
        {quality.length > 0 ? (
          <NoteList items={quality} testId="explain-data-quality-notes" tone="negative" />
        ) : (
          <p className="text-sm">No data problems found.</p>
        )}
      </div>

      {desk ? (
        <div className="space-y-2" data-testid="explain-desk">
          <div className="flex items-baseline justify-between gap-2 text-xs tabular-nums">
            <span className="text-muted-foreground">Desk score</span>
            <span data-testid="explain-desk-score">
              {fixed(desk.score, 1)}
              {desk.rank === null ? "" : ` · desk rank #${desk.rank}`}
            </span>
          </div>
          <DeskScoreBreakdown
            row={{
              desk_a_trend: desk.a_trend,
              desk_b_momentum: desk.b_momentum,
              desk_c_sharpe: desk.c_sharpe,
              desk_d_consistency: desk.d_consistency,
              desk_e_liquidity: desk.e_liquidity,
              desk_f_penalty: desk.f_penalty,
              desk_reject: desk.reject.split(";").filter(Boolean).join(", "),
              desk_eligible: desk.eligible,
            }}
            details={deskDetails(desk)}
          />
        </div>
      ) : null}

      <div className="space-y-1" data-testid="explain-rank-history">
        <SectionTitle>Rank over time</SectionTitle>
        <dl className="grid grid-cols-3 gap-2 text-center">
          <div className="rounded-md bg-muted/60 px-2 py-1.5">
            <dt className="text-xs text-muted-foreground">Today</dt>
            <dd className="text-sm font-medium tabular-nums" data-testid="explain-rank-today">
              {explanation.rank === null ? EMPTY_CELL : `#${explanation.rank}`}
            </dd>
          </div>
          <div className="rounded-md bg-muted/60 px-2 py-1.5">
            <dt className="text-xs text-muted-foreground">Previous</dt>
            <dd
              className="text-sm font-medium tabular-nums"
              data-testid="explain-rank-previous"
              title={previous ? formatTradeDate(previous.as_of) : undefined}
            >
              {previous ? `#${previous.rank}` : EMPTY_CELL}
            </dd>
          </div>
          <div className="rounded-md bg-muted/60 px-2 py-1.5">
            <dt className="text-xs text-muted-foreground">Change</dt>
            <dd className="text-sm font-medium tabular-nums" data-testid="explain-rank-change">
              {rankChange(explanation.rank, previous?.rank ?? null)}
            </dd>
          </div>
        </dl>
        <p className="text-xs text-muted-foreground" data-testid="explain-rank-previous-note">
          {previousNote(previous, explanation, history, historyPending)}
        </p>
      </div>

      <div className="space-y-1" data-testid="explain-provenance">
        <SectionTitle>Where these numbers come from</SectionTitle>
        <dl className="divide-y divide-border">
          {(
            [
              ["Stocks ranked", provenance.universe],
              ["Prices as of", formatTradeDate(provenance.as_of)],
              ["Data update", provenance.data_version === null ? EMPTY_CELL : `#${provenance.data_version}`],
              ["Ranking mode", MODE_LABEL[provenance.mode]],
              ["Compared against", SCOPE_LABEL[provenance.scope]],
              ["Ranking method", provenance.ranking_engine_version],
              ["Desk score method", provenance.desk_score_version ?? EMPTY_CELL],
              ["NSE momentum method", provenance.nse_momentum_version],
            ] as const
          ).map(([label, value]) => (
            <div key={label} className="flex items-baseline justify-between gap-4 py-1.5">
              <dt className="text-xs text-muted-foreground">{label}</dt>
              <dd className="text-xs tabular-nums">{value}</dd>
            </div>
          ))}
        </dl>
      </div>
    </div>
  );
}

export interface RankExplanationProps {
  definition: ScreenDefinition;
  symbol: string;
  asOf?: string | undefined;
  dataVersion?: number | undefined;
  /** Saved screen id — enables "previous" from saved runs. Absent on unsaved previews. */
  screenPublicId?: string | undefined;
}

/**
 * The peek drawer's ranked-screen section: fetches the explanation, and the saved rank history
 * only when the explanation has no previous-session rank to show.
 */
export function RankExplanation({
  definition,
  symbol,
  asOf,
  dataVersion,
  screenPublicId,
}: RankExplanationProps) {
  const explanation = useRankExplanation({ definition, symbol, asOf, dataVersion });
  const needsSavedRuns = explanation.data?.rank_history.previous === null;
  const savedScreen = needsSavedRuns ? screenPublicId : undefined;
  const history = useRankHistory(symbol, savedScreen);

  if (explanation.error) {
    return <ErrorState error={explanation.error} onRetry={() => void explanation.refetch()} />;
  }
  if (!explanation.data) {
    return (
      <p className="text-xs text-muted-foreground" data-testid="rank-explanation-loading">
        Loading why this stock ranks here…
      </p>
    );
  }
  return (
    <RankExplanationView
      explanation={explanation.data}
      history={savedScreen ? (history.data?.data ?? []) : undefined}
      historyPending={Boolean(savedScreen) && history.isPending}
    />
  );
}
