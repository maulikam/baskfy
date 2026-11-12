"use client";

import { useMemo, useState } from "react";

import { InstrumentLink } from "@/components/instrument/instrument-link";
import { FlagTip } from "@/components/mb/flag-tip";
import { SaveInstrumentButton } from "@/components/watchlist/save-instrument-button";
import { EMPTY_CELL, formatFraction, formatNumber, formatTradeDate } from "@/lib/format";
import {
  DEFAULT_FILTERS,
  type ExplorerFilters,
  type RangeFilter,
  type SortKey,
  combinedRank,
  defaultAscending,
  explore,
} from "@/lib/mb/explorer";
import type { MbExplorer, MbExplorerName } from "@/lib/mb/fetch";
import { MB_FLAGS, SWING_SCOPE, flagsOf } from "@/lib/mb/flags";

const PAGE = 100;
const SYMBOL = Object.fromEntries(MB_FLAGS.map((f) => [f.key, f]));
const INPUT =
  "h-8 w-full rounded-md border border-border bg-background px-2 text-sm tabular-nums outline-none focus-visible:ring-2 focus-visible:ring-ring";

function NumberField({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: number | null;
  onChange: (v: number | null) => void;
  placeholder?: string;
}) {
  return (
    <label className="space-y-1 text-xs text-muted-foreground">
      <span>{label}</span>
      <input
        type="number"
        inputMode="decimal"
        className={INPUT}
        value={value ?? ""}
        placeholder={placeholder ?? "any"}
        onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
      />
    </label>
  );
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center gap-2 text-sm">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}

function tone(v: string | null): string {
  if (v === null) return "";
  const n = Number(v);
  return n > 0 ? "text-emerald-600 dark:text-emerald-400" : n < 0 ? "text-red-600 dark:text-red-400" : "";
}

function DoublingCell({ n, total }: { n: MbExplorerName; total: number }) {
  const v = n.doubling;
  if (!v || v.score === null)
    return (
      <FlagTip label="Not scored" meaning="The doubling model has no row for this stock tonight." className="text-slate-400">
        ⊘
      </FlagTip>
    );
  const scope = v.scope ?? "universe";
  const out = scope !== "universe" ? SWING_SCOPE[scope] : null;
  const flags = flagsOf({
    rank: v.rank,
    score: v.score,
    investable: v.investable ?? false,
    hi63_trigger: n.hi63_trigger ?? false,
    hi63_trigger_date: null,
    close: n.close,
    turnover20: n.turnover20,
    sessions_listed: n.sessions_listed,
    ttm_rev_yoy_fraction: n.ttm_rev_yoy_fraction,
    ttm_pat_yoy_fraction: n.ttm_pat_yoy_fraction,
    scope,
  });
  return (
    <span className="whitespace-nowrap tabular-nums">
      <FlagTip
        label={`Doubling score ${v.score} · rank ${v.rank} of ${total}`}
        meaning={out ? `Outside the model's training range (${out.label.toLowerCase()}): a rough guide only.` : "Higher is likelier to double within a year."}
        className={out ? "text-muted-foreground" : v.investable ? "font-medium text-emerald-600 dark:text-emerald-400" : undefined}
      >
        {Number(v.score).toFixed(3)}
      </FlagTip>
      {flags.map((k) => (
        <FlagTip
          key={k}
          label={k === "outside" && out ? out.label : (SYMBOL[k]?.label ?? k)}
          meaning={k === "outside" && out ? out.meaning : SYMBOL[k]?.meaning}
          className={`ml-1 ${SYMBOL[k]?.tone ?? ""}`}
        >
          {SYMBOL[k]?.symbol}
        </FlagTip>
      ))}
    </span>
  );
}

function SwingCell({ n, total }: { n: MbExplorerName; total: number }) {
  const v = n.swing;
  if (!v || v.score === null)
    return (
      <FlagTip label="Not scored" meaning="The swing model has no row for this stock tonight." className="text-slate-400">
        ⊘
      </FlagTip>
    );
  const scope = v.scope ?? "universe";
  const out = scope !== "universe" ? SWING_SCOPE[scope] : null;
  return (
    <span className="whitespace-nowrap tabular-nums">
      <FlagTip
        label={`Swing score ${v.score} · rank ${v.rank} of ${total}`}
        meaning={
          out
            ? `Outside the model's training range (${out.label.toLowerCase()}): a rough guide only.`
            : "The model's chance of +14% under a −7% stop trailing 12%, within 60 sessions."
        }
        className={
          out
            ? "text-muted-foreground"
            : v.picked
              ? "font-semibold text-emerald-600 dark:text-emerald-400"
              : v.investable
                ? "text-sky-700 dark:text-sky-400"
                : "text-muted-foreground"
        }
      >
        {Number(v.score).toFixed(3)}
      </FlagTip>
      {v.picked ? (
        <FlagTip label="Swing pick" meaning="One of tonight's five swing picks." className="ml-1 text-amber-500">
          ★
        </FlagTip>
      ) : null}
      {out ? (
        <FlagTip label={out.label} meaning={out.meaning} className="ml-1 text-amber-600 dark:text-amber-400">
          ◌
        </FlagTip>
      ) : null}
    </span>
  );
}

const COLUMNS: readonly { key: SortKey; label: string; hint: string }[] = [
  { key: "combined", label: "Both", hint: "The two models' ranks averaged, as a share of the list: lower is better" },
  { key: "doubling", label: "Doubling", hint: "The doubling model's score; sorts by its rank" },
  { key: "swing", label: "Swing", hint: "The swing model's score; sorts by its rank" },
  { key: "symbol", label: "Stock", hint: "Symbol" },
  { key: "sector", label: "Sector", hint: "Sector" },
  { key: "close", label: "Close", hint: "Last close, rupees" },
  { key: "turnover", label: "Traded a day", hint: "20-day average traded value, ₹ crore" },
  { key: "r252", label: "1-yr return", hint: "Return over the last 252 sessions" },
  { key: "ath", label: "From high", hint: "Close against the all-time high" },
  { key: "age", label: "Listed", hint: "Years of trading history" },
  { key: "rev", label: "Revenue growth", hint: "Trailing-twelve-month revenue, year on year" },
  { key: "pat", label: "Profit growth", hint: "Trailing-twelve-month profit, year on year" },
];

const RANK_BY: readonly { key: SortKey; label: string }[] = [
  { key: "combined", label: "Both models" },
  { key: "doubling", label: "Doubling" },
  { key: "swing", label: "Swing" },
];

/** `/mb/explore` (PM11): every scored stock, filtered on several measures and ranked by either model or both. Read-only. */
export function MbExplorerPanel({
  data,
  watched = [],
}: {
  data: MbExplorer | null;
  /** Symbols already on the viewer's stock watchlist, so each row's button starts in the right state. */
  watched?: readonly string[];
}) {
  const watchedSet = useMemo(() => new Set(watched.map((s) => s.toUpperCase())), [watched]);
  const [f, setF] = useState<ExplorerFilters>(DEFAULT_FILTERS);
  const [key, setKey] = useState<SortKey>("combined");
  const [asc, setAsc] = useState(true);
  const [shown, setShown] = useState(PAGE);
  const names = useMemo(() => data?.names ?? [], [data]);
  const sectors = useMemo(
    () => [...new Set(names.map((n) => n.sector).filter((s): s is string => !!s))].sort(),
    [names],
  );
  const doublingN = useMemo(() => names.filter((n) => n.doubling).length, [names]);
  const swingN = useMemo(() => names.filter((n) => n.swing).length, [names]);
  const rows = useMemo(() => explore(names, f, key, asc), [names, f, key, asc]);
  const set = <K extends keyof ExplorerFilters>(k: K, v: ExplorerFilters[K]) => {
    setF((prev) => ({ ...prev, [k]: v }));
    setShown(PAGE);
  };
  const sortBy = (k: SortKey) => {
    if (k === key) setAsc(!asc);
    else {
      setKey(k);
      setAsc(defaultAscending(k));
    }
    setShown(PAGE);
  };
  if (data === null)
    return <p className="text-sm text-muted-foreground">The explorer&rsquo;s read is unavailable right now.</p>;
  return (
    <div className="space-y-5">
      <p className="text-sm text-muted-foreground">
        {formatNumber(names.length)} stocks scored · doubling model as of {formatTradeDate(data.doubling_as_of)}, swing
        model as of {formatTradeDate(data.swing_as_of)}. Information only: nothing here places an order.
      </p>

      <section aria-label="Filters" className="space-y-3 rounded-md border border-border p-4" data-testid="explorer-filters">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <label className="space-y-1 text-xs text-muted-foreground">
            <span>Search</span>
            <input className={INPUT} value={f.q} placeholder="Symbol or name" onChange={(e) => set("q", e.target.value)} />
          </label>
          <label className="space-y-1 text-xs text-muted-foreground">
            <span>Sector</span>
            <select className={INPUT} value={f.sector} onChange={(e) => set("sector", e.target.value)}>
              <option value="">All sectors</option>
              {sectors.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
          <label className="space-y-1 text-xs text-muted-foreground">
            <span>Training range</span>
            <select className={INPUT} value={f.range} onChange={(e) => set("range", e.target.value as RangeFilter)}>
              <option value="any">Any</option>
              <option value="both">Inside both models&rsquo; range</option>
              <option value="doubling">Inside the doubling model&rsquo;s range</option>
              <option value="swing">Inside the swing model&rsquo;s range</option>
            </select>
          </label>
          <div className="grid grid-cols-2 gap-2">
            <NumberField label="Doubling rank ≤" value={f.doublingTop} onChange={(v) => set("doublingTop", v)} />
            <NumberField label="Swing rank ≤" value={f.swingTop} onChange={(v) => set("swingTop", v)} />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <NumberField label="Close ≥ ₹" value={f.priceMin} onChange={(v) => set("priceMin", v)} />
            <NumberField label="Close ≤ ₹" value={f.priceMax} onChange={(v) => set("priceMax", v)} />
          </div>
          <NumberField label="Traded a day ≥ ₹ cr" value={f.turnoverMinCr} onChange={(v) => set("turnoverMinCr", v)} />
          <div className="grid grid-cols-2 gap-2">
            <NumberField label="1-yr return ≥ %" value={f.r252Min} onChange={(v) => set("r252Min", v)} />
            <NumberField label="1-yr return ≤ %" value={f.r252Max} onChange={(v) => set("r252Max", v)} />
          </div>
          <NumberField label="Within % of all-time high" value={f.athWithin} onChange={(v) => set("athWithin", v)} />
          <NumberField label="Listed ≥ years" value={f.ageMinYears} onChange={(v) => set("ageMinYears", v)} />
          <div className="grid grid-cols-2 gap-2">
            <NumberField label="Revenue growth ≥ %" value={f.revMin} onChange={(v) => set("revMin", v)} />
            <NumberField label="Profit growth ≥ %" value={f.patMin} onChange={(v) => set("patMin", v)} />
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
          <Toggle label="Investable only (≥ ₹50, ≥ ₹5 cr a day)" checked={f.investableOnly} onChange={(v) => set("investableOnly", v)} />
          <Toggle label="At a 63-day high lately ▲" checked={f.hi63Only} onChange={(v) => set("hi63Only", v)} />
          <Toggle label="Tonight's swing picks ★" checked={f.picksOnly} onChange={(v) => set("picksOnly", v)} />
          <button
            type="button"
            className="ml-auto text-sm underline decoration-dotted underline-offset-2"
            onClick={() => {
              setF(DEFAULT_FILTERS);
              setShown(PAGE);
            }}
          >
            Reset filters
          </button>
        </div>
      </section>

      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Rank by">
        <span className="text-sm text-muted-foreground">Rank by</span>
        {RANK_BY.map((r) => (
          <button
            key={r.key}
            type="button"
            aria-pressed={key === r.key}
            onClick={() => {
              setKey(r.key);
              setAsc(true);
              setShown(PAGE);
            }}
            className={`rounded-full border px-3 py-1 text-sm ${
              key === r.key ? "border-orange-500 bg-orange-500 text-white" : "border-border"
            }`}
          >
            {r.label}
          </button>
        ))}
        <span className="ml-auto text-sm tabular-nums" data-testid="explorer-count">
          {formatNumber(rows.length)} of {formatNumber(names.length)} match
        </span>
      </div>

      <div className="overflow-x-auto rounded-md border border-border">
        <table className="min-w-[72rem] border-collapse text-sm" data-testid="explorer-table">
          <thead>
            <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th scope="col" className="px-3 py-2 font-medium">
                #
              </th>
              {COLUMNS.map((c) => (
                <th
                  key={c.key}
                  scope="col"
                  className="px-3 py-2 font-medium"
                  aria-sort={key === c.key ? (asc ? "ascending" : "descending") : "none"}
                >
                  <button type="button" title={c.hint} onClick={() => sortBy(c.key)} className="uppercase tracking-wide">
                    {c.label}
                    {key === c.key ? (asc ? " ▲" : " ▼") : ""}
                  </button>
                </th>
              ))}
              <th scope="col" className="px-3 py-2 font-medium">
                Watch
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, shown).map((n, i) => {
              const both = combinedRank(n, doublingN, swingN);
              const age = n.sessions_listed === null ? null : n.sessions_listed / 250;
              return (
                <tr key={n.instrument_id} className="border-b border-border/40 tabular-nums">
                  <td className="px-3 py-1.5 text-muted-foreground">{i + 1}</td>
                  <td className="px-3 py-1.5">
                    {both === null ? EMPTY_CELL : (
                      <FlagTip
                        label={`Top ${formatFraction(both, 1)} on both models together`}
                        meaning="The two ranks averaged, as a share of each model's list."
                      >
                        {formatFraction(both, 1)}
                      </FlagTip>
                    )}
                  </td>
                  <td className="px-3 py-1.5">
                    <DoublingCell n={n} total={doublingN} />
                  </td>
                  <td className="px-3 py-1.5">
                    <SwingCell n={n} total={swingN} />
                  </td>
                  <td className="px-3 py-1.5">
                    <InstrumentLink symbol={n.symbol} flag={false} />
                    {n.name ? <div className="max-w-[16rem] truncate text-xs text-muted-foreground">{n.name}</div> : null}
                  </td>
                  <td className="px-3 py-1.5 text-muted-foreground">{n.sector ?? EMPTY_CELL}</td>
                  <td className="px-3 py-1.5">{formatNumber(n.close, { decimals: 2 })}</td>
                  <td className="px-3 py-1.5">
                    {n.turnover20 === null ? EMPTY_CELL : formatNumber(Number(n.turnover20) / 1e7, { decimals: 1, suffix: " cr" })}
                  </td>
                  <td className={`px-3 py-1.5 ${tone(n.r252_fraction)}`}>{formatFraction(n.r252_fraction, 0)}</td>
                  <td className="px-3 py-1.5">{formatFraction(n.dist_ath_fraction, 0)}</td>
                  <td className="px-3 py-1.5">{age === null ? EMPTY_CELL : formatNumber(age, { decimals: 1, suffix: " y" })}</td>
                  <td className={`px-3 py-1.5 ${tone(n.ttm_rev_yoy_fraction)}`}>{formatFraction(n.ttm_rev_yoy_fraction, 0)}</td>
                  <td className={`px-3 py-1.5 ${tone(n.ttm_pat_yoy_fraction)}`}>{formatFraction(n.ttm_pat_yoy_fraction, 0)}</td>
                  <td className="px-3 py-1.5">
                    <SaveInstrumentButton
                      symbol={n.symbol}
                      initiallySaved={watchedSet.has(n.symbol.toUpperCase())}
                      savedLabel="On watchlist"
                      unsavedLabel="Add to watchlist"
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {rows.length > shown ? (
        <button
          type="button"
          className="rounded-md border border-border px-3 py-1.5 text-sm"
          onClick={() => setShown(shown + PAGE)}
        >
          Show {Math.min(PAGE, rows.length - shown)} more ({formatNumber(rows.length - shown)} left)
        </button>
      ) : null}
    </div>
  );
}
