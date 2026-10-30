//! Ports `tests/test_ratelimit.py` (the order path's limiter, which failed in production on
//! 18 Aug 2026) and the clock-driven half of `tests/test_kite_limits.py` (the read limiter's
//! caps, burst floors, and the shared clock's composition). The source scans of
//! `test_kite_limits.py` over `app/kite_client.py` belong to the lane porting the client.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use baskfy_providers::ratelimit::{ScriptBackend, ScriptValue};
use baskfy_providers::{ErrorKind, ProviderError};
use tokio::time::Instant;

use super::super::kite_limits::{
    DeskLimits, FAMILY_FOR_CALL, GENERAL_PER_SECOND, HISTORICAL_PER_SECOND, LimitClock,
    MAX_SHARED_WAIT_SECONDS, QUOTE_PER_SECOND, RATE_FOR_FAMILY, READ_PER_SECOND, SHARED_KEY_PREFIX,
    SHARED_RETRY_AFTER_SECONDS, SharedSpacer, Spacer as ReadSpacer, claim, family_for_call,
};
use super::super::ratelimit::{Bucket, GatewayLimits, KiteLimits, Spacer};

// --- the order path (test_ratelimit.py) ------------------------------------------------------

/// Grant times, in seconds from the first call, on tokio's paused clock (Python measures the
/// real `time.monotonic`; a paused runtime proves the same floor without spending it).
async fn grant_times<F, Fut>(n: usize, mut take: F) -> Vec<f64>
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = ()>,
{
    let t0 = Instant::now();
    let mut out = Vec::with_capacity(n);
    for _ in 0..n {
        take().await;
        out.push(t0.elapsed().as_secs_f64());
    }
    out
}

fn max_in_any_window(times: &[f64]) -> usize {
    times
        .iter()
        .map(|s| times.iter().filter(|t| *s <= **t && **t < s + 1.0).count())
        .max()
        .unwrap_or(0)
}

#[tokio::test(start_paused = true)]
async fn test_twenty_one_orders_never_exceed_nine_in_any_second() {
    let lim = KiteLimits::default();
    let times = grant_times(21, || lim.orders_sec.take()).await;
    assert!(
        max_in_any_window(&times) <= 9,
        "burst of {}",
        max_in_any_window(&times)
    );
}

#[tokio::test(start_paused = true)]
async fn test_a_full_token_bucket_would_have_allowed_the_burst() {
    let b = Bucket::new(9, 1.0);
    let times = grant_times(18, || b.take()).await;
    assert!(max_in_any_window(&times) > 9);
}

#[tokio::test(start_paused = true)]
async fn test_the_spacer_holds_its_interval() {
    let sp = Spacer::new(9.0);
    let times = grant_times(10, || sp.take()).await;
    let min_gap = times
        .windows(2)
        .map(|w| w[1] - w[0])
        .fold(f64::INFINITY, f64::min);
    assert!(min_gap >= 1.0 / 9.0 * 0.95, "gap {min_gap}");
}

#[tokio::test(start_paused = true)]
async fn test_concurrent_callers_cannot_both_take_the_same_slot() {
    let sp = Arc::new(Spacer::new(9.0));
    let t0 = Instant::now();
    let tasks: Vec<_> = (0..9)
        .map(|_| {
            let sp = sp.clone();
            tokio::spawn(async move { sp.take().await })
        })
        .collect();
    for t in tasks {
        t.await.unwrap();
    }
    let elapsed = t0.elapsed().as_secs_f64();
    assert!(
        elapsed >= 8.0 / 9.0 * 0.95,
        "nine concurrent grants took only {elapsed:.3}s"
    );
}

#[tokio::test(start_paused = true)]
async fn test_the_full_order_slot_still_paces() {
    let lim = KiteLimits::default();
    let times = grant_times(12, || lim.order_slot()).await;
    assert!(max_in_any_window(&times) <= 9);
}

