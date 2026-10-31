//! `celery worker -Q ...`: consume protocol-2 messages from kombu's Redis lists and run them.

use std::collections::HashSet;
use std::rc::Rc;
use std::sync::{Arc, Mutex};
use std::time::{Duration as StdDuration, SystemTime, UNIX_EPOCH};

use baskfy_api::Settings;
use baskfy_db::{OrderedJson, WriteDb};
use chrono::{DateTime, Duration, FixedOffset, Utc};
use redis::aio::ConnectionManager;
use tokio::sync::{Semaphore, mpsc, oneshot, watch};

use super::celery::{self, Incoming};
use super::log;
use crate::pyjson::{object, s};
use crate::settings::WorkerSettings;
use crate::tasks::{self, TaskContext, TaskSpec, context::TaskClock};

/// Bound tasks whose body calls `self.retry(exc=..., countdown=..., max_retries=...)`:
/// `(task, exception class, countdown seconds, max_retries)`.
const BOUND_RETRIES: &[(&str, &str, i64, i64)] =
    &[("baskfy.swing.scan_now", "ScanNotRunnable", 2, 5)];

/// kombu's `brpop_timeout`, seconds.
const BRPOP_TIMEOUT_SECS: u64 = 1;

/// What a worker process serves.
#[derive(Debug, Clone)]
pub struct ServeConfig {
    /// `baskfy_api.Settings` (the database).
    pub settings: Settings,
    /// `WorkerSettings` (retries, redeliveries).
    pub worker: WorkerSettings,
    /// `-Q`.
    pub queues: Vec<String>,
    /// `--concurrency` (prefetch multiplier 1).
    pub concurrency: usize,
    /// `broker_url`.
    pub broker_url: String,
    /// `result_backend`.
    pub backend_url: String,
}

impl ServeConfig {
    /// The worker's configuration for `queues`, from Python's settings.
    #[must_use]
    pub fn new(
        settings: Settings,
        worker: WorkerSettings,
        queues: Vec<String>,
        concurrency: usize,
    ) -> Self {
        let broker_url = worker.broker_url().to_owned();
        let backend_url = if worker.celery_result_backend.is_empty() {
            worker.redis_url.clone()
        } else {
            worker.celery_result_backend.clone()
        };
        Self {
            settings,
            worker,
            queues,
            concurrency: concurrency.max(1),
            broker_url,
            backend_url,
        }
    }
}

#[derive(Debug)]
enum Outcome {
    Returned(OrderedJson),
    Raised {
        type_name: String,
        message: String,
        autoretry: bool,
    },
    TimedOut(OrderedJson),
    Lost(String),
}

struct Job {
    spec: &'static TaskSpec,
    task_id: String,
    args: Vec<OrderedJson>,
    kwargs: Vec<(String, OrderedJson)>,
    limit: Option<f64>,
    reply: oneshot::Sender<Outcome>,
}

fn limit_json(limit: f64) -> OrderedJson {
    if limit.fract() == 0.0 && limit.abs() < 9.0e15 {
        #[allow(clippy::cast_possible_truncation)]
        OrderedJson::Int(limit as i64)
    } else {
        OrderedJson::Float(limit)
    }
}

async fn execute(ctx: Rc<TaskContext>, job: Job) {
    let Job {
        spec,
        task_id,
        args,
        kwargs,
        limit,
        reply,
    } = job;
    let body = {
        let ctx = Rc::clone(&ctx);
        tokio::task::spawn_local(async move {
            let result = super::with_task_id(task_id, tasks::run_task(&ctx, spec.name, args, kwargs)).await;
            match result {
                Ok(v) => Outcome::Returned(v),
                Err(e) => Outcome::Raised {
                    type_name: e.python_type(),
                    message: e.to_string(),
                    autoretry: spec.retries_on(&e),
                },
            }
        })
    };
    let joined = match limit.filter(|l| *l > 0.0) {
        Some(secs) => {
            let abort = body.abort_handle();
            match tokio::time::timeout(StdDuration::from_secs_f64(secs), body).await {
                Ok(j) => j,
                Err(_) => {
                    abort.abort();
                    let _ = reply.send(Outcome::TimedOut(limit_json(secs)));
                    return;
                }
            }
        }
        None => body.await,
    };
    let outcome = joined.unwrap_or_else(|e| Outcome::Lost(e.to_string()));
    let _ = reply.send(outcome);
}

