import {
  ScreenDefinitionSchema,
  type RankingPresetOut,
  type ScreenDefinition,
  type ValidationStatus,
} from "@baskfy/api-client";

import { defaultDefinition } from "@/lib/screens/defaults";
import { NEW_SCREEN_RANKING_SCOPE, checkPatch } from "@/lib/screens/ranking";

/**
 * Named ranking presets and registry validation status — docs/ranking/PLAN.md §1.5 / C1 / C6,
 * gates/ranking-2.H-web.md G5.
 *
 * `GET /meta/ranking-presets` serves each preset's `patch` as loose JSON. The editor only patches
 * a valid screen, so a preset is **validated as a `ScreenDefinition` patch over the default
 * definition** before it is offered: a patch naming an unknown field, or one that breaks a C3 rule
 * on its own, is shown as unavailable with its reason instead of being applied and then silently
 * discarded by the URL state.
 */

type BadgeTone = "positive" | "warning" | "negative" | "neutral";

export interface StatusBadge {
  label: string;
  tone: BadgeTone;
  note: string;
}

/** A preset's promotion status (`ready` / `research`); anything else is shown as sent. */
export function presetStatusBadge(status: string): StatusBadge {
  switch (status) {
    case "ready":
      return { label: "Ready", tone: "positive", note: "Tested enough to rank by today." };
    case "research":
      return {
        label: "Research",
        tone: "warning",
        note: "A starting point. Not yet tested enough to rely on.",
      };
    default:
      return { label: status, tone: "neutral", note: "Status as reported by the server." };
  }
}

export const VALIDATION_STATUS_BADGE: Record<ValidationStatus, StatusBadge> = {
  validated: { label: "Validated", tone: "positive", note: "Passed out-of-sample testing." },
  research: {
    label: "Research",
    tone: "warning",
    note: "Still being tested. Use with care.",
  },
  rejected: { label: "Rejected", tone: "negative", note: "Failed testing. Not recommended." },
  legacy: {
    label: "Legacy",
    tone: "neutral",
    note: "An original factor, not yet put through the new tests.",
  },
};

/** `undefined` when the registry sent no status (an older API), so no badge is drawn. */
export function validationStatusBadge(status: string | undefined): StatusBadge | undefined {
  if (status === undefined) return undefined;
  return (VALIDATION_STATUS_BADGE as Record<string, StatusBadge | undefined>)[status];
}

export type PresetCheck =
  | { ok: true; patch: Partial<ScreenDefinition> }
  | { ok: false; reason: string };

/**
 * The preset's patch, typed, when `{...defaultDefinition(), ...patch}` is a valid screen.
 *
 * The typed patch carries only the keys the preset names, read back from the parsed definition so
 * nested defaults (a `factor_two` without `sort_direction`, say) arrive filled in.
 */
export function validatePreset(preset: RankingPresetOut): PresetCheck {
  const parsed = ScreenDefinitionSchema.safeParse({ ...defaultDefinition(), ...preset.patch });
  if (!parsed.success) {
    return {
      ok: false,
      reason: parsed.error.issues[0]?.message ?? "this preset is not a valid screen.",
    };
  }
  const patch: Partial<ScreenDefinition> = {};
  for (const key of Object.keys(preset.patch) as (keyof ScreenDefinition)[]) {
    Object.assign(patch, { [key]: parsed.data[key] });
  }
  return { ok: true, patch };
}

/**
 * The change that applies a validated preset patch to the current definition.
 *
 * A preset ranks by its Sort By, so ranking terms it does not name are cleared (with terms,
 * `sort_by` must equal the first term), and whatever C3 then requires follows: no family weights
 * or missing-data rule without terms, no Within sector without terms, and no factor two or three
 * in Single mode or under the desk score. Filters the preset does not name are left alone.
 */
export function presetChange(
  definition: ScreenDefinition,
  patch: Partial<ScreenDefinition>,
): Partial<ScreenDefinition> {
  const change: Partial<ScreenDefinition> = { ...patch };
  const terms = patch.ranking_terms ?? [];
  if (patch.ranking_terms === undefined) change.ranking_terms = [];
  if (terms.length === 0) {
    if (patch.family_weights === undefined) change.family_weights = null;
    if (patch.missing_data === undefined) change.missing_data = "penalize";
    const scope = patch.ranking_scope ?? definition.ranking_scope;
    if (scope === "within_sector") change.ranking_scope = NEW_SCREEN_RANKING_SCOPE;
  }
  const mode = patch.ranking_mode ?? definition.ranking_mode;
  const sortBy = patch.sort_by ?? definition.sort_by;
  if (mode === "single" || sortBy === "desk_score") {
    if (patch.factor_two === undefined) {
      change.factor_two = { ...definition.factor_two, enabled: false };
    }
    if (patch.factor_three === undefined) {
      change.factor_three = { ...definition.factor_three, enabled: false };
    }
  }
  return change;
}

export type PresetApplication =
  | { ok: true; change: Partial<ScreenDefinition> }
  | { ok: false; reason: string };

/** Validate over the default, then check the change against the current screen. */
export function applyPreset(
  definition: ScreenDefinition,
  preset: RankingPresetOut,
): PresetApplication {
  const checked = validatePreset(preset);
  if (!checked.ok) return checked;
  const change = presetChange(definition, checked.patch);
  const problem = checkPatch(definition, change);
  return problem === null ? { ok: true, change } : { ok: false, reason: problem };
}
