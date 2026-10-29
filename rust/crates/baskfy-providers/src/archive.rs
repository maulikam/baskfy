//! The raw-file archive (Prompt 2 deliverable 3). Ports `baskfy_providers.archive`.
//!
//! docs/09 §"NSE specifics" is unambiguous about the ordering:
//!
//! > "Fetch once, archive the raw file to R2 keyed by `nse/{kind}/{date}.csv`, and parse from the
//! > archive. Never re-fetch to re-parse: the archive is the reproducibility record."
//!
//! So the contract here is *archive-then-parse*, and a failed archive must abort the parse. It is
//! tempting to treat archiving as best-effort telemetry and carry on with the bytes already in
//! memory — that is exactly the shortcut that produces a published number nobody can re-derive
//! six months later when an exchange file turns out to have been wrong.
//!
//! Two implementations, one interface:
//!
//! * [`S3RawArchive`] — Cloudflare R2 or any S3-compatible store (docs/02), over an
//!   [`S3ClientLike`]; [`SigV4S3Client`] is the production client (Python's is boto3).
//! * [`LocalRawArchive`] — a directory on disk, for local development and tests, so the
//!   archive-then-parse path is exercised everywhere rather than bypassed when S3 is absent.
//!
//! The archive is **async** (DECISIONS-RS R4.1): an S3 put is a network call.

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::future::Future;
use std::path::{Component, Path, PathBuf};

use async_trait::async_trait;
use chrono::{DateTime, NaiveDate, Utc};
use hmac::{Hmac, Mac};
use percent_encoding::{AsciiSet, NON_ALPHANUMERIC, utf8_percent_encode};
use sha2::{Digest, Sha256};

use crate::clock::{Clock, SystemClock};
use crate::errors::ProviderError;
use crate::net::GuardedClient;
use crate::py::repr_str;
use crate::secret::Secret;

/// `urllib.parse.quote`'s default safe set: everything but letters, digits, `_.-~` and `/` is
/// escaped (Python's `quote(s)` with `safe="/"`).
pub(crate) const PY_QUOTE: &AsciiSet = &NON_ALPHANUMERIC
    .remove(b'_')
    .remove(b'.')
    .remove(b'-')
    .remove(b'~')
    .remove(b'/');

/// `archive_key`: `nse/{kind}/{date}.{ext}` — the layout docs/09 specifies.
///
/// # Errors
/// `ValueError` for an empty kind.
pub fn archive_key(kind: &str, on: NaiveDate, extension: &str) -> Result<String, ProviderError> {
    if kind.is_empty() {
        return Err(ProviderError::value_error("archive kind cannot be empty"));
    }
    let cleaned = extension.trim_start_matches('.');
    Ok(format!("nse/{kind}/{}.{cleaned}", on.format("%Y-%m-%d")))
}

/// Where raw upstream files are preserved before anything parses them (`RawArchive`).
#[async_trait]
pub trait RawArchive: Send + Sync {
    /// Store `payload` at `key`. Returns a URI for logs.
    ///
    /// # Errors
    /// `ArchiveError` on failure.
    async fn put(
        &self,
        key: &str,
        payload: &[u8],
        content_type: &str,
    ) -> Result<String, ProviderError>;

    /// Read back what was stored. This is what parsers read from.
    ///
    /// # Errors
    /// `ArchiveError` when the key cannot be read.
    async fn get(&self, key: &str) -> Result<Vec<u8>, ProviderError>;

    /// Is something stored at `key`?
    ///
    /// # Errors
    /// `ArchiveError` when a key escapes a local archive's root (Python raises from `exists`
    /// too); an S3 failure is `false`, as Python's `head_object` handler returns.
    async fn exists(&self, key: &str) -> Result<bool, ProviderError>;

    /// Human-readable location, for `providers doctor`.
    fn describe(&self) -> String;
}

/// A directory on disk. Same contract as S3, no credentials (`LocalRawArchive`).
#[derive(Debug, Clone)]
pub struct LocalRawArchive {
    root: PathBuf,
}

