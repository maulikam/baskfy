"use client";

import { createContext, useContext, type ReactNode } from "react";

import { MB_FLAGS, flagsOf } from "@/lib/mb/flags";
import { EMPTY_MB_SCORES, type MbScores, useMbScores } from "@/lib/mb/scores";

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

/** The swing-picks model's score for one stock (PM11). Information only: it never filters the row. */
export function SwingScoreValue({ symbol }: { symbol: string }) {
  const { swing, loaded } = useMbScoresContext();
  if (!loaded) return <span className="text-muted-foreground">·</span>;
  const v = swing.bySymbol[symbol.trim().toUpperCase()];
  if (!v)
    return (
      <span className="text-slate-400" title="Not scored by the swing model" data-testid="swing-score-cell">
        ⊘
      </span>
    );
  return (
    <span className="whitespace-nowrap tabular-nums" data-testid="swing-score-cell">
      <span
        title={`Rank ${v.rank}${swing.universeN ? ` of ${swing.universeN}` : ""} · score ${v.score}${v.investable ? "" : " · too cheap or thin to be picked"}`}
        className={
          v.picked
            ? "font-semibold text-emerald-600 dark:text-emerald-400"
            : v.investable
              ? "text-sky-700 dark:text-sky-400"
              : "text-muted-foreground"
        }
      >
        {Number(v.score).toFixed(3)}
      </span>
      {v.picked ? (
        <span className="ml-1 text-amber-500" title="One of the session's five swing picks" aria-label="Swing pick">
          ★
        </span>
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
  const title = MB_FLAGS.map((f) => `${f.symbol} ${f.label}`).join(" · ");
  return (
    <th
      scope="col"
      className={className}
      title={`The doubling model's score: higher is likelier to double within a year; hover a value for its rank. ${title}`}
    >
      <a href="/mb" className="underline decoration-dotted underline-offset-2">
        Model
      </a>
    </th>
  );
}

/** The model's rank for one stock and its flags. Information only: it never filters the row. */
export function MbScoreValue({ symbol }: { symbol: string }) {
  const { bySymbol, universeN, loaded } = useMbScoresContext();
  if (!loaded) return <span className="text-muted-foreground">·</span>;
  const v = bySymbol[symbol.trim().toUpperCase()];
  return (
    <span
      className="whitespace-nowrap tabular-nums"
      data-testid="mb-score-cell"
    >
      {v ? (
        <span
          title={`Rank ${v.rank}${universeN ? ` of ${universeN}` : ""} · score ${v.score}`}
          className={
            v.investable
              ? "font-medium text-emerald-600 dark:text-emerald-400"
              : undefined
          }
        >
          {Number(v.score).toFixed(3)}
        </span>
      ) : null}
      {flagsOf(v).map((k) => (
        <span
          key={k}
          className={`ml-1 ${SYMBOL[k]?.tone ?? ""}`}
          title={`${SYMBOL[k]?.label}: ${SYMBOL[k]?.meaning}`}
          aria-label={SYMBOL[k]?.label}
        >
          {SYMBOL[k]?.symbol}
        </span>
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