fn slot(settings: Settings, worker: WorkerSettings, jobs: Arc<tokio::sync::Mutex<mpsc::Receiver<Job>>>) {
    let rt = match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            log("CRITICAL", &format!("pool process could not start: {e}"));
            return;
        }
    };
    let local = tokio::task::LocalSet::new();
    local.block_on(&rt, async move {
        let db = match WriteDb::connect_lazy(&settings.db_settings()) {
            Ok(db) => db,
            Err(e) => {
                log("CRITICAL", &format!("pool process database: {e}"));
                return;
            }
        };
        let ctx = Rc::new(TaskContext::new(
            db,
            settings,
            worker,
            TaskClock::system(),
            None,
            None,
        ));
        loop {
            let next = jobs.lock().await.recv().await;
            let Some(job) = next else { break };
            execute(Rc::clone(&ctx), job).await;
        }
        ctx.db.close().await;
    });
}

fn epoch_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}

fn now_ist() -> DateTime<FixedOffset> {
    Utc::now().with_timezone(&celery::ist())
}

const RESTORE_SCRIPT: &str = r"
local p = redis.call('HGET', KEYS[1], ARGV[1])
if not p then
  redis.call('ZREM', KEYS[2], ARGV[1])
  return 0
end
if p ~= ARGV[2] then return -1 end
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[1], ARGV[1])
for i = 5, #ARGV do
  if ARGV[3] == '1' then redis.call('LPUSH', ARGV[i], ARGV[4]) else redis.call('RPUSH', ARGV[i], ARGV[4]) end
end
return 1
";

const RELEASE_SCRIPT: &str = r"
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
";

/// The broker side of kombu's ack emulation (`QoS`).
#[derive(Clone)]
struct Broker {
    conn: ConnectionManager,
}

impl Broker {
    async fn append(&mut self, msg: &Incoming, raw: &str) -> Result<(), String> {
        let stored = format!(
            "[{raw}, {}, {}]",
            crate::pyjson::dumps(&s(&msg.exchange))?,
            crate::pyjson::dumps(&s(&msg.routing_key))?
        );
        redis::pipe()
            .cmd("ZADD")
            .arg(celery::UNACKED_INDEX)
            .arg(epoch_now())
            .arg(&msg.delivery_tag)
            .ignore()
            .cmd("HSET")
            .arg(celery::UNACKED)
            .arg(&msg.delivery_tag)
            .arg(stored)
            .ignore()
            .query_async::<()>(&mut self.conn)
            .await
            .map_err(|e| e.to_string())
    }

    async fn ack(&mut self, tag: &str) {
        let result = redis::pipe()
            .cmd("ZREM")
            .arg(celery::UNACKED_INDEX)
            .arg(tag)
            .ignore()
            .cmd("HDEL")
            .arg(celery::UNACKED)
            .arg(tag)
            .ignore()
            .query_async::<()>(&mut self.conn)
            .await;
        if let Err(e) = result {
            log("ERROR", &format!("could not ack {tag}: {e}"));
        }
    }

    async fn lookup(&mut self, exchange: &str, routing_key: &str) -> Vec<String> {
        if exchange.is_empty() {
            return vec![routing_key.to_owned()];
        }
        let members: Vec<String> = redis::cmd("SMEMBERS")
            .arg(format!("_kombu.binding.{exchange}"))
            .query_async(&mut self.conn)
            .await
            .unwrap_or_default();
        let queues: Vec<String> = members
            .iter()
            .filter_map(|m| {
                let parts: Vec<&str> = m.split(celery::SEP).collect();
                match parts.as_slice() {
                    [rk, _, queue] if *rk == routing_key => Some((*queue).to_owned()),
                    _ => None,
                }
            })
            .collect();
        if queues.is_empty() {
            vec![routing_key.to_owned()]
        } else {
            queues
        }
    }

