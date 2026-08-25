"use client";

import { Bell, LogIn, LogOut, Plug, ShieldCheck, User } from "lucide-react";
import Link from "next/link";

import { NAV_GROUPS } from "@/lib/nav";

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
 *
 * **M36 moved the Account and Help groups in here.** They were two blocks of the sidebar, drawn at
 * the same weight as the seven product destinations, which is a lot of furniture for "change my
 * password" and "read the FAQ". With navigation along the top there is no rail to put them in and
 * no reason to want one: settings belong behind the account control. `NAV_GROUPS` is unchanged, so
 * `nav.test.ts` still pins docs/08's IA — only where it is *drawn* moved.
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

/** Real money (desk read-only) + Account + Help — secondary chrome since Tree 6. */
const MENU_GROUPS = NAV_GROUPS.filter(
  (group) =>
    group.label === "Real money" || group.label === "Account" || group.label === "Help",
);

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

        {MENU_GROUPS.map((group) => (
          <div key={group.label}>
            <DropdownMenuLabel className="pt-1 text-[11px] font-medium tracking-[0.04em]">
              {group.label}
            </DropdownMenuLabel>
            {group.items.map((item) =>
              item.status === "ready" ? (
                <DropdownMenuItem key={item.href} asChild>
                  <Link href={item.href}>{item.label}</Link>
                </DropdownMenuItem>
              ) : null,
            )}
          </div>
        ))}

        <DropdownMenuSeparator className="my-1 h-px bg-border" />
        {/*
          Prompt 20 §1 and §3. Here rather than in `NAV_GROUPS` for exactly the reason the admin
          link is: `src/lib/nav.ts` is pinned to docs/08's sidebar IA by a test, and docs/08's
          Account group is Pricing, Invoices, Profile, Change Password — four items, no more.
          Neither surface exists in the reference product, so adding them to the sidebar would be
          this build editing the specification rather than following it.
        */}
        <DropdownMenuItem asChild>
          <Link href="/alerts">
            <Bell aria-hidden="true" className="size-4" />
            Screen alerts
          </Link>
        </DropdownMenuItem>
        <DropdownMenuItem asChild>
          <Link href="/api-keys">
            <Plug aria-hidden="true" className="size-4" />
            API keys
          </Link>
        </DropdownMenuItem>
        {isStaff ? (
          <DropdownMenuItem asChild>
            <Link href="/admin">
              <ShieldCheck aria-hidden="true" className="size-4" />
              Admin
            </Link>
          </DropdownMenuItem>
        ) : null}
        {/*
          `prefetch={false}`, and it is not an optimisation. `/logout` is a route handler whose GET
          signs the user out; a prefetch is a GET. Left on, opening this menu would arm a request
          that ends the session before anybody clicked anything.
        */}
        <DropdownMenuItem asChild>
          <Link href="/logout" prefetch={false}>
            <LogOut aria-hidden="true" className="size-4" />
            Sign out
          </Link>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
