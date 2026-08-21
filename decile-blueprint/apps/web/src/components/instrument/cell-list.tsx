import type { CellOut } from "@baskfy/api-client";

import { display } from "@/lib/instrument/present";
import { cn } from "@/lib/utils";

/**
 * A labelled list of cells — docs/01 §5 blocks 2 and 5 ("Key stats", "Price & Moving Averages").
 *
 * A `<dl>` rather than a table: these are name/value pairs, not a grid, and the semantics are what
 * a screen reader announces. Every value goes through `display()`, so a `null` is an em dash here
 * exactly as it is everywhere else.
 */
export interface CellListProps {
  cells: readonly CellOut[];
  columns?: 2 | 3 | 4;
  className?: string | undefined;
}

export function CellList({ cells, columns = 3, className }: CellListProps) {
  return (
    <dl
      className={cn(
        "grid gap-x-6 gap-y-3",
        columns === 2 && "grid-cols-2",
        columns === 3 && "grid-cols-2 sm:grid-cols-3",
        columns === 4 && "grid-cols-2 sm:grid-cols-4",
        className,
      )}
    >
      {cells.map((cell) => (
        <div key={cell.key} className="flex flex-col gap-0.5">
          <dt className="text-xs text-muted-foreground">{cell.label}</dt>
          <dd className="text-sm font-medium tnum">{display(cell)}</dd>
        </div>
      ))}
    </dl>
  );
}
