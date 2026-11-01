//! The shared token bucket — ports `tests/test_ratelimit.py` (Prompt 2 deliverable 2,
//! acceptance criterion 2: "Rate limiter test proves >3 req/s is impossible across two
//! concurrent workers"), plus the call spacer and the layered lanes.
//!
//! Python runs these against a live Redis and real time. Here they run against
//! `InMemoryScriptBackend` (the scripts' semantics, exactly) on tokio's **paused** clock: the
//! limiter, the server's `TIME` and every sleep share one virtual timeline, so two concurrent
//! workers interleave exactly as they would on a real box, and the assertions are deterministic
//! (DECISIONS-RS R4.8). `real_redis_*` repeats the single-worker cases against a real Redis when
//! `BASKFY_TEST_REDIS_URL` names one on loopback; otherwise it returns without running.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

mod common;

use std::sync::{Arc, Mutex};

use baskfy_providers::clock::{Clock, TokioClock, TokioSleeper};
use baskfy_providers::errors::ErrorKind;
use baskfy_providers::ratelimit::{
    CallSpacingConfig, InMemoryScriptBackend, LayeredCallSpacer, RateLimiter, RedisCallSpacer,
    RedisScriptBackend, RedisTokenBucket, SPACING_SCRIPT, ScriptBackend, SpacedLimiter,
    TAKE_SCRIPT, TokenBucketConfig,
};
use baskfy_providers::secret::Secret;
use common::{FixedRandom, arm};

/// docs/09's figure for Kite.
const KITE_RATE: f64 = 3.0;

/// tokio's timer wheel fires on whole milliseconds, so a departure lands up to 1 ms after its
/// reserved slot; the *reservations* are exactly 1/rate apart.
const TIMER_RESOLUTION: f64 = 1e-3;

struct World {
    clock: Arc<TokioClock>,
    backend: Arc<dyn ScriptBackend>,
}

fn world() -> World {
    let clock = Arc::new(TokioClock::new());
    let backend: Arc<dyn ScriptBackend> =
        Arc::new(InMemoryScriptBackend::new(clock.clone() as Arc<dyn Clock>));
    World { clock, backend }
}

fn make_bucket(w: &World, key: &str, rate: f64, max_wait: f64) -> RedisTokenBucket {
    RedisTokenBucket::with_seams(
        w.backend.clone(),
        key,
        TokenBucketConfig::with_max_wait(rate, rate, max_wait).unwrap(),
        w.clock.clone(),
        Arc::new(TokioSleeper),
        Arc::new(FixedRandom(0.5)),
    )
}

// --- TestSingleWorker ---------------------------------------------------------------------

#[tokio::test(start_paused = true)]
async fn a_cold_bucket_starts_full() {
    arm();
    let w = world();
    let bucket = make_bucket(&w, "t:cold", KITE_RATE, 30.0);
    for _ in 0..3 {
        assert_eq!(bucket.try_acquire(1.0, None).await.unwrap(), 0.0);
    }
}

#[tokio::test(start_paused = true)]
async fn exhausting_the_bucket_reports_the_wait() {
    arm();
    let w = world();
    let bucket = make_bucket(&w, "t:exhaust", KITE_RATE, 30.0);
    for _ in 0..3 {
        bucket.try_acquire(1.0, None).await.unwrap();
    }
    let wait = bucket.try_acquire(1.0, None).await.unwrap();
    assert!(wait > 0.0 && wait <= 1.0 / KITE_RATE + 0.01, "{wait}");
}

#[tokio::test(start_paused = true)]
async fn tokens_refill_at_the_configured_rate() {
    arm();
    let w = world();
    let bucket = make_bucket(&w, "t:refill", KITE_RATE, 30.0);
    let start = w.clock.monotonic();
    for _ in 0..3 {
        bucket.try_acquire(1.0, Some(start)).await.unwrap();
    }
    assert!(bucket.try_acquire(1.0, Some(start)).await.unwrap() > 0.0);
    assert_eq!(
        bucket
            .try_acquire(1.0, Some(start + 1.0 / KITE_RATE + 0.001))
            .await
            .unwrap(),
        0.0
    );
}

