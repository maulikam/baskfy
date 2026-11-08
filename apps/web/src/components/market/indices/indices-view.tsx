"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { LayoutGrid, RefreshCw, Rows3, Search } from "lucide-react";
import { parseAsString, parseAsStringLiteral, useQueryState } from "nuqs";
import { Fragment, useDeferredValue, useId, useMemo, useState } from "react";

import { Sparkline } from "@/components/data/sparkline";
import { useIsStaff } from "@/components/shell/staff-context";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  type IndexRow,
  type IndicesList,
  fetchIndices,
  indicesPollInterval,
  refreshIndices,
} from "@/lib/indices/api";
import { EMPTY_CELL } from "@/lib/format";
import { useLiveMarks } from "@/lib/screens/live-marks";
import { cn } from "@/lib/utils";

import { FreshnessChip } from "./freshness-chip";
import { IndexDetail } from "./index-detail";
import { Check, GroupPill, HitsBadge, Pct, SortHeader, compareNullable, num } from "./parts";

const TILES: readonly { name: string; label: string }[] = [
  { name: "NIFTY 50", label: "NIFTY 50" },
  { name: "NIFTY BANK", label: "NIFTY Bank" },
  { name: "NIFTY MIDCAP 150", label: "Midcap 150" },
  { name: "NIFTY SMALLCAP 250", label: "Smallcap 250" },
  { name: "INDIA VIX", label: "India VIX" },
];

const FILTERS = ["all", "broad", "sector", "theme", "strategy"] as const;
type Filter = (typeof FILTERS)[number];
const FILTER_LABEL: Record<Filter, string> = {
  all: "All",
  broad: "Broad",
  sector: "Sector",
  theme: "Theme",
  strategy: "Strategy",
};
const FILTER_GROUP: Record<Exclude<Filter, "all">, string> = {
  broad: "Broad market",
  sector: "Sectoral",
  theme: "Thematic",
  strategy: "Strategy",
};

const VIEWS = ["table", "cards"] as const;

type SortKey =
  | "name"
  | "chg_pct"
  | "level"
  | "vs20_pct"
  | "cross_10_20"
  | "ret_1y"
  | "dist_52w_hi_pct"
  | "pe"
  | "pb"
  | "div_yield"
  | "swing_hits";

const COLUMNS: readonly { key: SortKey | null; label: string; align: "left" | "right" }[] = [
  { key: "name", label: "Index", align: "left" },
  { key: null, label: "30 days", align: "left" },
  { key: "chg_pct", label: "Chg %", align: "right" },
  { key: "level", label: "Level", align: "right" },
  { key: "vs20_pct", label: "vs 20DMA", align: "right" },
  { key: "cross_10_20", label: "10>20", align: "right" },
  { key: "ret_1y", label: "1y", align: "right" },
  { key: "dist_52w_hi_pct", label: "From 52w high", align: "right" },
  { key: "pe", label: "P/E", align: "right" },
  { key: "pb", label: "P/B", align: "right" },
  { key: "div_yield", label: "Yield", align: "right" },
  { key: "swing_hits", label: "Swing hits", align: "right" },
];

function sortValue(r: IndexRow, k: SortKey): number | string | null {
  if (k === "cross_10_20") return r.cross_10_20 === null ? null : r.cross_10_20 ? 1 : 0;
  return r[k];
}

const norm = (s: string) => s.trim().toUpperCase();

function findRow(rows: readonly IndexRow[], name: string): IndexRow | undefined {
  const target = norm(name);
  const slug = name.toLowerCase().replace(/\s+/g, "-");
  return rows.find((r) => norm(r.name) === target || r.slug === slug);
}

function Tile({
  label,
  level,
  chg,
  spark,
}: {
  label: string;
  level: number | null | undefined;
  chg: number | null | undefined;
  spark: readonly number[];
}) {
  return (
    <li className="min-w-[10.5rem] shrink-0 snap-start rounded-xl border border-border/70 bg-card px-3.5 py-3 sm:min-w-0">
      <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{label}</p>
      <div className="mt-1 flex items-end justify-between gap-2">
        <div>
          <p className="text-lg font-semibold leading-tight tabular-nums">{num(level)}</p>
          <Pct value={chg} className="text-xs" />
        </div>
        <Sparkline values={spark} width={64} height={22} label={`${label} 30-day level`} />
      </div>
    </li>
  );
}

