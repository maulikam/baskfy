import type { Collection } from "./fetch";

/**
 * Which shelves are worth stacking on a page that already shows the catalogue.
 *
 * The problem this solves, measured against the real database: four collections exist, three of
 * them (`start-here`, `momentum`, `run-by-the-engine`) resolve to the same single basket, and
 * `quarterly` resolves to none. Stacking all four rendered one basket card three times and a
 * dashed empty box — a browse surface that reads as broken while every row of data behind it is
 * true. Nothing is wrong with the shelves; the catalogue is one basket deep, and shelves cannot
 * group a catalogue that small.
 *
 * The rule, therefore, is about presentation and only presentation:
 *
 *   *A shelf earns space on a page that already shows the catalogue only when it groups
 *   something the shelves above it did not.*
 *
 * Two consequences fall out of that sentence, and they are the whole module:
 *
 *  - a shelf with no baskets groups nothing, and
 *  - a shelf holding *exactly* the baskets of a shelf above it is that shelf under a second name.
 *
 * Identity, and deliberately not containment. Containment was tried first and was wrong: once the
 * catalogue grew past one basket, `quarterly` became a strict subset of the cheapest-six shelf,
 * and suppressing it would have hidden a real editorial claim ("four decisions a year") because
 * its three baskets happened to also be cheap. A narrower shelf is the entire point of shelves.
 * Overlap between two genuinely different shelves is normal browsing and is left alone; only an
 * exact repeat is removed.
 *
 * And one more, at the level of the section rather than the shelf: if fewer than two shelves
 * survive, the *stack* groups nothing either — one shelf is just the catalogue again, re-titled.
 * The caller then shows the directory instead, which states the same truth in four short tiles.
 *
 * **Suppression here is never concealment.** A suppressed collection keeps its directory tile and
 * its own page; `selectShelves` returns it in `suppressed` precisely so a caller can prove that.
 * The honest empty state that `CollectionShelf` renders is not deleted — it is moved to where a
 * person asked for that shelf by name, instead of being shown to everyone who scrolls past.
 */

/** Below this, a stack of shelves is not grouping anything, and the directory says it better. */
export const MIN_SHELVES_TO_STACK = 2;

export interface ShelfSelection {
  /** Shelves worth stacking, in curator order. */
  shelves: Collection[];
  /** Collections left out of the stack. Still in the directory, still on their own pages. */
  suppressed: Collection[];
  /** True when the stack groups something: at least `MIN_SHELVES_TO_STACK` shelves that differ. */
  stack: boolean;
}

function slugsOf(collection: Collection): Set<string> {
  return new Set(collection.baskets.map((basket) => basket.slug));
}

function sameBaskets(left: Set<string>, right: Set<string>): boolean {
  if (left.size !== right.size) return false;
  for (const slug of left) {
    if (!right.has(slug)) return false;
  }
  return true;
}

/**
 * Partition collections into the ones worth stacking and the ones that would only repeat.
 *
 * Order is the curator's — the API returns `cb_collection` by `position` — and it is load-bearing:
 * of two shelves holding the same baskets the earlier one is kept, so the curator decides which
 * name that group is browsed under. Reversing the order would suppress the opposite shelf, which
 * is why this never sorts.
 */
export function selectShelves(collections: readonly Collection[]): ShelfSelection {
  const shelves: Collection[] = [];
  const keptSlugs: Set<string>[] = [];
  const suppressed: Collection[] = [];

  for (const collection of collections) {
    const slugs = slugsOf(collection);
    if (slugs.size === 0 || keptSlugs.some((kept) => sameBaskets(slugs, kept))) {
      suppressed.push(collection);
      continue;
    }
    shelves.push(collection);
    keptSlugs.push(slugs);
  }

  return { shelves, suppressed, stack: shelves.length >= MIN_SHELVES_TO_STACK };
}
