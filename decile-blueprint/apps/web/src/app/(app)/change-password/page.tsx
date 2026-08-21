import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { ChangePasswordForm } from "@/components/account/change-password-form";
import { fetchMe } from "@/lib/auth/me";

export const metadata: Metadata = {
  title: "Change password",
  robots: { index: false, follow: false },
};

/**
 * `POST /me/change-password` — docs/07 §"Account & billing", docs/08 §Routes ("RSC + forms").
 *
 * An account with no password is *setting* one (docs/11: "password optional"), so the current
 * password field is not shown. The API applies the same rule; this only decides what to render.
 */
export default async function ChangePasswordPage() {
  const me = await fetchMe();
  if (!me) redirect("/login?next=/change-password");

  return (
    <div className="flex max-w-md flex-col gap-4">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold tracking-tight">
          {me.has_password ? "Change password" : "Set a password"}
        </h1>
        <p className="text-sm text-muted-foreground">
          Changing your password signs out every other session.
        </p>
      </header>

      <ChangePasswordForm hasPassword={me.has_password} />
    </div>
  );
}
