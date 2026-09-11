import { describe, expect, it } from "vitest";

import {
  DESK_LADDER_DEFAULTS,
  exposureGap,
  regimeReading,
  sentinelReading,
  type RegimeOut,
} from "@/lib/portfolio/regime";

/**
 * The regime module's arithmetic and its refusals.
 *
 * Written against the desk's own behaviour rather than against this code's, because the desk is
 * the authority here: `baskfy_core/exposure/regime.py` decides what a tier means,
 * `regime_store.record_exposure` decides which way the gap is signed, and
 * `services/api/.../routers/desk.py` decides which of it reaches the browser. Each assertion below
 * names the one it is holding.
 *
 * The four that would each be a real defect in a product that places live orders:
 *
 *   · a cap taken from the configured ladder instead of from the evaluation;
 *   · R2 described as an exit, when it is a lower cap with entries usually still open;
 *   · a gap whose sign disagrees with the desk's own;
 *   · a figure appearing for something `RegimeOut` does not carry.
 */

/* Schema-exact, no `as`. `RegimeOut` is generated from the API's OpenAPI document, so an assertion
   here would let a fixture describe a payload the server never sends and still go green. */
function payload(over: Partial<RegimeOut> = {}): RegimeOut {
  return {
    evaluated_at: "2026-09-04T18:30:00+05:30",
    signal_date: "2026-09-04",
    tier: "R2",
    previous_tier: "R1",
    new_buys: "half",
    mode: "observe",
    breadth_pct: 38.4,
    actual_equity_pct: 78.0,
    target_equity_cap_pct: 70.0,
    reasons: ["Breadth below the R3 threshold.", "No tier change this evaluation."],
    data_stale: false,
    manual_action_required: false,
    next_evaluation_date: "2026-09-11",
    ...over,
  };
}

function ready(over: Partial<RegimeOut> = {}, today: string | null = "2026-09-07") {
  const state = regimeReading({ regime: payload(over), today });
  if (state.kind !== "ready") throw new Error(`expected a ready reading, got ${state.kind}`);
  return state.reading;
}

/* ------------------------------------------------------------------------------- the gap */

describe("the exposure gap", () => {
  it("gap: is actual minus target, in percentage points, with the direction in words", () => {
    /* The brief's own sentence: "Current exposure is 8 percentage points above target." */
    const r = ready({ actual_equity_pct: 78, target_equity_cap_pct: 70 });

    expect(r.gap.metric.value).toBe("8.0");
    expect(r.gap.direction).toBe("above");
    expect(r.gap.magnitude).toBe("8.0");
    expect(r.gap.sentence).toBe("Current exposure is 8.0 percentage points above target.");
  });

  it("gap: signs the same way the desk does, so the two can never disagree", () => {
    /* `regime_store.record_exposure`: gap = actual_equity_pct - target_equity_cap_pct. Under the
       cap is therefore NEGATIVE, and the word for it is "below". */
    const under = exposureGap(payload({ actual_equity_pct: 31.8, target_equity_cap_pct: 40 }));

    expect(under.metric.value).toBe("-8.2");
    expect(under.direction).toBe("below");
    expect(under.sentence).toBe("Current exposure is 8.2 percentage points below target.");
  });

  it("gap: the word never contradicts the rounded figure it is printed beside", () => {
    /* 70.02 - 70 is a real difference and it rounds to 0.0. A panel that printed "0.0pp" and
       "above target" would be lying by rounding, so the direction is taken from the ROUNDED
       figure and this reads as on target. */
    const flat = exposureGap(payload({ actual_equity_pct: 70.02, target_equity_cap_pct: 70 }));

    expect(flat.magnitude).toBe("0.0");
    expect(flat.direction).toBe("on-target");
    expect(flat.sentence).toMatch(/on target/);
  });

  it("gap: floating-point subtraction never reaches the screen unrounded", () => {
    /* 78.4 - 70 is 8.399999999999999 in IEEE 754. */
    const gap = exposureGap(payload({ actual_equity_pct: 78.4, target_equity_cap_pct: 70 }));

    expect(gap.metric.value).toBe("8.4");
  });

  it("gap: one percentage point is singular", () => {
    const gap = exposureGap(payload({ actual_equity_pct: 71, target_equity_cap_pct: 70 }));

    expect(gap.sentence).toBe("Current exposure is 1.0 percentage point above target.");
  });

  it("gap: with either side missing there is no gap and no direction, only the reason", () => {
    const noCap = exposureGap(payload({ target_equity_cap_pct: null }));
    const noActual = exposureGap(payload({ actual_equity_pct: null }));
    const neither = exposureGap(payload({ actual_equity_pct: null, target_equity_cap_pct: null }));

    for (const gap of [noCap, noActual, neither]) {
      expect(gap.metric.value).toBeNull();
      expect(gap.direction).toBeNull();
      expect(gap.metric.unavailable).toBeTruthy();
    }
    expect(noCap.metric.unavailable).toMatch(/^The cap /);
    expect(noActual.metric.unavailable).toMatch(/^The actual exposure /);
    expect(neither.metric.unavailable).toMatch(/^Neither /);
  });
});

