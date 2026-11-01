//! Locating, loading and grading golden cases.

use std::path::{Path, PathBuf};

use chrono::NaiveDate;
use polars::prelude::DataFrame;
use rust_decimal::Decimal;
use serde::de::DeserializeOwned;
use serde_json::Value;

use crate::compare::{Mismatches, compare};
use crate::error::TestkitError;
use crate::frame::{decode_frame, encode_frame};
use crate::scalar;

/// `<repo>/go/testdata/golden`, resolved from this crate's manifest directory — never from
/// the current working directory — so it is right under `cargo test` from anywhere.
pub fn golden_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../go/testdata/golden")
        .components()
        .collect()
}

/// The comparison tolerance of a case (`meta.tolerance`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Tolerance {
    /// `meta.tolerance.float`: a float passes when
    /// `|actual - expected| <= float * max(1, |expected|)`.
    pub float: f64,
}

impl Default for Tolerance {
    /// golden.py's default, `{"float": 1e-9}`.
    fn default() -> Self {
        Self { float: 1e-9 }
    }
}

/// The `meta` block of a golden.
#[derive(Debug, Clone, PartialEq)]
pub struct Meta {
    /// A reference such as `"DECISIONS.md §21.9"` when the golden carries a known-wrong value
    /// the port must still reproduce.
    pub known_bug: Option<String>,
    /// Comparison tolerance.
    pub tolerance: Tolerance,
    /// The dumper's notes on the case (conventions, fixture origin).
    pub notes: Option<String>,
    /// True when the golden was dumped byte-stable (no timestamp in meta).
    pub stable: bool,
}

/// One golden case: `{"fn", "inputs", "output", "meta"}`.
#[derive(Debug, Clone)]
pub struct Golden {
    /// Case name relative to the golden root without `.json`, e.g. `L1/swing/manage.case_001`.
    pub case: String,
    /// Absolute path of the file.
    pub path: PathBuf,
    /// Fully-qualified Python function, e.g. `baskfy_core.swing.stops.manage`.
    pub fn_name: String,
    /// The keyword arguments the Python function was called with, encoded.
    pub inputs: Value,
    /// What the Python function returned, encoded.
    pub output: Value,
    /// Tolerance, known bug, notes.
    pub meta: Meta,
}

fn malformed(path: &Path, detail: impl Into<String>) -> TestkitError {
    TestkitError::Malformed {
        path: path.to_owned(),
        detail: detail.into(),
    }
}

fn parse_meta(path: &Path, v: &Value) -> Result<Meta, TestkitError> {
    let m = v
        .as_object()
        .ok_or_else(|| malformed(path, "meta is not an object"))?;
    let mut tolerance = Tolerance::default();
    if let Some(t) = m.get("tolerance").filter(|t| !t.is_null()) {
        let t = t
            .as_object()
            .ok_or_else(|| malformed(path, "meta.tolerance is not an object"))?;
        for (k, x) in t {
            match k.as_str() {
                "float" => {
                    tolerance.float = x
                        .as_f64()
                        .ok_or_else(|| malformed(path, "meta.tolerance.float is not a number"))?;
                }
                other => return Err(TestkitError::UnsupportedTolerance(other.to_owned())),
            }
        }
    }
    let opt_str = |key: &str| -> Result<Option<String>, TestkitError> {
        match m.get(key) {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(s)) => Ok(Some(s.clone())),
            Some(other) => Err(malformed(
                path,
                format!("meta.{key} is not a string: {other}"),
            )),
        }
    };
    Ok(Meta {
        known_bug: opt_str("known_bug")?,
        tolerance,
        notes: opt_str("notes")?,
        stable: m.get("stable").and_then(Value::as_bool).unwrap_or(false),
    })
}