    async fn restore_by_tag(&mut self, tag: &str, leftmost: bool) {
        let stored: Option<String> = match redis::cmd("HGET")
            .arg(celery::UNACKED)
            .arg(tag)
            .query_async(&mut self.conn)
            .await
        {
            Ok(v) => v,
            Err(e) => {
                log("ERROR", &format!("could not restore {tag}: {e}"));
                return;
            }
        };
        let Some(stored) = stored else {
            self.ack(tag).await;
            return;
        };
        let parsed = OrderedJson::parse(&stored).ok();
        let Some(OrderedJson::Array(parts)) = parsed else {
            log("CRITICAL", &format!("Could not restore message: {stored}"));
            return;
        };
        let [message, exchange, routing_key] = parts.as_slice() else {
            log("CRITICAL", &format!("Could not restore message: {stored}"));
            return;
        };
        let text = |v: &OrderedJson| match v {
            OrderedJson::Str(t) => t.clone(),
            _ => String::new(),
        };
        let priority = message
            .get("properties")
            .and_then(|p| p.get("priority"))
            .and_then(|p| match p {
                OrderedJson::Int(n) => Some((*n).clamp(0, 9)),
                _ => None,
            })
            .unwrap_or(0);
        let payload = match crate::pyjson::dumps(&celery::mark_redelivered(message.clone())) {
            Ok(p) => p,
            Err(e) => {
                log("CRITICAL", &format!("Could not restore message: {e}"));
                return;
            }
        };
        let queues = self.lookup(&text(exchange), &text(routing_key)).await;
        let script = redis::Script::new(RESTORE_SCRIPT);
        let mut call = script.prepare_invoke();
        call.key(celery::UNACKED)
            .key(celery::UNACKED_INDEX)
            .arg(tag)
            .arg(&stored)
            .arg(if leftmost { "1" } else { "0" })
            .arg(payload);
        for q in &queues {
            call.arg(celery::queue_key(q, priority));
        }
        let result: Result<i64, _> = call.invoke_async(&mut self.conn).await;
        if let Err(e) = result {
            log("CRITICAL", &format!("Could not restore message {tag}: {e}"));
        }
    }

    async fn restore_visible(&mut self) {
        let token = match crate::tasks::swing_live::producer::uuid4() {
            Ok(t) => t,
            Err(_) => return,
        };
        let held: Option<String> = redis::cmd("SET")
            .arg(celery::UNACKED_MUTEX)
            .arg(&token)
            .arg("NX")
            .arg("PX")
            .arg(celery::UNACKED_MUTEX_EXPIRE_SECS * 1000)
            .query_async(&mut self.conn)
            .await
            .unwrap_or(None);
        if held.is_none() {
            return;
        }
        let ceil = epoch_now() - celery::VISIBILITY_TIMEOUT_SECS;
        let tags: Vec<String> = redis::cmd("ZREVRANGEBYSCORE")
            .arg(celery::UNACKED_INDEX)
            .arg(ceil)
            .arg(0)
            .arg("LIMIT")
            .arg(0)
            .arg(10)
            .query_async(&mut self.conn)
            .await
            .unwrap_or_default();
        for tag in tags {
            log("WARNING", &format!("Restoring unacknowledged message {tag}"));
            self.restore_by_tag(&tag, false).await;
        }
        let release = redis::Script::new(RELEASE_SCRIPT);
        let mut call = release.prepare_invoke();
        call.key(celery::UNACKED_MUTEX).arg(&token);
        let _: Result<i64, _> = call.invoke_async(&mut self.conn).await;
    }

    async fn publish(&mut self, message: &celery::Outgoing) -> Result<(), String> {
        super::beat_daemon::publish(&mut self.conn, message).await
    }
}

/// The result backend (`RedisBackend`).
#[derive(Clone)]
struct Backend {
    conn: ConnectionManager,
}

impl Backend {
    async fn store(
        &mut self,
        task_id: &str,
        status: &str,
        result: OrderedJson,
        traceback: Option<&str>,
        done: bool,
    ) {
        let key = celery::meta_key(task_id);
        let current: Option<String> = redis::cmd("GET")
            .arg(&key)
            .query_async(&mut self.conn)
            .await
            .unwrap_or(None);
        if current
            .and_then(|c| OrderedJson::parse(&c).ok())
            .is_some_and(|m| m.get("status") == Some(&s("SUCCESS")))
        {
            return;
        }
        let value = match celery::meta(
            task_id,
            status,
            result,
            traceback,
            OrderedJson::Array(Vec::new()),
            done.then(now_ist),
        ) {
            Ok(v) => v,
            Err(e) => {
                log("ERROR", &format!("could not encode the result of {task_id}: {e}"));
                return;
            }
        };
        let written = redis::pipe()
            .cmd("SETEX")
            .arg(&key)
            .arg(celery::RESULT_EXPIRES_SECS)
            .arg(&value)
            .ignore()
            .cmd("PUBLISH")
            .arg(&key)
            .arg(&value)
            .ignore()
            .query_async::<()>(&mut self.conn)
            .await;
        if let Err(e) = written {
            log("ERROR", &format!("could not store the result of {task_id}: {e}"));
        }
    }
}

