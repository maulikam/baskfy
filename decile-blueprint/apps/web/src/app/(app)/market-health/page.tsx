import { redirect } from "next/navigation";

/** Tree 6: permanent consumer IA move — see next.config.ts. */
export default function LegacyRedirectPage() {
  redirect("/market/mood");
}
