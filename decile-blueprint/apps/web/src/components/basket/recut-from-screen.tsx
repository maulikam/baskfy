"use client";

import type { Route } from "next";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { CreateBasketError, recutBasketFromScreen, type RecutResponse } from "@/lib/create/fetch";

/**
 * "Re-cut from screen" — Maulik, 14 Oct 2026: a basket saved from a screen is a snapshot, so
 * re-run the screen on demand, show what was added and removed, and save a new version only when
 * he says so. Two clicks: the first previews (nothing written), the second saves.
 *
 * No order path. Saving changes the basket's holdings; buying them is still the Invest hand-off.
 */

type State =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "preview"; result: RecutResponse }
  | { status: "saving"; result: RecutResponse }
  | { status: "saved"; result: RecutResponse }
  | { status: "failed"; message: string };

function names(list: readonly string[]): string {
  return list.length ? list.join(", ") : "none";
}

export function RecutFromScreen({ slug }: { slug: string }) {
  const router = useRouter();
  const [state, setState] = useState<State>({ status: "idle" });

  async function run(confirm: boolean, previous?: RecutResponse) {
    setState(previous ? { status: "saving", result: previous } : { status: "loading" });
    try {
      const result = await recutBasketFromScreen(slug, confirm);
      setState({ status: confirm ? "saved" : "preview", result });
      if (confirm && result.written) router.refresh();
    } catch (error) {
      setState({
        status: "failed",
        message:
          error instanceof CreateBasketError ? error.message : "Could not re-run the screen.",
      });
    }
  }

  const result =
    state.status === "preview" || state.status === "saving" || state.status === "saved"
      ? state.result
      : null;

  return (
    <section
      aria-label="Re-cut from screen"
      data-testid="recut-from-screen"
      className="min-w-0 space-y-3 rounded-xl border border-border/70 bg-card px-4 py-3 text-sm"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-muted-foreground">
          Saved from a screen. Re-run it to see which names it picks today.
        </p>
        <Button
          type="button"
          variant="outline"
          disabled={state.status === "loading" || state.status === "saving"}
          onClick={() => void run(false)}
        >
          {state.status === "loading" ? "Running the screen…" : "Re-cut from screen"}
        </Button>
      </div>

      {result ? (
        <div className="space-y-2" data-testid="recut-preview">
          <p className="text-xs text-muted-foreground">
            Screen as of {result.as_of} · {result.holdings.length} names · {result.method} weights
            {result.method_fell_back ? " (custom weights can't carry over to new names)" : ""}
          </p>
          {result.changed ? (
            <ul className="space-y-1">
              <li>
                <span className="font-medium text-positive">Added:</span> {names(result.added)}
              </li>
              <li>
                <span className="font-medium text-negative">Removed:</span>{" "}
                {names(result.removed)}
              </li>
              <li className="text-muted-foreground">Kept: {names(result.kept)}</li>
            </ul>
          ) : (
            <p data-testid="recut-unchanged">
              No change — the screen picks the same names at the same weights as version{" "}
              {result.current_version_no}.
            </p>
          )}

          {state.status === "saved" && result.written ? (
            <p data-testid="recut-saved">
              Saved as version {result.version_no}.{" "}
              <Link
                href={`/basket/${slug}/versions` as Route}
                className="text-accent underline-offset-4 hover:underline"
              >
                See versions
              </Link>
            </p>
          ) : result.changed ? (
            <Button
              type="button"
              variant="primary"
              disabled={state.status === "saving"}
              onClick={() => void run(true, result)}
            >
              {state.status === "saving"
                ? "Saving…"
                : `Save as version ${result.current_version_no + 1}`}
            </Button>
          ) : null}
        </div>
      ) : null}

      {state.status === "failed" ? (
        <p className="text-warning-foreground" role="alert">
          {state.message}
        </p>
      ) : null}
    </section>
  );
}
