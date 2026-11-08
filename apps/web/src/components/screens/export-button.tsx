"use client";

import { isProblem } from "@baskfy/api-client";
import { Download, Loader2 } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";

/**
 * Export menu — CSV stays one level down (§3.3). Share lives as a sibling control.
 */
const UPGRADE_FALLBACK = "/pricing";

export interface ExportButtonProps {
  publicId: string;
  screenName: string;
  asOf: string | null;
  disabled?: boolean | undefined;
  iconOnly?: boolean | undefined;
}

interface Paywall {
  detail: string;
  upgradeUrl: string;
}

export function ExportButton({ publicId, screenName, asOf, disabled, iconOnly }: ExportButtonProps) {
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
    <div className="relative">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="outline"
            size={iconOnly ? "icon" : "sm"}
            disabled={disabled || busy}
            data-testid="export-menu"
            aria-label="Export"
          >
            {busy ? (
              <Loader2 aria-hidden="true" className="animate-spin" />
            ) : (
              <Download aria-hidden="true" />
            )}
            {iconOnly ? null : "Export"}
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuLabel>Export</DropdownMenuLabel>
          <DropdownMenuItem
            data-testid="export-csv"
            disabled={busy}
            onSelect={() => void download()}
          >
            <Download aria-hidden="true" className="size-4" />
            Download CSV
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      {failure ? (
        <p
          role="alert"
          className="absolute right-0 top-full z-20 mt-1 max-w-56 text-pretty text-xs text-negative"
        >
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
              <a href={paywall?.upgradeUrl ?? UPGRADE_FALLBACK}>See plans</a>
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setPaywall(null)}>
              Not now
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function filenameFrom(header: string | null, screenName: string, asOf: string | null): string {
  const match = header ? /filename="([^"]+)"/.exec(header) : null;
  if (match?.[1]) return match[1];
  const slug = screenName.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  return `${slug || "screen"}-${asOf ?? "latest"}.csv`;
}
