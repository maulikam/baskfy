//! `baskfy-desk`: the operator desk (`kite-momentum-rebalancer/app`) in Rust — wave D
//! (DECISIONS-RS R8.0–R8.5).
//!
//! **Porting is not cutover** (R8.0). The Python desk keeps serving and placing every order.
//! This crate reproduces it route by route, graded by `tools/parity/desk_diff.py` against the
//! Python desk on bed clones with a replay Kite and an injected clock; the execution core is
//! ported test-first, DRY_RUN-only, and cuts over only on Maulik's explicit go after two shadow
//! Fridays.
//!
//! **The foundation** (this commit's modules) is what every lane codes against:
//!
//! | Module | Python | |
//! |---|---|---|
//! | [`settings`] | `app.config` + every module-level `os.getenv` | defaults held to a dumped fixture; DRY_RUN true, every execute/auto flag false |
//! | [`storage`] | `app.analytics.db`, `app.analytics.pg` | Postgres (`desk` schema) and SQLite, SQLite-dialect SQL with `?` |
//! | [`clock`], [`ids`] | `time.time`, `datetime.now`, `uuid.uuid4` | the seams the harness drives |
//! | [`kite`], [`fake_kite`] | `kiteconnect.KiteConnect` as the desk calls it | reads and writes split (law 2); replay client records every call |
//! | [`gateway`] | `baskfy_execution` interface + client ids, tenancy, gates | the trait the exec_core lane implements |
//! | [`plan`] | `PLANS`, `/execute`'s gate | 30-minute expiry, `plan_digest` |
//! | [`templates`], [`pyfmt`] | `Jinja2Templates`, `%` and `str.format` | Jinja2-exact output on minijinja |
//! | [`wire`] | orjson / `json.dumps` | the desk's bytes |
//! | [`app`], [`websec`], [`path_prefix`] | `FastAPI(...)`, `DeskSecurity`, `PathPrefix` | the skeleton |
//! | [`lanes`] | one module per porting lane | declared stubs; see `docs/rust-rewrite/STATUS.md` |

pub mod app;
pub mod clock;
pub mod fake_kite;
pub mod gateway;
pub mod ids;
pub mod kite;
pub mod lanes;
pub mod path_prefix;
pub mod plan;
pub mod pyfmt;
pub mod settings;
pub mod storage;
pub mod templates;
pub mod websec;
pub mod wire;

pub use app::{AppState, DeskPaths, build_app, build_state};
pub use settings::Settings;
