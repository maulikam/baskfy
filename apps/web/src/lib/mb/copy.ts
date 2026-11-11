/**
 * The honest numbers for `/mb`'s "what this is" panel (PM7). Source: `STRATEGY.md` §0 and §3
 * (PM0.8 table, dead names included, variant b) and `out/pm4/PM4-RESULT.md` (DECISIONS-PM PM4.10).
 * Fixed text, not a live read: they change only when the research is re-run, and then here.
 */

export const MB_BOOK = {
  from: "2014",
  cagrPct: "17.2",
  maxDrawdownPct: "-66.3",
  sharpe: "0.75",
  equalWeightCagrPct: "13.0",
  equalWeightDrawdownPct: "-55.8",
  smallcapCagrPct: "16.3",
  smallcapDrawdownPct: "-60.8",
} as const;

/** Signal year → top-20 vs universe: share of names that touched 2× within 12 months, and the mean 12-month return. */
export interface MbYear {
  year: string;
  baseHitPct: string;
  top20HitPct: string;
  top20MeanPct: string;
  universeMeanPct: string;
}

export const MB_YEARS: readonly MbYear[] = [
  {
    year: "2014",
    baseHitPct: "18.7",
    top20HitPct: "30.9",
    top20MeanPct: "+41",
    universeMeanPct: "+26",
  },
  {
    year: "2015",
    baseHitPct: "5.3",
    top20HitPct: "13.6",
    top20MeanPct: "+1",
    universeMeanPct: "+3",
  },
  {
    year: "2016",
    baseHitPct: "13.3",
    top20HitPct: "32.1",
    top20MeanPct: "+49",
    universeMeanPct: "+32",
  },
  {
    year: "2017",
    baseHitPct: "10.1",
    top20HitPct: "28.9",
    top20MeanPct: "+22",
    universeMeanPct: "+5",
  },
  {
    year: "2018",
    baseHitPct: "1.1",
    top20HitPct: "5.9",
    top20MeanPct: "-33",
    universeMeanPct: "-18",
  },
  {
    year: "2019",
    baseHitPct: "4.7",
    top20HitPct: "4.4",
    top20MeanPct: "-19",
    universeMeanPct: "-6",
  },
  {
    year: "2020",
    baseHitPct: "39.3",
    top20HitPct: "61.9",
    top20MeanPct: "+110",
    universeMeanPct: "+77",
  },
  {
    year: "2021",
    baseHitPct: "17.1",
    top20HitPct: "42.9",
    top20MeanPct: "+48",
    universeMeanPct: "+18",
  },
  {
    year: "2022",
    baseHitPct: "11.9",
    top20HitPct: "21.9",
    top20MeanPct: "+26",
    universeMeanPct: "+24",
  },
  {
    year: "2023",
    baseHitPct: "30.2",
    top20HitPct: "53.8",
    top20MeanPct: "+91",
    universeMeanPct: "+56",
  },
  {
    year: "2024",
    baseHitPct: "8.0",
    top20HitPct: "15.1",
    top20MeanPct: "-14",
    universeMeanPct: "-1",
  },
  {
    year: "2025",
    baseHitPct: "5.5",
    top20HitPct: "4.0",
    top20MeanPct: "-12",
    universeMeanPct: "+3",
  },
];

export const MB_CAVEATS: readonly string[] = [
  "Price and volume only. Fundamentals were tested in PM4 and added nothing out of sample; none feeds this ranking.",
  "It ranks doublers better than chance every year, but in weak years its top 20 fell further than the universe (2015, 2018, 2019, 2024, 2025).",
  "2025 showed no edge on the full universe, and on the 2022–25 re-test it failed the 2024 window too.",
  "A list of names, not a portfolio and not advice. Nothing on this page places, plans or schedules an order.",
];

/** PM8's expectation for the paper book (KICKOFF PM8; STRATEGY §4, PM0.17). Fractions. */
export const MB_PAPER_EXPECT = {
  touched2x: "20–23%",
  halved: "about 7%",
  median12m: "about +6%",
  triggerDrawdown: "about −26%",
} as const;

export const MB_BOOK_NAMES: Record<string, string> = {
  book: "Monthly picks: top 20 investable each month, equal weight, held 250 sessions",
  hi63_20: "Shadow: 63-day-high trigger, 1/20 of a tranche a name",
  hi63_16: "Shadow: 63-day-high trigger, 1/16 of a tranche a name",
  book_overlay:
    "Shadow: the monthly picks, in cash while the Smallcap 250 is under its 100-day average (5 closes to confirm)",
};
