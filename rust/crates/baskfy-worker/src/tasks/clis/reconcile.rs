//! `baskfy_worker.reconcile_cli` as a subcommand: the reconciliation report (Prompt 19 §4).

mod checks;
mod export;

use std::path::{Path, PathBuf};

use baskfy_db::OrderedJson;

use self::checks::{CheckResult, reconcile, total_disagreements};
use self::export::read_export;
use super::cli::{Failure, Outcome, Parsed, Spec, code, emit, say, sort_keys, warn};
use crate::error::WorkerError;

const AS_OF: &str = "2026-08-18";

const SPEC: Spec = Spec {
    valued: &[
        "--export",
        "--symbols",
        "--bars",
        "--benchmark",
        "--limit",
        "--write",
    ],
    switches: &["--json"],
    optional: &[],
    positionals: 0,
};

pub(super) async fn run(args: &[String]) -> Outcome {
    let flags = Parsed::parse(args, SPEC)?;
    let limit = usize::try_from(flags.int("--limit")?.unwrap_or(25).max(0)).unwrap_or(0);
    let export_path = match flags.get("--export") {
        Some(p) => PathBuf::from(p),
        None => baskfy_providers::reference_export_io::reference_export_path()
            .map_err(WorkerError::from)?,
    };
    let mut export = read_export(&export_path)?;
    if let Some(symbols) = flags.get("--symbols") {
        let wanted: Vec<String> = symbols
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_uppercase)
            .collect();
        export = export.only_symbols(&wanted);
        if export.height() == 0 {
            let quoted: Vec<String> = wanted.iter().map(|s| format!("'{s}'")).collect();
            warn(&format!(
                "none of [{}] are in the export",
                quoted.join(", ")
            ));
            return Ok(std::process::ExitCode::from(2));
        }
    }
    let results = reconcile(&export)?;
    if flags.get("--bars").is_some() {
        return Err(Failure::Exit(
            "--bars (docs/13 §5 step 2) is not available in the Rust worker: it reads Parquet, \
             and the workspace builds polars without its parquet feature (law_one.rs). Run \
             `python -m baskfy_worker.reconcile_cli --bars ...` for the recomputation."
                .to_owned(),
        ));
    }
    let text = render(&results, limit);
    if let Some(target) = flags.get("--write") {
        write_report(Path::new(target), &text)?;
    }
    if flags.on("--json") {
        emit(&sort_keys(to_payload(&results)));
    } else {
        say(&text);
        say(&note_line(&export_path));
    }
    Ok(code(total_disagreements(&results) == 0))
}

fn write_report(target: &Path, text: &str) -> Result<(), Failure> {
    let os = |e: std::io::Error| Failure::Raised(WorkerError::python("OSError", e.to_string()));
    if let Some(parent) = target.parent()
        && !parent.as_os_str().is_empty()
    {
        std::fs::create_dir_all(parent).map_err(os)?;
    }
    std::fs::write(target, format!("{text}\n")).map_err(os)
}

fn note_line(export_path: &Path) -> String {
    let name = export_path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    format!(
        "NOTE: run with --bars to enable docs/13 §5 step 2 (full recomputation). The committed \
         snapshot is {name} and carries no price history, so no factor was recomputed from bars \
         in this run (it is an answer key, not a bar panel)."
    )
}

/// `render(results, limit=limit)`.
pub(crate) fn render(results: &[CheckResult], limit: usize) -> String {
    let rule = "=".repeat(92);
    let mut lines: Vec<String> = vec![
        "Reconciliation report — our formulas against the reference product's published output"
            .to_owned(),
        rule.clone(),
        String::new(),
        format!(
            "{:<30} {:<26} {:>7} {:>5}  status",
            "check", "docs", "cells", "bad"
        ),
        "-".repeat(92),
    ];
    for result in results {
        let mut status = if result.clean() {
            "clean"
        } else {
            "DISAGREEMENTS"
        };
        if !result.unresolved.is_empty() && result.clean() {
            status = "clean (see UNRESOLVED)";
        }
        lines.push(format!(
            "{:<30} {:<26} {:>7} {:>5}  {status}",
            result.name,
            result.reference,
            result.cells,
            result.disagreements.len()
        ));
    }
    let bad = total_disagreements(results);
    lines.extend([
        String::new(),
        format!("{bad} disagreement(s) over tolerance."),
        String::new(),
    ]);
    for result in results {
        if result.disagreements.is_empty() {
            continue;
        }
        lines.extend([
            format!("--- {} ({}) ---", result.name, result.reference),
            format!("    {}", result.description),
            String::new(),
        ]);
        for disagreement in result.disagreements.iter().take(limit) {
            lines.push(format!("    {disagreement}"));
        }
        if result.disagreements.len() > limit {
            lines.push(format!(
                "    ... and {} more",
                result.disagreements.len() - limit
            ));
        }
        lines.push(String::new());
    }
    let unresolved: Vec<&CheckResult> = results
        .iter()
        .filter(|r| !r.unresolved.is_empty())
        .collect();
    if !unresolved.is_empty() {
        lines.extend([
            "UNRESOLVED — investigated, documented, not settled by this data".to_owned(),
            rule.clone(),
            String::new(),
        ]);
        for result in unresolved {
            lines.push(format!("--- {} ({}) ---", result.name, result.reference));
            lines.extend(result.unresolved.iter().map(|l| format!("    {l}")));
            lines.push(String::new());
        }
    }
    let noted: Vec<&CheckResult> = results.iter().filter(|r| !r.notes.is_empty()).collect();
    if !noted.is_empty() {
        lines.extend(["NOTES".to_owned(), rule, String::new()]);
        for result in noted {
            for note in &result.notes {
                lines.push(format!("    {}: {note}", result.name));
            }
        }
        lines.push(String::new());
    }
    lines.join("\n")
}

