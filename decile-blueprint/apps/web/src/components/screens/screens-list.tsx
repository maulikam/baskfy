"use client";

import type { ScreenDefinition, ScreenOut } from "@decile/api-client";
import { Copy, Play, Plus, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { Disclaimer } from "@/components/data/disclaimer";
import { EmptyState } from "@/components/data/empty-state";
import { ErrorState } from "@/components/data/error-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import {
  useCreateScreen,
  useDeleteScreen,
  useDuplicateScreen,
  useFactors,
  useUniverses,
} from "@/lib/screens/queries";
import { defaultDefinition } from "@/lib/screens/defaults";
import { parseDefinition } from "@/lib/screens/url-state";

/**
 * docs/01 §1 and Prompt 9 deliverable 1:
 *
 *     "/screens — Example Screens and Your Screens sections, each card showing name, index,
 *      ranking factor, order (mirroring the reference product), plus Run / Duplicate / Delete."
 *
 * The two sections come from one payload: docs/07 says `GET /screens` returns "user screens +
 * example screens", and `is_example` separates them. `editable` (which the API computes) decides
 * whether Delete is offered at all — an example screen is read-only, and offering a button that
 * will 404 is worse than not offering it.
 */
export interface ScreensListProps {
  initial: ScreenOut[] | null;
  error: unknown;
}

export function ScreensList({ initial, error }: ScreensListProps) {
  const router = useRouter();
  const factors = useFactors();
  const universes = useUniverses();
  const duplicate = useDuplicateScreen();
  const remove = useDeleteScreen();
  const create = useCreateScreen();
  const [pendingDelete, setPendingDelete] = useState<ScreenOut | null>(null);
  const [screens, setScreens] = useState<ScreenOut[]>(initial ?? []);

  const factorLabel = useMemo(() => {
    const byKey = new Map((factors.data ?? []).map((factor) => [factor.key, factor.label]));
    return (key: string) => byKey.get(key) ?? key;
  }, [factors.data]);

  const universeName = useMemo(() => {
    const bySlug = new Map((universes.data ?? []).map((universe) => [universe.slug, universe.name]));
    return (slug: string) => bySlug.get(slug) ?? slug;
  }, [universes.data]);

  if (error) {
    return <ErrorState error={error} onRetry={() => router.refresh()} />;
  }

  const examples = screens.filter((screen) => screen.is_example);
  const mine = screens.filter((screen) => !screen.is_example);

  async function newScreen() {
    const created = await create.mutateAsync({
      name: `Untitled screen ${mine.length + 1}`,
      definition: defaultDefinition(),
    });
    setScreens((current) => [...current, created]);
    router.push(`/screens/${created.public_id}` as never);
  }

  async function duplicateScreen(screen: ScreenOut) {
    const copy = await duplicate.mutateAsync(screen.public_id);
    setScreens((current) => [...current, copy]);
    router.push(`/screens/${copy.public_id}` as never);
  }

  async function confirmDelete(screen: ScreenOut) {
    await remove.mutateAsync(screen.public_id);
    setScreens((current) => current.filter((entry) => entry.public_id !== screen.public_id));
    setPendingDelete(null);
  }

  return (
    <div className="space-y-10">
      <header className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Screens</h1>
          <p className="text-sm text-muted-foreground">
            Six read-only templates, plus whatever you save.
          </p>
        </div>
        <Button
          variant="primary"
          size="sm"
          className="ml-auto"
          disabled={create.isPending}
          onClick={() => void newScreen()}
          data-testid="new-screen"
        >
          <Plus aria-hidden="true" />
          New screen
        </Button>
      </header>

      {create.error ? <ErrorState error={create.error} /> : null}
      {duplicate.error ? <ErrorState error={duplicate.error} /> : null}
      {remove.error ? <ErrorState error={remove.error} /> : null}

      <Section
        title="Your Screens"
        testId="your-screens"
        empty={
          <EmptyState
            title="No screens yet"
            reason="Screens you save appear here. Start from a template — duplicating one keeps its filters and gives you a copy you can edit."
          />
        }
        screens={mine}
        factorLabel={factorLabel}
        universeName={universeName}
        onDuplicate={(screen) => void duplicateScreen(screen)}
        onDelete={setPendingDelete}
        loading={initial === null}
      />

      <Section
        title="Example Screens"
        testId="example-screens"
        screens={examples}
        factorLabel={factorLabel}
        universeName={universeName}
        onDuplicate={(screen) => void duplicateScreen(screen)}
        onDelete={setPendingDelete}
        loading={initial === null}
      />

      <Dialog
        open={pendingDelete !== null}
        onOpenChange={(open) => (open ? undefined : setPendingDelete(null))}
      >
        <DialogContent className="max-w-md">
          <DialogTitle className="text-base font-semibold">
            Delete “{pendingDelete?.name}”?
          </DialogTitle>
          <DialogDescription className="mt-2 text-sm text-muted-foreground">
            This removes the screen and its run history. It cannot be undone.
          </DialogDescription>
          <div className="mt-4 flex gap-2">
            <Button
              variant="destructive"
              size="sm"
              disabled={remove.isPending}
              data-testid="confirm-delete"
              onClick={() => pendingDelete && void confirmDelete(pendingDelete)}
            >
              Delete screen
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setPendingDelete(null)}>
              Keep it
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Disclaimer />
    </div>
  );
}

