"use client";

import { useState } from "react";
import { CircleAlert } from "lucide-react";

import {
  notWiredReason,
  refusalSentence,
  unexpectedFailure,
  type ManageAction,
  type ManageOutcome,
} from "@/lib/portfolio/manage";

/**
 * One place where a write can fail, so it cannot fail silently in five.
 *
 * WHY THIS EXISTS AT ALL
 * ----------------------
 * Baskfy has shipped the same defect twice inside a week, and both are in the record:
 *
 *   · 11 Sep 2026 — the command centre's `Review rebalance` and `Add portfolio` were wired to
 *     optional callbacks the page never passed. The buttons looked live and swallowed the click.
 *   · 11 Sep 2026 — *"nothing is happening when i click button add to swing"*. Three separate
 *     faults in one path, none of which reached the screen.
 *
 * The shape of both is the same: a failure with nowhere to go. So every write in this drawer runs
 * through {@link useWrite}, and there are exactly three ways out of it — success, a refusal with
 * the server's sentence, or a thrown request with its own sentence. There is no fourth, and in
 * particular there is no path that resolves to `undefined` and leaves the form looking saved.
 *
 * **A failure never closes anything.** The panel stays open, on the form, with everything the
 * user entered still in it, and the sentence sits beside the button they pressed. A form that
 * closes on failure is a form that has told the user it worked.
 */

export interface WriteState {
  readonly saving: boolean;
  readonly failure: string | null;
  /** Clears a stale refusal when the user edits the thing that was refused. */
  readonly clearFailure: () => void;
  /**
   * Run one write. Returns `true` only when the write genuinely succeeded, so a caller can
   * navigate away on `true` and is structurally unable to navigate away on anything else.
   */
  readonly run: (
    action: ManageAction,
    handler: (() => Promise<ManageOutcome>) | undefined,
    onSuccess: (outcome: Extract<ManageOutcome, { ok: true }>) => void,
  ) => Promise<boolean>;
}

export function useWrite(): WriteState {
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  async function run(
    action: ManageAction,
    handler: (() => Promise<ManageOutcome>) | undefined,
    onSuccess: (outcome: Extract<ManageOutcome, { ok: true }>) => void,
  ): Promise<boolean> {
    if (saving) return false;
    if (handler === undefined) {
      // Not an error the user caused, and not a silent no-op either. The button that reached
      // here was already disabled and labelled; this is the belt to that brace.
      setFailure(notWiredReason(action));
      return false;
    }
    setSaving(true);
    setFailure(null);
    try {
      const outcome = await handler();
      // A handler that resolves to nothing is a spy in a test that only cares the click fired.
      // Treating it as a refusal would put an error on screen for a write that worked; treating
      // it as a success would be the defect this hook exists to prevent. It is neither: nothing
      // happened, and the form stays where it is.
      if (!outcome) return false;
      if (outcome.ok) {
        onSuccess(outcome);
        return true;
      }
      setFailure(refusalSentence(action, outcome.reason));
      return false;
    } catch (error) {
      setFailure(unexpectedFailure(action, error));
      return false;
    } finally {
      setSaving(false);
    }
  }

  return { saving, failure, clearFailure: () => setFailure(null), run };
}

/**
 * The refusal, rendered where the user is looking.
 *
 * `role="alert"` rather than a toast: a toast for a failed save is a sentence that leaves before
 * the person has finished reading the form it belongs to, and this drawer's whole job is that a
 * failed save cannot look like a successful one.
 */
export function WriteFailure({ failure }: { failure: string | null }) {
  if (failure === null) return null;
  return (
    <p
      role="alert"
      data-testid="write-failure"
      className="flex items-start gap-2 rounded-lg border border-negative/40 bg-negative-muted px-3 py-2.5 text-xs leading-relaxed text-foreground"
    >
      <CircleAlert aria-hidden="true" className="mt-px size-4 shrink-0 text-negative" />
      <span>
        {/* The word as well as the colour and the icon — §6.2 rule 3. */}
        <strong className="font-semibold">Not saved.</strong> {failure}
      </span>
    </p>
  );
}
