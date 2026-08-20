import type { Metadata } from "next";

import { KitchenSink } from "@/app/(app)/kitchen-sink/kitchen-sink";

/**
 * Prompt 8 deliverable 3: every core primitive "with stories or a /kitchen-sink route".
 *
 * A route rather than Storybook: it exercises the primitives inside the real shell, with the real
 * theme and the real fonts, which is what the acceptance criteria measure — Lighthouse runs
 * against a page, and the 4,000-row scroll trace needs a real table in a real scroll container.
 */
export const metadata: Metadata = {
  title: "Kitchen sink",
  description: "Every core interface primitive, rendered in both themes.",
  robots: { index: false, follow: false },
};

export default function KitchenSinkPage() {
  return <KitchenSink />;
}
