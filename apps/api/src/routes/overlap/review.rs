//! `baskfy_core.candidate_review`, ported for `GET /overlap` and the review labels (wave 6, lane
//! api_routes; R5.21's first half). Pure: a row's stored facts in, the sentence Laya is shown, its
//! content-addressed key and the rules' attention opinion out — no table, no model, no clock.
//!
//! **Numbers are said exactly as Python says them.** Every figure is a stored `Decimal` (or an
//! `int`) put through the default context (28 digits, half-even) and Python's `format(d, ".Nf")`
//! — a half-even rescale with no digit limit that keeps a negative zero's sign — via
//! [`WideDecimal`]; a float never enters. The state is hashed (`review_key`), so one character
//! off would be a different key and a different label.
//!
//! The question is **attention, never a trade**: nothing here reaches a rank, a size or an order.

use std::collections::BTreeSet;

use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use sha2::{Digest, Sha256};

use super::tags::{CACHE_KEY_VERSION, tag_headline};
use crate::routes::common::py_space;

/// `REVIEW_CONFIDENCE_FLOOR`.
pub const REVIEW_CONFIDENCE_FLOOR: f64 = 0.60;
/// `SOURCE_LAYA` / `SOURCE_LABELLED` / `SOURCE_RULES`.
pub const SOURCE_LAYA: &str = "laya";
/// A person's label.
pub const SOURCE_LABELLED: &str = "labelled";
/// The rules' baseline.
pub const SOURCE_RULES: &str = "rules";
/// `REVIEW_SCHEMA = question_schema_hash(REVIEW_QUESTIONS)` — eight hex characters, printed by
/// the live Python (`baskfy_core.candidate_review.REVIEW_SCHEMA`, 30 Oct 2026).
pub const REVIEW_SCHEMA: &str = "903c43c0";
/// `ReviewLabel`.
pub const REVIEW_LABELS: [&str; 3] = ["look_first", "worth_a_look", "skip"];
/// `SHUT_GATES`.
const SHUT_GATES: [&str; 4] = ["RED", "SHUT", "CLOSED", "OFF"];
/// `ADVERSE_PHRASES`.
const ADVERSE_PHRASES: [&str; 14] = [
    "sebi order",
    "nclt",
    "court order",
    "insolvency",
    "penalty",
    "default",
    "orders passed",
    "action(s) taken",
    "actions taken",
    "demand order",
    "show cause",
    "show-cause",
    "resignation",
    "resigns",
];
/// `_TOP_TENTH` / `_UPPER_HALF`.
const TOP_TENTH: i64 = 9;
const UPPER_HALF: i64 = 5;

/// `Number = Decimal | int | str | bool | None` — one stored fact.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Num {
    /// A `numeric` column.
    Dec(WideDecimal),
    /// An integer column.
    Int(i64),
    /// A word (`sector_slug`, `failed_filters` joined).
    Str(String),
    /// A flag.
    Bool(bool),
    /// `None`.
    Null,
}

impl Num {
    /// An optional `numeric`.
    #[must_use]
    pub fn dec(d: Option<rust_decimal::Decimal>) -> Self {
        d.map_or(Self::Null, |d| Self::Dec(WideDecimal::from(d)))
    }

    /// An optional integer.
    #[must_use]
    pub fn int(i: Option<i64>) -> Self {
        i.map_or(Self::Null, Self::Int)
    }

    /// An optional word.
    #[must_use]
    pub fn str(s: Option<String>) -> Self {
        s.map_or(Self::Null, Self::Str)
    }

    /// An optional flag.
    #[must_use]
    pub fn bool(b: Option<bool>) -> Self {
        b.map_or(Self::Null, Self::Bool)
    }
}

