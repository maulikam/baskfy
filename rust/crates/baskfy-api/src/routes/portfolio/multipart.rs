//! `await request.form()` for a route with one `UploadFile = File(...)` parameter, as Starlette 1.6
//! and python-multipart 0.0.32 read it, then FastAPI's `_get_multidict_value` for that field.
//!
//! * The request's `Content-Type` is read with python-multipart's `parse_options_header`: without
//!   a `;` the type is lower-cased and stripped, with parameters it is compared as sent (so
//!   `Multipart/Form-Data; boundary=…` is not multipart and the form is empty). Only
//!   `multipart/form-data` and `application/x-www-form-urlencoded` produce fields.
//! * A multipart body runs through python-multipart's `MultipartParser` state machine (ported
//!   below on the whole body at once): no preamble other than CR/LF, `--boundary` then CRLF, header
//!   names of token characters, header values up to CR then LF, at most 8 headers of at most 4,224
//!   bytes, data up to `\r\n--boundary`; a body that stops before the closing boundary finishes no
//!   part. A parse error is FastAPI's `400 There was an error parsing the body`; Starlette's own
//!   `MultiPartException`s (no boundary, no `name`, a field part over 1 MiB, more than 1,000 fields
//!   or 1,000 files) are a 400 with their message. Field names, filenames and text values are
//!   decoded with the `Content-Type`'s `charset` (`_user_safe_decode`: UTF-8 if it names it, else latin-1).
//! * A urlencoded body runs through `FormParser`: at most 1,000 fields, each name plus value at
//!   most 1 MiB (raw bytes), else the same 400s.
//! * A part with a `filename` parameter is a file (even `filename=""`); RFC 2231 `filename*` is
//!   ignored, so such a part is a text field. The last item named `file` wins (`FormData.get`).

use crate::problems::{Problem, framework};

/// What the form held under the field's name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum FormValue {
    /// An `UploadFile`: its filename and bytes.
    File {
        /// `UploadFile.filename`.
        filename: String,
        /// The part's data.
        data: Vec<u8>,
    },
    /// A text field.
    Text(String),
}

const MAX_BOUNDARY_LENGTH: usize = 256;
const MAX_HEADER_COUNT: usize = 8;
const MAX_HEADER_SIZE: usize = 4096 + 128;
const MAX_PART_SIZE: usize = 1024 * 1024;
/// Starlette's `max_fields` and `max_files` defaults for `request.form()`.
const MAX_FIELDS: usize = 1000;
const MAX_FILES: usize = 1000;

fn too_many_fields() -> Problem {
    framework(
        400,
        &format!("Too many fields. Maximum number of fields is {MAX_FIELDS}."),
    )
}

fn part_too_large() -> Problem {
    framework(
        400,
        &format!("Part exceeded maximum size of {}KB.", MAX_PART_SIZE / 1024),
    )
}

fn parse_error() -> Problem {
    framework(400, "There was an error parsing the body")
}

/// `parse_options_header(value)`: `(type, params)` with params as latin-1 strings.
pub(crate) fn parse_options_header(value: Option<&str>) -> (String, Vec<(String, String)>) {
    let Some(value) = value.filter(|v| !v.is_empty()) else {
        return (String::new(), Vec::new());
    };
    if !value.contains(';') {
        return (value.to_lowercase().trim().to_owned(), Vec::new());
    }
    let parts = parseparam(value);
    let mut iter = parts.into_iter();
    let ctype = iter.next().unwrap_or_default();
    let mut options = Vec::new();
    for segment in iter {
        let (key, val) = segment.split_once('=').unwrap_or((segment.as_str(), ""));
        if key.contains('*') {
            continue;
        }
        let mut val = val.to_owned();
        if val.len() >= 2 && val.starts_with('"') && val.ends_with('"') {
            val = val[1..val.len() - 1]
                .replace("\\\\", "\\")
                .replace("\\\"", "\"");
        }
        if key == "filename" && (val.get(1..3) == Some(":\\") || val.starts_with("\\\\")) {
            val = val.rsplit('\\').next().unwrap_or("").to_owned();
        }
        match options
            .iter_mut()
            .find(|(k, _): &&mut (String, String)| k == key)
        {
            Some(slot) => slot.1 = val,
            None => options.push((key.to_owned(), val)),
        }
    }
    (ctype, options)
}

