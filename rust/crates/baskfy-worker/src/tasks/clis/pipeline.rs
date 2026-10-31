//! `baskfy_worker.pipeline_cli`: the nightly pipeline for one date, on one transaction.

use baskfy_db::OrderedJson;
use sqlx::PgConnection;

use super::cli::{Failure, Outcome, Parsed, Spec, code, emit, iso, parse_date, say, sort_keys};
use crate::error::WorkerError;
use crate::orchestrator::{PipelineOutcome, run_nightly_pipeline};
use crate::settings::WorkerSettings;
use crate::steps::RunStatus;
use crate::tasks::context::build_pipeline_dependencies;

const SPEC: Spec = Spec {
    valued: &["--date"],
    switches: &["--json"],
    optional: &[],
    positionals: 0,
};

pub(super) async fn run(args: &[String]) -> Outcome {
    let parsed = Parsed::parse(args, SPEC)?;
    let day = parse_date(parsed.required("--date")?)?;
    let worker = WorkerSettings::from_env()?;
    let deps = build_pipeline_dependencies(&worker, None)
        .await
        .map_err(|e| Failure::Raised(WorkerError::Settings(e)))?;
    let db = super::cli::connect(None)?;
    let mut tx = db.begin().await?;
    let outcome = match run_nightly_pipeline(&mut tx, day, &deps, None).await {
        Ok(o) => o,
        Err(e) => {
            let _ = tx.rollback().await;
            db.close().await;
            return Err(Failure::Raised(e));
        }
    };
    let gate = gate_payload(&mut tx, &outcome).await?;
    tx.commit().await?;
    db.close().await;
    if parsed.on("--json") {
        emit(&sort_keys(payload(&outcome, gate)));
    } else {
        say(&render(&outcome));
    }
    Ok(code(outcome.status == RunStatus::Succeeded))
}

/// `outcome.gate.to_payload()`: the gate step's detail, which carries exactly that payload.
async fn gate_payload(
    conn: &mut PgConnection,
    outcome: &PipelineOutcome,
) -> Result<OrderedJson, Failure> {
    let (Some(_), Some(run_id)) = (&outcome.gate, outcome.run_id) else {
        return Ok(OrderedJson::Null);
    };
    let detail: Option<String> = sqlx::query_scalar(
        "SELECT error::text FROM pipeline_run_step WHERE run_id = $1 \
         AND step = 'data_quality_gate' ORDER BY id DESC LIMIT 1",
    )
    .bind(run_id)
    .fetch_optional(&mut *conn)
    .await?
    .flatten();
    let parsed = detail
        .map(|t| OrderedJson::parse(&t))
        .transpose()
        .map_err(|e| Failure::Raised(WorkerError::python("ValueError", e.to_string())))?
        .unwrap_or(OrderedJson::Null);
    Ok(OrderedJson::Object(
        ["trade_date", "passed", "checks"]
            .iter()
            .map(|k| {
                (
                    (*k).to_owned(),
                    parsed.get(k).cloned().unwrap_or(OrderedJson::Null),
                )
            })
            .collect(),
    ))
}

/// `_payload(outcome)`.
pub(super) fn payload(outcome: &PipelineOutcome, gate: OrderedJson) -> OrderedJson {
    let opt = |v: Option<String>| v.map_or(OrderedJson::Null, OrderedJson::Str);
    OrderedJson::Object(vec![
        (
            "trade_date".to_owned(),
            OrderedJson::Str(iso(outcome.trade_date)),
        ),
        (
            "status".to_owned(),
            OrderedJson::Str(outcome.status.value().to_owned()),
        ),
        (
            "data_version".to_owned(),
            outcome
                .data_version
                .map_or(OrderedJson::Null, OrderedJson::Int),
        ),
        (
            "failed_step".to_owned(),
            opt(outcome.failed_step.map(|s| s.value().to_owned())),
        ),
        ("error".to_owned(), opt(outcome.error.clone())),
        (
            "steps_completed".to_owned(),
            OrderedJson::Array(
                outcome
                    .steps_completed
                    .iter()
                    .map(|s| OrderedJson::Str(s.value().to_owned()))
                    .collect(),
            ),
        ),
        ("gate".to_owned(), gate),
    ])
}

/// The text report.
pub(super) fn render(outcome: &PipelineOutcome) -> String {
    let mut lines = vec![format!(
        "{}: {}",
        iso(outcome.trade_date),
        outcome.status.value()
    )];
    if let Some(v) = outcome.data_version {
        lines.push(format!("  published data_version {v}"));
    }
    if let Some(gate) = &outcome.gate {
        for r in &gate.results {
            lines.push(format!(
                "  [{}] {:<7} {}: {}",
                r.assertion, r.status, r.name, r.message
            ));
        }
    }
    if let Some(error) = outcome.error.as_deref().filter(|e| !e.is_empty()) {
        lines.push(format!("  error: {error}"));
    }
    lines.join("\n")
}
