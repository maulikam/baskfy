"use client";

import { LogIn, LogOut, User } from "lucide-react";
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
}

export function UserMenu({ email, name }: UserMenuProps) {
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
