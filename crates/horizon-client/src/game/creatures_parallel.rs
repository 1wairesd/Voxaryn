//! Experimental parallel mob pass (Voxaryn P1). Built only with the
//! `parallel-entities` feature and run only when `HORIZON_ENTITY_THREADS`
//! is set; otherwise `tick_creature_entities` runs [`mob_pass`] over every
//! mob as before.
//!
//! The pass splits the tick's mobs into islands (`mobs::islands`), gives
//! each island its own copy of everything the sequential pass would share
//! (the snapshot and its grid, pending pushes, the event lists, the died set
//! and guest bindings of its own mobs, a copy of the level random), runs
//! [`mob_pass`] on each island on a bounded worker pool, and merges the
//! results back **in the original mob order**. Islands share nothing
//! mutable, so the result cannot depend on how the threads are scheduled.
//!
//! It declines (returns false, touching nothing) unless every mob is of a
//! verified species with no rider, mount or lead, ids are unique, and the
//! plan has at least two islands. `HORIZON_ENTITY_VERIFY=N` also runs the
//! sequential pass on a copy every N-th parallel pass (1 = every one),
//! compares the complete results and keeps the sequential ones on any
//! difference. Each check covers **one step** from the state the tick
//! started with: a difference made by an unchecked pass is carried into
//! that state and never compared, so only `VERIFY=1` checks every tick
//! (N > 1 is sampling, P1.1 audit finding).
use super::creatures::{Mark, PassInput, PassOut, mob_pass};
use horizon_client::mobs::{Mob, MobEvent, Seen, islands, path::stats};
use horizon_core::random::LegacyRandom;
use rayon::prelude::*;
use std::{
    cell::RefCell,
    collections::{HashMap, HashSet},
    fmt::Write as _,
    hash::{DefaultHasher, Hasher},
    sync::{
        OnceLock,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::Instant,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Order {
    /// Largest island first (the default: the critical path starts first).
    Largest,
    /// Smallest first: a different schedule, for testing.
    Smallest,
    /// A seeded shuffle, for testing.
    Shuffle(u64),
}
struct Config {
    threads: usize,
    /// Check one parallel pass in this many against the sequential pass
    /// (`HORIZON_ENTITY_VERIFY=N`; 0 = never, 1 = every pass).
    verify: u64,
    order: Order,
    min_mobs: usize,
    /// `HORIZON_ENTITY_UNSAFE_REACH`: a reach radius forced below the proven
    /// one, **only** to show that verify mode catches a wrong plan. Exists
    /// only in builds with the `parallel-entities-unsafe-test` feature; any
    /// other build ignores the variable.
    #[cfg(feature = "parallel-entities-unsafe-test")]
    unsafe_reach: Option<f64>,
    /// Test-only (`HORIZON_ENTITY_UNSAFE_NO_MOTION_BOUND=1`): plan with P1's
    /// fixed margin instead of the proven step.
    #[cfg(feature = "parallel-entities-unsafe-test")]
    unsafe_no_motion_bound: bool,
    /// Test-only (`HORIZON_ENTITY_UNSAFE_NO_GUARD=1`): no copy, no redo.
    #[cfg(feature = "parallel-entities-unsafe-test")]
    unsafe_no_guard: bool,
    /// Test-only (`HORIZON_ENTITY_UNSAFE_SPECIES=axolotl,bee`): species let
    /// through the gate (the species and level random checks) though unverified.
    #[cfg(feature = "parallel-entities-unsafe-test")]
    unsafe_species: Vec<String>,
    /// Test-only (`HORIZON_ENTITY_UNSAFE_INJECT=attack:N` or `target:N`,
    /// P1.3.1): on the N-th pass that reaches the split, a mob of one island
    /// hits (or alerts) a mob of another that ticks after it.
    #[cfg(feature = "parallel-entities-unsafe-test")]
    unsafe_inject: Option<(super::creatures::InjectKind, u64)>,
}
fn config() -> &'static Config {
    static CONFIG: OnceLock<Config> = OnceLock::new();
    CONFIG.get_or_init(|| {
        let env = |k: &str| std::env::var(k).ok();
        let threads = env("HORIZON_ENTITY_THREADS")
            .and_then(|v| v.parse().ok())
            .unwrap_or(0);
        let order = match env("HORIZON_ENTITY_ORDER").as_deref() {
            Some("smallest") => Order::Smallest,
            Some(s) if s.starts_with("shuffle") => {
                Order::Shuffle(s.trim_start_matches("shuffle:").parse().unwrap_or(1))
            }
            _ => Order::Largest,
        };
        Config {
            threads,
            verify: env("HORIZON_ENTITY_VERIFY")
                .and_then(|v| v.parse().ok())
                .unwrap_or(0),
            order,
            min_mobs: env("HORIZON_ENTITY_MIN_MOBS")
                .and_then(|v| v.parse().ok())
                .unwrap_or(64),
            #[cfg(feature = "parallel-entities-unsafe-test")]
            unsafe_no_motion_bound: env("HORIZON_ENTITY_UNSAFE_NO_MOTION_BOUND")
                .is_some_and(|v| v != "0"),
            #[cfg(feature = "parallel-entities-unsafe-test")]
            unsafe_no_guard: env("HORIZON_ENTITY_UNSAFE_NO_GUARD").is_some_and(|v| v != "0"),
            #[cfg(feature = "parallel-entities-unsafe-test")]
            unsafe_species: env("HORIZON_ENTITY_UNSAFE_SPECIES")
                .map(|v| {
                    v.split(',')
                        .map(|s| s.trim().to_ascii_lowercase())
                        .filter(|s| !s.is_empty())
                        .collect()
                })
                .unwrap_or_default(),
            #[cfg(feature = "parallel-entities-unsafe-test")]
            unsafe_inject: env("HORIZON_ENTITY_UNSAFE_INJECT").and_then(|v| {
                use super::creatures::InjectKind as K;
                let (kind, n) = v.split_once(':').unwrap_or((v.as_str(), "50"));
                let kind = match kind {
                    "attack" => K::Attack,
                    "attack_same" => K::AttackSame,
                    "target" => K::Alert,
                    "push" => K::Push,
                    "pushed_attack" => K::PushedAttack,
                    _ => return None,
                };
                Some((kind, n.parse().unwrap_or(50)))
            }),
            #[cfg(feature = "parallel-entities-unsafe-test")]
            unsafe_reach: env("HORIZON_ENTITY_UNSAFE_REACH")
                .and_then(|v| v.parse().ok())
                .filter(|r: &f64| *r > 0.0),
        }
    })
}
/// The bounded worker pool, built once with `HORIZON_ENTITY_THREADS` threads.
pub(super) fn pool() -> Option<&'static rayon::ThreadPool> {
    static POOL: OnceLock<Option<rayon::ThreadPool>> = OnceLock::new();
    POOL.get_or_init(|| {
        let n = config().threads;
        if n == 0 {
            return None;
        }
        let built = rayon::ThreadPoolBuilder::new()
            .num_threads(n)
            .thread_name(|i| format!("horizon-entity-{i}"))
            .build();
        match built {
            Ok(p) => {
                log::info!(
                    "Horizon: experimental parallel entity pass on {n} worker threads \
                     (verify {}, order {:?})",
                    config().verify,
                    config().order
                );
                #[cfg(feature = "parallel-entities-unsafe-test")]
                if let Some(r) = config().unsafe_reach {
                    log::warn!(
                        "Horizon: HORIZON_ENTITY_UNSAFE_REACH={r}: islands are planned with a \
                         reach radius that is NOT proven safe (verify-mode testing only)"
                    );
                }
                #[cfg(not(feature = "parallel-entities-unsafe-test"))]
                if std::env::var_os("HORIZON_ENTITY_UNSAFE_REACH").is_some() {
                    log::warn!(
                        "Horizon: HORIZON_ENTITY_UNSAFE_REACH is ignored: this build has no \
                         parallel-entities-unsafe-test feature"
                    );
                }
                Some(p)
            }
            Err(e) => {
                log::error!("Horizon: no parallel entity pool: {e}");
                None
            }
        }
    })
    .as_ref()
}
/// Set when an island broke an invariant the audit promised (it drew from
/// the level random): the pass stays off from then on.
static DISABLED: AtomicBool = AtomicBool::new(false);
/// The movement step the last pass proved (f64 bits), where the next pass
/// tries first.
static LAST_STEP: AtomicU64 = AtomicU64::new(0);
/// Passes left that skip the movement proof after one failed (they run
/// sequentially, as a failed proof would make them).
static MOTION_BACKOFF: AtomicU64 = AtomicU64::new(0);
const MOTION_BACKOFF_PASSES: u64 = 20;
/// Parallel passes so far, for `HORIZON_ENTITY_VERIFY=N` sampling.
static VERIFY_SEQ: AtomicU64 = AtomicU64::new(0);
/// Test-only: passes that reached the split, for `HORIZON_ENTITY_UNSAFE_INJECT`.
#[cfg(feature = "parallel-entities-unsafe-test")]
static INJECT_SEQ: AtomicU64 = AtomicU64::new(0);
/// Runs `f` with the test-only fault injection set on this thread (a no-op
/// without the test feature).
#[cfg(feature = "parallel-entities-unsafe-test")]
fn injecting<R>(inj: Option<super::creatures::Inject>, f: impl FnOnce() -> R) -> R {
    super::creatures::INJECT.set(inj);
    let r = f();
    super::creatures::INJECT.set(None);
    r
}
#[cfg(not(feature = "parallel-entities-unsafe-test"))]
fn injecting<R>(_: Option<()>, f: impl FnOnce() -> R) -> R {
    f()
}

