//! PM1.9: `pull` archives Tapetide's raw fundamentals, shareholding and pledge pages for grading.

use std::str::FromStr as _;
use std::sync::Arc;

use baskfy_providers::factory::{RedisSource, build_archive, build_rate_limiter};
use baskfy_providers::ratelimit::RateLimiter;
use baskfy_providers::settings::ProviderSettings;
use baskfy_providers::tapetide::{RATE_LIMIT_KEY, RATE_PER_SECOND, TapetideProvider};
use chrono::NaiveDate;

use super::cli::{Failure, Outcome, Parsed, Spec, code, command, say};
use crate::error::WorkerError;

const ENDPOINTS: [&str; 3] = ["fundamentals", "shareholding", "pledge"];

fn settings_error(text: impl ToString) -> Failure {
    Failure::Raised(WorkerError::Settings(text.to_string()))
}

async fn provider() -> Result<TapetideProvider, Failure> {
    let token = std::env::var("BASKFY_TAPETIDE_API_KEY")
        .map_err(|_| settings_error("BASKFY_TAPETIDE_API_KEY is not set"))?;
    let settings = ProviderSettings::from_env().map_err(settings_error)?;
    let source = RedisSource::from_settings(&settings);
    let limiter = build_rate_limiter(&source, RATE_LIMIT_KEY, RATE_PER_SECOND)
        .await
        .map_err(settings_error)?
        .ok_or_else(|| settings_error("Tapetide needs Redis for its limiter"))?;
    let archive = build_archive(&settings, None).map_err(settings_error)?;
    TapetideProvider::new(token, limiter as Arc<dyn RateLimiter>, archive).map_err(settings_error)
}

pub(super) async fn run(args: &[String]) -> Outcome {
    let (_, rest) = command(args, &["pull"])?;
    let parsed = Parsed::parse(
        rest,
        Spec {
            valued: &["--symbols", "--on"],
            ..Spec::default()
        },
    )?;
    let symbols = parsed
        .get("--symbols")
        .ok_or_else(|| Failure::Usage("pull needs --symbols".to_owned()))?;
    let on = match parsed.get("--on") {
        Some(text) => NaiveDate::from_str(text).map_err(settings_error)?,
        None => chrono::Utc::now().date_naive(),
    };
    let provider = provider().await?;
    let mut failed = 0;
    for symbol in symbols.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        for endpoint in ENDPOINTS {
            match provider.pull(symbol, endpoint, on).await {
                Ok(Some(pages)) => say(&format!("PULL {symbol} {endpoint} pages={pages}")),
                Ok(None) => say(&format!("NOTFOUND {symbol} {endpoint}")),
                Err(e) => {
                    failed += 1;
                    say(&format!("FAILED {symbol} {endpoint}: {e}"));
                }
            }
        }
    }
    say(&format!("TAPETIDE done failed={failed}"));
    Ok(code(failed == 0))
}
