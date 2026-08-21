import { MAIN_CONTENT_ID } from "@/components/shell/ids";

/**
 * docs/08 §"Accessibility & quality bar" and Prompt 8's third acceptance criterion: "Keyboard-only
 * walkthrough of the shell is possible: skip link, focus rings, no traps."
 *
 * First focusable element on the page, hidden until focused (`.skip-link` in `globals.css`), so a
 * keyboard user is not required to tab through fifteen sidebar links to reach the table.
 */
export function SkipLink() {
  return (
    <a
      href={`#${MAIN_CONTENT_ID}`}
      className="skip-link rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-foreground shadow-lg"
    >
      Skip to main content
    </a>
  );
}
