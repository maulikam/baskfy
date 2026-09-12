"use client";

import { createContext, useContext, type ReactNode } from "react";

/**
 * Whether the signed-in account is staff (`app_user.is_staff` via `GET /me`).
 *
 * SectionTabs reads this when its `isStaff` prop is omitted, so every Build page
 * does not have to thread the bit. AppShell is the one writer; default is civilian.
 */
const StaffContext = createContext(false);

export function StaffProvider({ value, children }: { value: boolean; children: ReactNode }) {
  return <StaffContext.Provider value={value}>{children}</StaffContext.Provider>;
}

export function useIsStaff(): boolean {
  return useContext(StaffContext);
}
