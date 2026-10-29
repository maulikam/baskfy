//! pydantic's `EmailStr`: `pydantic.networks.validate_email` over `email_validator` 2.3's
//! `validate_email(email, check_deliverability=False)`, for the bodies of `DELETE /me` and
//! `POST /me/restore` (`schemas.Email`).
//!
//! **ASCII only** (DECISIONS-RS R6.7). Every ASCII input is answered exactly as Python answers it
//! — the accepted address normalised the same way (`Info@Example.COM` → `info@example.com`), or
//! the same `value is not a valid email address: <reason>` sentence — and `tests/email_vectors.rs`
//! holds the cases, each run through pydantic 2.13 + email-validator 2.3 first. What needs Unicode
//! tables or IDNA is not reproduced: an input with any non-ASCII character, and an ASCII domain
//! with a Punycode (`xn--`) label, are refused with [`UNSUPPORTED`] where Python may accept them
//! (`é@b.co`) or give another sentence. Both routes compare the address with the account's own,
//! so such an input can only ever be refused there too; the body differs.

/// The reason given for an address this port does not validate (see the module docs).
pub const UNSUPPORTED: &str =
    "The email address contains characters this deployment does not validate yet.";

/// `pydantic.networks.MAX_EMAIL_LENGTH`.
const PYDANTIC_MAX_LENGTH: usize = 2048;
/// `rfc_constants.EMAIL_MAX_LENGTH`.
const EMAIL_MAX_LENGTH: usize = 254;
/// `rfc_constants.DOMAIN_MAX_LENGTH`.
const DOMAIN_MAX_LENGTH: usize = 253;
/// `rfc_constants.DNS_LABEL_LENGTH_LIMIT`.
const DNS_LABEL_LENGTH_LIMIT: usize = 63;
/// `rfc_constants.CASE_INSENSITIVE_MAILBOX_NAMES`.
const CASE_INSENSITIVE_MAILBOX_NAMES: [&str; 15] = [
    "info",
    "marketing",
    "sales",
    "support",
    "abuse",
    "noc",
    "security",
    "postmaster",
    "hostmaster",
    "usenet",
    "news",
    "webmaster",
    "www",
    "uucp",
    "ftp",
];
/// `email_validator.SPECIAL_USE_DOMAIN_NAMES`.
const SPECIAL_USE_DOMAIN_NAMES: [&str; 6] =
    ["arpa", "invalid", "local", "localhost", "onion", "test"];

type Reason = String;

/// `ATEXT` (RFC 5322 atext, ASCII).
fn is_atext(c: char) -> bool {
    c.is_ascii_alphanumeric() || "_!#$%&'*+-/=?^`{|}~".contains(c)
}

/// `str.strip()` over ASCII: whitespace plus the four separators Python also strips.
fn is_py_space(c: char) -> bool {
    matches!(
        c,
        ' ' | '\t' | '\n' | '\r' | '\x0b' | '\x0c' | '\x1c'..='\x1f'
    )
}

/// `unicodedata.category(c)` reduced to what the checks ask of an ASCII character.
fn category(c: char) -> &'static str {
    if c.is_ascii_alphabetic() {
        "L"
    } else if c.is_ascii_digit() {
        "N"
    } else if c == ' ' {
        "Zs"
    } else if c.is_ascii_control() {
        "Cc"
    } else if "$+<=>^`|~".contains(c) {
        "S"
    } else {
        "P"
    }
}

/// `repr(c)` for one printable ASCII character.
fn repr_char(c: char) -> String {
    match c {
        '\'' => "\"'\"".to_owned(),
        '\\' => "'\\\\'".to_owned(),
        _ => format!("'{c}'"),
    }
}

/// `safe_character_display(c)`.
fn safe_display(c: char) -> String {
    if c == '\\' {
        return "\"\\\"".to_owned();
    }
    match category(c) {
        "L" | "N" | "P" | "S" => repr_char(c),
        // `unicodedata.name(c, ...)`: SPACE is named; ASCII controls have no name.
        _ if c == ' ' => "SPACE".to_owned(),
        _ => format!("U+{:04X}", u32::from(c)),
    }
}