#[tokio::test(start_paused = true)]
async fn it_gives_up_rather_than_waiting_forever() {
    arm();
    let w = world();
    let bucket = make_bucket(&w, "t:giveup", 1.0, 0.05);
    bucket.acquire(1.0).await.unwrap();
    let err = bucket.acquire(1.0).await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::RateLimited);
    assert!(err.to_string().contains("budget"), "{err}");
    assert_eq!(
        err.to_string(),
        "waited 0.00s for a rate-limit token on 't:giveup' and the next one is 1.00s away, \
         exceeding the 0s budget"
    );
}

// --- TestConcurrentWorkers ----------------------------------------------------------------

#[tokio::test(start_paused = true)]
async fn two_workers_cannot_exceed_the_rate_between_them() {
    arm();
    let w = world();
    let total_requests = 12;
    let workers = 2;
    let a = Arc::new(make_bucket(&w, "t:two", KITE_RATE, 30.0));
    let b = Arc::new(make_bucket(&w, "t:two", KITE_RATE, 30.0));
    let start = w.clock.monotonic();
    let run = |bucket: Arc<RedisTokenBucket>| {
        tokio::spawn(async move {
            for _ in 0..(total_requests / workers) {
                bucket.acquire(1.0).await.unwrap();
            }
            total_requests / workers
        })
    };
    let (ra, rb) = (run(a), run(b));
    let done = ra.await.unwrap() + rb.await.unwrap();
    let elapsed = w.clock.monotonic() - start;
    assert_eq!(done, total_requests);
    // 3 free from the initial burst, the other 9 paced at KITE_RATE per second.
    let floor = (f64::from(total_requests) - KITE_RATE) / KITE_RATE;
    assert!(
        elapsed >= floor * 0.9,
        "{total_requests} requests across {workers} workers finished in {elapsed:.2}s; a shared \
         {KITE_RATE} req/s limiter cannot go below ~{floor:.2}s"
    );
}

#[tokio::test(start_paused = true)]
async fn sustained_throughput_never_exceeds_the_configured_rate() {
    arm();
    let w = world();
    let total_requests = 14;
    let stamps = Arc::new(Mutex::new(Vec::<f64>::new()));
    let mut handles = Vec::new();
    for _ in 0..2 {
        let bucket = make_bucket(&w, "t:sustained", KITE_RATE, 30.0);
        let stamps = stamps.clone();
        let clock = w.clock.clone();
        handles.push(tokio::spawn(async move {
            for _ in 0..(total_requests / 2) {
                bucket.acquire(1.0).await.unwrap();
                stamps.lock().unwrap().push(clock.monotonic());
            }
        }));
    }
    for h in handles {
        h.await.unwrap();
    }
    let mut stamps = stamps.lock().unwrap().clone();
    stamps.sort_by(f64::total_cmp);

    // 1. The token-bucket bound: capacity + rate x T in any window of length T.
    let burst_ceiling = KITE_RATE + KITE_RATE * 1.0;
    let max_in_window = stamps
        .iter()
        .map(|&s| stamps.iter().filter(|&&t| s <= t && t < s + 1.0).count())
        .max()
        .unwrap();
    assert!(
        f64::from(u32::try_from(max_in_window).unwrap()) <= burst_ceiling,
        "observed {max_in_window} requests in a one-second window"
    );

    // 2. The sustained bound: discount the initial burst and the rate must hold.
    let span = stamps[stamps.len() - 1] - stamps[0];
    let sustained = (f64::from(u32::try_from(stamps.len()).unwrap()) - KITE_RATE) / span;
    assert!(
        sustained <= KITE_RATE * 1.15,
        "sustained {sustained:.2} req/s"
    );
}

#[tokio::test(start_paused = true)]
async fn the_burst_is_bounded_by_capacity() {
    arm();
    let w = world();
    let bucket = make_bucket(&w, "t:burst", KITE_RATE, 30.0);
    let mut granted = 0;
    while bucket.try_acquire(1.0, None).await.unwrap() == 0.0 {
        granted += 1;
        assert!(granted <= 30, "the bucket granted an unbounded burst");
    }
    assert_eq!(granted, 3);
}

#[tokio::test(start_paused = true)]
async fn a_separate_key_is_a_separate_budget() {
    arm();
    let w = world();
    let kite = make_bucket(&w, "t:sep:kite", KITE_RATE, 30.0);
    let nse = make_bucket(&w, "t:sep:nse", KITE_RATE, 30.0);
    for _ in 0..3 {
        kite.try_acquire(1.0, None).await.unwrap();
    }
    assert!(kite.try_acquire(1.0, None).await.unwrap() > 0.0);
    assert_eq!(nse.try_acquire(1.0, None).await.unwrap(), 0.0);
}

