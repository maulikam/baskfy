//! The raw-file archive (Prompt 2 deliverable 3) — `test_archive.py`, case for case, plus the
//! S3 client's SigV4 signature against AWS's published example.
//!
//! docs/09: "Fetch once, archive the raw file to R2 keyed by `nse/{kind}/{date}.csv`, and parse
//! from the archive. Never re-fetch to re-parse." Three properties follow and all three are
//! asserted: the key layout, archive-before-parse ordering, and fetch-once.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;

use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use baskfy_providers::archive::{
    LocalRawArchive, PutObject, RawArchive, S3ClientLike, S3Failure, S3RawArchive, SigV4Request,
    archive_key, fetch_and_archive, sigv4_authorization,
};
use baskfy_providers::errors::ErrorKind;
use chrono::{NaiveDate, TimeZone, Utc};
use common::arm;

fn on() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 18).unwrap()
}

// --- TestKeyLayout ---------------------------------------------------------------------------

/// docs/09: `nse/{kind}/{date}.csv`.
#[test]
fn the_key_matches_the_documented_layout() {
    arm();
    assert_eq!(
        archive_key("bhavcopy", on(), "csv").unwrap(),
        "nse/bhavcopy/2026-08-18.csv"
    );
}

#[test]
fn a_non_csv_extension_is_honoured() {
    arm();
    assert_eq!(
        archive_key("bhavcopy", on(), "zip").unwrap(),
        "nse/bhavcopy/2026-08-18.zip"
    );
}

#[test]
fn a_leading_dot_on_the_extension_is_tolerated() {
    arm();
    assert_eq!(
        archive_key("listings", on(), ".json").unwrap(),
        "nse/listings/2026-08-18.json"
    );
}

#[test]
fn an_empty_kind_is_rejected() {
    arm();
    let err = archive_key("", on(), "csv").unwrap_err();
    assert_eq!(err.python_type(), "ValueError");
    assert!(err.to_string().contains("kind"));
}

/// ISO dates sort lexically, so an operator listing the bucket sees chronological order.
#[test]
fn the_date_is_sortable() {
    arm();
    let mut keys: Vec<String> = [18, 3, 11]
        .iter()
        .map(|d| {
            archive_key(
                "bhavcopy",
                NaiveDate::from_ymd_opt(2026, 8, *d).unwrap(),
                "csv",
            )
            .unwrap()
        })
        .collect();
    keys.sort();
    assert_eq!(
        keys,
        [
            "nse/bhavcopy/2026-08-03.csv",
            "nse/bhavcopy/2026-08-11.csv",
            "nse/bhavcopy/2026-08-18.csv"
        ]
    );
}

// --- TestLocalArchive ------------------------------------------------------------------------

#[tokio::test]
async fn local_round_trip() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let archive = LocalRawArchive::new(dir.path());
    let uri = archive
        .put(
            "nse/bhavcopy/2026-08-18.csv",
            b"SYMBOL,CLOSE\nSBIN,800\n",
            "text/csv",
        )
        .await
        .unwrap();
    assert!(uri.starts_with("file:///"));
    assert!(uri.ends_with("/nse/bhavcopy/2026-08-18.csv"));
    assert!(
        archive
            .get("nse/bhavcopy/2026-08-18.csv")
            .await
            .unwrap()
            .starts_with(b"SYMBOL")
    );
}

#[tokio::test]
async fn exists_is_false_before_and_true_after() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let archive = LocalRawArchive::new(dir.path());
    let key = archive_key("listings", on(), "csv").unwrap();
    assert!(!archive.exists(&key).await.unwrap());
    archive.put(&key, b"x", "text/csv").await.unwrap();
    assert!(archive.exists(&key).await.unwrap());
}

#[tokio::test]
async fn reading_a_missing_key_raises_archive_error() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let err = LocalRawArchive::new(dir.path())
        .get("nse/bhavcopy/1999-01-01.csv")
        .await
        .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::ArchiveError);
}

/// Keys are built from provider-supplied names; they must not write outside the root.
#[tokio::test]
async fn a_key_cannot_escape_the_archive_root() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let archive = LocalRawArchive::new(dir.path().join("root"));
    let err = archive
        .put("../../etc/passwd", b"nope", "text/csv")
        .await
        .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::ArchiveError);
    assert!(err.to_string().contains("escapes"));
    assert!(!dir.path().join("etc").exists());
}

#[test]
fn the_local_archive_describes_its_directory() {
    arm();
    assert_eq!(
        LocalRawArchive::new("/srv/raw").describe(),
        "local directory /srv/raw"
    );
}

// --- the S3 double (`FakeS3`) ----------------------------------------------------------------

