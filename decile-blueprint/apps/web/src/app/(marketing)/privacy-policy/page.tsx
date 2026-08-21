import type { Metadata } from "next";

import { LegalPage } from "@/components/marketing/legal-page";
import Body from "@/content/legal/privacy-policy.mdx";
import { legalDocument } from "@/lib/marketing/legal";

/**
 * `/privacy-policy` — docs/11 §"Compliance & legal (India)": "DPDP Act: consent record, data export and
 * deletion endpoints, breach notification runbook."
 *
 * The text lives in `src/content/legal/privacy-policy.mdx`, which opens with the
 * **DRAFT REQUIRING LEGAL REVIEW** marker Prompt 18 §3 asks for. The marker is an MDX comment, so
 * it is unmissable in the repository and absent from the rendered page.
 */
const DOCUMENT = legalDocument("privacy-policy");

export const metadata: Metadata = {
  title: DOCUMENT.title,
  description: DOCUMENT.description,
  alternates: { canonical: "/privacy-policy" },
};

export default function Page() {
  return (
    <LegalPage title={DOCUMENT.title}>
      <Body />
    </LegalPage>
  );
}