fn s(text: impl Into<String>) -> OrderedJson {
    OrderedJson::Str(text.into())
}

fn strings(items: &[String]) -> OrderedJson {
    OrderedJson::Array(items.iter().map(|i| s(i.clone())).collect())
}

fn int(n: usize) -> OrderedJson {
    OrderedJson::Int(i64::try_from(n).unwrap_or(i64::MAX))
}

/// `to_payload(results)`.
pub(crate) fn to_payload(results: &[CheckResult]) -> OrderedJson {
    let checks = results
        .iter()
        .map(|r| {
            let disagreements = r
                .disagreements
                .iter()
                .map(|d| {
                    OrderedJson::Object(vec![
                        ("symbol".to_owned(), s(d.symbol.clone())),
                        ("column".to_owned(), s(d.column.clone())),
                        ("expected".to_owned(), s(d.expected.to_sci_string())),
                        ("published".to_owned(), s(d.actual.to_sci_string())),
                        ("delta".to_owned(), s(d.delta().to_sci_string())),
                        ("tolerance".to_owned(), s(d.tolerance.to_sci_string())),
                    ])
                })
                .collect();
            OrderedJson::Object(vec![
                ("name".to_owned(), s(r.name)),
                ("reference".to_owned(), s(r.reference)),
                ("description".to_owned(), s(r.description.clone())),
                ("cells".to_owned(), int(r.cells)),
                ("clean".to_owned(), OrderedJson::Bool(r.clean())),
                (
                    "disagreements".to_owned(),
                    OrderedJson::Array(disagreements),
                ),
                ("unresolved".to_owned(), strings(&r.unresolved)),
                ("notes".to_owned(), strings(&r.notes)),
            ])
        })
        .collect();
    OrderedJson::Object(vec![
        ("as_of".to_owned(), s(AS_OF)),
        (
            "disagreements".to_owned(),
            int(total_disagreements(results)),
        ),
        ("checks".to_owned(), OrderedJson::Array(checks)),
    ])
}

