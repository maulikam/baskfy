"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "next-themes";
import { NuqsAdapter } from "nuqs/adapters/next/app";
import { useState, type ReactNode } from "react";

import { TooltipProvider } from "@/components/ui/tooltip";

/**
 * Client-side providers — Prompt 8 deliverable 4: "TanStack Query provider, the generated API
 * client wired with auth headers, and nuqs for URL-synced state."
 *
 * `useState` rather than a module-level `new QueryClient()`: on the server a module-level client
 * is shared by every request, which leaks one user's cached screen results into another's page.
 *
 * Cache policy follows the shape of the data. docs/06 §Caching keys server-side results on
 * `data_version`, and that version only moves at the nightly publish (docs/03 step 10) — so
 * refetching on window focus buys nothing and costs a request per tab switch. Retries exclude
 * 4xx, because a 402 or a 422 is an answer, not a failure to be repeated three times.
 */

const ONE_MINUTE = 60_000;
const CLIENT_ERROR_FLOOR = 400;
const SERVER_ERROR_FLOOR = 500;
const MAX_RETRIES = 2;

function isClientError(error: unknown): boolean {
  if (typeof error !== "object" || error === null) return false;
  const status = (error as { status?: unknown }).status;
  return (
    typeof status === "number" && status >= CLIENT_ERROR_FLOOR && status < SERVER_ERROR_FLOOR
  );
}

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5 * ONE_MINUTE,
        gcTime: 30 * ONE_MINUTE,
        refetchOnWindowFocus: false,
        retry: (failureCount, error) => !isClientError(error) && failureCount < MAX_RETRIES,
      },
    },
  });
}

export function Providers({
  children,
  nonce,
}: {
  children: ReactNode;
  nonce?: string | undefined;
}) {
  const [queryClient] = useState(makeQueryClient);

  return (
    <ThemeProvider
      /*
       * docs/11 §Security asks for a strict CSP, and `next-themes` writes an inline script before
       * hydration to prevent the light-then-dark flash. An inline script under a nonce policy
       * needs the nonce; the middleware generates one per request and the root layout passes it
       * down. Without this the theme script is blocked and the flash comes back.
       */
      /* `exactOptionalPropertyTypes` makes `nonce={undefined}` a type error against a
         `nonce?: string` prop, so the absent case omits the prop rather than passing undefined. */
      {...(nonce ? { nonce } : {})}
      attribute="class"
      defaultTheme="system"
      enableSystem
      /* Prompt 8 acceptance criterion: "No layout shift on theme toggle". Suppressing the
         transition also stops every token animating at once, which reads as a flash. */
      disableTransitionOnChange
    >
      <QueryClientProvider client={queryClient}>
        <NuqsAdapter>
          <TooltipProvider delayDuration={200}>{children}</TooltipProvider>
        </NuqsAdapter>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
