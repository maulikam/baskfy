import type { Metadata } from "next";
import Link from "next/link";

import { LoginForm } from "@/app/(auth)/login/login-form";

export const metadata: Metadata = {
  title: "Sign in",
  robots: { index: false, follow: false },
};

/**
 * Sign in — and, since Google became the only way in, sign *up* as well
 * (`docs/DECISIONS-MERGE.md` M46). There is no separate `/register` page any more: a first
 * sign-in creates the account, so a page asking people to choose between the two would be asking
 * about a distinction the system no longer makes.
 */
export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const next = typeof params.next === "string" ? params.next : undefined;
  const deleted = params.deleted === "1";

  return (
    <main className="space-y-6">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">Sign in</h1>
        <p className="text-sm text-muted-foreground">
          Continue with Google. If you have not been here before, this creates your account.
        </p>
      </div>

      {deleted ? (
        <p className="rounded-md border border-warning/30 bg-warning-muted px-3 py-2 text-sm">
          Your account is scheduled for deletion. Signing in again cancels it.
        </p>
      ) : null}
      {next ? (
        <p className="text-sm text-muted-foreground">Sign in to continue to {next}.</p>
      ) : null}

      <LoginForm next={next} />

      {/*
        The consent line. docs/11 §Compliance carries a DPDP consent record, and until this change
        it was collected as a mandatory checkbox on `/register`. The checkbox is gone (Maulik,
        27 Aug 2026); the record is not — `link_google_identity` writes it at first sign-in, with
        the document version, against this sentence. `docs/DECISIONS-MERGE.md` M46.2.
      */}
      <p className="text-sm text-muted-foreground">
        By continuing you agree to our{" "}
        <Link href="/terms-conditions" className="text-accent underline-offset-4 hover:underline">
          Terms
        </Link>{" "}
        and{" "}
        <Link href="/privacy-policy" className="text-accent underline-offset-4 hover:underline">
          Privacy Policy
        </Link>
        .
      </p>
    </main>
  );
}
