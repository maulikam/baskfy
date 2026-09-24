"use client";

import { ExternalLink } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState, useTransition, type ReactNode } from "react";

import { InstrumentLink } from "@/components/instrument/instrument-link";
import { LiveMarksProvider, LivePrice, LiveStatus } from "@/components/screens/live-price";
import { formatTradeDate } from "@/lib/format";
import {
  OVERLAP_EVENT_TYPES,
  type CorrectTagAction,
  type OverlapCandidate,
  type OverlapCandidates,
  type OverlapEventType,
  type OverlapScope,
  type OverlapStrategy,
  type OverlapTag,
} from "@/lib/overlap/candidates";
import { cn } from "@/lib/utils";

/**
 * Today's candidates, one row per name, with what each strategy said about it.
 *
 * **Every cell is a stored fact restated.** The strategy chips are the detail the sleeve's own
 * page shows (`EP · GAP_DAY`, `signal`, `tight 4 sessions`); the count is how many chips there
 * are; the price is the exchange print with the shared live overlay; the earnings date and the
 * catalyst link are the swing feed's (SW11B, A3), on loan, for the names it covers. There is no
 * combined score and no rank across strategies — the rows are ordered by count, then by whether
 * a strategy could act, then by symbol, and the header says so.
 *
 * **The link out is the filing, never the text.** A catalyst is a headline and the exchange's
 * own URL, opened in a new tab. Nothing is reproduced. The small tag beside it is a reading of
 * the *headline* (`baskfy_core.catalyst_tags` — an order, a result, a routine notice) by fixed
 * rules or by Laya, labelled with its source and never read by any rank or order path; whether
 * the filing explains the move is still the reader's judgement, on the exchange's page.
 *
 * **The one thing a person can change here is that tag.** The select on the chip records their
 * word on the headline (`correctTag`, a server action): it wins on the page, and the corrections
 * are the set the model is fine-tuned on. A correction is a label on display context — it
 * reaches one table and no rank, size or order.
 *
 * **Nothing here can place an order**, queue a scan, or change a setting. The table reads one
 * GET and renders it; the sleeve pages it links to are where a person acts.
 */