impl LocalRawArchive {
    /// An archive rooted at `root` (created lazily, on the first `put`).
    #[must_use]
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    /// The configured root, as given.
    #[must_use]
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// `_path`: the resolved file for `key`, refusing one that escapes the root. A key is built
    /// from provider-supplied names; keep it from escaping the archive root.
    fn path(&self, key: &str) -> Result<PathBuf, ProviderError> {
        let candidate = resolve(&self.root.join(key));
        let root = resolve(&self.root);
        if !candidate.starts_with(&root) {
            return Err(ProviderError::archive_error(format!(
                "archive key {} escapes the archive root",
                repr_str(key)
            )));
        }
        Ok(candidate)
    }
}

/// `Path.resolve()` (non-strict): make absolute, resolve symlinks along the longest existing
/// prefix, and fold the rest lexically (`..` pops, `.` vanishes) — `os.path.realpath`.
fn resolve(path: &Path) -> PathBuf {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("/"))
            .join(path)
    };
    let mut out = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::Prefix(p) => out.push(p.as_os_str()),
            Component::RootDir => out.push(Component::RootDir.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            Component::Normal(part) => {
                out.push(part);
                if let Ok(real) = std::fs::canonicalize(&out) {
                    out = real;
                }
            }
        }
    }
    out
}

/// `PurePath.as_uri()` for an absolute POSIX path: `file://` plus the percent-quoted bytes.
fn file_uri(path: &Path) -> String {
    let text = path.to_string_lossy();
    format!("file://{}", utf8_percent_encode(&text, PY_QUOTE))
}

#[async_trait]
impl RawArchive for LocalRawArchive {
    async fn put(
        &self,
        key: &str,
        payload: &[u8],
        _content_type: &str,
    ) -> Result<String, ProviderError> {
        // A filesystem has nowhere to record the content type.
        let path = self.path(key)?;
        let written = async {
            if let Some(parent) = path.parent() {
                tokio::fs::create_dir_all(parent).await?;
            }
            tokio::fs::write(&path, payload).await
        }
        .await;
        written.map_err(|e| {
            ProviderError::archive_error(format!(
                "could not archive {} to {}: {e}",
                repr_str(key),
                path.display()
            ))
            .with_source(e)
        })?;
        Ok(file_uri(&path))
    }

    async fn get(&self, key: &str) -> Result<Vec<u8>, ProviderError> {
        let path = self.path(key)?;
        tokio::fs::read(&path).await.map_err(|e| {
            ProviderError::archive_error(format!(
                "could not read {} from {}: {e}",
                repr_str(key),
                path.display()
            ))
            .with_source(e)
        })
    }

    async fn exists(&self, key: &str) -> Result<bool, ProviderError> {
        let path = self.path(key)?;
        Ok(tokio::fs::metadata(&path).await.is_ok_and(|m| m.is_file()))
    }

    fn describe(&self) -> String {
        format!("local directory {}", self.root.display())
    }
}

/// An S3 failure as the client raised it (boto3's `ClientError`, a socket error, ...).
pub type S3Failure = Box<dyn std::error::Error + Send + Sync + 'static>;

/// What `put_object` sends (`S3ClientLike.put_object(**kwargs)`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PutObject<'a> {
    /// `Bucket`.
    pub bucket: &'a str,
    /// `Key`.
    pub key: &'a str,
    /// `Body`.
    pub body: &'a [u8],
    /// `ContentType`.
    pub content_type: &'a str,
    /// `Metadata` (sent as `x-amz-meta-*`).
    pub metadata: BTreeMap<String, String>,
}

/// The slice of boto3's S3 client this module uses (`S3ClientLike`).
#[async_trait]
pub trait S3ClientLike: Send + Sync {
    /// `put_object(Bucket=, Key=, Body=, ContentType=, Metadata=)`.
    ///
    /// # Errors
    /// Whatever the client raised.
    async fn put_object(&self, request: PutObject<'_>) -> Result<(), S3Failure>;

    /// `get_object(Bucket=, Key=)["Body"].read()`.
    ///
    /// # Errors
    /// Whatever the client raised.
    async fn get_object(&self, bucket: &str, key: &str) -> Result<Vec<u8>, S3Failure>;

    /// `head_object(Bucket=, Key=)`.
    ///
    /// # Errors
    /// Whatever the client raised (a missing key included).
    async fn head_object(&self, bucket: &str, key: &str) -> Result<(), S3Failure>;
}

/// Cloudflare R2, or any S3-compatible bucket (docs/02 §"Object storage") (`S3RawArchive`).
pub struct S3RawArchive {
    client: std::sync::Arc<dyn S3ClientLike>,
    bucket: String,
}

impl std::fmt::Debug for S3RawArchive {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("S3RawArchive")
            .field("bucket", &self.bucket)
            .finish_non_exhaustive()
    }
}

