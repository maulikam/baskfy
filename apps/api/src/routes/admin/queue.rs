//! `baskfy_api.queue`: the API as a **Celery producer** (`CeleryTaskQueue.send_task`), for the
//! three admin actions that enqueue — the nightly re-run, the instrument reprocess and the resync.
//!
//! Celery over Redis is kombu's Redis transport: publishing a task to queue `Q` is
//! `SADD _kombu.binding.Q "Q\x06\x16\x06\x16Q"` (the queue's declaration, idempotent) and
//! `LPUSH Q <message>`, where the message is kombu's JSON envelope around Celery's protocol-2
//! body — base64 of `[args, kwargs, embed]`. Both go in one Lua script (DECISIONS-RS R6.36). The
//! routing table is `PRODUCER_TASK_ROUTES` (the producer resolves it at send time, M45.5), with
//! `default` for an unrouted name.
//!
//! What differs between two correct producers is volatile by construction: the task id (a
//! uuid4), `reply_to` (the producer app's oid), `delivery_tag`, and `origin` (`gen<pid>@<host>`).
//!
//! **These tasks reach ingestion, the calendar and the pipeline — never `packages/execution`,
//! never an order or a GTT** (law 2).

use base64::Engine;
use std::sync::OnceLock;

use crate::cache;
use crate::settings::Settings;

/// `PRODUCER_TASK_ROUTES`, as (glob, queue), in the dict's order.
const ROUTES: [(&str, &str); 21] = [
    ("baskfy.ingest.*", "ingest"),
    ("baskfy.eq_bars.*", "ingest"),
    ("baskfy.compute.*", "compute"),
    ("baskfy.backtest.*", "backtest"),
    ("baskfy.pipeline.*", "default"),
    ("baskfy.ops.*", "default"),
    ("baskfy.alerts.*", "default"),
    ("baskfy.desk.*", "default"),
    ("baskfy.cb.*", "compute"),
    ("baskfy.portfolio.*", "compute"),
    ("baskfy.swing.scan_now", "compute"),
    ("baskfy.swing.scan_after_login", "compute"),
    ("baskfy.swing.scan_sweep", "default"),
    ("baskfy.kite.*", "compute"),
    ("baskfy.vbt.backtest", "compute"),
    ("baskfy.overlap.catalyst_scan", "compute"),
    ("baskfy.fno.retest", "compute"),
    ("baskfy.vbt.rescan", "compute"),
    ("baskfy.vbt.rescan_sweep", "default"),
    ("baskfy.twt.scan", "compute"),
    ("baskfy.twt.scan_publish", "default"),
];

/// `DEFAULT_QUEUE`.
const DEFAULT_QUEUE: &str = "default";

/// The queue Celery's router picks for `name` (glob routes match by `fnmatch`; only trailing
/// `*` appears in the table).
#[must_use]
pub(crate) fn queue_for(name: &str) -> &'static str {
    for (glob, queue) in ROUTES {
        let hit = match glob.strip_suffix('*') {
            Some(prefix) => name.starts_with(prefix),
            None => name == glob,
        };
        if hit {
            return queue;
        }
    }
    DEFAULT_QUEUE
}

/// One positional argument of a task: the two kinds the admin actions send.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Arg {
    /// A string (`trade_date.isoformat()`).
    Str(String),
    /// An integer (an instrument id, a user id, a day count).
    Int(i64),
}

impl Arg {
    fn json(&self) -> String {
        match self {
            Self::Str(s) => serde_json::Value::String(s.clone()).to_string(),
            Self::Int(i) => i.to_string(),
        }
    }

    fn repr(&self) -> String {
        match self {
            Self::Str(s) => baskfy_providers::py::repr_str(s),
            Self::Int(i) => i.to_string(),
        }
    }
}

fn uuid4() -> String {
    let mut b: [u8; 16] = rand::random();
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    let h: String = b.iter().map(|x| format!("{x:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &h[0..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..32]
    )
}

/// The producer app's `oid` (one per process, as Celery's is).
fn oid() -> &'static str {
    static OID: OnceLock<String> = OnceLock::new();
    OID.get_or_init(uuid4)
}

/// `gen<pid>@<hostname>` (Celery's `anon_nodename`).
fn origin() -> String {
    let host = std::env::var("HOSTNAME").unwrap_or_else(|_| "localhost".to_owned());
    format!("gen{}@{host}", std::process::id())
}