/// One island as [`stranded_hits`] reads it: its mobs' ids, its events, and
/// the mobs its pass had writes for and could not reach.
type IslandView<'a> = (&'a [u32], &'a [MobEvent], &'a [u32]);

/// The writes an island aimed at a mob of **another** island of this tick,
/// which the sequential pass would have made at once:
/// - hits (`AttackMob`) and alerts (`SetTarget`) left unapplied in its
///   events (`mobs::apply_hit_events` lands those on its own mobs during the
///   attacker's tick, P1.3 B);
/// - pushes, brain memories (`BreedTarget`, `LockGaze`) and a bred partner's
///   rest its pass could not deliver (P1.3.1: dropped silently before).
///
/// The tick's ids are taken from the islands themselves: the pass's own mob
/// list is empty while the islands hold its mobs, and the P1.3 check built
/// its set from that list, so it caught nothing (P1.3.1). A write aimed at a
/// mob of its own island (one removed this tick) or at none of this tick's
/// is left alone, as the sequential pass leaves it.
fn stranded_hits<'a>(islands: impl Iterator<Item = IslandView<'a>> + Clone) -> usize {
    let aimed = |events: &'a [MobEvent], undelivered: &'a [u32]| {
        events
            .iter()
            .filter_map(|e| match e {
                MobEvent::AttackMob { target, .. } => Some(*target),
                MobEvent::SetTarget { mob, .. } => Some(*mob),
                _ => None,
            })
            .chain(undelivered.iter().copied())
    };
    if islands
        .clone()
        .all(|(_, events, undelivered)| aimed(events, undelivered).next().is_none())
    {
        return 0;
    }
    // Which island each mob of the tick is in.
    let owner: horizon_client::fast_hash::FxHashMap<u32, usize> = islands
        .clone()
        .enumerate()
        .flat_map(|(r, (ids, _, _))| ids.iter().map(move |&id| (id, r)))
        .collect();
    islands
        .enumerate()
        .map(|(r, (_, events, undelivered))| {
            aimed(events, undelivered)
                .filter(|id| owner.get(id).is_some_and(|&o| o != r))
                .count()
        })
        .sum()
}

/// One island's private copy of the pass state, and what came out of it.
struct Island {
    /// The island's snapshot indices in the tick, ascending.
    members: Vec<u32>,
    /// The ids of those mobs (the died set and bindings are merged by id).
    ids: Vec<u32>,
    mobs: Vec<Mob>,
    snapshot: Vec<Seen>,
    died: HashSet<u32>,
    bindings: HashMap<u32, u32>,
    scratch: LegacyRandom,
    events: Vec<MobEvent>,
    dead: Vec<Mob>,
    guest_events: Vec<(u32, MobEvent)>,
    marks: Vec<Mark>,
    stats: stats::PathStats,
    work_ns: u128,
    /// The pool thread that ran it.
    worker: usize,
    /// Where its mobs stood at the start of the tick (the plan's positions).
    start: Vec<horizon_core::geometry::Vec3>,
    /// The mobs its pass had writes for and could not reach (pushes, brain
    /// memories, a partner's rest; `creatures::take_undelivered`, P1.3.1).
    undelivered: Vec<u32>,
}

/// The pass's state before the islands ran, to redo it sequentially.
struct Guard {
    mobs: Vec<Mob>,
    snapshot: Vec<Seen>,
    died: HashSet<u32>,
    bindings: HashMap<u32, u32>,
    scratch: LegacyRandom,
}

/// What a sequential pass over a copy gave (`HORIZON_ENTITY_VERIFY`).
struct Reference {
    mobs: Vec<Mob>,
    died: HashSet<u32>,
    bindings: HashMap<u32, u32>,
    scratch: LegacyRandom,
    events: Vec<MobEvent>,
    dead: Vec<Mob>,
    guest_events: Vec<(u32, MobEvent)>,
}