#[derive(Default)]
struct FakeS3 {
    objects: Mutex<HashMap<String, Vec<u8>>>,
    metadata: Mutex<HashMap<String, BTreeMap<String, String>>>,
    content_types: Mutex<HashMap<String, String>>,
    fail_on_put: bool,
}

#[async_trait]
impl S3ClientLike for FakeS3 {
    async fn put_object(&self, request: PutObject<'_>) -> Result<(), S3Failure> {
        if self.fail_on_put {
            return Err("bucket is on fire".into());
        }
        self.objects
            .lock()
            .unwrap()
            .insert(request.key.to_owned(), request.body.to_vec());
        self.metadata
            .lock()
            .unwrap()
            .insert(request.key.to_owned(), request.metadata.clone());
        self.content_types
            .lock()
            .unwrap()
            .insert(request.key.to_owned(), request.content_type.to_owned());
        Ok(())
    }

    async fn get_object(&self, _bucket: &str, key: &str) -> Result<Vec<u8>, S3Failure> {
        self.objects
            .lock()
            .unwrap()
            .get(key)
            .cloned()
            .ok_or_else(|| format!("KeyError: {key}").into())
    }

    async fn head_object(&self, _bucket: &str, key: &str) -> Result<(), S3Failure> {
        if self.objects.lock().unwrap().contains_key(key) {
            Ok(())
        } else {
            Err(format!("KeyError: {key}").into())
        }
    }
}

// --- TestS3Archive ---------------------------------------------------------------------------

#[tokio::test]
async fn s3_round_trip() {
    arm();
    let archive = S3RawArchive::new(Arc::new(FakeS3::default()), "baskfy-raw").unwrap();
    let key = archive_key("bhavcopy", on(), "csv").unwrap();
    let uri = archive.put(&key, b"payload", "text/csv").await.unwrap();
    assert_eq!(uri, "s3://baskfy-raw/nse/bhavcopy/2026-08-18.csv");
    assert_eq!(archive.get(&key).await.unwrap(), b"payload");
    assert!(archive.exists(&key).await.unwrap());
    assert_eq!(archive.describe(), "s3://baskfy-raw");
}

/// Cheap corruption detection: what came back should be what went in.
#[tokio::test]
async fn a_checksum_is_recorded() {
    arm();
    let client = Arc::new(FakeS3::default());
    let archive = S3RawArchive::new(client.clone(), "baskfy-raw").unwrap();
    let key = archive_key("listings", on(), "csv").unwrap();
    archive
        .put(&key, b"payload", "application/zip")
        .await
        .unwrap();
    let metadata = client.metadata.lock().unwrap();
    // hashlib.sha256(b"payload").hexdigest()
    assert_eq!(
        metadata[&key]["sha256"],
        "239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5"
    );
    assert_eq!(
        client.content_types.lock().unwrap()[&key],
        "application/zip"
    );
}

#[tokio::test]
async fn a_failed_put_raises_archive_error() {
    arm();
    let fake = FakeS3 {
        fail_on_put: true,
        ..FakeS3::default()
    };
    let archive = S3RawArchive::new(Arc::new(fake), "baskfy-raw").unwrap();
    let err = archive
        .put(
            &archive_key("bhavcopy", on(), "csv").unwrap(),
            b"payload",
            "text/csv",
        )
        .await
        .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::ArchiveError);
    assert!(err.to_string().contains("could not archive"));
    assert_eq!(
        err.message(),
        "could not archive 'nse/bhavcopy/2026-08-18.csv' to s3://baskfy-raw: bucket is on fire"
    );
}

#[test]
fn an_unconfigured_bucket_is_refused_at_construction() {
    arm();
    let err = S3RawArchive::new(Arc::new(FakeS3::default()), "").unwrap_err();
    assert_eq!(err.kind(), ErrorKind::CredentialsMissing);
}

#[tokio::test]
async fn a_missing_s3_key_is_absent_and_unreadable() {
    arm();
    let archive = S3RawArchive::new(Arc::new(FakeS3::default()), "b").unwrap();
    assert!(!archive.exists("nse/x/2026-08-18.csv").await.unwrap());
    let err = archive.get("nse/x/2026-08-18.csv").await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::ArchiveError);
}

// --- TestArchiveThenParse --------------------------------------------------------------------

#[tokio::test]
async fn the_file_is_archived_before_it_is_returned() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let archive = LocalRawArchive::new(dir.path());
    let key = archive_key("bhavcopy", on(), "csv").unwrap();
    let payload = fetch_and_archive(
        &archive,
        &key,
        || async { Ok(b"raw bytes".to_vec()) },
        "text/csv",
        false,
    )
    .await
    .unwrap();
    assert!(archive.exists(&key).await.unwrap());
    assert_eq!(payload, b"raw bytes");
}