function RegimeBar({ data }: { data: IndicesList }) {
  const r = data.regime;
  if (!r) return null;
  const share = r.total > 0 ? (r.above_20 / r.total) * 100 : null;
  const series = r.breadth_30d.map((p) => p.above_20_pct).filter((v): v is number => v !== null);
  return (
    <section
      aria-label="Index regime"
      className="flex flex-col gap-3 rounded-xl border border-border/70 bg-card px-4 py-3 sm:flex-row sm:items-center sm:justify-between"
    >
      <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
        <p className="text-sm">
          <span className="text-lg font-semibold tabular-nums">{r.above_20}</span>
          <span className="text-muted-foreground"> of {r.total} above their 20-day average</span>
        </p>
        <p className="text-sm">
          <span className="text-lg font-semibold tabular-nums">
            {r.cross_pct === null ? EMPTY_CELL : `${Math.round(r.cross_pct)}%`}
          </span>
          <span className="text-muted-foreground"> with 10-day over 20-day</span>
        </p>
      </div>
      <div className="flex items-center gap-3">
        <div className="h-1.5 w-28 overflow-hidden rounded-full bg-muted" aria-hidden="true">
          <div className="h-full rounded-full bg-accent" style={{ width: `${share ?? 0}%` }} />
        </div>
        <Sparkline values={series} width={120} height={26} label="Share above 20-day average, 30 days" />
      </div>
    </section>
  );
}

function IndexCard({ row, open, onToggle }: { row: IndexRow; open: boolean; onToggle: () => void }) {
  return (
    <li>
      <button
        type="button"
        aria-expanded={open}
        onClick={onToggle}
        className={cn(
          "flex w-full flex-col gap-2 rounded-xl border border-border/70 bg-card p-3 text-left transition-colors hover:bg-muted/40",
          open && "border-accent/60",
        )}
      >
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="truncate text-sm font-medium leading-tight">{row.name}</p>
            <div className="mt-1 flex items-center gap-1.5">
              <GroupPill group={row.group} />
              {row.swing_hits ? <HitsBadge count={row.swing_hits} /> : null}
            </div>
          </div>
          <Sparkline values={row.spark_30d ?? []} label={`${row.name} 30-day level`} />
        </div>
        <div className="flex items-baseline gap-2">
          <p className="text-lg font-semibold tabular-nums">{num(row.level)}</p>
          <Pct value={row.chg_pct} className="text-sm" />
        </div>
        <dl className="grid grid-cols-3 gap-2 text-xs text-muted-foreground">
          <div>
            <dt>vs 20DMA</dt>
            <dd>
              <Pct value={row.vs20_pct} />
            </dd>
          </div>
          <div>
            <dt>1y</dt>
            <dd>
              <Pct value={row.ret_1y} />
            </dd>
          </div>
          <div>
            <dt>P/E</dt>
            <dd className="tabular-nums text-foreground">{num(row.pe)}</dd>
          </div>
        </dl>
      </button>
    </li>
  );
}

