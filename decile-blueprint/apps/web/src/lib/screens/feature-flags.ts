/**
 * Screen-page redesign flags.
 *
 * The `NEXT_PUBLIC_SCREEN_CHIP_FILTERS` legacy-editor escape hatch was deleted (AUDIT 4.10).
 * Chip filters are the only editor surface.
 */

/** Which of the screen editor's two result views is on screen before anything is clicked. */
export type ScreenDefaultView = "basket" | "table";

/**
 * `NEXT_PUBLIC_SCREEN_DEFAULT_VIEW` defaults to `table` (Tree 7 D8): `/build/[id]` is where the
 * ranked artifact is constructed, so the ranked list is what a first paint owes the reader. Set to
 * `basket` to restore the basket-first default Tree 6 §5 shipped. Anything else reads as `table`,
 * because a typo must not decide what the page opens on.
 *
 * Only the *editor's* default moves. `/discover`, `/basket/[slug]` and the build list cards are
 * basket-first regardless of this flag; none of them reads it.
 */
export function screenDefaultView(): ScreenDefaultView {
  const raw = process.env.NEXT_PUBLIC_SCREEN_DEFAULT_VIEW;
  if (raw === "basket") return "basket";
  return "table";
}
