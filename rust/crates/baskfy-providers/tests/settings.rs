//! Provider settings — the `BASKFY_` environment contract of `baskfy_providers.settings`
//! (Python has no dedicated test file; these assert the module's stated behaviour and the
//! values the conftest fixtures rely on).
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

mod common;

use std::path::PathBuf;

use baskfy_providers::settings::{ProviderSettings, parse_dotenv};
use common::arm;

fn env(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
    pairs
        .iter()
        .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
        .collect()
}

#[test]
fn an_empty_environment_gives_python_s_defaults() {
    arm();
    let s = ProviderSettings::from_sources(Vec::new(), &[]).unwrap();
    assert_eq!(s, ProviderSettings::default());
    assert_eq!(s.kite_rate_limit_per_second, 3.0);
    assert_eq!(s.kite_bulk_rate_limit_per_second, 2.0);
    assert_eq!(s.nse_rate_limit_per_second, 1.0);
    assert_eq!(s.kite_max_days_per_request, 2000);
    assert_eq!(s.nse_fetch_concurrency, 4);
    assert_eq!(s.redis_url.expose(), "redis://localhost:6380/0");
    assert!(!s.kite_configured());
    assert!(!s.s3_configured());
    assert!(!s.token_encryption_configured());
    assert!(!s.desk_session_pull_configured());
}

#[test]
fn the_prefix_is_case_insensitive_like_pydantic_settings() {
    arm();
    let s = ProviderSettings::from_sources(
        env(&[
            ("baskfy_kite_rate_limit_per_second", "4"),
            ("BASKFY_KITE_API_KEY", "k"),
            ("Baskfy_Kite_Api_Secret", "s"),
            ("KITE_API_KEY", "not-prefixed-ignored"),
            ("BASKFY_SOMETHING_ELSE", "ignored"),
        ]),
        &[],
    )
    .unwrap();
    assert_eq!(s.kite_rate_limit_per_second, 4.0);
    assert_eq!(s.kite_api_key.expose(), "k");
    assert!(s.kite_configured());
}

#[test]
fn the_environment_beats_dotenv_and_a_later_dotenv_beats_an_earlier_one() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let first = dir.path().join("a.env");
    let second = dir.path().join("b.env");
    std::fs::write(
        &first,
        "# comment\nBASKFY_S3_BUCKET=first\nexport BASKFY_S3_REGION='ap-south-1'\nBASKFY_NSE_FETCH_CONCURRENCY=8 # inline\n",
    )
    .unwrap();
    std::fs::write(&second, "BASKFY_S3_BUCKET=\"second\"\n").unwrap();
    let files: Vec<PathBuf> = vec![first, second, dir.path().join("missing.env")];
    let s = ProviderSettings::from_sources(Vec::new(), &files).unwrap();
    assert_eq!(s.s3_bucket, "second");
    assert_eq!(s.s3_region, "ap-south-1");
    assert_eq!(s.nse_fetch_concurrency, 8);
    let s = ProviderSettings::from_sources(env(&[("BASKFY_S3_BUCKET", "env")]), &files).unwrap();
    assert_eq!(s.s3_bucket, "env");
}

#[test]
fn a_bulk_lane_at_or_above_the_ceiling_is_refused() {
    arm();
    let err = ProviderSettings::from_sources(
        env(&[("BASKFY_KITE_BULK_RATE_LIMIT_PER_SECOND", "3.0")]),
        &[],
    )
    .unwrap_err();
    assert_eq!(
        err.errors,
        vec![
            "Value error, BASKFY_KITE_BULK_RATE_LIMIT_PER_SECOND (3) must be strictly below \
             BASKFY_KITE_RATE_LIMIT_PER_SECOND (3); the difference is the headroom a \
             login-time read depends on"
                .to_owned()
        ]
    );
}

#[test]
fn constraints_are_checked_and_reported_together() {
    arm();
    let err = ProviderSettings::from_sources(
        env(&[
            ("BASKFY_KITE_RATE_LIMIT_PER_SECOND", "0"),
            ("BASKFY_NSE_FETCH_CONCURRENCY", "17"),
            ("BASKFY_PROVIDER_MAX_ATTEMPTS", "0"),
        ]),
        &[],
    )
    .unwrap_err();
    assert_eq!(err.errors.len(), 3, "{err}");
    let err = ProviderSettings::from_sources(env(&[("BASKFY_KITE_MAX_CONCURRENCY", "three")]), &[])
        .unwrap_err();
    assert!(err.errors[0].starts_with("kite_max_concurrency: "));
}

#[test]
fn lax_numbers_parse_as_pydantic_does() {
    arm();
    let s = ProviderSettings::from_sources(
        env(&[
            ("BASKFY_KITE_MAX_DAYS_PER_REQUEST", " 2_000 "),
            ("BASKFY_KITE_MAX_CONCURRENCY", "3.0"),
            ("BASKFY_NSE_RATE_LIMIT_PER_SECOND", "1e0"),
        ]),
        &[],
    )
    .unwrap();
    assert_eq!(s.kite_max_days_per_request, 2000);
    assert_eq!(s.kite_max_concurrency, 3);
    assert_eq!(s.nse_rate_limit_per_second, 1.0);
    assert!(
        ProviderSettings::from_sources(env(&[("BASKFY_KITE_MAX_CONCURRENCY", "3.5")]), &[])
            .is_err()
    );
}

#[test]
fn credentials_never_appear_in_debug_output() {
    arm();
    let s = ProviderSettings::from_sources(
        env(&[
            ("BASKFY_KITE_API_KEY", "key-AAAA"),
            ("BASKFY_KITE_API_SECRET", "secret-BBBB"),
            ("BASKFY_KITE_TOKEN_ENCRYPTION_KEY", "fernet-CCCC"),
            ("BASKFY_S3_ACCESS_KEY_ID", "id-DDDD"),
            ("BASKFY_S3_SECRET_ACCESS_KEY", "s3-EEEE"),
            ("BASKFY_REDIS_URL", "redis://:pw-FFFF@localhost:6380/0"),
        ]),
        &[],
    )
    .unwrap();
    let printed = format!("{s:?}");
    for needle in ["AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF"] {
        assert!(!printed.contains(needle), "{needle} leaked");
    }
    assert!(s.s3_configured() || s.s3_bucket.is_empty());
}

#[test]
fn the_dotenv_parser_reads_the_usual_forms() {
    arm();
    let parsed = parse_dotenv(
        "A=1\n# c\n\nexport B = two \nC=\"x\\ny\"\nD='lit # not comment'\nE=v # comment\n",
    );
    assert_eq!(
        parsed,
        vec![
            ("A".to_owned(), "1".to_owned()),
            ("B".to_owned(), "two".to_owned()),
            ("C".to_owned(), "x\ny".to_owned()),
            ("D".to_owned(), "lit # not comment".to_owned()),
            ("E".to_owned(), "v".to_owned()),
        ]
    );
}