struct Shared {
    broker: Broker,
    backend: Backend,
    permits: Arc<Semaphore>,
    jobs: mpsc::Sender<Job>,
    worker: WorkerSettings,
    nodename: String,
    warm: watch::Receiver<bool>,
    waiting: Mutex<HashSet<String>>,
}

fn traceback(type_name: &str, message: &str) -> String {
    format!("Traceback (most recent call last):\n{type_name}: {message}\n")
}

fn exc_args(message: &str) -> Vec<OrderedJson> {
    vec![s(message)]
}

/// Wait until `eta`; `false` when a warm shutdown came first.
async fn wait_until(eta: DateTime<FixedOffset>, warm: &mut watch::Receiver<bool>) -> bool {
    let wait = (eta.with_timezone(&Utc) - Utc::now())
        .to_std()
        .unwrap_or_default();
    let slept = tokio::select! {
        () = tokio::time::sleep(wait) => true,
        _ = warm.wait_for(|v| *v) => false,
    };
    slept && !*warm.borrow()
}

fn retry_countdown(spec: &TaskSpec, retries: i64) -> i64 {
    if spec.retry_backoff {
        let exp = u32::try_from(retries.clamp(0, 30)).unwrap_or(30);
        let countdown = 2_i64
            .saturating_pow(exp)
            .min(tasks::CELERY_RETRY_BACKOFF_MAX_SECONDS);
        rand::random_range(0..=countdown).max(0)
    } else {
        tasks::CELERY_DEFAULT_RETRY_DELAY_SECONDS
    }
}

async fn process(shared: Arc<Shared>, msg: Incoming, permit: tokio::sync::OwnedSemaphorePermit) {
    let mut broker = shared.broker.clone();
    let mut backend = shared.backend.clone();
    let Some(spec) = tasks::find(&msg.task).filter(|t| t.is_ported()) else {
        log(
            "ERROR",
            &format!(
                "Received unregistered task of type '{}'.\nThe message has been ignored and discarded.",
                msg.task
            ),
        );
        broker.ack(&msg.delivery_tag).await;
        backend
            .store(
                &msg.id,
                "FAILURE",
                celery::exc_result("NotRegistered", vec![s(&msg.task)]),
                None,
                true,
            )
            .await;
        return;
    };
    log(
        "INFO",
        &format!("Task {}[{}] received", msg.task, msg.id),
    );
    if msg.redelivered {
        let count = msg.redeliveries + 1;
        if count > shared.worker.task_max_redeliveries {
            log(
                "ERROR",
                &format!(
                    "Signal handler _bound_redelivery raised: RuntimeError('task {} exceeded max_redeliveries={}; acking to stop the OOM requeue loop')",
                    msg.id, shared.worker.task_max_redeliveries
                ),
            );
        }
    }
    let mut permit = Some(permit);
    if let Some(eta) = msg.eta.filter(|e| *e > now_ist()) {
        drop(permit.take());
        lock(&shared.waiting).insert(msg.delivery_tag.clone());
        let mut warm = shared.warm.clone();
        let ready = wait_until(eta, &mut warm).await;
        let acquired = if ready {
            tokio::select! {
                p = Arc::clone(&shared.permits).acquire_owned() => p.ok(),
                _ = warm.wait_for(|v| *v) => None,
            }
        } else {
            None
        };
        lock(&shared.waiting).remove(&msg.delivery_tag);
        let Some(p) = acquired else {
            broker.restore_by_tag(&msg.delivery_tag, false).await;
            return;
        };
        permit = Some(p);
    }
    if msg.expires.is_some_and(|x| now_ist() >= x) {
        log("INFO", &format!("Discarding revoked task: {}[{}]", msg.task, msg.id));
        if !msg.ignore_result {
            backend
                .store(
                    &msg.id,
                    "REVOKED",
                    celery::exc_result("TaskRevokedError", vec![s("expired")]),
                    None,
                    true,
                )
                .await;
        }
        broker.ack(&msg.delivery_tag).await;
        return;
    }
    if !spec.acks_late {
        broker.ack(&msg.delivery_tag).await;
    }
    if !msg.ignore_result {
        backend
            .store(
                &msg.id,
                "STARTED",
                object([
                    ("pid", OrderedJson::Int(i64::from(std::process::id()))),
                    ("hostname", s(&shared.nodename)),
                ]),
                None,
                false,
            )
            .await;
    }
    #[allow(clippy::cast_precision_loss)]
    let limit = msg
        .hard_time_limit()
        .or_else(|| spec.time_limit.map(|t| t as f64));
    let (reply, outcome) = oneshot::channel();
    let job = Job {
        spec,
        task_id: msg.id.clone(),
        args: msg.args.clone(),
        kwargs: msg.kwargs.clone(),
        limit,
        reply,
    };
    let outcome = if shared.jobs.send(job).await.is_ok() {
        outcome
            .await
            .unwrap_or_else(|_| Outcome::Lost("the pool process exited".to_owned()))
    } else {
        Outcome::Lost("the pool is closed".to_owned())
    };
    drop(permit);
    finish(&shared, &mut broker, &mut backend, spec, &msg, outcome).await;
}

