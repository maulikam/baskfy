//! `baskfy_api::idempotency` against the bed's Redis (database 14, flushed of its own keys only),
//! the sequence `baskfy_api.idempotency`'s docstring promises (M45.6): one winner, the losers told
//! the request is in flight, a completed record replayed, a failed attempt's key given back, and
//! no idempotency at all without a key. Reported ignored, with the reason, off the bed (R5.4).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_api::idempotency::{PENDING, Reservation, release, remember, replay, reserve};
use baskfy_providers::secret::Secret;

fn url() -> Secret {
    Secret::new(
        std::env::var("BASKFY_RS_TEST_REDIS_URL")
            .unwrap_or_else(|_| "redis://127.0.0.1:56379/14".to_owned()),
    )
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn reserve_replay_release_follow_pythons_contract() {
    let url = url();
    let scope = format!("user:rs-test-{}", std::process::id());
    let key = Some("retry-1");

    assert_eq!(reserve(&url, &scope, key).await, Reservation::Reserved);
    // A second request with the same key while the first is working.
    assert_eq!(reserve(&url, &scope, key).await, Reservation::InFlight);
    assert_eq!(replay(&url, &scope, key).await.as_deref(), Some(PENDING));
    // The first finishes: the record names what it made, and a retry replays it.
    remember(&url, &scope, key, "0123456789abcdef01234567").await;
    assert_eq!(
        reserve(&url, &scope, key).await,
        Reservation::Replay("0123456789abcdef01234567".to_owned())
    );
    // `release` is compare-and-delete: a completed record is never thrown away.
    release(&url, &scope, key).await;
    assert_eq!(
        replay(&url, &scope, key).await.as_deref(),
        Some("0123456789abcdef01234567")
    );

    // A failed attempt gives its key back, so the client's retry is not refused.
    let other = Some("retry-2");
    assert_eq!(reserve(&url, &scope, other).await, Reservation::Reserved);
    release(&url, &scope, other).await;
    assert_eq!(replay(&url, &scope, other).await, None);
    assert_eq!(reserve(&url, &scope, other).await, Reservation::Reserved);
    release(&url, &scope, other).await;

    // No key: no idempotency, and nothing written.
    assert_eq!(reserve(&url, &scope, None).await, Reservation::Reserved);
    assert_eq!(reserve(&url, &scope, Some("")).await, Reservation::Reserved);
    // Leave nothing behind on the shared bed.
    for k in ["retry-1", "retry-2"] {
        baskfy_api::cache::eval(
            &url,
            "return redis.call('del', KEYS[1])",
            &[baskfy_api::idempotency::redis_key(&scope, k)],
            &[],
        )
        .await
        .unwrap();
    }
}

#[tokio::test]
async fn without_redis_every_call_degrades_to_no_idempotency() {
    // Port 9 on loopback: nothing listens. Never a refused write.
    let url = Secret::new("redis://127.0.0.1:9/14");
    assert_eq!(reserve(&url, "s", Some("k")).await, Reservation::Reserved);
    assert_eq!(replay(&url, "s", Some("k")).await, None);
    remember(&url, "s", Some("k"), "v").await;
    release(&url, "s", Some("k")).await;
}
