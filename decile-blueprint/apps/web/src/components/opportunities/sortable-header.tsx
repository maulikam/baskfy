import type { Header } from "@tanstack/react-table";
import { flexRender } from "@tanstack/react-table";
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";

export function SortableHeader<T>({ header }: { header: Header<T, unknown> }) {
  if (header.isPlaceholder) return <th className="py-2 pr-3" />;
  const canSort = header.column.getCanSort();
  const sorted = header.column.getIsSorted();
  const content = flexRender(header.column.columnDef.header, header.getContext());
  const ariaSort = sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : "none";
  return (
    <th
      className="whitespace-nowrap py-2 pr-3 text-left text-xs font-medium uppercase tracking-wide text-muted-foreground"
      {...(canSort ? { "aria-sort": ariaSort } : {})}
    >
      {canSort ? (
        <button
          type="button"
          className="inline-flex items-center gap-1 hover:text-foreground"
          onClick={header.column.getToggleSortingHandler()}
        >
          {content}
          {sorted === "asc" ? (
            <ArrowUp className="size-3" aria-hidden />
          ) : sorted === "desc" ? (
            <ArrowDown className="size-3" aria-hidden />
          ) : (
            <ArrowUpDown className="size-3 opacity-40" aria-hidden />
          )}
        </button>
      ) : (
        content
      )}
    </th>
  );
}
