"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import type { Route } from "next";
import { useMemo } from "react";

import { type SwingCandidate, fetchSwingCandidates, indicesPollInterval, istDay, istTime } from "@/lib/indices/api";
import { EMPTY_CELL, formatNumber, formatPercent } from "@/lib/format";

/** Read-only: the latest index-led swing scan, top 30, grouped by the index each name rides. */

const TOP = 30;

function leadOf(c: SwingCandidate): string {
  const best = [...c.via_indices].sort((a, b) => (b.strength ?? -Infinity) - (a.strength ?? -Infinity))[0];
  return best?.index ?? "No leading index";
}

export function SwingOpportunities() {
  const q = useQuery({
    queryKey: ["swing-candidates", "latest"],
    queryFn: ({ signal }) => fetchSwingCandidates(signal),
    refetchInterval: indicesPollInterval,
  });

  const groups = useMemo(() => {
    const top = [...(q.data?.data ?? [])]
      .sort((a, b) => (a.rank ?? Infinity) - (b.rank ?? Infinity))
      .slice(0, TOP);
    const map = new Map<string, SwingCandidate[]>();
    for (const c of top) {
      const k = leadOf(c);
      map.set(k, [...(map.get(k) ?? []), c]);
    }
    return [...map.entries()];
  }, [q.data]);

  const leading = q.data?.leading ?? [];

  return (
    <section aria-label="Swing opportunities" className="flex flex-col gap-3 rounded-xl border border-border/70 bg-card p-4">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-base font-semibold">Swing opportunities</h2>
        <p className="text-xs text-muted-foreground tabular-nums">
          {q.data?.run_at
            ? `Scan of ${istDay(q.data.run_at)} ${istTime(q.data.run_at)} · top ${Math.min(TOP, q.data.data.length)} of ${q.data.data.length}`
            : q.isError
              ? "The latest scan did not load; it retries on its own."
              : "Loading the latest scan…"}
        </p>
      </header>

      {leading.length > 0 ? (
        <p className="text-xs text-muted-foreground">
          Leading indices:{" "}
          {leading.map((l, i) => (
            <span key={l.index}>
              {i > 0 ? " · " : ""}
              <span className="font-medium text-foreground">{l.index}</span>
              {l.strength !== null ? ` (${formatNumber(l.strength, { decimals: 1 })})` : ""}
            </span>
          ))}
        </p>
      ) : null}

      {q.data && groups.length === 0 ? (
        <p className="rounded-md border border-dashed border-border px-4 py-6 text-center text-sm text-muted-foreground">
          {leading.length > 0
            ? `${leading.map((l) => l.index).join(", ")} lead, but the scan found no candidates in them.`
            : "No index is leading right now, and the scan found no candidates."}
        </p>
      ) : null}

      {groups.map(([index, list]) => (
        <div key={index} className="flex flex-col gap-1.5">
          <p className="eyebrow">
            {index} · {list.length}
          </p>
          <div className="overflow-x-auto rounded-lg border border-border/70">
            <table className="w-full min-w-[620px] text-sm">
              <thead className="border-b border-border bg-muted/40 text-xs text-muted-foreground">
                <tr>
                  <th scope="col" className="px-3 py-1.5 text-left font-medium">#</th>
                  <th scope="col" className="px-3 py-1.5 text-left font-medium">Symbol</th>
                  <th scope="col" className="px-3 py-1.5 text-left font-medium">Scanner</th>
                  <th scope="col" className="px-3 py-1.5 text-left font-medium">Setup</th>
                  <th scope="col" className="px-3 py-1.5 text-right font-medium">Entry</th>
                  <th scope="col" className="px-3 py-1.5 text-right font-medium">Stop</th>
                  <th scope="col" className="px-3 py-1.5 text-right font-medium">Risk</th>
                  <th scope="col" className="px-3 py-1.5 text-right font-medium">Score</th>
                </tr>
              </thead>
              <tbody>
                {list.map((c) => (
                  <tr key={`${c.symbol}-${c.scanner}`} className="border-b border-border/50 last:border-0">
                    <td className="px-3 py-1.5 tabular-nums text-muted-foreground">{c.rank ?? EMPTY_CELL}</td>
                    <td className="px-3 py-1.5 font-medium">
                      <Link href={`/instruments/${encodeURIComponent(c.symbol)}` as Route} className="hover:text-accent">
                        {c.symbol}
                      </Link>
                    </td>
                    <td className="px-3 py-1.5">
                      <span className="rounded-full bg-accent-muted px-1.5 py-px text-[10.5px] font-semibold text-accent">
                        {c.scanner}
                      </span>
                    </td>
                    <td className="px-3 py-1.5 text-muted-foreground">{c.setup ?? EMPTY_CELL}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums">{formatNumber(c.entry, { decimals: 2 })}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums">{formatNumber(c.stop, { decimals: 2 })}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums">
                      {c.risk_pct === null ? EMPTY_CELL : formatPercent(c.risk_pct).replace(/^\+/, "")}
                    </td>
                    <td className="px-3 py-1.5 text-right tabular-nums">{formatNumber(c.score, { decimals: 2 })}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </section>
  );
}