fn lock(m: &Mutex<HashSet<String>>) -> std::sync::MutexGuard<'_, HashSet<String>> {
    m.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
}

async fn finish(
    shared: &Shared,
    broker: &mut Broker,
    backend: &mut Backend,
    spec: &TaskSpec,
    msg: &Incoming,
    outcome: Outcome,
) {
    let store = !msg.ignore_result;
    match outcome {
        Outcome::Returned(value) => {
            log("INFO", &format!("Task {}[{}] succeeded", msg.task, msg.id));
            if store {
                backend.store(&msg.id, "SUCCESS", value, None, true).await;
            }
        }
        Outcome::Raised {
            type_name,
            message,
            autoretry,
        } => {
            let bound = BOUND_RETRIES
                .iter()
                .find(|(task, class, _, _)| *task == spec.name && *class == type_name);
            let tb = traceback(&type_name, &message);
            let retry = match (bound, autoretry) {
                (Some((_, _, countdown, max)), _) => Some((*countdown, *max)),
                (None, true) => Some((
                    retry_countdown(spec, msg.retries),
                    spec.max_retries(&shared.worker),
                )),
                (None, false) => None,
            };
            let mut retried = false;
            if let Some((countdown, max_retries)) = retry
                && msg.retries < max_retries
            {
                let eta = now_ist() + Duration::seconds(countdown);
                match msg.retry_message(eta) {
                    Ok(next) => match broker.publish(&next).await {
                        Ok(()) => {
                            log(
                                "INFO",
                                &format!(
                                    "Task {}[{}] retry: Retry in {countdown}s: {type_name}('{message}')",
                                    msg.task, msg.id
                                ),
                            );
                            if store {
                                backend
                                    .store(
                                        &msg.id,
                                        "RETRY",
                                        celery::exc_result(&type_name, exc_args(&message)),
                                        Some(&tb),
                                        false,
                                    )
                                    .await;
                            }
                            retried = true;
                        }
                        Err(e) => log("ERROR", &format!("retry of {} not published: {e}", msg.id)),
                    },
                    Err(e) => log("ERROR", &format!("retry of {} not built: {e}", msg.id)),
                }
            }
            if !retried {
                log(
                    "ERROR",
                    &format!("Task {}[{}] raised unexpected: {type_name}('{message}')", msg.task, msg.id),
                );
                if store {
                    backend
                        .store(
                            &msg.id,
                            "FAILURE",
                            celery::exc_result(&type_name, exc_args(&message)),
                            Some(&tb),
                            true,
                        )
                        .await;
                }
            }
        }
        Outcome::TimedOut(limit) => {
            log(
                "ERROR",
                &format!(
                    "Hard time limit ({}s) exceeded for {}[{}]",
                    celery::py_repr(&limit),
                    msg.task,
                    msg.id
                ),
            );
            if store {
                backend
                    .store(
                        &msg.id,
                        "FAILURE",
                        celery::exc_result("TimeLimitExceeded", vec![limit.clone()]),
                        Some(&traceback("TimeLimitExceeded", &celery::py_repr(&limit))),
                        true,
                    )
                    .await;
            }
        }
        Outcome::Lost(why) => {
            log("ERROR", &format!("Process for {}[{}] lost: {why}", msg.task, msg.id));
            if spec.acks_late {
                broker.restore_by_tag(&msg.delivery_tag, true).await;
                return;
            }
            if store {
                backend
                    .store(
                        &msg.id,
                        "FAILURE",
                        celery::exc_result("WorkerLostError", exc_args(&why)),
                        None,
                        true,
                    )
                    .await;
            }
        }
    }
    broker.ack(&msg.delivery_tag).await;
}