export function CandidatesTable({
  candidates,
  scope,
  correctTag,
}: {
  candidates: OverlapCandidates | null;
  scope: OverlapScope;
  /** Absent in a render that cannot write (a test, a preview): the chip then has no select. */
  correctTag?: CorrectTagAction;
}) {
  const rows = candidates?.data ?? [];
  const sessions = candidates?.sessions ?? null;
  const latest = sessions
    ? [sessions.swing, sessions.volume_breakout, sessions.three_weeks_tight]
        .filter((value): value is string => Boolean(value))
        .sort()
        .at(-1) ?? null
    : null;

  return (
    <section
      className="space-y-3 rounded-xl border border-border/70 bg-card/40 p-5"
      data-testid="overlap-candidates"
      aria-labelledby="overlap-candidates-heading"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h2
            id="overlap-candidates-heading"
            className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
          >
            Today&rsquo;s candidates
          </h2>
          <p className="max-w-[72ch] text-sm leading-relaxed text-muted-foreground">
            One row per name, with each strategy&rsquo;s own word on it, the screens it is on, and
            the exchange filing the swing feed linked. Ordered by how many strategies raised the
            name — a count, not a score.
          </p>
        </div>
        <nav aria-label="Which rows to show" className="flex gap-2">
          <ScopeChip current={scope} value="actionable">
            A strategy could act
          </ScopeChip>
          <ScopeChip current={scope} value="all">
            Every row the scans wrote
          </ScopeChip>
        </nav>
      </div>

      {sessions ? (
        <p className="text-xs text-muted-foreground" data-testid="overlap-candidates-sessions">
          Swing {formatTradeDate(sessions.swing)} · Volume breakout{" "}
          {formatTradeDate(sessions.volume_breakout)} · Three weeks tight{" "}
          {formatTradeDate(sessions.three_weeks_tight)}
          {new Set(Object.values(sessions).filter(Boolean)).size > 1
            ? " — the strategies are not on the same session; each chip names its own."
            : null}
        </p>
      ) : null}

      {candidates === null ? (
        <p className="text-sm text-muted-foreground" data-testid="overlap-candidates-unread">
          The candidates could not be read just now. The symbol matrix below is still computed
          from each hub&rsquo;s own page.
        </p>
      ) : !candidates.strategies_read ? (
        <p className="text-sm text-muted-foreground" data-testid="overlap-candidates-not-yours">
          The strategy scans belong to the account that runs them, and this is not that account —
          so there are no strategy rows to show here.
        </p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-muted-foreground" data-testid="overlap-candidates-empty">
          {scope === "actionable"
            ? "No name on any strategy's latest session is one its plan builder could take. Switch to every row to see what the scans wrote."
            : "The strategies have not written a session yet, so there is nothing to list."}
        </p>
      ) : (
        <LiveMarksProvider symbols={rows.map((row) => row.symbol)}>
          <LiveStatus asOf={latest} />
          <p className="text-xs text-muted-foreground" data-testid="overlap-candidates-tag-note">
            The small tag under a filing is read from its headline — by Laya, with its
            confidence, when the model is sure, and by fixed rules otherwise; a &ldquo;?&rdquo; means
            the two disagreed. It is the subject the exchange named, nothing more: context for
            which filing to open first, not used in any rank, size or order. If it is wrong,
            correct it: your word wins on the page and is what the model is trained on next.
          </p>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[56rem] text-sm" data-testid="overlap-candidates-table">
              <thead>
                <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <th className="py-2 pr-3 font-medium">Name</th>
                  <th className="px-2 py-2 text-center font-medium">On</th>
                  <th className="px-2 py-2 font-medium">Strategies</th>
                  <th className="px-2 py-2 text-right font-medium">Price</th>
                  <th className="px-2 py-2 font-medium">Results</th>
                  <th className="px-2 py-2 font-medium">Filing</th>
                  <th className="py-2 pl-2 font-medium">Screens</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <CandidateRow key={row.instrument_id} row={row} correctTag={correctTag} />
                ))}
              </tbody>
            </table>
          </div>
        </LiveMarksProvider>
      )}
    </section>
  );
}

