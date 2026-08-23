import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { CustomizeForm } from "@/components/investments/customize-form";
import { PageHeader } from "@/components/shell/page-header";
import { fetchInvestment } from "@/lib/investments/fetch";

/**
 * `/investments/[id]/customize` — leaf 4.7. Weight-diff → CUSTOMIZE plan preview.
 * No execute / OrderGateway.
 */

export const dynamic = "force-dynamic";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ id: string }>;
}): Promise<Metadata> {
  const { id } = await params;
  const detail = await fetchInvestment(id);
  return {
    title: detail ? `Customize · ${detail.basket_name}` : "Customize",
    robots: { index: false, follow: false },
  };
}

export default async function CustomizeInvestmentPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const detail = await fetchInvestment(id);
  // Allow the form even when the investments API is empty — id is path-scoped.
  const basketName = detail?.basket_name ?? `Investment ${id}`;
  if (id.trim() === "") notFound();

  return (
    <div className="flex max-w-xl flex-col gap-6">
      <PageHeader
        title="Customize constituents"
        blurb="Edit target weights for this investment. Preview builds a CUSTOMIZE order plan — it does not place orders."
        meta={
          <Link
            href={`/investments/${id}`}
            className="text-xs text-muted-foreground underline-offset-4 hover:underline"
          >
            Back to {basketName}
          </Link>
        }
      />
      <CustomizeForm investmentId={id} basketName={basketName} />
    </div>
  );
}
