"use client";

import { X } from "lucide-react";
import { useState, type ReactNode } from "react";

import { COOKIE_MAX_AGE_SECONDS, cookieName } from "@/components/shell/announcement";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * docs/08 §"App shell": "A dismissible announcement banner slot at the top (the reference product
 * uses it for the December 2026 update)."
 *
 * **Dismissal is a cookie, not `localStorage`.** That is the whole design.
 *
 * `localStorage` is unreadable on the server, so a banner gated on it can only appear *after*
 * hydration — which pushes the entire shell down by its own height, one frame late. Measured, that
 * was a CLS of 0.031 on every page load, and Prompt 8's fourth acceptance criterion is "No layout
 * shift on theme toggle or on data load". A cookie is sent with the request, so the server already
 * knows whether to render the banner and the first paint is already correct.
 *
 * Dismissing it does move the layout, but that shift follows a click and is excluded from CLS by
 * `hadRecentInput` — which is the distinction the metric exists to draw.
 */
export interface AnnouncementBannerProps {
  /** Change this when the message changes; dismissal is remembered per id. */
  id: string;
  children: ReactNode;
  action?: { label: string; href: string } | undefined;
  className?: string;
}

export function AnnouncementBanner({ id, children, action, className }: AnnouncementBannerProps) {
  const [dismissed, setDismissed] = useState(false);

  if (dismissed) return null;

  return (
    <div
      role="region"
      aria-label="Announcement"
      className={cn(
        "flex items-center gap-3 border-b border-border bg-accent-muted px-4 py-2 text-sm",
        className,
      )}
    >
      <p className="flex-1 text-foreground">{children}</p>
      {action ? (
        <Button variant="link" size="sm" asChild className="h-auto p-0">
          <a href={action.href}>{action.label}</a>
        </Button>
      ) : null}
      <Button
        variant="ghost"
        size="icon"
        className="size-7"
        aria-label="Dismiss announcement"
        onClick={() => {
          setDismissed(true);
          document.cookie = `${cookieName(id)}=1; path=/; max-age=${COOKIE_MAX_AGE_SECONDS}; samesite=lax`;
        }}
      >
        <X aria-hidden="true" className="size-4" />
      </Button>
    </div>
  );
}
