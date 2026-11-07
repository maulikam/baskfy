"use client";

import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import { createContext, Fragment, useContext, useState, type ReactNode } from "react";

import {
  nextRegimeSort,
  orderByRegime,
  type RegimeSignal,
  type RegimeSort,
} from "@/lib/regime-signal/types";
import { cn } from "@/lib/utils";

interface RegimeTableState {
  sort: RegimeSort;
  setSort: (sort: RegimeSort) => void;
  greenOnly: boolean;
  setGreenOnly: (value: boolean) => void;
}

const RegimeTableContext = createContext<RegimeTableState | null>(null);

function useRegimeTable(): RegimeTableState {
  const state = useContext(RegimeTableContext);
  if (!state) throw new Error("Regime table controls must sit inside <RegimeTable>.");
  return state;
}

/** Controlled "Green only" toggle, shared by the server-rendered tables and the opportunities table. */
export function GreenOnlyToggle({
  pressed,
  onChange,
  className,
}: {
  pressed: boolean;
  onChange: (value: boolean) => void;
  className?: string;
}) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      data-testid="regime-green-only"
      onClick={() => onChange(!pressed)}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs",
        pressed
          ? "border-foreground bg-foreground text-background"
          : "border-border text-muted-foreground hover:border-foreground/60",
        className,
      )}
    >
      <span aria-hidden="true" className="block size-2 rounded-full bg-positive" />
      Green only
    </button>
  );
}

/** Sort-state icon matching `SortableHeader`'s. */
export function RegimeSortIcon({ sort }: { sort: RegimeSort }) {
  if (sort === "green") return <ArrowDown className="size-3" aria-hidden />;
  if (sort === "red") return <ArrowUp className="size-3" aria-hidden />;
  return <ArrowUpDown className="size-3 opacity-40" aria-hidden />;
}

/** Wraps one server-rendered table: holds its regime sort and "Green only" state, renders the toggle. */
export function RegimeTable({ children }: { children: ReactNode }) {
  const [sort, setSort] = useState<RegimeSort>("none");
  const [greenOnly, setGreenOnly] = useState(false);
  return (
    <RegimeTableContext.Provider value={{ sort, setSort, greenOnly, setGreenOnly }}>
      <div className="space-y-2">
        <div className="flex justify-end">
          <GreenOnlyToggle pressed={greenOnly} onChange={setGreenOnly} />
        </div>
        {children}
      </div>
    </RegimeTableContext.Provider>
  );
}

/** The first column's heading: a sort button, GREEN first on the first press. */
export function RegimeSortHeader({ className }: { className?: string }) {
  const { sort, setSort } = useRegimeTable();
  const ariaSort = sort === "green" ? "descending" : sort === "red" ? "ascending" : "none";
  return (
    <th scope="col" aria-sort={ariaSort} className={cn("w-10 py-2 pr-2 font-medium", className)}>
      <button
        type="button"
        className="inline-flex items-center gap-1 uppercase hover:text-foreground"
        onClick={() => setSort(nextRegimeSort(sort))}
        title="Sort by market regime, green first"
      >
        Regime
        <RegimeSortIcon sort={sort} />
      </button>
    </th>
  );
}

export interface RegimeRow {
  key: string;
  signal: RegimeSignal | null | undefined;
  node: ReactNode;
}

/** Server-rendered rows, reordered and filtered by the table's regime state. */
export function RegimeRows({ rows, colSpan }: { rows: readonly RegimeRow[]; colSpan: number }) {
  const { sort, greenOnly } = useRegimeTable();
  const ordered = orderByRegime(rows, (row) => row.signal, sort, greenOnly);
  if (ordered.length === 0 && rows.length > 0) {
    return (
      <tr>
        <td colSpan={colSpan} className="py-3 text-sm text-muted-foreground" data-testid="regime-none-green">
          No stock in this list has a green market regime. Turn off &ldquo;Green only&rdquo; to see all{" "}
          {rows.length}.
        </td>
      </tr>
    );
  }
  return (
    <>
      {ordered.map((row) => (
        <Fragment key={row.key}>{row.node}</Fragment>
      ))}
    </>
  );
}
