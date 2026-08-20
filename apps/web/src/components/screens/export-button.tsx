"use client";

import { isProblem } from "@decile/api-client";
import { Download, Loader2 } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";

/**
 * docs/07: "GET /screens/{public_id}/csv?as_of=… → text/csv (entitlement-gated)".
 *
 * Prompt 9 deliverable 6: "Export button hitting /screens/{id}/csv, gated by entitlement with an
 * upgrade prompt on 402."
 *
 * The download is fetched rather than linked, because a plain `<a href>` cannot carry the bearer
 * token and — more importantly — cannot see a 402. A link to a gated endpoint either downloads a
 * problem+json document with a `.csv` name or navigates away from the screen; neither is an
 * upgrade prompt. Fetching lets the 402's own `upgrade_url` drive the dialog (docs/07
 * §Entitlements), so the paywall's destination comes from the server rather than being hard-coded.
 */
const UPGRADE_FALLBACK = "/pricing";

export interface ExportButtonProps {
  publicId: string;
  screenName: string;
  asOf: string | null;
  disabled?: boolean | undefined;
}

interface Paywall {
  detail: string;
  upgradeUrl: string;
}

export function ExportButton({ publicId, screenName, asOf, disabled }: ExportButtonProps) {
  const [busy, setBusy] = useState(false);
  const [paywall, setPaywall] = useState<Paywall | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  async function download() {
    setBusy(true);
    setFailure(null);
    try {
      const url = new URL(`${apiOrigin()}/api/v1/screens/${publicId}/csv`);
      if (asOf) url.searchParams.set("as_of", asOf);
      const token = await accessToken();
      const response = await fetch(url, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });

      if (response.status === 402) {
        const body: unknown = await response.json();
        const upgrade = isProblem(body)
          ? ((body as Record<string, unknown>).upgrade_url as string | undefined)
          : undefined;
        setPaywall({
          detail: isProblem(body) ? body.detail : "Export is not included in your plan.",
          upgradeUrl: upgrade ?? UPGRADE_FALLBACK,
        });
        return;
      }
      if (!response.ok) {
        const body: unknown = await response.json().catch(() => null);
        setFailure(isProblem(body) ? body.detail : "The export could not be generated.");
        return;
      }

      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = filenameFrom(response.headers.get("content-disposition"), screenName, asOf);
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(objectUrl);
    } catch {
      setFailure("The export could not be generated.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button
        variant="outline"
        size="sm"
        disabled={disabled || busy}
        onClick={() => void download()}
        data-testid="export-csv"
      >
        {busy ? (
          <Loader2 aria-hidden="true" className="animate-spin" />
        ) : (
          <Download aria-hidden="true" />
        )}
        Export
      </Button>

      {failure ? (
        <p role="alert" className="text-xs text-negative">
          {failure}
        </p>
      ) : null}

      <Dialog open={paywall !== null} onOpenChange={(open) => (open ? undefined : setPaywall(null))}>
        <DialogContent className="max-w-md">
          <DialogTitle className="text-base font-semibold">CSV export is a paid feature</DialogTitle>
          <DialogDescription className="mt-2 text-sm text-muted-foreground">
            {paywall?.detail}
          </DialogDescription>
          <div className="mt-4 flex gap-2">
            <Button variant="primary" size="sm" asChild>
              {/*
                A plain anchor, not `next/link`: the destination comes from the 402's own
                `upgrade_url` (docs/07 §Entitlements), so it is a runtime string that `typedRoutes`
                cannot check — and one a future deployment could point off-site.
              */}
              <a href={paywall?.upgradeUrl ?? UPGRADE_FALLBACK}>See plans</a>
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setPaywall(null)}>
              Not now
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

/** The server names the file (`Content-Disposition`); this is the fallback if it did not. */
function filenameFrom(header: string | null, screenName: string, asOf: string | null): string {
  const match = header ? /filename="([^"]+)"/.exec(header) : null;
  if (match?.[1]) return match[1];
  const slug = screenName.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  return `${slug || "screen"}-${asOf ?? "latest"}.csv`;
}
