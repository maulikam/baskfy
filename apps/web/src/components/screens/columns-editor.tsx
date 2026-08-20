"use client";

import type { ColumnOut, ScreenOut } from "@decile/api-client";
import { ArrowDown, ArrowUp, GripVertical, Loader2, Save, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { ErrorState } from "@/components/data/error-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { groupColumns } from "@/lib/screens/column-families";
import { useSaveScreen } from "@/lib/screens/queries";
import { cn } from "@/lib/utils";

/**
 * docs/08 §"Columns editor":
 *
 *     "34 toggles in the reference's order, grouped by family, with drag-to-reorder and a live
 *      preview of the header row. Save writes `screen.columns`."
 *
 * It is thirty-six, not thirty-four — docs/01 §4 is headed "34 available columns" and then
 * enumerates thirty-six, the same arithmetic slip as the 62/64 factor count. The registry
 * implements every named key (`decile_core.factor_registry`), so the picker offers every one.
 *
 * **Reordering is keyboard-first.** HTML5 drag-and-drop is mouse-only: there is no keyboard event
 * that starts a drag, so a picker built on it alone is unusable for anyone who does not use a
 * pointer — and docs/08 §"Accessibility & quality bar" requires "All interactive elements keyboard
 * reachable". Each selected column therefore has explicit Move-up / Move-down buttons, and the
 * pointer drag is layered on top for people who prefer it.
 */
export interface ColumnsEditorProps {
  screen: ScreenOut;
  columns: readonly ColumnOut[];
}

/**
 * The three columns the API always projects (`decile_core.screener.IDENTITY_COLUMNS`). They are
 * shown in the preview but are not toggles: a results table with no symbol is not a results table.
 */
const ALWAYS_PRESENT = ["symbol", "name", "sorting_factor"] as const;
const ALWAYS_PRESENT_LABELS: Record<(typeof ALWAYS_PRESENT)[number], string> = {
  symbol: "Symbol",
  name: "Name",
  sorting_factor: "Sorting Factor",
};

export function ColumnsEditor({ screen, columns }: ColumnsEditorProps) {
  const router = useRouter();
  const save = useSaveScreen();
  const [selected, setSelected] = useState<string[]>(() => [...screen.columns]);
  const [dragging, setDragging] = useState<string | null>(null);

  const byKey = useMemo(() => new Map(columns.map((column) => [column.key, column])), [columns]);
  const groups = useMemo(() => groupColumns(columns), [columns]);
  const dirty = useMemo(
    () => JSON.stringify(selected) !== JSON.stringify(screen.columns),
    [selected, screen.columns],
  );

  function toggle(key: string, on: boolean) {
    setSelected((current) =>
      on ? [...current, key] : current.filter((entry) => entry !== key),
    );
  }

  function move(key: string, delta: number) {
    setSelected((current) => {
      const index = current.indexOf(key);
      const target = index + delta;
      if (index < 0 || target < 0 || target >= current.length) return current;
      const next = [...current];
      const [item] = next.splice(index, 1);
      if (item !== undefined) next.splice(target, 0, item);
      return next;
    });
  }

  function dropOnto(target: string) {
    if (dragging === null || dragging === target) return;
    setSelected((current) => {
      const from = current.indexOf(dragging);
      const to = current.indexOf(target);
      if (from < 0 || to < 0) return current;
      const next = [...current];
      const [item] = next.splice(from, 1);
      if (item !== undefined) next.splice(to, 0, item);
      return next;
    });
    setDragging(null);
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Edit columns</h1>
          <p className="text-sm text-muted-foreground">{screen.name}</p>
        </div>
        {dirty ? <Badge variant="warning">Unsaved changes</Badge> : null}
        <div className="ml-auto flex gap-2">
          <Button variant="ghost" size="sm" asChild>
            <Link href={`/screens/${screen.public_id}`}>Back to results</Link>
          </Button>
          <Button
            variant="primary"
            size="sm"
            data-testid="save-columns"
            disabled={!dirty || !screen.editable || save.isPending}
            onClick={() =>
              save.mutate(
                { publicId: screen.public_id, columns: selected },
                { onSuccess: () => router.push(`/screens/${screen.public_id}` as never) },
              )
            }
          >
            {save.isPending ? (
              <Loader2 aria-hidden="true" className="animate-spin" />
            ) : (
              <Save aria-hidden="true" />
            )}
            Save columns
          </Button>
        </div>
      </header>

      {screen.editable ? null : (
        <p className="rounded-md border border-border bg-muted/50 px-3 py-2 text-sm text-muted-foreground">
          Example screens are read-only. Duplicate the screen to save a column layout.
        </p>
      )}

      {save.error ? <ErrorState error={save.error} onRetry={() => save.reset()} /> : null}

      <section aria-labelledby="preview-heading" className="space-y-2">
        <h2 id="preview-heading" className="text-sm font-semibold tracking-tight">
          Header preview
        </h2>
        <div
          data-testid="header-preview"
          className="flex gap-px overflow-x-auto rounded-md border border-border bg-muted/60 p-px text-xs font-medium text-muted-foreground"
        >
          <span className="shrink-0 bg-card px-2 py-1.5">#</span>
          {ALWAYS_PRESENT.map((key) => (
            <span key={key} className="shrink-0 bg-card px-2 py-1.5">
              {ALWAYS_PRESENT_LABELS[key]}
            </span>
          ))}
          {selected.map((key) => (
            <span key={key} className="shrink-0 bg-card px-2 py-1.5">
              {byKey.get(key)?.label ?? key}
            </span>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">
          Rank, symbol, name and the sorting factor are always shown.
        </p>
      </section>

      <div className="grid gap-6 lg:grid-cols-2">
        <section aria-labelledby="chosen-heading" className="space-y-2">
          <h2 id="chosen-heading" className="text-sm font-semibold tracking-tight">
            Chosen columns, in order
          </h2>
          {selected.length === 0 ? (
            <p className="rounded-md border border-dashed border-border px-3 py-6 text-center text-sm text-muted-foreground">
              No optional columns. The table will show rank, symbol, name and the sorting factor.
            </p>
          ) : (
            <ol data-testid="chosen-columns" className="space-y-1">
              {selected.map((key, index) => (
                <li
                  key={key}
                  draggable
                  data-column={key}
                  onDragStart={() => setDragging(key)}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={() => dropOnto(key)}
                  onDragEnd={() => setDragging(null)}
                  className={cn(
                    "flex items-center gap-2 rounded-md border border-border bg-card px-2 py-1.5 text-sm",
                    dragging === key && "opacity-50",
                  )}
                >
                  <GripVertical
                    aria-hidden="true"
                    className="size-4 shrink-0 cursor-grab text-muted-foreground"
                  />
                  <span className="flex-1 truncate">{byKey.get(key)?.label ?? key}</span>
                  <span className="tnum text-xs text-muted-foreground">{index + 1}</span>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="size-7"
                    aria-label={`Move ${byKey.get(key)?.label ?? key} up`}
                    disabled={index === 0}
                    onClick={() => move(key, -1)}
                  >
                    <ArrowUp aria-hidden="true" className="size-3.5" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="size-7"
                    aria-label={`Move ${byKey.get(key)?.label ?? key} down`}
                    disabled={index === selected.length - 1}
                    onClick={() => move(key, 1)}
                  >
                    <ArrowDown aria-hidden="true" className="size-3.5" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="size-7"
                    aria-label={`Remove ${byKey.get(key)?.label ?? key}`}
                    onClick={() => toggle(key, false)}
                  >
                    <X aria-hidden="true" className="size-3.5" />
                  </Button>
                </li>
              ))}
            </ol>
          )}
        </section>

        <section aria-labelledby="available-heading" className="space-y-4">
          <h2 id="available-heading" className="text-sm font-semibold tracking-tight">
            Available columns ({columns.length})
          </h2>
          {groups.map((group) => (
            <fieldset key={group.family.id} className="space-y-1.5">
              <legend className="pb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                {group.family.label}
              </legend>
              {group.columns.map((column) => (
                <div key={column.key} className="flex items-center justify-between gap-3">
                  <Label htmlFor={`column-${column.key}`} className="font-normal">
                    {column.label}
                  </Label>
                  <Switch
                    id={`column-${column.key}`}
                    checked={selected.includes(column.key)}
                    onCheckedChange={(checked) => toggle(column.key, checked)}
                  />
                </div>
              ))}
            </fieldset>
          ))}
        </section>
      </div>
    </div>
  );
}
