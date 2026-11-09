//! Experiment (Voxaryn P2 feasibility, `p2-speculate` builds only, run with
//! `HORIZON_P2_SPECULATE=1`): speculative parallelism **inside one island**,
//! as a shadow of the sequential pass. Nothing here reaches the world: the
//! sequential `mob_pass` runs right after on the real state, and this module
//! only measures, then compares its own result with it.
//!
//! 1. Phase 1 (parallel): every mob ticks on its own copy against the
//!    snapshot of the start of the tick (as if no mob before it had moved),
//!    recording what it read of the others (`mobs::spec_probe`).
//! 2. Phase 2 (sequential, in mob order, on the true progressive state): a
//!    mob's speculative tick is **accepted** when its inputs provably equal
//!    the ones the sequential pass gives it: no push from an earlier mob is
//!    waiting for it, it read the others only through its push query, that
//!    query asked again of the true snapshot answers the same list (same
//!    mobs, same order, same positions), it drew nothing from the level
//!    random and set nothing on other mobs. Otherwise it is **recomputed**:
//!    the original mob ticks against the true state, as `mob_pass` does.
//!    Because the checks read the true state in order, a recomputed mob's
//!    new position is what every later mob is checked against (transitive
//!    dependencies), and the result must equal the sequential one exactly:
//!    `finish` compares them.
use super::creatures::{PassInput, PassOut, seen};
use super::creatures_parallel::digest;
use horizon_client::mobs::{
    self, Mob, MobEvent, MobWorld, PendingPushes, Seen, Species, Who, seen_grid::SeenGrid,
    spec_probe,
};
use horizon_core::random::LegacyRandom;
use rayon::prelude::*;
use std::{
    cell::RefCell,
    collections::HashSet,
    sync::OnceLock,
    time::{Duration, Instant},
};

pub(super) fn enabled() -> bool {
    static ON: OnceLock<bool> = OnceLock::new();
    *ON.get_or_init(|| {
        let on = std::env::var("HORIZON_P2_SPECULATE").is_ok_and(|v| v != "0")
            && super::creatures_parallel::pool().is_some();
        if on {
            log::warn!(
                "Horizon: P2 speculation experiment on (a shadow of the sequential pass; \
                 the parallel island pass is off)"
            );
        }
        on
    })
}
/// Compare every mob's full state (its `Debug`) once in this many passes;
/// the other passes compare positions, velocities, health and the events.
fn deep_every() -> u64 {
    static N: OnceLock<u64> = OnceLock::new();
    *N.get_or_init(|| {
        std::env::var("HORIZON_P2_DEEP_EVERY")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(10)
            .max(1)
    })
}

/// The species the experiment accepts: the farm animals, whose goals read
/// other mobs only through `others_near`/`seen` (counted by the probe).
fn eligible(s: Species) -> bool {
    matches!(
        s,
        Species::Cow | Species::Mooshroom | Species::Pig | Species::Sheep | Species::Chicken
    )
}

/// One speculative tick.
struct Spec {
    mob: Mob,
    /// The guest it was bound to (`None` with no guests).
    conn: Option<u32>,
    kept: bool,
    events: Vec<MobEvent>,
    reads: spec_probe::Reads,
    drew: bool,
    ns: u64,
}

