"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { Route } from "next";
import { useId, useMemo, useState } from "react";

import { Switch } from "@/components/ui/switch";
import {
  type Constituent,
  type IndexRow,
  fetchIndexConstituents,
  fetchIndexDetail,
  fetchIndexHistory,
  indicesPollInterval,
  istDay,
} from "@/lib/indices/api";
import { formatTradeDate } from "@/lib/format";
import { cn } from "@/lib/utils";

import { FreshnessChip } from "./freshness-chip";
import { IndexHistoryChart } from "./index-history-chart";
import {
  GroupPill,
  HitTags,
  Pct,
  RangeBar,
  SortHeader,
  Stat,
  compareNullable,
  num,
  toneOf,
} from "./parts";

type CKey = "symbol" | "name" | "weight" | "ltp" | "chg_pct" | "vs20_pct" | "dist_52w_hi_pct" | "hits";

const C_COLUMNS: readonly { key: CKey; label: string; align: "left" | "right" }[] = [
  { key: "symbol", label: "Symbol", align: "left" },
  { key: "name", label: "Name", align: "left" },
  { key: "weight", label: "Weight %", align: "right" },
  { key: "ltp", label: "LTP", align: "right" },
  { key: "chg_pct", label: "Chg %", align: "right" },
  { key: "vs20_pct", label: "vs 20DMA", align: "right" },
  { key: "dist_52w_hi_pct", label: "From 52w high", align: "right" },
  { key: "hits", label: "Swing hits", align: "left" },
];

function cValue(c: Constituent, key: CKey): number | string | null {
  if (key === "hits") return c.hits.length;
  return c[key];
}

function instrumentHref(symbol: string): Route {
  return `/instruments/${encodeURIComponent(symbol)}` as Route;
}

