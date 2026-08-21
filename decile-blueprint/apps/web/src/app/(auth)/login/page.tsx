import type { Metadata } from "next";
import Link from "next/link";

import { LoginForm } from "@/app/(auth)/login/login-form";

export const metadata: Metadata = {
  title: "Sign in",
  robots: { index: false, follow: false },
};

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
          A one-time code is the default; a password works too.
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
      <p className="text-sm text-muted-foreground">
        No account yet?{" "}
        <Link href="/register" className="text-accent underline-offset-4 hover:underline">
          Create one
        </Link>
        {" · "}
        <Link href="/forgot-password" className="text-accent underline-offset-4 hover:underline">
          Forgot password
        </Link>
      </p>
    </main>
  );
}
