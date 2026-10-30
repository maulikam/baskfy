//! `baskfy_worker.settings.WorkerSettings` in Rust: the same fields, environment names and
//! defaults, held to `fixtures/python_worker_settings.json` (generated from
//! `WorkerSettings.model_fields` in an empty directory, so no `.env` leaked into it).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_worker::settings::WorkerSettings;

fn fixture() -> Vec<serde_json::Value> {
    let v: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/python_worker_settings.json")).unwrap();
    v["fields"].as_array().unwrap().clone()
}

#[test]
fn the_field_list_and_environment_names_are_pythons() {
    let fields = fixture();
    let names: Vec<&str> = fields.iter().map(|f| f["name"].as_str().unwrap()).collect();
    let envs: Vec<&str> = fields.iter().map(|f| f["env"].as_str().unwrap()).collect();
    assert_eq!(
        WorkerSettings::FIELDS.to_vec(),
        names,
        "a Python setting was added or moved"
    );
    assert_eq!(WorkerSettings::ENV_NAMES.to_vec(), envs);
}

#[test]
fn the_defaults_are_pythons() {
    let s = WorkerSettings::from_sources(Vec::new(), &[]).unwrap();
    for f in fixture() {
        let name = f["name"].as_str().unwrap();
        let (kind, value) = f["default"].as_object().unwrap().iter().next().unwrap();
        let expected = match kind.as_str() {
            "bool" => if value.as_bool().unwrap() {
                "True"
            } else {
                "False"
            }
            .to_owned(),
            "int" => value.as_i64().unwrap().to_string(),
            _ => value.as_str().unwrap().to_owned(),
        };
        assert_eq!(s.show(name).unwrap(), expected, "{name}");
    }
}

#[test]
fn the_environment_is_read_case_insensitively_with_the_desks_aliases() {
    let env = [
        ("baskfy_twt_nightly_enabled", "off"),
        ("BASKFY_SWING_INDEX_SLUG", "nifty-500"),
        ("BASKFY_INGEST_CONCURRENCY", " 1_2 "),
        ("BASKFY_GATE_MIN_BAR_RATIO", "0.75"),
        ("DRY_RUN", "false"),
        ("BASKFY_OPTIONS_RISK_PCT_MAX", "0.50"),
        ("BASKFY_OPTIONS_HARD_EXIT_LATEST", "14:30"),
    ];
    let s = WorkerSettings::from_sources(
        env.iter().map(|(k, v)| ((*k).to_owned(), (*v).to_owned())),
        &[],
    )
    .unwrap();
    assert!(!s.twt_nightly_enabled);
    assert_eq!(s.swing_index_slug, "nifty-500");
    assert_eq!(s.ingest_concurrency, 12);
    assert_eq!(s.show("gate_min_bar_ratio").unwrap(), "0.75");
    assert_eq!(s.options_desk_dry_run, "false");
    assert_eq!(s.options_risk_pct_max.to_string(), "0.50");
    assert_eq!(s.show("options_hard_exit_latest").unwrap(), "14:30:00");
}

#[test]
fn constraints_refuse_with_pydantics_words() {
    for (k, v, needle) in [
        (
            "BASKFY_GATE_MIN_BAR_RATIO",
            "1.5",
            "gate_min_bar_ratio: Input should be less than or equal to 1",
        ),
        (
            "BASKFY_INGEST_CONCURRENCY",
            "0",
            "ingest_concurrency: Input should be greater than 0",
        ),
        (
            "BASKFY_TASK_MAX_RETRIES",
            "-1",
            "task_max_retries: Input should be greater than or equal to 0",
        ),
        (
            "BASKFY_VBT_NIGHTLY_ENABLED",
            "maybe",
            "Input should be a valid boolean",
        ),
    ] {
        let err = WorkerSettings::from_sources([(k.to_owned(), v.to_owned())], &[]).unwrap_err();
        assert!(err.to_string().contains(needle), "{k}={v}: {err}");
    }
}