export function IndexDetail({ row }: { row: IndexRow }) {
  const router = useRouter();
  const key = row.slug || String(row.id);
  const detail = useQuery({
    queryKey: ["index-detail", key],
    queryFn: ({ signal }) => fetchIndexDetail(key, signal),
    refetchInterval: indicesPollInterval,
  });
  const constituents = useQuery({
    queryKey: ["index-constituents", key],
    queryFn: ({ signal }) => fetchIndexConstituents(key, signal),
    refetchInterval: indicesPollInterval,
  });
  const history = useQuery({
    queryKey: ["index-history", key],
    queryFn: ({ signal }) => fetchIndexHistory(key, 365, signal),
    staleTime: 10 * 60_000,
  });

  const [sortK, setSortK] = useState<CKey>("weight");
  const [sortDir, setSortDir] = useState<1 | -1>(-1);
  const [hitsOnly, setHitsOnly] = useState(false);
  const hitsId = useId();

  const idx = detail.data?.index ?? null;
  const level = idx?.level ?? row.level;
  const chg = idx?.chg_pct ?? row.chg_pct;

  const rows = useMemo(() => {
    const list = constituents.data?.data ?? [];
    const filtered = hitsOnly ? list.filter((c) => c.hits.length > 0) : list;
    return [...filtered].sort((a, b) => compareNullable(cValue(a, sortK), cValue(b, sortK), sortDir));
  }, [constituents.data, hitsOnly, sortK, sortDir]);

  const sortBy = (k: CKey) => {
    if (k === sortK) setSortDir((d) => (d === 1 ? -1 : 1));
    else {
      setSortK(k);
      setSortDir(k === "symbol" || k === "name" ? 1 : -1);
    }
  };

  const freshness = detail.data?.freshness ?? null;

  return (
    <section
      aria-label={`${row.name} detail`}
      className="flex flex-col gap-5 rounded-xl border border-border/70 bg-card p-4 sm:p-5"
    >
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold leading-tight">{row.name}</h2>
            <GroupPill group={row.group} />
            {detail.data?.leading ? (
              <span className="rounded-full bg-accent-muted px-2 py-px text-[10.5px] font-semibold uppercase tracking-wide text-accent">
                Leading
              </span>
            ) : null}
          </div>
          <div className="flex items-baseline gap-3">
            <p className="text-3xl font-light tabular-nums">{num(level)}</p>
            <Pct value={chg} className="text-base font-medium" />
          </div>
        </div>
        <FreshnessChip freshness={freshness} fetchFailed={detail.isError} />
      </header>

      <div className="grid gap-4 sm:grid-cols-2">
        <RangeBar label="Day range" lo={idx?.low} hi={idx?.high} at={level} />
        <RangeBar label="52-week range" lo={idx?.lo_52w ?? row.lo_52w} hi={idx?.hi_52w ?? row.hi_52w} at={level} />
      </div>

      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
        <Stat k="1 year">
          <Pct value={idx?.ret_1y ?? row.ret_1y} />
        </Stat>
        <Stat k="30 days">
          <Pct value={idx?.ret_30d ?? row.ret_30d} />
        </Stat>
        <Stat k="5 days">
          <Pct value={idx?.ret_5d ?? row.ret_5d} />
        </Stat>
        <Stat k="P/E">{num(idx?.pe ?? row.pe)}</Stat>
        <Stat k="P/B">{num(idx?.pb ?? row.pb)}</Stat>
        <Stat k="Yield">{num(idx?.div_yield ?? row.div_yield)}</Stat>
        <Stat k="Up / down">
          <span className="text-positive">{idx?.constituents_up ?? "—"}</span>
          <span className="text-muted-foreground"> / </span>
          <span className="text-negative">{idx?.constituents_down ?? "—"}</span>
        </Stat>
      </dl>

      <div>
        <p className="eyebrow mb-2">One year, with 10, 20 and 50-day averages</p>
        {history.data ? (
          <IndexHistoryChart points={history.data} name={row.name} />
        ) : history.isError ? (
          <p className="rounded-md border border-dashed border-border px-4 py-10 text-center text-sm text-muted-foreground">
            The year&apos;s history did not load this time. It retries on the next refresh.
          </p>
        ) : (
          <div className="h-48 animate-pulse rounded-md bg-muted/60" />
        )}
      </div>

      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="eyebrow">
            Constituents
            {constituents.data ? ` · ${constituents.data.data.length}` : ""}
            {constituents.data?.constituents_as_of
              ? ` · list as of ${formatTradeDate(constituents.data.constituents_as_of.slice(0, 10))}`
              : ""}
          </p>
          <span className="flex items-center gap-2 text-xs text-muted-foreground">
            <Switch id={hitsId} checked={hitsOnly} onCheckedChange={setHitsOnly} />
            <label htmlFor={hitsId}>Only scanner hits</label>
          </span>
        </div>
        {constituents.data?.stale_constituents ? (
          <p className="rounded-md bg-warning-muted px-3 py-1.5 text-xs text-warning">
            This list may be out of date: NSE&apos;s latest file could not be read, so it is the one
            saved on {istDay(constituents.data.constituents_as_of) || "the last good read"}.
          </p>
        ) : null}
        {constituents.isError && !constituents.data ? (
          <p className="rounded-md bg-warning-muted px-3 py-1.5 text-xs text-warning">
            The stocks in this index did not load this time. They retry on the next refresh.
          </p>
        ) : null}
        <div className="overflow-x-auto rounded-lg border border-border/70">
          <table className="w-full min-w-[720px] text-sm">
            <thead className="border-b border-border bg-muted/40">
              <tr>
                {C_COLUMNS.map((c) => (
                  <SortHeader
                    key={c.key}
                    label={c.label}
                    align={c.align}
                    active={sortK === c.key}
                    dir={sortDir}
                    onClick={() => sortBy(c.key)}
                  />
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <tr
                  key={c.symbol}
                  onClick={() => router.push(instrumentHref(c.symbol))}
                  className="cursor-pointer border-b border-border/50 last:border-0 hover:bg-muted/40"
                >
                  <td className="px-3 py-2 font-medium">
                    <Link
                      href={instrumentHref(c.symbol)}
                      onClick={(e) => e.stopPropagation()}
                      className="hover:text-accent"
                    >
                      {c.symbol}
                    </Link>
                  </td>
                  <td className="max-w-[16rem] truncate px-3 py-2 text-muted-foreground">{c.name ?? "—"}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{num(c.weight)}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{num(c.ltp)}</td>
                  <td className="px-3 py-2 text-right">
                    <Pct value={c.chg_pct} />
                  </td>
                  <td className={cn("px-3 py-2 text-right tabular-nums", toneOf(c.vs20_pct))}>
                    <Pct value={c.vs20_pct} />
                  </td>
                  <td className="px-3 py-2 text-right">
                    <Pct value={c.dist_52w_hi_pct} />
                  </td>
                  <td className="px-3 py-2">
                    <HitTags hits={c.hits} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {constituents.data && rows.length === 0 ? (
            <p className="px-4 py-6 text-center text-sm text-muted-foreground">
              {hitsOnly ? "No stock in this index is on a scanner today." : "No constituent list saved for this index yet."}
            </p>
          ) : null}
          {!constituents.data && !constituents.isError ? (
            <p className="px-4 py-6 text-center text-sm text-muted-foreground">Loading the stocks in this index…</p>
          ) : null}
        </div>
      </div>
    </section>
  );
}