/// `", ".join(sorted({safe_character_display(c) for c in ... if bad(c)}))`.
fn bad_chars(text: &str, bad: impl Fn(char) -> bool) -> Option<String> {
    let mut shown: Vec<String> = text.chars().filter(|c| bad(*c)).map(safe_display).collect();
    if shown.is_empty() {
        return None;
    }
    shown.sort();
    shown.dedup();
    Some(shown.join(", "))
}

/// `get_length_reason(addr, limit)`.
fn length_reason(len: usize, limit: usize) -> String {
    let diff = len - limit;
    format!(
        "({diff} character{} too many)",
        if diff > 1 { "s" } else { "" }
    )
}

/// `check_unsafe_chars(s, allow_space=)` over ASCII: controls, and spaces unless allowed.
fn check_unsafe(s: &str, allow_space: bool) -> Result<(), Reason> {
    let mut bad: Vec<char> = s
        .chars()
        .filter(|&c| match category(c) {
            "Zs" => !allow_space,
            "Cc" => true,
            _ => false,
        })
        .collect();
    if bad.is_empty() {
        return Ok(());
    }
    bad.sort_unstable();
    bad.dedup();
    let shown: Vec<String> = bad.into_iter().map(safe_display).collect();
    Err(format!(
        "The email address contains unsafe characters: {}.",
        shown.join(", ")
    ))
}

/// `check_dot_atom(label, start_descr, end_descr, is_hostname)`.
fn check_dot_atom(label: &str, start: &str, end: &str, hostname: bool) -> Result<(), Reason> {
    if label.ends_with('.') {
        return Err(end.replace("{}", "period"));
    }
    if label.starts_with('.') {
        return Err(start.replace("{}", "period"));
    }
    if label.contains("..") {
        return Err("An email address cannot have two periods in a row.".to_owned());
    }
    if hostname {
        if label.ends_with('-') {
            return Err(end.replace("{}", "hyphen"));
        }
        if label.starts_with('-') {
            return Err(start.replace("{}", "hyphen"));
        }
        if label.contains(".-") || label.contains("-.") {
            return Err(
                "An email address cannot have a period and a hyphen next to each other.".to_owned(),
            );
        }
    }
    Ok(())
}

/// `DOT_ATOM_TEXT.match(local)` (anchored at both ends).
fn is_dot_atom_text(s: &str) -> bool {
    !s.is_empty()
        && s.split('.')
            .all(|part| !part.is_empty() && part.chars().all(is_atext))
}

/// `DOT_ATOM_TEXT_HOSTNAME.match(domain)`.
fn is_hostname_text(s: &str) -> bool {
    s.split('.').all(|label| {
        let b = label.as_bytes();
        !b.is_empty()
            && b.iter().all(|c| c.is_ascii_alphanumeric() || *c == b'-')
            && b.first().is_some_and(u8::is_ascii_alphanumeric)
            && b.last().is_some_and(u8::is_ascii_alphanumeric)
    })
}

/// `split_string_at_unquoted_special(text, specials)`.
fn split_at_unquoted<'a>(text: &'a str, specials: &[char]) -> Result<(&'a str, &'a str), Reason> {
    let mut inside_quote = false;
    let mut escaped = false;
    for (i, c) in text.char_indices() {
        if inside_quote {
            if c == '\\' && !escaped {
                escaped = true;
            } else if c == '"' && !escaped {
                inside_quote = false;
                escaped = false;
            } else {
                escaped = false;
            }
        } else if c == '"' {
            inside_quote = true;
        } else if specials.contains(&c) {
            return Ok((&text[..i], &text[i..]));
        }
    }
    Err("An email address must have an @-sign.".to_owned())
}