#[tokio::test(start_paused = true)]
async fn test_the_limiter_stays_under_the_sebi_threshold() {
    let lim = KiteLimits::default();
    let times = grant_times(15, || lim.orders_sec.take()).await;
    assert!(max_in_any_window(&times) < 10);
}

#[tokio::test(start_paused = true)]
async fn the_long_windows_are_buckets_under_kites_caps() {
    // test_the_order_path_is_spaced_not_bucketed: the types say Spacer/Bucket; the numbers
    // are 380/min (under 400) and 2,900/day (under 3,000).
    let lim = KiteLimits::default();
    let times = grant_times(380, || lim.orders_min.take()).await;
    assert!(
        times.last().copied().unwrap_or(1.0) < 1e-6,
        "380 granted at once"
    );
    let t0 = Instant::now();
    lim.orders_min.take().await;
    assert!(
        t0.elapsed().as_secs_f64() > 0.1,
        "the 381st waits for a refill"
    );
}

// --- the read limiter (test_kite_limits.py) --------------------------------------------------

/// A clock and a sleep that agree with each other, so a burst test costs no seconds.
#[derive(Default)]
struct FakeClock {
    state: Mutex<(f64, f64)>,
}

impl FakeClock {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            state: Mutex::new((1000.0, 0.0)),
        })
    }
    fn slept(&self) -> f64 {
        self.state.lock().unwrap().1
    }
}

#[async_trait]
impl LimitClock for FakeClock {
    fn now(&self) -> f64 {
        self.state.lock().unwrap().0
    }
    async fn sleep(&self, seconds: f64) {
        let mut s = self.state.lock().unwrap();
        s.0 += seconds;
        s.1 += seconds;
    }
}

fn limits() -> (DeskLimits, Arc<FakeClock>) {
    let clock = FakeClock::new();
    (DeskLimits::new(clock.clone(), HashMap::new()), clock)
}

fn approx(a: f64, b: f64) -> bool {
    (a - b).abs() <= 1e-6 * b.abs().max(1e-12)
}

#[test]
fn test_the_families_and_their_rates() {
    assert!(approx(QUOTE_PER_SECOND, 1.0));
    assert!(approx(HISTORICAL_PER_SECOND, 3.0));
    assert!(approx(GENERAL_PER_SECOND, 9.0));
    assert!(approx(READ_PER_SECOND, 3.0));
    let fams: Vec<&str> = RATE_FOR_FAMILY.iter().map(|(f, _)| *f).collect();
    assert_eq!(fams, ["read", "quote", "historical", "general"]);
}

#[test]
fn test_the_combined_ceiling_sits_above_every_family() {
    // M85: Kite's combined ceiling is 3 HTTP requests a second; the fourth is a 429.
    let read = RATE_FOR_FAMILY
        .iter()
        .find(|(f, _)| *f == "read")
        .map(|(_, r)| *r);
    assert_eq!(read, Some(3.0));
    for (fam, rate) in RATE_FOR_FAMILY {
        assert!(rate > 0.0, "{fam}");
    }
}

#[test]
fn test_each_kite_method_is_filed_under_its_own_cap() {
    for (call, family) in [
        ("ltp", "quote"),
        ("quote", "quote"),
        ("ohlc", "quote"),
        ("historical_data", "historical"),
        ("holdings", "general"),
        ("positions", "general"),
        ("orders", "general"),
        ("order_history", "general"),
        ("trades", "general"),
        ("margins", "general"),
        ("instruments", "general"),
        ("profile", "general"),
        ("get_gtts", "general"),
    ] {
        assert_eq!(family_for_call(call), Some(family), "{call}");
    }
    assert_eq!(FAMILY_FOR_CALL.len(), 13);
}

#[tokio::test]
async fn test_an_unknown_method_is_throttled_rather_than_free() {
    let (l, clock) = limits();
    l.slot_for_call("some_endpoint_kite_adds_in_2027").await;
    l.slot_for_call("some_endpoint_kite_adds_in_2027").await;
    assert!(
        approx(clock.slept(), 1.0 / READ_PER_SECOND),
        "{}",
        clock.slept()
    );
}