/* ------------------------------------------------------------------------------ the ladder */

describe("the target cap", () => {
  it("cap: comes from the evaluation, never from the configured ladder", () => {
    /* `app/config.py` sets R2 = 70. If the desk records something else — an operator changed
       REGIME_* on the box — the evaluation wins, because the ladder's rungs are defaults. */
    const r = ready({ tier: "R2", target_equity_cap_pct: 55 });

    expect(r.targetEquity.value).toBe("55.0");
    expect(DESK_LADDER_DEFAULTS.find((rung) => rung.tier === "R2")?.equityPct).toBe(70);
  });

  it("cap: a missing cap is unavailable with its reason and is NOT filled in from the rung", () => {
    const r = ready({ tier: "R2", target_equity_cap_pct: null });

    expect(r.targetEquity.value).toBeNull();
    expect(r.targetEquity.unavailable).toMatch(/will not fill it in from the configured ladder/);
    /* The rung's value must not appear anywhere in the figure. */
    expect(r.targetEquity.unavailable).not.toMatch(/\b70\b/);
  });

  it("cap: the ladder is labelled as configured defaults and cites REGIME_R4_EQUITY_PCT", () => {
    const r = ready();

    expect(r.ladderCaveat).toMatch(/configured defaults/);
    expect(r.ladderCaveat).toMatch(/REGIME_R4_EQUITY_PCT/);
    expect(r.ladderCaveat).toMatch(/never from this table/);
    expect(r.ladder.find((rung) => rung.tier === "R4")?.fromEnv).toBe("REGIME_R4_EQUITY_PCT");
  });

  it("cap: the ladder matches app/config.py's tier_exposure_pct", () => {
    /* `kite-momentum-rebalancer/app/config.py`: {"R1": 100.0, "R2": 70.0, "R3": 40.0,
       "R4": REGIME_R4_EQUITY_PCT}, and REGIME_R4_EQUITY_PCT defaults to 10. NOT
       `baskfy_core/sleeves.py`'s R4 = 0, which is a fallback for a missing caps table. */
    expect(DESK_LADDER_DEFAULTS.map((rung) => [rung.tier, rung.equityPct])).toEqual([
      ["R1", 100],
      ["R2", 70],
      ["R3", 40],
      ["R4", 10],
    ]);
  });

  it("cap: R4 is only called a full exit when the recorded cap is zero", () => {
    /* `app/config.py`: "R4 residual policy must be EXPLICIT ... rather than describing a 10%
       residual as a full exit." */
    const residual = ready({ tier: "R4", target_equity_cap_pct: 10, previous_tier: "R3" });
    const flat = ready({ tier: "R4", target_equity_cap_pct: 0, previous_tier: "R3" });
    const unknown = ready({ tier: "R4", target_equity_cap_pct: null, previous_tier: "R3" });

    expect(residual.applied.stance).toMatch(/residual holding and not a full exit/);
    expect(flat.applied.stance).toMatch(/is 0%, which is a full exit/);
    expect(unknown.applied.stance).toMatch(/depends on the cap, which this evaluation did not record/);
  });
});

/* ---------------------------------------------------------------------------------- R2 */