#[tokio::test(start_paused = true)]
async fn an_idle_bucket_expires_and_starts_full_again() {
    arm();
    let w = world();
    let bucket = make_bucket(&w, "t:expire", KITE_RATE, 30.0);
    for _ in 0..3 {
        bucket.try_acquire(1.0, None).await.unwrap();
    }
    // PEXPIRE is ceil((capacity/rate + 1) * 1000) ms = 2 s.
    tokio::time::sleep(std::time::Duration::from_millis(2001)).await;
    for _ in 0..3 {
        assert_eq!(bucket.try_acquire(1.0, None).await.unwrap(), 0.0);
    }
}

// --- TestConfiguration --------------------------------------------------------------------

#[test]
fn a_non_positive_rate_is_rejected() {
    arm();
    let err = TokenBucketConfig::new(0.0, 3.0).unwrap_err();
    assert_eq!(err.python_type(), "ValueError");
    assert!(err.to_string().contains("rate_per_second"));
}

#[test]
fn a_non_positive_capacity_is_rejected() {
    arm();
    let err = TokenBucketConfig::new(3.0, 0.0).unwrap_err();
    assert!(err.to_string().contains("capacity"));
}

// --- The call spacer and the lanes (M85) ---------------------------------------------------

fn spacer(w: &World, key: &str, rate: f64, max_wait: f64) -> Arc<RedisCallSpacer> {
    Arc::new(RedisCallSpacer::with_sleeper(
        w.backend.clone(),
        key,
        CallSpacingConfig::with_max_wait(rate, max_wait).unwrap(),
        Arc::new(TokioSleeper),
    ))
}

#[tokio::test(start_paused = true)]
async fn a_cold_spacer_grants_no_burst() {
    arm();
    let w = world();
    let s = spacer(&w, "t:space", KITE_RATE, 30.0);
    let start = w.clock.monotonic();
    let mut departures = Vec::new();
    for _ in 0..4 {
        s.acquire(1.0).await.unwrap();
        departures.push(w.clock.monotonic() - start);
    }
    for pair in departures.windows(2) {
        assert!(
            pair[1] - pair[0] >= 1.0 / KITE_RATE - TIMER_RESOLUTION,
            "{departures:?}"
        );
    }
    assert!(departures[0] < 1e-6);
}

#[tokio::test(start_paused = true)]
async fn a_spacer_refuses_beyond_its_budget_with_python_s_message() {
    arm();
    let w = world();
    let s = spacer(&w, "t:space:budget", 1.0, 0.5);
    s.acquire(1.0).await.unwrap();
    // The first departure took the slot at t=0; the next is 1 s away, over the 0.5 s budget.
    let err = s.acquire(1.0).await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::RateLimited);
    assert_eq!(
        err.to_string(),
        "the next rate-limit slot on 't:space:budget' is 1.00s away, exceeding the 0s budget"
    );
}

#[tokio::test(start_paused = true)]
async fn a_spacer_grants_one_request_per_acquire() {
    arm();
    let w = world();
    let err = spacer(&w, "t:space:one", 1.0, 1.0)
        .acquire(2.0)
        .await
        .unwrap_err();
    assert_eq!(err.python_type(), "ValueError");
}

#[tokio::test(start_paused = true)]
async fn concurrent_callers_on_one_spacer_key_are_spaced_box_wide() {
    arm();
    let w = world();
    let stamps = Arc::new(Mutex::new(Vec::<f64>::new()));
    let mut handles = Vec::new();
    for _ in 0..3 {
        let s = spacer(&w, "t:space:shared", KITE_RATE, 30.0);
        let stamps = stamps.clone();
        let clock = w.clock.clone();
        handles.push(tokio::spawn(async move {
            for _ in 0..4 {
                s.acquire(1.0).await.unwrap();
                stamps.lock().unwrap().push(clock.monotonic());
            }
        }));
    }
    for h in handles {
        h.await.unwrap();
    }
    let mut stamps = stamps.lock().unwrap().clone();
    stamps.sort_by(f64::total_cmp);
    for pair in stamps.windows(2) {
        assert!(
            pair[1] - pair[0] >= 1.0 / KITE_RATE - TIMER_RESOLUTION,
            "{stamps:?}"
        );
    }
}

