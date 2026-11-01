//! Query parameters, parsed and validated as FastAPI does.
//!
//! Starlette parses the query string with `urllib.parse.parse_qsl(keep_blank_values=True)`
//! (`&`-separated, `+` is a space, percent-decoding with UTF-8 `replace`); a scalar parameter
//! reads the **last** value of its name; unknown names are ignored. FastAPI then validates every
//! declared parameter in signature order, collecting **all** failures before answering; the app's
//! handler turns them into `400 invalid-screen-definition` with `errors[] = {field, message,
//! type}` (`app._handle_validation`). [`Validation`] reproduces that accumulation.

use baskfy_db::OrderedJson;
use chrono::NaiveDate;

use crate::problems::{ErrorEntry, Problem, invalid_screen_definition};

/// `request.query_params`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct QueryParams {
    pairs: Vec<(String, String)>,
}

fn percent_decode(text: &str) -> String {
    let replaced = text.replace('+', " ");
    percent_encoding::percent_decode_str(&replaced)
        .decode_utf8_lossy()
        .into_owned()
}

impl QueryParams {
    /// `parse_qsl(query, keep_blank_values=True)`.
    #[must_use]
    pub fn parse(query: Option<&str>) -> Self {
        let mut pairs = Vec::new();
        for part in query.unwrap_or("").split('&') {
            if part.is_empty() {
                continue;
            }
            let (k, v) = part.split_once('=').unwrap_or((part, ""));
            pairs.push((percent_decode(k), percent_decode(v)));
        }
        Self { pairs }
    }

    /// `query_params.get(name)`: the last value.
    #[must_use]
    pub fn get(&self, name: &str) -> Option<&str> {
        self.pairs
            .iter()
            .rev()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.as_str())
    }

    /// `query_params.getlist(name)`.
    #[must_use]
    pub fn get_all(&self, name: &str) -> Vec<&str> {
        self.pairs
            .iter()
            .filter(|(k, _)| k == name)
            .map(|(_, v)| v.as_str())
            .collect()
    }
}

/// FastAPI's error accumulator for one request.
#[derive(Debug, Default)]
pub struct Validation {
    errors: Vec<ErrorEntry>,
}

impl Validation {
    /// A fresh accumulator.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Records one failure at `field` (the path below `query`).
    pub fn error(&mut self, field: &str, message: &str, kind: &str) {
        self.errors.push(vec![
            ("field".to_owned(), OrderedJson::Str(field.to_owned())),
            ("message".to_owned(), OrderedJson::Str(message.to_owned())),
            ("type".to_owned(), OrderedJson::Str(kind.to_owned())),
        ]);
    }

    /// A required parameter: `Field required` / `missing` when absent.
    pub fn required<'a>(&mut self, q: &'a QueryParams, name: &str) -> Option<&'a str> {
        let v = q.get(name);
        if v.is_none() {
            self.error(name, "Field required", "missing");
        }
        v
    }

    /// A `datetime.date` parameter (pydantic's lax string parse).
    pub fn date(&mut self, name: &str, raw: Option<&str>) -> Option<NaiveDate> {
        match baskfy_core::screener::lax_date_from_str(raw?) {
            Ok(d) => Some(d),
            Err((kind, message)) => {
                self.error(name, &message, kind);
                None
            }
        }
    }

    /// The 400 problem when anything failed.
    ///
    /// # Errors
    /// `invalid_screen_definition(errors)`.
    pub fn finish(self) -> Result<(), Problem> {
        if self.errors.is_empty() {
            Ok(())
        } else {
            Err(invalid_screen_definition(self.errors))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn last_value_wins_and_blank_values_are_kept() {
        let q = QueryParams::parse(Some("from=2026-10-01&to=&from=2026-10-05&x+y=a%20b+c&flag"));
        assert_eq!(q.get("from"), Some("2026-10-05"));
        assert_eq!(q.get("to"), Some(""));
        assert_eq!(q.get("x y"), Some("a b c"));
        assert_eq!(q.get("flag"), Some(""));
        assert_eq!(q.get_all("from"), vec!["2026-10-01", "2026-10-05"]);
        assert_eq!(q.get("missing"), None);
    }

    #[test]
    fn dates_fail_with_pydantics_words() {
        let mut v = Validation::new();
        assert!(v.date("from", Some("2026-13-01")).is_none());
        assert!(v.date("to", Some("2026-10-01T10:00:00")).is_none());
        assert_eq!(
            v.date("ok", Some("2026-10-01T00:00:00")),
            NaiveDate::from_ymd_opt(2026, 10, 1)
        );
        let Err(problem) = v.finish() else {
            unreachable!("two errors were recorded")
        };
        let body = problem
            .body(Some("/x"))
            .to_python_json()
            .unwrap_or_default();
        assert!(body.contains(r#"{"field":"from","message":"Input should be a valid date or datetime, month value is outside expected range of 1-12","type":"date_from_datetime_parsing"}"#), "{body}");
        assert!(body.contains(r#"{"field":"to","message":"Datetimes provided to dates should have zero time - e.g. be exact dates","type":"date_from_datetime_inexact"}"#), "{body}");
    }
}
