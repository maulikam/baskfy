/**
 * Screen-page redesign flags.
 *
 * `NEXT_PUBLIC_SCREEN_CHIP_FILTERS` defaults ON. Set to `0` or `false` to fall back to the
 * classic left-rail accordion while iterating.
 */
export function screenChipFiltersEnabled(): boolean {
  const raw = process.env.NEXT_PUBLIC_SCREEN_CHIP_FILTERS;
  if (raw === "0" || raw === "false") return false;
  return true;
}

/** Which of the screen editor's two result views is on screen before anything is clicked. */
export type ScreenDefaultView = "basket" | "table";

/**
 * `NEXT_PUBLIC_SCREEN_DEFAULT_VIEW` defaults to `table` (Tree 7 D8): `/build/[id]` is where the
 * ranked artifact is constructed, so the ranked list is what a first paint owes the reader. Set to
 * `basket` to restore the basket-first default Tree 6 §5 shipped. Anything else reads as `table`,
 * because a typo must not decide what the page opens on.
 *
 * Only the *editor's* default moves. `/baskets`, `/basket/[slug]` and the build list cards are
 * basket-first regardless of this flag; none of them reads it.
 */
export function screenDefaultView(): ScreenDefaultView {
  const raw = process.env.NEXT_PUBLIC_SCREEN_DEFAULT_VIEW;
  if (raw === "basket") return "basket";
  return "table";
}
