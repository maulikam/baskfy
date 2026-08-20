"use client";

import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";

import { Button } from "@/components/ui/button";
import { useIsMounted } from "@/lib/use-is-mounted";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

/**
 * docs/08 §"App shell": "theme toggle"; §"Design principles": "Dark mode is first-class (traders
 * live in it)."
 *
 * Three states, not two — "system" is the default and a two-state toggle silently opts the user
 * out of it. The icon only renders after mount because the resolved theme is not knowable during
 * SSR; rendering the wrong icon and then correcting it is the hydration flash this avoids. The
 * button keeps its size throughout, so nothing shifts (Prompt 8's fourth acceptance criterion).
 */
const OPTIONS = [
  { value: "light", label: "Light", Icon: Sun },
  { value: "dark", label: "Dark", Icon: Moon },
  { value: "system", label: "System", Icon: Monitor },
] as const;

export function ThemeToggle() {
  const { theme, setTheme, resolvedTheme } = useTheme();
  const mounted = useIsMounted();

  const ActiveIcon = mounted && resolvedTheme === "dark" ? Moon : Sun;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label="Change theme">
          {mounted ? <ActiveIcon aria-hidden="true" /> : <span className="size-4" />}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>Theme</DropdownMenuLabel>
        {OPTIONS.map(({ value, label, Icon }) => (
          <DropdownMenuItem key={value} onSelect={() => setTheme(value)}>
            <Icon aria-hidden="true" className="size-4" />
            <span className="flex-1">{label}</span>
            {mounted && theme === value ? (
              <span aria-hidden="true" className="text-accent">
                ✓
              </span>
            ) : null}
            {mounted && theme === value ? <span className="sr-only">(current)</span> : null}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
