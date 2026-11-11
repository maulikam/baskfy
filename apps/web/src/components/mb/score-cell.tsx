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

/** The column's header. The key to the flags lives on `/mb`; the title carries the short version. */
export function MbScoreHead({
  className = "py-2 pr-3 font-medium",
}: {
  className?: string;
}) {
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
    <td className={className}>
      <MbScoreValue symbol={symbol} />
    </td>
  );
}
