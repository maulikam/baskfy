//! Sets `cfg(bed)` when `BASKFY_RS_TEST_DATABASE_URL` names the test bed, so the tests that need
//! a database run there and are reported **ignored, with the reason**, everywhere else — never
//! silently passing (DECISIONS-RS R5.4).

fn main() {
    println!("cargo::rustc-check-cfg=cfg(bed)");
    println!("cargo::rerun-if-env-changed=BASKFY_RS_TEST_DATABASE_URL");
    if std::env::var_os("BASKFY_RS_TEST_DATABASE_URL").is_some_and(|v| !v.is_empty()) {
        println!("cargo::rustc-cfg=bed");
    }
}
