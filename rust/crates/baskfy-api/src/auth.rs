//! Bearer-token verification. Ports `baskfy_api.auth` (and the slice of PyJWT 2.13 it relies on).
//!
//! The API **verifies**; it never issues (the web app mints access tokens with the shared
//! secret). What is refused, exactly as Python refuses it:
//!
//! * any algorithm but HS256 (`alg: none`, an RS256 header) — no negotiation;
//! * a bad signature, a missing `exp`/`iat`/`sub`, an expired token, an `iat` or `nbf` in the
//!   future (all with `BASKFY_JWT_LEEWAY_SECONDS`), a wrong or unexpected `aud`, a wrong `iss`;
//! * a lifetime `exp - iat` above `BASKFY_JWT_MAX_LIFETIME_SECONDS`;
//! * a subject that is not an active `app_user.public_id`;
//! * **a stale session epoch**: the token's `epoch` claim (missing = 0) below the account's
//!   `session_epoch` — a cookie issued before a revocation is dead (AUDIT 0.8 / 2.2).
//!
//! Every refusal is the same 401 `unauthenticated` with one short sentence (no oracle). A
//! request without a bearer token is [`Principal::anonymous`], not an error.
//!
//! PyJWT is reimplemented rather than replaced by a JWT crate because the acceptance rules are
//! the contract: which `aud` is "unexpected", that `iat` is checked, that `int("12")` is a valid
//! `exp`. A crate with its own opinions would accept or refuse a different set (DECISIONS-RS R5.5).

use base64::Engine;
use baskfy_db::{Db, OrderedJson};
use hmac::{Hmac, Mac};
use sha2::Sha256;

use crate::logging;
use crate::problems::{
    Problem, ProblemType, internal_error, unauthenticated, unauthenticated_default,
};
use crate::settings::{JWT_ALGORITHM, Settings};

/// `BEARER_PREFIX`.
pub const BEARER_PREFIX: &str = "Bearer ";

/// `API_KEY_HEADER` — knowingly ignored until the public API's keys exist.
pub const API_KEY_HEADER: &str = "X-API-Key";

/// `PrincipalKind`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PrincipalKind {
    /// `anonymous`.
    Anonymous,
    /// `user`.
    User,
    /// `api_key` — reachable only once API keys are issued.
    ApiKey,
}

/// `Principal`: who is making this request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Principal {
    /// `kind`.
    pub kind: PrincipalKind,
    /// `app_user.id`.
    pub user_id: Option<i64>,
    /// `app_user.public_id`.
    pub public_id: Option<String>,
    /// `app_user.email`.
    pub email: Option<String>,
    /// `app_user.is_staff`.
    pub is_staff: bool,
}

impl Principal {
    /// `ANONYMOUS`.
    #[must_use]
    pub fn anonymous() -> Self {
        Self {
            kind: PrincipalKind::Anonymous,
            user_id: None,
            public_id: None,
            email: None,
            is_staff: false,
        }
    }

    /// `is_authenticated`.
    #[must_use]
    pub fn is_authenticated(&self) -> bool {
        self.kind != PrincipalKind::Anonymous
    }

    /// `require_user()`: the caller's `app_user.id`, or a 401.
    ///
    /// # Errors
    /// `unauthenticated()`.
    pub fn require_user(&self) -> Result<i64, Problem> {
        self.user_id.ok_or_else(unauthenticated_default)
    }
}

/// `require_authenticated`: for routes with no anonymous behaviour at all.
///
/// # Errors
/// `unauthenticated()` for an anonymous caller.
pub fn require_authenticated(principal: &Principal) -> Result<&Principal, Problem> {
    if principal.is_authenticated() {
        Ok(principal)
    } else {
        Err(unauthenticated_default())
    }
}

