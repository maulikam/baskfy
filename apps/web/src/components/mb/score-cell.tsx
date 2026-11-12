"use client";

import { createContext, useContext, type ReactNode } from "react";

import { FlagTip } from "@/components/mb/flag-tip";
import { MB_FLAGS, SWING_SCOPE, flagsOf } from "@/lib/mb/flags";
import {
  EMPTY_MB_SCORES,
  type MbScores,
  useMbScores,
} from "@/lib/mb/scores";

const MbScoresContext = createContext<MbScores>(EMPTY_MB_SCORES);
const SYMBOL = Object.fromEntries(MB_FLAGS.map((f) => [f.key, f]));

/** Wraps a Build table: one lookup for every symbol in it. */
export function MbScoresProvider({
  symbols,
  children,
}: {
  symbols: readonly string[];
  children: ReactNode;
}) {
  const scores = useMbScores(symbols);
  return (
    <MbScoresContext.Provider value={scores}>
      {children}
    </MbScoresContext.Provider>
  );
}

/** The same context from a value the caller already fetched (a table that also sorts by the scores). */
export function MbScoresValueProvider({ value, children }: { value: MbScores; children: ReactNode }) {
  return <MbScoresContext.Provider value={value}>{children}</MbScoresContext.Provider>;
}

export function useMbScoresContext(): MbScores {
  return useContext(MbScoresContext);
}

/** PM11's column header: the swing-picks model, linked to `/mb/swing`. */
export function SwingScoreHeadLink() {
  return (
    <a
      href="/mb/swing"
      className="underline decoration-dotted underline-offset-2"
      title="The swing-picks model's score: higher is likelier to make +14% under a −7% stop trailing 12%, within 60 sessions. ★ = one of the session's five picks (only while the market switch is on)."
    >
      Swing
    </a>
  );
}

/** The lookup failed after its retries: say so, never an endless "·". */
function ScoresUnavailable({ model, error, testId }: { model: string; error: string; testId: string }) {
  return (
    <span data-testid={testId}>
      <FlagTip
        label={`${model} score unavailable`}
        meaning={`The score lookup failed (${error}). Reload the page to try again.`}
        className="text-amber-600 dark:text-amber-400"
        testId={`${testId}-unavailable`}
      >
        ?
      </FlagTip>
    </span>
  );
}

/** The swing-picks model's score for one stock (PM11). Information only: it never filters the row. */
export function SwingScoreValue({ symbol }: { symbol: string }) {
  const { swing, loaded, error } = useMbScoresContext();
  if (!loaded) return <span className="text-muted-foreground">·</span>;
  if (error) return <ScoresUnavailable model="Swing" error={error} testId="swing-score-cell" />;
  const v = swing.bySymbol[symbol.trim().toUpperCase()];
  if (!v)
    return (
      <span data-testid="swing-score-cell">
        <FlagTip
          label="Not scored"
          meaning="The swing model has no price row for this stock tonight: not an ordinary equity share (an ETF, bond or other instrument), or no trade today."
          className="text-slate-400"
        >
          ⊘
        </FlagTip>
      </span>
    );
  const out = v.scope && v.scope !== "universe" ? SWING_SCOPE[v.scope] : null;
  if (out)
    return (
      <span className="whitespace-nowrap tabular-nums" data-testid="swing-score-cell">
        <FlagTip
          label={`Swing score ${v.score} · rank ${v.rank}${swing.universeN ? ` of ${swing.universeN}` : ""}`}
          meaning={`Outside the model's training range: ${out.label.toLowerCase()}. Read it as a rough guide only.`}
          className="text-muted-foreground"
        >
          {Number(v.score).toFixed(3)}
        </FlagTip>
        <FlagTip label={out.label} meaning={out.meaning} className="ml-1 text-amber-600 dark:text-amber-400">
          ◌
        </FlagTip>
      </span>
    );
  return (
    <span className="whitespace-nowrap tabular-nums" data-testid="swing-score-cell">
      <FlagTip
        label={`Swing score ${v.score} · rank ${v.rank}${swing.universeN ? ` of ${swing.universeN}` : ""}`}
        meaning={
          v.picked
            ? "One of tonight's five swing picks: buy at the next open, stop 7% under entry, trailing 12% under the high, out after 60 sessions."
            : v.investable
              ? "The model's chance of +14% under the swing exit rule. Liquid enough to be picked, but not in tonight's top five."
              : "The model's chance of +14% under the swing exit rule. Under ₹50 or under ₹5 cr a day, so never picked."
        }
        className={
          v.picked
            ? "font-semibold text-emerald-600 dark:text-emerald-400"
            : v.investable
              ? "text-sky-700 dark:text-sky-400"
              : "text-muted-foreground"
        }
      >
        {Number(v.score).toFixed(3)}
      </FlagTip>
      {v.picked ? (
        <FlagTip
          label="Swing pick"
          meaning={`One of the session's five picks${swing.switchOn === false ? " (the market switch has since turned off)" : ""}. See Doubling → Swing picks.`}
          className="ml-1 text-amber-500"
        >
          ★
        </FlagTip>
      ) : null}
    </span>
  );
}

