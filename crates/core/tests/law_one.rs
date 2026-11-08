//! Law 1 (root CLAUDE.md), in Rust: `baskfy-core` touches nothing — no database, no network,
//! no disk, no clock, no environment. This test scans the crate's source and manifest and fails
//! on any token or dependency that would let it. Never widen an allow-list to pass it.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::{Path, PathBuf};

/// Tokens that mean I/O, a clock, the environment, or `unsafe` in non-comment source.
const FORBIDDEN_TOKENS: &[&str] = &[
    "tokio",
    "reqwest",
    "sqlx",
    "hyper",
    "axum",
    "std::fs",
    "std::net",
    "std::env",
    "std::process",
    "std::io::stdin",
    "File::",
    "TcpStream",
    "UdpSocket",
    "SystemTime",
    "Instant::now",
    "Local::now",
    "Utc::now",
    "::now()",
    "env!(",
    "option_env!(",
    "unsafe",
    "println!",
    "eprintln!",
];

/// Dependencies the core may never take, by crate name.
const FORBIDDEN_DEPS: &[&str] = &[
    "tokio",
    "reqwest",
    "sqlx",
    "hyper",
    "axum",
    "tower",
    "ureq",
    "diesel",
    "rusqlite",
    "postgres",
    "tokio-postgres",
    "redis",
    "mio",
    "async-std",
    "smol",
    "kiteconnect",
];

/// The only dependencies the core declares: computation, and nothing else (R0.2).
const ALLOWED_DEPS: &[&str] = &["polars", "rust_decimal", "chrono", "serde", "thiserror"];

/// polars features that do I/O.
const FORBIDDEN_POLARS_FEATURES: &[&str] = &[
    "csv",
    "parquet",
    "json",
    "ipc",
    "ipc_streaming",
    "avro",
    "cloud",
    "http",
    "aws",
    "azure",
    "gcp",
    "async",
    "polars-io",
    "polars-parquet",
    "fs",
];

fn manifest_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for e in std::fs::read_dir(dir).unwrap() {
        let p = e.unwrap().path();
        if p.is_dir() {
            rust_files(&p, out);
        } else if p.extension().is_some_and(|x| x == "rs") {
            out.push(p);
        }
    }
}

/// Source with `//` line comments removed (doc comments may name what is forbidden).
fn code_only(text: &str) -> String {
    text.lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn core_source_has_no_io_clock_or_unsafe() {
    let mut files = Vec::new();
    rust_files(&manifest_dir().join("src"), &mut files);
    assert!(!files.is_empty());
    let mut hits = Vec::new();
    for f in &files {
        let code = code_only(&std::fs::read_to_string(f).unwrap());
        for t in FORBIDDEN_TOKENS {
            if code.contains(t) {
                hits.push(format!("{}: {t}", f.display()));
            }
        }
    }
    assert!(hits.is_empty(), "law 1 violated:\n{}", hits.join("\n"));
}

/// The `[dependencies]` section (not dev-dependencies) of the core's Cargo.toml.
fn dependencies_section(manifest: &str) -> String {
    let mut out = Vec::new();
    let mut inside = false;
    for line in manifest.lines() {
        let t = line.trim();
        if t.starts_with('[') {
            inside = t == "[dependencies]";
            continue;
        }
        if inside && !t.is_empty() && !t.starts_with('#') {
            out.push(t.to_owned());
        }
    }
    out.join("\n")
}

#[test]
fn core_manifest_declares_only_computation_deps() {
    let manifest = std::fs::read_to_string(manifest_dir().join("Cargo.toml")).unwrap();
    let deps = dependencies_section(&manifest);
    for line in deps.lines() {
        let name = line.split(['=', '.', ' ']).next().unwrap().trim();
        assert!(
            !FORBIDDEN_DEPS.contains(&name),
            "forbidden dependency {name}"
        );
        assert!(
            ALLOWED_DEPS.contains(&name),
            "dependency {name} is not in law_one.rs ALLOWED_DEPS; adding one is a DECISIONS-RS entry"
        );
    }
    assert!(
        !manifest.contains("[target."),
        "no target-specific dependencies in the core"
    );
    assert!(!manifest.contains("build ="), "no build script in the core");
    assert!(
        !manifest_dir().join("build.rs").exists(),
        "no build script in the core"
    );
}

#[test]
fn workspace_pins_keep_io_and_the_clock_out() {
    let ws = std::fs::read_to_string(manifest_dir().join("../../Cargo.toml")).unwrap();
    let line_of = |name: &str| -> String {
        let start = ws
            .find(&format!("\n{name} = "))
            .unwrap_or_else(|| panic!("{name} not pinned"));
        let rest = &ws[start + 1..];
        // A dependency entry ends at the first line that closes its inline table.
        let end = rest.find("}\n").map_or(rest.len(), |i| i + 1);
        rest[..end].to_owned()
    };
    let polars = line_of("polars");
    assert!(
        polars.contains("default-features = false"),
        "polars must not take default (IO) features"
    );
    for f in FORBIDDEN_POLARS_FEATURES {
        assert!(
            !polars.contains(&format!("\"{f}\"")),
            "polars feature {f} does IO"
        );
    }
    let chrono = line_of("chrono");
    assert!(
        chrono.contains("default-features = false"),
        "chrono defaults include the clock"
    );
    assert!(
        !chrono.contains("\"clock\"") && !chrono.contains("\"now\""),
        "chrono clock feature"
    );
    for dep in ALLOWED_DEPS {
        assert!(
            line_of(dep).contains("\"="),
            "{dep} must be pinned exactly (=x.y.z)"
        );
    }
}
