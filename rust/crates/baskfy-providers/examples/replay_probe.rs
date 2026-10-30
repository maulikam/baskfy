//! Drives the replay adapter over a bundle with a list of calls and prints every answer as
//! JSON, for `tools/parity/replay_selfcheck.py`, which runs the Python adapter over the same
//! calls and requires the two outputs to be equal (DECISIONS-RS R7.3). No network: the block is
//! armed before anything runs.
//!
//! `cargo run -p baskfy-providers --example replay_probe -- <bundle> <calls.json>`
#![allow(clippy::print_stdout)]

use std::collections::HashMap;
use std::sync::Arc;

use baskfy_providers::ProviderError;
use baskfy_providers::composite::SupplementaryReads;
use baskfy_providers::ports::{BarsProvider, ReferenceProvider};
use baskfy_providers::replay::ReplayProvider;
use chrono::NaiveDate;
use polars::prelude::{AnyValue, DataFrame, DataType};
use rust_decimal::Decimal;
use serde_json::{Value, json};

fn kind(dtype: &DataType) -> String {
    match dtype {
        DataType::String => "str".to_owned(),
        DataType::Date => "date".to_owned(),
        DataType::Int64 => "i64".to_owned(),
        DataType::Decimal(p, s) => format!("dec({p},{s})"),
        other => format!("{other}"),
    }
}

fn frame(df: &DataFrame) -> Value {
    let schema: Vec<Value> = df
        .columns()
        .iter()
        .map(|c| json!([c.name().as_str(), kind(c.dtype())]))
        .collect();
    let mut rows = Vec::new();
    for i in 0..df.height() {
        let mut row = Vec::new();
        for c in df.columns() {
            let v = c.get(i).unwrap_or(AnyValue::Null);
            row.push(match v {
                AnyValue::Null => Value::Null,
                AnyValue::String(s) => json!(s),
                AnyValue::StringOwned(s) => json!(s.as_str()),
                AnyValue::Int64(x) => json!(x.to_string()),
                AnyValue::Date(days) => json!(
                    NaiveDate::from_num_days_from_ce_opt(days + 719_163)
                        .map(|d| d.to_string())
                        .unwrap_or_default()
                ),
                AnyValue::Decimal(m, _, s) => json!(
                    Decimal::try_from_i128_with_scale(m, u32::try_from(s).unwrap_or(0))
                        .map(|d| d.to_string())
                        .unwrap_or_default()
                ),
                other => json!(format!("{other:?}")),
            });
        }
        rows.push(Value::Array(row));
    }
    json!({"schema": schema, "rows": rows})
}

fn err(e: &ProviderError) -> Value {
    json!({"error_type": e.python_type(), "error": e.to_string()})
}

fn ok<T: serde::Serialize>(r: Result<T, ProviderError>) -> Value {
    match r {
        Ok(v) => json!({"ok": serde_json::to_value(v).unwrap_or(Value::Null)}),
        Err(e) => err(&e),
    }
}

fn date(v: &Value, key: &str) -> NaiveDate {
    NaiveDate::parse_from_str(v[key].as_str().unwrap_or(""), "%Y-%m-%d").unwrap_or_default()
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    baskfy_providers::net::block_network();
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (Some(bundle), Some(calls)) = (args.first(), args.get(1)) else {
        println!("usage: replay_probe <bundle> <calls.json>");
        return;
    };
    let provider = match ReplayProvider::open(bundle) {
        Ok(p) => Arc::new(p),
        Err(e) => {
            println!("{}", json!([err(&e)]));
            return;
        }
    };
    let calls: Vec<Value> =
        serde_json::from_str(&std::fs::read_to_string(calls).unwrap_or_default())
            .unwrap_or_default();
    let mut out = Vec::new();
    for call in &calls {
        let p = &provider;
        let answer = match call["method"].as_str().unwrap_or("") {
            "list_instruments" => ok(p.list_instruments().await),
            "listings" => ok(p.listings().await),
            "sme_listings" => ok(p.sme_listings().await),
            "etf_symbols" => ok(p.etf_symbols().await),
            "fno_underlyings" => ok(p.fno_underlyings().await),
            "index_level" => ok(p.index_level(call["slug"].as_str().unwrap_or("")).await),
            "daily_bars" => match p
                .daily_bars(
                    call["token"].as_i64().unwrap_or(0),
                    date(call, "start"),
                    date(call, "end"),
                )
                .await
            {
                Ok(df) => json!({"ok": frame(&df)}),
                Err(e) => err(&e),
            },
            "bhavcopy" => match p.bhavcopy(date(call, "on")).await {
                Ok(df) => json!({"ok": frame(&df)}),
                Err(e) => err(&e),
            },
            "corporate_actions" => ok(p.corporate_actions(date(call, "since")).await),
            "index_constituents" => ok(p
                .index_constituents(call["index_slug"].as_str().unwrap_or(""), date(call, "on"))
                .await),
            "index_snapshots" => ok(p.index_snapshots(date(call, "on")).await),
            "equity_fundamentals" => {
                let symbols: Vec<String> = call["symbols"]
                    .as_array()
                    .map(|a| {
                        a.iter()
                            .filter_map(|s| s.as_str().map(str::to_owned))
                            .collect()
                    })
                    .unwrap_or_default();
                let hint: HashMap<String, String> = HashMap::new();
                ok(
                    p.equity_fundamentals(date(call, "on"), &symbols, Some(&hint))
                        .await,
                )
            }
            other => json!({"error_type": "unknown", "error": other}),
        };
        out.push(answer);
    }
    println!("{}", Value::Array(out));
}