/** The column's header. The key to the flags lives on `/mb`; the title carries the short version.
 * PM11: renders the Swing column's header right after it. */
export function MbScoreHead({
  className = "py-2 pr-3 font-medium",
}: {
  className?: string;
}) {
  return (
    <>
      <MbOnlyHead className={className} />
      <th scope="col" className={className}>
        <SwingScoreHeadLink />
      </th>
    </>
  );
}

function MbOnlyHead({ className }: { className: string }) {
  return (
    <th scope="col" className={className}>
      <MbScoreHeadLink />
    </th>
  );
}

/** The Model column's header: the doubling model, linked to `/mb` (the key to the flags). */
export function MbScoreHeadLink() {
  const flags = MB_FLAGS.map((f) => `${f.symbol} ${f.label}`).join(" · ");
  return (
    <a
      href="/mb"
      className="underline decoration-dotted underline-offset-2"
      title={`The doubling model's score: higher is likelier to double within a year; hover a value for its rank. ${flags}`}
    >
      Model
    </a>
  );
}

/** The model's rank for one stock and its flags. Information only: it never filters the row. */
export function MbScoreValue({ symbol }: { symbol: string }) {
  const { bySymbol, universeN, loaded, error } = useMbScoresContext();
  if (!loaded) return <span className="text-muted-foreground">·</span>;
  if (error) return <ScoresUnavailable model="Doubling" error={error} testId="mb-score-cell" />;
  const v = bySymbol[symbol.trim().toUpperCase()];
  return (
    <span
      className="whitespace-nowrap tabular-nums"
      data-testid="mb-score-cell"
    >
      {v ? (
        <FlagTip
          label={`Doubling score ${v.score} · rank ${v.rank}${universeN ? ` of ${universeN}` : ""}`}
          meaning={
            v.scope && v.scope !== "universe"
              ? `Outside the model's training range (${SWING_SCOPE[v.scope].label.toLowerCase()}): read it as a rough guide only.`
              : "Higher is likelier to double within a year. Green: investable."
          }
          className={
            v.scope && v.scope !== "universe"
              ? "text-muted-foreground"
              : v.investable
                ? "font-medium text-emerald-600 dark:text-emerald-400"
                : undefined
          }
        >
          {Number(v.score).toFixed(3)}
        </FlagTip>
      ) : null}
      {flagsOf(v).map((k) => (
        <FlagTip
          key={k}
          label={k === "outside" && v?.scope && v.scope !== "universe" ? SWING_SCOPE[v.scope].label : (SYMBOL[k]?.label ?? k)}
          meaning={k === "outside" && v?.scope && v.scope !== "universe" ? SWING_SCOPE[v.scope].meaning : SYMBOL[k]?.meaning}
          className={`ml-1 ${SYMBOL[k]?.tone ?? ""}`}
        >
          {SYMBOL[k]?.symbol}
        </FlagTip>
      ))}
    </span>
  );
}

export function MbScoreCell({
  symbol,
  className = "py-2 pr-3",
}: {
  symbol: string;
  className?: string;
}) {
  return (
    <>
      <td className={className}>
        <MbScoreValue symbol={symbol} />
      </td>
      <td className={className}>
        <SwingScoreValue symbol={symbol} />
      </td>
    </>
  );
}
