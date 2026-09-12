"use client";

import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useEffect, useRef } from "react";

import { ErrorState } from "@/components/data/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { useCreateScreen } from "@/lib/screens/queries";
import { defaultDefinition } from "@/lib/screens/defaults";

/**
 * `/build/new` and the legacy `/screens/new` redirect target.
 *
 * docs/09a §10: there is no unsaved-screen editor — "New screen" creates via `POST /screens`
 * and navigates to the issued id. This page does that on mount so bookmarked `/screens/new` links
 * land in the create flow instead of `/build/new` being treated as a screen id.
 */
export function NewScreenRedirect() {
  const router = useRouter();
  const create = useCreateScreen();
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void (async () => {
      const created = await create.mutateAsync({
        name: "New screen",
        definition: defaultDefinition(),
      });
      router.replace(`/build/${created.public_id}` as Route);
    })();
  }, [create, router]);

  if (create.error) {
    return <ErrorState error={create.error} onRetry={() => router.replace("/build")} />;
  }

  return (
    <div className="space-y-3" aria-busy="true" aria-live="polite">
      <Skeleton className="h-8 w-48" />
      <Skeleton className="h-4 w-96 max-w-full" />
      <p className="text-sm text-muted-foreground">Creating your screen…</p>
    </div>
  );
}