#[tokio::test]
async fn test_thirty_quotes_take_at_least_twenty_nine_seconds() {
    let (l, clock) = limits();
    for _ in 0..30 {
        l.slot("quote").await.unwrap();
    }
    assert!(approx(clock.slept(), 29.0), "{}", clock.slept());
}

#[tokio::test]
async fn test_thirty_historical_calls_take_at_least_nine_and_two_thirds() {
    let (l, clock) = limits();
    for _ in 0..30 {
        l.slot("historical").await.unwrap();
    }
    assert!(approx(clock.slept(), 29.0 / 3.0), "{}", clock.slept());
}

#[tokio::test]
async fn test_thirty_general_calls_take_at_least_nine_and_two_thirds() {
    let (l, clock) = limits();
    for _ in 0..30 {
        l.slot("general").await.unwrap();
    }
    assert!(
        approx(clock.slept(), 29.0 / READ_PER_SECOND),
        "{}",
        clock.slept()
    );
}

#[tokio::test]
async fn test_a_backfill_costs_another_family_one_slot_and_never_its_queue() {
    let (l, clock) = limits();
    for _ in 0..10 {
        l.slot("historical").await.unwrap();
    }
    let before = clock.slept();
    l.slot("quote").await.unwrap();
    let waited = clock.slept() - before;
    assert!(
        waited <= 1.0 / READ_PER_SECOND + 1e-9,
        "a quote waited {waited:.3}s"
    );
}

#[tokio::test]
async fn test_the_first_call_of_each_family_waits_only_on_the_combined_ceiling() {
    let (l, clock) = limits();
    for fam in ["quote", "historical", "general"] {
        l.slot(fam).await.unwrap();
    }
    assert!(
        approx(clock.slept(), 2.0 / READ_PER_SECOND),
        "{}",
        clock.slept()
    );
}

#[tokio::test]
async fn test_the_very_first_call_of_the_process_is_free() {
    let (l, clock) = limits();
    l.slot("quote").await.unwrap();
    assert!(clock.slept() == 0.0);
}

#[tokio::test]
async fn test_two_tasks_get_two_different_slots() {
    let clock = FakeClock::new();
    let sp = Arc::new(ReadSpacer::new(20.0, clock).unwrap());
    let a = tokio::spawn({
        let sp = sp.clone();
        async move { sp.take().await }
    });
    let b = tokio::spawn({
        let sp = sp.clone();
        async move { sp.take().await }
    });
    let waits = [a.await.unwrap(), b.await.unwrap()];
    assert!(
        waits.iter().copied().fold(0.0, f64::max) >= 0.04,
        "both left at once: {waits:?}"
    );
}

#[test]
fn test_a_non_positive_rate_is_refused() {
    let e = ReadSpacer::new(0.0, FakeClock::new()).unwrap_err();
    assert_eq!(e.python_type, "ValueError");
    assert!(e.message.contains("positive"), "{}", e.message);
}

#[tokio::test]
async fn an_unknown_family_is_a_key_error() {
    let (l, _) = limits();
    let e = l.slot("orders").await.unwrap_err();
    assert_eq!(e.python_type, "KeyError");
}

/// `StubScript`: answers the Lua with fixed replies (or fails), and records what it was told.
struct StubScript {
    reply: Option<ScriptValue>,
    calls: Mutex<Vec<(Vec<String>, Vec<String>)>>,
}

impl StubScript {
    fn ok(flag: i64, wait: &str) -> Arc<Self> {
        Arc::new(Self {
            reply: Some(ScriptValue::Array(vec![
                ScriptValue::Int(flag),
                ScriptValue::Bytes(wait.as_bytes().to_vec()),
            ])),
            calls: Mutex::new(Vec::new()),
        })
    }
    fn dead() -> Arc<Self> {
        Arc::new(Self {
            reply: None,
            calls: Mutex::new(Vec::new()),
        })
    }
    fn n(&self) -> usize {
        self.calls.lock().unwrap().len()
    }
}