/// `_parseparam`: `;`-separated parts, a `;` inside double quotes kept, keys lower-cased.
fn parseparam(s: &str) -> Vec<String> {
    let s: Vec<char> = format!(";{s}").chars().collect();
    let find = |c: char, from: usize| -> Option<usize> {
        s.iter().skip(from).position(|x| *x == c).map(|p| p + from)
    };
    let count = |pat: &[char], from: usize, to: usize| -> usize {
        if to <= from || to > s.len() {
            return 0;
        }
        let hay = &s[from..to];
        let mut n = 0;
        let mut i = 0;
        while i + pat.len() <= hay.len() {
            if &hay[i..i + pat.len()] == pat {
                n += 1;
                i += pat.len();
            } else {
                i += 1;
            }
        }
        n
    };
    let mut plist = Vec::new();
    let mut start = 0usize;
    while find(';', start) == Some(start) {
        start += 1;
        let mut end = find(';', start);
        let mut ind = start;
        let mut diff: i64 = 0;
        while let Some(e) = end {
            diff += i64::try_from(count(&['"'], ind, e)).unwrap_or(0)
                - i64::try_from(count(&['\\', '"'], ind, e)).unwrap_or(0);
            if diff % 2 == 0 {
                break;
            }
            ind = e;
            end = find(';', e + 1);
        }
        let end = end.unwrap_or(s.len());
        let segment: String = s[start..end].iter().collect();
        let f = match segment.find('=') {
            None => segment,
            Some(i) => format!(
                "{}={}",
                segment[..i].trim().to_lowercase(),
                segment[i + 1..].trim()
            ),
        };
        plist.push(f.trim().to_owned());
        start = end;
    }
    plist
}

/// Whether a `charset` names UTF-8, as `codecs.lookup` resolves it (`encodings.normalize_encoding`
/// then the alias table). Every other name reads as latin-1: an ASCII or latin-1 codec decodes the
/// same text or fails over to latin-1, and an unknown name is Python's `LookupError`, which
/// `_user_safe_decode` also answers with latin-1. Other real codecs (`cp1252`, `utf-16`, …) are
/// approximated as latin-1 (DECISIONS-RS R6.21).
fn names_utf8(charset: &str) -> bool {
    let mut norm = String::new();
    let mut punct = false;
    for c in charset.chars() {
        if c.is_alphanumeric() || c == '.' {
            if punct && !norm.is_empty() {
                norm.push('_');
            }
            if c.is_ascii() {
                norm.push(c.to_ascii_lowercase());
            }
            punct = false;
        } else {
            punct = true;
        }
    }
    let dotted = norm.replace('.', "_");
    [
        "utf_8",
        "utf8",
        "u8",
        "utf",
        "utf8_ucs2",
        "utf8_ucs4",
        "cp65001",
    ]
    .iter()
    .any(|n| *n == norm || *n == dotted)
}

/// `_user_safe_decode(src, utf8)`: UTF-8 when the form's charset names it and the bytes are
/// UTF-8, else latin-1.
fn safe_decode(bytes: &[u8], utf8: bool) -> String {
    match std::str::from_utf8(bytes) {
        Ok(s) if utf8 => s.to_owned(),
        _ => bytes.iter().map(|&b| char::from(b)).collect(),
    }
}

/// How many bytes of an unterminated part python-multipart has handed to `on_part_data`: all of
/// them but a tail that could still be the start of the delimiter.
fn emitted_len(data: &[u8], delim: &[u8]) -> usize {
    let held = (1..delim.len().min(data.len() + 1))
        .rev()
        .find(|&k| data.ends_with(&delim[..k]))
        .unwrap_or(0);
    data.len() - held
}

fn is_token(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&b)
}

struct Part {
    disposition: Vec<u8>,
    name: String,
    filename: Option<String>,
    data: Vec<u8>,
}