#[tokio::test(start_paused = true)]
async fn a_bulk_lane_leaves_the_ceiling_free_for_an_interactive_read() {
    // M85: bulk at 2/s through its own clock, then the 3/s ceiling; an interactive call takes
    // the ceiling alone and never waits more than one ceiling interval.
    arm();
    let w = world();
    let ceiling = spacer(&w, "t:kite:read", 3.0, 30.0);
    let lane = spacer(&w, "t:kite:bulk", 2.0, 30.0);
    let bulk = LayeredCallSpacer::new(vec![
        lane.clone() as Arc<dyn SpacedLimiter>,
        ceiling.clone() as Arc<dyn SpacedLimiter>,
    ])
    .unwrap();
    assert_eq!(bulk.key(), "t:kite:read");
    let bulk = Arc::new(bulk);
    let b = bulk.clone();
    let backfill = tokio::spawn(async move {
        for _ in 0..40 {
            b.acquire(1.0).await.unwrap();
        }
    });
    tokio::time::sleep(std::time::Duration::from_secs(5)).await;
    let waited = ceiling.acquire(1.0).await.unwrap();
    assert!(
        waited <= 1.0 / 3.0 + 1e-6,
        "interactive read waited {waited}s"
    );
    backfill.await.unwrap();
}

#[test]
fn a_layered_spacer_needs_a_clock() {
    arm();
    let err = LayeredCallSpacer::new(Vec::new()).err().unwrap();
    assert!(err.to_string().contains("at least one clock"));
}

// --- The scripts are Python's, byte for byte ------------------------------------------------

fn python_script(name: &str) -> String {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../decile-blueprint/packages/providers/src/baskfy_providers/ratelimit.py"
    );
    let source = std::fs::read_to_string(path).unwrap();
    let open = format!("{name} = \"\"\"");
    let start = source.find(&open).unwrap() + open.len();
    let end = start + source[start..].find("\"\"\"").unwrap();
    source[start..end].to_owned()
}

#[test]
fn the_lua_scripts_are_byte_identical_to_python_s() {
    arm();
    assert_eq!(TAKE_SCRIPT, python_script("TAKE_SCRIPT"));
    assert_eq!(SPACING_SCRIPT, python_script("SPACING_SCRIPT"));
}

// --- Against a real Redis, when one is named on loopback ------------------------------------

#[tokio::test]
async fn real_redis_single_worker_cases() {
    arm();
    let Ok(url) = std::env::var("BASKFY_TEST_REDIS_URL") else {
        return;
    };
    let backend: Arc<dyn ScriptBackend> = Arc::new(
        RedisScriptBackend::connect(&Secret::new(url))
            .await
            .unwrap(),
    );
    let clock: Arc<dyn Clock> = Arc::new(baskfy_providers::clock::SystemClock);
    let key = format!("baskfy:test:ratelimit:rust:{}", clock.monotonic());
    let bucket = RedisTokenBucket::with_seams(
        backend,
        &key,
        TokenBucketConfig::new(KITE_RATE, KITE_RATE).unwrap(),
        clock.clone(),
        Arc::new(TokioSleeper),
        Arc::new(FixedRandom(0.5)),
    );
    let start = clock.monotonic();
    for _ in 0..3 {
        assert_eq!(bucket.try_acquire(1.0, Some(start)).await.unwrap(), 0.0);
    }
    assert!(bucket.try_acquire(1.0, Some(start)).await.unwrap() > 0.0);
    assert_eq!(
        bucket
            .try_acquire(1.0, Some(start + 1.0 / KITE_RATE + 0.001))
            .await
            .unwrap(),
        0.0
    );
}

#[tokio::test]
async fn an_unparseable_redis_url_never_reaches_the_message() {
    arm();
    // A Redis URL can carry a password; a parse failure must not quote it.
    let url = Secret::new("redis://:hunter2@[not-a-host");
    let err = RedisScriptBackend::connect(&url).await.unwrap_err();
    assert_eq!(err.python_type(), "ValueError");
    assert!(!err.to_string().contains("hunter2"), "{err}");
    assert!(!format!("{err:?}").contains("hunter2"));
}