/// `unquote_quoted_string(text)`: the value and whether it was quoted.
fn unquote(text: &str) -> Result<(String, bool), Reason> {
    let mut quoted = false;
    let mut escaped = false;
    let mut value = String::new();
    let chars: Vec<char> = text.chars().collect();
    for (i, &c) in chars.iter().enumerate() {
        if quoted {
            if escaped {
                value.push(c);
                escaped = false;
            } else if c == '\\' {
                escaped = true;
            } else if c == '"' {
                if i != chars.len() - 1 {
                    let rest: Vec<String> =
                        chars[i + 1..].iter().map(|c| safe_display(*c)).collect();
                    return Err(format!(
                        "Extra character(s) found after close quote: {}",
                        rest.join(", ")
                    ));
                }
                break;
            } else {
                value.push(c);
            }
        } else if i == 0 && c == '"' {
            quoted = true;
        } else {
            value.push(c);
        }
    }
    Ok((value, quoted))
}

/// `split_email(email)`: display name, local part, domain part, whether the local part was quoted.
fn split_email(email: &str) -> Result<(Option<String>, String, String, bool), Reason> {
    let (left, right) = split_at_unquoted(email, &['@', '<'])?;
    let (display, local, domain) = if right.starts_with('<') {
        let (display, display_quoted) = unquote(left.trim_end_matches(is_py_space))?;
        if !display_quoted
            && let Some(bad) = bad_chars(&display, |c| {
                (!(is_atext(c) || c == '.') && c != ' ') || c == '.'
            })
        {
            return Err(format!(
                "The display name contains invalid characters when not quoted: {bad}."
            ));
        }
        check_unsafe(&display, true)?;
        if !right.contains('>') {
            return Err("An open angle bracket at the start of the email address has to be followed by a close angle bracket at the end.".to_owned());
        }
        let right = right.trim_end_matches(' ');
        if !right.ends_with('>') {
            return Err("There can't be anything after the email address.".to_owned());
        }
        let spec = right[1..].trim_end_matches('>');
        let (local, domain) = split_at_unquoted(spec, &['@'])?;
        (Some(display), local, domain)
    } else {
        (None, left, right)
    };
    let domain = domain.strip_prefix('@').unwrap_or(domain);
    let (local, quoted) = unquote(local)?;
    Ok((display, local, domain.to_owned(), quoted))
}

/// `validate_email_local_part(local, allow_smtputf8=True, allow_empty_local=False, ...)`: the
/// local part as it will be stored.
fn validate_local(local: &str, quoted: bool) -> Result<String, Reason> {
    if local.is_empty() {
        return Err("There must be something before the @-sign.".to_owned());
    }
    if is_dot_atom_text(local) {
        return Ok(local.to_owned());
    }
    if quoted {
        if let Some(bad) = bad_chars(local, |c| !(' '..='~').contains(&c)) {
            return Err(format!(
                "The email address contains invalid characters in quotes before the @-sign: {bad}."
            ));
        }
        check_unsafe(local, true)?;
        let mut requoted = String::from("\"");
        for c in local.chars() {
            if c == '"' || c == '\\' {
                requoted.push('\\');
            }
            requoted.push(c);
        }
        requoted.push('"');
        return Ok(requoted);
    }
    if let Some(bad) = bad_chars(local, |c| !(is_atext(c) || c == '.')) {
        return Err(format!(
            "The email address contains invalid characters before the @-sign: {bad}."
        ));
    }
    check_dot_atom(
        local,
        "An email address cannot start with a {}.",
        "An email address cannot have a {} immediately before the @-sign.",
        false,
    )?;
    Err("The email address contains invalid characters before the @-sign.".to_owned())
}

/// `ipaddress.IPv4Address(text)`'s error, or `None` when it parses.
fn ipv4_error(text: &str) -> Option<String> {
    let quoted = format!("'{text}'");
    if text.is_empty() {
        return Some("Address cannot be empty".to_owned());
    }
    let octets: Vec<&str> = text.split('.').collect();
    if octets.len() != 4 {
        return Some(format!("Expected 4 octets in {quoted}"));
    }
    for octet in octets {
        let problem = if octet.is_empty() {
            Some("Empty octet not permitted".to_owned())
        } else if !octet.bytes().all(|b| b.is_ascii_digit()) {
            Some(format!("Only decimal digits permitted in '{octet}'"))
        } else if octet.len() > 3 {
            Some(format!("At most 3 characters permitted in '{octet}'"))
        } else if octet != "0" && octet.starts_with('0') {
            Some(format!("Leading zeros are not permitted in '{octet}'"))
        } else {
            match octet.parse::<u32>() {
                Ok(v) if v > 255 => Some(format!("Octet {v} (> 255) not permitted")),
                _ => None,
            }
        };
        if let Some(p) = problem {
            return Some(format!("{p} in {quoted}"));
        }
    }
    None
}