/// Counters over the passes between two `PARSTATS` lines (main thread only).
#[derive(Default)]
struct Agg {
    calls: u64,
    parallel: u64,
    fallbacks: Vec<(&'static str, u64)>,
    islands: u64,
    largest: u64,
    mobs: u64,
    work_ms: f64,
    critical_ms: f64,
    plan_ms: f64,
    split_ms: f64,
    run_ms: f64,
    merge_ms: f64,
    verify_ms: f64,
    verified: u64,
    mismatches: u64,
    /// The last species that kept a pass sequential.
    blocking_species: Option<String>,
    // P1.1 diagnostics (parallel passes only).
    gate_ms: f64,
    /// The busiest worker's island time (its makespan without dispatch).
    busy_max_ms: f64,
    est_speedup: f64,
    allocs: u64,
    alloc_bytes: u64,
    /// The largest movement step a plan allowed (blocks along x or z).
    step_max: f64,
    // P1.2: the copy taken before the pass, and the passes redone from it.
    guard_ms: f64,
    tripped: u64,
    redone: u64,
}
thread_local! {
    static AGG: RefCell<Agg> = RefCell::new(Agg::default());
}
fn fallback(reason: &'static str) -> bool {
    AGG.with(|a| {
        let mut a = a.borrow_mut();
        match a.fallbacks.iter_mut().find(|(r, _)| *r == reason) {
            Some((_, n)) => *n += 1,
            None => a.fallbacks.push((reason, 1)),
        }
    });
    report();
    false
}
/// Logs the counters once every 20 passes (one second at 20 TPS).
fn report() {
    AGG.with(|a| {
        let mut a = a.borrow_mut();
        a.calls += 1;
        if a.calls < 20 {
            return;
        }
        let p = a.parallel.max(1) as f64;
        let fallbacks: Vec<String> = a
            .fallbacks
            .iter()
            .map(|(r, n)| format!("{r}={n}"))
            .collect();
        log::info!(
            "Horizon: PARSTATS passes {}, parallel {}, threads {}, islands {:.1}, largest {:.1}, \
             mobs {:.1}, work {:.3} ms, critical {:.3} ms, bound {:.2}x, plan {:.3} ms, \
             split {:.3} ms, run {:.3} ms, merge {:.3} ms, verify {:.3} ms, verified {}, \
             mismatches {}, fallbacks [{}], blocking {}, gate {:.3} ms, busy_max {:.3} ms, \
             est {:.2}x, allocs {:.0}, alloc_kb {:.1}, step {:.2}, guard {:.3} ms, tripped {}, \
             redone {}",
            a.calls,
            a.parallel,
            config().threads,
            a.islands as f64 / p,
            a.largest as f64 / p,
            a.mobs as f64 / p,
            a.work_ms / p,
            a.critical_ms / p,
            if a.critical_ms > 0.0 {
                a.work_ms / a.critical_ms
            } else {
                0.0
            },
            a.plan_ms / p,
            a.split_ms / p,
            a.run_ms / p,
            a.merge_ms / p,
            a.verify_ms / p,
            a.verified,
            a.mismatches,
            fallbacks.join(" "),
            a.blocking_species.as_deref().unwrap_or("-"),
            a.gate_ms / p,
            a.busy_max_ms / p,
            a.est_speedup / p,
            a.allocs as f64 / p,
            a.alloc_bytes as f64 / p / 1024.0,
            a.step_max,
            a.guard_ms / p,
            a.tripped,
            a.redone,
        );
        *a = Agg::default();
    });
}

/// Why this tick's mobs must run sequentially, if they must. Only the mobs
/// that tick this pass must be of a verified species: one outside the
/// entity-ticking chunks (or in an unloaded one) only checks despawning,
/// which reads itself and the nearest player.
fn gate<T: Fn(i32, i32) -> bool>(
    input: &PassInput<'_, '_, T>,
    out: &PassOut<'_>,
) -> Option<&'static str> {
    if DISABLED.load(Ordering::Relaxed) {
        return Some("disabled");
    }
    if cfg!(feature = "rng-trace") {
        // Its trace buffer records draws in execution order.
        return Some("rng-trace");
    }
    if out.mobs.len() < config().min_mobs {
        return Some("few");
    }
    if out.snapshot.len() != out.mobs.len()
        || !out.events.is_empty()
        || !out.dead.is_empty()
        || !out.guest_events.is_empty()
    {
        return Some("state");
    }
    let mut ids = HashSet::with_capacity(out.mobs.len());
    for m in out.mobs.iter() {
        let (x, z) = (m.position.x.floor() as i32, m.position.z.floor() as i32);
        let ticks = input.base.world.loaded(x, z) && (input.ticking)(x, z);
        // Test-only: species let through though unverified (negative tests).
        #[cfg(feature = "parallel-entities-unsafe-test")]
        let forced = {
            let name = format!("{:?}", m.species()).to_ascii_lowercase();
            config().unsafe_species.contains(&name)
        };
        #[cfg(not(feature = "parallel-entities-unsafe-test"))]
        let forced = false;
        if ticks && !forced && !islands::verified(m.species()) {
            AGG.with(|a| a.borrow_mut().blocking_species = Some(format!("{:?}", m.species())));
            return Some("species");
        }
        // P1.2: the level random stays out of the parallel pass by
        // construction: no mob whose tick can reach it is let in (checked
        // apart from `verified`, at the source in `ai::reaches_level_random`).
        if ticks && !forced && horizon_client::mobs::ai::reaches_level_random(m.species()) {
            AGG.with(|a| a.borrow_mut().blocking_species = Some(format!("{:?}", m.species())));
            return Some("level-random");
        }
        if m.rider.is_some()
            || m.extra.mount.is_some()
            || m.extra.rider.is_some()
            || m.extra.leash.is_some()
            || m.extra.leash_pending.is_some()
        {
            return Some("links");
        }
        if !ids.insert(m.id) {
            return Some("duplicate-ids");
        }
    }
    None
}

