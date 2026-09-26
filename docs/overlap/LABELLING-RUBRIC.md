# Labelling rubric for the overlap page's Laya column — DRAFT · ⚠ UNREVIEWED (26 Sep 2026)

**Status.** Drafted by an agent from what the three strategies' own code and decision logs
already say, so that the labels a person puts on `/build/overlap` rows — the fine-tuning set for
the row question, `PUT /overlap/reviews` — are given the same way on Monday as on Friday. Where the
repo settles a point, the line cites it. Where it does not, the line is marked **PROPOSED** and
the question is repeated in `NEEDS-MAULIK.md` ("Laya rubric"). Nothing here is read by any rank,
size or order path; the rules baseline (`baskfy_core.candidate_review.rules_opinion`) implements
the settled lines and none of the proposed ones.

The three words (`docs/07a` §17): **look first** — the pattern is strong and the filing is the
kind of news that produces it; **worth a look** — the pattern is real but the filing is routine or
unknown, or the filing is good but the pattern is weak; **skip** — the pattern is weak or stale,
or the filing is adverse. Attention, never a trade.

## 1. Rules that hold for every strategy

| # | Rule | Source |
|---|---|---|
| C1 | **A row no strategy could act on is a skip.** A swing PARABOLIC_SHORT (detect-only), a VBT SCAN_ONLY (any failed filter), a TWT name merely in the tight state (no entry event): each plan builder refuses it as it stands. | `swing/config.py:37` TRADEABLE_SETUPS; `vbt/signals.py:60-104`; `twt/plan.py:504-520`; settled, in the baseline |
| C2 | **A shut gate is a skip for that strategy's row.** Swing RED allows no new entries (rung 0, `new_entries_allowed=False`, `plan.build_entries` skips GATE_RED); VBT and TWT SHUT refuse every entry. Another strategy's shut gate does not touch a row it did not raise. | `swing/market.py:186-193`, `swing/plan.py:555-557`; `vbt/plan.py:219-221`; `twt/plan.py:504-505`; DECISIONS-SW ~826, VB0.4, TW0.4; settled, in the baseline |
| C3 | **Swing AMBER is not a skip.** It holds or lowers the exposure rung; it refuses nothing. Label on the setup and the filing. | `swing/market.py:102-121`; settled |
| C4 | **Locked in the upper circuit is a skip.** All three plan builders refuse the name outright ("no fill at band"); they never size it down. | `swing/plan.py:561-563`; `vbt/plan.py:222-224`; `twt/plan.py:516-520`; settled, in the baseline |
| C5 | **An adverse filing is a skip whatever the pattern.** The rules' phrases: SEBI/NCLT/court order, insolvency, penalty, default, "orders passed", "action(s) taken", demand order, show cause, resignation. Read from the headline by the rules, whatever tag the Filing column shows (OV11). | `catalyst_tags.ADVERSE_PHRASES`, `adverse_phrases`; settled, in the baseline |
| C6 | **Strong technicals with no filing on record: worth a look, not look first.** "Look first" is reserved for a row where the news explains the move; without a filing there is nothing to open first. | **PROPOSED** (Q1) |
| C7 | **Appearing on two or three strategies does not raise the label.** The count is a fact the page shows; the combined-sleeves study found confluence is a filter and every filter cost, and the product refuses a cross-strategy score. Label the strongest single setup. | DECISIONS-MERGE "served, not re-scored"; `research/combined/COMBINED.md`; **PROPOSED** as a labelling rule (Q2) |
| C8 | **Chronology decides whether the filing explains the move.** A material filing published on the setup's session or the one or two sessions before it is a catalyst; one published well after the breakout is not, and one published weeks before is history (the page already drops material filings older than 45 days). The `timeline` field carries these gaps. | `overlap.MATERIAL_WINDOW_DAYS`; **PROPOSED** gap of ≤ 2 sessions (Q3) |
| C9 | **Results due within the next few sessions.** Not a skip by itself; note it. A trader may not want to hold a fresh entry through a result. | **PROPOSED** (Q4) |

## 2. Swing — episodic pivot (EP)