function CandidateRow({
  row,
  correctTag,
}: {
  row: OverlapCandidate;
  correctTag: CorrectTagAction | undefined;
}) {
  return (
    <tr
      className={cn("border-b border-border/40", !row.actionable && "text-muted-foreground")}
      data-testid="overlap-candidate-row"
      data-symbol={row.symbol}
      data-actionable={row.actionable ? "true" : "false"}
    >
      <td className="py-2 pr-3 align-top">
        <InstrumentLink symbol={row.symbol} className="font-medium">
          {row.symbol}
        </InstrumentLink>
        <span className="block max-w-[14rem] truncate text-xs text-muted-foreground">{row.name}</span>
      </td>
      <td className="px-2 py-2 text-center align-top tabular-nums" data-testid="overlap-candidate-count">
        {row.strategy_count}
      </td>
      <td className="px-2 py-2 align-top">
        <ul className="flex flex-wrap gap-1.5">
          {row.strategies.map((hit) => (
            <li key={hit.strategy}>
              <StrategyChip hit={hit} />
            </li>
          ))}
        </ul>
      </td>
      <td className="px-2 py-2 text-right align-top">
        <LivePrice symbol={row.symbol} close={row.close} />
      </td>
      <td className="px-2 py-2 align-top">
        {row.catalyst?.earnings_date ? (
          <span
            className="rounded-md border border-border/70 px-1.5 py-0.5 text-xs"
            data-testid="overlap-candidate-earnings"
            title="Result meeting, from the exchange's event calendar"
          >
            {formatTradeDate(row.catalyst.earnings_date)}
          </span>
        ) : (
          <span className="text-muted-foreground/70">—</span>
        )}
      </td>
      <td className="max-w-[22rem] px-2 py-2 align-top">
        {row.catalyst?.url ? (
          <a
            href={row.catalyst.url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex max-w-full items-start gap-1 text-primary hover:underline"
            data-testid="overlap-candidate-filing"
            title={row.catalyst.headline ?? undefined}
          >
            <span className="truncate">{row.catalyst.headline ?? "Filing"}</span>
            <ExternalLink aria-hidden="true" className="mt-0.5 size-3 shrink-0" />
          </a>
        ) : (
          <span className="text-muted-foreground/70">—</span>
        )}
        {row.catalyst?.published_at || row.catalyst?.tag ? (
          <span className="mt-0.5 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            {row.catalyst?.tag ? (
              <TagChip
                tag={row.catalyst.tag}
                headline={row.catalyst.headline}
                correctTag={correctTag}
              />
            ) : null}
            {row.catalyst?.published_at ? (
              <span>{formatTradeDate(row.catalyst.published_at.slice(0, 10))}</span>
            ) : null}
          </span>
        ) : null}
      </td>
      <td className="py-2 pl-2 align-top text-xs">
        {row.screens.length === 0 ? (
          <span className="text-muted-foreground/70">—</span>
        ) : (
          <ul className="space-y-0.5">
            {row.screens.map((hit) => (
              <li key={hit.public_id} data-testid="overlap-candidate-screen">
                <Link
                  href={`/build/${hit.public_id}` as Route}
                  className="hover:underline"
                  title={hit.definition_changed ? "The screen was edited after this run" : undefined}
                >
                  {hit.name}
                </Link>
                {hit.rank !== null && hit.of !== null ? (
                  <span className="text-muted-foreground">
                    {" "}
                    #{hit.rank} of {hit.of}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </td>
    </tr>
  );
}

/** The reader's word for each event type — the wire keeps the snake_case, the page does not. */
const EVENT_WORDS: Record<OverlapTag["event_type"], string> = {
  earnings: "Results",
  order: "Order win",
  approval: "Approval",
  fundraising: "Fund raise",
  governance: "Governance",
  corporate_action: "Corporate action",
  routine: "Routine notice",
  other: "Unclear",
};

/** The `<option>` value that takes a correction back — never one of the eight words. */
const CLEAR_CORRECTION = "__clear";

function TagChip({
  tag,
  headline,
  correctTag,
}: {
  tag: OverlapTag;
  headline: string | null;
  correctTag: CorrectTagAction | undefined;
}) {
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const why =
    tag.corrected
      ? "you corrected it"
      : tag.source === "laya"
        ? `Laya read the headline at ${Math.round((tag.confidence ?? 0) * 100)}% confidence`
        : tag.matched.length > 0
          ? `matched: ${tag.matched.join(", ")}`
          : "no subject in the headline";
  const [otherSource, otherType] = tag.disagrees_with?.split(":") ?? [];
  const disagreement =
    otherSource && otherType
      ? tag.corrected
        ? ` It overrules ${otherSource}, which said ${otherType}.`
        : ` The other reader (${otherSource}) said ${otherType}.`
      : "";
  const by = tag.corrected ? "Corrected by you" : `Read from the headline by ${tag.source}`;

  const onChange = (value: string) => {
    if (!correctTag || !headline) return;
    const next: OverlapEventType | null =
      value === CLEAR_CORRECTION
        ? null
        : (OVERLAP_EVENT_TYPES.find((eventType) => eventType === value) ?? null);
    if (next === null && value !== CLEAR_CORRECTION) return;
    setError(null);
    startTransition(async () => {
      const result = await correctTag(headline, next);
      if (!result.ok) setError(result.error);
    });
  };

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5",
        tag.review_priority === "high"
          ? "border-primary/40 text-foreground"
          : tag.review_priority === "medium"
            ? "border-border text-foreground/80"
            : "border-border/60 text-muted-foreground",
        tag.corrected && "border-dashed",
      )}
      data-testid="overlap-candidate-tag"
      data-event={tag.event_type}
      data-priority={tag.review_priority}
      data-source={tag.source}
      data-confidence={tag.confidence ?? undefined}
      data-disagrees-with={tag.disagrees_with ?? undefined}
      data-corrected={tag.corrected ? "true" : undefined}
      title={`${by} (${why}).${disagreement} Context only — not used in any rank, size or order.`}
    >
      <span
        aria-hidden="true"
        className={cn(
          "inline-block size-1.5 rounded-full",
          tag.review_priority === "high"
            ? "bg-primary"
            : tag.review_priority === "medium"
              ? "bg-foreground/50"
              : "bg-muted-foreground/40",
        )}
      />
      {EVENT_WORDS[tag.event_type]}
      {tag.corrected ? (
        <span className="text-muted-foreground" data-testid="overlap-candidate-tag-source">
          corrected
        </span>
      ) : tag.source === "laya" && tag.confidence !== null ? (
        <span className="text-muted-foreground" data-testid="overlap-candidate-tag-confidence">
          {Math.round(tag.confidence * 100)}%
        </span>
      ) : null}
      {tag.disagrees_with ? (
        <span aria-label="the two readers disagree" className="text-muted-foreground" title={disagreement.trim()}>
          ?
        </span>
      ) : null}
      {correctTag && headline ? (
        <>
          <select
            aria-label={`Correct the tag on “${headline}”`}
            className="ml-0.5 max-w-[9rem] rounded border border-border/70 bg-background px-1 py-0 text-xs text-foreground"
            data-testid="overlap-candidate-tag-correct"
            disabled={pending}
            value={tag.corrected ? tag.event_type : ""}
            onChange={(event) => onChange(event.target.value)}
          >
            <option value="" disabled>
              Correct…
            </option>
            {OVERLAP_EVENT_TYPES.map((eventType) => (
              <option key={eventType} value={eventType}>
                {EVENT_WORDS[eventType]}
              </option>
            ))}
            <option value={CLEAR_CORRECTION} disabled={!tag.corrected}>
              Clear correction
            </option>
          </select>
          {error ? (
            <span role="alert" className="text-destructive" data-testid="overlap-candidate-tag-error">
              {error}
            </span>
          ) : null}
        </>
      ) : null}
    </span>
  );
}

function StrategyChip({ hit }: { hit: OverlapStrategy }) {
  return (
    <Link
      href={hit.ref as Route}
      className={cn(
        "inline-flex items-baseline gap-1 rounded-md border px-1.5 py-0.5 text-xs",
        hit.actionable
          ? "border-primary/40 bg-primary/5 text-foreground"
          : "border-border/70 text-muted-foreground",
      )}
      data-testid="overlap-candidate-strategy"
      data-strategy={hit.strategy}
      data-actionable={hit.actionable ? "true" : "false"}
      title={`${hit.name} · ${formatTradeDate(hit.as_of)}`}
    >
      <span className="font-medium">{hit.name}</span>
      <span>· {hit.detail}</span>
    </Link>
  );
}

function ScopeChip({
  current,
  value,
  children,
}: {
  current: OverlapScope;
  value: OverlapScope;
  children: ReactNode;
}) {
  const active = current === value;
  /* Keep the picked screens (`?screen=`) while switching scope: the two are independent. */
  const params = new URLSearchParams(useSearchParams().toString());
  if (value === "all") params.set("scope", "all");
  else params.delete("scope");
  const query = params.toString();
  return (
    <Link
      href={query ? `/build/overlap?${query}` : "/build/overlap"}
      scroll={false}
      aria-current={active ? "true" : undefined}
      className={cn(
        "rounded-full border px-3 py-1 text-xs",
        active ? "border-foreground bg-foreground text-background" : "border-border/70",
      )}
      data-testid={`overlap-candidates-scope-${value}`}
    >
      {children}
    </Link>
  );
}
