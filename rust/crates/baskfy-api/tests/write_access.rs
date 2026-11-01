//! The read-write pool reaches write routes only (DECISIONS-RS R6.2). `session.rs`'s unit tests
//! prove it at run time (a GET handler asking for the pool gets the missing-extension 500); this
//! proves it in the source: the pool's internals are named in `state.rs` and `session.rs` alone,
//! and every handler that takes a `WritePool` is mounted on a write method.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::{Path, PathBuf};

fn sources(dir: &Path, out: &mut Vec<PathBuf>) {
    for entry in std::fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            sources(&path, out);
        } else if path.extension().is_some_and(|e| e == "rs") {
            out.push(path);
        }
    }
}

fn all() -> Vec<(String, String)> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut files = Vec::new();
    sources(&root, &mut files);
    files
        .into_iter()
        .map(|p| {
            let rel = p
                .strip_prefix(&root)
                .unwrap()
                .to_string_lossy()
                .into_owned();
            (rel, std::fs::read_to_string(&p).unwrap())
        })
        .collect()
}

#[test]
fn only_the_session_module_opens_the_write_pool() {
    for (file, text) in all() {
        if file == "state.rs" || file == "session.rs" {
            continue;
        }
        for needle in ["write_db()", "WriteDb", "write.0"] {
            assert!(
                !text.contains(needle),
                "{file} names `{needle}`: only state.rs and session.rs may"
            );
        }
    }
}

const GET_WRITERS: [&str; 1] = ["oauth_callback"];

#[test]
fn every_handler_that_takes_the_write_pool_is_mounted_on_a_write_method() {
    let files = all();
    // Handlers whose signature takes the pool.
    let mut writers: Vec<String> = Vec::new();
    for (_, text) in files.iter().filter(|(f, _)| f.starts_with("routes")) {
        for chunk in text.split("pub async fn ").skip(1) {
            let name: String = chunk
                .chars()
                .take_while(|c| c.is_alphanumeric() || *c == '_')
                .collect();
            let signature = chunk.split('{').next().unwrap_or("");
            if !name.is_empty() && signature.contains("WritePool") {
                writers.push(name);
            }
        }
    }
    assert!(!writers.is_empty(), "the session routes take the pool");
    let mut mounted = 0;
    for (file, text) in &files {
        for name in &writers {
            for (i, _) in text.match_indices(&format!("::{name})")) {
                let before = &text[..i];
                let call = before.rsplit(['(', '.']).nth(1).unwrap_or("");
                let verb = before
                    .rsplit('(')
                    .nth(1)
                    .map(|s| s.rsplit(['.', ' ', '\n']).next().unwrap_or(""));
                let verb = verb.unwrap_or(call);
                assert!(
                    ["post", "put", "patch", "delete"].contains(&verb),
                    "{file}: `{name}` is mounted with `{verb}`"
                );
                mounted += 1;
            }
            for verb in ["post", "put", "patch", "delete", "get"] {
                let hits = text.matches(&format!("{verb}({name})")).count();
                if hits > 0 {
                    assert!(
                        verb != "get" || GET_WRITERS.contains(&name.as_str()),
                        "{file}: `{name}` is mounted with `get`"
                    );
                    mounted += hits;
                }
            }
        }
    }
    assert_eq!(
        mounted,
        writers.len(),
        "every writer is mounted exactly once: {writers:?}"
    );
}
