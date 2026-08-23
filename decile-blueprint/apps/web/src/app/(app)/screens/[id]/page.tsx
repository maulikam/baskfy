import { redirect } from "next/navigation";

export default async function LegacyScreenRedirect({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  redirect(`/build/${id}`);
}