fn load_path(path: &Path) -> Result<Golden, TestkitError> {
    let text = std::fs::read_to_string(path).map_err(|source| TestkitError::Io {
        path: path.to_owned(),
        source,
    })?;
    let doc: Value = serde_json::from_str(&text).map_err(|e| malformed(path, e.to_string()))?;
    let obj = doc
        .as_object()
        .ok_or_else(|| malformed(path, "not an object"))?;
    let get = |k: &str| {
        obj.get(k)
            .ok_or_else(|| malformed(path, format!("missing {k:?}")))
    };
    let fn_name = get("fn")?
        .as_str()
        .ok_or_else(|| malformed(path, "fn is not a string"))?
        .to_owned();
    let root = golden_root();
    let case = path
        .strip_prefix(&root)
        .unwrap_or(path)
        .with_extension("")
        .to_string_lossy()
        .replace('\\', "/");
    Ok(Golden {
        case,
        path: path.to_owned(),
        fn_name,
        inputs: get("inputs")?.clone(),
        output: get("output")?.clone(),
        meta: parse_meta(path, get("meta")?)?,
    })
}

/// Load one case by name relative to [`golden_root`]: `"L1/swing/manage.case_001"` (a
/// trailing `.json` is accepted).
pub fn load(case: &str) -> Result<Golden, TestkitError> {
    let rel = case.strip_suffix(".json").unwrap_or(case);
    load_path(&golden_root().join(format!("{rel}.json")))
}

fn collect(dir: &Path, out: &mut Vec<PathBuf>) -> Result<(), TestkitError> {
    let entries = std::fs::read_dir(dir).map_err(|source| TestkitError::Io {
        path: dir.to_owned(),
        source,
    })?;
    for entry in entries {
        let p = entry
            .map_err(|source| TestkitError::Io {
                path: dir.to_owned(),
                source,
            })?
            .path();
        if p.is_dir() {
            collect(&p, out)?;
        } else if p.extension().is_some_and(|e| e == "json") {
            out.push(p);
        }
    }
    Ok(())
}

/// Every golden file under [`golden_root`], sorted by path, loaded.
pub fn all_cases() -> Result<Vec<Golden>, TestkitError> {
    let mut paths = Vec::new();
    collect(&golden_root(), &mut paths)?;
    paths.sort();
    paths.iter().map(|p| load_path(p)).collect()
}

/// Every case whose `fn` equals `fn_name` (e.g. `"baskfy_core.swing.stops.manage"`), sorted by
/// case name. Matching nothing is an error ([`TestkitError::NoCases`]) so a typo cannot pass.
pub fn cases_for(fn_name: &str) -> Result<Vec<Golden>, TestkitError> {
    let cases: Vec<Golden> = all_cases()?
        .into_iter()
        .filter(|g| g.fn_name == fn_name)
        .collect();
    if cases.is_empty() {
        Err(TestkitError::NoCases(fn_name.to_owned()))
    } else {
        Ok(cases)
    }
}

impl Golden {
    /// The raw JSON of input `key`.
    pub fn input(&self, key: &str) -> Result<&Value, TestkitError> {
        self.inputs
            .get(key)
            .ok_or_else(|| TestkitError::MissingInput {
                case: self.case.clone(),
                key: key.to_owned(),
            })
    }

    fn at(&self, key: &str, e: TestkitError) -> TestkitError {
        TestkitError::decode(format!("{}: inputs.{key}", self.case), e.to_string())
    }

    /// Input `key` deserialised into any serde type (config structs, positions, enums).
    /// Decimal fields need no annotation with the workspace's `rust_decimal/serde-str`.
    pub fn input_as<T: DeserializeOwned>(&self, key: &str) -> Result<T, TestkitError> {
        serde_json::from_value(self.input(key)?.clone()).map_err(|e| {
            TestkitError::decode(format!("{}: inputs.{key}", self.case), e.to_string())
        })
    }

    /// All inputs deserialised into one serde struct (field names = Python keyword names).
    pub fn inputs_as<T: DeserializeOwned>(&self) -> Result<T, TestkitError> {
        serde_json::from_value(self.inputs.clone())
            .map_err(|e| TestkitError::decode(format!("{}: inputs", self.case), e.to_string()))
    }