#[async_trait]
impl ScriptBackend for StubScript {
    async fn eval(
        &self,
        _script: &'static str,
        keys: &[String],
        args: &[String],
    ) -> Result<ScriptValue, ProviderError> {
        self.calls
            .lock()
            .unwrap()
            .push((keys.to_vec(), args.to_vec()));
        self.reply
            .clone()
            .ok_or_else(|| ProviderError::new(ErrorKind::ProviderUnavailable, "connection refused"))
    }
}

fn shared(
    script: Arc<StubScript>,
    family: &str,
    rate: f64,
    clock: Arc<FakeClock>,
) -> HashMap<String, Arc<SharedSpacer>> {
    let sp = SharedSpacer::new(
        script,
        &format!("k:{family}"),
        rate,
        MAX_SHARED_WAIT_SECONDS,
        SHARED_RETRY_AFTER_SECONDS,
        clock,
    )
    .unwrap();
    HashMap::from([(family.to_owned(), Arc::new(sp))])
}

#[test]
fn test_the_keys_live_under_the_namespace_baskfy_already_owns() {
    assert_eq!(SHARED_KEY_PREFIX, "baskfy:ratelimit:kite");
}

#[tokio::test]
async fn test_a_dead_redis_still_spaces_the_call_locally() {
    let clock = FakeClock::new();
    let script = StubScript::dead();
    let l = DeskLimits::new(
        clock.clone(),
        shared(script.clone(), "quote", 1.0, clock.clone()),
    );
    l.slot("quote").await.unwrap();
    l.slot("quote").await.unwrap();
    assert!(approx(clock.slept(), 1.0), "{}", clock.slept());
    assert_eq!(l.local_only("quote"), 2);
}

#[tokio::test]
async fn test_a_dead_redis_is_not_asked_again_on_every_read() {
    let clock = FakeClock::new();
    let script = StubScript::dead();
    let l = DeskLimits::new(
        clock.clone(),
        shared(script.clone(), "quote", 1.0, clock.clone()),
    );
    for _ in 0..5 {
        l.slot("quote").await.unwrap();
    }
    assert_eq!(
        script.n(),
        1,
        "the back-off holds for SHARED_RETRY_AFTER_SECONDS"
    );
}

#[tokio::test]
async fn test_a_queue_past_the_budget_falls_back_rather_than_hanging_the_page() {
    let clock = FakeClock::new();
    let script = StubScript::ok(0, &format!("{}", MAX_SHARED_WAIT_SECONDS + 5.0));
    let l = DeskLimits::new(
        clock.clone(),
        shared(script, "quote", QUOTE_PER_SECOND, clock.clone()),
    );
    l.slot("quote").await.unwrap();
    assert!(
        clock.slept() == 0.0,
        "it waited out a slot it was told it could not have"
    );
    assert_eq!(l.local_only("quote"), 1);
}

#[test]
fn test_a_reply_the_script_could_not_have_sent_is_refused() {
    for junk in [
        ScriptValue::Bytes(b"ok".to_vec()),
        ScriptValue::Array(vec![ScriptValue::Int(1)]),
        ScriptValue::Array(vec![
            ScriptValue::Int(1),
            ScriptValue::Int(2),
            ScriptValue::Int(3),
        ]),
        ScriptValue::Nil,
    ] {
        let e = claim(&junk).unwrap_err();
        assert!(e.message.contains("Kite limiter"), "{}", e.message);
    }
    assert_eq!(
        claim(&ScriptValue::Array(vec![
            ScriptValue::Int(1),
            ScriptValue::Bytes(b"0.25".to_vec())
        ]))
        .unwrap(),
        (true, 0.25)
    );
}

#[tokio::test]
async fn test_the_shared_clock_cannot_loosen_the_local_floor() {
    let clock = FakeClock::new();
    let script = StubScript::ok(1, "0");
    let l = DeskLimits::new(
        clock.clone(),
        shared(script.clone(), "quote", QUOTE_PER_SECOND, clock.clone()),
    );
    for _ in 0..30 {
        l.slot("quote").await.unwrap();
    }
    assert!(approx(clock.slept(), 29.0), "{}", clock.slept());
    assert_eq!(script.n(), 30, "a read went out without asking the box");
}

