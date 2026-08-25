import type { ScreenOut } from "@baskfy/api-client";

/**
 * How `/create` picks which screen to size (SB2).
 *
 * Login itself hands the investor the example screens — `GET /screens` returns them for a
 * brand-new account and for a visitor who has not signed in yet (`user_id IS NULL`). This module
 * is the grouping and the default so the page does not invent a second rule for "which one is
 * selected first".
 */

export interface GroupedScreens {
  examples: ScreenOut[];
  mine: ScreenOut[];
}

export function groupScreens(screens: readonly ScreenOut[]): GroupedScreens {
  const examples: ScreenOut[] = [];
  const mine: ScreenOut[] = [];
  for (const screen of screens) {
    if (screen.is_example) examples.push(screen);
    else mine.push(screen);
  }
  return { examples, mine };
}

/**
 * The screen the form opens on.
 *
 * An explicit id wins when it is in the list (a link from `/build` with `?screen=`). Otherwise
 * the first template — those exist on first login — then the first of the investor's own.
 */
export function pickDefaultScreenId(
  screens: readonly ScreenOut[],
  requested?: string | null,
): string | null {
  if (requested && screens.some((screen) => screen.public_id === requested)) {
    return requested;
  }
  const { examples, mine } = groupScreens(screens);
  return examples[0]?.public_id ?? mine[0]?.public_id ?? null;
}
