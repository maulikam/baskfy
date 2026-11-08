//! `WideDecimal` helpers for the simple routes (`billing`, `trades`, `portfolios`).
//!
//! Every stored `numeric` these routes echo or multiply is a
//! [`WideDecimal`] — Python's `Decimal` in its default context (28 significant digits,
//! half-even). On the wire a `Decimal` is pydantic's `str(d)` ([`WideDecimal::to_sci_string`]).
//! The overview's arithmetic, which needs other precisions and non-integral powers, uses
//! [`super::pydec::Dec`].

use baskfy_core::decimal::{Rounding, WideDecimal};
use serde::Serializer;

/// `Decimal(0)`.
pub(crate) fn zero() -> WideDecimal {
    WideDecimal::zero()
}

/// `value.quantize(Decimal("1E{exp}"))` with the context's half-even rounding. Python raises
/// `InvalidOperation` for a result wider than 28 digits; no figure here is near that, so the
/// unrounded value is kept rather than inventing an error path.
pub(crate) fn quantize(value: &WideDecimal, exp: i64) -> WideDecimal {
    value
        .quantize(exp, Rounding::HalfEven)
        .unwrap_or_else(|_| value.clone())
}

/// `serialize_with` for a `Decimal` field.
pub(crate) fn ser<S: Serializer>(value: &WideDecimal, s: S) -> Result<S::Ok, S::Error> {
    s.serialize_str(&value.to_sci_string())
}

/// `serialize_with` for a `Decimal | None` field.
pub(crate) fn ser_opt<S: Serializer>(value: &Option<WideDecimal>, s: S) -> Result<S::Ok, S::Error> {
    match value {
        Some(v) => ser(v, s),
        None => s.serialize_none(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lit(text: &str) -> WideDecimal {
        text.parse().unwrap_or_default()
    }

    #[test]
    fn python_context_arithmetic() {
        // Each right-hand side is CPython 3.12's `str()` of the same expression.
        assert_eq!(
            lit("1").div(&lit("3")).map(|d| d.to_sci_string()),
            Ok("0.3333333333333333333333333333".to_owned())
        );
        assert_eq!(zero().add(&lit("0.00")).to_sci_string(), "0.00");
        assert_eq!(
            lit("392.0000").mul(&lit("6436.1584")).to_sci_string(),
            "2522974.09280000"
        );
        assert_eq!(quantize(&lit("2.675"), -2).to_sci_string(), "2.68");
        assert_eq!(quantize(&lit("2.665"), -2).to_sci_string(), "2.66");
    }
}
