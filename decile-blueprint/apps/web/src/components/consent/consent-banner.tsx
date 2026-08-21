"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { useIsMounted } from "@/lib/use-is-mounted";
import {
  ACCEPT_ALL,
  CONSENT_COOKIE,
  CONSENT_MAX_AGE_SECONDS,
  ESSENTIAL_ONLY,
  parseConsent,
  serialiseConsent,
  type ConsentRecord,
} from "@/lib/consent/state";

/** The raw cookie value, or `undefined`. Client-only — `document` does not exist on the server. */
function readConsentCookie(): string | undefined {
  return document.cookie
    .split("; ")
    .find((pair) => pair.startsWith(`${CONSENT_COOKIE}=`))
    ?.slice(CONSENT_COOKIE.length + 1);
}

/**
 * Prompt 18 deliverable 5 — "a cookie/consent banner that defaults to essential-only".
 *
 * Three properties, in the order they matter:
 *
 * 1. **It defaults to essential-only.** The two buttons are "Essential only" and "Allow all", and
 *    dismissing the banner without pressing either leaves the visitor on essential-only. There is
 *    no pre-ticked box and no implied consent — see `src/lib/consent/state.ts` for why.
 * 2. **It costs no layout shift.** It renders `null` on the server and on the first client paint,
 *    then mounts as a *fixed* element that takes no space in the document flow. Prompt 8's fourth
 *    acceptance criterion (CLS ≈ 0) is measured across the whole app, and a banner that pushed the
 *    page down on hydration would break it everywhere at once.
 * 3. **It is announced, not shouted.** `role="region"` with a label rather than `role="dialog"`:
 *    it does not trap focus and it does not block the page, because nothing here is a decision the
 *    visitor has to make before reading a legal page. The DPDP Act asks for a real choice, not a
 *    wall.
 */
export function ConsentBanner() {
  /*
   * The cookie is readable during the *client* render and unknowable during the server one, so the
   * mount flag decides which. `useIsMounted` is `useSyncExternalStore` rather than the usual
   * `useState` + `useEffect(setState)` pair, which `react-hooks/set-state-in-effect` rejects and
   * which would schedule a second render of the whole document on every page load.
   *
   * `chosen` holds a choice made *in this session*; before one is made the stored cookie is the
   * answer, and `null` from either means "no choice yet" and therefore essential-only.
   */
  const mounted = useIsMounted();
  const [chosen, setChosen] = useState<ConsentRecord | null>(null);
  const record = chosen ?? (mounted ? parseConsent(readConsentCookie()) : ESSENTIAL_ONLY);

  function choose(choice: ConsentRecord) {
    /*
     * `SameSite=Lax` and no `Secure` flag in development, because a `Secure` cookie is never
     * stored over plain HTTP and the banner would then reappear on every page. This cookie is
     * read by JavaScript by design — it gates client-side tags — so it is deliberately not
     * `httpOnly`, unlike every cookie in `decile_api.csrf`.
     */
    const secure = window.location.protocol === "https:" ? "; Secure" : "";
    document.cookie =
      `${CONSENT_COOKIE}=${serialiseConsent(choice)}; Path=/; Max-Age=${CONSENT_MAX_AGE_SECONDS}` +
      `; SameSite=Lax${secure}`;
    setChosen(choice);
  }

  if (record !== null) return null;

  return (
    <section
      aria-label="Cookie choices"
      className="fixed inset-x-0 bottom-0 z-50 border-t border-border bg-background/95 backdrop-blur"
    >
      <div className="mx-auto flex max-w-4xl flex-col gap-3 px-6 py-4 sm:flex-row sm:items-center sm:justify-between">
        <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">
          We set cookies that are needed to sign you in and to keep your session safe. We run no
          advertising or analytics tags today; if that ever changes it will be behind this choice.
          You can change it later on the{" "}
          <a className="underline underline-offset-2" href="/privacy-policy">
            privacy policy
          </a>{" "}
          page.
        </p>
        <div className="flex shrink-0 gap-2">
          <Button variant="outline" size="sm" onClick={() => choose(ESSENTIAL_ONLY)}>
            Essential only
          </Button>
          <Button variant="primary" size="sm" onClick={() => choose(ACCEPT_ALL)}>
            Allow all
          </Button>
        </div>
      </div>
    </section>
  );
}
