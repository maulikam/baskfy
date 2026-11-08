//! The desk's state as the template globals see it.
//!
//! `fno_badge()` is a Jinja2 global every page's nav calls (`base.html`); in Python it is
//! `fno_desk.nav_badge`, a closure over the module's config and database. minijinja globals are
//! registered before the [`AppState`] exists (`lanes::register_templates` runs inside
//! `build_state`), so the state is handed in here once it does: [`install`] is called by every
//! handler and service of the fno and options lanes, and is idempotent. The integrator may also
//! call `crate::lanes::fno::install(&state)` right after `build_state` so the very first page
//! of another lane already sees it (DECISIONS-RS R8.F2).

use std::sync::{OnceLock, RwLock};

use crate::app::AppState;

fn slot() -> &'static RwLock<Option<AppState>> {
    static SLOT: OnceLock<RwLock<Option<AppState>>> = OnceLock::new();
    SLOT.get_or_init(|| RwLock::new(None))
}

/// Hand the desk's state to the template globals (first call wins; later calls are no-ops
/// unless the state's database differs, as a test building a second app does).
pub fn install(state: &AppState) {
    if let Ok(read) = slot().read()
        && let Some(existing) = read.as_ref()
        && existing.paths == state.paths
        && std::ptr::eq(
            std::sync::Arc::as_ptr(&existing.settings),
            std::sync::Arc::as_ptr(&state.settings),
        )
    {
        return;
    }
    if let Ok(mut write) = slot().write() {
        *write = Some(state.clone());
    }
}

/// The installed state, if any.
#[must_use]
pub fn installed() -> Option<AppState> {
    slot().read().ok().and_then(|s| s.clone())
}