/// `require_staff`: 401 for anonymous, **404** (not 403) for a signed-in non-staff caller.
///
/// # Errors
/// As described.
pub fn require_staff(principal: &Principal) -> Result<&Principal, Problem> {
    if !principal.is_authenticated() {
        return Err(unauthenticated_default());
    }
    if !principal.is_staff {
        logging::warning(
            "baskfy_api.auth",
            "non-staff principal refused an admin route",
            &[("public_id", principal.public_id.as_deref().unwrap_or(""))],
        );
        return Err(Problem::new(ProblemType::NotFound, "Not found."));
    }
    Ok(principal)
}

/// `_bearer_token`: the token after `Bearer ` (case-sensitive prefix, Python-`strip`ped), or
/// `None` — which makes the request anonymous.
#[must_use]
pub fn bearer_token(authorization: Option<&str>) -> Option<String> {
    let header = authorization?;
    let rest = header.strip_prefix(BEARER_PREFIX)?;
    let token = rest.trim_matches(|c: char| {
        c.is_whitespace() || c == '\x1c' || c == '\x1d' || c == '\x1e' || c == '\x1f'
    });
    (!token.is_empty()).then(|| token.to_owned())
}

/// Why a token was refused, before it becomes the uniform 401.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TokenError {
    /// A PyJWT `PyJWTError`: logged by class name, answered with "not valid".
    Invalid(&'static str),
    /// A non-PyJWT exception PyJWT lets escape (`int()` of a list claim): a 500 in Python.
    Internal(&'static str),
}

/// `base64url_decode` with `base64.urlsafe_b64decode`'s leniency: padding is added, characters
/// outside the alphabet are discarded (binascii's non-strict mode).
fn b64url(segment: &str) -> Result<Vec<u8>, ()> {
    let cleaned: String = segment
        .chars()
        .filter_map(|c| match c {
            '-' => Some('+'),
            '_' => Some('/'),
            c if c.is_ascii_alphanumeric() || c == '+' || c == '/' => Some(c),
            _ => None,
        })
        .collect();
    let engine = base64::engine::GeneralPurpose::new(
        &base64::alphabet::STANDARD,
        base64::engine::GeneralPurposeConfig::new()
            .with_decode_padding_mode(base64::engine::DecodePaddingMode::Indifferent)
            .with_decode_allow_trailing_bits(true),
    );
    engine.decode(cleaned).map_err(|_| ())
}

/// Python's `int(x)` for a JSON claim value: ints, floats (truncated), bools and integer
/// strings. `Err(true)` = `ValueError` (PyJWT turns it into a 401), `Err(false)` = `TypeError`
/// or `OverflowError` (escapes PyJWT: a 500).
fn py_int(value: &OrderedJson) -> Result<i128, bool> {
    match value {
        OrderedJson::Int(i) => Ok(i128::from(*i)),
        OrderedJson::UInt(u) => Ok(i128::from(*u)),
        // Python's int is unbounded; beyond i128 only the sign decides any comparison here.
        OrderedJson::BigInt(big) => Ok(big.parse::<i128>().unwrap_or(if big.starts_with('-') {
            i128::MIN
        } else {
            i128::MAX
        })),
        OrderedJson::Bool(b) => Ok(i128::from(*b)),
        OrderedJson::Float(f) if f.is_finite() => {
            let t = f.trunc();
            if t.abs() < 1.0e38 {
                #[allow(clippy::cast_possible_truncation)]
                Ok(t as i128)
            } else {
                Err(false)
            }
        }
        OrderedJson::Float(_) => Err(false),
        OrderedJson::Str(s) => {
            let t = s.trim();
            let body = t.strip_prefix(['+', '-']).unwrap_or(t);
            let ok = !body.is_empty()
                && !body.starts_with('_')
                && !body.ends_with('_')
                && !body.contains("__")
                && body.chars().all(|c| c.is_ascii_digit() || c == '_');
            if !ok {
                return Err(true);
            }
            t.replace('_', "").parse::<i128>().map_err(|_| true)
        }
        _ => Err(false),
    }
}

fn truthy(value: &OrderedJson) -> bool {
    match value {
        OrderedJson::Null => false,
        OrderedJson::Bool(b) => *b,
        OrderedJson::Int(i) => *i != 0,
        OrderedJson::UInt(u) => *u != 0,
        OrderedJson::BigInt(_) => true,
        OrderedJson::Float(f) => *f != 0.0,
        OrderedJson::Str(s) => !s.is_empty(),
        OrderedJson::Array(a) => !a.is_empty(),
        OrderedJson::Object(o) => !o.is_empty(),
    }
}

/// `jwt.decode(token, secret, algorithms=["HS256"], audience=, issuer=, leeway=,
/// options={"require": ["exp", "iat", "sub"]})` at wall-clock `now` (Unix seconds).
///
/// # Errors
/// [`TokenError`] naming PyJWT's exception class.
pub fn pyjwt_decode(
    token: &str,
    secret: &[u8],
    audience: Option<&str>,
    issuer: Option<&str>,
    leeway: f64,
    now: f64,
) -> Result<Vec<(String, OrderedJson)>, TokenError> {
    pyjwt_decode_requiring(
        token,
        secret,
        audience,
        issuer,
        leeway,
        now,
        &["exp", "iat", "sub"],
    )
}

/// [`pyjwt_decode`] with `options={"require": required}` — `routers.auth._restore_code_valid`
/// requires `["exp", "sub", "purpose"]`.
///
/// # Errors
/// [`TokenError`] naming PyJWT's exception class.
pub fn pyjwt_decode_requiring(
    token: &str,
    secret: &[u8],
    audience: Option<&str>,
    issuer: Option<&str>,
    leeway: f64,
    now: f64,
    required: &[&str],
) -> Result<Vec<(String, OrderedJson)>, TokenError> {
    let invalid = TokenError::Invalid;
    let (signing_input, crypto) = token.rsplit_once('.').ok_or(invalid("DecodeError"))?;
    let (header_seg, payload_seg) = signing_input
        .split_once('.')
        .ok_or(invalid("DecodeError"))?;
    let header_bytes = b64url(header_seg).map_err(|()| invalid("DecodeError"))?;
    let header_text = std::str::from_utf8(&header_bytes).map_err(|_| invalid("DecodeError"))?;
    let header = OrderedJson::parse(header_text).map_err(|_| invalid("DecodeError"))?;
    let OrderedJson::Object(_) = header else {
        return Err(invalid("DecodeError"));
    };
    let detached = header.get("b64") == Some(&OrderedJson::Bool(false));
    let payload_bytes = if detached {
        if !payload_seg.is_empty() {
            return Err(invalid("DecodeError"));
        }
        Vec::new()
    } else {
        b64url(payload_seg).map_err(|()| invalid("DecodeError"))?
    };
    let signature = b64url(crypto).map_err(|()| invalid("DecodeError"))?;
    // _validate_headers: kid must be a string; crit a non-empty list of supported, present names.
    if let Some(kid) = header.get("kid")
        && !matches!(kid, OrderedJson::Str(_))
    {
        return Err(invalid("InvalidTokenError"));
    }
    if let Some(crit) = header.get("crit") {
        let OrderedJson::Array(items) = crit else {
            return Err(invalid("InvalidTokenError"));
        };
        if items.is_empty() {
            return Err(invalid("InvalidTokenError"));
        }
        for ext in items {
            let OrderedJson::Str(name) = ext else {
                return Err(invalid("InvalidTokenError"));
            };
            if name != "b64" || header.get(name).is_none() {
                return Err(invalid("InvalidTokenError"));
            }
        }
    }
    if detached {
        // A b64=false token needs a detached payload, which the API never passes.
        return Err(invalid("DecodeError"));
    }
    match header.get("alg") {
        None => return Err(invalid("InvalidAlgorithmError")),
        Some(OrderedJson::Str(alg)) if alg == JWT_ALGORITHM => {}
        Some(_) => return Err(invalid("InvalidAlgorithmError")),
    }
    let mut mac = Hmac::<Sha256>::new_from_slice(secret).map_err(|_| invalid("InvalidKeyError"))?;
    mac.update(signing_input.as_bytes());
    mac.verify_slice(&signature)
        .map_err(|_| invalid("InvalidSignatureError"))?;

    let payload_text = std::str::from_utf8(&payload_bytes).map_err(|_| invalid("DecodeError"))?;
    let payload = OrderedJson::parse(payload_text).map_err(|_| invalid("DecodeError"))?;
    let OrderedJson::Object(claims) = payload.clone() else {
        return Err(invalid("DecodeError"));
    };
    for required in required {
        if matches!(payload.get(required), None | Some(OrderedJson::Null)) {
            return Err(invalid("MissingRequiredClaimError"));
        }
    }
    let to_int = |v: &OrderedJson, class: &'static str| -> Result<i128, TokenError> {
        py_int(v).map_err(|value_error| {
            if value_error {
                TokenError::Invalid(class)
            } else {
                TokenError::Internal("TypeError")
            }
        })
    };
    #[allow(clippy::cast_precision_loss)]
    if let Some(iat) = payload.get("iat") {
        let iat = to_int(iat, "InvalidIssuedAtError")?;
        if (iat as f64) > now + leeway {
            return Err(invalid("ImmatureSignatureError"));
        }
    }
    #[allow(clippy::cast_precision_loss)]
    if let Some(nbf) = payload.get("nbf") {
        let nbf = to_int(nbf, "DecodeError")?;
        if (nbf as f64) > now + leeway {
            return Err(invalid("ImmatureSignatureError"));
        }
    }
    #[allow(clippy::cast_precision_loss)]
    if let Some(exp) = payload.get("exp") {
        let exp = to_int(exp, "DecodeError")?;
        if (exp as f64) <= now - leeway {
            return Err(invalid("ExpiredSignatureError"));
        }
    }
    if let Some(expected) = issuer {
        match payload.get("iss") {
            None => return Err(invalid("MissingRequiredClaimError")),
            Some(OrderedJson::Str(iss)) if iss == expected => {}
            Some(_) => return Err(invalid("InvalidIssuerError")),
        }
    }
    let aud = payload.get("aud");
    match audience {
        None => {
            if aud.is_some_and(truthy) {
                return Err(invalid("InvalidAudienceError"));
            }
        }
        Some(expected) => {
            let Some(claim) = aud.filter(|a| truthy(a)) else {
                return Err(invalid("MissingRequiredClaimError"));
            };
            let names: Vec<&OrderedJson> = match claim {
                OrderedJson::Array(items) => items.iter().collect(),
                single @ OrderedJson::Str(_) => vec![single],
                _ => return Err(invalid("InvalidAudienceError")),
            };
            if names.iter().any(|n| !matches!(n, OrderedJson::Str(_))) {
                return Err(invalid("InvalidAudienceError"));
            }
            if !names
                .iter()
                .any(|n| matches!(n, OrderedJson::Str(s) if s == expected))
            {
                return Err(invalid("InvalidAudienceError"));
            }
        }
    }
    if let Some(sub) = payload.get("sub")
        && !matches!(sub, OrderedJson::Str(_))
    {
        return Err(invalid("InvalidSubjectError"));
    }
    if let Some(jti) = payload.get("jti")
        && !matches!(jti, OrderedJson::Str(_))
    {
        return Err(invalid("InvalidJTIError"));
    }
    Ok(claims)
}