interface SectionProps {
  title: string;
  testId: string;
  screens: ScreenOut[];
  empty?: React.ReactNode;
  loading: boolean;
  factorLabel: (key: string) => string;
  universeName: (slug: string) => string;
  onDuplicate: (screen: ScreenOut) => void;
  onDelete: (screen: ScreenOut) => void;
}

function Section({
  title,
  testId,
  screens,
  empty,
  loading,
  factorLabel,
  universeName,
  onDuplicate,
  onDelete,
}: SectionProps) {
  return (
    <section aria-labelledby={`${testId}-heading`} data-testid={testId} className="space-y-3">
      <h2 id={`${testId}-heading`} className="text-sm font-semibold tracking-tight">
        {title}
      </h2>
      {loading ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((index) => (
            <Skeleton key={index} className="h-36 rounded-md" />
          ))}
        </div>
      ) : screens.length === 0 ? (
        (empty ?? null)
      ) : (
        <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {screens.map((screen) => (
            <li key={screen.public_id}>
              <ScreenCard
                screen={screen}
                factorLabel={factorLabel}
                universeName={universeName}
                onDuplicate={onDuplicate}
                onDelete={onDelete}
              />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

interface ScreenCardProps {
  screen: ScreenOut;
  factorLabel: (key: string) => string;
  universeName: (slug: string) => string;
  onDuplicate: (screen: ScreenOut) => void;
  onDelete: (screen: ScreenOut) => void;
}

/** docs/01 §2.1's own wording for the two directions. */
function directionLabel(definition: ScreenDefinition): string {
  return definition.sort_direction === "desc" ? "Highest to Lowest" : "Lowest to Highest";
}

function ScreenCard({
  screen,
  factorLabel,
  universeName,
  onDuplicate,
  onDelete,
}: ScreenCardProps) {
  const definition = parseDefinition(screen.definition);

  return (
    <article
      data-testid="screen-card"
      data-screen={screen.public_id}
      className="flex h-full flex-col gap-3 rounded-md border border-border bg-card p-4"
    >
      <div className="flex items-start justify-between gap-2">
        <h3 className="font-medium leading-tight">
          <Link
            href={`/screens/${screen.public_id}`}
            className="underline-offset-4 hover:underline"
          >
            {screen.name}
          </Link>
        </h3>
        {screen.is_example ? <Badge>Template</Badge> : null}
      </div>

      <dl className="space-y-1 text-xs text-muted-foreground">
        <div className="flex gap-2">
          <dt className="w-16 shrink-0">Index</dt>
          <dd className="truncate text-foreground">{universeName(definition.index)}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="w-16 shrink-0">Factor</dt>
          <dd className="truncate text-foreground">{factorLabel(definition.sort_by)}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="w-16 shrink-0">Order</dt>
          <dd className="truncate text-foreground">{directionLabel(definition)}</dd>
        </div>
      </dl>

      <div className="mt-auto flex flex-wrap gap-2 pt-1">
        <Button variant="outline" size="sm" asChild>
          <Link href={`/screens/${screen.public_id}`}>
            <Play aria-hidden="true" />
            Run
          </Link>
        </Button>
        <Button variant="ghost" size="sm" onClick={() => onDuplicate(screen)}>
          <Copy aria-hidden="true" />
          Duplicate
        </Button>
        {screen.editable ? (
          <Button
            variant="ghost"
            size="sm"
            className="text-negative hover:bg-negative-muted"
            data-testid="delete-screen"
            onClick={() => onDelete(screen)}
          >
            <Trash2 aria-hidden="true" />
            Delete
          </Button>
        ) : null}
      </div>
    </article>
  );
}