    /// Input `key` as a Decimal (`"5.10"` keeps scale 2).
    pub fn input_decimal(&self, key: &str) -> Result<Decimal, TestkitError> {
        scalar::decimal(self.input(key)?).map_err(|e| self.at(key, e))
    }

    /// Input `key` as `Option<Decimal>` (`null` is `None`).
    pub fn input_opt_decimal(&self, key: &str) -> Result<Option<Decimal>, TestkitError> {
        match self.input(key)? {
            Value::Null => Ok(None),
            v => scalar::decimal(v).map(Some).map_err(|e| self.at(key, e)),
        }
    }

    /// Input `key` as a date.
    pub fn input_date(&self, key: &str) -> Result<NaiveDate, TestkitError> {
        scalar::date(self.input(key)?).map_err(|e| self.at(key, e))
    }

    /// Input `key` as a polars `DataFrame` with the dumped dtypes.
    pub fn input_frame(&self, key: &str) -> Result<DataFrame, TestkitError> {
        decode_frame(self.input(key)?).map_err(|e| self.at(key, e))
    }

    /// Input `key` as a pandas frame (any of the three shapes; see the `pandas` module).
    pub fn input_pandas_frame(&self, key: &str) -> Result<crate::PandasFrame, TestkitError> {
        crate::decode_pandas_frame(self.input(key)?).map_err(|e| self.at(key, e))
    }

    /// The expected output deserialised into a serde type.
    pub fn output_as<T: DeserializeOwned>(&self) -> Result<T, TestkitError> {
        serde_json::from_value(self.output.clone())
            .map_err(|e| TestkitError::decode(format!("{}: output", self.case), e.to_string()))
    }

    /// The expected output as a polars `DataFrame`.
    pub fn output_frame(&self) -> Result<DataFrame, TestkitError> {
        decode_frame(&self.output)
            .map_err(|e| TestkitError::decode(format!("{}: output", self.case), e.to_string()))
    }

    /// Grade an already-encoded actual output.
    pub fn check_value(&self, actual: &Value) -> Result<(), Mismatches> {
        compare(&self.output, actual, &self.meta.tolerance)
    }

    /// Grade any serialisable actual output (structs, enums, `Vec`s, tuples-as-structs).
    pub fn check_output<T: serde::Serialize + ?Sized>(&self, actual: &T) -> Result<(), String> {
        let v = crate::to_golden_value(actual).map_err(|e| e.to_string())?;
        self.check_value(&v).map_err(|m| self.report(&m))
    }

    /// Grade an actual `DataFrame` (encoded first, so dtypes and column order count).
    pub fn check_frame(&self, actual: &DataFrame) -> Result<(), String> {
        let v = encode_frame(actual).map_err(|e| e.to_string())?;
        self.check_value(&v).map_err(|m| self.report(&m))
    }

    fn report(&self, m: &Mismatches) -> String {
        let bug = self
            .meta
            .known_bug
            .as_deref()
            .map(|b| format!(" [known_bug: {b} — reproduce it, do not fix it]"))
            .unwrap_or_default();
        format!(
            "golden {} ({}){bug}: {} mismatch(es)\n{m}",
            self.case,
            self.fn_name,
            m.0.len()
        )
    }

    /// [`Golden::check_output`], panicking with the full diff. For `#[test]` bodies.
    #[allow(clippy::panic)] // a failed grade is a failed test: panicking is the contract
    pub fn assert_output<T: serde::Serialize + ?Sized>(&self, actual: &T) {
        if let Err(msg) = self.check_output(actual) {
            panic!("{msg}");
        }
    }

    /// [`Golden::check_frame`], panicking with the full diff. For `#[test]` bodies.
    #[allow(clippy::panic)] // a failed grade is a failed test: panicking is the contract
    pub fn assert_frame(&self, actual: &DataFrame) {
        if let Err(msg) = self.check_frame(actual) {
            panic!("{msg}");
        }
    }
}
