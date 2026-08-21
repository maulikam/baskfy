import type { Metadata } from "next";
import Link from "next/link";

import { ResetPasswordForm } from "@/app/(auth)/reset-password/reset-form";

export const metadata: Metadata = {
  title: "Choose a new password",
  robots: { index: false, follow: false },
};

/**
 * `POST /auth/reset-password` — the page the link in the reset email opens.
 *
 * The token is read from the query string and posted back with the new password. It is never
 * shown, never stored client-side, and works once: the API burns it on use.
 */
export default async function ResetPasswordPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const token = typeof params.token === "string" ? params.token : "";

  return (
    <main className="space-y-6">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">Choose a new password</h1>
        <p className="text-sm text-muted-foreground">
          {token
            ? "Setting a new password signs you in and ends every other session."
            : "That link is incomplete. Request a new one and try again."}
        </p>
      </div>

      {token ? (
        <ResetPasswordForm token={token} />
      ) : (
        <p className="text-sm">
          <Link href="/forgot-password" className="text-accent underline-offset-4 hover:underline">
            Request a new reset link
          </Link>
        </p>
      )}
    </main>
  );
}