/// python-multipart's `MultipartParser` over the whole body, with Starlette's callbacks.
fn parse_multipart(
    boundary: &[u8],
    body: &[u8],
    utf8: bool,
) -> Result<Vec<(String, FormValue)>, Problem> {
    if boundary.len() > MAX_BOUNDARY_LENGTH {
        return Err(parse_error());
    }
    let mut delim = b"\r\n--".to_vec();
    delim.extend_from_slice(boundary);
    let mut items = Vec::new();
    let (mut fields, mut files) = (0usize, 0usize);
    let mut i = 0usize;
    // START: skip CR/LF, then expect `--boundary`.
    while i < body.len() && (body[i] == b'\r' || body[i] == b'\n') {
        match body[i..].iter().position(|&b| b == b'-') {
            Some(p) => i += p,
            None => return Ok(items),
        }
    }
    if i >= body.len() {
        return Ok(items);
    }
    // START_BOUNDARY: `--boundary` then CRLF (or `--` for an empty form).
    let opening = &delim[2..];
    for (k, &want) in opening.iter().enumerate() {
        match body.get(i + k) {
            None => return Ok(items),
            Some(&c) if c == want => {}
            Some(_) => return Err(parse_error()),
        }
    }
    i += opening.len();
    match body.get(i) {
        None => return Ok(items),
        Some(b'-') => {
            // END_BOUNDARY: a second `-` ends an empty form.
            return match body.get(i + 1) {
                None | Some(b'-') => Ok(items),
                Some(_) => Err(parse_error()),
            };
        }
        Some(b'\r') => {}
        Some(_) => return Err(parse_error()),
    }
    match body.get(i + 1) {
        None => return Ok(items),
        Some(b'\n') => {}
        Some(_) => return Err(parse_error()),
    }
    i += 2;
    loop {
        // HEADER_FIELD_START .. HEADERS_ALMOST_DONE.
        let mut part = Part {
            disposition: Vec::new(),
            name: String::new(),
            filename: None,
            data: Vec::new(),
        };
        let mut header_count = 0usize;
        loop {
            match body.get(i) {
                None => return Ok(items),
                Some(b'\r') => {
                    match body.get(i + 1) {
                        None => return Ok(items),
                        Some(b'\n') => {}
                        Some(_) => return Err(parse_error()),
                    }
                    i += 2;
                    break;
                }
                Some(_) => {}
            }
            header_count += 1;
            if header_count > MAX_HEADER_COUNT {
                return Err(parse_error());
            }
            let Some(colon) = body[i..].iter().position(|&b| b == b':') else {
                // A header name running to the end of the data: no part finishes.
                if body[i..].iter().all(|&b| is_token(b)) {
                    return Ok(items);
                }
                return Err(parse_error());
            };
            let name = &body[i..i + colon];
            if !name.iter().all(|&b| is_token(b)) || name.is_empty() {
                return Err(parse_error());
            }
            let mut size = colon + 1;
            if size > MAX_HEADER_SIZE {
                return Err(parse_error());
            }
            i += colon + 1;
            while body.get(i) == Some(&b' ') {
                size += 1;
                if size > MAX_HEADER_SIZE {
                    return Err(parse_error());
                }
                i += 1;
            }
            let Some(cr) = body[i..].iter().position(|&b| b == b'\r') else {
                return Ok(items);
            };
            if size + cr > MAX_HEADER_SIZE {
                return Err(parse_error());
            }
            let value = body[i..i + cr].to_vec();
            i += cr;
            match body.get(i + 1) {
                None => return Ok(items),
                Some(b'\n') => {}
                Some(_) => return Err(parse_error()),
            }
            i += 2;
            if name.eq_ignore_ascii_case(b"content-disposition") {
                part.disposition = value;
            }
        }
        // on_headers_finished.
        let disposition = safe_decode_latin1(&part.disposition);
        let (_, options) = parse_options_header(Some(&disposition));
        let Some((_, name)) = options.iter().find(|(k, _)| k == "name") else {
            return Err(framework(
                400,
                "The Content-Disposition header field \"name\" must be provided.",
            ));
        };
        part.name = safe_decode(&latin1_bytes(name), utf8);
        part.filename = options
            .iter()
            .find(|(k, _)| k == "filename")
            .map(|(_, v)| safe_decode(&latin1_bytes(v), utf8));
        // Starlette counts the part as soon as its headers are finished.
        if part.filename.is_some() {
            files += 1;
            if files > MAX_FILES {
                return Err(framework(
                    400,
                    &format!("Too many files. Maximum number of files is {MAX_FILES}."),
                ));
            }
        } else {
            fields += 1;
            if fields > MAX_FIELDS {
                return Err(too_many_fields());
            }
        }
        // A text part's data is size-checked as it streams, so even a part that never finishes
        // is refused once more than 1 MiB of it has been handed over.
        let text_part = part.filename.is_none();
        let unfinished = |emitted: usize| -> Result<(), Problem> {
            if text_part && emitted > MAX_PART_SIZE {
                return Err(part_too_large());
            }
            Ok(())
        };
        // PART_DATA: up to `\r\n--boundary`, then CRLF (another part) or `--` (the end).
        let mut search = i;
        let (end, last) = loop {
            let Some(at) = find(&body[search..], &delim).map(|p| p + search) else {
                unfinished(emitted_len(&body[i..], &delim))?;
                return Ok(items);
            };
            let after = at + delim.len();
            match (body.get(after), body.get(after + 1)) {
                (Some(b'\r'), Some(b'\n')) => break (at, false),
                (Some(b'-'), Some(b'-')) => break (at, true),
                (Some(b'\r' | b'-'), None) | (None, _) => {
                    unfinished(at - i)?;
                    return Ok(items);
                }
                _ => search = at + 1,
            }
        };
        part.data = body[i..end].to_vec();
        if part.filename.is_none() && part.data.len() > MAX_PART_SIZE {
            return Err(part_too_large());
        }
        items.push(match part.filename {
            Some(filename) => (
                part.name,
                FormValue::File {
                    filename,
                    data: part.data,
                },
            ),
            None => (part.name, FormValue::Text(safe_decode(&part.data, utf8))),
        });
        if last {
            return Ok(items);
        }
        i = end + delim.len() + 2;
    }
}

