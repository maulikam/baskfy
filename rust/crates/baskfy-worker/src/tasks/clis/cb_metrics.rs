//! `baskfy_worker.cb_metrics_cli`: `cb_metrics` for every non-archived basket, without Celery.

use baskfy_db::OrderedJson;

use super::cli::{Outcome, Parsed, Spec, code, context, emit, iso, parse_date};
use crate::tasks::run_task;

const SPEC: Spec = Spec {
    valued: &["--date"],
    switches: &[],
    optional: &[],
    positionals: 0,
};

pub(super) async fn run(args: &[String]) -> Outcome {
    let parsed = Parsed::parse(args, SPEC)?;
    let ctx = context()?;
    let day = match parsed.get("--date") {
        Some(text) => parse_date(text)?,
        None => ctx.today_ist(),
    };
    let result = run_task(
        &ctx,
        "baskfy.cb.compute_metrics",
        Vec::new(),
        vec![("trade_date".to_owned(), OrderedJson::Str(iso(day)))],
    )
    .await;
    ctx.db.close().await;
    let result = result?;
    emit(&result);
    Ok(code(succeeded(&result)))
}

/// `baskets >= 0`, an `int`.
pub(super) fn succeeded(result: &OrderedJson) -> bool {
    match result.get("baskets") {
        Some(OrderedJson::Int(n)) => *n >= 0,
        Some(OrderedJson::UInt(_)) => true,
        None => true,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_exit_code_reads_the_basket_count() {
        let with = |v: OrderedJson| OrderedJson::Object(vec![("baskets".to_owned(), v)]);
        assert!(succeeded(&with(OrderedJson::Int(0))));
        assert!(!succeeded(&with(OrderedJson::Int(-1))));
        assert!(!succeeded(&with(OrderedJson::Str("3".to_owned()))));
        assert!(succeeded(&OrderedJson::Object(Vec::new())));
    }
}