/// The committed export, parsed (for the tests).
#[cfg(test)]
fn committed_export() -> export::Frame {
    #[allow(clippy::unwrap_used)]
    let path = baskfy_providers::reference_export_io::reference_export_path().unwrap();
    #[allow(clippy::unwrap_used)]
    read_export(&path).unwrap()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic)]

    use super::export::{Cell, parse_export};
    use super::*;

    fn committed_report() -> String {
        std::fs::read_to_string(
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../../decile-blueprint/reconciliation/REPORT.md"),
        )
        .unwrap()
    }

    #[test]
    fn the_committed_report_is_what_this_port_renders_byte_for_byte() {
        let results = reconcile(&committed_export()).unwrap();
        assert_eq!(render(&results, 25) + "\n", committed_report());
    }

    #[test]
    fn the_committed_export_has_no_disagreement_and_surfaces_unresolved() {
        let results = reconcile(&committed_export()).unwrap();
        let text = render(&results, 25);
        assert!(text.contains("0 disagreement(s) over tolerance."));
        assert!(text.contains("UNRESOLVED"));
        assert_eq!(total_disagreements(&results), 0);
    }

    #[test]
    fn the_note_says_no_factor_was_recomputed_from_bars() {
        let line = note_line(Path::new("/x/reference-screen-export-2026-08-18.csv"));
        assert!(line.contains("no factor was recomputed from bars in this run"));
        assert!(line.contains("reference-screen-export-2026-08-18.csv"));
    }

    #[test]
    fn one_symbol_gives_five_sharpe_cells_and_declines_the_distribution_check() {
        let frame = committed_export().only_symbols(&["CUPID".to_owned()]);
        let payload = to_payload(&reconcile(&frame).unwrap());
        let OrderedJson::Array(checks) = payload.get("checks").unwrap() else {
            panic!("checks is a list");
        };
        let find = |name: &str| {
            checks
                .iter()
                .find(|c| c.get("name") == Some(&OrderedJson::Str(name.to_owned())))
                .unwrap()
        };
        assert_eq!(
            find("sharpe_identity").get("cells"),
            Some(&OrderedJson::Int(5))
        );
        let turnover = find("turnover_is_exchange_value");
        assert_eq!(turnover.get("clean"), Some(&OrderedJson::Bool(true)));
        let Some(OrderedJson::Array(notes)) = turnover.get("notes") else {
            panic!("notes is a list");
        };
        let OrderedJson::Str(first) = &notes[0] else {
            panic!("a note is text");
        };
        assert!(first.contains("below the 30"));
    }

    #[test]
    fn the_payload_names_the_date_and_both_inferred_areas() {
        let payload = to_payload(&reconcile(&committed_export()).unwrap());
        assert_eq!(
            payload.get("as_of"),
            Some(&OrderedJson::Str(AS_OF.to_owned()))
        );
        assert_eq!(payload.get("disagreements"), Some(&OrderedJson::Int(0)));
        let text = super::super::cli::pretty(&sort_keys(payload));
        assert!(text.contains("\"skip_month_momentum\""));
        assert!(text.contains("\"circuit_detection_rule\""));
    }

    fn tampered(column: &str, value: &str) -> export::Frame {
        let bytes =
            std::fs::read(baskfy_providers::reference_export_io::reference_export_path().unwrap())
                .unwrap();
        let text = String::from_utf8(bytes).unwrap();
        let mut lines: Vec<String> = text.lines().map(str::to_owned).collect();
        let header: Vec<&str> = lines[0].trim_start_matches('\u{feff}').split(',').collect();
        let at = header.iter().position(|h| *h == column).unwrap();
        let mut reader = csv::ReaderBuilder::new()
            .has_headers(false)
            .from_reader(lines[1].as_bytes());
        let record = reader.records().next().unwrap().unwrap();
        let mut fields: Vec<String> = record.iter().map(str::to_owned).collect();
        fields[at] = value.to_owned();
        let mut writer = csv::Writer::from_writer(Vec::new());
        writer.write_record(&fields).unwrap();
        lines[1] = String::from_utf8(writer.into_inner().unwrap())
            .unwrap()
            .trim_end()
            .to_owned();
        parse_export(lines.join("\n").as_bytes()).unwrap()
    }

    #[test]
    fn a_wrong_sharpe_cell_is_reported_with_its_derived_tolerance() {
        let frame = tampered("sharpe_return_one_year", "99.99");
        let results = reconcile(&frame).unwrap();
        let sharpe = &results[0];
        assert_eq!(sharpe.disagreements.len(), 1);
        assert_eq!(sharpe.disagreements[0].column, "sharpe_return_one_year");
        let text = render(&results, 25);
        assert!(text.contains("DISAGREEMENTS"));
        assert!(text.contains("--- sharpe_identity (docs/05 §3) ---"));
    }

    #[test]
    fn a_universe_flag_outside_its_parent_is_reported() {
        let frame = tampered("is_nifty_100", "0");
        let first = frame.rows().next().unwrap();
        if first.get("is_nifty_50").is_one() || first.get("is_nifty_next_50").is_one() {
            let results = reconcile(&frame).unwrap();
            assert!(!results[8].disagreements.is_empty());
        }
    }

    #[test]
    fn the_limit_caps_the_printed_disagreements() {
        let frame = tampered("rsi_one_year", "150");
        let results = reconcile(&frame).unwrap();
        assert_eq!(results[7].disagreements.len(), 1);
        assert!(!render(&results, 0).contains("rsi_one_year outside"));
        assert!(render(&results, 0).contains("... and 1 more"));
    }

    #[test]
    fn schema_drift_is_refused_with_both_lists() {
        let err = parse_export(b"\xef\xbb\xbfname,symbol,bogus\nA,B,1\n").unwrap_err();
        let text = err.to_string();
        assert!(text.contains("reference export schema drift"));
        assert!(text.contains("unexpected=['bogus']"));
    }

    #[test]
    fn cells_are_typed_as_polars_infers_them() {
        let frame = committed_export();
        let row = frame.rows().next().unwrap();
        assert!(matches!(row.get("symbol"), Cell::Str(_)));
        assert!(matches!(row.get("circuits_one_year"), Cell::Int(_)));
        assert!(matches!(row.get("close"), Cell::Float(_)));
        assert_eq!(frame.height(), 271);
    }

    #[test]
    fn the_skip_month_arithmetic_is_pythons() {
        let lines = checks::skip_month_evidence().unwrap();
        assert!(lines[3].contains("(1 + 753.00%) / (1 + 37.29%) - 1"));
        assert!(lines[4].contains("is under-determined"));
    }
}
