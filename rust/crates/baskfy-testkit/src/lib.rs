//! Parity goldens for the Rust rewrite: load a case, decode its inputs, grade an output.
//!
//! The answer key is `go/testdata/golden/**.json`, written by `tools/parity/golden.py` from the
//! live Python. Encoding (golden.py's docstring is authoritative):
//!
//! | Python | golden JSON | decode with |
//! |---|---|---|
//! | `Decimal` | string, `format(d, "f")` — scale kept (`"5.10"`) | [`Golden::input_decimal`], [`decimal`] |
//! | `date` | `"2026-09-02"` | [`Golden::input_date`], [`date`] |
//! | aware `datetime` | `"2026-10-27T12:41:00+05:30"` | [`datetime`] |
//! | naive `datetime` (exchange-local IST) | `"2026-09-02T09:31:00"` | [`naive_datetime`] |
//! | `time` | `"14:00:00"` | [`time`] |
//! | NaN / inf | `null` | — |
//! | polars `DataFrame` | `{"columns", "dtypes", "rows"}` (polars dtype names) | [`Golden::input_frame`], [`decode_frame`] |
//! | polars `Series` | `{"dtype", "values"}` | [`decode_series`] |
//! | pandas `DataFrame` (ranking goldens) | inputs `{"columns", "rows"}` / `{"columns", "date_columns", "rows"}`; outputs `{"columns", "dtypes", "index", "rows"}` | [`Golden::input_pandas_frame`], [`decode_pandas_frame`]; grade with [`encode_pandas_frame`] |
//! | pandas `Series` | `{"dtype", "index", "values"}` | [`decode_pandas_series`]; grade with [`encode_pandas_series`] |
//! | dataclass / pydantic | object with sorted keys | [`Golden::input_as`] into a serde struct |
//! | `StrEnum` | its value | serde `rename_all` on the Rust enum |
//!
//! Pandas frames carry an index and pandas dtype names (`object` vs `bool`/`str`) a polars
//! frame cannot hold, so they decode to a [`PandasFrame`] — the polars frame plus both — and a
//! port grades a pandas output by re-encoding its polars result with
//! [`encode_pandas_frame`]`(df, index, dtypes)` and [`Golden::check_value`]. The `pandas`
//! module docs give the dtype mapping (`object` columns are inferred from their values; an
//! inferred input column of integers beside a `null` becomes `Float64`, as pandas would).
//!
//! Grading ([`compare`], [`Golden::check_output`], [`Golden::assert_output`]):
//!
//! * objects: identical key sets, values compared recursively;
//! * arrays: same length, order-sensitive;
//! * strings (incl. every Decimal and date): **byte-exact** — `"5.1"` is not `"5.10"`;
//!   match Python's `quantize` with `Decimal::rescale` / `round_dp_with_strategy`;
//! * integers: exact; an integer where the golden has a float (or the reverse) is a mismatch;
//! * floats: `|actual - expected| <= tol * max(1, |expected|)` where `tol` is the case's
//!   `meta.tolerance.float` (1e-9 in every golden today, docs/go-rewrite/03 §Tolerances);
//! * `null` matches only `null` (so NaN matches NaN, because both encode to `null`).
//!
//! `meta.known_bug` does **not** relax grading: the port reproduces the Python, bug included,
//! with a `// KNOWN BUG` comment and a DECISIONS-RS entry. The bug text is printed on failure.

mod compare;
mod error;
mod frame;
mod golden;
mod pandas;
mod scalar;

pub use compare::{Mismatch, Mismatches, compare};
pub use error::TestkitError;
pub use frame::{decode_frame, decode_series, encode_frame, encode_series};
pub use golden::{Golden, Meta, Tolerance, all_cases, cases_for, golden_root, load};
pub use pandas::{
    PandasFrame, decode_pandas_frame, decode_pandas_series, encode_pandas_frame,
    encode_pandas_series, float_series, is_pandas_frame, is_pandas_series, pandas_dtype_name,
};
pub use scalar::{
    Decoded, date, datetime, decimal, decode_value, naive_datetime, time, wide_decimals,
};

/// Serialise any value the way the goldens are encoded (Decimal as string with
/// `rust_decimal`'s `serde-str` feature, dates ISO, non-finite floats as `null`).
pub fn to_golden_value<T: serde::Serialize + ?Sized>(
    value: &T,
) -> Result<serde_json::Value, TestkitError> {
    serde_json::to_value(value).map_err(|e| TestkitError::Encode(e.to_string()))
}