/// Not the bytes just fetched — otherwise "we saved a copy" and "we parsed the copy" can
/// silently diverge.
#[tokio::test]
async fn what_is_returned_is_read_back_from_the_archive() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let archive = LocalRawArchive::new(dir.path());
    let key = archive_key("bhavcopy", on(), "csv").unwrap();
    fetch_and_archive(
        &archive,
        &key,
        || async { Ok(b"original".to_vec()) },
        "text/csv",
        false,
    )
    .await
    .unwrap();
    // Tamper with the archived copy; a re-read must observe the tampering.
    archive.put(&key, b"tampered", "text/csv").await.unwrap();
    let got = fetch_and_archive(
        &archive,
        &key,
        || async { Ok(b"original".to_vec()) },
        "text/csv",
        false,
    )
    .await
    .unwrap();
    assert_eq!(got, b"tampered");
}

/// docs/09: "Never re-fetch to re-parse".
#[tokio::test]
async fn an_already_archived_file_is_not_refetched() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let archive = LocalRawArchive::new(dir.path());
    let key = archive_key("bhavcopy", on(), "csv").unwrap();
    let calls = AtomicUsize::new(0);
    let fetch = || async {
        calls.fetch_add(1, Ordering::SeqCst);
        Ok(b"payload".to_vec())
    };
    fetch_and_archive(&archive, &key, fetch, "text/csv", false)
        .await
        .unwrap();
    fetch_and_archive(&archive, &key, fetch, "text/csv", false)
        .await
        .unwrap();
    assert_eq!(calls.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn refetch_is_available_when_explicitly_asked_for() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let archive = LocalRawArchive::new(dir.path());
    let key = archive_key("bhavcopy", on(), "csv").unwrap();
    let calls = AtomicUsize::new(0);
    let fetch = || async {
        calls.fetch_add(1, Ordering::SeqCst);
        Ok(b"payload".to_vec())
    };
    fetch_and_archive(&archive, &key, fetch, "text/csv", false)
        .await
        .unwrap();
    fetch_and_archive(&archive, &key, fetch, "text/csv", true)
        .await
        .unwrap();
    assert_eq!(calls.load(Ordering::SeqCst), 2);
}

/// An empty upstream file is a publication failure, not a day with no trading.
#[tokio::test]
async fn an_empty_file_is_refused() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let archive = LocalRawArchive::new(dir.path());
    let err = fetch_and_archive(
        &archive,
        &archive_key("bhavcopy", on(), "csv").unwrap(),
        || async { Ok(Vec::new()) },
        "text/csv",
        false,
    )
    .await
    .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::ArchiveError);
    assert!(err.to_string().contains("empty"));
}

/// The tempting shortcut — carry on with the bytes in memory — is the one docs/09 forbids.
#[tokio::test]
async fn a_failed_archive_aborts_the_parse() {
    arm();
    let fake = FakeS3 {
        fail_on_put: true,
        ..FakeS3::default()
    };
    let archive = S3RawArchive::new(Arc::new(fake), "baskfy-raw").unwrap();
    let err = fetch_and_archive(
        &archive,
        &archive_key("bhavcopy", on(), "csv").unwrap(),
        || async { Ok(b"payload".to_vec()) },
        "text/csv",
        false,
    )
    .await
    .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::ArchiveError);
}

// --- The production S3 client's signer -------------------------------------------------------

/// AWS's published SigV4 example ("GET Object", examplebucket, 24 May 2013): the signer must
/// reproduce its signature exactly, or every put to R2 is refused.
#[test]
fn sigv4_reproduces_awss_published_get_object_example() {
    arm();
    let empty_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
    let headers = BTreeMap::from([
        (
            "host".to_owned(),
            "examplebucket.s3.amazonaws.com".to_owned(),
        ),
        ("range".to_owned(), "bytes=0-9".to_owned()),
        ("x-amz-content-sha256".to_owned(), empty_sha.to_owned()),
        ("x-amz-date".to_owned(), "20130524T000000Z".to_owned()),
    ]);
    let auth = sigv4_authorization(
        &SigV4Request {
            method: "GET",
            canonical_uri: "/test.txt",
            canonical_query: "",
            headers: &headers,
            payload_sha256: empty_sha,
        },
        "AKIAIOSFODNN7EXAMPLE",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "us-east-1",
        Utc.with_ymd_and_hms(2013, 5, 24, 0, 0, 0).unwrap(),
    );
    assert_eq!(
        auth,
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, \
         SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, \
         Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    );
}
