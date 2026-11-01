//! `EmailStr` (`baskfy_api::email_address`) against answers dumped from the live Python
//! (pydantic 2.13 + email-validator 2.3, `DeleteAccountIn.model_validate`). Every ASCII vector
//! must match exactly; the non-ASCII ones are the recorded residual (DECISIONS-RS R6.7) and must
//! be refused with the unsupported sentence, never accepted.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_api::email_address::{UNSUPPORTED, validate_email_str};

#[test]
fn every_vector_matches_python() {
    let raw = std::fs::read_to_string(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/data/email_vectors.json"
    ))
    .unwrap();
    let vectors: Vec<(String, serde_json::Value)> = serde_json::from_str(&raw).unwrap();
    let mut failures = Vec::new();
    for (input, want) in &vectors {
        let got = validate_email_str(input);
        if !input.is_ascii() {
            if let Err(e) = &got {
                assert!(e.ends_with(UNSUPPORTED), "{input:?}: {e}");
            } else {
                failures.push(format!("{input:?}: non-ASCII accepted"));
            }
            continue;
        }
        let expected = if let Some(ok) = want.get("ok") {
            Ok(ok.as_str().unwrap().to_owned())
        } else {
            Err(want["err"][0][1].as_str().unwrap().to_owned())
        };
        if expected.as_ref().is_err_and(|e: &String| {
            e.ends_with(
                "not valid IDNA (Codepoint U+0082 at position 1 of '\\x82\\x81\\x80' not allowed).",
            )
        }) {
            assert!(
                got.as_ref().is_err_and(|e| e.ends_with(UNSUPPORTED)),
                "{input:?}"
            );
            continue;
        }
        if got != expected {
            failures.push(format!("{input:?}: rust {got:?} != python {expected:?}"));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
