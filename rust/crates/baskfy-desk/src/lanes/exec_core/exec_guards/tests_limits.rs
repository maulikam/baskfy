//! Ports `tests/test_ratelimit.py` (the order path's limiter, which failed in production on
//! 18 Aug 2026) and all of `tests/test_kite_limits.py`: the read limiter's caps, burst floors
//! and the shared clock's composition; the builder over the environment; the box's one limit
//! against a real Redis (the bed's, skipped where there is none, as Python skips); and the
//! source scans, which read the Rust client (`kite_client.rs`) and the analytics lane where
//! Python reads `app/kite_client.py` and `app/analytics/`.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::print_stderr
)]

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use baskfy_providers::ratelimit::{ScriptBackend, ScriptValue};
use baskfy_providers::{ErrorKind, ProviderError};
use tokio::time::Instant;

use super::super::kite_limits::{
    DeskLimits, FAMILY_FOR_CALL, GENERAL_PER_SECOND, HISTORICAL_PER_SECOND, LimitClock,
    MAX_SHARED_WAIT_SECONDS, QUOTE_PER_SECOND, RATE_FOR_FAMILY, READ_PER_SECOND, SHARED_KEY_PREFIX,
    SHARED_REBUILD_AFTER_SECONDS, SHARED_RETRY_AFTER_SECONDS, SharedSpacer, SharedSpacers,
    Spacer as ReadSpacer, TokioClock, claim, family_for_call,
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
    /// `clock.now += seconds` without a sleep.
    fn advance(&self, seconds: f64) {
        self.state.lock().unwrap().0 += seconds;
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
    l.slot_for_call("some_endpoint_kite_adds_in_2027")
        .await
        .unwrap();
    l.slot_for_call("some_endpoint_kite_adds_in_2027")
        .await
        .unwrap();
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
        shared(script.clone(), "quote", QUOTE_PER_SECOND, clock.clone()),
    );
    for _ in 0..5 {
        l.slot("quote").await.unwrap();
    }
    assert!(
        approx(clock.slept(), 4.0),
        "the local 1 req/s floor did not hold: {}",
        clock.slept()
    );
    assert_eq!(l.local_only("quote"), 5, "the degradation was not counted");
}

