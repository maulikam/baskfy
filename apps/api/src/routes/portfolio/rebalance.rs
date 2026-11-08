//! `RebalanceOut.model_validate({**payload, "id": ..., "created_at": ...})` — pydantic's lax
//! validation of a stored `portfolio_rebalance.payload`, for the canonical shapes the writer
//! (`RebalancePayload.model_dump(mode="json")`) produces. Anything else is the `ValidationError`
//! Python raises, which the API answers with its 500 catch-all.

use baskfy_core::decimal::WideDecimal;
use baskfy_db::OrderedJson;
use chrono::{DateTime, NaiveDate, Utc};
use serde::Serialize;

use super::num;
use super::params::parse_py_int;
use crate::wire;

/// `RebalanceNameOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct RebalanceNameOut {
    instrument_id: i64,
    symbol: String,
    name: String,
    rank: Option<i64>,
    reason: Option<String>,
}

/// `TargetWeightOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct TargetWeightOut {
    instrument_id: i64,
    symbol: String,
    name: String,
    rank: i64,
    #[serde(serialize_with = "num::ser")]
    weight: WideDecimal,
    action: String,
}

/// `RebalanceScreenOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct RebalanceScreenOut {
    public_id: String,
    name: String,
}

/// `RebalanceOut` (`RebalancePayload` + `id`, `created_at`).
#[derive(Debug, Clone, Serialize)]
pub(crate) struct RebalanceOut {
    #[serde(serialize_with = "wire::ser::date")]
    as_of: NaiveDate,
    #[serde(serialize_with = "wire::ser::opt_date")]
    requested_as_of: Option<NaiveDate>,
    data_version: i64,
    screen: RebalanceScreenOut,
    top_n: i64,
    hold_buffer: i64,
    exits: Vec<RebalanceNameOut>,
    inside_wrh: Vec<RebalanceNameOut>,
    entries: Vec<RebalanceNameOut>,
    holds: Vec<RebalanceNameOut>,
    target_weights: Vec<TargetWeightOut>,
    holdings_count: i64,
    screen_result_count: i64,
    delisted_count: i64,
    id: i64,
    #[serde(serialize_with = "wire::ser::datetime")]
    created_at: DateTime<Utc>,
}

type Fields<'a> = &'a [(String, OrderedJson)];

fn object<'a>(value: &'a OrderedJson, what: &str, allowed: &[&str]) -> Result<Fields<'a>, String> {
    let OrderedJson::Object(entries) = value else {
        return Err(format!("{what}: not an object"));
    };
    if let Some((k, _)) = entries.iter().find(|(k, _)| !allowed.contains(&k.as_str())) {
        return Err(format!("{what}.{k}: extra_forbidden"));
    }
    Ok(entries)
}

fn field<'a>(entries: Fields<'a>, key: &str) -> Option<&'a OrderedJson> {
    entries.iter().rev().find(|(k, _)| k == key).map(|(_, v)| v)
}

fn int(value: Option<&OrderedJson>, what: &str) -> Result<i64, String> {
    match value {
        Some(OrderedJson::Int(i)) => Ok(*i),
        Some(OrderedJson::Float(f)) if f.fract() == 0.0 && f.is_finite() => format!("{f:.0}")
            .parse()
            .map_err(|_| format!("{what}: int")),
        Some(OrderedJson::Str(s)) => parse_py_int(s)
            .and_then(|v| i64::try_from(v).ok())
            .ok_or_else(|| format!("{what}: int_parsing")),
        _ => Err(format!("{what}: int")),
    }
}

fn opt_int(value: Option<&OrderedJson>, what: &str) -> Result<Option<i64>, String> {
    match value {
        None => Ok(None),
        Some(OrderedJson::Null) => Ok(None),
        some => int(some, what).map(Some),
    }
}

fn string(value: Option<&OrderedJson>, what: &str) -> Result<String, String> {
    match value {
        Some(OrderedJson::Str(s)) => Ok(s.clone()),
        _ => Err(format!("{what}: string_type")),
    }
}

fn date(value: Option<&OrderedJson>, what: &str) -> Result<NaiveDate, String> {
    match value {
        Some(OrderedJson::Str(s)) => baskfy_core::screener::lax_date_from_str(s)
            .map_err(|(kind, _)| format!("{what}: {kind}")),
        _ => Err(format!("{what}: date_type")),
    }
}

