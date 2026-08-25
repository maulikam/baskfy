"use client";

import { Check, Copy, Download, Loader2, Share2 } from "lucide-react";
import { useMemo, useRef, useState } from "react";

import type { ResultRow } from "@/components/screens/result-columns";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { formatPercent, formatTradeDate } from "@/lib/format";
import {
  canvasToBlob,
  renderShareCard,
  type ShareAspect,
} from "@/lib/screens/share-card";
import { cn } from "@/lib/utils";

export interface ShareButtonProps {
  screenName: string;
  storySentence: string;
  asOf: string | null;
  rows: readonly ResultRow[];
  disabled?: boolean;
  className?: string | undefined;
}

export function ShareButton({
  screenName,
  storySentence,
  asOf,
  rows,
  disabled,
  className,
}: ShareButtonProps) {
  const [open, setOpen] = useState(false);
  const [aspect, setAspect] = useState<ShareAspect>("story");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);

  const payload = useMemo(
    () => ({
      screenName,
      storySentence,
      asOf: formatTradeDate(asOf),
      rows: rows.slice(0, 10).map((row) => {
        const ret = typeof row.ret_12m === "number" ? formatPercent(row.ret_12m) : null;
        const score =
          typeof row.sorting_factor === "number"
            ? row.sorting_factor.toLocaleString("en-IN", { maximumFractionDigits: 2 })
            : null;
        const name = typeof row.name === "string" ? row.name : undefined;
        return {
          rank: typeof row.rank === "number" ? row.rank : 0,
          symbol: typeof row.symbol === "string" ? row.symbol : "—",
          ...(name ? { name } : {}),
          score,
          ret,
        };
      }),
    }),
    [screenName, storySentence, asOf, rows],
  );

  function paint(nextAspect: ShareAspect = aspect) {
    const canvas = canvasRef.current;
    if (!canvas) return;
    renderShareCard(canvas, nextAspect, payload);
  }

  function handleOpen(next: boolean) {
    setOpen(next);
    setError(null);
    setCopied(false);
    if (next) {
      // Dialog content mounts a frame later; a second rAF is the cheap reliable paint.
      requestAnimationFrame(() => {
        requestAnimationFrame(() => paint(aspect));
      });
    }
  }

  function switchAspect(next: ShareAspect) {
    setAspect(next);
    requestAnimationFrame(() => paint(next));
  }

  async function download() {
    const canvas = canvasRef.current;
    if (!canvas) return;
    setBusy(true);
    setError(null);
    try {
      paint();
      const blob = await canvasToBlob(canvas);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      const slug =
        screenName.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "screen";
      anchor.download = `baskfy-${slug}-${aspect}.png`;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch {
      setError("Could not download the share card.");
    } finally {
      setBusy(false);
    }
  }

  async function copyImage() {
    const canvas = canvasRef.current;
    if (!canvas) return;
    setBusy(true);
    setError(null);
    try {
      paint();
      const blob = await canvasToBlob(canvas);
      if (!("clipboard" in navigator) || !window.ClipboardItem) {
        setError("Copy-image needs a modern browser — download instead.");
        return;
      }
      await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("Could not copy the image. Try download.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button
        variant="outline"
        size="icon"
        disabled={disabled || rows.length === 0}
        onClick={() => handleOpen(true)}
        data-testid="share-screen"
        aria-label="Share screen card"
        className={className}
      >
        <Share2 aria-hidden="true" />
      </Button>

      <Dialog open={open} onOpenChange={handleOpen}>
        <DialogContent className="max-w-lg">
          <DialogTitle className="text-base font-semibold">Share this screen</DialogTitle>
          <DialogDescription className="text-sm text-muted-foreground">
            A story card with the top 10, the one-liner, Baskfy branding, and the disclaimer baked
            in.
          </DialogDescription>

          <div className="mt-3 flex gap-2">
            <Button
              size="sm"
              variant={aspect === "story" ? "primary" : "outline"}
              onClick={() => switchAspect("story")}
              data-testid="share-aspect-story"
            >
              9:16 story
            </Button>
            <Button
              size="sm"
              variant={aspect === "square" ? "primary" : "outline"}
              onClick={() => switchAspect("square")}
              data-testid="share-aspect-square"
            >
              1:1 square
            </Button>
          </div>

          <div
            className={cn(
              "mt-4 overflow-hidden rounded-md border border-border bg-[#14161a]",
              aspect === "story"
                ? "mx-auto aspect-[9/16] h-[min(28rem,70vh)] w-auto"
                : "mx-auto aspect-square w-full max-w-xs",
            )}
          >
            <canvas
              ref={canvasRef}
              className="h-full w-full object-contain"
              aria-label="Share card preview"
            />
          </div>

          {error ? (
            <p role="alert" className="mt-2 text-xs text-negative">
              {error}
            </p>
          ) : null}

          <div className="mt-4 flex flex-wrap gap-2">
            <Button
              variant="primary"
              size="sm"
              disabled={busy}
              onClick={() => void copyImage()}
              data-testid="share-copy"
            >
              {busy ? (
                <Loader2 aria-hidden="true" className="animate-spin" />
              ) : copied ? (
                <Check aria-hidden="true" />
              ) : (
                <Copy aria-hidden="true" />
              )}
              {copied ? "Copied" : "Copy image"}
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={() => void download()}
              data-testid="share-download"
            >
              <Download aria-hidden="true" />
              Download PNG
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
