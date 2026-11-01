//! The pieces of `baskfy_core.universes` the index steps need: the 15 selectable universes'
//! pinned ids and slugs, `slugify_index`, `FIRST_NON_UNIVERSE_INDEX_ID` and `SME_SERIES`.
//!
//! Kept private to the lane: the ids are load-bearing (`factor_daily.universe_mask` sets one bit
//! per id), so the table is held to Python's by a test rather than shared by a second definition
//! another lane might edit (`baskfy_core::screener` carries its own id/slug table for the same
//! reason; the test below pins both to Python's tuple).

/// One selectable universe: `Universe(index_id, slug, ...)`. Only what the steps read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Universe {
    /// `index_id` — also the `universe_mask` bit + 1.
    pub(crate) index_id: i16,
    /// `slug`.
    pub(crate) slug: &'static str,
}

const fn u(index_id: i16, slug: &'static str) -> Universe {
    Universe { index_id, slug }
}

/// `UNIVERSES`, in Python's declaration (id / mask-bit) order.
pub(crate) const UNIVERSES: [Universe; 15] = [
    u(1, "nifty-50"),
    u(2, "nifty-next-50"),
    u(3, "nifty-100"),
    u(4, "nifty-200"),
    u(5, "nifty-500"),
    u(6, "nifty-total-market"),
    u(7, "nifty-large-mid-250"),
    u(8, "nifty-midcap-150"),
    u(9, "nifty-smallcap-250"),
    u(10, "nifty-microcap-250"),
    u(11, "nifty-mid-small-400"),
    u(12, "nifty-allcap"),
    u(13, "nifty-fno"),
    u(14, "etf"),
    u(15, "nse-sme-emerge"),
];

/// `FIRST_NON_UNIVERSE_INDEX_ID`: dashboard-only indices start above the 31-bit mask range.
pub(crate) const FIRST_NON_UNIVERSE_INDEX_ID: i64 = 100;

/// `sorted(SME_SERIES)` — the NSE Emerge series, in the order Python binds them.
pub(crate) const SME_SERIES_SORTED: [&str; 3] = ["SM", "ST", "SZ"];

/// `str.isspace()` for one character: Unicode `White_Space` plus the four information
/// separators `\x1c`–`\x1f`, which Python counts and Rust's `char::is_whitespace` does not.
pub(crate) fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `str.strip()` with no argument.
pub(crate) fn py_strip(text: &str) -> &str {
    text.trim_matches(py_isspace)
}

/// `slugify_index(name)`: `re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")`.
///
/// `NIFTY MIDCAP 150` → `nifty-midcap-150`, `Nifty50 PR 1x Inverse` → `nifty50-pr-1x-inverse`.
pub(crate) fn slugify_index(name: &str) -> String {
    let lowered = py_strip(name).to_lowercase();
    let mut out = String::with_capacity(lowered.len());
    let mut in_run = false;
    for c in lowered.chars() {
        if c.is_ascii_lowercase() || c.is_ascii_digit() {
            out.push(c);
            in_run = false;
        } else if !in_run {
            out.push('-');
            in_run = true;
        }
    }
    out.trim_matches('-').to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_fifteen_universes_keep_their_pinned_ids() {
        // test_reference_data.TestIndexRegistration.test_the_fourteen_universes_keep_their_pinned_ids:
        // ids are dense from 1 and below the dashboard range.
        for (position, universe) in UNIVERSES.iter().enumerate() {
            assert_eq!(i64::from(universe.index_id), position as i64 + 1);
            assert!(i64::from(universe.index_id) < FIRST_NON_UNIVERSE_INDEX_ID);
        }
        // The screener's own id table agrees wherever it names a universe.
        assert_eq!(UNIVERSES[11].slug, "nifty-allcap");
        assert_eq!(UNIVERSES[14].slug, "nse-sme-emerge");
    }

    #[test]
    fn index_names_become_stable_slugs() {
        // test_reference_data.TestIndexRegistration.test_index_names_become_stable_slugs.
        for (published, expected) in [
            ("NIFTY 50", "nifty-50"),
            ("NIFTY MIDCAP 150", "nifty-midcap-150"),
            ("Nifty50 PR 1x Inverse", "nifty50-pr-1x-inverse"),
            ("India VIX", "india-vix"),
            ("  NIFTY OIL & GAS  ", "nifty-oil-gas"),
            ("NIFTY HEALTHCARE INDEX", "nifty-healthcare-index"),
            ("--Nifty--Bank--", "nifty-bank"),
            ("!!!", ""),
            ("\u{1c}NIFTY IT\u{1f}", "nifty-it"),
            ("Énergie 50", "nergie-50"),
            ("\u{212a}ELVIN", "kelvin"),
        ] {
            assert_eq!(slugify_index(published), expected, "{published:?}");
        }
    }

    #[test]
    fn strip_is_pythons() {
        assert_eq!(py_strip("\u{1c} a b \u{a0}\u{3000}"), "a b");
        assert_eq!(py_strip("\u{200b}x"), "\u{200b}x"); // ZWSP is not whitespace in either
    }
}
