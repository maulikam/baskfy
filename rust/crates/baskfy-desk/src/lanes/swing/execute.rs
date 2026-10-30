//! Moved: `app/swing_execute.py`'s port lives in [`super::swing_exec::execute`] (lane
//! swing_exec). This path re-exports it so the swing siblings' `super::execute::…` imports
//! keep resolving.

pub use super::swing_exec::execute::*;
