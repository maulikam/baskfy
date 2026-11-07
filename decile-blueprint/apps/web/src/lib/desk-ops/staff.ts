import "server-only";

import { fetchMe } from "@/lib/auth/me";

/** Server truth for the operator pages: `GET /me`'s `is_staff`, the bit AppShell's StaffProvider carries. */
export async function isStaffServer(): Promise<boolean> {
  const me = await fetchMe();
  return me?.is_staff ?? false;
}
