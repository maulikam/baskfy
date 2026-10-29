//! PyJWT 2.13's acceptance rules as `baskfy_api.auth.decode_token` uses them, case by case. Each
//! decode outcome was checked against `jwt.decode(..., algorithms=["HS256"], options={"require":
//! ["exp", "iat", "sub"]}, leeway=30)` in the decile-blueprint venv (the `exp == now - leeway`
//! boundary follows PyJWT's `<=`; the epoch cases are the API's `_token_epoch`, DECISIONS-RS R5.5).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use base64::Engine;
use baskfy_api::auth::{TokenError, bearer_token, pyjwt_decode, token_epoch};
use hmac::{Hmac, Mac};
use sha2::Sha256;

const KEY: &[u8] = b"baskfy-test-secret-0123456789abc";
const NOW: f64 = 1_790_000_000.0;

fn b64(bytes: &[u8]) -> String {
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

fn token(header: &str, payload: &str, key: &[u8]) -> String {
    let signing = format!("{}.{}", b64(header.as_bytes()), b64(payload.as_bytes()));
    let mut mac = Hmac::<Sha256>::new_from_slice(key).unwrap();
    mac.update(signing.as_bytes());
    format!("{signing}.{}", b64(&mac.finalize().into_bytes()))
}

const H: &str = r#"{"alg":"HS256","typ":"JWT"}"#;

fn decode(t: &str) -> Result<(), TokenError> {
    pyjwt_decode(t, KEY, None, None, 30.0, NOW).map(|_| ())
}

fn claims(extra: &str) -> String {
    format!(r#"{{"sub":"abc","iat":1789999990,"exp":1790000500{extra}}}"#)
}

#[test]
fn a_good_token_decodes() {
    assert_eq!(decode(&token(H, &claims(""), KEY)), Ok(()));
}

#[test]
fn signature_and_algorithm() {
    assert_eq!(
        decode(&token(H, &claims(""), b"another-key")),
        Err(TokenError::Invalid("InvalidSignatureError"))
    );
    let none = format!(
        "{}.{}.",
        b64(br#"{"alg":"none"}"#),
        b64(claims("").as_bytes())
    );
    assert_eq!(
        decode(&none),
        Err(TokenError::Invalid("InvalidAlgorithmError"))
    );
    assert_eq!(
        decode(&token(r#"{"alg":"HS512"}"#, &claims(""), KEY)),
        Err(TokenError::Invalid("InvalidAlgorithmError"))
    );
    assert_eq!(
        decode(&token(r#"{"typ":"JWT"}"#, &claims(""), KEY)),
        Err(TokenError::Invalid("InvalidAlgorithmError"))
    );
    assert_eq!(decode("only.two"), Err(TokenError::Invalid("DecodeError")));
    assert_eq!(
        decode("not-a-token"),
        Err(TokenError::Invalid("DecodeError"))
    );
    assert_eq!(
        decode(&token("[1]", &claims(""), KEY)),
        Err(TokenError::Invalid("DecodeError"))
    );
}

#[test]
fn headers_are_validated_before_the_signature() {
    assert_eq!(
        decode(&token(r#"{"alg":"HS256","kid":1}"#, &claims(""), KEY)),
        Err(TokenError::Invalid("InvalidTokenError"))
    );
    assert_eq!(
        decode(&token(
            r#"{"alg":"HS256","crit":["exp"]}"#,
            &claims(""),
            KEY
        )),
        Err(TokenError::Invalid("InvalidTokenError"))
    );
    assert_eq!(
        decode(&token(r#"{"alg":"HS256","kid":"k1"}"#, &claims(""), KEY)),
        Ok(())
    );
}

#[test]
fn required_claims_and_their_types() {
    assert_eq!(
        decode(&token(H, r#"{"iat":1789999990,"exp":1790000500}"#, KEY)),
        Err(TokenError::Invalid("MissingRequiredClaimError"))
    );
    assert_eq!(
        decode(&token(H, r#"{"sub":"a","iat":null,"exp":1790000500}"#, KEY)),
        Err(TokenError::Invalid("MissingRequiredClaimError"))
    );
    assert_eq!(
        decode(&token(
            H,
            r#"{"sub":1,"iat":1789999990,"exp":1790000500}"#,
            KEY
        )),
        Err(TokenError::Invalid("InvalidSubjectError"))
    );
    assert_eq!(
        decode(&token(H, &claims(r#","jti":5"#), KEY)),
        Err(TokenError::Invalid("InvalidJTIError"))
    );
    // int("1790000500") is a valid exp; int("x") is a DecodeError; int([1]) escapes PyJWT.
    assert_eq!(
        decode(&token(
            H,
            r#"{"sub":"a","iat":1789999990,"exp":"1790000500"}"#,
            KEY
        )),
        Ok(())
    );
    assert_eq!(
        decode(&token(H, r#"{"sub":"a","iat":1789999990,"exp":"x"}"#, KEY)),
        Err(TokenError::Invalid("DecodeError"))
    );
    assert_eq!(
        decode(&token(H, r#"{"sub":"a","iat":1789999990,"exp":[1]}"#, KEY)),
        Err(TokenError::Internal("TypeError"))
    );
    assert_eq!(
        decode(&token(
            H,
            r#"{"sub":"a","iat":"soon","exp":1790000500}"#,
            KEY
        )),
        Err(TokenError::Invalid("InvalidIssuedAtError"))
    );
}

#[test]
fn time_claims_with_leeway() {
    // Expired 20 s ago: inside the 30 s leeway. Expired 40 s ago: out.
    assert_eq!(
        decode(&token(
            H,
            r#"{"sub":"a","iat":1789999000,"exp":1789999980}"#,
            KEY
        )),
        Ok(())
    );
    assert_eq!(
        decode(&token(
            H,
            r#"{"sub":"a","iat":1789999000,"exp":1789999960}"#,
            KEY
        )),
        Err(TokenError::Invalid("ExpiredSignatureError"))
    );
    // exp exactly now - leeway is expired (<=).
    assert_eq!(
        decode(&token(
            H,
            r#"{"sub":"a","iat":1789999000,"exp":1789999970}"#,
            KEY
        )),
        Err(TokenError::Invalid("ExpiredSignatureError"))
    );
    // Issued in the future beyond the leeway.
    assert_eq!(
        decode(&token(
            H,
            r#"{"sub":"a","iat":1790000100,"exp":1790000500}"#,
            KEY
        )),
        Err(TokenError::Invalid("ImmatureSignatureError"))
    );
    assert_eq!(
        decode(&token(H, &claims(r#","nbf":1790000100"#), KEY)),
        Err(TokenError::Invalid("ImmatureSignatureError"))
    );
    // A float exp truncates, as int(1790000500.9) does.
    assert_eq!(
        decode(&token(
            H,
            r#"{"sub":"a","iat":1789999990,"exp":1790000500.9}"#,
            KEY
        )),
        Ok(())
    );
}

#[test]
fn audience_and_issuer() {
    let with = |aud: Option<&str>, iss: Option<&str>, payload: &str| {
        pyjwt_decode(&token(H, payload, KEY), KEY, aud, iss, 30.0, NOW).map(|_| ())
    };
    // No audience configured: a token that names one is refused, an empty one is fine.
    assert_eq!(
        with(None, None, &claims(r#","aud":"web""#)),
        Err(TokenError::Invalid("InvalidAudienceError"))
    );
    assert_eq!(with(None, None, &claims(r#","aud":"""#)), Ok(()));
    // Audience configured.
    assert_eq!(with(Some("web"), None, &claims(r#","aud":"web""#)), Ok(()));
    assert_eq!(
        with(Some("web"), None, &claims(r#","aud":["x","web"]"#)),
        Ok(())
    );
    assert_eq!(
        with(Some("web"), None, &claims(r#","aud":"x""#)),
        Err(TokenError::Invalid("InvalidAudienceError"))
    );
    assert_eq!(
        with(Some("web"), None, &claims("")),
        Err(TokenError::Invalid("MissingRequiredClaimError"))
    );
    assert_eq!(
        with(Some("web"), None, &claims(r#","aud":[1]"#)),
        Err(TokenError::Invalid("InvalidAudienceError"))
    );
    // Issuer.
    assert_eq!(
        with(None, Some("baskfy"), &claims(r#","iss":"baskfy""#)),
        Ok(())
    );
    assert_eq!(
        with(None, Some("baskfy"), &claims(r#","iss":"other""#)),
        Err(TokenError::Invalid("InvalidIssuerError"))
    );
    assert_eq!(
        with(None, Some("baskfy"), &claims("")),
        Err(TokenError::Invalid("MissingRequiredClaimError"))
    );
    assert_eq!(with(None, None, &claims(r#","iss":"anything""#)), Ok(()));
}

#[test]
fn the_epoch_claim_is_an_int_or_zero() {
    let read = |extra: &str| {
        let c = pyjwt_decode(&token(H, &claims(extra), KEY), KEY, None, None, 30.0, NOW).unwrap();
        token_epoch(&c)
    };
    assert_eq!(read(r#","epoch":3"#), 3);
    assert_eq!(read(""), 0);
    assert_eq!(read(r#","epoch":true"#), 0, "a bool is not an epoch");
    assert_eq!(read(r#","epoch":"4""#), 0);
    assert_eq!(read(r#","epoch":4.0"#), 0);
}

#[test]
fn the_bearer_prefix_is_exact() {
    assert_eq!(bearer_token(Some("Bearer abc ")).as_deref(), Some("abc"));
    assert_eq!(bearer_token(Some("Bearer    ")), None);
    assert_eq!(bearer_token(Some("bearer abc")), None);
    assert_eq!(bearer_token(Some("Basic abc")), None);
    assert_eq!(bearer_token(None), None);
}