/// Wall-clock Unix seconds (PyJWT's `datetime.now(tz=utc).timestamp()`).
#[must_use]
pub fn unix_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// `decode_token(token, settings)`: verify, then refuse an over-long lifetime.
///
/// # Errors
/// The 401 problem Python raises (or a 500 for the exceptions PyJWT lets escape).
pub fn decode_token(
    token: &str,
    settings: &Settings,
) -> Result<Vec<(String, OrderedJson)>, Problem> {
    if !settings.jwt_secret.is_set() {
        return Err(unauthenticated(
            "This deployment cannot verify bearer tokens.",
        ));
    }
    #[allow(clippy::cast_precision_loss)]
    let leeway = settings.jwt_leeway_seconds as f64;
    let claims = match pyjwt_decode(
        token,
        settings.jwt_secret.expose().as_bytes(),
        settings.jwt_audience.as_deref(),
        settings.jwt_issuer.as_deref(),
        leeway,
        unix_now(),
    ) {
        Ok(c) => c,
        Err(TokenError::Invalid(class)) => {
            logging::info("baskfy_api.auth", "token rejected", &[("reason", class)]);
            return Err(unauthenticated("The bearer token is not valid."));
        }
        Err(TokenError::Internal(class)) => {
            logging::error(
                "baskfy_api.app",
                "unhandled error",
                &[("error_class", class)],
            );
            return Err(internal_error());
        }
    };
    let get = |k: &str| claims.iter().find(|(key, _)| key == k).map(|(_, v)| v);
    // `isinstance(x, int)` is true of a bool too.
    let as_int = |v: Option<&OrderedJson>| match v {
        Some(OrderedJson::Int(i)) => Some(i128::from(*i)),
        Some(OrderedJson::UInt(u)) => Some(i128::from(*u)),
        Some(OrderedJson::BigInt(big)) => {
            Some(big.parse::<i128>().unwrap_or(if big.starts_with('-') {
                i128::MIN
            } else {
                i128::MAX
            }))
        }
        Some(OrderedJson::Bool(b)) => Some(i128::from(*b)),
        _ => None,
    };
    if let (Some(iat), Some(exp)) = (as_int(get("iat")), as_int(get("exp"))) {
        let lifetime = exp - iat;
        if lifetime > i128::from(settings.jwt_max_lifetime_seconds) {
            logging::warning(
                "baskfy_api.auth",
                "token lifetime too long",
                &[("lifetime_seconds", &lifetime.to_string())],
            );
            return Err(unauthenticated(
                "The bearer token's lifetime exceeds the accepted maximum.",
            ));
        }
    }
    Ok(claims)
}