export function IndicesView() {
  const isStaff = useIsStaff();
  const client = useQueryClient();
  const list = useQuery({
    queryKey: ["indices"],
    queryFn: ({ signal }) => fetchIndices(signal),
    refetchInterval: indicesPollInterval,
  });

  const [query, setQuery] = useQueryState(
    "q",
    parseAsString.withDefault("").withOptions({ history: "replace", throttleMs: 200 }),
  );
  const [view, setView] = useQueryState(
    "view",
    parseAsStringLiteral(VIEWS).withDefault("table").withOptions({ history: "replace" }),
  );
  const [filter, setFilter] = useQueryState(
    "group",
    parseAsStringLiteral(FILTERS).withDefault("all").withOptions({ history: "replace" }),
  );
  const [aboveOnly, setAboveOnly] = useState(false);
  const aboveId = useId();
  const [crossOnly, setCrossOnly] = useState(false);
  const crossId = useId();
  const [sortK, setSortK] = useState<SortKey>("vs20_pct");
  const [sortDir, setSortDir] = useState<1 | -1>(-1);
  const [openId, setOpenId] = useState<number | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshNote, setRefreshNote] = useState<string | null>(null);

  const rows = useMemo(() => list.data?.data ?? [], [list.data]);
  const vixRow = findRow(rows, "INDIA VIX");
  const vixLive = useLiveMarks(list.data && !vixRow ? ["INDIA VIX"] : []);

  const deferred = useDeferredValue(query);
  const visible = useMemo(() => {
    const needle = deferred.trim().toLowerCase();
    const group = filter === "all" ? null : FILTER_GROUP[filter];
    return rows
      .filter((r) => (group ? r.group === group : true))
      .filter((r) => (aboveOnly ? r.above_20 === true : true))
      .filter((r) => (crossOnly ? r.cross_10_20 === true : true))
      .filter((r) => !needle || r.name.toLowerCase().includes(needle) || r.slug.includes(needle))
      .sort((a, b) => compareNullable(sortValue(a, sortK), sortValue(b, sortK), sortDir));
  }, [rows, deferred, filter, aboveOnly, crossOnly, sortK, sortDir]);

  const sortBy = (k: SortKey) => {
    if (k === sortK) setSortDir((d) => (d === 1 ? -1 : 1));
    else {
      setSortK(k);
      setSortDir(k === "name" ? 1 : -1);
    }
  };

  const toggle = (id: number) => setOpenId((cur) => (cur === id ? null : id));

  const onRefresh = async () => {
    setRefreshing(true);
    setRefreshNote(null);
    try {
      if (isStaff) {
        const r = await refreshIndices();
        setRefreshNote(
          `Master list ${r.refreshed_master} · constituents ${r.constituents_ok} read` +
            (r.constituents_failed.length ? ` · ${r.constituents_failed.length} kept their last list` : ""),
        );
      }
    } catch (error) {
      setRefreshNote(`Refresh of the master list did not finish (${String(error)}); showing the last good data.`);
    } finally {
      await client.invalidateQueries({ queryKey: ["indices"] });
      await client.invalidateQueries({ queryKey: ["index-detail"] });
      await client.invalidateQueries({ queryKey: ["index-constituents"] });
      setRefreshing(false);
    }
  };

  const failed = list.isError;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <FreshnessChip freshness={list.data?.freshness} fetchFailed={failed} />
        <div className="flex items-center gap-2">
          {refreshNote ? <p className="text-xs text-muted-foreground">{refreshNote}</p> : null}
          <Button variant="outline" size="sm" onClick={() => void onRefresh()} disabled={refreshing}>
            <RefreshCw aria-hidden="true" className={cn("size-3.5", refreshing && "animate-spin")} />
            Refresh
          </Button>
        </div>
      </div>

      <ul
        data-testid="market-headline-strip"
        className="-mx-4 flex snap-x gap-2 overflow-x-auto px-4 pb-1 sm:mx-0 sm:grid sm:grid-cols-3 sm:overflow-visible sm:px-0 lg:grid-cols-5"
      >
        {TILES.map((t) => {
          const row = findRow(rows, t.name);
          const quote = !row && t.name === "INDIA VIX" ? vixLive.quotes["INDIA VIX"] : undefined;
          return (
            <Tile
              key={t.name}
              label={t.label}
              level={row?.level ?? quote?.lastPrice ?? vixLive.marks[t.name] ?? null}
              chg={row?.chg_pct ?? quote?.changePct ?? null}
              spark={row?.spark_30d ?? []}
            />
          );
        })}
      </ul>

      {list.data ? <RegimeBar data={list.data} /> : null}

      <div className="flex flex-wrap items-center gap-3">
        <div role="group" aria-label="Index group" className="flex flex-wrap rounded-full border border-border p-0.5">
          {FILTERS.map((f) => (
            <button
              key={f}
              type="button"
              aria-pressed={filter === f}
              onClick={() => void setFilter(f)}
              className={cn(
                "rounded-full px-3 py-1 text-xs",
                filter === f ? "bg-foreground font-medium text-background" : "text-muted-foreground hover:text-foreground",
              )}
            >
              {FILTER_LABEL[f]}
            </button>
          ))}
        </div>
        <div className="relative w-full max-w-xs flex-1">
          <Search
            aria-hidden="true"
            className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
          />
          <Input
            type="search"
            value={query}
            onChange={(e) => void setQuery(e.target.value)}
            placeholder="Search indices"
            aria-label="Search indices"
            className="pl-8"
          />
        </div>
        <span className="flex items-center gap-2 text-xs text-muted-foreground">
          <Switch id={aboveId} checked={aboveOnly} onCheckedChange={setAboveOnly} />
          <label htmlFor={aboveId}>Above 20DMA only</label>
        </span>
        <span className="flex items-center gap-2 text-xs text-muted-foreground">
          <Switch id={crossId} checked={crossOnly} onCheckedChange={setCrossOnly} />
          <label htmlFor={crossId}>10DMA above 20DMA only</label>
        </span>
        <div className="ml-auto flex items-center gap-2">
          <p aria-live="polite" className="text-xs tabular-nums text-muted-foreground">
            {visible.length} of {rows.length}
          </p>
          <div role="group" aria-label="View" className="hidden rounded-md border border-border p-0.5 md:flex">
            {VIEWS.map((v) => (
              <button
                key={v}
                type="button"
                aria-pressed={view === v}
                onClick={() => void setView(v)}
                className={cn(
                  "flex items-center gap-1.5 rounded-sm px-2 py-1 text-xs",
                  view === v ? "bg-muted font-medium" : "text-muted-foreground",
                )}
              >
                {v === "table" ? (
                  <Rows3 aria-hidden="true" className="size-3.5" />
                ) : (
                  <LayoutGrid aria-hidden="true" className="size-3.5" />
                )}
                {v === "table" ? "Table" : "Cards"}
              </button>
            ))}
          </div>
        </div>
      </div>

      {!list.data && failed ? (
        <p className="rounded-xl border border-dashed border-border px-4 py-10 text-center text-sm text-muted-foreground">
          The index service did not answer and nothing is cached in this tab yet. It retries
          automatically; Refresh tries now.
        </p>
      ) : null}
      {!list.data && !failed ? <div className="h-64 animate-pulse rounded-xl bg-muted/60" /> : null}

      {list.data ? (
        <>
          <div className={cn("overflow-x-auto rounded-xl border border-border/70 bg-card", view === "cards" ? "hidden" : "hidden md:block")}>
            <table className="w-full min-w-[980px] text-sm">
              <thead className="sticky top-0 border-b border-border bg-card">
                <tr>
                  {COLUMNS.map((c) =>
                    c.key === null ? (
                      <th key={c.label} scope="col" className="px-3 py-2 text-left text-xs font-medium text-muted-foreground">
                        {c.label}
                      </th>
                    ) : (
                      <SortHeader
                        key={c.key}
                        label={c.label}
                        align={c.align}
                        active={sortK === c.key}
                        dir={sortDir}
                        onClick={() => sortBy(c.key as SortKey)}
                      />
                    ),
                  )}
                </tr>
              </thead>
              <tbody>
                {visible.map((r) => {
                  const open = openId === r.id;
                  return (
                    <Fragment key={r.id}>
                      <tr
                        onClick={() => toggle(r.id)}
                        aria-expanded={open}
                        className={cn(
                          "cursor-pointer border-b border-border/50 transition-colors hover:bg-muted/40",
                          open && "bg-accent-muted hover:bg-accent-muted",
                        )}
                      >
                        <td className="px-3 py-2">
                          <span className="flex items-center gap-2">
                            <span
                              aria-hidden="true"
                              className={cn("inline-block transition-transform motion-reduce:transition-none", open && "rotate-90")}
                            >
                              ›
                            </span>
                            <span className="font-medium">{r.name}</span>
                            <GroupPill group={r.group} />
                            {r.stale ? (
                              <span className="rounded-full bg-warning-muted px-1.5 py-px text-[10px] font-semibold text-warning">
                                Last close
                              </span>
                            ) : null}
                          </span>
                        </td>
                        <td className="px-3 py-2">
                          <Sparkline values={r.spark_30d ?? []} label={`${r.name} 30-day level`} />
                        </td>
                        <td className="px-3 py-2 text-right">
                          <Pct value={r.chg_pct} />
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums">{num(r.level)}</td>
                        <td className="px-3 py-2 text-right">
                          <Pct value={r.vs20_pct} />
                        </td>
                        <td className="px-3 py-2 text-right">
                          <Check on={r.cross_10_20} />
                        </td>
                        <td className="px-3 py-2 text-right">
                          <Pct value={r.ret_1y} />
                        </td>
                        <td className="px-3 py-2 text-right">
                          <Pct value={r.dist_52w_hi_pct} />
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums">{num(r.pe)}</td>
                        <td className="px-3 py-2 text-right tabular-nums">{num(r.pb)}</td>
                        <td className="px-3 py-2 text-right tabular-nums">{num(r.div_yield)}</td>
                        <td className="px-3 py-2 text-right">
                          <HitsBadge count={r.swing_hits} />
                        </td>
                      </tr>
                      {open ? (
                        <tr className="border-b border-border/50 bg-muted/30">
                          <td colSpan={COLUMNS.length} className="p-3">
                            <IndexDetail row={r} />
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>

          <ul
            aria-label="Indices"
            className={cn("grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4", view === "cards" ? "" : "md:hidden")}
          >
            {visible.map((r) => (
              <Fragment key={r.id}>
                <IndexCard row={r} open={openId === r.id} onToggle={() => toggle(r.id)} />
                {openId === r.id ? (
                  <li className="col-span-full">
                    <IndexDetail row={r} />
                  </li>
                ) : null}
              </Fragment>
            ))}
          </ul>

          {visible.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">
              No index matches {query ? `“${query}”` : "these filters"}.
            </p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