/// Feeds `Debug` output straight into a hasher (no huge strings).
struct HashWriter(DefaultHasher);
impl std::fmt::Write for HashWriter {
    fn write_str(&mut self, s: &str) -> std::fmt::Result {
        self.0.write(s.as_bytes());
        Ok(())
    }
}
pub(super) fn digest(v: &impl std::fmt::Debug) -> u64 {
    let mut w = HashWriter(DefaultHasher::new());
    let _ = write!(w, "{v:?}");
    w.0.finish()
}
fn sorted_set(s: &HashSet<u32>) -> Vec<u32> {
    let mut v: Vec<u32> = s.iter().copied().collect();
    v.sort_unstable();
    v
}
fn sorted_map(m: &HashMap<u32, u32>) -> Vec<(u32, u32)> {
    let mut v: Vec<(u32, u32)> = m.iter().map(|(k, v)| (*k, *v)).collect();
    v.sort_unstable();
    v
}
/// The first difference between the parallel result and the reference, if
/// any, described for the log.
/// The byte offset where two strings first differ (the shorter one's length
/// when one is a prefix of the other).
fn first_difference(a: &str, b: &str) -> usize {
    a.bytes()
        .zip(b.bytes())
        .position(|(x, y)| x != y)
        .unwrap_or(a.len().min(b.len()))
}
/// Up to `radius` bytes of `s` on each side of byte `at`, cut on character
/// boundaries (a mob's `Debug` holds names in any script: slicing at a raw
/// byte offset could split a character and panic).
fn excerpt(s: &str, at: usize, radius: usize) -> &str {
    let mut from = at.saturating_sub(radius).min(s.len());
    while !s.is_char_boundary(from) {
        from -= 1;
    }
    let mut to = at.saturating_add(radius).min(s.len());
    while !s.is_char_boundary(to) {
        to += 1;
    }
    &s[from..to]
}
fn difference(out: &PassOut<'_>, r: &Reference) -> Option<String> {
    if out.mobs.len() != r.mobs.len() {
        return Some(format!(
            "{} mobs, sequential {}",
            out.mobs.len(),
            r.mobs.len()
        ));
    }
    for (a, b) in out.mobs.iter().zip(&r.mobs) {
        if digest(a) != digest(b) {
            let (sa, sb) = (format!("{a:?}"), format!("{b:?}"));
            let at = first_difference(&sa, &sb);
            return Some(format!(
                "mob {} differs: parallel …{}… sequential …{}…",
                a.id,
                excerpt(&sa, at, 120),
                excerpt(&sb, at, 120)
            ));
        }
    }
    if digest(&*out.events) != digest(&r.events) {
        return Some(format!(
            "events differ ({} vs {})",
            out.events.len(),
            r.events.len()
        ));
    }
    if digest(&*out.guest_events) != digest(&r.guest_events) {
        return Some("guest events differ".into());
    }
    if digest(&*out.dead) != digest(&r.dead) {
        return Some("dead mobs differ".into());
    }
    if sorted_set(out.died) != sorted_set(&r.died) {
        return Some("died sets differ".into());
    }
    if sorted_map(out.bindings) != sorted_map(&r.bindings) {
        return Some("guest bindings differ".into());
    }
    if digest(&*out.scratch) != digest(&r.scratch) {
        return Some("level random differs".into());
    }
    None
}