/// `RowFacts`: one strategy's stored numbers about the row (`numbers` in insertion order).
#[derive(Debug, Clone)]
pub struct RowFacts {
    /// `strategy` (`swing`, `volume_breakout`, `three_weeks_tight`).
    pub strategy: &'static str,
    /// `detail`.
    pub detail: String,
    /// `actionable`.
    pub actionable: bool,
    /// `numbers`.
    pub numbers: Vec<(&'static str, Num)>,
}

impl RowFacts {
    fn get(&self, key: &str) -> Option<&Num> {
        self.numbers.iter().find(|(k, _)| *k == key).map(|(_, v)| v)
    }
}

/// `RowContext`: the day around the row (dict fields as insertion-ordered pairs).
#[derive(Debug, Clone, Default)]
pub struct RowContext {
    /// `gates`.
    pub gates: Vec<(String, String)>,
    /// `breadth_pct`.
    pub breadth_pct: Vec<(String, WideDecimal)>,
    /// `sector`.
    pub sector: Option<String>,
    /// `screens`: `(name, rank, of)`.
    pub screens: Vec<(String, Option<i64>, Option<i64>)>,
    /// `sessions`.
    pub sessions: Vec<(String, NaiveDate)>,
    /// `filing_published`.
    pub filing_published: Option<NaiveDate>,
    /// `earnings_date`.
    pub earnings_date: Option<NaiveDate>,
    /// `screen_runs` (a `dict` comprehension: a repeated name keeps its first place, last value).
    pub screen_runs: Vec<(String, NaiveDate)>,
    /// `screens_changed`.
    pub screens_changed: Vec<String>,
}

/// `ReviewOpinion`.
#[derive(Debug, Clone, PartialEq)]
pub struct ReviewOpinion {
    /// `label` (one of [`REVIEW_LABELS`]).
    pub label: &'static str,
    /// `confidence`.
    pub confidence: f64,
    /// `source`.
    pub source: &'static str,
    /// `reason`.
    pub reason: Option<String>,
}

// --- Python's numbers in words -----------------------------------------------------------------

fn d(n: i64) -> WideDecimal {
    WideDecimal::from(n)
}

/// `format(value, f".{places}f")`.
fn fixed(value: &WideDecimal, places: i64) -> String {
    value.rescale_exact(-places, Rounding::HalfEven).to_string()
}

/// `int(value)`: truncation toward zero, printed.
fn int_text(value: &WideDecimal) -> String {
    let text = value.rescale_exact(0, Rounding::Down).to_string();
    if text == "-0" { "0".to_owned() } else { text }
}

/// `int(value)` as a number (saturating; the facts are small).
fn int_of(value: &WideDecimal) -> i64 {
    int_text(value).parse().unwrap_or(0)
}

fn div(a: &WideDecimal, b: &WideDecimal) -> Option<WideDecimal> {
    a.div(b).ok()
}

/// `_num(n, key)`.
fn num(f: &RowFacts, key: &str) -> Option<WideDecimal> {
    match f.get(key)? {
        Num::Dec(v) => Some(v.clone()),
        Num::Int(i) => Some(d(*i)),
        Num::Str(_) | Num::Bool(_) | Num::Null => None,
    }
}

/// `_pct(value, places)`.
fn pct(value: Option<&WideDecimal>, places: i64) -> Option<String> {
    value.map(|v| format!("{}%", fixed(v, places)))
}

/// `_signed_pct(value)`.
fn signed_pct(value: &WideDecimal) -> String {
    format!(
        "{}% {}",
        fixed(&value.abs(), 0),
        if value.is_sign_negative() && !value.is_zero() {
            "below"
        } else {
            "above"
        }
    )
}

/// `_times(value)`.
fn times(value: Option<&WideDecimal>) -> Option<String> {
    value.map(|v| format!("{} times", fixed(v, 1)).replace(".0 times", " times"))
}

/// `_crore(value)`.
fn crore(value: &WideDecimal) -> String {
    let crores = div(value, &d(10_000_000)).unwrap_or_default();
    format!("average turnover Rs {} crore a day", fixed(&crores, 1))
}

/// `(close / level - 1) * 100`, or `None` when the level is missing or not positive.
fn distance(f: &RowFacts, key: &str, what: &str) -> Option<String> {
    let close = num(f, "close")?;
    let level = num(f, key)?;
    if level <= d(0) {
        return None;
    }
    let ratio = div(&close, &level)?;
    let pct = ratio.sub(&d(1)).mul(&d(100));
    Some(format!("{} {what}", signed_pct(&pct)))
}

/// `_stop_below(n, entry_key, stop_key, what)`.
fn stop_below(f: &RowFacts, entry_key: &str, stop_key: &str, what: &str) -> Option<String> {
    let entry = num(f, entry_key)?;
    let stop = num(f, stop_key)?;
    if entry <= d(0) {
        return None;
    }
    let below = div(&entry.sub(&stop), &entry)?.mul(&d(100));
    Some(format!(
        "{what} {} with the stop {}% below it",
        fixed(&entry, 2),
        fixed(&below, 1)
    ))
}

/// `_flags(n)`.
fn flags(f: &RowFacts) -> Vec<String> {
    let mut parts = Vec::new();
    if f.get("locked_upper_circuit") == Some(&Num::Bool(true)) {
        parts.push("locked in the upper circuit".to_owned());
    }
    if f.get("listed_within_2y") == Some(&Num::Bool(true)) {
        parts.push("listed within the last two years".to_owned());
    }
    if let Some(Num::Str(failed)) = f.get("failed_filters")
        && !failed.is_empty()
    {
        parts.push(format!(
            "failed the {} filter(s)",
            failed.to_lowercase().replace('_', " ")
        ));
    }
    parts
}

fn said(head: String, pieces: Vec<Option<String>>, extra: Vec<String>) -> String {
    let parts: Vec<String> = pieces
        .into_iter()
        .flatten()
        .chain(extra)
        .filter(|p| !p.is_empty())
        .collect();
    if parts.is_empty() {
        head
    } else {
        format!("{head}: {}", parts.join("; "))
    }
}

fn swing_words(f: &RowFacts) -> String {
    let (setup, status) = f.detail.split_once(" · ").unwrap_or((&f.detail, ""));
    let what = match setup {
        "EP" => "episodic pivot".to_owned(),
        "FLAG" => "flag".to_owned(),
        "PARABOLIC_SHORT" => "parabolic run (detect only)".to_owned(),
        other => other.to_lowercase(),
    };
    let where_ = match status {
        "GAP_DAY" => "gap day".to_owned(),
        "BREAKOUT_TODAY" => "breakout today above the pivot".to_owned(),
        "SETTING_UP" => "still setting up below the pivot".to_owned(),
        "TRIGGERED" => "triggered".to_owned(),
        other => other.to_lowercase().replace('_', " "),
    };
    let head = format!("Swing {what}, {where_}");
    let score = num(f, "score");
    let gap = pct(num(f, "gap_pct").as_ref(), 0);
    let rvol = times(num(f, "rvol").as_ref());
    let depth = pct(num(f, "base_depth_pct").as_ref(), 0);
    let bars = num(f, "base_bars");
    let tight = num(f, "tightness_adr");
    let dry = num(f, "dryup_ratio");
    let prior = pct(num(f, "prior_move_pct").as_ref(), 0);
    let adr = pct(num(f, "adr_pct").as_ref(), 1);
    let fast = num(f, "dist_ma_fast_pct");
    let slow = num(f, "dist_ma_slow_pct");
    let streak = num(f, "up_streak");
    let turnover = num(f, "turnover_avg");
    said(
        head,
        vec![
            score.map(|v| format!("setup score {} of 100", fixed(&v, 0))),
            gap.map(|v| format!("gapped {v}")),
            rvol.map(|v| format!("volume {v} its average")),
            depth.map(|v| format!("base {v} deep")),
            bars.map(|v| format!("base {} sessions long", int_text(&v))),
            tight.map(|v| format!("base tightness {} ADRs", fixed(&v, 1))),
            dry.map(|v| format!("volume dried up to {} of its base average", fixed(&v, 2))),
            prior.map(|v| format!("up {v} in the prior move")),
            adr.map(|v| format!("average daily range {v}")),
            fast.map(|v| format!("{} the fast moving average", signed_pct(&v))),
            slow.map(|v| format!("{} the slow moving average", signed_pct(&v))),
            streak
                .filter(|v| *v > d(0))
                .map(|v| format!("{} up sessions in a row", int_text(&v))),
            stop_below(f, "trigger", "stop_ref", "trigger"),
            distance(f, "pivot_high", "the pivot"),
            turnover.map(|v| crore(&v)),
        ],
        flags(f),
    )
}

fn vbt_words(f: &RowFacts) -> String {
    let head = if f.actionable {
        "Volume breakout signal".to_owned()
    } else {
        "Volume breakout scanned but rejected by the filters".to_owned()
    };
    let rvol = times(num(f, "rvol").as_ref());
    let chg = num(f, "change_pct");
    let pos = num(f, "close_position");
    let ret = num(f, "ret_20_pct");
    let tenth = pos.map(|p| int_of(&p.mul(&d(10))).min(9));
    let turnover = num(f, "turnover_avg_20");
    said(
        head,
        vec![
            rvol.map(|v| format!("volume {v} its 50-day average")),
            chg.map(|c| {
                format!(
                    "closed {} {}%",
                    if c >= d(0) { "up" } else { "down" },
                    fixed(&c.abs(), 0)
                )
            }),
            tenth.map(|t| {
                if t >= TOP_TENTH {
                    "in the top tenth of the day's range".to_owned()
                } else if t >= UPPER_HALF {
                    "in the upper half of the day's range".to_owned()
                } else {
                    "in the lower half of the day's range".to_owned()
                }
            }),
            ret.map(|r| {
                format!(
                    "{} {}% over the last 20 sessions",
                    if r >= d(0) { "up" } else { "down" },
                    fixed(&r.abs(), 0)
                )
            }),
            distance(f, "sma_200", "its 200-day average"),
            distance(f, "ema_21", "its 21-day average"),
            distance(f, "high_20_prior", "the prior 20-day high"),
            stop_below(f, "limit_price", "stop_price", "entry limit"),
            turnover.map(|v| crore(&v)),
        ],
        flags(f),
    )
}

fn twt_words(f: &RowFacts) -> String {
    let head = if f.actionable {
        "Three weeks tight, entry today".to_owned()
    } else {
        "Three weeks tight, in the state".to_owned()
    };
    let rng = pct(num(f, "week_range_pct").as_ref(), 1);
    let closes = [
        num(f, "week_close_2"),
        num(f, "week_close_1"),
        num(f, "week_close_0"),
    ];
    let sessions = num(f, "sessions_in_state");
    let out = num(f, "sessions_out_before");
    let ratio = num(f, "month_low_ratio");
    let vol = num(f, "volume");
    let avg = num(f, "vol_sma_50");
    let turnover = num(f, "turnover_avg_20");
    let stop = num(f, "stop_preview");
    let close = num(f, "close");
    let closes_text = if closes.iter().any(Option::is_none) {
        None
    } else {
        Some(format!(
            "weekly closes {} oldest first",
            closes
                .iter()
                .flatten()
                .map(|c| fixed(c, 2))
                .collect::<Vec<_>>()
                .join(", ")
        ))
    };
    said(
        head,
        vec![
            rng.map(|r| format!("three weekly closes within {r} of each other")),
            closes_text,
            sessions.map(|s| {
                let n = int_of(&s);
                format!("{n} session{} in the state", if n == 1 { "" } else { "s" })
            }),
            out.map(|o| {
                format!(
                    "{} sessions out of the state before this entry",
                    int_text(&o)
                )
            }),
            ratio.map(|r| {
                format!(
                    "{}% above its three-month low",
                    fixed(&r.sub(&d(1)).mul(&d(100)), 0)
                )
            }),
            distance(f, "sma_dma", "its 200-day average"),
            match (&vol, &avg) {
                (Some(v), Some(a)) if *a > d(0) => div(v, a)
                    .and_then(|x| times(Some(&x)))
                    .map(|t| format!("volume {t} its 50-day average")),
                _ => None,
            },
            turnover.map(|v| crore(&v)),
            match (&stop, &close) {
                (Some(s), Some(c)) if *c > d(0) => div(&c.sub(s), c)
                    .map(|x| format!("stop {}% below the close", fixed(&x.mul(&d(100)), 0))),
                _ => None,
            },
        ],
        flags(f),
    )
}

/// `describe_row(facts)`: one sentence per strategy, each ending in a full stop.
#[must_use]
pub fn describe_row(facts: &[RowFacts]) -> String {
    facts
        .iter()
        .map(|f| {
            let sentence = match f.strategy {
                "swing" => swing_words(f),
                "volume_breakout" => vbt_words(f),
                "three_weeks_tight" => twt_words(f),
                other => format!("{other}: {}", f.detail),
            };
            format!("{}.", sentence.trim_end_matches('.'))
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// `describe_context(context)`.
#[must_use]
pub fn describe_context(context: Option<&RowContext>) -> String {
    let Some(c) = context else {
        return String::new();
    };
    let mut parts: Vec<String> = Vec::new();
    for (name, gate) in &c.gates {
        let mut line = format!("{name} gate {}", gate.to_lowercase());
        if let Some((_, b)) = c.breadth_pct.iter().find(|(n, _)| n == name) {
            line.push_str(&format!(
                " with {}% of the universe above its long average",
                fixed(b, 0)
            ));
        }
        parts.push(line);
    }
    if let Some(sector) = c.sector.as_deref().filter(|s| !s.is_empty()) {
        parts.push(format!("sector {}", sector.replace('-', " ")));
    }
    if !c.screens.is_empty() {
        let named: Vec<String> = c
            .screens
            .iter()
            .map(|(name, rank, of)| match (rank, of) {
                (Some(r), Some(o)) => format!("{name} #{r} of {o}"),
                _ => name.clone(),
            })
            .collect();
        parts.push(format!("on screens {}", named.join(", ")));
    }
    if parts.is_empty() {
        String::new()
    } else {
        format!("{}.", parts.join("; "))
    }
}

/// `_day(date)`: `"Thu 25 Sep 2026"`.
fn day(date: NaiveDate) -> String {
    date.format("%a %d %b %Y").to_string().replace(" 0", " ")
}

/// `_days(n)`.
fn days(n: i64) -> String {
    format!("{n} day{}", if n == 1 { "" } else { "s" })
}

/// `_relative(gap, what, when, before=, after=)`.
fn relative(gap: i64, what: &str, when: &str, before: &str, after: &str) -> String {
    if gap == 0 {
        format!("{what} on the session day")
    } else if gap > 0 {
        format!("{what} {} {before} ({when})", days(gap))
    } else {
        format!("{what} {} {after} ({when})", days(-gap))
    }
}

/// `_screen_ages(context, anchor)`.
fn screen_ages(c: &RowContext, anchor: NaiveDate) -> String {
    let ages: Vec<String> = c
        .screen_runs
        .iter()
        .map(|(name, run)| {
            let age = (anchor - *run).num_days();
            let note = if c.screens_changed.contains(name) {
                " (definition changed since)"
            } else {
                ""
            };
            if age == 0 {
                format!("{name} from the session day{note}")
            } else {
                let older = if age > 0 { "older" } else { "newer" };
                format!("{name} {} {older} than the session{note}", days(age.abs()))
            }
        })
        .collect();
    format!("screen ranks: {}", ages.join(", "))
}

/// `describe_timeline(context, filing_on_record=)`: every date relative to the setup's session.
#[must_use]
pub fn describe_timeline(context: Option<&RowContext>, filing_on_record: bool) -> String {
    let Some(c) = context else {
        return String::new();
    };
    let Some(anchor) = c.sessions.iter().map(|(_, d)| *d).max() else {
        return String::new();
    };
    let same: Vec<&str> = c
        .sessions
        .iter()
        .filter(|(_, d)| *d == anchor)
        .map(|(n, _)| n.as_str())
        .collect();
    let mut first = format!(
        "{} facts are from the session of {}",
        same.join(" and "),
        day(anchor)
    );
    for (name, d) in c.sessions.iter().filter(|(_, d)| *d != anchor) {
        first.push_str(&format!(
            "; {name} facts from {}, {} earlier",
            day(*d),
            days((anchor - *d).num_days())
        ));
    }
    let mut parts = vec![first];
    match (filing_on_record, c.filing_published) {
        (true, None) => parts.push("the filing's date is not on record".to_owned()),
        (true, Some(published)) => parts.push(relative(
            (anchor - published).num_days(),
            "the filing was published",
            &day(published),
            "before the session",
            "after the session",
        )),
        _ => {}
    }
    if let Some(earnings) = c.earnings_date {
        parts.push(relative(
            (anchor - earnings).num_days(),
            if earnings >= anchor {
                "results are due"
            } else {
                "results were"
            },
            &day(earnings),
            "before the session",
            "ahead",
        ));
    }
    if !c.screen_runs.is_empty() {
        parts.push(screen_ages(c, anchor));
    }
    format!("{}.", parts.join("; "))
}

/// `STATE_FIELDS`.
pub const STATE_FIELDS: [&str; 4] = ["filing", "timeline", "context", "setup"];

/// `review_state(facts, filing, context)`: the non-empty fields in [`STATE_FIELDS`] order.
#[must_use]
pub fn review_state(
    facts: &[RowFacts],
    filing: Option<&str>,
    context: Option<&RowContext>,
) -> Vec<(&'static str, String)> {
    let on_record = filing.is_some_and(|f| !f.trim_matches(py_space).is_empty());
    let filing_text = match filing {
        Some(f) if on_record => f.trim_matches(py_space).to_owned(),
        _ => "No filing on record.".to_owned(),
    };
    let fields = [
        ("filing", filing_text),
        ("timeline", describe_timeline(context, on_record)),
        ("context", describe_context(context)),
        ("setup", describe_row(facts)),
    ];
    fields.into_iter().filter(|(_, v)| !v.is_empty()).collect()
}

/// `json.dumps(state, sort_keys=True, ensure_ascii=False)`.
#[must_use]
pub fn state_json(state: &[(&'static str, String)]) -> String {
    let mut sorted: Vec<&(&'static str, String)> = state.iter().collect();
    sorted.sort_by(|a, b| a.0.cmp(b.0));
    let body: Vec<String> = sorted
        .iter()
        .map(|(k, v)| {
            format!(
                "{}: {}",
                crate::routes::options::derivatives::shared::py_json_str_utf8(k),
                crate::routes::options::derivatives::shared::py_json_str_utf8(v)
            )
        })
        .collect();
    format!("{{{}}}", body.join(", "))
}

/// `review_key(state)`: `candidate_review:v2:<schema>:<sha256>`.
#[must_use]
pub fn review_key(state: &[(&'static str, String)]) -> String {
    let digest = Sha256::digest(state_json(state).as_bytes());
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    format!("candidate_review:{CACHE_KEY_VERSION}:{REVIEW_SCHEMA}:{hex}")
}

fn json_number(v: Option<&OrderedJson>) -> Option<f64> {
    match v {
        #[allow(clippy::cast_precision_loss)]
        Some(OrderedJson::Int(i)) => Some(*i as f64),
        #[allow(clippy::cast_precision_loss)]
        Some(OrderedJson::UInt(u)) => Some(*u as f64),
        Some(OrderedJson::BigInt(s)) => s.parse::<f64>().ok(),
        Some(OrderedJson::Float(f)) => Some(*f),
        _ => None,
    }
}

/// `round(x, 4)`.
fn round4(x: f64) -> f64 {
    format!("{x:.4}").parse().unwrap_or(x)
}

/// `opinion_from_laya(answer)`: the sidecar's cached answer as an opinion.
#[must_use]
pub fn opinion_from_laya(answer: &OrderedJson) -> Option<ReviewOpinion> {
    let OrderedJson::Object(_) = answer else {
        return None;
    };
    let Some(OrderedJson::Str(choice)) = answer.get("choice") else {
        return None;
    };
    let confidence = json_number(answer.get("answer_confidence")).or_else(|| {
        match answer.get("probabilities") {
            Some(p @ OrderedJson::Object(_)) => json_number(p.get(choice)),
            _ => None,
        }
    })?;
    let label = REVIEW_LABELS.iter().find(|l| **l == choice.as_str())?;
    Some(ReviewOpinion {
        label,
        confidence: round4(confidence),
        source: SOURCE_LAYA,
        reason: None,
    })
}

/// `shown(opinion)`.
#[must_use]
pub fn shown(opinion: &ReviewOpinion) -> bool {
    opinion.source == SOURCE_LABELLED
        || opinion.source == SOURCE_RULES
        || opinion.confidence >= REVIEW_CONFIDENCE_FLOOR
}

/// `catalyst_tags.adverse_phrases(headline)`.
#[must_use]
pub fn adverse_phrases(headline: Option<&str>) -> Vec<&'static str> {
    let tag = tag_headline(headline.unwrap_or(""));
    if tag.event_type != "governance" {
        return Vec::new();
    }
    tag.matched
        .into_iter()
        .filter(|p| ADVERSE_PHRASES.contains(p))
        .collect()
}

fn strategy_name(strategy: &str) -> &str {
    match strategy {
        "swing" => "Swing",
        "volume_breakout" => "Volume breakout",
        "three_weeks_tight" => "Three weeks tight",
        other => other,
    }
}

/// `rules_opinion(facts, filing_headline, filing_event, filing_priority, context)`.
#[must_use]
pub fn rules_opinion(
    facts: &[RowFacts],
    filing_headline: Option<&str>,
    filing_event: Option<&str>,
    filing_priority: Option<&str>,
    context: Option<&RowContext>,
) -> ReviewOpinion {
    let mut reasons: Vec<String> = Vec::new();
    let actionable: Vec<&RowFacts> = facts.iter().filter(|f| f.actionable).collect();
    let adverse = adverse_phrases(filing_headline);
    let locked = facts
        .iter()
        .any(|f| f.get("locked_upper_circuit") == Some(&Num::Bool(true)));
    let acting: BTreeSet<&str> = actionable
        .iter()
        .map(|f| strategy_name(f.strategy))
        .collect();
    let gate_of = |name: &str| -> String {
        context
            .and_then(|c| c.gates.iter().find(|(n, _)| n == name))
            .map(|(_, g)| g.to_uppercase())
            .unwrap_or_default()
    };
    // `sorted(shut)`: the set's names in code-point order (BTreeSet's order).
    let shut: Vec<&str> = acting
        .iter()
        .copied()
        .filter(|name| SHUT_GATES.contains(&gate_of(name).as_str()))
        .collect();
    if actionable.is_empty() {
        reasons.push("no strategy could act on the row as it stands".to_owned());
    }
    if !adverse.is_empty() {
        reasons.push(format!("the filing is adverse ({})", adverse.join(", ")));
    }
    if locked {
        reasons.push("locked in the upper circuit".to_owned());
    }
    if !shut.is_empty() {
        reasons.push(format!("{} gate shut", shut.join(", ")));
    }
    if !reasons.is_empty() {
        return ReviewOpinion {
            label: "skip",
            confidence: 1.0,
            source: SOURCE_RULES,
            reason: Some(reasons.join("; ")),
        };
    }
    if filing_priority == Some("high") {
        return ReviewOpinion {
            label: "look_first",
            confidence: 1.0,
            source: SOURCE_RULES,
            reason: Some(format!(
                "a strategy could act and the filing is material ({})",
                filing_event.unwrap_or("None")
            )),
        };
    }
    let what = filing_event.map_or_else(
        || "no filing on record".to_owned(),
        |e| format!("the filing is {}", e.replace('_', " ")),
    );
    ReviewOpinion {
        label: "worth_a_look",
        confidence: 1.0,
        source: SOURCE_RULES,
        reason: Some(format!("a strategy could act; {what}")),
    }
}

/// `datetime.astimezone(IST).date()` for a stored UTC stamp.
#[must_use]
pub fn ist_date(t: chrono::DateTime<chrono::Utc>) -> NaiveDate {
    (t + chrono::Duration::minutes(330)).date_naive()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn dec(s: &str) -> Num {
        Num::Dec(s.parse().unwrap())
    }

    #[test]
    fn decimals_are_said_as_python_formats_them() {
        assert_eq!(fixed(&"2.5".parse().unwrap(), 0), "2");
        assert_eq!(fixed(&"3.5".parse().unwrap(), 0), "4");
        assert_eq!(fixed(&"-0.4".parse().unwrap(), 0), "-0");
        assert_eq!(fixed(&"12.30".parse().unwrap(), 1), "12.3");
        assert_eq!(times(Some(&"4.0".parse().unwrap())).unwrap(), "4 times");
        assert_eq!(signed_pct(&"-3.6".parse().unwrap()), "4% below");
        assert_eq!(
            day(NaiveDate::from_ymd_opt(2026, 10, 5).unwrap()),
            "Mon 5 Oct 2026"
        );
    }

    #[test]
    fn a_swing_row_in_words() {
        let facts = RowFacts {
            strategy: "swing",
            detail: "EP · GAP_DAY".to_owned(),
            actionable: true,
            numbers: vec![
                ("score", dec("82.50")),
                ("close", dec("110.00")),
                ("trigger", dec("105.00")),
                ("stop_ref", dec("97.00")),
                ("pivot_high", dec("100.00")),
                ("gap_pct", dec("9.20")),
                ("rvol", dec("6.00")),
                ("up_streak", Num::Int(0)),
                ("turnover_avg", Num::Int(84_000_000)),
                ("locked_upper_circuit", Num::Bool(false)),
                ("sector_slug", Num::Str("capital-goods".to_owned())),
            ],
        };
        assert_eq!(
            describe_row(&[facts]),
            "Swing episodic pivot, gap day: setup score 82 of 100; gapped 9%; volume 6 times its \
             average; trigger 105.00 with the stop 7.6% below it; 10% above the pivot; average \
             turnover Rs 8.4 crore a day."
        );
    }
}
