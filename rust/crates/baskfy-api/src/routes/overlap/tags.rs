//! The pieces of `baskfy_core.catalyst_tags` the overlap page's corrections need (lane
//! writes-sleeves): the keyword baseline (`tag_headline`), Laya's cached answer as a tag
//! (`tag_from_laya`), the resolution between them and a person's word (`resolve_tag`), and the
//! content-addressed cache key (`cache_key`). Display context only: no tag reaches a rank, a
//! size or an order.
//!
//! `RULES` and `TAG_SCHEMA` are generated from the Python module (its phrase table, in order, and
//! the eight-hex hash of `LAYA_QUESTIONS`); `tests` pins `cache_key` against Python's output.
//! Python's `str.casefold()` is reproduced exactly from `casefold_table.rs`, every code point
//! CPython folds to something else (generated from the API's interpreter; the key is stored, so
//! an approximation would file a correction under a key Python never reads).

use baskfy_db::OrderedJson;
use sha2::{Digest, Sha256};

use crate::routes::sleeves::common::{J, Obj};

include!("rules_table.rs");
include!("casefold_table.rs");

/// `CACHE_KEY_VERSION`.
pub(crate) const CACHE_KEY_VERSION: &str = "v2";
/// `LAYA_CONFIDENCE_FLOOR`.
pub(crate) const LAYA_CONFIDENCE_FLOOR: f64 = 0.60;
/// `SOURCE_RULES`.
pub(crate) const SOURCE_RULES: &str = "rules";
/// `SOURCE_LAYA`.
pub(crate) const SOURCE_LAYA: &str = "laya";
/// `SOURCE_CORRECTED`.
pub(crate) const SOURCE_CORRECTED: &str = "corrected";
/// `EventType`, in declaration order.
pub(crate) const EVENT_TYPES: [&str; 8] = [
    "earnings",
    "order",
    "approval",
    "fundraising",
    "governance",
    "corporate_action",
    "routine",
    "other",
];

/// `PRIORITY_OF[event_type]`.
pub(crate) fn priority_of(event_type: &str) -> &'static str {
    match event_type {
        "order" | "earnings" | "approval" => "high",
        "fundraising" | "corporate_action" | "governance" => "medium",
        _ => "low",
    }
}

/// Python's `str.casefold()`: full case folding, code point by code point (no context, so a
/// final sigma folds like any other), from the table generated off the API's own interpreter.
pub(crate) fn casefold(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match CASEFOLD.binary_search_by_key(&u32::from(c), |(cp, _)| *cp) {
            Ok(i) => out.push_str(CASEFOLD.get(i).map_or("", |(_, f)| *f)),
            Err(_) => out.push(c),
        }
    }
    out
}

/// `cache_key(headline)`: `catalyst_tag:v2:<schema>:<sha256(strip().casefold())>`.
pub(crate) fn cache_key(headline: &str) -> String {
    let folded = casefold(crate::routes::sleeves::pyd::py_strip(headline));
    let digest = Sha256::digest(folded.as_bytes());
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    format!("catalyst_tag:{CACHE_KEY_VERSION}:{TAG_SCHEMA}:{hex}")
}

/// `CatalystTag`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Tag {
    /// `event_type`.
    pub(crate) event_type: &'static str,
    /// `matched`.
    pub(crate) matched: Vec<&'static str>,
    /// `source`.
    pub(crate) source: &'static str,
    /// `confidence`.
    pub(crate) confidence: Option<f64>,
    /// `disagrees_with`.
    pub(crate) disagrees_with: Option<String>,
}

fn is_word(b: u8) -> bool {
    b.is_ascii_lowercase() || b.is_ascii_digit()
}

/// `re.compile(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])").search(text)`.
fn hit(text: &str, phrase: &str) -> bool {
    let t = text.as_bytes();
    let p = phrase.as_bytes();
    if p.is_empty() || p.len() > t.len() {
        return p.is_empty();
    }
    (0..=t.len() - p.len()).any(|i| {
        &t[i..i + p.len()] == p
            && (i == 0 || !is_word(t[i - 1]))
            && t.get(i + p.len()).is_none_or(|b| !is_word(*b))
    })
}

fn first_hit(text: &str) -> Option<Tag> {
    for (event_type, phrases) in RULES {
        let hits: Vec<&'static str> = phrases.iter().copied().filter(|p| hit(text, p)).collect();
        if !hits.is_empty() {
            return Some(Tag {
                event_type,
                matched: hits,
                source: SOURCE_RULES,
                confidence: None,
                disagrees_with: None,
            });
        }
    }
    None
}

/// `SUBJECT_SEPARATOR`.
const SUBJECT_SEPARATOR: &str = " — ";

/// `tag_headline(headline)`: the subject line first, then the rest; `other` when nothing hits.
pub(crate) fn tag_headline(headline: &str) -> Tag {
    let other = Tag {
        event_type: "other",
        matched: Vec::new(),
        source: SOURCE_RULES,
        confidence: None,
        disagrees_with: None,
    };
    let strip = crate::routes::sleeves::pyd::py_strip;
    if strip(headline).is_empty() {
        return other;
    }
    let (subject, rest) = match headline.split_once(SUBJECT_SEPARATOR) {
        Some((s, r)) => (strip(s), strip(r)),
        None => (strip(headline), ""),
    };
    if let Some(t) = first_hit(&casefold(subject)) {
        return t;
    }
    if !rest.is_empty()
        && let Some(t) = first_hit(&casefold(rest))
    {
        return t;
    }
    other
}

