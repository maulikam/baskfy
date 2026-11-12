/**
 * The detail page's delete confirmation, pure. The API re-checks every rule (409 on holdings
 * without `confirm=holdings`, 409 on a linked basket investment), so this only decides what the
 * dialog asks for and says.
 */

export interface DeleteTarget {
  readonly id: number;
  readonly name: string;
  readonly holdingsCount: number;
}

export interface DeleteDialogCopy {
  readonly title: string;
  readonly holdingsLine: string;
  readonly brokerLine: string;
  readonly undoLine: string;
  /** With holdings the portfolio's name must be typed, and the API call carries the confirm. */
  readonly needsTypedName: boolean;
}

export function deleteDialogCopy(target: DeleteTarget): DeleteDialogCopy {
  const n = target.holdingsCount;
  return {
    title: `Delete “${target.name}”?`,
    holdingsLine:
      n === 0
        ? "It records no holdings."
        : `It records ${n} holding${n === 1 ? "" : "s"}; those records are removed with it.`,
    brokerLine:
      "This removes Baskfy's record only. No order is placed or cancelled, no stop is changed, and any shares stay in your broker account as unallocated.",
    undoLine:
      "This cannot be undone: its value history, returns and cash records are deleted for good. Any portfolio inside it moves to the top level.",
    needsTypedName: n > 0,
  };
}

/** Whether the destructive button may fire for what has been typed so far. */
export function canConfirmDelete(target: DeleteTarget, typed: string): boolean {
  if (target.holdingsCount <= 0) return true;
  return typed.trim() === target.name.trim();
}