#[tokio::test]
async fn test_a_dead_redis_is_not_asked_again_on_every_read() {
    let clock = FakeClock::new();
    let script = StubScript::dead();
    let spacer = SharedSpacer::new(
        script.clone(),
        "k:general",
        GENERAL_PER_SECOND,
        MAX_SHARED_WAIT_SECONDS,
        30.0,
        clock.clone(),
    )
    .unwrap();
    for _ in 0..20 {
        assert_eq!(spacer.take().await, Ok(None));
    }
    assert_eq!(script.n(), 1, "it asked a dead Redis {} times", script.n());
    clock.advance(31.0);
    assert_eq!(spacer.take().await, Ok(None));
    assert_eq!(script.n(), 2, "it never tried Redis again");
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
    sp.take().await.unwrap();
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

// --- wave D2 (exec_guards): a malformed reply raises, as `_claim` does --------------------------

#[test]
fn the_refusal_names_the_reply_as_python_reprs_it() {
    let cases = [
        (ScriptValue::Bytes(b"ok".to_vec()), "b'ok'"),
        (ScriptValue::Array(vec![ScriptValue::Int(1)]), "[1]"),
        (ScriptValue::Nil, "None"),
    ];
    for (junk, shown) in cases {
        let e = claim(&junk).unwrap_err();
        assert_eq!(e.python_type, "ValueError");
        assert_eq!(
            e.message,
            format!("unexpected reply from the Kite limiter script: {shown}")
        );
    }
    let e = claim(&ScriptValue::Array(vec![
        ScriptValue::Int(1),
        ScriptValue::Bytes(b"soon".to_vec()),
    ]))
    .unwrap_err();
    assert_eq!(e.message, "could not convert string to float: 'soon'");
    // `float()` reads what Python's reads: underscores between digits, `inf`.
    assert_eq!(
        claim(&ScriptValue::Array(vec![
            ScriptValue::Bytes(b" 1 ".to_vec()),
            ScriptValue::Bytes(b"1_0.5".to_vec()),
        ])),
        Ok((true, 10.5))
    );
}

/// A script that answers a shape `SPACING_SCRIPT` never sends.
fn junk_script() -> Arc<StubScript> {
    Arc::new(StubScript {
        reply: Some(ScriptValue::Bytes(b"ok".to_vec())),
        calls: Mutex::new(Vec::new()),
    })
}

#[tokio::test]
async fn a_malformed_reply_raises_out_of_take_and_slot_without_a_back_off() {
    let clock = FakeClock::new();
    let script = junk_script();
    let sp = SharedSpacer::new(
        script.clone(),
        "k:quote",
        QUOTE_PER_SECOND,
        MAX_SHARED_WAIT_SECONDS,
        SHARED_RETRY_AFTER_SECONDS,
        clock.clone(),
    )
    .unwrap();
    let e = sp.take().await.unwrap_err();
    assert_eq!(e.python_type, "ValueError");
    // Not a Redis failure: Python sets no `_down_until`, so the next read asks again.
    assert!(sp.available());
    let _ = sp.take().await;
    assert_eq!(script.n(), 2);

    let l = DeskLimits::new(
        clock.clone(),
        shared(script.clone(), "quote", QUOTE_PER_SECOND, clock.clone()),
    );
    let e = l.slot("quote").await.unwrap_err();
    assert!(e.message.contains("Kite limiter"), "{}", e.message);
    assert_eq!(l.local_only("quote"), 0, "a raise is not a degradation");
    assert!(approx(l.waits("quote"), 0.0), "Python totals nothing");
    let e = l.slot_for_call("ltp").await.unwrap_err();
    assert_eq!(e.python_type, "ValueError");
}

#[tokio::test]
async fn a_caller_that_drops_the_error_still_cannot_send_an_unspaced_read() {
    // R8.10.1: the local spacer is taken before the error returns, so a consumer that ignores it
    // (the Kite client's `let _ = slot(...)`) is still held to 1 req/s.
    let clock = FakeClock::new();
    let l = DeskLimits::new(
        clock.clone(),
        shared(junk_script(), "quote", QUOTE_PER_SECOND, clock.clone()),
    );
    for _ in 0..5 {
        let _ = l.slot("quote").await;
    }
    assert!(approx(clock.slept(), 4.0), "{}", clock.slept());
}

// --- TestTheFamiliesAreTheSameOnBothSides / TestTheBuilderReadsTheEnvironment ---------------

#[test]
fn test_every_family_has_a_rate_and_a_spacer() {
    let (l, _) = limits();
    let fams: Vec<&str> = RATE_FOR_FAMILY.iter().map(|(f, _)| *f).collect();
    assert_eq!(fams, ["read", "quote", "historical", "general"]);
    for (family, rate) in RATE_FOR_FAMILY {
        let sp = l.spacer(family).unwrap();
        assert!(approx(sp.interval(), 1.0 / rate), "{family}");
    }
}

#[tokio::test]
async fn test_a_limiter_takes_the_processes_shared_spacers_by_default() {
    // Python: `DeskLimits()` takes `shared_spacers()`. Rust passes them explicitly, and the one
    // place a limiter is built for the client hands it the process's shared spacers.
    let clock = FakeClock::new();
    let l = DeskLimits::new(
        clock.clone(),
        shared(
            StubScript::ok(1, "0"),
            "quote",
            QUOTE_PER_SECOND,
            clock.clone(),
        ),
    );
    assert!(l.is_shared());
    assert!(!DeskLimits::new(clock, HashMap::new()).is_shared());
    let client = include_str!("../kite_client.rs");
    let built = &client[client.find("Arc::new(DeskLimits::new(").unwrap()..];
    let built = &built[..built.find("))").unwrap()];
    assert!(
        built.contains("self.shared.get().await"),
        "the client's limiter is not the box's: {built}"
    );
}

#[tokio::test]
async fn test_no_redis_url_is_per_process_and_says_so() {
    let s = SharedSpacers::new(true, "", FakeClock::new());
    assert!(s.get().await.is_empty());
    assert_eq!(s.mode().await, "per-process");
}

#[tokio::test]
async fn test_the_switch_turns_it_off_with_a_url_present() {
    // A refusing listener stands in for the URL's server: with the switch off it is never
    // contacted at all.
    let (url, attempts) = counting_listener().await;
    let s = SharedSpacers::new(false, &url, FakeClock::new());
    assert!(s.get().await.is_empty());
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    assert_eq!(attempts.load(std::sync::atomic::Ordering::SeqCst), 0);
}

#[tokio::test]
async fn test_an_unreachable_redis_is_per_process_not_an_exception() {
    // Port 1 is reserved and nothing listens on it; the connect fails inside the timeout.
    let s = SharedSpacers::new(true, "redis://127.0.0.1:1/0", FakeClock::new());
    assert!(s.get().await.is_empty());
    assert_eq!(s.mode().await, "per-process");
}

/// A loopback listener that accepts and drops every connection, counting them: a Redis that
/// is "there" but never answers a handshake.
async fn counting_listener() -> (String, Arc<std::sync::atomic::AtomicUsize>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let attempts = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let seen = attempts.clone();
    tokio::spawn(async move {
        while let Ok((socket, _)) = listener.accept().await {
            seen.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            drop(socket);
        }
    });
    (format!("redis://127.0.0.1:{port}/0"), attempts)
}

#[tokio::test]
async fn test_a_failed_connection_is_retried_rather_than_given_up_on() {
    use std::sync::atomic::Ordering;
    let (url, attempts) = counting_listener().await;
    let clock = FakeClock::new();
    let s = SharedSpacers::new(true, &url, clock.clone());
    assert!(s.get().await.is_empty());
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    let first = attempts.load(Ordering::SeqCst);
    assert!(first >= 1, "the first build never tried Redis");
    assert!(s.get().await.is_empty());
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    assert_eq!(
        attempts.load(Ordering::SeqCst),
        first,
        "it rebuilt on every call, paying the connect timeout each time"
    );
    // The minute, brought forward.
    clock.advance(SHARED_REBUILD_AFTER_SECONDS + 0.01);
    assert!(s.get().await.is_empty());
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    assert!(
        attempts.load(Ordering::SeqCst) > first,
        "it never looked at Redis again"
    );
}

// --- TestTheBoxHoldsOneLimit: against a REAL Redis --------------------------------------------

/// The bed's Redis (index 15, this lane's), or `BASKFY_RS_DESK_TEST_REDIS_URL`. `None` (and the
/// test skips, as Python's `_redis_or_skip` does) where nothing answers.
async fn redis_or_skip() -> Option<Arc<dyn ScriptBackend>> {
    let url = std::env::var("BASKFY_RS_DESK_TEST_REDIS_URL")
        .unwrap_or_else(|_| "redis://127.0.0.1:56379/15".to_owned());
    let secret = baskfy_providers::secret::Secret::new(url);
    let connect = baskfy_providers::ratelimit::RedisScriptBackend::connect(&secret);
    match tokio::time::timeout(std::time::Duration::from_millis(1_500), connect).await {
        Ok(Ok(backend)) => Some(Arc::new(backend)),
        _ => {
            eprintln!("no Redis for the shared-limit tests; skipped");
            None
        }
    }
}

const DEL: &str = "return redis.call('DEL', KEYS[1])";
const GET: &str = "return redis.call('GET', KEYS[1])";
const PTTL: &str = "return redis.call('PTTL', KEYS[1])";

struct BoxKey {
    backend: Arc<dyn ScriptBackend>,
    name: String,
}

impl BoxKey {
    async fn new(test: &str) -> Option<Self> {
        let backend = redis_or_skip().await?;
        let name = format!("baskfy:test:ratelimit:kite:{test}");
        backend
            .eval(DEL, std::slice::from_ref(&name), &[])
            .await
            .unwrap();
        Some(Self { backend, name })
    }
    fn spacer(&self, max_wait: f64) -> SharedSpacer {
        SharedSpacer::new(
            self.backend.clone(),
            &self.name,
            BOX_RATE,
            max_wait,
            SHARED_RETRY_AFTER_SECONDS,
            Arc::new(TokioClock::default()),
        )
        .unwrap()
    }
    async fn done(self) {
        let _ = self
            .backend
            .eval(DEL, std::slice::from_ref(&self.name), &[])
            .await;
    }
}

/// Not Kite's rate: two limiters queueing behind one clock is as true at 20/s as at 1/s.
const BOX_RATE: f64 = 20.0;
const BOX_INTERVAL: f64 = 1.0 / BOX_RATE;

#[tokio::test]
async fn test_two_processes_queue_behind_one_clock() {
    let Some(k) = BoxKey::new("test_two_processes_queue_behind_one_clock").await else {
        return;
    };
    // Two connections: two containers on one box.
    let Some(second) = redis_or_skip().await else {
        return;
    };
    let web = k.spacer(MAX_SHARED_WAIT_SECONDS);
    let monitor = SharedSpacer::new(
        second,
        &k.name,
        BOX_RATE,
        MAX_SHARED_WAIT_SECONDS,
        SHARED_RETRY_AFTER_SECONDS,
        Arc::new(TokioClock::default()),
    )
    .unwrap();
    let started = std::time::Instant::now();
    for spacer in [&web, &monitor, &web, &monitor, &web, &monitor] {
        assert!(spacer.take().await.unwrap().is_some());
    }
    let elapsed = started.elapsed().as_secs_f64();
    k.done().await;
    assert!(
        elapsed >= 5.0 * BOX_INTERVAL,
        "six calls from two processes took {elapsed:.3}s"
    );
}

#[tokio::test]
async fn test_the_first_call_against_a_cold_key_does_not_wait() {
    let Some(k) = BoxKey::new("test_the_first_call_against_a_cold_key_does_not_wait").await else {
        return;
    };
    let waited = k.spacer(MAX_SHARED_WAIT_SECONDS).take().await.unwrap();
    k.done().await;
    assert!(waited.is_some_and(|w| w.abs() <= 0.01), "{waited:?}");
}

#[tokio::test]
async fn test_a_caller_that_will_not_wait_does_not_spend_the_slot() {
    let Some(k) = BoxKey::new("test_a_caller_that_will_not_wait_does_not_spend_the_slot").await
    else {
        return;
    };
    let patient = k.spacer(MAX_SHARED_WAIT_SECONDS);
    let impatient = k.spacer(0.0);
    patient.take().await.unwrap();
    assert_eq!(
        impatient.take().await,
        Ok(None),
        "it waited past a budget of zero"
    );
    assert!(patient.take().await.unwrap().is_some());
    let raw = k
        .backend
        .eval(GET, std::slice::from_ref(&k.name), &[])
        .await
        .unwrap();
    k.done().await;
    let ScriptValue::Bytes(b) = raw else {
        panic!("no departure stored: {raw:?}")
    };
    let next: f64 = String::from_utf8(b).unwrap().parse().unwrap();
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64();
    assert!(next <= now + 2.0 * BOX_INTERVAL + 0.5, "{next} vs {now}");
}

#[tokio::test]
async fn test_the_key_expires_so_an_idle_family_costs_nothing() {
    let Some(k) = BoxKey::new("test_the_key_expires_so_an_idle_family_costs_nothing").await else {
        return;
    };
    k.spacer(MAX_SHARED_WAIT_SECONDS).take().await.unwrap();
    let ttl = k
        .backend
        .eval(PTTL, std::slice::from_ref(&k.name), &[])
        .await
        .unwrap();
    k.done().await;
    let ScriptValue::Int(ttl) = ttl else {
        panic!("{ttl:?}")
    };
    #[allow(clippy::cast_possible_truncation)]
    let cap = ((BOX_INTERVAL + 1.0) * 1000.0) as i64 + 1;
    assert!(0 < ttl && ttl <= cap, "pttl={ttl}");
}

// --- TestEveryReadInTheClientTakesASlot (over `kite_client.rs`) -------------------------------

/// Attributes of `kc` that are session plumbing, not requests.
const NOT_A_REQUEST: [&str; 4] = [
    "set_access_token",
    "generate_session",
    "login_url",
    "access_token",
];
/// The order path's own calls, limited inside the gateway.
const ORDER_PATH: [&str; 4] = ["place_order", "place_gtt", "delete_gtt", "modify_gtt"];

/// Every function in `src` that touches `self.kc.<attr>`: (name, body, attrs).
fn methods_with_kc_calls(src: &str) -> Vec<(String, String, Vec<String>)> {
    let fn_start =
        regex::Regex::new(r"^(\s*)(?:pub(?:\([^)]*\))? )?(?:const )?(?:async )?fn (\w+)").unwrap();
    let kc = regex::Regex::new(r"self\.kc\s*\.\s*(\w+)").unwrap();
    let lines: Vec<&str> = src.lines().collect();
    let mut out = Vec::new();
    let mut i = 0;
    while i < lines.len() {
        let Some(c) = fn_start.captures(lines[i]) else {
            i += 1;
            continue;
        };
        let indent = c[1].to_owned();
        let name = c[2].to_owned();
        let close = format!("{indent}}}");
        let mut j = i + 1;
        while j < lines.len() && lines[j] != close {
            j += 1;
        }
        let body = lines[i..j.min(lines.len())].join("\n");
        let attrs: Vec<String> = kc.captures_iter(&body).map(|m| m[1].to_owned()).collect();
        if !attrs.is_empty() {
            out.push((name, body, attrs));
        }
        i = j + 1;
    }
    out
}

fn takes_a_slot(body: &str) -> bool {
    body.contains("self.slot(") || body.contains(".slot_for_call(")
}

#[test]
fn test_the_scan_sees_the_file_it_thinks_it_sees() {
    let found = methods_with_kc_calls(include_str!("../kite_client.rs"));
    assert!(
        found.len() >= 10,
        "the scan found only {:?}",
        found.iter().map(|f| &f.0).collect::<Vec<_>>()
    );
}

#[test]
fn test_every_request_carrying_method_takes_a_slot() {
    let mut offenders = Vec::new();
    for (name, body, attrs) in methods_with_kc_calls(include_str!("../kite_client.rs")) {
        let requests: Vec<&String> = attrs
            .iter()
            .filter(|a| !NOT_A_REQUEST.contains(&a.as_str()) && !ORDER_PATH.contains(&a.as_str()))
            .collect();
        if !requests.is_empty() && !takes_a_slot(&body) {
            offenders.push(format!("{name} calls {requests:?} with no slot"));
        }
    }
    assert!(offenders.is_empty(), "{}", offenders.join("; "));
}

#[test]
fn test_the_scan_fails_when_a_slot_is_removed() {
    let src = "impl K {\n    fn read(&self) {\n        self.kc.holdings()\n    }\n}\n";
    let found = methods_with_kc_calls(src);
    assert_eq!(found.len(), 1);
    assert!(!takes_a_slot(&found[0].1));
}

// --- TestAnalyticsGoesThroughTheClient -------------------------------------------------------

/// `app/analytics/` modules that may still hold the raw client, with the reason. Empty.
const ANALYTICS_RAW_ALLOWED: [(&str, &str); 1] = [(
    "performance_view.rs",
    "ports main._holdings_block, whose k.kc.holdings()/get_gtts() are raw in Python too; state.kite is limited_reads",
)];

fn analytics_sources() -> Vec<(std::path::PathBuf, String)> {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lanes/analytics");
    let mut out = Vec::new();
    let mut stack = vec![root.clone(), root.with_extension("rs")];
    while let Some(p) = stack.pop() {
        if p.is_dir() {
            for e in std::fs::read_dir(&p).unwrap() {
                stack.push(e.unwrap().path());
            }
        } else if p.extension().is_some_and(|x| x == "rs") {
            out.push((p.clone(), std::fs::read_to_string(&p).unwrap()));
        }
    }
    out.sort();
    out
}

#[test]
fn test_no_analytics_module_calls_the_raw_client() {
    // Python's raw handle is `kite.kc`; the Rust lane's would be `.kc.` or the app state's
    // unlimited `state.kite` reads.
    let raw = regex::Regex::new(r"\.kc\s*\.|state\s*\.\s*kite\s*\.").unwrap();
    let sources = analytics_sources();
    assert!(
        sources.len() >= 5,
        "the scan found only {} files",
        sources.len()
    );
    let mut offenders = Vec::new();
    for (path, text) in sources {
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        if ANALYTICS_RAW_ALLOWED.iter().any(|(n, _)| *n == name) {
            continue;
        }
        for (n, line) in text.lines().enumerate() {
            if raw.is_match(line) {
                offenders.push(format!("{name}:{} {}", n + 1, line.trim()));
            }
        }
    }
    assert!(offenders.is_empty(), "{offenders:?}");
}

#[test]
fn test_the_allow_list_names_nothing_that_has_gone_away() {
    let names: Vec<String> = analytics_sources()
        .iter()
        .map(|(p, _)| p.file_name().unwrap().to_string_lossy().into_owned())
        .collect();
    for (name, _) in ANALYTICS_RAW_ALLOWED {
        assert!(
            names.iter().any(|n| n == name),
            "{name} is allow-listed but gone"
        );
    }
}

#[test]
fn test_the_client_offers_the_wrapper_the_call_sites_need() {
    use super::super::kite_client::DeskKite;
    // Each a method of the client (the names `test_kite_limits.py` lists); a missing one does
    // not compile.
    let _ = (
        DeskKite::instruments,
        DeskKite::historical,
        DeskKite::margins,
        DeskKite::orders,
        DeskKite::order_history,
        DeskKite::get_gtts,
        DeskKite::quote_raw,
    );
    let _ = (
        DeskKite::ltp,
        DeskKite::quotes,
        DeskKite::holdings,
        DeskKite::trades,
    );
}

// --- TestTheWebsocketIsTheQuoteSource --------------------------------------------------------

#[test]
fn test_a_subscription_longer_than_one_connection_is_refused() {
    use crate::lanes::session::ticker::{MAX_INSTRUMENTS_PER_CONNECTION, TickBus, start_ticker};
    let tokens: Vec<i64> = (0..=i64::try_from(MAX_INSTRUMENTS_PER_CONNECTION).unwrap()).collect();
    let e = start_ticker(&tokens, &TickBus::new()).unwrap_err();
    assert!(e.contains("one websocket"), "{e}");
}

struct CountingQuotes {
    calls: Mutex<Vec<Vec<String>>>,
}

#[async_trait]
impl crate::lanes::swing::swing_monitor::runner::QuoteReader for CountingQuotes {
    async fn quote_raw(&self, keys: &[String]) -> Result<crate::wire::PyValue, String> {
        use crate::wire::{PyDict, PyValue};
        self.calls.lock().unwrap().push(keys.to_vec());
        let mut out = PyDict::default();
        for k in keys {
            let mut row = PyDict::default();
            row.insert("last_price", PyValue::Float(100.0));
            row.insert("ohlc", PyValue::Map(PyDict::default()));
            row.insert("volume", PyValue::Int(0));
            out.insert(k, PyValue::Map(row));
        }
        Ok(PyValue::Map(out))
    }
}

#[tokio::test]
async fn test_the_monitors_fallback_asks_kite_at_most_once_per_window() {
    use crate::lanes::swing::swing_monitor::runner::QuoteFallback;
    let source = CountingQuotes {
        calls: Mutex::new(Vec::new()),
    };
    let mut fallback = QuoteFallback::new(
        std::collections::BTreeMap::from([(1001, "ALPHAFLAG".to_owned())]),
        None,
    );
    let at = chrono::NaiveDate::from_ymd_opt(2026, 9, 4)
        .unwrap()
        .and_hms_opt(9, 30, 0)
        .unwrap();
    let mut now = 1000.0;
    for _ in 0..100 {
        let _ = fallback.poll(&source, at, now).await;
    }
    assert_eq!(
        fallback.calls, 1,
        "{} Kite calls inside one window",
        fallback.calls
    );
    assert_eq!(source.calls.lock().unwrap().len(), 1);
    now += 5.01;
    let _ = fallback.poll(&source, at, now).await;
    assert_eq!(
        fallback.calls, 2,
        "the window expired and no second call was made"
    );
}

#[test]
fn test_the_fallback_reaches_kite_through_the_limited_wrapper() {
    // `quote_raw` takes the 1 req/s slot; a raw `kc.quote` would not.
    let runner = include_str!("../../swing/swing_monitor/runner.rs");
    let start = runner.find("impl QuoteFallback {").unwrap();
    let body = &runner[start..];
    let body = &body[..body.find("\n}\n").unwrap()];
    assert!(body.contains("quote_raw("));
    assert!(!body.contains("kc.quote("));
    let client = include_str!("../kite_client.rs");
    let wrapper = &client[client.find("pub async fn quote_raw(").unwrap()..];
    let wrapper = &wrapper[..wrapper.find("\n    }\n").unwrap()];
    assert!(wrapper.contains("self.slot(\"quote\")"), "{wrapper}");
}
