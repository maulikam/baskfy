//! The NSE tests' doubles: `FakeHttpClient`, `InertLimiter` and `build` from `test_nse.py`, and
//! the recorded fixtures read in place from `decile-blueprint/packages/providers/tests/fixtures`.
#![allow(dead_code, clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use baskfy_providers::archive::LocalRawArchive;
use baskfy_providers::errors::ProviderError;
use baskfy_providers::nse::{HttpClientLike, HttpFailure, HttpResponse, NseProvider, NseRuntime};
use baskfy_providers::ratelimit::RateLimiter;
use baskfy_providers::retry::RetryPolicy;
use baskfy_providers::settings::ProviderSettings;

/// `decile-blueprint/packages/providers/tests/fixtures/nse/<name>`, read in place.
pub fn recorded(name: &str) -> Vec<u8> {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../decile-blueprint/packages/providers/tests/fixtures/nse")
        .join(name);
    std::fs::read(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

/// The `settings` fixture: Python's defaults, no dotenv, no environment.
pub fn settings() -> ProviderSettings {
    ProviderSettings::default()
}

/// The `archive` fixture: a local archive in a fresh temporary directory.
pub fn archive() -> (tempfile::TempDir, Arc<LocalRawArchive>) {
    let dir = tempfile::tempdir().unwrap();
    let archive = Arc::new(LocalRawArchive::new(dir.path().join("archive")));
    (dir, archive)
}

/// Serves canned bodies by URL substring (first matching route, in insertion order) and records
/// the order of every request.
pub struct FakeHttpClient {
    routes: Vec<(String, Vec<u8>)>,
    status: u16,
    raises: Option<String>,
    fallback: Vec<u8>,
    delay: Option<Duration>,
    pub requests: Mutex<Vec<String>>,
    live: AtomicUsize,
    pub peak: AtomicUsize,
}

impl FakeHttpClient {
    pub fn new(routes: &[(&str, Vec<u8>)]) -> Self {
        Self {
            routes: routes
                .iter()
                .map(|(k, v)| ((*k).to_owned(), v.clone()))
                .collect(),
            status: 200,
            raises: None,
            fallback: Vec::new(),
            delay: None,
            requests: Mutex::new(Vec::new()),
            live: AtomicUsize::new(0),
            peak: AtomicUsize::new(0),
        }
    }

    pub fn status(mut self, status: u16) -> Self {
        self.status = status;
        self
    }

    pub fn raises(mut self, message: &str) -> Self {
        self.raises = Some(message.to_owned());
        self
    }

    /// The body for an unrouted URL (`b""` in test_nse.py, `b"[]"` in the catalyst tests).
    pub fn fallback(mut self, body: &[u8]) -> Self {
        self.fallback = body.to_vec();
        self
    }

    /// Each GET sleeps this long (on tokio's clock), so overlapping requests can be counted.
    pub fn delay(mut self, delay: Duration) -> Self {
        self.delay = Some(delay);
        self
    }

    pub fn requests(&self) -> Vec<String> {
        self.requests.lock().unwrap().clone()
    }
}

#[async_trait]
impl HttpClientLike for FakeHttpClient {
    async fn get(&self, url: &str) -> Result<HttpResponse, HttpFailure> {
        self.requests.lock().unwrap().push(url.to_owned());
        let now = self.live.fetch_add(1, Ordering::SeqCst) + 1;
        self.peak.fetch_max(now, Ordering::SeqCst);
        if let Some(d) = self.delay {
            tokio::time::sleep(d).await;
        }
        self.live.fetch_sub(1, Ordering::SeqCst);
        if let Some(message) = &self.raises {
            return Err(Box::new(std::io::Error::new(
                std::io::ErrorKind::ConnectionReset,
                message.clone(),
            )));
        }
        let body = self
            .routes
            .iter()
            .find(|(fragment, _)| url.contains(fragment.as_str()))
            .map_or_else(|| self.fallback.clone(), |(_, b)| b.clone());
        Ok(HttpResponse {
            status_code: self.status,
            content: body,
        })
    }
}

/// `InertLimiter`: never waits, counts acquisitions.
#[derive(Default)]
pub struct InertLimiter {
    pub acquisitions: AtomicUsize,
}

impl InertLimiter {
    pub fn count(&self) -> usize {
        self.acquisitions.load(Ordering::SeqCst)
    }
}

#[async_trait]
impl RateLimiter for InertLimiter {
    async fn acquire(&self, _tokens: f64) -> Result<f64, ProviderError> {
        self.acquisitions.fetch_add(1, Ordering::SeqCst);
        Ok(0.0)
    }
}

/// `build(settings, archive, client, limiter)` — one attempt, so failures surface at once.
pub fn build(
    settings: ProviderSettings,
    archive: Arc<LocalRawArchive>,
    client: Arc<FakeHttpClient>,
    limiter: Option<Arc<InertLimiter>>,
) -> NseProvider {
    NseProvider::new(
        settings,
        Some(archive),
        NseRuntime {
            client: Some(client),
            rate_limiter: Some(limiter.unwrap_or_default()),
            retry_policy: Some(RetryPolicy::new(1, 0.01, 30.0).unwrap()),
            retry_hooks: None,
            clock: None,
        },
    )
    .unwrap()
}

/// A single-entry ZIP_STORED archive, as `zipfile.ZipFile(..., "w").writestr` writes.
pub fn zip_one(name: &str, payload: &[u8]) -> Vec<u8> {
    use std::io::Write;
    let mut buffer = std::io::Cursor::new(Vec::new());
    {
        let mut writer = zip::ZipWriter::new(&mut buffer);
        let options = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Stored);
        writer.start_file(name, options).unwrap();
        writer.write_all(payload).unwrap();
        writer.finish().unwrap();
    }
    buffer.into_inner()
}
