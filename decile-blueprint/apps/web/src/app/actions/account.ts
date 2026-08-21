"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";

import { signOut } from "@/lib/auth";
import { callMe, type MutationResult } from "@/lib/auth/me";

/**
 * Server actions for `/profile` and `/change-password` — Prompt 12 deliverable 4.
 *
 * They call the API with the session's bearer token rather than touching the database, so every
 * rule the API enforces — the current-password check, the password policy, the session
 * revocation — applies identically whether the change came from the web app or from a script.
 */

const MIN_PASSWORD_LENGTH = 8;

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

export async function changePassword(
  _previous: MutationResult | null,
  formData: FormData,
): Promise<MutationResult> {
  const current = field(formData, "current_password");
  const next = field(formData, "new_password");
  const confirm = field(formData, "confirm_password");

  if (next.length < MIN_PASSWORD_LENGTH) {
    return { ok: false, message: `Passwords must be at least ${MIN_PASSWORD_LENGTH} characters.` };
  }
  if (next !== confirm) return { ok: false, message: "The two new passwords do not match." };

  const result = await callMe("/me/change-password", "POST", {
    current_password: current || null,
    new_password: next,
  });
  if (!result.ok) return { ok: false, message: result.detail };
  return {
    ok: true,
    message: "Your password has been changed. Every other session has been signed out.",
  };
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
