import type { Metadata } from "next";

import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/build/desk` — the operator console inside the web shell (LOCAL-3, 2 Nov 2026).
 *
 * Caddy already reverse-proxies same-origin `/desk` to the desk service. This page is the
 * AppShell chrome around that UI (iframe), so Maulik reaches it from Build rather than a
 * separate password-gated host. Nothing here places an order: POST /execute stays on the desk
 * origin. Do not add an execute route to this app.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/build/desk"].title,
  description: PAGES["/build/desk"].blurb,
  robots: { index: false, follow: false },
};

export default function BuildDeskPage() {
  return (
    <div className="flex flex-col gap-4">
      <PageHeader title={PAGES["/build/desk"].title} blurb={PAGES["/build/desk"].blurb} />
      <SectionTabs section="build" />
      <iframe
        title="Momentum desk"
        src="/desk/"
        className="h-[calc(100dvh-14rem)] w-full rounded-lg border border-border bg-background"
      />
    </div>
  );
}
