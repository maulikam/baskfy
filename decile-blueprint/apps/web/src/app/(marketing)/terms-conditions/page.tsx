import type { Metadata } from "next";

import { LegalPage } from "@/components/marketing/legal-page";
import Body from "@/content/legal/terms-conditions.mdx";
import { legalDocument } from "@/lib/marketing/legal";

/**
 * `/terms-conditions` — docs/11 §"Compliance & legal (India)": "Terms & Conditions, Privacy Policy,
 * Refund Policy pages before taking a single payment."
 *
 * The text lives in `src/content/legal/terms-conditions.mdx`, which opens with the
 * **DRAFT REQUIRING LEGAL REVIEW** marker Prompt 18 §3 asks for. The marker is an MDX comment, so
 * it is unmissable in the repository and absent from the rendered page.
 */
const DOCUMENT = legalDocument("terms-conditions");

export const metadata: Metadata = {
  title: DOCUMENT.title,
  description: DOCUMENT.description,
  alternates: { canonical: "/terms-conditions" },
};

export default function Page() {
  return (
    <LegalPage title={DOCUMENT.title}>
      <Body />
    </LegalPage>
  );
}