/// What the experiment produced for one pass (or why it skipped it).
pub(super) enum Shadow {
    Skipped(&'static str),
    Ran(Box<Outcome>),
}
pub(super) struct Outcome {
    /// The mobs' boxes at the start of the tick (for the oracle groups).
    start: Vec<mobs::islands::Motion>,
    mobs: Vec<Mob>,
    events: Vec<MobEvent>,
    dead: Vec<Mob>,
    died: HashSet<u32>,
    bindings: std::collections::HashMap<u32, u32>,
    guest_events: Vec<(u32, MobEvent)>,
    stats: Stats,
}

#[derive(Default, Clone)]
struct Stats {
    passes: u64,
    mobs: u64,
    accepted: u64,
    recomputed: u64,
    rej_pending: u64,
    rej_reads: u64,
    rej_targets: u64,
    rej_rng: u64,
    rej_brain: u64,
    /// Rejected for its push list where a mob in it had been recomputed.
    cascades: u64,
    /// Accepted although a mob its push query met had been recomputed (the
    /// check re-asked the true state and it still agreed).
    accepted_after_recompute: u64,
    clone_ms: f64,
    phase1_ms: f64,
    phase1_work_ms: f64,
    phase2_ms: f64,
    recompute_ms: f64,
    seq_ms: f64,
    cheap_checked: u64,
    cheap_mismatches: u64,
    deep_checked: u64,
    deep_mismatches: u64,
    skipped: Vec<(&'static str, u64)>,
    // the contact groups (passes where the movement step was proven)
    cluster_passes: u64,
    step: f64,
    clusters: f64,
    largest_cluster: f64,
    isolated: f64,
    cluster_ms: f64,
    // the oracle: the same groups at the step the tick really took
    oracle_passes: u64,
    oracle_step: f64,
    oracle_groups: f64,
    oracle_largest: f64,
    oracle_alone: f64,
    /// Groups (count, largest) at the fixed steps 0.3, 0.5, 0.8.
    fixed_groups: [f64; 3],
    fixed_largest: [f64; 3],
}

thread_local! {
    static AGG: RefCell<Stats> = RefCell::new(Stats::default());
}

/// Runs the speculative pass on copies of the pass's state.
pub(super) fn run<T: Fn(i32, i32) -> bool + Sync>(
    input: &PassInput<'_, '_, T>,
    out: &PassOut<'_>,
) -> Shadow {
    match try_run(input, out) {
        Ok(o) => Shadow::Ran(Box::new(o)),
        Err(why) => Shadow::Skipped(why),
    }
}

fn try_run<T: Fn(i32, i32) -> bool + Sync>(
    input: &PassInput<'_, '_, T>,
    out: &PassOut<'_>,
) -> Result<Outcome, &'static str> {
    let pool = super::creatures_parallel::pool().ok_or("no-pool")?;
    let n = out.mobs.len();
    if n < 64 {
        return Err("few");
    }
    if !out.events.is_empty()
        || !out.dead.is_empty()
        || !out.guest_events.is_empty()
        || out.snapshot.len() != n
    {
        return Err("state");
    }
    for m in out.mobs.iter() {
        let (x, z) = (m.position.x.floor() as i32, m.position.z.floor() as i32);
        if !input.base.world.loaded(x, z) || !(input.ticking)(x, z) {
            return Err("not-ticking");
        }
        if !eligible(m.species()) {
            return Err("species");
        }
        if m.rider.is_some()
            || m.extra.mount.is_some()
            || m.extra.rider.is_some()
            || m.extra.leash.is_some()
            || m.extra.leash_pending.is_some()
        {
            return Err("links");
        }
        // Breeding sets state on the partner (apply_brain_events).
        if m.extra.in_love > 0 {
            return Err("in-love");
        }
    }
    let mut st = Stats {
        passes: 1,
        mobs: n as u64,
        ..Stats::default()
    };
    let snapshot0: &[Seen] = out.snapshot;
    let grid0 = SeenGrid::new(snapshot0.iter().map(|s| s.bounds));
    let mut index = horizon_client::fast_hash::FxHashMap::<u32, u32>::default();
    for (i, s) in snapshot0.iter().enumerate() {
        index.entry(s.id).or_insert(i as u32);
    }
    let level0: &LegacyRandom = out.scratch;
    let start_state = level0.state();
    // The guest a mob sees, exactly as `mob_pass` binds it: from the mob
    // before its tick, the binding table and the guests, none of which another
    // mob's tick changes (each mob binds only its own id).
    let guests = input.guests;
    let bind = |m: &Mob, w: &mut MobWorld<'_>| -> Option<u32> {
        if guests.is_empty() {
            return None;
        }
        let bound = out.bindings.get(&m.id).copied();
        let conn = super::lan_mobs::player_for(m, w.player.as_ref(), guests, bound);
        if let Some(conn) = conn {
            w.player = guests.iter().find(|g| g.0 == conn).map(|g| g.1.clone());
        }
        conn
    };
    let nearest = |m: &Mob| {
        super::natural_spawning::nearest_player(
            input.players,
            m.position.x,
            m.position.y,
            m.position.z,
        )
        .map(|p| p.position)
    };
    // ---- the alternative (diagnostic): groups that may touch this tick -----
    // With the movement step proven (P1.2), mobs whose boxes are farther apart
    // than it cannot push each other; for these farm animals the push is the
    // only read of other mobs (the probe's `reads` count says so), so the
    // connected groups of the "may touch" graph are independent islands.
    let t = Instant::now();
    let motions: Option<Vec<mobs::islands::Motion>> = out
        .mobs
        .iter()
        .zip(snapshot0)
        .map(|(m, s)| mobs::islands::Motion::of(m, s.bounds, true))
        .collect();
    let start: Vec<mobs::islands::Motion> = motions.clone().unwrap_or_default();
    if let Some(ms) = motions.as_deref()
        && let Some(step) = mobs::islands::motion_step(ms)
    {
        let sizes = mobs::islands::contact_clusters(ms, step);
        st.cluster_passes = 1;
        st.step = step;
        st.clusters = sizes.len() as f64;
        st.largest_cluster = sizes.first().copied().unwrap_or(0) as f64;
        st.isolated = sizes.iter().filter(|&&s| s == 1).count() as f64;
    }
    st.cluster_ms = t.elapsed().as_secs_f64() * 1e3;
    // ---- the copies phase 1 works on (the experiment's "rollback" cost) ----
    let t = Instant::now();
    let copies: Vec<Mob> = out.mobs.clone();
    st.clone_ms = t.elapsed().as_secs_f64() * 1e3;
    // ---- phase 1: every mob against the tick's start, in parallel ---------
    let t = Instant::now();
    let base = input.base;
    let specs: Vec<Spec> = pool.install(|| {
        copies
            .into_par_iter()
            .with_min_len(16)
            .map(|mut m| {
                let started = Instant::now();
                let mut w = MobWorld {
                    others: snapshot0,
                    others_grid: Some(&grid0),
                    others_index: Some(&index),
                    ..base.clone()
                };
                w.nearest_player = nearest(&m);
                let conn = bind(&m, &mut w);
                let mut rng = level0.clone();
                let mut events = Vec::new();
                spec_probe::begin();
                let kept = m.tick(&w, &mut rng, &mut events);
                let reads = spec_probe::end();
                Spec {
                    mob: m,
                    conn,
                    kept,
                    events,
                    reads,
                    drew: rng.state() != start_state,
                    ns: started.elapsed().as_nanos() as u64,
                }
            })
            .collect()
    });
    st.phase1_ms = t.elapsed().as_secs_f64() * 1e3;
    st.phase1_work_ms = specs.iter().map(|s| s.ns as f64 / 1e6).sum();
    // ---- phase 2: check in order against the true state, or recompute -----
    let t = Instant::now();
    let mut snapshot: Vec<Seen> = snapshot0.to_vec();
    let mut grid = SeenGrid::new(snapshot.iter().map(|s| s.bounds));
    let mut pending = PendingPushes::default();
    let mut events: Vec<MobEvent> = Vec::new();
    let mut dead: Vec<Mob> = Vec::new();
    let mut died = out.died.clone();
    let mut bindings = out.bindings.clone();
    let mut guest_events: Vec<(u32, MobEvent)> = Vec::new();
    let mut level = level0.clone();
    let mut keep = Vec::with_capacity(n);
    let mut fin: Vec<Mob> = Vec::with_capacity(n);
    let mut recomputed = vec![false; n];
    let mut buf: Vec<(Who, horizon_core::geometry::Vec3)> = Vec::new();
    let is_recomputed = |list: &[(Who, horizon_core::geometry::Vec3)], recomputed: &[bool]| {
        list.iter().any(|(who, _)| match who {
            Who::Mob(id) => index
                .get(id)
                .is_some_and(|&j| recomputed.get(j as usize).copied().unwrap_or(false)),
            Who::Player => false,
        })
    };
    for (k, spec) in specs.into_iter().enumerate() {
        let id = out.mobs[k].id;
        let (m, kept, ev, conn) = {
            let mut w = MobWorld {
                others: &snapshot,
                others_grid: Some(&grid),
                others_index: Some(&index),
                ..base.clone()
            };
            w.nearest_player = nearest(&out.mobs[k]);
            let conn = bind(&out.mobs[k], &mut w);
            debug_assert_eq!(conn, spec.conn);
            if !guests.is_empty() {
                match conn {
                    Some(c) => {
                        bindings.insert(id, c);
                    }
                    None => {
                        bindings.remove(&id);
                    }
                }
            }
            let mut reason: Option<&'static str> = None;
            if pending.has(id) {
                reason = Some("pending");
            } else if spec.reads.other > 0 {
                reason = Some("reads");
            } else if spec.drew {
                reason = Some("rng");
            } else if spec.events.iter().any(|e| {
                matches!(
                    e,
                    MobEvent::BreedTarget { .. }
                        | MobEvent::LockGaze { .. }
                        | MobEvent::Breed { .. }
                )
            }) {
                reason = Some("brain");
            } else if let Some(pushed) = &spec.reads.pushed {
                mobs::ai::push_targets(&spec.mob, &w, &mut buf);
                if buf != *pushed {
                    reason = Some("targets");
                    if is_recomputed(&buf, &recomputed) || is_recomputed(pushed, &recomputed) {
                        st.cascades += 1;
                    }
                } else if is_recomputed(&buf, &recomputed) {
                    st.accepted_after_recompute += 1;
                }
            }
            match reason {
                None => {
                    st.accepted += 1;
                    (spec.mob, spec.kept, spec.events, spec.conn)
                }
                Some(why) => {
                    match why {
                        "pending" => st.rej_pending += 1,
                        "reads" => st.rej_reads += 1,
                        "rng" => st.rej_rng += 1,
                        "brain" => st.rej_brain += 1,
                        _ => st.rej_targets += 1,
                    }
                    let r = Instant::now();
                    let mut m = out.mobs[k].clone();
                    // (bound to the same guest: `conn` above)
                    mobs::apply_mob_pushes(&mut m, &mut pending);
                    let mut ev = Vec::new();
                    let kept = m.tick(&w, &mut level, &mut ev);
                    st.recompute_ms += r.elapsed().as_secs_f64() * 1e3;
                    st.recomputed += 1;
                    recomputed[k] = true;
                    (m, kept, ev, conn)
                }
            }
        };
        let from = events.len();
        events.extend(ev);
        mobs::take_mob_pushes(&mut events, from, &mut pending);
        if let Some(c) = conn {
            let mut i = from;
            while i < events.len() {
                if super::lan_mobs::acts_on_player(&events[i]) {
                    guest_events.push((c, events.remove(i)));
                } else {
                    i += 1;
                }
            }
        }
        if !m.alive() && !m.removed && died.insert(m.id) {
            dead.push(m.clone());
        }
        if !kept {
            died.remove(&m.id);
        }
        keep.push(kept);
        snapshot[k] = seen(&m, input.uuid);
        snapshot[k].alive &= kept;
        grid.update(k, snapshot[k].bounds);
        fin.push(m);
    }
    let mut flags = keep.into_iter();
    fin.retain(|_| flags.next().unwrap_or(true));
    for m in fin.iter_mut() {
        mobs::apply_mob_pushes(m, &mut pending);
    }
    st.phase2_ms = t.elapsed().as_secs_f64() * 1e3;
    if level.state() != start_state {
        // A recomputed mob drew: the farm animals never do.
        return Err("level-random");
    }
    Ok(Outcome {
        start,
        mobs: fin,
        events,
        dead,
        died,
        bindings,
        guest_events,
        stats: st,
    })
}