/// Runs the tick's mob pass in parallel islands, or returns false (leaving
/// `out` untouched) when it cannot prove the result equal to the
/// sequential pass.
pub(super) fn try_pass<T: Fn(i32, i32) -> bool + Sync>(
    input: &PassInput<'_, '_, T>,
    out: &mut PassOut<'_>,
) -> bool {
    let Some(pool) = pool() else {
        return false;
    };
    let cfg = config();
    let t_gate = Instant::now();
    if let Some(reason) = gate(input, out) {
        return fallback(reason);
    }
    // ---- plan ---------------------------------------------------------------
    // (Only the server thread runs the pass: a plain load and store.)
    let skip = MOTION_BACKOFF.load(Ordering::Relaxed);
    if skip > 0 {
        MOTION_BACKOFF.store(skip - 1, Ordering::Relaxed);
        return fallback("motion");
    }
    let t_plan = Instant::now();
    let ticks_at = |g: u32| {
        let p = out.mobs[g as usize].position;
        let (x, z) = (p.x.floor() as i32, p.z.floor() as i32);
        input.base.world.loaded(x, z) && (input.ticking)(x, z)
    };
    // How far a mob may move along x or z this pass, proven before it runs
    // (P1.2, `islands::motion_step`); no proof, no parallel pass.
    let motions: Option<Vec<islands::Motion>> = out
        .mobs
        .iter()
        .zip(out.snapshot.iter())
        .enumerate()
        .map(|(g, (m, s))| islands::Motion::of(m, s.bounds, ticks_at(g as u32)))
        .collect();
    // The contacts are counted on the worker pool (read-only, one mob each).
    let proven = motions.as_deref().and_then(|ms| {
        let grid = islands::ContactGrid::new(ms);
        let count = |s: f64| -> Vec<u32> {
            pool.install(|| {
                (0..ms.len())
                    .into_par_iter()
                    .with_min_len(64)
                    .map(|i| grid.count(i, s))
                    .collect()
            })
        };
        // Any step that holds is a bound: try last pass's first (one count
        // most ticks), then search from below.
        let last = f64::from_bits(LAST_STEP.load(Ordering::Relaxed));
        let warm = (last > 0.0)
            .then(|| islands::motion_step_holds(ms, last, &count))
            .flatten();
        warm.or_else(|| islands::motion_step_with(ms, &count))
    });
    if let Some(s) = proven {
        LAST_STEP.store(s.to_bits(), Ordering::Relaxed);
    }
    // Test-only: plan with P1's fixed margin and no proof (so the check after
    // the pass and the redo are what keep the result right).
    #[cfg(feature = "parallel-entities-unsafe-test")]
    let proven = if cfg.unsafe_no_motion_bound {
        Some(4.0)
    } else {
        proven
    };
    let Some(step) = proven else {
        // A crowd too dense to prove stays so for a while: do not pay the
        // failed proof (up to 8 counts) every tick.
        MOTION_BACKOFF.store(MOTION_BACKOFF_PASSES, Ordering::Relaxed);
        return fallback("motion");
    };
    let reach = out
        .mobs
        .iter()
        .map(|m| m.attrs.follow_range)
        .fold(16.0_f64, f64::max)
        + islands::reach_margin(step);
    // Only a build with the test-only feature can plan with a radius below
    // the proven one (to show that verify mode catches a wrong plan).
    #[cfg(feature = "parallel-entities-unsafe-test")]
    let reach = cfg.unsafe_reach.unwrap_or(reach);
    let held = islands::held_links(out.mobs);
    let plan = islands::plan(out.snapshot, reach, &held);
    let plan_ms = t_plan.elapsed().as_secs_f64() * 1e3;
    if plan.islands.len() < 2 {
        return fallback("one-island");
    }
    // Worth it only when the ticking work spreads over islands: a mob outside
    // the entity-ticking chunks only checks despawning, so islands of those
    // cost next to nothing. With one island holding most of the ticking mobs
    // the pass could gain under 1.33x and still pays the split and merge.
    let per_island: Vec<usize> = plan
        .islands
        .iter()
        .map(|isl| isl.iter().filter(|&&g| ticks_at(g)).count())
        .collect();
    let ticking_total: usize = per_island.iter().sum();
    let ticking_top = per_island.iter().copied().max().unwrap_or(0);
    // Diagnostic (P1.1): the speedup of the pass a cost model predicts, with
    // a ticking mob as the unit of cost and a greedy largest-first schedule
    // over the workers. Not used to decide (see p1-stabilization.md).
    let est_speedup = {
        let makespan = islands::makespan(&per_island, cfg.threads.max(1));
        if makespan > 0 {
            ticking_total as f64 / makespan as f64
        } else {
            0.0
        }
    };
    let gate_ms = t_gate.elapsed().as_secs_f64() * 1e3 - plan_ms;
    if ticking_total == 0 || ticking_top * 4 >= ticking_total * 3 {
        return fallback("dominant");
    }
    let largest = plan.largest();
    // Test-only fault injection: on the chosen pass, the first ticking mob of
    // one island hits (alerts) a ticking mob of another that comes after it
    // in the tick's order, so the sequential pass applies it before that mob
    // ticks.
    #[cfg(feature = "parallel-entities-unsafe-test")]
    let inject = cfg.unsafe_inject.and_then(|(kind, at)| {
        if INJECT_SEQ.fetch_add(1, Ordering::Relaxed) != at {
            return None;
        }
        let live = |g: u32| ticks_at(g) && out.mobs[g as usize].alive();
        if kind == super::creatures::InjectKind::PushedAttack {
            // Pusher c and target b in island X, attacker a in island Y, c
            // ticking before a and b not between them (b < c < a: the push
            // arrives after b's tick; c < a < b: before it), so b's push is
            // still pending when a hits it.
            let (c, a, b) = plan.islands.iter().enumerate().find_map(|(ix, x)| {
                plan.islands
                    .iter()
                    .enumerate()
                    .filter(|&(iy, _)| iy != ix)
                    .find_map(|(_, y)| {
                        let &a = y.iter().find(|&&g| live(g))?;
                        let &c = x.iter().rev().find(|&&g| g < a && live(g))?;
                        let &b = x
                            .iter()
                            .find(|&&g| g > a && live(g))
                            .or_else(|| x.iter().find(|&&g| g < c && live(g)))?;
                        Some((c, a, b))
                    })
            })?;
            let inj = super::creatures::Inject {
                attacker: out.mobs[a as usize].id,
                target: out.mobs[b as usize].id,
                kind,
                pusher: Some(out.mobs[c as usize].id),
            };
            log::warn!(
                "Horizon: TEST fault injection on this pass: {inj:?} (tick order: pusher {c}, \
                 attacker {a}, target {b})"
            );
            return Some(inj);
        }
        let same = kind == super::creatures::InjectKind::AttackSame;
        let (a, b) = plan.islands.iter().enumerate().find_map(|(ia, isl)| {
            let &a = isl.iter().find(|&&g| live(g))?;
            plan.islands
                .iter()
                .enumerate()
                .filter(|&(ib, _)| (ib == ia) == same)
                .find_map(|(_, other)| other.iter().copied().find(|&g| g > a && live(g)))
                .map(|b| (a, b))
        })?;
        let inj = super::creatures::Inject {
            attacker: out.mobs[a as usize].id,
            target: out.mobs[b as usize].id,
            kind,
            pusher: None,
        };
        log::warn!("Horizon: TEST fault injection on this pass: {inj:?}");
        Some(inj)
    });
    #[cfg(not(feature = "parallel-entities-unsafe-test"))]
    let inject: Option<()> = None;
    #[cfg(feature = "parallel-entities-unsafe-test")]
    let injected_before = inject.and_then(|inj| {
        out.mobs
            .iter()
            .find(|m| m.id == inj.target)
            .map(|m| (m.health, m.ai.target, m.velocity))
    });
    // ---- reference (verify mode): the sequential pass on a copy ------------------
    let t_verify = Instant::now();
    let check = cfg.verify > 0
        && VERIFY_SEQ
            .fetch_add(1, Ordering::Relaxed)
            .is_multiple_of(cfg.verify);
    // Test-only: a `PushedAttack` pass is always compared with a sequential
    // pass on copies, whatever HORIZON_ENTITY_VERIFY says.
    #[cfg(feature = "parallel-entities-unsafe-test")]
    let pushed_attack = inject.filter(|i| i.kind == super::creatures::InjectKind::PushedAttack);
    #[cfg(feature = "parallel-entities-unsafe-test")]
    let check = check || pushed_attack.is_some();
    let reference = check.then(|| {
        let mut r = Reference {
            mobs: out.mobs.clone(),
            died: out.died.clone(),
            bindings: out.bindings.clone(),
            scratch: out.scratch.clone(),
            events: Vec::new(),
            dead: Vec::new(),
            guest_events: Vec::new(),
        };
        let mut snapshot = out.snapshot.clone();
        let mut o = PassOut {
            mobs: &mut r.mobs,
            snapshot: &mut snapshot,
            died: &mut r.died,
            bindings: &mut r.bindings,
            scratch: &mut r.scratch,
            events: &mut r.events,
            dead: &mut r.dead,
            guest_events: &mut r.guest_events,
        };
        injecting(inject, || mob_pass(input, &mut o, None));
        r
    });
    // Test-only: what the reference pass saw when the attacker began its
    // tick, and the writes it could not deliver.
    #[cfg(feature = "parallel-entities-unsafe-test")]
    let reference_seen = pushed_attack.map(|_| {
        (
            super::creatures::INJECT_PENDING_AT_HIT.take(),
            super::creatures::take_undelivered(),
            super::creatures::INJECT_HIT_TRACE.take(),
        )
    });
    let mut verify_ms = t_verify.elapsed().as_secs_f64() * 1e3;
    // ---- guard (P1.2): the state before the pass ----------------------------
    // If a check after the pass fails (a mob moved further than the proven
    // step, an island drew from the level random) the parallel result is
    // thrown away and the tick is redone sequentially from this copy: an
    // unproven result never reaches the world.
    let t_guard = Instant::now();
    #[cfg(feature = "parallel-entities-unsafe-test")]
    let keep_guard = !cfg.unsafe_no_guard;
    #[cfg(not(feature = "parallel-entities-unsafe-test"))]
    let keep_guard = true;
    let mut guard = keep_guard.then(|| Guard {
        // Cloned on the pool: a mob's copy is its own (goals, path, effects).
        // (`par_iter_mut`: a `Mob` is `Send` but not `Sync`; it is only read.)
        mobs: pool.install(|| {
            out.mobs
                .par_iter_mut()
                .with_min_len(32)
                .map(|m| m.clone())
                .collect()
        }),
        snapshot: out.snapshot.clone(),
        died: out.died.clone(),
        bindings: out.bindings.clone(),
        scratch: out.scratch.clone(),
    });
    let guard_ms = t_guard.elapsed().as_secs_f64() * 1e3;
    // ---- split --------------------------------------------------------------
    let t_split = Instant::now();
    let n = out.mobs.len();
    // P1's split. P1.1 tried one move per mob (pushing straight into each
    // island's vector): no faster, 0.1-0.2 ms slower (p1-overhead.md), so
    // the slots stay.
    let mut mob_slots: Vec<Option<Mob>> = std::mem::take(out.mobs).into_iter().map(Some).collect();
    let mut seen_slots: Vec<Option<Seen>> =
        std::mem::take(out.snapshot).into_iter().map(Some).collect();
    let mut runs: Vec<Island> = plan
        .islands
        .into_iter()
        .map(|members| {
            let mobs: Vec<Mob> = members
                .iter()
                .map(|&g| {
                    mob_slots[g as usize]
                        .take()
                        .expect("each mob in one island")
                })
                .collect();
            let snapshot: Vec<Seen> = members
                .iter()
                .map(|&g| {
                    seen_slots[g as usize]
                        .take()
                        .expect("each mob in one island")
                })
                .collect();
            let ids: Vec<u32> = mobs.iter().map(|m| m.id).collect();
            let died = ids
                .iter()
                .copied()
                .filter(|id| out.died.contains(id))
                .collect();
            let bindings = ids
                .iter()
                .filter_map(|id| out.bindings.get(id).map(|c| (*id, *c)))
                .collect();
            Island {
                marks: Vec::with_capacity(members.len()),
                start: snapshot.iter().map(|s| s.position).collect(),
                members,
                ids,
                mobs,
                snapshot,
                died,
                bindings,
                scratch: out.scratch.clone(),
                events: Vec::new(),
                dead: Vec::new(),
                guest_events: Vec::new(),
                stats: stats::PathStats::default(),
                work_ns: 0,
                worker: 0,
                undelivered: Vec::new(),
            }
        })
        .collect();
    match cfg.order {
        Order::Largest => runs.sort_by_key(|r| std::cmp::Reverse(r.members.len())),
        Order::Smallest => runs.sort_by_key(|r| r.members.len()),
        Order::Shuffle(seed) => {
            let mut s = seed | 1;
            for i in (1..runs.len()).rev() {
                s = s
                    .wrapping_mul(6_364_136_223_846_793_005)
                    .wrapping_add(1_442_695_040_888_963_407);
                runs.swap(i, (s >> 33) as usize % (i + 1));
            }
        }
    }
    // Where each mob of the tick goes: (island, index in the island).
    let mut owner = vec![(0u32, 0u32); n];
    for (r, isl) in runs.iter().enumerate() {
        for (l, &g) in isl.members.iter().enumerate() {
            owner[g as usize] = (r as u32, l as u32);
        }
    }
    let split_ms = t_split.elapsed().as_secs_f64() * 1e3;
    // ---- run ------------------------------------------------------------------
    #[cfg(feature = "alloc-stats")]
    let allocs_at_run = super::alloc_stats::counts();
    let t_run = Instant::now();
    pool.install(|| {
        runs.par_iter_mut().with_max_len(1).for_each(|isl| {
            isl.worker = rayon::current_thread_index().unwrap_or(0);
            let started = Instant::now();
            // A worker's own diagnostics, handed back with the island.
            let _ = stats::take();
            let mut o = PassOut {
                mobs: &mut isl.mobs,
                snapshot: &mut isl.snapshot,
                died: &mut isl.died,
                bindings: &mut isl.bindings,
                scratch: &mut isl.scratch,
                events: &mut isl.events,
                dead: &mut isl.dead,
                guest_events: &mut isl.guest_events,
            };
            injecting(inject, || mob_pass(input, &mut o, Some(&mut isl.marks)));
            isl.undelivered = super::creatures::take_undelivered();
            isl.stats = stats::take();
            // The worker's span timers would otherwise pile up unread.
            let _ = horizon_client::spans::take();
            isl.work_ns = started.elapsed().as_nanos();
        });
    });
    #[cfg(feature = "alloc-stats")]
    let run_allocs = {
        let after = super::alloc_stats::counts();
        (after.0 - allocs_at_run.0, after.1 - allocs_at_run.1)
    };
    #[cfg(not(feature = "alloc-stats"))]
    let run_allocs = (0u64, 0u64);
    let run_ms = t_run.elapsed().as_secs_f64() * 1e3;
    // ---- merge, in the original mob order ---------------------------------------
    let t_merge = Instant::now();
    // Safety nets: what the plan assumed, checked against what happened.
    let before = out.scratch.state();
    let start_random = digest(&*out.scratch);
    let drew: Vec<usize> = runs
        .iter()
        .enumerate()
        .filter(|(_, isl)| digest(&isl.scratch) != start_random)
        .map(|(r, _)| r)
        .collect();
    let mut alarm: Vec<String> = Vec::new();
    if !drew.is_empty() {
        // No verified species reaches the level random (the gate checks
        // `ai::reaches_level_random`), so a draw means the audit is wrong.
        // Even one island's draws are not trusted: the tick is redone.
        alarm.push(format!(
            "{} island(s) drew from the level random (state {before:x})",
            drew.len()
        ));
    }
    // Every mob must have moved no further along x or z than the plan's step.
    let moved = runs
        .iter()
        .flat_map(|isl| isl.start.iter().zip(&isl.snapshot))
        .filter(|(s, e)| islands::exceeds_step(**s, e.position, step))
        .count();
    if moved > 0 {
        alarm.push(format!(
            "{moved} mobs moved further than the plan's step of {step:.2} blocks: this \
             tick's islands are NOT proven independent"
        ));
    }
    // P1.3 B: a hit or alert left unapplied for a mob of another island.
    // (The tick's ids come from the islands: `out.mobs` was emptied by the
    // split, so a set built from it caught nothing, P1.3.1.)
    let stranded = stranded_hits(runs.iter().map(|isl| {
        (
            isl.ids.as_slice(),
            isl.events.as_slice(),
            isl.undelivered.as_slice(),
        )
    }));
    if stranded > 0 {
        alarm.push(format!(
            "{stranded} write(s) (hit, alert, push or brain memory) aimed at a mob of another island"
        ));
    }
    let tripped = !alarm.is_empty();
    if tripped {
        // The audit or the movement proof was wrong: stop using the pass.
        DISABLED.store(true, Ordering::Relaxed);
        log::error!(
            "Horizon: parallel entity pass: {}; the tick is {}, the pass is now off",
            alarm.join("; "),
            if guard.is_some() {
                "redone sequentially"
            } else {
                "NOT redone (test build without the guard)"
            }
        );
    }
    debug_assert!(runs.iter().all(|isl| isl.marks.len() == isl.members.len()));
    struct Drain {
        mobs: std::vec::IntoIter<Mob>,
        events: std::vec::IntoIter<MobEvent>,
        guest: std::vec::IntoIter<(u32, MobEvent)>,
        dead: std::vec::IntoIter<Mob>,
        marks: Vec<Mark>,
        prev: Mark,
    }
    let (mut work_ms, mut critical_ms) = (0.0_f64, 0.0_f64);
    // Per worker: the islands' time it ran (load balance) and allocations.
    let mut busy = vec![0.0_f64; cfg.threads.max(1)];
    let (allocs, alloc_bytes) = run_allocs;
    let mut drains: Vec<Drain> = Vec::with_capacity(runs.len());
    for isl in runs {
        let ms = isl.work_ns as f64 / 1e6;
        work_ms += ms;
        critical_ms = critical_ms.max(ms);
        if let Some(b) = busy.get_mut(isl.worker) {
            *b += ms;
        }
        stats::add(isl.stats);
        // Each mob's pass touched only its own id in the died set and the
        // bindings, so the island's copy is the truth for its ids.
        for id in &isl.ids {
            if isl.died.contains(id) {
                out.died.insert(*id);
            } else {
                out.died.remove(id);
            }
            out.bindings.remove(id);
        }
        out.bindings.extend(isl.bindings);
        drains.push(Drain {
            mobs: isl.mobs.into_iter(),
            events: isl.events.into_iter(),
            guest: isl.guest_events.into_iter(),
            dead: isl.dead.into_iter(),
            marks: isl.marks,
            prev: Mark {
                kept: true,
                events: 0,
                guest_events: 0,
                dead: 0,
            },
        });
    }
    out.mobs.reserve(n);
    for &(r, l) in &owner {
        let d = &mut drains[r as usize];
        let mark = d.marks[l as usize];
        out.events
            .extend(d.events.by_ref().take(mark.events - d.prev.events));
        out.guest_events.extend(
            d.guest
                .by_ref()
                .take(mark.guest_events - d.prev.guest_events),
        );
        out.dead
            .extend(d.dead.by_ref().take(mark.dead - d.prev.dead));
        if mark.kept {
            out.mobs
                .push(d.mobs.next().expect("a kept mob for every kept mark"));
        }
        d.prev = mark;
    }
    debug_assert!(drains.iter_mut().all(|d| d.mobs.next().is_none()
        && d.events.next().is_none()
        && d.guest.next().is_none()
        && d.dead.next().is_none()));
    let merge_ms = t_merge.elapsed().as_secs_f64() * 1e3;
    // ---- redo (P1.2): a failed check discards the parallel result ---------------
    let mut redone = false;
    if tripped && let Some(g) = guard.take() {
        *out.mobs = g.mobs;
        *out.snapshot = g.snapshot;
        *out.died = g.died;
        *out.bindings = g.bindings;
        *out.scratch = g.scratch;
        out.events.clear();
        out.dead.clear();
        out.guest_events.clear();
        #[cfg(feature = "parallel-entities-unsafe-test")]
        {
            super::creatures::INJECT_PENDING_AT_HIT.set(None);
            super::creatures::INJECT_HIT_TRACE.set(None);
        }
        injecting(inject, || mob_pass(input, out, None));
        redone = true;
    }
    // Test-only (`PushedAttack`): the redo against the sequential reference,
    // bit for bit, and the push delivered exactly once in each.
    #[cfg(feature = "parallel-entities-unsafe-test")]
    if let Some(inj) = pushed_attack {
        let redo_seen = (
            super::creatures::INJECT_PENDING_AT_HIT.take(),
            super::creatures::take_undelivered(),
            super::creatures::INJECT_HIT_TRACE.take(),
        );
        // The hit recomputed from the trace (LivingEntity.knockback(0.4) on
        // the velocity the pending pushes make, each added once): the
        // velocity right after the attacker's tick must be this, bit for bit.
        let knocked = |t: &super::creatures::HitTrace, v: horizon_core::geometry::Vec3| {
            let power = f64::from(0.4f32) * (1.0 - t.resistance);
            let (xd, zd) = (t.from[0] - t.position.x, t.from[2] - t.position.z);
            if power <= 0.0 || xd * xd + zd * zd < f64::from(1.0e-5_f32) {
                return None;
            }
            let len = (xd * xd + zd * zd).sqrt();
            Some([
                (v.x / 2.0 - xd / len * power).to_bits(),
                if t.on_ground {
                    (v.y / 2.0 + power).min(0.4)
                } else {
                    v.y
                }
                .to_bits(),
                (v.z / 2.0 - zd / len * power).to_bits(),
            ])
        };
        let check_once = |t: &Option<super::creatures::HitTrace>| {
            let t = t.as_ref()?;
            let after = t.after?;
            let after = [after.x.to_bits(), after.y.to_bits(), after.z.to_bits()];
            let p = super::creatures::INJECT_PUSH;
            let twice = horizon_core::geometry::Vec3::new(
                t.pushed.x + p[0],
                t.pushed.y + p[1],
                t.pushed.z + p[2],
            );
            Some(format!(
                "injected pushes waiting {}, left after {}, after == once {} (twice {}, none {})",
                t.waiting,
                t.left_after,
                knocked(t, t.pushed) == Some(after),
                knocked(t, twice) == Some(after),
                knocked(t, t.before) == Some(after),
            ))
        };
        let target = |mobs: &[Mob]| {
            mobs.iter().find(|m| m.id == inj.target).map(|m| {
                (
                    m.health.to_bits(),
                    [
                        m.velocity.x.to_bits(),
                        m.velocity.y.to_bits(),
                        m.velocity.z.to_bits(),
                    ],
                    digest(&m.random),
                    digest(m),
                )
            })
        };
        let hits = |events: &[MobEvent]| {
            events
                .iter()
                .filter(|e| matches!(e, MobEvent::MobHit { target, .. } if *target == inj.target))
                .count()
        };
        let pushes_left = |events: &[MobEvent]| {
            events
                .iter()
                .filter(
                    |e| matches!(e, MobEvent::Push { target: horizon_client::mobs::Who::Mob(t), .. } if *t == inj.target),
                )
                .count()
        };
        let r = reference
            .as_ref()
            .expect("a PushedAttack pass has a reference");
        let (ref_pending, ref_undelivered, ref_trace) = reference_seen.clone().unwrap_or_default();
        let (redo_pending, redo_undelivered, redo_trace) = redo_seen;
        log::warn!(
            "Horizon: TEST pushed_attack hit: redo {:?}; reference {:?}",
            check_once(&redo_trace),
            check_once(&ref_trace)
        );
        log::warn!(
            "Horizon: TEST pushed_attack: tripped {tripped}, redone {redone}, \
             pending at hit: redo {redo_pending:?} reference {ref_pending:?}, \
             target undelivered: redo {} reference {}, \
             leftover Push events: redo {} reference {}, MobHit: redo {} reference {}, \
             target (health bits, velocity bits, random digest, mob digest): redo {:x?} reference {:x?}, \
             whole pass equal: {}",
            redo_undelivered.contains(&inj.target),
            ref_undelivered.contains(&inj.target),
            pushes_left(out.events),
            pushes_left(&r.events),
            hits(out.events),
            hits(&r.events),
            target(out.mobs),
            target(&r.mobs),
            difference(out, r).is_none(),
        );
    }
    // Test-only: what the injected hit or alert left in the world.
    #[cfg(feature = "parallel-entities-unsafe-test")]
    if let Some(inj) = inject {
        let after = out
            .mobs
            .iter()
            .find(|m| m.id == inj.target)
            .map(|m| (m.health, m.ai.target, m.velocity));
        let count = |f: &dyn Fn(&MobEvent) -> bool| out.events.iter().filter(|e| f(e)).count();
        let unapplied = count(&|e| match e {
            MobEvent::AttackMob { target, .. } => *target == inj.target,
            MobEvent::SetTarget { mob, .. } => *mob == inj.target,
            _ => false,
        });
        let applied =
            count(&|e| matches!(e, MobEvent::MobHit { target, .. } if *target == inj.target));
        log::warn!(
            "Horizon: TEST fault injection result: tripped {tripped}, redone {redone}, \
             target (health, target, velocity) {injected_before:?} -> {after:?}, unapplied events {unapplied}, \
             MobHit events {applied}"
        );
    }
    // Freeing 2000 copied mobs takes the better part of a millisecond: on a
    // worker, off the server thread.
    if let Some(g) = guard {
        pool.spawn(move || drop(g));
    }
    // ---- verify ---------------------------------------------------------------
    let mut mismatch = false;
    if let Some(r) = reference {
        let t = Instant::now();
        if let Some(what) = difference(out, &r) {
            mismatch = true;
            log::error!("Horizon: parallel entity pass differs from the sequential pass: {what}");
            // Keep the sequential result.
            *out.mobs = r.mobs;
            *out.died = r.died;
            *out.bindings = r.bindings;
            *out.scratch = r.scratch;
            *out.events = r.events;
            *out.dead = r.dead;
            *out.guest_events = r.guest_events;
        }
        verify_ms += t.elapsed().as_secs_f64() * 1e3;
    }
    AGG.with(|a| {
        let mut a = a.borrow_mut();
        a.parallel += 1;
        a.islands += drains.len() as u64;
        a.largest += largest as u64;
        a.mobs += n as u64;
        a.work_ms += work_ms;
        a.critical_ms += critical_ms;
        a.plan_ms += plan_ms;
        a.split_ms += split_ms;
        a.run_ms += run_ms;
        a.merge_ms += merge_ms;
        a.verify_ms += verify_ms;
        a.gate_ms += gate_ms;
        a.busy_max_ms += busy.iter().copied().fold(0.0, f64::max);
        a.est_speedup += est_speedup;
        a.allocs += allocs;
        a.alloc_bytes += alloc_bytes;
        a.step_max = a.step_max.max(step);
        a.guard_ms += guard_ms;
        a.redone += u64::from(redone);
        a.tripped += u64::from(tripped);
        if check {
            a.verified += 1;
        }
        a.mismatches += u64::from(mismatch);
    });
    report();
    true
}

