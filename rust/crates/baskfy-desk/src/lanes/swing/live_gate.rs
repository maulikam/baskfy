//! Moved: `app/swing_live_gate.py`'s port lives in [`super::swing_exec::live_gate`] (lane
//! swing_exec). This path re-exports it so the swing siblings' `super::live_gate::…` imports
//! keep resolving.

pub use super::swing_exec::live_gate::*;