Detector floor: gap ≥ 10%, relative volume ≥ 3.0, close in the upper half of the range
(`docs/swing/04-business-rules.md` §3.5). Score 0–100 = 35·(gap/20) + 35·(rvol/6) + 15·close
position + 15·(1 − prior move/30), clamped (`swing/setups.py:325-329`). Auto-watch threshold 60
(`swing/config.py:271`).

- **Look first** — score ≥ 60, and a material filing (order win, result, approval) published on the
  gap day or ≤ 2 sessions before it (C8). Gap ≥ 15% on ≥ 5× volume closing in the top tenth is the
  textbook row.
- **Worth a look** — score ≥ 60 with a routine, unknown or absent filing (C6); or score 40–60 with a
  material filing; or a gap that came with results/order news but the prior move is already ≥ 30%
  (the score's own penalty).
- **Skip** — score < 40; not actionable (C1); RED gate (C2); circuit lock (C4); adverse filing (C5);
  a stop wider than the sleeve allows (the plan skips it: `swing/stops.py:112-117`).
- **Tolerable weakness** — a close in the upper half rather than the top tenth; a prior move of
  10–30%. **Not tolerable** — relative volume near the 3.0 floor with a gap near 10%: that is the
  detector's edge, not a strong row. **PROPOSED** thresholds (Q5).

## 3. Swing — flag

Score 0–100 = 30·(1 − tightness/3 ADR) + 25·(prior move/60) + 20·(ADR%/8) + 15·(1 − depth/30) +
10·(1 − dry-up), +5 listed < 2y, +5 top-3 sector (`swing/setups.py:230-236`; `04-business-rules`
§79-83).

- **Look first** — status BREAKOUT_TODAY or TRIGGERED, score ≥ 60, tightness < 1.5 ADR, base depth
  ≤ 10%, dry-up < 0.7, and a material filing within C8's window.
- **Worth a look** — SETTING_UP below the pivot with score ≥ 60 (no entry today, so never look
  first — **PROPOSED**, Q6); or a breakout with a routine or absent filing.
- **Skip** — score < 40; depth > 20% or tightness > 2.5 ADR (a base that is not a base); C1–C5.

## 4. Volume breakout (VBT)

`SIGNAL` needs Chartink's scan (volume ≥ 3× the 50-day SMA, close ≥ ₹30, change ≥ 6.5%) **and**
all six trend filters — above the 200-DMA, above the prior 20-day high, 20-session return < 25%,
close position ≥ 0.6, change ≤ 15%, turnover ≥ ₹2 crore (`vbt/config.py:117-157`,
`vbt/signals.py:60-104`). There is no score; the sleeve ranks by turnover. The stop is a fixed 12%
under the fill; the working exit is a close below the 21-EMA (`vbt/exits.py:91-97`).

- **Look first** — SIGNAL with volume ≥ 5× its 50-day average, close position ≥ 0.8, and a material
  filing within C8's window.
- **Worth a look** — any SIGNAL with a routine, unknown or absent filing; or a SIGNAL near the
  filters' edges (volume 3–4×, close position 0.6–0.7, 20-session return 20–25%) with a good
  filing.
- **Skip** — SCAN_ONLY (C1, and the `failed_filters` say which); SHUT gate (C2); circuit lock (C4);
  adverse filing (C5). **PROPOSED** thresholds (Q5).

## 5. Three weeks tight (TWT)

In the state: three weekly closes within 3.01% of each other, ≥ 1.3× the three-month low, close
≥ ₹30, volume SMA ≥ 10,000, not an ETF. An **entry** additionally needs ≥ 5 sessions out of the
state before it and ₹2 crore average turnover (`twt/config.py:139-220`, `twt/plan.py:508-513`).
No score. The stop is a fixed distance under the entry session's open and only ever ratchets up.

- **Look first** — entry today, weekly range < 1.5%, entry-session volume ≥ 1.5× its 50-day
  average, and a material filing within C8's window.
- **Worth a look** — entry today with a routine, unknown or absent filing; or an entry on thin
  volume with a good filing.
- **Skip** — merely in the state (C1: no entry event); SHUT gate (C2); circuit lock (C4); adverse
  filing (C5); the liquidity floor failed. **PROPOSED** thresholds (Q5).

## 6. Filings — what each type means for the label

`catalyst_tags.PRIORITY_OF` fixes the priority per type; the vocabulary is the one settled on
25 Sep 2026.

| Type | Priority | For the label |
|---|---|---|
| order, earnings, approval | high | the news that produces a pattern — the only types that make **look first** |
| corporate_action (dividend, buyback, merger, stake, JV, capacity expansion) | medium | supportive but rarely the cause of a gap; worth a look unless the setup is textbook (**PROPOSED**, Q7) |
| fundraising (QIP, preferential, rights, warrants) | medium | dilution: neutral to negative for a fresh entry; never look first (**PROPOSED**, Q7) |
| governance (director/auditor/KMP change, rating) | medium | worth a look; **adverse phrases in it are a skip** (C5) |
| routine, other | low | says nothing about the move; the setup alone decides between worth a look and skip |

The feed stores a headline, a stamp and a link, never the filing (SW11B, A3). Order value as a
share of revenue, earnings surprise, approval geography, dilution and penalty size are **not
available** and would need the attachment — a D10 (market-data licensing) question, human-track.

## 7. Screens — what a rank on one means

A screen (`baskfy_core.screen_definition.ScreenDefinition`) is a universe index, a sort factor and
direction, the bucket the filters apply to (`all`, `decile_1`…`decile_5`, `top_50`, `top_100`), and
filter blocks (moving average, distance from high, positive days, circuits, market cap, PE, price,
second and third factors, up to three custom filters; Phase-2 ranking terms and family weights).
The factors (`factor_registry.FACTORS`, ~64 keys) are families: absolute returns over 1–12 months,
Sharpe-scaled returns, RSI, skip-month momentum (12-1, 12-2), non-momentum (volatility, beta, PE,
market cap, distance from high), path and participation (positive days, volume expansion, MA
stack), and momentum-quality terms (efficiency ratio, drawdown, Sortino, acceleration, residual
return, rank persistence), plus the desk's own `desk_score`.

- **Membership is the fact; the rank within the list is not a signal.** No document in the repo
  says rank 1 is materially better than rank 20, and the screener's bucket vocabulary (deciles)
  is the product's own statement that it thinks in buckets. **PROPOSED** (Q8).
- A screen result is as old as its run's `as_of`; the `timeline` field says how many days older
  than the setup's session it is, and whether the screen's definition was edited since the run
  (`ScreenHit.definition_changed`). A rank from a changed definition is an earlier version's.

## 8. Open questions for Maulik (mirrored in NEEDS-MAULIK.md)

1. **Q1** — strong technicals, no filing on record: always at most "worth a look"?
2. **Q2** — two or three strategies on one name: does that raise the label, or is C7 right?
3. **Q3** — the window in which a filing "explains" the move: ≤ 2 sessions before the setup session
   or on it? And a filing published after the breakout — never a catalyst?
4. **Q4** — results due within N sessions: a note, a downgrade to "worth a look", or a skip? N?
5. **Q5** — the numeric "strong" thresholds proposed in §2–§5 (EP score ≥ 60 and ≥ 5× volume; flag
   tightness < 1.5 ADR; VBT ≥ 5× volume and close position ≥ 0.8; TWT range < 1.5% and ≥ 1.5×
   volume): keep, move, or drop the numbers and label by eye?
6. **Q6** — a flag still SETTING_UP below the pivot: never "look first" because there is no entry
   today?
7. **Q7** — corporate_action and fundraising: supportive, neutral, or negative for a fresh entry?
8. **Q8** — screens: membership only, or does a top-20 rank on a momentum screen count for more?
9. **Q9** — three separate Laya questions (event type; positive/neutral/adverse/unclear;
   materiality high/medium/low/unknown) combined with the deterministic technical facts, instead
   of the one attention question? The review recommends it; it is a schema change and a new
   labelling surface, so it is not built.