fn decimal(value: Option<&OrderedJson>, what: &str) -> Result<WideDecimal, String> {
    match value {
        Some(OrderedJson::Str(s)) => s
            .trim()
            .parse::<WideDecimal>()
            .map_err(|_| format!("{what}: decimal_parsing")),
        Some(OrderedJson::Int(i)) => Ok(WideDecimal::from(*i)),
        Some(OrderedJson::Float(f)) => {
            baskfy_core::py::wide_from_float_str(*f).map_err(|_| format!("{what}: decimal_parsing"))
        }
        _ => Err(format!("{what}: decimal_type")),
    }
}

fn enum_value(value: Option<&OrderedJson>, what: &str, members: &[&str]) -> Result<String, String> {
    let s = string(value, what)?;
    if members.contains(&s.as_str()) {
        Ok(s)
    } else {
        Err(format!("{what}: enum"))
    }
}

fn names(value: Option<&OrderedJson>, what: &str) -> Result<Vec<RebalanceNameOut>, String> {
    let Some(OrderedJson::Array(items)) = value else {
        return Err(format!("{what}: list_type"));
    };
    items
        .iter()
        .map(|item| {
            let f = object(
                item,
                what,
                &["instrument_id", "symbol", "name", "rank", "reason"],
            )?;
            let reason = match field(f, "reason") {
                None | Some(OrderedJson::Null) => None,
                some => Some(enum_value(
                    some,
                    what,
                    &["rank_outside_buffer", "not_in_screen"],
                )?),
            };
            Ok(RebalanceNameOut {
                instrument_id: int(field(f, "instrument_id"), what)?,
                symbol: string(field(f, "symbol"), what)?,
                name: string(field(f, "name"), what)?,
                rank: opt_int(field(f, "rank"), what)?,
                reason,
            })
        })
        .collect()
}

fn weights(value: Option<&OrderedJson>) -> Result<Vec<TargetWeightOut>, String> {
    let Some(OrderedJson::Array(items)) = value else {
        return Err("target_weights: list_type".to_owned());
    };
    items
        .iter()
        .map(|item| {
            let what = "target_weights";
            let f = object(
                item,
                what,
                &[
                    "instrument_id",
                    "symbol",
                    "name",
                    "rank",
                    "weight",
                    "action",
                ],
            )?;
            Ok(TargetWeightOut {
                instrument_id: int(field(f, "instrument_id"), what)?,
                symbol: string(field(f, "symbol"), what)?,
                name: string(field(f, "name"), what)?,
                rank: int(field(f, "rank"), what)?,
                weight: decimal(field(f, "weight"), what)?,
                action: enum_value(field(f, "action"), what, &["enter", "hold"])?,
            })
        })
        .collect()
}

/// `RebalanceOut.model_validate({**payload, "id": id, "created_at": created_at})`.
///
/// # Errors
/// A sentence naming the first field pydantic would refuse.
pub(crate) fn rebalance_out(
    payload: &OrderedJson,
    id: i64,
    created_at: DateTime<Utc>,
) -> Result<RebalanceOut, String> {
    let f = object(
        payload,
        "payload",
        &[
            "as_of",
            "requested_as_of",
            "data_version",
            "screen",
            "top_n",
            "hold_buffer",
            "exits",
            "inside_wrh",
            "entries",
            "holds",
            "target_weights",
            "holdings_count",
            "screen_result_count",
            "delisted_count",
            "id",
            "created_at",
        ],
    )?;
    let screen_value = field(f, "screen").ok_or("screen: missing")?;
    let screen = object(screen_value, "screen", &["public_id", "name"])?;
    let requested_as_of = match field(f, "requested_as_of") {
        None | Some(OrderedJson::Null) => None,
        some => Some(date(some, "requested_as_of")?),
    };
    Ok(RebalanceOut {
        as_of: date(field(f, "as_of"), "as_of")?,
        requested_as_of,
        data_version: int(field(f, "data_version"), "data_version")?,
        screen: RebalanceScreenOut {
            public_id: string(field(screen, "public_id"), "screen.public_id")?,
            name: string(field(screen, "name"), "screen.name")?,
        },
        top_n: int(field(f, "top_n"), "top_n")?,
        hold_buffer: int(field(f, "hold_buffer"), "hold_buffer")?,
        exits: names(field(f, "exits"), "exits")?,
        inside_wrh: names(field(f, "inside_wrh"), "inside_wrh")?,
        entries: names(field(f, "entries"), "entries")?,
        holds: names(field(f, "holds"), "holds")?,
        target_weights: weights(field(f, "target_weights"))?,
        holdings_count: int(field(f, "holdings_count"), "holdings_count")?,
        screen_result_count: int(field(f, "screen_result_count"), "screen_result_count")?,
        delisted_count: int(field(f, "delisted_count"), "delisted_count")?,
        id,
        created_at,
    })
}
