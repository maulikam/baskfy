//! A credential that never prints. No Python twin: Python keeps the Kite key, the Fernet key
//! and the S3 secret as plain `str` fields; the Rust settings wrap them so that a `{:?}` of the
//! settings, an error, or a log line can never carry one (root CLAUDE.md: never print, log or
//! commit secrets).

use std::fmt;

/// A secret string. `Debug` says only whether it is set; there is no `Display` and no
/// `Serialize`, so the only way to the text is [`Secret::expose`], which is greppable.
#[derive(Clone, Default, PartialEq, Eq)]
pub struct Secret(String);

impl Secret {
    /// Wraps a credential.
    #[must_use]
    pub fn new(value: impl Into<String>) -> Self {
        Self(value.into())
    }

    /// The credential itself — call only where it is handed to its consumer (a header, a
    /// cipher), never to format it.
    #[must_use]
    pub fn expose(&self) -> &str {
        &self.0
    }

    /// Python's truthiness of the field: is anything configured?
    #[must_use]
    pub fn is_set(&self) -> bool {
        !self.0.is_empty()
    }
}

impl fmt::Debug for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(if self.0.is_empty() {
            "Secret(<unset>)"
        } else {
            "Secret(<redacted>)"
        })
    }
}

impl From<&str> for Secret {
    fn from(value: &str) -> Self {
        Self::new(value)
    }
}

impl From<String> for Secret {
    fn from(value: String) -> Self {
        Self(value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn debug_never_shows_the_value() {
        let s = Secret::new("hunter2");
        assert_eq!(format!("{s:?}"), "Secret(<redacted>)");
        assert!(!format!("{s:?}").contains("hunter2"));
        assert_eq!(format!("{:?}", Secret::default()), "Secret(<unset>)");
    }
}