#[cfg(test)]
mod tests {
    use super::{excerpt, first_difference, stranded_hits};
    use horizon_client::mobs::{Damage, MobEvent, Who};

    fn hit(target: u32) -> MobEvent {
        MobEvent::AttackMob {
            target,
            amount: 1.0,
            source: Damage::mob(1, [0.0; 3]),
        }
    }
    fn alert(mob: u32) -> MobEvent {
        MobEvent::SetTarget {
            mob,
            target: Who::Player,
        }
    }

    /// P1.3.1: mob 1 (island A) hits mob 3 (island B) after the split. The
    /// detector must see it from the islands' own ids: the old one built its
    /// set from the pass's mob list, empty at that point, which is what the
    /// empty-ids case below stands for, and found nothing.
    const NONE: &[u32] = &[];
    const NO_EVENTS: &[MobEvent] = &[];

    #[test]
    fn a_hit_on_another_islands_mob_is_stranded() {
        let (a, b) = ([1, 2], [3, 4]);
        let ev_a = [hit(3)];
        let islands = [(&a[..], &ev_a[..], NONE), (&b[..], NO_EVENTS, NONE)];
        assert_eq!(stranded_hits(islands.iter().copied()), 1);
        // What the P1.3 detector saw: the ids of an emptied mob list, so
        // nothing was stranded.
        let old = [(NONE, &ev_a[..], NONE), (NONE, NO_EVENTS, NONE)];
        assert_eq!(stranded_hits(old.iter().copied()), 0);
    }