describe("what R2 means", () => {
  it("R2 is reduced exposure and is never described as an exit", () => {
    const r = ready({ tier: "R2" });

    expect(r.applied.label).toBe("Cautious");
    expect(r.applied.stance).toMatch(/Reduced exposure, not an exit/);
    expect(r.applied.stance).not.toMatch(/out of the market|full exit|sell everything|all in cash/i);
  });

  it("R2 with blocked new buys is still not an exit", () => {
    /* The sentinel veto can block entries at R2 (`resolve_new_buys`). A blocked book is not a
       sold book, and the two must not be conflated: exposure stays at the cap. */
    const r = ready({ tier: "R2", new_buys: "blocked" });

    expect(r.newBuys.label).toBe("Blocked");
    expect(r.newBuys.detail).toMatch(/Positions already open are managed under their own stops/);
    /* "not an exit" contains the word, so the assertion is on the CLAIM rather than the token. */
    expect(r.applied.stance).toMatch(/not an exit/);
    expect(r.applied.stance).not.toMatch(/is an exit|out of the market|fully in cash/i);
  });

  it("R2's half-size entries come from the payload, never from the tier", () => {
    /* Tempting to derive: R2 means half size. It does not — `resolve_new_buys` gives R2 HALF
       only when the sentinel is clear and the data is usable, so the tier alone cannot say. */
    const stated = ready({ tier: "R2", new_buys: "half" });
    const silent = ready({ tier: "R2", new_buys: null });

    expect(stated.newBuys.label).toBe("Half size");
    expect(stated.newBuys.detail).toMatch(/smaller entry, not a stop on entries/);
    expect(silent.newBuys.recognised).toBe(false);
    expect(silent.newBuys.label).toBe("Not recorded");
    expect(silent.newBuys.detail).toMatch(/will not guess one from the tier/);
  });

  it("R2 from R1 is described as risk reduced, matching the brief's sentence", () => {
    const r = ready({ tier: "R2", previous_tier: "R1" });

    expect(r.movement).toBe("reduced");
    expect(r.movementSentence).toBe("Risk reduced to R2 from R1.");
  });

  it("R2 from R3 is a raise, not a reduction", () => {
    const r = ready({ tier: "R2", previous_tier: "R3" });

    expect(r.movement).toBe("raised");
    expect(r.movementSentence).toBe("Risk raised to R2 from R3.");
  });
});

/* --------------------------------------------------------------------- the sentinel veto */

describe("the momentum sentinel", () => {
  it("reads the 50-DMA veto from the desk's own sentence, verbatim", () => {
    const sentinel = sentinelReading([
      "Momentum sentinel below its 50-DMA — new entries vetoed.",
      "New entries blocked by the momentum sentinel veto.",
    ]);

    expect(sentinel.veto.state).toBe("in-force");
    expect(sentinel.veto.evidence).toEqual([
      "Momentum sentinel below its 50-DMA — new entries vetoed.",
      "New entries blocked by the momentum sentinel veto.",
    ]);
    expect(sentinel.indexName).toBe("NIFTY 500 MOMENTUM 50");
  });

  it("reads the 200-DMA floor separately from the 50-DMA veto", () => {
    const sentinel = sentinelReading(["Momentum sentinel confirmed below its 200-DMA."]);

    expect(sentinel.floor.state).toBe("in-force");
    /* A 200-DMA break floors the tier at R3; it is not itself the entry veto. */
    expect(sentinel.veto.state).toBe("not-stated");
  });

  it("silence is not a clear sentinel", () => {
    /* The evaluation writes a sentinel sentence only when the sentinel changes something, and
       `/desk/regime` returns rendered sentences rather than `reason_codes`. So nothing said is
       genuinely ambiguous, and it resolves to "not stated" rather than to "clear". */
    const sentinel = sentinelReading(["No risk-off rule fired and R1 conditions were not all met."]);

    expect(sentinel.veto.state).toBe("not-stated");
    expect(sentinel.veto.evidence).toEqual([]);
    /* Corrected 11 Sep 2026 with the copy it asserts: the reason a reader needs, not the column
       that holds it. See `no-internals.test.tsx` for why — a screenshot found an internal host,
       a route and three field names in front of a customer. */
    expect(sentinel.veto.summary).toMatch(/never reads silence as an all-clear/);
    expect(sentinel.veto.summary).not.toMatch(/reason_codes|\/api\/v\d/);
  });

  it("only the desk's positive sentence makes the sentinel read as clear", () => {
    const sentinel = sentinelReading(["Health, breadth and the momentum sentinel all risk-on."]);

    expect(sentinel.veto.state).toBe("reported-clear");
    expect(sentinel.floor.state).toBe("reported-clear");
  });

  it("an unconfirmed sentinel is its own state, not a clear one", () => {
    const sentinel = sentinelReading(["Momentum sentinel state is not yet confirmed."]);

    expect(sentinel.veto.state).toBe("unconfirmed");
    expect(sentinel.floor.state).toBe("unconfirmed");
  });

  it("a reworded sentence degrades to not-stated, never to clear", () => {
    /* The wording is not an audit contract; `reason_codes` is. So the failure direction is
       chosen: the panel can under-report a veto it cannot see, and can never invent one or
       claim the sentinel is clear on a sentence it does not recognise. */
    const sentinel = sentinelReading(["Momentum sentinel is below its 50 day average."]);

    expect(sentinel.veto.state).toBe("not-stated");
  });
});

