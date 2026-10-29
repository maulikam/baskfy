//! Python's `str()`, `repr()`, truthiness and `str.title()` over a `jsonb` value, for routes that
//! render stored JSON through Python expressions (`str(features.get("label") or ...)`).

use baskfy_db::OrderedJson;

/// `bool(value)`.
pub(crate) fn truthy(value: &OrderedJson) -> bool {
    match value {
        OrderedJson::Null => false,
        OrderedJson::Bool(b) => *b,
        OrderedJson::Int(i) => *i != 0,
        OrderedJson::UInt(u) => *u != 0,
        OrderedJson::Float(f) => *f != 0.0,
        OrderedJson::Str(s) => !s.is_empty(),
        OrderedJson::Array(a) => !a.is_empty(),
        OrderedJson::Object(o) => !o.is_empty(),
    }
}

/// `repr(value)` of what `json.loads` produced.
pub(crate) fn py_repr(value: &OrderedJson) -> String {
    match value {
        OrderedJson::Str(s) => baskfy_providers::py::repr_str(s),
        OrderedJson::Array(items) => {
            let parts: Vec<String> = items.iter().map(py_repr).collect();
            format!("[{}]", parts.join(", "))
        }
        OrderedJson::Object(entries) => {
            let parts: Vec<String> = entries
                .iter()
                .map(|(k, v)| format!("{}: {}", baskfy_providers::py::repr_str(k), py_repr(v)))
                .collect();
            format!("{{{}}}", parts.join(", "))
        }
        other => py_str(other),
    }
}

/// `str(value)` of what `json.loads` produced.
pub(crate) fn py_str(value: &OrderedJson) -> String {
    match value {
        OrderedJson::Null => "None".to_owned(),
        OrderedJson::Bool(true) => "True".to_owned(),
        OrderedJson::Bool(false) => "False".to_owned(),
        OrderedJson::Int(i) => i.to_string(),
        OrderedJson::UInt(u) => u.to_string(),
        OrderedJson::Float(f) => {
            if f.is_nan() {
                "nan".to_owned()
            } else if f.is_infinite() {
                if *f > 0.0 { "inf" } else { "-inf" }.to_owned()
            } else {
                baskfy_core::py::float_repr(*f)
            }
        }
        OrderedJson::Str(s) => s.clone(),
        other => py_repr(other),
    }
}

/// `str.title()`: a cased character following an uncased one is upper-cased, the rest lowered.
pub(crate) fn py_title(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut previous_cased = false;
    for c in text.chars() {
        let cased = c.is_alphabetic();
        if cased {
            if previous_cased {
                out.extend(c.to_lowercase());
            } else {
                out.extend(c.to_uppercase());
            }
        } else {
            out.push(c);
        }
        previous_cased = cased;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_spellings() {
        assert_eq!(py_title("forever plan"), "Forever Plan");
        assert_eq!(py_title("pro_2x"), "Pro_2X");
        assert_eq!(py_str(&OrderedJson::Float(1.0)), "1.0");
        assert_eq!(py_str(&OrderedJson::Null), "None");
        assert_eq!(
            py_str(&OrderedJson::Object(vec![(
                "a".to_owned(),
                OrderedJson::Array(vec![OrderedJson::Int(1), OrderedJson::Str("x".to_owned())])
            )])),
            "{'a': [1, 'x']}"
        );
        assert!(!truthy(&OrderedJson::Str(String::new())));
        assert!(truthy(&OrderedJson::Int(2)));
    }
}
