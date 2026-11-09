//! Experimental (Voxaryn P2 feasibility, `p2-speculate` builds only): what a
//! mob's tick read of the other mobs, recorded on the thread that ticks it.
//!
//! While recording, every read of the snapshot through `MobWorld::others_near`
//! and `MobWorld::seen` (`nearby`, `target_info` and the goals go through
//! them) is counted, except the one `push_entities` makes, whose outcome (the
//! list of mobs it pushes, in order, with the positions it read) is kept
//! instead: the shadow speculation (game/speculate.rs) re-runs exactly that
//! query against the true snapshot to validate a speculative tick, and
//! refuses any tick that read other mobs in any other way.
use super::Who;
use horizon_core::geometry::Vec3;
use std::cell::{Cell, RefCell};

thread_local! {
    static ON: Cell<bool> = const { Cell::new(false) };
    static IN_PUSH: Cell<bool> = const { Cell::new(false) };
    static OTHER_READS: Cell<u32> = const { Cell::new(0) };
    static PUSHES: RefCell<Option<Vec<(Who, Vec3)>>> = const { RefCell::new(None) };
}

/// What one mob's tick read of the others.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Reads {
    /// Reads other than the push query.
    pub other: u32,
    /// The push query's answer: who it pushed, in order, at what position.
    pub pushed: Option<Vec<(Who, Vec3)>>,
}

/// Starts recording this thread's reads.
pub fn begin() {
    ON.set(true);
    IN_PUSH.set(false);
    OTHER_READS.set(0);
    PUSHES.with(|p| *p.borrow_mut() = None);
}
/// Stops recording and returns what was read.
pub fn end() -> Reads {
    ON.set(false);
    Reads {
        other: OTHER_READS.get(),
        pushed: PUSHES.with(|p| p.borrow_mut().take()),
    }
}
/// A read of the snapshot (`others_near`, `seen`).
#[inline]
pub fn note_read() {
    if ON.get() && !IN_PUSH.get() {
        OTHER_READS.set(OTHER_READS.get() + 1);
    }
}
/// Runs `push_entities`' query: its own reads are not "other" reads.
#[inline]
pub fn push_query<R>(f: impl FnOnce() -> R) -> R {
    let was = IN_PUSH.replace(true);
    let out = f();
    IN_PUSH.set(was);
    out
}
/// The push query's answer.
#[inline]
pub fn record_pushes(targets: &[(Who, Vec3)]) {
    if ON.get() {
        PUSHES.with(|p| {
            let mut p = p.borrow_mut();
            if p.is_some() {
                // A second push query in one tick: count it as an other read.
                OTHER_READS.set(OTHER_READS.get() + 1);
            } else {
                *p = Some(targets.to_vec());
            }
        });
    }
}