/// Consume `cfg.queues` until `warm` turns true (then finish running tasks, restore waiting
/// ones) or `cold` does (return at once; unacked messages come back after the visibility
/// timeout).
///
/// # Errors
/// The broker or backend cannot be reached at start.
pub async fn serve(
    cfg: ServeConfig,
    warm: watch::Receiver<bool>,
    mut cold: watch::Receiver<bool>,
) -> Result<(), String> {
    let open = |url: &str| redis::Client::open(url).map_err(|e| e.to_string());
    let broker_client = open(&cfg.broker_url)?;
    let backend_client = open(&cfg.backend_url)?;
    let mut pop = broker_client
        .get_connection_manager()
        .await
        .map_err(|e| e.to_string())?;
    let broker = Broker {
        conn: broker_client
            .get_connection_manager()
            .await
            .map_err(|e| e.to_string())?,
    };
    let backend = Backend {
        conn: backend_client
            .get_connection_manager()
            .await
            .map_err(|e| e.to_string())?,
    };
    let (jobs_tx, jobs_rx) = mpsc::channel::<Job>(cfg.concurrency);
    let jobs_rx = Arc::new(tokio::sync::Mutex::new(jobs_rx));
    for i in 0..cfg.concurrency {
        let (settings, worker, rx) = (cfg.settings.clone(), cfg.worker.clone(), Arc::clone(&jobs_rx));
        std::thread::Builder::new()
            .name(format!("baskfy-pool-{i}"))
            .spawn(move || slot(settings, worker, rx))
            .map_err(|e| e.to_string())?;
    }
    let shared = Arc::new(Shared {
        broker,
        backend,
        permits: Arc::new(Semaphore::new(cfg.concurrency)),
        jobs: jobs_tx,
        worker: cfg.worker.clone(),
        nodename: format!("celery@{}", celery::hostname()),
        warm: warm.clone(),
        waiting: Mutex::new(HashSet::new()),
    });
    for queue in &cfg.queues {
        let (key, member) = celery::binding(queue);
        let _: Result<i64, _> = redis::cmd("SADD")
            .arg(key)
            .arg(member)
            .query_async(&mut shared.broker.conn.clone())
            .await;
    }
    log(
        "INFO",
        &format!(
            "{} ready (queues {}, concurrency {})",
            shared.nodename,
            cfg.queues.join(","),
            cfg.concurrency
        ),
    );
    let keys = celery::brpop_keys(&cfg.queues);
    let mut running = tokio::task::JoinSet::new();
    let mut polls: u64 = 0;
    while !*warm.borrow() {
        let permit = {
            let mut w = warm.clone();
            tokio::select! {
                p = Arc::clone(&shared.permits).acquire_owned() => match p { Ok(p) => p, Err(_) => break },
                _ = w.wait_for(|v| *v) => break,
            }
        };
        if polls.is_multiple_of(10) {
            let mut b = shared.broker.clone();
            running.spawn(async move { b.restore_visible().await });
        }
        polls += 1;
        let popped: Result<Option<(String, String)>, _> = redis::cmd("BRPOP")
            .arg(&keys)
            .arg(BRPOP_TIMEOUT_SECS)
            .query_async(&mut pop)
            .await;
        let (key, text) = match popped {
            Ok(Some(item)) => item,
            Ok(None) => continue,
            Err(e) => {
                log("WARNING", &format!("consumer: Connection to broker lost: {e}"));
                drop(permit);
                tokio::time::sleep(StdDuration::from_secs(1)).await;
                continue;
            }
        };
        let msg = match celery::decode(&text) {
            Ok(m) => m,
            Err(e) => {
                log(
                    "ERROR",
                    &format!("Received invalid task message from {}: {e}", celery::queue_of_key(&key)),
                );
                continue;
            }
        };
        let mut b = shared.broker.clone();
        if let Err(e) = b.append(&msg, &text).await {
            log("ERROR", &format!("could not record {} as unacked: {e}", msg.id));
        }
        running.spawn(process(Arc::clone(&shared), msg, permit));
        while running.try_join_next().is_some() {}
    }
    log("INFO", "worker: Warm shutdown (MainProcess)");
    loop {
        tokio::select! {
            next = running.join_next() => if next.is_none() { break },
            _ = cold.wait_for(|v| *v) => {
                log("WARNING", "worker: Cold shutdown (MainProcess)");
                running.abort_all();
                return Ok(());
            }
        }
    }
    let waiting: Vec<String> = lock(&shared.waiting).drain().collect();
    let mut b = shared.broker.clone();
    for tag in waiting {
        b.restore_by_tag(&tag, false).await;
    }
    Ok(())
}
