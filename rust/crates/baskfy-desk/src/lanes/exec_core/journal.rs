//! The gateway's order journal (`OrderGateway._journal` / `_replay_journal`): one
//! `json.dumps(rec, default=str)` line per event, every line carrying the `client_id` the order
//! was keyed on and `ts = time.time()`, appended to `data/outputs/orders_journal.jsonl` (or a
//! sleeve's own journal).
//!
//! `json.dumps` writes non-finite floats as `NaN`/`Infinity`/`-Infinity` (Python's default
//! `allow_nan=True`), and a refusal line can carry one (a NaN trigger is journalled before it
//! is refused), so the writer here does too, and the replay reads them back.

use std::collections::HashMap;
use std::fmt::Write as _;
use std::io::Write as _;
use std::path::{Path, PathBuf};

use crate::wire::{PyDict, PyValue};

/// `json.dumps(value)` with Python's defaults, `allow_nan=True` included.
#[must_use]
pub fn dumps_allow_nan(value: &PyValue) -> String {
    let mut out = String::new();
    write_value(value, &mut out);
    out
}

fn write_value(value: &PyValue, out: &mut String) {
    match value {
        PyValue::None => out.push_str("null"),
        PyValue::Bool(true) => out.push_str("true"),
        PyValue::Bool(false) => out.push_str("false"),
        PyValue::Int(i) => {
            let _ = write!(out, "{i}");
        }
        PyValue::Float(f) => {
            if f.is_nan() {
                out.push_str("NaN");
            } else if f.is_infinite() {
                out.push_str(if *f > 0.0 { "Infinity" } else { "-Infinity" });
            } else {
                out.push_str(&baskfy_providers::py::repr_float(*f));
            }
        }
        PyValue::Str(s) => out.push_str(&baskfy_providers::py::json_str(s)),
        PyValue::Date(d) => {
            // `default=str` on a date.
            out.push_str(&baskfy_providers::py::json_str(
                &d.format("%Y-%m-%d").to_string(),
            ));
        }
        PyValue::DateTime(_) | PyValue::Bytes(_) => {
            // `default=str` — not a shape the gateway journals; written as its debug text so
            // the line is still a line rather than an exception mid-batch.
            out.push_str(&baskfy_providers::py::json_str(&format!("{value:?}")));
        }
        PyValue::List(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_value(item, out);
            }
            out.push(']');
        }
        PyValue::Map(map) => {
            out.push('{');
            for (i, (k, v)) in map.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                out.push_str(&baskfy_providers::py::json_str(k));
                out.push_str(": ");
                write_value(v, out);
            }
            out.push('}');
        }
    }
}

/// Replace the bare `NaN`/`Infinity`/`-Infinity` tokens `json.dumps` may write (outside
/// strings) with `null`, so a strict JSON reader can parse a journal line.
fn strictify(line: &str) -> String {
    let mut out = String::with_capacity(line.len());
    let mut in_str = false;
    let mut escaped = false;
    let mut rest = line;
    while let Some(c) = rest.chars().next() {
        if in_str {
            out.push(c);
            if escaped {
                escaped = false;
            } else if c == '\\' {
                escaped = true;
            } else if c == '"' {
                in_str = false;
            }
            rest = &rest[c.len_utf8()..];
            continue;
        }
        if c == '"' {
            in_str = true;
            out.push(c);
            rest = &rest[1..];
            continue;
        }
        let token = ["-Infinity", "Infinity", "NaN"]
            .into_iter()
            .find(|t| rest.starts_with(t));
        if let Some(t) = token {
            out.push_str("null");
            rest = &rest[t.len()..];
            continue;
        }
        out.push(c);
        rest = &rest[c.len_utf8()..];
    }
    out
}

/// What the replay recovered: `_sent` (orders) and `_gtt_sent` (GTTs), kept separate.
#[derive(Debug, Default, Clone, PartialEq)]
pub struct Replayed {
    /// client_id → broker order id (or `DRY-<cid>`).
    pub sent: HashMap<String, String>,
    /// client_id → trigger id (or `DRY-<cid>`).
    pub gtt_sent: HashMap<String, PyValue>,
}

