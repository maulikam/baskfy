//! PM1.9: Tapetide's REST API for the pilot. Fetches and archives raw pages; nothing here parses.

use std::sync::Arc;
use std::time::Duration;

use chrono::NaiveDate;
use reqwest::{StatusCode, Url};

use crate::archive::RawArchive;
use crate::errors::ProviderError;
use crate::ratelimit::RateLimiter;

/// The token bucket's Redis key.
pub const RATE_LIMIT_KEY: &str = "baskfy:ratelimit:tapetide";
/// Below Tapetide's burst cap of 60 requests a minute.
pub const RATE_PER_SECOND: f64 = 0.8;
const ORIGIN: &str = "https://api.tapetide.com";
const ATTEMPTS: u32 = 4;
const TIMEOUT: Duration = Duration::from_secs(40);

fn upstream(message: String) -> ProviderError {
    ProviderError::upstream_unavailable(format!("[tapetide] {message}"))
}

/// Raw pages of one symbol's endpoint, through the shared limiter and the raw archive.
pub struct TapetideProvider {
    client: reqwest::Client,
    token: String,
    limiter: Arc<dyn RateLimiter>,
    archive: Arc<dyn RawArchive>,
}

impl TapetideProvider {
    /// # Errors
    /// When the HTTP client cannot be built.
    pub fn new(
        token: String,
        limiter: Arc<dyn RateLimiter>,
        archive: Arc<dyn RawArchive>,
    ) -> Result<Self, ProviderError> {
        let client = reqwest::Client::builder()
            .timeout(TIMEOUT)
            .build()
            .map_err(|e| upstream(format!("client: {e}")))?;
        Ok(Self {
            client,
            token,
            limiter,
            archive,
        })
    }

    /// Every page of `endpoint` for `symbol`, archived per `on`; pages already stored cost no
    /// call. The number of pages, or `None` when Tapetide does not know the symbol.
    ///
    /// # Errors
    /// Transport, quota and payload failures after retries.
    pub async fn pull(
        &self,
        symbol: &str,
        endpoint: &str,
        on: NaiveDate,
    ) -> Result<Option<usize>, ProviderError> {
        let mut url = first_page(symbol, endpoint)?;
        let mut pages = 0;
        loop {
            let key = format!(
                "tapetide/{endpoint}/{symbol}/{}/p{pages}.json",
                on.format("%Y-%m-%d")
            );
            if !self.archive.exists(&key).await? {
                let Some(body) = self.fetch(&url).await? else {
                    return Ok(None);
                };
                self.archive.put(&key, &body, "application/json").await?;
            }
            let body = self.archive.get(&key).await?;
            pages += 1;
            let parsed: serde_json::Value = serde_json::from_slice(&body)
                .map_err(|e| ProviderError::unexpected_payload(format!("[tapetide] {key}: {e}")))?;
            match parsed.pointer("/links/next").and_then(|v| v.as_str()) {
                Some(next) if !next.is_empty() => url = format!("{ORIGIN}{next}"),
                _ => return Ok(Some(pages)),
            }
        }
    }

    async fn fetch(&self, url: &str) -> Result<Option<Vec<u8>>, ProviderError> {
        let mut last = String::new();
        for attempt in 1..=ATTEMPTS {
            self.limiter.acquire(1.0).await?;
            let sent = self.client.get(url).bearer_auth(&self.token).send().await;
            let response = match sent {
                Ok(r) => r,
                Err(e) => {
                    last = e.to_string();
                    tokio::time::sleep(Duration::from_secs(u64::from(attempt) * 5)).await;
                    continue;
                }
            };
            let status = response.status();
            if status == StatusCode::NOT_FOUND {
                return Ok(None);
            }
            if status == StatusCode::TOO_MANY_REQUESTS || status.is_server_error() {
                let wait = response
                    .headers()
                    .get(reqwest::header::RETRY_AFTER)
                    .and_then(|v| v.to_str().ok())
                    .and_then(|v| v.parse::<u64>().ok())
                    .unwrap_or(u64::from(attempt) * 10);
                last = format!("{url} returned {status}");
                tokio::time::sleep(Duration::from_secs(wait)).await;
                continue;
            }
            if !status.is_success() {
                return Err(upstream(format!("{url} returned {status}")));
            }
            let body = response
                .bytes()
                .await
                .map_err(|e| upstream(e.to_string()))?;
            return Ok(Some(body.to_vec()));
        }
        Err(upstream(format!("gave up after {ATTEMPTS}: {last}")))
    }
}

fn first_page(symbol: &str, endpoint: &str) -> Result<String, ProviderError> {
    let mut url = Url::parse(&format!("{ORIGIN}/v1"))
        .map_err(|e| ProviderError::value_error(e.to_string()))?;
    url.path_segments_mut()
        .map_err(|()| ProviderError::value_error("base URL cannot take a path"))?
        .extend(["stocks", symbol, endpoint]);
    Ok(url.into())
}

#[cfg(test)]
mod tests {
    use super::first_page;
    use crate::errors::ProviderError;

    #[test]
    fn a_symbol_with_an_ampersand_stays_one_path_segment() -> Result<(), ProviderError> {
        assert_eq!(
            first_page("J&KBANK", "fundamentals")?,
            "https://api.tapetide.com/v1/stocks/J&KBANK/fundamentals"
        );
        Ok(())
    }

    #[test]
    fn a_slash_in_a_symbol_cannot_change_the_endpoint() -> Result<(), ProviderError> {
        assert!(first_page("A/B", "pledge")?.contains("/stocks/A%2FB/pledge"));
        Ok(())
    }
}
