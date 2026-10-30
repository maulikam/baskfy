//! Lane **twt_exec** (parent `twt`): `app/twt_execute.py` and `app/twt_auto.py`.
//!
//! The code itself lives where the earlier, coarser run of wave D put it and where the parent
//! file's routes already reach it — [`super::execute`] (`twt_execute.py`: `execute_line`, the
//! four chores, `halt_sleeve`, `on_order_update`) and [`super::auto`] (`twt_auto.py`: the
//! `twt-auto` service and the session supervisor's `drain_now`). This module re-exports both
//! under the lane's name and holds the lane's tests, which port `tests/test_twt_auto.py` and the
//! refusal half of `tests/test_twt_execute.py` as the **spec**, not as current behaviour:
//!
//! * the TWT auto-execute flag defaults **false**, and only one of the eight cells of
//!   (`DRY_RUN`, `BASKFY_TWT_EXECUTION_ENABLED`, `BASKFY_TWT_AUTO_EXECUTE`) auto-executes —
//!   `(false, true, true)` (DECISIONS-TW TW17; the root's non-negotiable 1, second exception);
//! * with any flag off nothing is opened, read or sent, and the drain refuses on its own;
//! * on, every `PROPOSED` line of **today's MORNING or LIVE plan** (TW19) goes once, in the
//!   plan's own order (`SELL_AT_OPEN` first since TW20), through the same `execute_line` a
//!   click reaches with `confirm="true"`; one line's failure does not cost the rest;
//! * it never builds a plan: none, EVENING, stale, expired, before 09:15, at or after 15:30, a
//!   weekend or an exchange holiday all send nothing;
//! * `execute_line` refuses without `confirm=true` (400), for an unknown plan (404), an expired
//!   one — at the instant it expires too (410) — a line of another plan (404), a kind this
//!   sleeve does not execute (400) and a line already confirmed (409), all before a broker is
//!   asked anything; the sleeve's gates are CNC-only and dry unless `DRY_RUN=false` **and**
//!   `BASKFY_TWT_EXECUTION_ENABLED=true`.

pub use super::auto;
pub use super::execute;

pub mod auto_loop;

#[cfg(test)]
mod memory;

#[cfg(test)]
mod rig;

#[cfg(test)]
mod tests;

#[cfg(test)]
mod execute_tests;

#[cfg(test)]
mod auto_tests;

#[cfg(test)]
mod real_path_tests;