impl S3RawArchive {
    /// An archive over `client` and `bucket`.
    ///
    /// # Errors
    /// `CredentialsMissing` for an empty bucket name.
    pub fn new(
        client: std::sync::Arc<dyn S3ClientLike>,
        bucket: &str,
    ) -> Result<Self, ProviderError> {
        if bucket.is_empty() {
            return Err(ProviderError::credentials_missing(
                "no S3 bucket configured for the raw archive",
            ));
        }
        Ok(Self {
            client,
            bucket: bucket.to_owned(),
        })
    }
}

/// Lower-case hex SHA-256 (`hashlib.sha256(payload).hexdigest()`).
fn sha256_hex(payload: &[u8]) -> String {
    hex(&Sha256::digest(payload))
}

fn hex(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        let _ = write!(out, "{b:02x}");
    }
    out
}

#[async_trait]
impl RawArchive for S3RawArchive {
    async fn put(
        &self,
        key: &str,
        payload: &[u8],
        content_type: &str,
    ) -> Result<String, ProviderError> {
        // Cheap tamper/corruption detection on the way back out.
        let metadata = BTreeMap::from([("sha256".to_owned(), sha256_hex(payload))]);
        let request = PutObject {
            bucket: &self.bucket,
            key,
            body: payload,
            content_type,
            metadata,
        };
        self.client.put_object(request).await.map_err(|e| {
            ProviderError::archive_error(format!(
                "could not archive {} to s3://{}: {e}",
                repr_str(key),
                self.bucket
            ))
        })?;
        Ok(format!("s3://{}/{key}", self.bucket))
    }

    async fn get(&self, key: &str) -> Result<Vec<u8>, ProviderError> {
        self.client
            .get_object(&self.bucket, key)
            .await
            .map_err(|e| {
                ProviderError::archive_error(format!(
                    "could not read {} from s3://{}: {e}",
                    repr_str(key),
                    self.bucket
                ))
            })
    }

    async fn exists(&self, key: &str) -> Result<bool, ProviderError> {
        Ok(self.client.head_object(&self.bucket, key).await.is_ok())
    }

    fn describe(&self) -> String {
        format!("s3://{}", self.bucket)
    }
}

/// `fetch_and_archive`: the archive-then-parse discipline, in one place.
///
/// Returns the bytes **read back from the archive**, never the bytes just fetched. That is the
/// difference between "we saved a copy" and "what we parsed is what we saved": if the archive
/// silently truncated or transcoded, the parse fails now instead of producing a number that
/// cannot be reproduced from the record.
///
/// An already-archived key is not re-fetched, which is docs/09's "Never re-fetch to re-parse".
///
/// # Errors
/// The fetcher's own error; `ArchiveError` for an empty file or a failed put/get.
pub async fn fetch_and_archive<F, Fut>(
    archive: &dyn RawArchive,
    key: &str,
    fetch: F,
    content_type: &str,
    refetch: bool,
) -> Result<Vec<u8>, ProviderError>
where
    F: FnOnce() -> Fut + Send,
    Fut: Future<Output = Result<Vec<u8>, ProviderError>> + Send,
{
    if refetch || !archive.exists(key).await? {
        let payload = fetch().await?;
        if payload.is_empty() {
            return Err(ProviderError::archive_error(format!(
                "fetcher for {} returned an empty file",
                repr_str(key)
            )));
        }
        archive.put(key, &payload, content_type).await?;
    }
    archive.get(key).await
}

// ---------------------------------------------------------------------------------------------
// The production S3 client (Python's is boto3)
// ---------------------------------------------------------------------------------------------