/// `_token_epoch`: the `epoch` claim when it is an int (not a bool), else 0.
#[must_use]
pub fn token_epoch(claims: &[(String, OrderedJson)]) -> i128 {
    match claims.iter().find(|(k, _)| k == "epoch").map(|(_, v)| v) {
        Some(OrderedJson::Int(i)) => i128::from(*i),
        Some(OrderedJson::UInt(u)) => i128::from(*u),
        Some(OrderedJson::BigInt(big)) => big.parse::<i128>().unwrap_or(if big.starts_with('-') {
            i128::MIN
        } else {
            i128::MAX
        }),
        _ => 0,
    }
}

/// The `app_user` columns a principal needs (`_load_user`).
#[derive(Debug, Clone)]
struct UserRow {
    id: i64,
    public_id: String,
    email: String,
    is_staff: bool,
    session_epoch: i64,
}

/// `current_principal(request, session)`: anonymous without a bearer, otherwise the verified,
/// active, epoch-current user.
///
/// # Errors
/// The 401 problems Python raises; a database failure is the 500 catch-all.
pub async fn current_principal(
    authorization: Option<&str>,
    settings: &Settings,
    db: &Db,
) -> Result<Principal, Problem> {
    let Some(token) = bearer_token(authorization) else {
        return Ok(Principal::anonymous());
    };
    let claims = decode_token(&token, settings)?;
    let subject = match claims.iter().find(|(k, _)| k == "sub").map(|(_, v)| v) {
        Some(OrderedJson::Str(s)) if !s.is_empty() => s.clone(),
        _ => return Err(unauthenticated("The bearer token has no subject.")),
    };
    let row = sqlx::query_as::<_, (i64, String, String, bool, i64)>(
        "SELECT id, public_id, email::text, is_staff, session_epoch::bigint FROM app_user \
         WHERE public_id = $1 AND deleted_at IS NULL",
    )
    .bind(&subject)
    .fetch_optional(db.pool())
    .await
    .map_err(|e| crate::db_failure(&e.into()))?;
    let Some((id, public_id, email, is_staff, session_epoch)) = row else {
        logging::info(
            "baskfy_api.auth",
            "token for unknown subject",
            &[("subject", &subject)],
        );
        return Err(unauthenticated(
            "The bearer token refers to an unknown account.",
        ));
    };
    let user = UserRow {
        id,
        public_id,
        email,
        is_staff,
        session_epoch,
    };
    let epoch = token_epoch(&claims);
    if i128::from(user.session_epoch) > epoch {
        logging::info(
            "baskfy_api.auth",
            "token epoch stale",
            &[
                ("subject", &subject),
                ("token_epoch", &epoch.to_string()),
                ("user_epoch", &user.session_epoch.to_string()),
            ],
        );
        return Err(unauthenticated("The bearer token has been revoked."));
    }
    Ok(Principal {
        kind: PrincipalKind::User,
        user_id: Some(user.id),
        public_id: Some(user.public_id),
        email: Some(user.email),
        is_staff: user.is_staff,
    })
}

