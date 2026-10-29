//! `baskfy_api.settings` in Rust: the same fields, defaults, coercions and constraints.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::PathBuf;

use baskfy_api::Settings;

fn env(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
    pairs
        .iter()
        .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
        .collect()
}

#[test]
fn the_field_list_is_pythons() {
    let fixture: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/python_settings_fields.json")).unwrap();
    let python: Vec<&str> = fixture["fields"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    assert_eq!(
        Settings::FIELDS.to_vec(),
        python,
        "a Python setting was added or moved"
    );
}

#[test]
fn the_defaults_are_pythons() {
    let s = Settings::from_sources(Vec::new(), &[]).unwrap();
    assert_eq!(s.environment, "local");
    assert_eq!(s.jwt_max_lifetime_seconds, 900);
    assert_eq!(s.jwt_leeway_seconds, 30);
    assert_eq!(s.rate_limit_anonymous_per_minute, 10);
    assert_eq!(s.rate_limit_authenticated_per_minute, 60);
    assert_eq!(s.cors_origins, vec!["http://localhost:3000".to_owned()]);
    assert_eq!(
        s.twt_max_position_pct_max.to_string(),
        "15.00",
        "a Decimal keeps its scale"
    );
    assert_eq!(s.options_hard_exit_latest.to_string(), "15:00:00");
    assert!(s.rate_limit_enabled && s.cookie_secure && !s.public_api_enabled);
    assert!(!s.jwt_secret.is_set());
    assert!(s.is_staff_email(" MDAVE.5191@gmail.com "));
}

#[test]
fn the_environment_is_read_case_insensitively_and_coerced() {
    let s = Settings::from_sources(
        env(&[
            ("baskfy_environment", "test"),
            ("BASKFY_RATE_LIMIT_ENABLED", "off"),
            ("BASKFY_DB_POOL_SIZE", " 1_2 "),
            ("BASKFY_JWT_LEEWAY_SECONDS", "5.0"),
            (
                "BASKFY_CORS_ORIGINS",
                r#"["https://a.example", "https://b.example"]"#,
            ),
            ("BASKFY_SWING_RISK_PER_TRADE_PCT_MAX", "0.50"),
            ("BASKFY_COOKIE_DOMAIN", ""),
            ("BASKFY_NOT_A_SETTING", "ignored"),
            ("OTHER", "ignored"),
        ]),
        &[],
    )
    .unwrap();
    assert_eq!(s.environment, "test");
    assert!(!s.rate_limit_enabled);
    assert_eq!(s.db_pool_size, 12);
    assert_eq!(s.jwt_leeway_seconds, 5);
    assert_eq!(
        s.cors_origins,
        vec!["https://a.example", "https://b.example"]
    );
    assert_eq!(s.swing_risk_per_trade_pct_max.to_string(), "0.50");
    assert_eq!(s.cookie_domain.as_deref(), Some(""));
}

#[test]
fn constraints_and_literals_are_enforced_together() {
    let err = Settings::from_sources(
        env(&[
            ("BASKFY_DB_POOL_SIZE", "0"),
            ("BASKFY_BACKTEST_CONCURRENCY", "9"),
            ("BASKFY_SWING_RISK_PER_TRADE_PCT_MAX", "6"),
        ]),
        &[],
    )
    .unwrap_err();
    let text = err.to_string();
    assert!(
        text.starts_with("3 validation error(s) for Settings"),
        "{text}"
    );
    assert!(
        text.contains("db_pool_size: Input should be greater than 0"),
        "{text}"
    );
    assert!(
        text.contains("backtest_concurrency: Input should be less than or equal to 8"),
        "{text}"
    );
    assert!(
        text.contains("swing_risk_per_trade_pct_max: Input should be less than or equal to 5"),
        "{text}"
    );

    let err = Settings::from_sources(env(&[("BASKFY_ENVIRONMENT", "prod")]), &[]).unwrap_err();
    assert!(
        err.to_string()
            .contains("environment: Input should be 'local', 'test', 'staging' or 'production'"),
        "{err}"
    );
    assert!(Settings::from_sources(env(&[("BASKFY_DB_POOL_SIZE", "1.5")]), &[]).is_err());
    assert!(Settings::from_sources(env(&[("BASKFY_RATE_LIMIT_ENABLED", "maybe")]), &[]).is_err());
}

#[test]
fn half_configured_smtp_is_refused_in_every_environment() {
    let err = Settings::from_sources(
        env(&[
            ("BASKFY_EMAIL_TRANSPORT", "smtp"),
            ("BASKFY_SMTP_USERNAME", "u"),
        ]),
        &[],
    )
    .unwrap_err();
    assert!(err.to_string().contains("must be set together"), "{err}");
}

#[test]
fn the_process_environment_beats_the_dotenv_files() {
    let dir = std::env::temp_dir().join(format!("baskfy-api-settings-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let file = dir.join(".env");
    std::fs::write(
        &file,
        "BASKFY_ENVIRONMENT=staging\nBASKFY_LOG_LEVEL=DEBUG\n",
    )
    .unwrap();
    let files: Vec<PathBuf> = vec![file];
    let s = Settings::from_sources(env(&[("BASKFY_ENVIRONMENT", "test")]), &files).unwrap();
    assert_eq!(s.environment, "test");
    assert_eq!(s.log_level, "DEBUG");
    std::fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn production_refuses_development_defaults() {
    let mut s = Settings::from_sources(env(&[("BASKFY_ENVIRONMENT", "production")]), &[]).unwrap();
    assert_eq!(
        s.require_configured().unwrap_err(),
        "BASKFY_JWT_SECRET is empty in production; the API could not verify any token"
    );
    s.jwt_secret = baskfy_providers::secret::Secret::new("short");
    assert!(
        s.require_configured()
            .unwrap_err()
            .contains("shorter than the 32 bytes")
    );
    s.environment = "test".to_owned();
    assert!(s.require_configured().is_ok());
}

#[test]
fn secrets_never_print() {
    let s = Settings::from_sources(
        env(&[
            ("BASKFY_JWT_SECRET", "hunter2-hunter2-hunter2-hunter2!"),
            ("BASKFY_DATABASE_URL", "postgresql://u:pw-in-url@h/d"),
        ]),
        &[],
    )
    .unwrap();
    let debug = format!("{s:?}");
    assert!(
        !debug.contains("hunter2") && !debug.contains("pw-in-url"),
        "{debug}"
    );
}