/// `validate_email_domain_literal(literal)` — the literal is checked, then refused
/// (`allow_domain_literal` is False).
fn validate_literal(literal: &str) -> Result<(), Reason> {
    if !literal.is_empty() && literal.bytes().all(|b| b.is_ascii_digit() || b == b'.') {
        if let Some(e) = ipv4_error(literal) {
            return Err(format!(
                "The address in brackets after the @-sign is not valid: It is not an IPv4 address ({e}) or is missing an address literal tag."
            ));
        }
        return Ok(());
    }
    if let Some(v6) = literal.strip_prefix("IPv6:") {
        // `ipaddress.IPv6Address`'s own sentences are not reproduced (R6.7): a valid address is
        // refused below as Python refuses it; an invalid one gets the unsupported sentence.
        return if v6.parse::<std::net::Ipv6Addr>().is_ok() {
            Ok(())
        } else {
            Err(UNSUPPORTED.to_owned())
        };
    }
    if !literal.contains(':') {
        return Err("The part after the @-sign in brackets is not an IPv4 address and has no address literal tag.".to_owned());
    }
    if let Some(bad) = bad_chars(literal, |c| !('\u{21}'..='\u{FA}').contains(&c)) {
        return Err(format!(
            "The part after the @-sign contains invalid characters in brackets: {bad}."
        ));
    }
    Err("The part after the @-sign contains an invalid address literal tag in brackets.".to_owned())
}

/// `validate_email_domain_name(domain, test_environment=False, globally_deliverable=True)`: the
/// domain as it will be stored (lower-cased, as UTS-46 maps ASCII).
fn validate_domain(domain: &str) -> Result<String, Reason> {
    if let Some(bad) = bad_chars(domain, |c| {
        !(c.is_ascii_alphanumeric() || c == '-' || c == '.')
    }) {
        return Err(format!(
            "The part after the @-sign contains invalid characters: {bad}."
        ));
    }
    let original = domain;
    let domain = domain.to_ascii_lowercase();
    check_dot_atom(
        &domain,
        "An email address cannot have a {} immediately after the @-sign.",
        "An email address cannot end with a {}.",
        true,
    )?;
    for label in domain.split('.') {
        let b = label.as_bytes();
        if b.len() >= 4 && &b[2..4] == b"--" && !(b[0] == b'x' && b[1] == b'n') {
            return Err("An email address cannot have two letters followed by two dashes immediately after the @-sign or after a period, except Punycode.".to_owned());
        }
    }
    if !is_hostname_text(&domain) {
        return Err(UNSUPPORTED.to_owned());
    }
    if domain.len() > DOMAIN_MAX_LENGTH {
        if domain == original {
            return Err(format!(
                "The email address is too long after the @-sign {}.",
                length_reason(domain.len(), DOMAIN_MAX_LENGTH)
            ));
        }
        let diff = domain.len() - DOMAIN_MAX_LENGTH;
        return Err(format!(
            "The email address is too long after the @-sign ({diff} byte{} too many after IDNA encoding).",
            if diff == 1 { "" } else { "s" }
        ));
    }
    for label in domain.split('.') {
        if label.len() > DNS_LABEL_LENGTH_LIMIT {
            return Err(format!(
                "After the @-sign, periods cannot be separated by so many characters {}.",
                length_reason(label.len(), DNS_LABEL_LENGTH_LIMIT)
            ));
        }
    }
    if !domain.contains('.') {
        return Err("The part after the @-sign is not valid. It should have a period.".to_owned());
    }
    if !domain.ends_with(|c: char| c.is_ascii_alphabetic()) {
        return Err(
            "The part after the @-sign is not valid. It is not within a valid top-level domain."
                .to_owned(),
        );
    }
    for d in SPECIAL_USE_DOMAIN_NAMES {
        if domain == d || domain.ends_with(&format!(".{d}")) {
            return Err("The part after the @-sign is a special-use or reserved name that cannot be used with email.".to_owned());
        }
    }
    // `idna.decode`: a Punycode label needs IDNA 2008, which is not ported (R6.7).
    if domain.split('.').any(|l| l.starts_with("xn--")) {
        return Err(UNSUPPORTED.to_owned());
    }
    Ok(domain)
}