/// Compares the speculative result with the sequential pass's and logs the
/// counters (`SPECSTATS`) every 20 passes.
fn sorted(m: &std::collections::HashMap<u32, u32>) -> Vec<(u32, u32)> {
    let mut v: Vec<(u32, u32)> = m.iter().map(|(k, v)| (*k, *v)).collect();
    v.sort_unstable();
    v
}
pub(super) fn finish(shadow: Shadow, out: &PassOut<'_>, seq: Duration) {
    AGG.with(|a| {
        let mut a = a.borrow_mut();
        match shadow {
            Shadow::Skipped(why) => match a.skipped.iter_mut().find(|(r, _)| *r == why) {
                Some((_, n)) => *n += 1,
                None => a.skipped.push((why, 1)),
            },
            Shadow::Ran(o) => {
                let s = &o.stats;
                let mut cheap_ok = o.mobs.len() == out.mobs.len()
                    && o.died.len() == out.died.len()
                    && o.dead.len() == out.dead.len()
                    && digest(&o.events) == digest(&*out.events)
                    && digest(&o.guest_events) == digest(&*out.guest_events)
                    && sorted(&o.bindings) == sorted(out.bindings);
                if cheap_ok {
                    cheap_ok = o.mobs.iter().zip(out.mobs.iter()).all(|(x, y)| {
                        x.id == y.id
                            && x.position == y.position
                            && x.velocity == y.velocity
                            && x.health.to_bits() == y.health.to_bits()
                    });
                }
                // Oracle (not a proof): the largest move of the tick really
                // made, and the "may touch" groups at that step.
                if !o.start.is_empty() && o.mobs.len() == o.start.len() {
                    let moved = o
                        .start
                        .iter()
                        .zip(out.mobs.iter())
                        .map(|(s, m)| {
                            let c = s.bounds.center();
                            (m.position.x - c.x).abs().max((m.position.z - c.z).abs())
                        })
                        .fold(0.0_f64, f64::max);
                    let sizes = mobs::islands::contact_clusters(&o.start, moved);
                    // The same at fixed steps: how tight a movement proof would
                    // have to be for the groups to split a pen.
                    for (k, s) in [0.3, 0.5, 0.8].into_iter().enumerate() {
                        let g = mobs::islands::contact_clusters(&o.start, s);
                        a.fixed_largest[k] += g.first().copied().unwrap_or(0) as f64;
                        a.fixed_groups[k] += g.len() as f64;
                    }
                    a.oracle_passes += 1;
                    a.oracle_step += moved;
                    a.oracle_groups += sizes.len() as f64;
                    a.oracle_largest += sizes.first().copied().unwrap_or(0) as f64;
                    a.oracle_alone += sizes.iter().filter(|&&s| s == 1).count() as f64;
                }
                a.cheap_checked += 1;
                if !cheap_ok {
                    a.cheap_mismatches += 1;
                    log::error!("Horizon: P2 speculation differs from the sequential pass (positions/events)");
                }
                if a.passes % deep_every() == 0 {
                    a.deep_checked += 1;
                    let same = o.mobs.len() == out.mobs.len()
                        && o.mobs
                            .iter()
                            .zip(out.mobs.iter())
                            .all(|(x, y)| digest(x) == digest(y))
                        && digest(&o.dead) == digest(&*out.dead);
                    if !same {
                        a.deep_mismatches += 1;
                        log::error!("Horizon: P2 speculation differs from the sequential pass (full state)");
                    }
                }
                a.passes += 1;
                a.mobs += s.mobs;
                a.accepted += s.accepted;
                a.recomputed += s.recomputed;
                a.rej_pending += s.rej_pending;
                a.rej_reads += s.rej_reads;
                a.rej_targets += s.rej_targets;
                a.rej_rng += s.rej_rng;
                a.rej_brain += s.rej_brain;
                a.cascades += s.cascades;
                a.accepted_after_recompute += s.accepted_after_recompute;
                a.clone_ms += s.clone_ms;
                a.phase1_ms += s.phase1_ms;
                a.phase1_work_ms += s.phase1_work_ms;
                a.phase2_ms += s.phase2_ms;
                a.recompute_ms += s.recompute_ms;
                a.seq_ms += seq.as_secs_f64() * 1e3;
                a.cluster_passes += s.cluster_passes;
                a.step += s.step;
                a.clusters += s.clusters;
                a.largest_cluster += s.largest_cluster;
                a.isolated += s.isolated;
                a.cluster_ms += s.cluster_ms;
            }
        }
        let calls = a.passes + a.skipped.iter().map(|(_, n)| n).sum::<u64>();
        if calls < 20 {
            return;
        }
        let p = a.passes.max(1) as f64;
        let spec_ms = (a.clone_ms + a.phase1_ms + a.phase2_ms) / p;
        let skipped: Vec<String> = a.skipped.iter().map(|(r, n)| format!("{r}={n}")).collect();
        log::info!(
            "Horizon: SPECSTATS passes {}, mobs {:.0}, accepted {:.2}%, recomputed {:.1}, \
             rejects pending {:.1} reads {:.1} targets {:.1} rng {:.1} brain {:.1}, cascades {:.1}, \
             accepted_after_recompute {:.1}, clone {:.3} ms, phase1 {:.3} ms, phase1_work {:.3} ms, \
             phase2 {:.3} ms, recompute {:.3} ms, spec {:.3} ms, seq {:.3} ms, speedup {:.2}x, \
             cheap {}/{}, deep {}/{}, skipped [{}], groups proven {}, step {:.3}, groups {:.1}, \
             largest {:.1}, alone {:.1}, group_ms {:.3}, oracle {} step {:.3} groups {:.1} \
             largest {:.1} alone {:.1}, fixed 0.3/0.5/0.8 groups {:.1}/{:.1}/{:.1} \
             largest {:.1}/{:.1}/{:.1}",
            a.passes,
            a.mobs as f64 / p,
            100.0 * a.accepted as f64 / (a.mobs.max(1) as f64),
            a.recomputed as f64 / p,
            a.rej_pending as f64 / p,
            a.rej_reads as f64 / p,
            a.rej_targets as f64 / p,
            a.rej_rng as f64 / p,
            a.rej_brain as f64 / p,
            a.cascades as f64 / p,
            a.accepted_after_recompute as f64 / p,
            a.clone_ms / p,
            a.phase1_ms / p,
            a.phase1_work_ms / p,
            a.phase2_ms / p,
            a.recompute_ms / p,
            spec_ms,
            a.seq_ms / p,
            if spec_ms > 0.0 { a.seq_ms / p / spec_ms } else { 0.0 },
            a.cheap_checked - a.cheap_mismatches,
            a.cheap_checked,
            a.deep_checked - a.deep_mismatches,
            a.deep_checked,
            skipped.join(" "),
            a.cluster_passes,
            a.step / a.cluster_passes.max(1) as f64,
            a.clusters / a.cluster_passes.max(1) as f64,
            a.largest_cluster / a.cluster_passes.max(1) as f64,
            a.isolated / a.cluster_passes.max(1) as f64,
            a.cluster_ms / p,
            a.oracle_passes,
            a.oracle_step / a.oracle_passes.max(1) as f64,
            a.oracle_groups / a.oracle_passes.max(1) as f64,
            a.oracle_largest / a.oracle_passes.max(1) as f64,
            a.oracle_alone / a.oracle_passes.max(1) as f64,
            a.fixed_groups[0] / a.oracle_passes.max(1) as f64,
            a.fixed_groups[1] / a.oracle_passes.max(1) as f64,
            a.fixed_groups[2] / a.oracle_passes.max(1) as f64,
            a.fixed_largest[0] / a.oracle_passes.max(1) as f64,
            a.fixed_largest[1] / a.oracle_passes.max(1) as f64,
            a.fixed_largest[2] / a.oracle_passes.max(1) as f64,
        );
        *a = Stats::default();
    });
}