/// The parts of a request that SigV4 signs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SigV4Request<'a> {
    /// `GET`, `PUT`, `HEAD`.
    pub method: &'a str,
    /// The path, already URI-encoded per S3's rules (each segment, `/` kept).
    pub canonical_uri: &'a str,
    /// The canonical query string (empty for these calls).
    pub canonical_query: &'a str,
    /// Every signed header, lower-case name to trimmed value (`host` and `x-amz-*` included).
    pub headers: &'a BTreeMap<String, String>,
    /// Hex SHA-256 of the payload (`x-amz-content-sha256`).
    pub payload_sha256: &'a str,
}

/// The `Authorization` header for `request`, signed with AWS Signature Version 4 for the `s3`
/// service at `when` in `region`.
#[must_use]
pub fn sigv4_authorization(
    request: &SigV4Request<'_>,
    access_key_id: &str,
    secret_access_key: &str,
    region: &str,
    when: DateTime<Utc>,
) -> String {
    let date = when.format("%Y%m%d").to_string();
    let stamp = when.format("%Y%m%dT%H%M%SZ").to_string();
    let signed_headers: Vec<&str> = request.headers.keys().map(String::as_str).collect();
    let signed = signed_headers.join(";");
    let mut canonical = String::new();
    canonical.push_str(request.method);
    canonical.push('\n');
    canonical.push_str(request.canonical_uri);
    canonical.push('\n');
    canonical.push_str(request.canonical_query);
    canonical.push('\n');
    for (name, value) in request.headers {
        canonical.push_str(name);
        canonical.push(':');
        canonical.push_str(value.trim());
        canonical.push('\n');
    }
    canonical.push('\n');
    canonical.push_str(&signed);
    canonical.push('\n');
    canonical.push_str(request.payload_sha256);
    let scope = format!("{date}/{region}/s3/aws4_request");
    let to_sign = format!(
        "AWS4-HMAC-SHA256\n{stamp}\n{scope}\n{}",
        sha256_hex(canonical.as_bytes())
    );
    let k_date = hmac_sha256(
        format!("AWS4{secret_access_key}").as_bytes(),
        date.as_bytes(),
    );
    let k_region = hmac_sha256(&k_date, region.as_bytes());
    let k_service = hmac_sha256(&k_region, b"s3");
    let k_signing = hmac_sha256(&k_service, b"aws4_request");
    let signature = hex(&hmac_sha256(&k_signing, to_sign.as_bytes()));
    format!(
        "AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, SignedHeaders={signed}, \
         Signature={signature}"
    )
}

fn hmac_sha256(key: &[u8], data: &[u8]) -> Vec<u8> {
    // HMAC accepts a key of any length, so construction cannot fail; an empty tag is the
    // unreachable fallback rather than a panic.
    Hmac::<Sha256>::new_from_slice(key).map_or_else(
        |_| Vec::new(),
        |mut mac| {
            mac.update(data);
            mac.finalize().into_bytes().to_vec()
        },
    )
}

/// S3's URI encoding of an object key: every byte but `A-Za-z0-9-_.~` escaped, `/` kept.
const S3_KEY: &AsciiSet = &NON_ALPHANUMERIC
    .remove(b'-')
    .remove(b'_')
    .remove(b'.')
    .remove(b'~')
    .remove(b'/');

/// botocore's `DEFAULT_TIMEOUT` (`connect_timeout` and `read_timeout`, seconds), which the
/// Python archive's `boto3.client("s3", ...)` runs with.
pub const S3_TIMEOUT_SECONDS: u64 = 60;

/// A minimal S3 client —`PutObject`, `GetObject`, `HeadObject`, path-style, SigV4-signed —
/// built on the guarded HTTP client. Stands where Python builds
/// `boto3.client("s3", endpoint_url=..., region_name=..., ...)` (DECISIONS-RS R4.14).
pub struct SigV4S3Client {
    http: GuardedClient,
    endpoint: String,
    region: String,
    access_key_id: Secret,
    secret_access_key: Secret,
    clock: std::sync::Arc<dyn Clock>,
}

impl std::fmt::Debug for SigV4S3Client {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SigV4S3Client")
            .field("endpoint", &self.endpoint)
            .field("region", &self.region)
            .finish_non_exhaustive()
    }
}

