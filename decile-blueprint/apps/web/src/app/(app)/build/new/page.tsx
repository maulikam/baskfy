import type { Metadata } from "next";

import { NewScreenRedirect } from "@/components/screens/new-screen-redirect";

export const metadata: Metadata = {
  title: "New screen",
  robots: { index: false, follow: false },
};

/** docs/08 §Routes lists `/screens/new`; creation is POST + redirect to the saved id. */
export default function BuildNewScreenPage() {
  return <NewScreenRedirect />;
}