/// Python's `json.dumps` of a `str`: ASCII-only, `\uXXXX` escapes.
fn py_json_str(s: &str) -> String {
    let mut out = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// The message kombu writes for `send_task(name, args=list(args))`, and its task id.
pub(crate) fn message(name: &str, args: &[Arg]) -> (String, String) {
    let task_id = uuid4();
    let queue = queue_for(name);
    let args_json = format!(
        "[{}]",
        args.iter().map(Arg::json).collect::<Vec<_>>().join(", ")
    );
    let body = format!(
        "[{args_json}, {{}}, {{\"callbacks\": null, \"errbacks\": null, \"chain\": null, \
         \"chord\": null}}]"
    );
    let body64 = base64::engine::general_purpose::STANDARD.encode(body.as_bytes());
    let argsrepr = format!(
        "[{}]",
        args.iter().map(Arg::repr).collect::<Vec<_>>().join(", ")
    );
    let envelope = format!(
        "{{\"body\": \"{body64}\", \"content-encoding\": \"utf-8\", \"content-type\": \
         \"application/json\", \"headers\": {{\"lang\": \"py\", \"task\": {task}, \"id\": \
         \"{task_id}\", \"shadow\": null, \"eta\": null, \"expires\": null, \"group\": null, \
         \"group_index\": null, \"retries\": 0, \"timelimit\": [null, null], \"root_id\": \
         \"{task_id}\", \"parent_id\": null, \"argsrepr\": {argsrepr}, \"kwargsrepr\": \"{{}}\", \
         \"origin\": {origin}, \"ignore_result\": false, \"replaced_task_nesting\": 0, \
         \"stamped_headers\": null, \"stamps\": {{}}}}, \"properties\": {{\"correlation_id\": \
         \"{task_id}\", \"reply_to\": \"{reply_to}\", \"delivery_mode\": 2, \"delivery_info\": \
         {{\"exchange\": \"\", \"routing_key\": \"{queue}\"}}, \"priority\": 0, \
         \"body_encoding\": \"base64\", \"delivery_tag\": \"{tag}\"}}}}",
        task = py_json_str(name),
        argsrepr = py_json_str(&argsrepr),
        origin = py_json_str(&origin()),
        reply_to = oid(),
        tag = uuid4(),
    );
    (task_id, envelope)
}

/// Declare the queue and push the message, as kombu's Redis transport does.
const PUBLISH_SCRIPT: &str = "redis.call('SADD', KEYS[1], ARGV[1]); \
                              return redis.call('LPUSH', KEYS[2], ARGV[2])";

/// Why a task was not published.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum NotSent {
    /// `build_task_queue` returned `None`: no broker configured.
    NoBroker,
    /// The broker refused or could not be reached (kombu raises: the 500 catch-all).
    Failed(String),
}

/// `queue.send_task(name, args)`: the task id.
///
/// # Errors
/// [`NotSent`].
pub(crate) async fn send_task(
    settings: &Settings,
    name: &str,
    args: &[Arg],
) -> Result<String, NotSent> {
    if settings.redis_url.expose().is_empty() {
        return Err(NotSent::NoBroker);
    }
    let queue = queue_for(name);
    let (task_id, envelope) = message(name, args);
    let binding = format!("{queue}\u{6}\u{16}\u{6}\u{16}{queue}");
    cache::eval(
        &settings.redis_url,
        PUBLISH_SCRIPT,
        &[format!("_kombu.binding.{queue}"), queue.to_owned()],
        &[binding, envelope],
    )
    .await
    .map_err(NotSent::Failed)?;
    Ok(task_id)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn the_producer_routes_by_name() {
        // test_celery_config.py's anti-drift table, for the names the admin actions send.
        assert_eq!(queue_for("baskfy.pipeline.nightly"), "default");
        assert_eq!(queue_for("baskfy.compute.reprocess_instrument"), "compute");
        assert_eq!(queue_for("baskfy.ops.resync"), "default");
        assert_eq!(queue_for("baskfy.unrouted"), "default");
        assert_eq!(queue_for("baskfy.twt.scan"), "compute");
    }

    #[test]
    fn the_envelope_is_komb_us() {
        let (id, m) = message("baskfy.compute.reprocess_instrument", &[Arg::Int(5)]);
        let v: serde_json::Value = serde_json::from_str(&m).unwrap();
        assert_eq!(v["headers"]["id"], id);
        assert_eq!(v["headers"]["argsrepr"], "[5]");
        assert_eq!(v["properties"]["delivery_info"]["routing_key"], "compute");
        let body = base64::engine::general_purpose::STANDARD
            .decode(v["body"].as_str().unwrap())
            .unwrap();
        assert_eq!(
            String::from_utf8(body).unwrap(),
            "[[5], {}, {\"callbacks\": null, \"errbacks\": null, \"chain\": null, \"chord\": null}]"
        );
        let (_, m) = message("baskfy.pipeline.nightly", &[Arg::Str("2026-10-29".into())]);
        let v: serde_json::Value = serde_json::from_str(&m).unwrap();
        assert_eq!(v["headers"]["argsrepr"], "['2026-10-29']");
        assert_eq!(id.len(), 36);
    }
}