impl SigV4S3Client {
    /// A client for `endpoint_url` (empty: AWS's regional endpoint) in `region`.
    ///
    /// # Errors
    /// `UpstreamUnavailable` when the HTTP client cannot be built.
    pub fn configure(
        endpoint_url: &str,
        region: &str,
        access_key_id: Secret,
        secret_access_key: Secret,
    ) -> Result<Self, ProviderError> {
        let endpoint = if endpoint_url.is_empty() {
            format!("https://s3.{region}.amazonaws.com")
        } else {
            endpoint_url.trim_end_matches('/').to_owned()
        };
        // botocore's defaults: 60 s to connect and 60 s per read. Without them a stalled bucket
        // would hang the archive-then-parse step (and the nightly behind it) forever.
        let builder = crate::net::client_builder()
            .connect_timeout(std::time::Duration::from_secs(S3_TIMEOUT_SECONDS))
            .read_timeout(std::time::Duration::from_secs(S3_TIMEOUT_SECONDS));
        Ok(Self {
            http: GuardedClient::from_builder(builder)?,
            endpoint,
            region: region.to_owned(),
            access_key_id,
            secret_access_key,
            clock: std::sync::Arc::new(SystemClock),
        })
    }

    async fn send(
        &self,
        method: reqwest::Method,
        bucket: &str,
        key: &str,
        body: Option<(&[u8], &str, &BTreeMap<String, String>)>,
    ) -> Result<reqwest::Response, S3Failure> {
        let path = format!(
            "/{}/{}",
            utf8_percent_encode(bucket, S3_KEY),
            utf8_percent_encode(key, S3_KEY)
        );
        let url = format!("{}{path}", self.endpoint);
        let parsed = reqwest::Url::parse(&url)?;
        let host = match (parsed.host_str(), parsed.port()) {
            (Some(h), Some(p)) => format!("{h}:{p}"),
            (Some(h), None) => h.to_owned(),
            (None, _) => return Err(format!("no host in {url}").into()),
        };
        let payload: &[u8] = body.map_or(&[], |(b, _, _)| b);
        let payload_sha256 = sha256_hex(payload);
        let now = self.clock.now();
        let mut headers = BTreeMap::from([
            ("host".to_owned(), host),
            ("x-amz-content-sha256".to_owned(), payload_sha256.clone()),
            (
                "x-amz-date".to_owned(),
                now.format("%Y%m%dT%H%M%SZ").to_string(),
            ),
        ]);
        if let Some((_, content_type, metadata)) = body {
            headers.insert("content-type".to_owned(), content_type.to_owned());
            for (k, v) in metadata {
                headers.insert(format!("x-amz-meta-{}", k.to_lowercase()), v.clone());
            }
        }
        let authorization = sigv4_authorization(
            &SigV4Request {
                method: method.as_str(),
                canonical_uri: &path,
                canonical_query: "",
                headers: &headers,
                payload_sha256: &payload_sha256,
            },
            self.access_key_id.expose(),
            self.secret_access_key.expose(),
            &self.region,
            now,
        );
        let mut builder = self.http.request(method, &url)?;
        for (name, value) in &headers {
            if name != "host" {
                builder = builder.header(name.as_str(), value.as_str());
            }
        }
        builder = builder.header("authorization", authorization);
        if let Some((b, _, _)) = body {
            builder = builder.body(b.to_vec());
        }
        let response = builder.send().await?;
        let status = response.status();
        if !status.is_success() {
            return Err(format!("S3 answered {status}").into());
        }
        Ok(response)
    }
}

#[async_trait]
impl S3ClientLike for SigV4S3Client {
    async fn put_object(&self, request: PutObject<'_>) -> Result<(), S3Failure> {
        self.send(
            reqwest::Method::PUT,
            request.bucket,
            request.key,
            Some((request.body, request.content_type, &request.metadata)),
        )
        .await?;
        Ok(())
    }

    async fn get_object(&self, bucket: &str, key: &str) -> Result<Vec<u8>, S3Failure> {
        let response = self.send(reqwest::Method::GET, bucket, key, None).await?;
        Ok(response.bytes().await?.to_vec())
    }

    async fn head_object(&self, bucket: &str, key: &str) -> Result<(), S3Failure> {
        self.send(reqwest::Method::HEAD, bucket, key, None).await?;
        Ok(())
    }
}
