//! Law 2 in the worker: nothing here places, modifies or cancels an order or a GTT, or calls a
//! Kite write endpoint. Execution is the last wave (DECISIONS-RS R3.10). A source scan, as the
//! API lanes' `no_write_here_reaches_a_broker`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::Path;

fn sources(dir: &Path, out: &mut Vec<(String, String)>) {
    for entry in std::fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            sources(&path, out);
        } else if path.extension().is_some_and(|e| e == "rs") {
            out.push((
                path.display().to_string(),
                std::fs::read_to_string(&path).unwrap(),
            ));
        }
    }
}

#[test]
fn no_source_file_can_reach_an_order_or_a_gtt() {
    let mut files = Vec::new();
    sources(
        &Path::new(env!("CARGO_MANIFEST_DIR")).join("src"),
        &mut files,
    );
    assert!(!files.is_empty());
    let forbidden = [
        "place_order",
        "modify_order",
        "cancel_order",
        "place_gtt",
        "modify_gtt",
        "delete_gtt",
        "/orders",
        "/gtt/triggers",
        "baskfy_execution",
        "baskfy-execution",
    ];
    for (path, text) in &files {
        for token in forbidden {
            assert!(!text.contains(token), "{path} mentions {token:?}");
        }
    }
}