    /// The same for an alert (`SetTarget`), and in any island order.
    #[test]
    fn an_alert_on_another_islands_mob_is_stranded() {
        let (a, b) = ([1, 2], [3, 4]);
        let ev_b = [alert(2)];
        let islands = [(&a[..], NO_EVENTS, NONE), (&b[..], &ev_b[..], NONE)];
        assert_eq!(stranded_hits(islands.iter().copied()), 1);
        let reversed = [(&b[..], &ev_b[..], NONE), (&a[..], NO_EVENTS, NONE)];
        assert_eq!(stranded_hits(reversed.iter().copied()), 1);
    }

    /// P1.3.1: a push, a brain memory or a partner's rest the island's pass
    /// could not deliver (dropped silently before) counts when its mob is in
    /// another island.
    #[test]
    fn an_undelivered_write_to_another_island_is_stranded() {
        let (a, b) = ([1, 2], [3, 4]);
        let islands = [(&a[..], NO_EVENTS, &[4][..]), (&b[..], NO_EVENTS, NONE)];
        assert_eq!(stranded_hits(islands.iter().copied()), 1);
    }

    /// No false alarms: independent islands, writes on a mob of the same
    /// island (one removed this tick keeps its pushes), and hits on a mob
    /// that is not in this tick (gone, or never loaded), which the
    /// sequential pass leaves to the game too.
    #[test]
    fn independent_islands_and_strangers_are_not_stranded() {
        let (a, b) = ([1, 2], [3, 4]);
        let push = MobEvent::Push {
            target: Who::Mob(3),
            velocity: [0.1, 0.0, 0.0],
        };
        let ev_a = [push, hit(99), alert(98)];
        let islands = [
            (&a[..], &ev_a[..], &[2, 97][..]),
            (&b[..], NO_EVENTS, &[3][..]),
        ];
        assert_eq!(stranded_hits(islands.iter().copied()), 0);
        assert_eq!(stranded_hits(std::iter::empty()), 0);
    }

