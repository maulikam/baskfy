"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";

import { signOut } from "@/lib/auth";
import { callMe, type MutationResult } from "@/lib/auth/me";

/**
 * Server actions for `/profile` — Prompt 12 deliverable 4.
 *
 * They call the API with the session's bearer token rather than touching the database, so every
 * rule the API enforces applies identically whether the change came from the web app or from a
 * script.
 *
 * `changePassword` used to live here. Google sign-in replaced the password entirely
 * (`docs/DECISIONS-MERGE.md` M46), so there is no credential on this side to change.
 */

function field(formData: FormData, name: string): string {
  const value = formData.get(name);
  return typeof value === "string" ? value : "";
}

export async function updateProfile(
  _previous: MutationResult | null,
  formData: FormData,
): Promise<MutationResult> {
  const name = field(formData, "name").trim();
  if (!name) return { ok: false, message: "Enter a name." };

  const result = await callMe("/me", "PATCH", { name });
  if (!result.ok) return { ok: false, message: result.detail };
  revalidatePath("/profile");
  return { ok: true, message: "Saved." };
}


/**
 * DPDP erasure — Prompt 12 §5. Signs the browser out on success, because the account it was
 * signed into is now deactivated and every one of its refresh tokens has been revoked.
 */
export async function deleteAccount(
  _previous: MutationResult | null,
  formData: FormData,
): Promise<MutationResult> {
  const email = field(formData, "email").trim();
  if (!email) return { ok: false, message: "Type your email address to confirm." };

  const result = await callMe("/me", "DELETE", { email });
  if (!result.ok) return { ok: false, message: result.detail };
  await signOut({ redirect: false });
  redirect("/login?deleted=1");
}