fn find(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

fn safe_decode_latin1(bytes: &[u8]) -> String {
    bytes.iter().map(|&b| char::from(b)).collect()
}

fn latin1_bytes(s: &str) -> Vec<u8> {
    s.chars()
        .map(|c| u8::try_from(u32::from(c)).unwrap_or(b'?'))
        .collect()
}

/// `application/x-www-form-urlencoded`: `name=value` pairs split on `&`, `+` and `%XX` decoded;
/// `FormParser`'s limits (a name plus value over 1 MiB, more than 1,000 fields) are its 400s.
fn parse_urlencoded(body: &[u8]) -> Result<Vec<(String, FormValue)>, Problem> {
    let decode = |s: &[u8]| -> String {
        let text: String = s
            .iter()
            .map(|&b| if b == b'+' { ' ' } else { char::from(b) })
            .collect();
        percent_encoding::percent_decode_str(&text)
            .decode_utf8()
            .map(|c| c.into_owned())
            .unwrap_or(text)
    };
    let mut items = Vec::new();
    for pair in body.split(|&b| b == b'&').filter(|pair| !pair.is_empty()) {
        let (k, v) = match pair.iter().position(|&b| b == b'=') {
            Some(p) => (&pair[..p], &pair[p + 1..]),
            None => (pair, &b""[..]),
        };
        if k.len() + v.len() > MAX_PART_SIZE {
            return Err(framework(
                400,
                &format!("Field exceeded maximum size of {}KB.", MAX_PART_SIZE / 1024),
            ));
        }
        if items.len() >= MAX_FIELDS {
            return Err(too_many_fields());
        }
        items.push((decode(k), FormValue::Text(decode(v))));
    }
    Ok(items)
}

/// The form field `field` as FastAPI hands it to an `UploadFile = File(...)` parameter.
///
/// # Errors
/// A body the parser refuses (400). The field's own absence or wrong type is returned as
/// `Ok(None)` / `Ok(Some(FormValue::Text))` for the caller to report among its other errors.
pub(crate) fn form_field(
    content_type: Option<&str>,
    body: &[u8],
    field: &str,
) -> Result<Option<FormValue>, Problem> {
    let (ctype, params) = parse_options_header(content_type);
    let items = if ctype == "multipart/form-data" {
        let Some((_, boundary)) = params.iter().find(|(k, _)| k == "boundary") else {
            return Err(framework(400, "Missing boundary in multipart."));
        };
        let utf8 = params
            .iter()
            .find(|(k, _)| k == "charset")
            .is_none_or(|(_, c)| names_utf8(c));
        parse_multipart(&latin1_bytes(boundary), body, utf8)?
    } else if ctype == "application/x-www-form-urlencoded" {
        parse_urlencoded(body)?
    } else {
        Vec::new()
    };
    Ok(items
        .into_iter()
        .rev()
        .find(|(k, _)| k == field)
        .map(|(_, v)| v)
        // `_get_multidict_value`: an empty text value of a `Form`/`File` field is missing.
        .filter(|v| *v != FormValue::Text(String::new())))
}

/// FastAPI's error for a file parameter that received text.
pub(crate) const NOT_A_FILE: &str = "Value error, Expected UploadFile, received: <class 'str'>";

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    const CT: &str = "multipart/form-data; boundary=XyZ";

    fn part(headers: &str, body: &str) -> String {
        format!("--XyZ\r\n{headers}\r\n\r\n{body}\r\n")
    }

    fn file(ct: Option<&str>, body: &str) -> Result<Option<FormValue>, u16> {
        form_field(ct, body.as_bytes(), "file").map_err(|p| p.status)
    }

    #[test]
    fn reads_the_form_as_starlette_does() {
        // Each pinned from FastAPI 0.141 + Starlette 1.6 + python-multipart 0.0.32.
        let ok = format!(
            "{}--XyZ--\r\n",
            part(
                "Content-Disposition: form-data; name=\"file\"; filename=\"a.csv\"",
                "SYM\nINFY"
            )
        );
        assert_eq!(
            file(Some(CT), &ok),
            Ok(Some(FormValue::File {
                filename: "a.csv".to_owned(),
                data: b"SYM\nINFY".to_vec()
            }))
        );
        assert_eq!(file(None, ""), Ok(None));
        assert_eq!(file(Some("application/json"), "{}"), Ok(None));
        assert_eq!(file(Some("multipart/form-data"), "abc"), Err(400));
        assert_eq!(file(Some(CT), "garbage here"), Err(400));
        assert_eq!(file(Some(CT), ""), Ok(None));
        let text = format!(
            "{}--XyZ--\r\n",
            part("Content-Disposition: form-data; name=\"file\"", "INFY")
        );
        assert_eq!(
            file(Some(CT), &text),
            Ok(Some(FormValue::Text("INFY".to_owned())))
        );
        let noend = part(
            "Content-Disposition: form-data; name=\"file\"; filename=\"a.csv\"",
            "one",
        );
        assert_eq!(file(Some(CT), &noend), Ok(None));
        let nodisp = format!("{}--XyZ--\r\n", part("Content-Type: text/plain", "one"));
        assert_eq!(file(Some(CT), &nodisp), Err(400));
        let upper = ok.clone();
        assert_eq!(
            file(Some("Multipart/Form-Data; boundary=XyZ"), &upper),
            Ok(None)
        );
        let two = format!(
            "{}{}--XyZ--\r\n",
            part(
                "Content-Disposition: form-data; name=\"file\"; filename=\"a.csv\"",
                "one"
            ),
            part(
                "Content-Disposition: form-data; name=\"file\"; filename=\"b.csv\"",
                "two"
            )
        );
        assert_eq!(
            file(Some(CT), &two),
            Ok(Some(FormValue::File {
                filename: "b.csv".to_owned(),
                data: b"two".to_vec()
            }))
        );
        let lf =
            "--XyZ\nContent-Disposition: form-data; name=\"file\"; filename=\"a\"\n\nx\n--XyZ--\n";
        assert_eq!(file(Some(CT), lf), Err(400));
        assert_eq!(file(Some(CT), &format!("preamble\r\n{ok}")), Err(400));
        assert_eq!(
            file(Some("application/x-www-form-urlencoded"), "file=abc"),
            Ok(Some(FormValue::Text("abc".to_owned())))
        );
    }

    fn status_and_detail(
        r: Result<Option<FormValue>, Problem>,
    ) -> Result<Option<FormValue>, (u16, String)> {
        r.map_err(|p| (p.status, p.detail.clone()))
    }

    #[test]
    fn starlettes_form_limits_and_charset() {
        // Each pinned from Starlette 1.6 + python-multipart 0.0.32 (`MultiPartParser`, `FormParser`).
        let m = MAX_PART_SIZE;
        let field = |i: usize| {
            part(
                &format!("Content-Disposition: form-data; name=\"f{i}\""),
                "x",
            )
        };
        let upload = part(
            "Content-Disposition: form-data; name=\"file\"; filename=\"a.csv\"",
            "SYM",
        );
        let with = |n: usize| {
            format!(
                "{}{upload}--XyZ--\r\n",
                (0..n).map(field).collect::<String>()
            )
        };
        assert!(matches!(
            file(Some(CT), &with(999)),
            Ok(Some(FormValue::File { .. }))
        ));
        assert_eq!(
            status_and_detail(form_field(Some(CT), with(1001).as_bytes(), "file")),
            Err((
                400,
                "Too many fields. Maximum number of fields is 1000.".to_owned()
            ))
        );
        let files: String = (0..1000)
            .map(|i| {
                part(
                    &format!("Content-Disposition: form-data; name=\"g{i}\"; filename=\"b\""),
                    "x",
                )
            })
            .collect();
        assert_eq!(
            status_and_detail(form_field(
                Some(CT),
                format!("{files}{upload}--XyZ--\r\n").as_bytes(),
                "file"
            )),
            Err((
                400,
                "Too many files. Maximum number of files is 1000.".to_owned()
            ))
        );
        // A text part that never finishes: refused once more than 1 MiB has streamed, a tail that
        // could still be the delimiter held back.
        let head = "--XyZ\r\nContent-Disposition: form-data; name=\"note\"\r\n\r\n";
        let unfinished =
            |n: usize, tail: &str| file(Some(CT), &format!("{head}{}{tail}", "x".repeat(n)));
        assert_eq!(unfinished(m, ""), Ok(None));
        assert_eq!(unfinished(m + 1, ""), Err(400));
        assert_eq!(unfinished(m, "\r\n--X"), Ok(None));
        assert_eq!(unfinished(m, "\r\n--XyZ\r"), Ok(None));
        // urlencoded: `&&` is no field, `=` is one; name plus value over 1 MiB is refused.
        let url = |body: &str| {
            status_and_detail(form_field(
                Some("application/x-www-form-urlencoded"),
                body.as_bytes(),
                "file",
            ))
        };
        let thousand = vec!["f=x"; 1000].join("&");
        assert_eq!(url(&format!("{thousand}&&")), Ok(None));
        assert_eq!(
            url(&format!("{thousand}&=")),
            Err((
                400,
                "Too many fields. Maximum number of fields is 1000.".to_owned()
            ))
        );
        assert_eq!(url(&format!("{}=x", "a".repeat(m - 1))), Ok(None));
        assert_eq!(
            url(&format!("{}=x", "a".repeat(m))),
            Err((400, "Field exceeded maximum size of 1024KB.".to_owned()))
        );
        // `charset`: UTF-8 under its aliases, latin-1 for anything else (an unknown name too).
        let named = |ct: &str| match file(
            Some(ct),
            &format!(
                "{}--XyZ--\r\n",
                part(
                    "Content-Disposition: form-data; name=\"file\"; filename=\"caf\u{e9}.csv\"",
                    "x"
                )
            ),
        ) {
            Ok(Some(FormValue::File { filename, .. })) => filename,
            other => format!("{other:?}"),
        };
        assert_eq!(named(&format!("{CT}; charset=UTF_8")), "caf\u{e9}.csv");
        assert_eq!(
            named(&format!("{CT}; charset=latin-1")),
            "caf\u{c3}\u{a9}.csv"
        );
        assert_eq!(
            named(&format!("{CT}; charset=nonsense")),
            "caf\u{c3}\u{a9}.csv"
        );
    }
}