    /// P1.1 audit finding: the mismatch report cut a mob's `Debug` text at
    /// raw byte offsets and panicked inside a multi-byte character.
    #[test]
    fn excerpts_cut_on_character_boundaries() {
        let a = format!("{}Корова «Бурёнка» x: 1.5", "a".repeat(117));
        let b = format!("{}Корова «Бурёнка» x: 2.5", "a".repeat(117));
        let at = first_difference(&a, &b);
        assert_eq!(&a[at..at + 1], "1");
        for radius in 0..140 {
            for s in [&a, &b] {
                let e = excerpt(s, at, radius);
                assert!(s.contains(e));
            }
        }
        // P1's slicing on a name in two-byte letters: the cut 120 bytes back
        // lands inside a letter and panics; the excerpt does not.
        let a = format!("{}x: 1", "ё".repeat(100));
        let b = format!("{}x: 2", "ё".repeat(100));
        let at = first_difference(&a, &b);
        assert_eq!(at, 203);
        let old = std::panic::catch_unwind(|| a[at.saturating_sub(120)..].len());
        assert!(old.is_err(), "P1's slice panics here");
        assert!(excerpt(&a, at, 120).ends_with("x: 1"));
        // A byte offset inside a character, and past the end.
        let s = "ёжик";
        assert_eq!(excerpt(s, 1, 0), "ё");
        assert_eq!(excerpt(s, 100, 3), "");
        assert_eq!(excerpt("", 0, 5), "");
        // One string a prefix of the other: the difference is at its end.
        assert_eq!(first_difference("кот", "кот и пёс"), "кот".len());
    }
}
