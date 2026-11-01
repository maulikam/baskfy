//! Grading an actual output against a golden's expected one.

use std::fmt;

use serde_json::Value;

use crate::golden::Tolerance;

/// One difference between expected and actual.
#[derive(Debug, Clone, PartialEq)]
pub struct Mismatch {
    /// JSON path, e.g. `$.lines[0].risk_inr` or `$.rows[3][7]`.
    pub path: String,
    /// What the golden holds there (`None` when the key is absent from the golden).
    pub expected: Option<Value>,
    /// What the port produced there (`None` when the key is absent from the output).
    pub actual: Option<Value>,
    /// Why they differ.
    pub reason: String,
}

impl fmt::Display for Mismatch {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let show = |v: &Option<Value>| {
            v.as_ref()
                .map_or_else(|| "<absent>".to_owned(), Value::to_string)
        };
        write!(
            f,
            "{}: {} (expected {}, actual {})",
            self.path,
            self.reason,
            show(&self.expected),
            show(&self.actual)
        )
    }
}

/// All differences for one case; empty means the case passes.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Mismatches(pub Vec<Mismatch>);

impl Mismatches {
    /// True when there is no difference.
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

impl fmt::Display for Mismatches {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        for (i, m) in self.0.iter().enumerate() {
            if i >= 50 {
                return writeln!(f, "  … and {} more", self.0.len() - 50);
            }
            writeln!(f, "  {m}")?;
        }
        Ok(())
    }
}

impl std::error::Error for Mismatches {}

/// Compare `actual` with `expected` under `tol` (rules in the crate docs). Returns every
/// mismatch, not just the first.
pub fn compare(expected: &Value, actual: &Value, tol: &Tolerance) -> Result<(), Mismatches> {
    let mut out = Vec::new();
    walk("$", expected, actual, tol, &mut out);
    if out.is_empty() {
        Ok(())
    } else {
        Err(Mismatches(out))
    }
}

fn push(out: &mut Vec<Mismatch>, path: &str, e: &Value, a: &Value, reason: impl Into<String>) {
    out.push(Mismatch {
        path: path.to_owned(),
        expected: Some(e.clone()),
        actual: Some(a.clone()),
        reason: reason.into(),
    });
}

fn kind(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "integer",
        Value::String(_) => "string",
        Value::Array(_) => "list",
        Value::Object(_) => "object",
    }
}

fn walk(path: &str, e: &Value, a: &Value, tol: &Tolerance, out: &mut Vec<Mismatch>) {
    match (e, a) {
        (Value::Object(em), Value::Object(am)) => {
            for (k, ev) in em {
                let p = format!("{path}.{k}");
                match am.get(k) {
                    Some(av) => walk(&p, ev, av, tol, out),
                    None => out.push(Mismatch {
                        path: p,
                        expected: Some(ev.clone()),
                        actual: None,
                        reason: "key missing from actual".into(),
                    }),
                }
            }
            for (k, av) in am {
                if !em.contains_key(k) {
                    out.push(Mismatch {
                        path: format!("{path}.{k}"),
                        expected: None,
                        actual: Some(av.clone()),
                        reason: "key not in golden".into(),
                    });
                }
            }
        }
        (Value::Array(ev), Value::Array(av)) => {
            if ev.len() != av.len() {
                out.push(Mismatch {
                    path: path.to_owned(),
                    expected: Some(Value::from(ev.len())),
                    actual: Some(Value::from(av.len())),
                    reason: "list length differs".into(),
                });
            }
            for (i, (x, y)) in ev.iter().zip(av).enumerate() {
                walk(&format!("{path}[{i}]"), x, y, tol, out);
            }
        }
        (Value::Number(en), Value::Number(an)) => match (en.is_f64(), an.is_f64()) {
            (false, false) => {
                if en != an {
                    push(out, path, e, a, "integer differs");
                }
            }
            (true, true) => {
                let (ef, af) = (
                    en.as_f64().unwrap_or(f64::NAN),
                    an.as_f64().unwrap_or(f64::NAN),
                );
                let bound = tol.float * ef.abs().max(1.0);
                let within = (af - ef).abs() <= bound;
                // A NaN on either side makes `within` false: a mismatch, never a pass.
                if !within {
                    push(
                        out,
                        path,
                        e,
                        a,
                        format!("float differs by {:e} > {bound:e}", (af - ef).abs()),
                    );
                }
            }
            _ => push(
                out,
                path,
                e,
                a,
                format!("expected {}, actual {}", kind(e), kind(a)),
            ),
        },
        _ if e == a => {}
        _ if kind(e) == kind(a) => push(out, path, e, a, format!("{} differs", kind(e))),
        _ => push(
            out,
            path,
            e,
            a,
            format!("expected {}, actual {}", kind(e), kind(a)),
        ),
    }
}
