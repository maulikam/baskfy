import type { Metadata } from "next";
import Link from "next/link";

import { confirmEmail } from "@/lib/auth/api-auth";

export const metadata: Metadata = {
  title: "Confirm your email",
  robots: { index: false, follow: false },
};

/**
 * The page the confirmation link in the registration email opens.
 *
 * The token is exchanged on the *server*, during the render, so the address is confirmed by
 * opening the link — no button to press, and no token in a client bundle. `POST /auth/verify-email`
 * is idempotent-ish: it burns the token, so a second visit says the link has been used, which is
 * the truth.
 */
export default async function VerifyEmailPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const token = typeof params.token === "string" ? params.token : "";
  const result = token
    ? await confirmEmail(token)
    : { accepted: false, detail: "That confirmation link is incomplete." };

  return (
    <main className="space-y-6">
      <h1 className="text-xl font-semibold tracking-tight">
        {result.accepted ? "Email confirmed" : "We could not confirm that link"}
      </h1>
      <p className="text-sm text-muted-foreground">{result.detail}</p>
      <p className="text-sm">
        <Link href="/login" className="text-accent underline-offset-4 hover:underline">
          Continue to sign in
        </Link>
      </p>
    </main>
  );
}
