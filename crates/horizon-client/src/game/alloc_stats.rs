//! Diagnostics (Voxaryn P1.1): the global allocator for the `alloc-stats` and
//! `exp-mimalloc` features; neither is in a default build, and the game code
//! stays free of `unsafe` (the allocators come from their crates).
//!
//! - `alloc-stats`: the system allocator wrapped by `stats_alloc`, which
//!   counts every allocation in process-wide atomics. Those atomics add
//!   contention of their own, so builds with it are for **counting** only,
//!   never for timing.
//! - `exp-mimalloc`: mimalloc instead of the system allocator (an A/B for
//!   allocator contention on the worker pool).
#[cfg(all(feature = "alloc-stats", feature = "exp-mimalloc"))]
compile_error!("alloc-stats and exp-mimalloc are separate experiments: enable one");

#[cfg(feature = "alloc-stats")]
#[global_allocator]
static GLOBAL: &stats_alloc::StatsAlloc<std::alloc::System> = &stats_alloc::INSTRUMENTED_SYSTEM;

/// Allocations (with reallocations) and bytes the whole process asked for so far.
#[cfg(feature = "alloc-stats")]
pub fn counts() -> (u64, u64) {
    let s = GLOBAL.stats();
    (
        (s.allocations + s.reallocations) as u64,
        s.bytes_allocated as u64,
    )
}

#[cfg(feature = "exp-mimalloc")]
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;
