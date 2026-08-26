"use client";

import { useActionState } from "react";

import { signInWithGoogle, type FormResult } from "@/app/actions/auth";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * One button. That is the whole sign-in surface now (`docs/DECISIONS-MERGE.md` M46).
 *
 * What this replaced was two tabbed modes — a one-time code and a password — each with its own
 * form, its own status line and its own failure vocabulary. None of it survived Google becoming
 * the only way in, and the page is better for it: the thing most likely to go wrong on a sign-in
 * page is the user picking the wrong one of several ways in, and there is now only one.
 *
 * `next` is the destination the middleware's gate wants the user returned to. It is carried in a
 * hidden field and validated again in the action — a redirect target that arrived in a URL is
 * never trusted on the strength of having arrived in a URL.
 *
 * The status line stays a live region even though it is almost never populated: when Auth.js
 * cannot reach Google the failure is silent otherwise, and a sign-in button that does nothing
 * visible when clicked is the worst state this page can be in.
 */
export function LoginForm({ next }: { next?: string | undefined }) {
  const [result, action, pending] = useActionState<FormResult | null, FormData>(
    signInWithGoogle,
    null,
  );

  return (
    <div className="space-y-4">
      <form action={action} className="space-y-3">
        {next ? <input type="hidden" name="next" value={next} /> : null}
        <Button type="submit" className="w-full gap-2" size="lg" disabled={pending}>
          <GoogleMark />
          {pending ? "Taking you to Google…" : "Continue with Google"}
        </Button>
      </form>

      <p
        role="status"
        aria-live="polite"
        className={cn(
          "min-h-5 text-sm",
          result?.ok === false ? "text-negative" : "text-muted-foreground",
        )}
      >
        {result?.message ?? ""}
      </p>
    </div>
  );
}

/**
 * Google's mark, inline.
 *
 * Inline rather than an `<img>` from Google's CDN because the page must render identically with
 * no third-party request — the sign-in page is the one screen where a blocked external asset
 * would leave a button with no indication of what it does. `aria-hidden` because the button's
 * own text already says "Continue with Google"; announcing the logo as well would say it twice.
 */
function GoogleMark() {
  return (
    <svg viewBox="0 0 18 18" className="size-4 shrink-0" aria-hidden="true" focusable="false">
      <path
        fill="#4285F4"
        d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92c1.7-1.57 2.68-3.88 2.68-6.62Z"
      />
      <path
        fill="#34A853"
        d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.8.54-1.84.86-3.04.86-2.34 0-4.32-1.58-5.03-3.7H.96v2.33A9 9 0 0 0 9 18Z"
      />
      <path
        fill="#FBBC05"
        d="M3.97 10.72a5.4 5.4 0 0 1 0-3.44V4.95H.96a9 9 0 0 0 0 8.1l3.01-2.33Z"
      />
      <path
        fill="#EA4335"
        d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.58C13.46.89 11.43 0 9 0A9 9 0 0 0 .96 4.95l3.01 2.33C4.68 5.16 6.66 3.58 9 3.58Z"
      />
    </svg>
  );
}