/// `encode_token(...)` without an audience or issuer: what the web app and the parity harness
/// mint.
#[must_use]
pub fn encode_token(
    subject: &str,
    secret: &[u8],
    issued_at: i64,
    lifetime_seconds: i64,
    epoch: i64,
) -> String {
    encode_token_full(
        subject,
        secret,
        issued_at,
        lifetime_seconds,
        None,
        None,
        epoch,
    )
}

/// `encode_token(subject, secret, lifetime_seconds=, issued_at=, audience=, issuer=, epoch=)`:
/// the claims in Python's dict order (`sub`, `iat`, `exp`, `epoch`, then `aud`, `iss` when set),
/// signed as `jwt.encode` signs them. `issue_session` mints the access token with it.
#[must_use]
pub fn encode_token_full(
    subject: &str,
    secret: &[u8],
    issued_at: i64,
    lifetime_seconds: i64,
    audience: Option<&str>,
    issuer: Option<&str>,
    epoch: i64,
) -> String {
    let mut claims = vec![
        ("sub".to_owned(), OrderedJson::Str(subject.to_owned())),
        ("iat".to_owned(), OrderedJson::Int(issued_at)),
        (
            "exp".to_owned(),
            OrderedJson::Int(issued_at + lifetime_seconds),
        ),
        ("epoch".to_owned(), OrderedJson::Int(epoch)),
    ];
    if let Some(aud) = audience {
        claims.push(("aud".to_owned(), OrderedJson::Str(aud.to_owned())));
    }
    if let Some(iss) = issuer {
        claims.push(("iss".to_owned(), OrderedJson::Str(iss.to_owned())));
    }
    sign_hs256(&claims, secret)
}