/// `email_validator.validate_email(email, check_deliverability=False).normalized`.
fn validate(email: &str) -> Result<String, Reason> {
    let (display, local, domain, quoted) = split_email(email)?;
    let original = format!(
        "{}@{domain}",
        if quoted {
            format!("\"{local}\"")
        } else {
            local.clone()
        }
    );
    let mut local = validate_local(&local, quoted)?;
    if quoted {
        return Err("Quoting the part before the @-sign is not allowed here.".to_owned());
    }
    if CASE_INSENSITIVE_MAILBOX_NAMES.contains(&local.to_ascii_lowercase().as_str()) {
        local = local.to_ascii_lowercase();
    }
    if domain.is_empty() {
        return Err("There must be something after the @-sign.".to_owned());
    }
    let domain = if domain.starts_with('[') && domain.ends_with(']') && domain.len() >= 2 {
        validate_literal(&domain[1..domain.len() - 1])?;
        return Err("A bracketed IP address after the @-sign is not allowed here.".to_owned());
    } else {
        validate_domain(&domain)?
    };
    let normalized = format!("{local}@{domain}");
    for (addr, after) in [
        (&original, None),
        (&normalized, Some("after normalization")),
    ] {
        if addr.len() > EMAIL_MAX_LENGTH {
            let reason = match after {
                None => length_reason(addr.len(), EMAIL_MAX_LENGTH),
                Some(why) => {
                    let diff = addr.len() - EMAIL_MAX_LENGTH;
                    format!(
                        "{why} ({diff} byte{} too many)",
                        if diff > 1 { "s" } else { "" }
                    )
                }
            };
            return Err(format!("The email address is too long {reason}."));
        }
    }
    if display.is_some() {
        return Err(
            "A display name and angle brackets around the email address are not permitted here."
                .to_owned(),
        );
    }
    Ok(normalized)
}

/// pydantic's `_build_pretty_email_regex()`, anchored as `fullmatch` anchors it. `\s` is
/// spelled out because Python's (over `str`) also matches `\x1c`-`\x1f`; the input is ASCII, so
/// `\w` agrees.
static PRETTY: std::sync::LazyLock<Option<regex::Regex>> = std::sync::LazyLock::new(|| {
    let s = r"[\t\n\x0B\x0C\r\x1C-\x1F ]";
    let name = r"[\w!#$%&'*+\-/=?^_`{|}~]";
    regex::Regex::new(&format!(
        r#"\A{s}*(?:((?:{name}+{s}+)*{name}+)|"((?:[^"]|")+)")?{s}*<(.+)>{s}*\z"#
    ))
    .ok()
});

/// The address inside `Name <address>`, when the whole value has that shape.
fn pretty_inner(value: &str) -> Option<&str> {
    let re = PRETTY.as_ref()?;
    re.captures(value)?.get(3).map(|m| m.as_str())
}

/// `pydantic.networks.validate_email(value)[1]`: the normalised address, or pydantic's sentence.
///
/// # Errors
/// `value is not a valid email address: <reason>` (pydantic's `value_error` message).
pub fn validate_email_str(value: &str) -> Result<String, String> {
    let fail = |reason: &str| format!("value is not a valid email address: {reason}");
    if value.chars().count() > PYDANTIC_MAX_LENGTH {
        return Err(fail(&format!(
            "Length must not exceed {PYDANTIC_MAX_LENGTH} characters"
        )));
    }
    if !value.is_ascii() {
        return Err(fail(UNSUPPORTED));
    }
    let inner = pretty_inner(value).unwrap_or(value);
    let email = inner.trim_matches(is_py_space);
    validate(email).map_err(|r| fail(&r))
}