#[tokio::test]
async fn test_the_shared_clock_can_tighten_it() {
    let clock = FakeClock::new();
    let script = StubScript::ok(1, "2.0");
    let l = DeskLimits::new(
        clock.clone(),
        shared(script, "general", GENERAL_PER_SECOND, clock.clone()),
    );
    let mut waits = Vec::new();
    for _ in 0..3 {
        waits.push(l.slot("general").await.unwrap());
    }
    // `read` has no shared spacer here; its local floor (1/3 s) is inside the 2 s the box
    // already spent, so it adds nothing.
    for w in &waits {
        assert!(approx(*w, 2.0), "{waits:?}");
    }
    assert!(
        approx(clock.slept(), 6.0),
        "the local spacer double-charged the wait"
    );
    assert!(approx(l.waits("general"), 6.0));
}

#[tokio::test]
async fn test_the_interval_and_the_budget_are_what_the_script_is_told() {
    let clock = FakeClock::new();
    let script = StubScript::ok(1, "0");
    let sp = SharedSpacer::new(
        script.clone(),
        "baskfy:ratelimit:kite:historical",
        HISTORICAL_PER_SECOND,
        MAX_SHARED_WAIT_SECONDS,
        SHARED_RETRY_AFTER_SECONDS,
        clock,
    )
    .unwrap();
    sp.take().await;
    let (keys, args) = script.calls.lock().unwrap()[0].clone();
    assert_eq!(keys, ["baskfy:ratelimit:kite:historical"]);
    assert!(approx(
        args[0].parse::<f64>().unwrap(),
        1.0 / HISTORICAL_PER_SECOND
    ));
    assert!(approx(
        args[1].parse::<f64>().unwrap(),
        MAX_SHARED_WAIT_SECONDS
    ));
}

// --- review R8 (exec_guards): the limiter never panics and never hangs on Redis --------------

/// A Redis that never answers: redis-py's `socket_timeout=1.5` turns that into a failure, and
/// the call is spaced locally. Without the timeout every Kite read would hang behind it.
struct HungScript;

#[async_trait]
impl ScriptBackend for HungScript {
    async fn eval(
        &self,
        _script: &'static str,
        _keys: &[String],
        _args: &[String],
    ) -> Result<ScriptValue, ProviderError> {
        std::future::pending::<()>().await;
        Err(ProviderError::new(
            ErrorKind::ProviderUnavailable,
            "unreachable",
        ))
    }
}

#[tokio::test(start_paused = true)]
async fn a_hung_redis_times_out_and_the_call_is_spaced_locally() {
    let clock = FakeClock::new();
    let sp = SharedSpacer::new(
        Arc::new(HungScript),
        "k:quote",
        QUOTE_PER_SECOND,
        MAX_SHARED_WAIT_SECONDS,
        SHARED_RETRY_AFTER_SECONDS,
        clock.clone(),
    )
    .unwrap();
    let l = DeskLimits::new(
        clock.clone(),
        HashMap::from([("quote".to_owned(), Arc::new(sp))]),
    );
    let started = Instant::now();
    l.slot("quote").await.unwrap();
    assert!(started.elapsed() <= std::time::Duration::from_millis(1_600));
    assert_eq!(l.local_only("quote"), 1);
}

#[tokio::test(start_paused = true)]
async fn an_infinite_bucket_wait_does_not_panic() {
    // A zero-capacity bucket's refill wait is infinite; the Python sleeps it, Rust must not
    // panic in `Duration::from_secs_f64`.
    let b = Arc::new(Bucket::new(0, 60.0));
    let task = tokio::spawn({
        let b = b.clone();
        async move { b.take().await }
    });
    tokio::time::sleep(std::time::Duration::from_secs(3_600)).await;
    assert!(
        !task.is_finished(),
        "a bucket with no capacity never grants"
    );
    task.abort();
}
