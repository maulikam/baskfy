import type { Metadata } from "next";

import { LegalPage } from "@/components/marketing/legal-page";
import Body from "@/content/legal/disclaimer.mdx";
import { legalDocument } from "@/lib/marketing/legal";

/**
 * `/disclaimer` — docs/11 §"Compliance & legal (India)": "Not a SEBI-registered investment adviser." The
 * one-sentence version is the `<Disclaimer/>` component rendered on every analytics surface; this
 * page is the long form of the same statement and must never contradict it.
 *
 * The text lives in `src/content/legal/disclaimer.mdx`, which opens with the
 * **DRAFT REQUIRING LEGAL REVIEW** marker Prompt 18 §3 asks for. The marker is an MDX comment, so
 * it is unmissable in the repository and absent from the rendered page.
 */
const DOCUMENT = legalDocument("disclaimer");

export const metadata: Metadata = {
  title: DOCUMENT.title,
  description: DOCUMENT.description,
  alternates: { canonical: "/disclaimer" },
};

export default function Page() {
  return (
    <LegalPage title={DOCUMENT.title}>
      <Body />
    </LegalPage>
  );
}
