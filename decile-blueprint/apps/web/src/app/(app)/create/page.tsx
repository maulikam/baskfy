import type { Metadata } from "next";

import { CreateWorkspace } from "@/components/create/create-workspace";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { serverApi } from "@/lib/api/server";
import { pickDefaultScreenId } from "@/lib/create/screens";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/create` — SB2. Pick a screen (templates on first login, or one you built), size it, save.
 * The manual symbol list is still behind "Pick stocks yourself". No execute / OrderGateway.
 */

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/create"].title,
  description: PAGES["/create"].blurb,
  robots: { index: false, follow: false },
};

function first(value: string | string[] | undefined): string | undefined {
  if (Array.isArray(value)) return value[0];
  return value;
}

export default async function CreateBasketPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const api = await serverApi();
  const { data } = await api.GET("/api/v1/screens");
  const screens = data?.data ?? [];
  const requested = first(params.screen) ?? null;
  const page = PAGES["/create"];

  return (
    <div className="flex w-full min-w-0 flex-col gap-6">
      <SectionTabs section="baskets" />
      <PageHeader title={page.title} blurb={page.blurb} />
      <CreateWorkspace
        screens={screens}
        initialScreenId={pickDefaultScreenId(screens, requested)}
      />
    </div>
  );
}