fn number(v: Option<&OrderedJson>) -> Option<f64> {
    match v {
        Some(OrderedJson::Int(i)) =>
        {
            #[allow(clippy::cast_precision_loss)]
            Some(*i as f64)
        }
        Some(OrderedJson::UInt(u)) =>
        {
            #[allow(clippy::cast_precision_loss)]
            Some(*u as f64)
        }
        Some(OrderedJson::BigInt(s)) => s.parse::<f64>().ok(),
        Some(OrderedJson::Float(f)) => Some(*f),
        _ => None,
    }
}

/// `round(x, 4)` (Python's correctly rounded `round`, via its shortest repr).
fn round4(x: f64) -> f64 {
    let text = format!("{x:.4}");
    text.parse().unwrap_or(x)
}

/// `tag_from_laya(answer)`.
pub(crate) fn tag_from_laya(answer: Option<&OrderedJson>) -> Option<Tag> {
    let answer = answer?;
    let OrderedJson::Object(_) = answer else {
        return None;
    };
    let Some(OrderedJson::Str(choice)) = answer.get("choice") else {
        return None;
    };
    let confidence =
        number(answer.get("answer_confidence")).or_else(|| match answer.get("probabilities") {
            Some(p @ OrderedJson::Object(_)) => number(p.get(choice)),
            _ => None,
        })?;
    let event_type = EVENT_TYPES.iter().find(|e| **e == choice.as_str())?;
    Some(Tag {
        event_type,
        matched: Vec::new(),
        source: SOURCE_LAYA,
        confidence: Some(round4(confidence)),
        disagrees_with: None,
    })
}

/// `resolve_tag(rules, laya, corrected)`.
pub(crate) fn resolve_tag(rules: &Tag, laya: Option<&Tag>, corrected: Option<&'static str>) -> Tag {
    if let Some(c) = corrected {
        let overruled = match laya {
            Some(l) if l.event_type != c => Some(l),
            _ if rules.event_type != c => Some(rules),
            _ => None,
        };
        return Tag {
            event_type: c,
            matched: Vec::new(),
            source: SOURCE_CORRECTED,
            confidence: None,
            disagrees_with: overruled.map(|o| format!("{}:{}", o.source, o.event_type)),
        };
    }
    let Some(laya) = laya else {
        return rules.clone();
    };
    let (chosen, other) = if laya.confidence.is_some_and(|c| c >= LAYA_CONFIDENCE_FLOOR) {
        (laya, rules)
    } else {
        (rules, laya)
    };
    if other.event_type == chosen.event_type {
        return chosen.clone();
    }
    Tag {
        disagrees_with: Some(format!("{}:{}", other.source, other.event_type)),
        ..chosen.clone()
    }
}

/// `_tag_out(tag)` (`OverlapTagOut`).
pub(crate) fn tag_out(tag: &Tag) -> J {
    Obj::new()
        .put("event_type", J::s(tag.event_type))
        .put("review_priority", J::s(priority_of(tag.event_type)))
        .put(
            "matched",
            J::Arr(tag.matched.iter().map(|m| J::s(m)).collect()),
        )
        .put("source", J::s(tag.source))
        .put("confidence", tag.confidence.map_or(J::Null, J::Float))
        .put("disagrees_with", J::opt_str(tag.disagrees_with.clone()))
        .put("corrected", J::Bool(tag.source == SOURCE_CORRECTED))
        .done()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn the_cache_key_is_pythons() {
        assert_eq!(
            cache_key(" Hello "),
            "catalyst_tag:v2:95019bd9:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        );
        assert_eq!(casefold("Straße ﬁle"), "strasse file");
        // CPython 3.12's own answers: the folds a lower-casing misses (iota subscript, the Greek
        // symbol variants, a decomposed `ǰ`, Cherokee folding *up*, a dotted capital I).
        let s = "ΣΑΣ ᾈϐ ǰ ᏸꭰ İ ﬃ Kelvin";
        assert_eq!(casefold(s), "σασ ἀιβ j\u{30c} ᏰᎠ i\u{307} ffi kelvin");
        assert_eq!(
            cache_key(&format!("  {s} ")),
            "catalyst_tag:v2:95019bd9:5ec690670903a3016dba5c62c07c5d2c5f7a1da563ca41156f6024b7721cdfc4"
        );
    }

    #[test]
    fn the_subject_decides_first() {
        let t = tag_headline("Financial Results — Acme Limited has informed the Exchange");
        assert_eq!(t.event_type, "earnings");
        assert_eq!(tag_headline("   ").event_type, "other");
        assert!(hit("award of order", "order"));
        assert!(!hit("orders", "order"));
        assert!(!hit("reorder", "order"));
    }

    #[test]
    fn a_correction_names_what_it_overruled() {
        let rules = tag_headline("Financial Results");
        let t = resolve_tag(&rules, None, Some("order"));
        assert_eq!(t.disagrees_with.as_deref(), Some("rules:earnings"));
        assert_eq!(
            resolve_tag(&rules, None, Some("earnings")).disagrees_with,
            None
        );
    }
}
