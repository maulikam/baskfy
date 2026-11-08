//! Law 2: the API never writes to the broker. The desk's gateway is the only path to an order.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::Path;

fn scan(dir: &Path, hits: &mut Vec<String>) {
    for entry in std::fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            scan(&path, hits);
        } else if path.extension().is_some_and(|e| e == "rs") {
            let code = std::fs::read_to_string(&path).unwrap();
            for needle in ["/orders/regular", "/gtt/triggers", ".post_form("] {
                if code.contains(needle) {
                    hits.push(format!("{}: {needle}", path.display()));
                }
            }
        }
    }
}

#[test]
fn the_api_has_no_broker_write() {
    let mut hits = Vec::new();
    scan(
        &Path::new(env!("CARGO_MANIFEST_DIR")).join("src"),
        &mut hits,
    );
    assert!(hits.is_empty(), "broker writes outside the desk: {hits:#?}");
}