fn py_str_of(v: &PyValue) -> String {
    match v {
        PyValue::Str(s) => s.clone(),
        PyValue::Int(i) => i.to_string(),
        PyValue::Float(f) => baskfy_core::py::float_repr(*f),
        PyValue::Bool(b) => if *b { "True" } else { "False" }.to_owned(),
        other => format!("{other:?}"),
    }
}

/// The append-only journal file.
#[derive(Debug, Clone)]
pub struct Journal {
    path: PathBuf,
}

impl Journal {
    /// `os.makedirs(dirname(journal_path), exist_ok=True)` then the journal at `path`.
    ///
    /// # Errors
    /// The directory cannot be created.
    pub fn open(path: &Path) -> std::io::Result<Self> {
        if let Some(parent) = path.parent()
            && !parent.as_os_str().is_empty()
        {
            std::fs::create_dir_all(parent)?;
        }
        Ok(Self {
            path: path.to_path_buf(),
        })
    }

    /// The file.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// `_journal(rec, client_id=...)`: `client_id` and `ts` appended to the record, one line.
    /// `client_id` has no default on purpose — `None` is a deliberate answer.
    ///
    /// # Errors
    /// The append fails.
    pub fn write(&self, mut rec: PyDict, client_id: Option<&str>, ts: f64) -> std::io::Result<()> {
        rec.insert(
            "client_id",
            client_id.map_or(PyValue::None, |c| PyValue::Str(c.to_owned())),
        );
        rec.insert("ts", PyValue::Float(ts));
        let mut line = dumps_allow_nan(&PyValue::Map(rec));
        line.push('\n');
        let mut f = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;
        f.write_all(line.as_bytes())
    }

    /// `_replay_journal()`: rebuild the idempotency maps from the durable journal so a restart
    /// cannot double-send a client_id that already reached the broker (AF 3.4).
    ///
    /// # Errors
    /// The file cannot be read, or a line is not JSON (Python's `json.loads` raises).
    pub fn replay(&self) -> Result<Replayed, String> {
        let mut out = Replayed::default();
        if !self.path.is_file() {
            return Ok(out);
        }
        let text = std::fs::read_to_string(&self.path).map_err(|e| e.to_string())?;
        for raw in text.lines() {
            let line = raw.trim();
            if line.is_empty() {
                continue;
            }
            let rec = PyValue::from_json(strictify(line).as_bytes())?;
            // Python's `rec.get(...)` raises AttributeError on a line that is JSON but not an
            // object, so the gateway is never built; fail the same way rather than skip it.
            let PyValue::Map(rec) = rec else {
                return Err(format!(
                    "AttributeError: a journal line is not a JSON object: {line}"
                ));
            };
            let Some(cid) = rec.get("client_id").filter(|v| v.truthy()) else {
                continue;
            };
            let cid = py_str_of(cid);
            match rec.get("event") {
                Some(PyValue::Str(e)) if e == "placed" => {
                    if let Some(oid) = rec.get("order_id").filter(|v| **v != PyValue::None) {
                        out.sent.insert(cid, py_str_of(oid));
                    }
                }
                Some(PyValue::Str(e)) if e == "dry_run" => {
                    out.sent.insert(cid.clone(), format!("DRY-{cid}"));
                }
                Some(PyValue::Str(e)) if e == "gtt_placed" => {
                    if let Some(gid) = rec.get("gtt_id").filter(|v| **v != PyValue::None) {
                        out.gtt_sent.insert(cid, gid.clone());
                    }
                }
                Some(PyValue::Str(e)) if e == "gtt_dry_run" => {
                    out.gtt_sent
                        .insert(cid.clone(), PyValue::Str(format!("DRY-{cid}")));
                }
                _ => {}
            }
        }
        Ok(out)
    }
}