/* ------------------------------------------------------------- staleness and currentness */

describe("whether the stance is current", () => {
  it("stale data makes the panel stop calling the tier current", () => {
    const r = ready({ data_stale: true });

    expect(r.current).toBe(false);
    expect(r.standingHeadline).toBe("Last recorded stance — not confirmed current");
    expect(r.notices.map((n) => n.id)).toContain("data-stale");
    expect(r.notices.find((n) => n.id === "data-stale")?.nextStep).toBeTruthy();
  });

  it("a manual-action flag is critical and carries one next step", () => {
    const r = ready({ manual_action_required: true });

    const notice = r.notices.find((n) => n.id === "manual-action");
    expect(notice?.severity).toBe("critical");
    expect(notice?.nextStep).toMatch(/desk console/);
    expect(r.current).toBe(false);
  });

  it("an evaluation older than its own next date is flagged overdue", () => {
    const r = ready({ next_evaluation_date: "2026-09-11" }, "2026-09-14");

    expect(r.notices.map((n) => n.id)).toContain("overdue");
    expect(r.current).toBe(false);
  });

  it("an evaluation whose next date has not arrived is current", () => {
    const r = ready({ next_evaluation_date: "2026-09-11" }, "2026-09-07");

    expect(r.notices).toEqual([]);
    expect(r.current).toBe(true);
    expect(r.standingHeadline).toBe("Stance in force");
  });

  it("without a date to compare against, the panel says so rather than assuming fresh", () => {
    const r = ready({ next_evaluation_date: "2026-09-11" }, null);

    const notice = r.notices.find((n) => n.id === "overdue-unknown");
    expect(notice?.severity).toBe("info");
    expect(notice?.detail).toMatch(/not claiming the stance is current/);
    /* Informational alone does not unset `current` — it states what was not checked. */
    expect(r.current).toBe(true);
  });

  it("an unreachable desk shows no tier at all", () => {
    const state = regimeReading({
      regime: null,
      today: "2026-09-07",
      unavailableReason: "/desk/regime timed out after 4000ms.",
    });

    expect(state.kind).toBe("unavailable");
    if (state.kind !== "unavailable") throw new Error("unreachable");
    expect(state.notice.detail).toMatch(/timed out after 4000ms/);
    expect(state.notice.nextStep).toBeTruthy();
  });

  it("an unreachable desk with no stated reason still explains itself", () => {
    const state = regimeReading({ regime: null, today: "2026-09-07" });

    if (state.kind !== "unavailable") throw new Error("unreachable");
    /* Corrected 11 Sep 2026 with the copy it asserts: the reason a reader needs, not the column
       that holds it. See `no-internals.test.tsx` for why — a screenshot found an internal host,
       a route and three field names in front of a customer. */
    expect(state.notice.detail).toMatch(/gave no reason/);
    expect(state.notice.detail).not.toMatch(/\/api\/v\d|https?:\/\//);
  });
});

/* ------------------------------------------------------- what the response does not carry */

describe("figures RegimeOut does not carry", () => {
  it("names each one as unavailable with where it lives and what would surface it", () => {
    const r = ready();

    expect(r.unavailable.map((u) => u.id)).toEqual([
      "candidate-tier",
      "index-dma",
      "confirmation-progress",
      "breadth-coverage",
      "pending-execution",
      "algorithm-version",
    ]);
    for (const item of r.unavailable) {
      expect(item.reason.length).toBeGreaterThan(40);
      expect(item.unblockedBy.length).toBeGreaterThan(10);
    }
  });

  it("names the desk's three structural indices rather than the swing book's gate index", () => {
    /* CLAUDE.md, "When the code and a doc disagree": the SWING gate reads NIFTY MidSmallcap 400
       (SW17, 67df9b4). This is the WEEKLY desk's regime, whose `RegimeConfig.structural_indices`
       are NIFTY 50, NIFTY MIDCAP 150 and NIFTY SMLCAP 250. Two books, two questions. */
    const r = ready();
    const dma = r.unavailable.find((u) => u.id === "index-dma");

    expect(dma?.name).toMatch(/NIFTY 50, NIFTY MIDCAP 150, NIFTY SMLCAP 250/);
    expect(JSON.stringify(r.unavailable)).not.toMatch(/MidSmallcap 400|NIFTY 500\b(?! MOMENTUM)/);
  });

  it("declares a candidate tier as absent and never derives one from the applied tier", () => {
    const r = ready({ tier: "R2", previous_tier: "R1" });
    const candidate = r.unavailable.find((u) => u.id === "candidate-tier");

    /* Corrected 11 Sep 2026 with the copy it asserts: the reason a reader needs, not the column
       that holds it. See `no-internals.test.tsx` for why — a screenshot found an internal host,
       a route and three field names in front of a customer. */
    expect(candidate?.reason).toMatch(/before the one-rung-a-week limit is applied/);
    expect(candidate?.reason).not.toMatch(/raw_candidate_tier|\/api\/v\d/);
    expect(JSON.stringify(r)).not.toMatch(/candidateTier/);
  });
});

/* ----------------------------------------------------------------- reasons, mode, breadth */

describe("the rest of the reading", () => {
  it("quotes the desk's reasons unchanged and in the order it recorded them", () => {
    const reasons = [
      "Breadth below the R3 threshold.",
      "Momentum sentinel confirmed below its 200-DMA.",
      "Risk reduction applied directly to the candidate tier.",
    ];
    const r = ready({ reasons });

    expect(r.reasons).toEqual(reasons);
    expect(r.reasonsNote).toBeNull();
  });

  it("drops blank reason strings rather than rendering an empty bullet", () => {
    const r = ready({ reasons: ["Breadth below the R3 threshold.", "  ", ""] });

    expect(r.reasons).toEqual(["Breadth below the R3 threshold."]);
  });

  it("says so when an evaluation recorded no reasons", () => {
    const r = ready({ reasons: [] });

    expect(r.reasons).toEqual([]);
    expect(r.reasonsNote).toMatch(/recorded no reasons/);
  });

  it("says whether the stance is being acted on at all", () => {
    /* In `observe` the desk forces no selling. A tier shown without this reads as though the
       book had been moved to the cap when nothing has happened. */
    expect(ready({ mode: "observe" }).mode.detail).toMatch(/acts on none of it/);
    expect(ready({ mode: "enforce" }).mode.label).toBe("Enforcing");
    expect(ready({ mode: null }).mode.detail).toMatch(/did not record which mode/);
  });

  it("every figure it emits is a Metric carrying a definition", () => {
    /* §6.2 rule 1, made checkable: a component cannot render one of these without also having
       the explanation to hand, so there is no path to an unexplained dash. */
    const r = ready({
      breadth_pct: null,
      actual_equity_pct: null,
      target_equity_cap_pct: null,
      signal_date: null,
      next_evaluation_date: null,
    });

    for (const m of [r.breadth, r.actualEquity, r.targetEquity, r.signalDate, r.nextEvaluation, r.gap.metric]) {
      expect(m.label).toBeTruthy();
      expect(m.definition.length).toBeGreaterThan(20);
      expect(m.value).toBeNull();
      expect(m.unavailable).toBeTruthy();
    }
  });

  it("rounds percentages to one place at the edge, so the API and the screen cannot disagree", () => {
    const r = ready({ breadth_pct: 38.44, actual_equity_pct: 77.96 });

    expect(r.breadth.value).toBe("38.4");
    expect(r.actualEquity.value).toBe("78.0");
  });

  it("prints an unrecognised tier as written rather than mapping it to a guess", () => {
    const r = ready({ tier: "R5", previous_tier: "R4" });

    expect(r.applied.recognised).toBe(false);
    expect(r.applied.label).toBe("R5");
    expect(r.current).toBe(false);
    /* The tier changed; only the DIRECTION is unknown. Calling that "unchanged" would be the
       quieter of the two lies and still a lie. */
    expect(r.movement).toBe("unstated");
    expect(r.movementSentence).toMatch(/the direction is not stated/);
    /* And the changed heading is explained rather than left to be noticed. */
    expect(r.notices.map((n) => n.id)).toContain("tier-unrecognised");
  });

  it("carries the evaluation date as a figure like the two that can be missing", () => {
    const r = ready({ evaluated_at: "2026-09-04T18:30:00+05:30" });

    expect(r.evaluated.value).toBe("2026-09-04");
    expect(r.evaluated.definition.length).toBeGreaterThan(20);
  });

  it("holds the tier when it is unchanged, and says so", () => {
    const r = ready({ tier: "R2", previous_tier: "R2" });

    expect(r.movement).toBe("unchanged");
    expect(r.movementSentence).toBe("Held at R2; unchanged from the previous evaluation.");
  });

  it("says so when the desk recorded no previous tier", () => {
    const r = ready({ tier: "R1", previous_tier: null });

    expect(r.previous).toBeNull();
    expect(r.movement).toBe("first");
    expect(r.movementSentence).toMatch(/no previous tier/);
  });
});