/// `jwt.encode(claims, secret, algorithm="HS256")`: the header `{"alg":"HS256","typ":"JWT"}`
/// (PyJWT sorts it), the payload `json.dumps(claims, separators=(",", ":"))` in the dict's order
/// with `ensure_ascii`, HMAC-SHA256 over both.
#[must_use]
pub fn sign_hs256(claims: &[(String, OrderedJson)], secret: &[u8]) -> String {
    let engine = base64::engine::general_purpose::URL_SAFE_NO_PAD;
    let header = engine.encode(br#"{"alg":"HS256","typ":"JWT"}"#);
    let json = OrderedJson::Object(claims.to_vec())
        .to_python_json()
        .unwrap_or_default();
    // `ensure_ascii=True`: every non-ASCII character as `\uXXXX` (surrogate pairs above U+FFFF).
    let mut ascii = String::with_capacity(json.len());
    for c in json.chars() {
        if c.is_ascii() {
            ascii.push(c);
        } else {
            let mut units = [0u16; 2];
            for u in c.encode_utf16(&mut units) {
                ascii.push_str(&format!("\\u{:04x}", u));
            }
        }
    }
    let payload = engine.encode(ascii);
    let signing_input = format!("{header}.{payload}");
    let signature = Hmac::<Sha256>::new_from_slice(secret)
        .map(|mut mac| {
            mac.update(signing_input.as_bytes());
            mac.finalize().into_bytes().to_vec()
        })
        .unwrap_or_default();
    format!("{signing_input}.{}", engine.encode(signature))
}
