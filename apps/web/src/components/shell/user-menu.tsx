"use client";

import { LogIn, LogOut, ShieldCheck, User } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

/**
 * docs/08 §"App shell": "user menu".
 *
 * The signed-in identity is passed in from the server layout (`auth()`), not fetched here: the
 * session is already known when the shell renders, and a client fetch would flash "Sign in" for a
 * user who is signed in — a layout shift on every page load.
 */
export interface UserMenuProps {
  email: string | null;
  name: string | null;
  /**
   * `app_user.is_staff`, from `GET /me` — server truth, exactly like `entitlements`
   * (PROMPTS.md Prompt 17 §4). The admin link lives here rather than in `NAV_GROUPS` because
   * `src/lib/nav.ts` is pinned to docs/08's sidebar IA by a test, and docs/08 does not list an
   * admin section. Hiding the link is a courtesy; `require_staff` on every `/admin/*` route is
   * the enforcement, and it answers 404 to a non-staff caller.
   */
  isStaff?: boolean;
}

export function UserMenu({ email, name, isStaff = false }: UserMenuProps) {
  if (!email) {
    return (
      <Button variant="outline" size="sm" asChild>
        <Link href="/login">
          <LogIn aria-hidden="true" />
          Sign in
        </Link>
      </Button>
    );
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label={`Account menu for ${email}`}>
          <User aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>
          <span className="block font-medium text-foreground">{name ?? "Signed in"}</span>
          <span className="block truncate">{email}</span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator className="my-1 h-px bg-border" />
        {isStaff ? (
          <DropdownMenuItem asChild>
            <Link href="/admin">
              <ShieldCheck aria-hidden="true" className="size-4" />
              Admin
            </Link>
          </DropdownMenuItem>
        ) : null}
        <DropdownMenuItem asChild>
          <Link href="/logout">
            <LogOut aria-hidden="true" className="size-4" />
            Sign out
          </Link>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
