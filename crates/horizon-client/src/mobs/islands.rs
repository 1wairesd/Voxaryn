//! Experimental (Voxaryn P1): splits one tick's mobs into *islands*, groups
//! that provably cannot read or write each other during the mob pass, so the
//! game may tick each island on its own thread and still reach exactly the
//! sequential result.
//!
//! The model is deliberately conservative:
//! - Only species whose every in-tick read of other mobs was audited as
//!   spatially bounded qualify ([`verified`]); one other mob anywhere in the
//!   pass and the caller must tick sequentially.
//! - Two mobs share an island when they *might* come within the reach radius
//!   of each other this tick: the plan unions whole grid columns of the
//!   radius' size with their eight neighbours, so it over-merges but never
//!   splits a pair within reach.
//! - Explicit links by id (a mob's target, its last attacker, a mob a goal
//!   holds such as a breeding partner) join islands whatever the distance.
//!
//! Over-merging costs parallelism, never correctness; the planner is pure
//! and has no say in how islands are run.
use super::{Seen, Species, Who};
use crate::fast_hash::FxHashMap;
use horizon_core::geometry::Vec3;

/// The species the parallel pass accepts. For each, every read of another
/// mob during its tick (pushing, `nearby`, `others_near`, the hurt-by alert's
/// scan of the snapshot, a held parent) is bounded by its follow range or
/// less or goes through a link the plan joins (target, attacker, breeding
/// partner), and it never draws from the level random. See
/// `docs/research/p1-parallel-architecture.md` (P1) and
/// `docs/research/p1-stabilization.md` (P1.1) for the audits.
pub fn verified(s: Species) -> bool {
    matches!(
        s,
        // P1
        Species::Cow
            | Species::Mooshroom
            | Species::Pig
            | Species::Sheep
            | Species::Chicken
            | Species::Zombie
            // P1.1: the mobs that kept real scenes sequential (generated sea
            // life, goats) and the common monsters.
            | Species::Squid
            | Species::GlowSquid
            | Species::Dolphin
            | Species::Drowned
            | Species::Goat
            | Species::Nautilus
            | Species::Skeleton
            | Species::Spider
            | Species::Creeper
    )
}

/// The smallest margin added to the follow range for the reach radius (P1's
/// fixed margin): [`REACH_BOXES`] plus two mobs moving 4 blocks each toward
/// each other.
pub const REACH_MARGIN: f64 = 11.0;
/// The part of the margin for the boxes themselves: the half-width of two
/// boxes and a block of `inflate`.
pub const REACH_BOXES: f64 = 3.0;
/// What one tick can add to a mob's speed along x or z before it moves,
/// in P1.1's first fix (a guess, kept for the tests of [`reach_margin`]);
/// P1.2 proves a bound per tick instead ([`motion_step`]).
pub const IN_TICK_SPEEDUP: f64 = 1.0;

/// How far along x or z a mob may move in one pass, from the largest speed
/// (along x or z, blocks per tick) of any mob at the start of the tick
/// (P1.1's first fix).
pub fn step_budget(max_speed: f64) -> f64 {
    max_speed.max(0.0) + IN_TICK_SPEEDUP
}

// ---- P1.2: a proven bound on how far a mob moves in one pass --------------
//
// A mob's position changes only in its own `travel` (`LivingEntity.travel`,
// mobs/living.rs): `do_move` by its velocity at that moment, and block
// collisions only shorten the move. Along x or z that velocity is at most:
//   |v| at the start of the tick (the pass never moves a mob before its turn)
// + the pushes it gets from the mobs that tick before it: `push_entities`
//   adds at most 0.05 per axis per pushing mob (mobs/ai.rs: the push vector
//   is scaled by min(1, 1/sqrt(d)) / sqrt(d) * d <= 1, times 0.05), and a
//   pusher's box meets the mob's start box, so it started within one step
// + its own input: `move_relative(speed, input)` adds at most `speed` (an
//   input longer than 1 is normalised); `speed` is at most the species'
//   largest goal speed modifier times its movement speed (on ground
//   `friction_speed` <= `ai.speed`, 0.02 in the air and in lava, in water
//   between the two)
// + what a few species set themselves (`species_extra`).
// The velocity zeroing in `ai_step` and the climbers' clamp only lower it.
// Pushes a mob gets from mobs after it land after its move (next tick).

/// The largest goal speed modifier any species may set (Cow/Mooshroom
/// `Panic(2.0)`); the bound for a species [`max_speed_modifier`] does not list.
pub const MAX_SPEED_MODIFIER: f64 = 2.0;
/// The largest speed modifier a species' goals register (`Goal::new(speed)`
/// in mobs/farm.rs, undead.rs, hostile.rs, aquatic.rs, nautilus.rs, and the
/// generic goals of ai.rs, read 2026-11-07). Zombies and drowned list only
/// 1.0, but their turtle-egg and water goals take their speed elsewhere:
/// 1.25 for them. Any other species: [`MAX_SPEED_MODIFIER`].
pub fn max_speed_modifier(s: Species) -> f64 {
    match s {
        Species::Cow | Species::Mooshroom => 2.0,
        Species::Nautilus => 1.6,
        Species::Chicken => 1.4,
        Species::Pig | Species::Sheep | Species::Goat => 1.25,
        Species::Zombie | Species::Drowned => 1.25,
        Species::Dolphin | Species::Skeleton | Species::Creeper | Species::Spider => 1.2,
        Species::Squid | Species::GlowSquid => 1.0,
        _ => MAX_SPEED_MODIFIER,
    }
}
/// What a mob's own tick may add to its movement speed attribute (an angry
/// mob's +0.05 in undead.rs; effects only end during the pass).
pub const SPEED_ATTRIBUTE_SLACK: f64 = 0.05;
/// A push's largest component along x or z (mobs/ai.rs `push_entities`).
pub const PUSH_PER_CONTACT: f64 = 0.05;
/// Above this step (blocks per tick along x or z) the bound is refused: the
/// tick runs sequentially.
pub const STEP_MAX: f64 = 3.0;

/// What a species' own code may set or add to its horizontal velocity in
/// its tick, besides walking; `None` when it cannot be bounded beforehand.
pub fn species_extra(m: &super::Mob) -> Option<f64> {
    Some(match m.species() {
        // LeapAtTarget: (d / |d|) * 0.4 + v * 0.2 (goals.rs).
        Species::Spider => 0.4,
        // Charge: the direction to its target times 0.6 (nautilus.rs).
        Species::Nautilus => 0.6,
        // A dolphin on dry ground flops by up to 0.2 (aquatic.rs).
        Species::Dolphin => 0.2,
        // DrownedMoveControl adds speed * xd * 0.005 toward its next path
        // node (undead.rs).
        Species::Drowned => 0.1,
        // A squid's swim vector replaces its velocity: at most 0.2 when it
        // wanders, but a fleeing squid's grows with its distance to the
        // attacker (`SquidFlee`, aquatic.rs): no bound before the tick.
        Species::Squid | Species::GlowSquid => {
            if m.ai.last_hurt_by.is_some() {
                return None;
            }
            0.2
        }
        _ => 0.0,
    })
}
/// The most a mob's tick may add along x or z to its speed by itself (no
/// pushes); `None` when unbounded.
pub fn accel_bound(m: &super::Mob) -> Option<f64> {
    use super::control::MoveKind;
    // What `ai.speed` can reach: SmoothSwimmingMoveControl scales the speed by
    // its in-water or outside factor (control.rs); a squid's travel only moves
    // by its velocity (aquatic.rs `travel`); the generic travels add at least
    // 0.02 (in the air, water and lava).
    let control = match m.ai.mov.kind {
        MoveKind::SmoothSwimming {
            in_water, outside, ..
        } => f64::from(in_water.max(outside)),
        _ if matches!(m.species(), Species::Squid | Species::GlowSquid) => 0.0,
        _ => 1.0,
    };
    let s =
        (control * max_speed_modifier(m.species()) * (m.movement_speed() + SPEED_ATTRIBUTE_SLACK))
            .max(if control == 0.0 { 0.0 } else { 0.02 });
    if !s.is_finite() || s < 0.0 {
        return None;
    }
    // `move_relative(speed, input)` normalises an input longer than 1, so it
    // adds at most `speed` whatever the input.
    Some(s + species_extra(m)?)
}
/// One mob of the tick for [`motion_step`]: its box at the start of the
/// tick, and its speed bound without pushes (0 for a mob that does not tick).
#[derive(Clone, Copy, Debug)]
pub struct Motion {
    pub bounds: horizon_core::geometry::Aabb,
    pub base: f64,
}
impl Motion {
    pub fn of(m: &super::Mob, bounds: horizon_core::geometry::Aabb, ticks: bool) -> Option<Self> {
        let base = if ticks {
            m.velocity.x.abs().max(m.velocity.z.abs()) + accel_bound(m)?
        } else {
            0.0
        };
        base.is_finite().then_some(Self { bounds, base })
    }
}
/// The smallest step S (blocks along x or z) such that every mob, moving at
/// most its base plus 0.05 for each mob whose start box lies within S of its
/// own (along x and z, any height), moves at most S: a fixed point found from
/// below. `None` when it is above [`STEP_MAX`] or not reached in a few
/// rounds (a pile: every mob touches every other).
pub fn motion_step(mobs: &[Motion]) -> Option<f64> {
    motion_step_with(mobs, &|s| contact_counts(mobs, s))
}
/// [`motion_step`] with the contact counter supplied (the parallel pass
/// counts on its worker pool). Any S for which every mob's base plus 0.05 per
/// mob within S is at most S is a bound (the argument above needs no more);
/// it looks for the smallest from below, in at most [`MOTION_ROUNDS`] counts.
pub fn motion_step_with(mobs: &[Motion], count: &dyn Fn(f64) -> Vec<u32>) -> Option<f64> {
    // `f64::max` would skip a NaN: refuse it outright.
    if mobs.iter().any(|m| !m.base.is_finite() || m.base < 0.0) {
        return None;
    }
    let top = mobs.iter().map(|m| m.base).fold(0.0_f64, f64::max);
    if top == 0.0 {
        // Nothing moves by itself, so nothing pushes: no mob moves.
        return Some(0.0);
    }
    // From below: the count grows with S, so the first S that holds is the
    // smallest the count can prove (P1.2: two fixed tries refused half the
    // pens of `herds_8x250`).
    let mut s = top;
    for _ in 0..MOTION_ROUNDS {
        if !s.is_finite() || s > STEP_MAX {
            return None;
        }
        let contacts = count(s);
        let need = mobs
            .iter()
            .zip(&contacts)
            .map(|(m, &c)| m.base + PUSH_PER_CONTACT * f64::from(c))
            .fold(0.0_f64, f64::max);
        if need <= s {
            return Some(s);
        }
        s = need;
    }
    None
}
/// `Some(s)` when `s` is a bound for this tick (every mob's base plus 0.05
/// per mob within `s` is at most `s`, and `s` is a usable step), one count.
pub fn motion_step_holds(mobs: &[Motion], s: f64, count: &dyn Fn(f64) -> Vec<u32>) -> Option<f64> {
    if !s.is_finite() || !(0.0..=STEP_MAX).contains(&s) {
        return None;
    }
    if mobs
        .iter()
        .any(|m| !m.base.is_finite() || m.base < 0.0 || m.base > s)
    {
        return None;
    }
    let contacts = count(s);
    mobs.iter()
        .zip(&contacts)
        .all(|(m, &c)| m.base + PUSH_PER_CONTACT * f64::from(c) <= s)
        .then_some(s)
}
/// Counting rounds [`motion_step_with`] tries before it gives up.
pub const MOTION_ROUNDS: usize = 8;
/// Where the mobs' start boxes are, for counting the ones within a step.
pub struct ContactGrid<'a> {
    mobs: &'a [Motion],
    cells: FxHashMap<(i64, i64), Vec<u32>>,
    widest: f64,
}
const CONTACT_CELL: f64 = 2.0;
fn contact_cell(v: f64) -> i64 {
    (v / CONTACT_CELL).floor() as i64
}
impl<'a> ContactGrid<'a> {
    pub fn new(mobs: &'a [Motion]) -> Self {
        use horizon_core::geometry::Axis;
        let mut cells: FxHashMap<(i64, i64), Vec<u32>> = FxHashMap::default();
        let mut widest = 0.0_f64;
        for (i, m) in mobs.iter().enumerate() {
            let b = m.bounds;
            widest = widest
                .max(b.max(Axis::X) - b.min(Axis::X))
                .max(b.max(Axis::Z) - b.min(Axis::Z));
            cells
                .entry((contact_cell(b.min(Axis::X)), contact_cell(b.min(Axis::Z))))
                .or_default()
                .push(i as u32);
        }
        Self {
            mobs,
            cells,
            widest,
        }
    }
    /// How many other mobs' boxes lie within `s` of mob `i`'s box along x and
    /// along z.
    pub fn count(&self, i: usize, s: f64) -> u32 {
        use horizon_core::geometry::Axis;
        let gap = |lo1: f64, hi1: f64, lo2: f64, hi2: f64| (lo2 - hi1).max(lo1 - hi2).max(0.0);
        let b = self.mobs[i].bounds;
        let (x0, x1) = (b.min(Axis::X) - s - self.widest, b.max(Axis::X) + s);
        let (z0, z1) = (b.min(Axis::Z) - s - self.widest, b.max(Axis::Z) + s);
        let mut n = 0u32;
        for cx in contact_cell(x0)..=contact_cell(x1) {
            for cz in contact_cell(z0)..=contact_cell(z1) {
                for &j in self.cells.get(&(cx, cz)).map_or(&[][..], Vec::as_slice) {
                    let o = self.mobs[j as usize].bounds;
                    if j as usize != i
                        && gap(
                            b.min(Axis::X),
                            b.max(Axis::X),
                            o.min(Axis::X),
                            o.max(Axis::X),
                        ) <= s
                        && gap(
                            b.min(Axis::Z),
                            b.max(Axis::Z),
                            o.min(Axis::Z),
                            o.max(Axis::Z),
                        ) <= s
                    {
                        n += 1;
                    }
                }
            }
        }
        n
    }
}
impl ContactGrid<'_> {
    /// Calls `f` with every other mob whose box lies within `s` of mob `i`'s
    /// box along x and along z.
    pub fn each_within(&self, i: usize, s: f64, mut f: impl FnMut(usize)) {
        use horizon_core::geometry::Axis;
        let gap = |lo1: f64, hi1: f64, lo2: f64, hi2: f64| (lo2 - hi1).max(lo1 - hi2).max(0.0);
        let b = self.mobs[i].bounds;
        let (x0, x1) = (b.min(Axis::X) - s - self.widest, b.max(Axis::X) + s);
        let (z0, z1) = (b.min(Axis::Z) - s - self.widest, b.max(Axis::Z) + s);
        for cx in contact_cell(x0)..=contact_cell(x1) {
            for cz in contact_cell(z0)..=contact_cell(z1) {
                for &j in self.cells.get(&(cx, cz)).map_or(&[][..], Vec::as_slice) {
                    let o = self.mobs[j as usize].bounds;
                    if j as usize != i
                        && gap(
                            b.min(Axis::X),
                            b.max(Axis::X),
                            o.min(Axis::X),
                            o.max(Axis::X),
                        ) <= s
                        && gap(
                            b.min(Axis::Z),
                            b.max(Axis::Z),
                            o.min(Axis::Z),
                            o.max(Axis::Z),
                        ) <= s
                    {
                        f(j as usize);
                    }
                }
            }
        }
    }
}
/// Diagnostic (P2 feasibility): the sizes of the connected groups of the
/// "may touch this tick" graph (boxes within `s` of each other), largest
/// first. The groups a plan with no follow-range reach would make.
pub fn contact_clusters(mobs: &[Motion], s: f64) -> Vec<usize> {
    let grid = ContactGrid::new(mobs);
    let mut u = Union::new(mobs.len());
    for i in 0..mobs.len() {
        grid.each_within(i, s, |j| u.join(i as u32, j as u32));
    }
    let mut sizes: FxHashMap<u32, usize> = FxHashMap::default();
    for i in 0..mobs.len() as u32 {
        *sizes.entry(u.find(i)).or_default() += 1;
    }
    let mut v: Vec<usize> = sizes.into_values().collect();
    v.sort_unstable_by(|a, b| b.cmp(a));
    v
}
/// For each mob, how many other mobs' boxes lie within `s` of its box along
/// x and along z.
pub fn contact_counts(mobs: &[Motion], s: f64) -> Vec<u32> {
    let grid = ContactGrid::new(mobs);
    (0..mobs.len()).map(|i| grid.count(i, s)).collect()
}

/// The margin added to the follow range: the boxes, plus two mobs each
/// moving a full step toward each other; never below P1's [`REACH_MARGIN`].
pub fn reach_margin(step: f64) -> f64 {
    (REACH_BOXES + 2.0 * step).max(REACH_MARGIN)
}
/// Whether a mob that started at `start` and ended at `end` moved further
/// along x or z than the plan's `step` allowed.
pub fn exceeds_step(start: Vec3, end: Vec3, step: f64) -> bool {
    (end.x - start.x).abs() > step || (end.z - start.z).abs() > step
}

/// One tick's islands.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Plan {
    /// Snapshot indices of each island, ascending; islands ordered by their
    /// first index (so the plan is deterministic).
    pub islands: Vec<Vec<u32>>,
}
impl Plan {
    pub fn largest(&self) -> usize {
        self.islands.iter().map(Vec::len).max().unwrap_or(0)
    }
}

/// The finish time of a greedy largest-first schedule (LPT) of `costs` over
/// `workers` equal workers: each cost, largest first, goes to the least
/// loaded worker. A cost model for the pass, not a decision rule.
pub fn makespan(costs: &[usize], workers: usize) -> usize {
    let workers = workers.max(1);
    let mut sorted: Vec<usize> = costs.to_vec();
    sorted.sort_unstable_by(|a, b| b.cmp(a));
    let mut load = vec![0usize; workers.min(sorted.len().max(1))];
    for c in sorted {
        let least = load.iter_mut().min().expect("at least one worker");
        *least += c;
    }
    load.into_iter().max().unwrap_or(0)
}

/// The (mob, held mob) id pairs of the mobs' goals and target goals
/// ([`super::goals::Goal::held_mob`]), for [`plan`].
pub fn held_links(mobs: &[super::Mob]) -> Vec<(u32, u32)> {
    let mut out = Vec::new();
    for m in mobs {
        for held in m.ai.goals.held_mobs().chain(m.ai.targets.held_mobs()) {
            out.push((m.id, held));
        }
    }
    out
}

struct Union {
    parent: Vec<u32>,
}
impl Union {
    fn new(n: usize) -> Self {
        Self {
            parent: (0..n as u32).collect(),
        }
    }
    fn find(&mut self, mut i: u32) -> u32 {
        while self.parent[i as usize] != i {
            let p = self.parent[i as usize];
            self.parent[i as usize] = self.parent[p as usize];
            i = p;
        }
        i
    }
    fn join(&mut self, a: u32, b: u32) {
        let (ra, rb) = (self.find(a), self.find(b));
        if ra != rb {
            // The smaller root wins: roots stay deterministic.
            let (lo, hi) = if ra < rb { (ra, rb) } else { (rb, ra) };
            self.parent[hi as usize] = lo;
        }
    }
}

/// The islands of `snapshot` for a reach radius `radius` (blocks, > 0).
/// Positions are the snapshot's (the start of the tick); `radius` must
/// already include the in-tick movement margin. `held` lists pairs of mob
/// ids a goal links at any distance ([`held_links`]).
pub fn plan(snapshot: &[Seen], radius: f64, held: &[(u32, u32)]) -> Plan {
    let n = snapshot.len();
    if n == 0 {
        return Plan::default();
    }
    let mut u = Union::new(n);
    let cell = |v: f64| (v / radius).floor() as i64;
    let mut cells: FxHashMap<(i64, i64), u32> = FxHashMap::default();
    for (i, s) in snapshot.iter().enumerate() {
        let key = (cell(s.position.x), cell(s.position.z));
        match cells.get(&key) {
            Some(&first) => u.join(first, i as u32),
            None => {
                cells.insert(key, i as u32);
            }
        }
    }
    // Neighbouring columns: two mobs within `radius` of each other sit in the
    // same or adjacent columns (on x and on z), whatever their heights.
    let keys: Vec<((i64, i64), u32)> = cells.iter().map(|(k, v)| (*k, *v)).collect();
    for ((x, z), first) in keys {
        for (dx, dz) in [(1, -1), (1, 0), (1, 1), (0, 1)] {
            if let Some(&other) = cells.get(&(x + dx, z + dz)) {
                u.join(first, other);
            }
        }
    }
    // Links by id, at any distance: who a mob targets or was last hurt by.
    let mut index: FxHashMap<u32, u32> = FxHashMap::default();
    for (i, s) in snapshot.iter().enumerate() {
        index.entry(s.id).or_insert(i as u32);
    }
    for (i, s) in snapshot.iter().enumerate() {
        let linked = [s.target, s.hurt_by.map(|(w, _)| w)];
        for who in linked.into_iter().flatten() {
            if let Who::Mob(id) = who
                && let Some(&j) = index.get(&id)
            {
                u.join(i as u32, j);
            }
        }
    }
    // Mobs a goal holds by id with no distance check (a breeding partner,
    // `Goal::held_mob`), at any distance.
    for &(a, b) in held {
        if let (Some(&i), Some(&j)) = (index.get(&a), index.get(&b)) {
            u.join(i, j);
        }
    }
    let mut by_root: FxHashMap<u32, usize> = FxHashMap::default();
    let mut islands: Vec<Vec<u32>> = Vec::new();
    for i in 0..n as u32 {
        let r = u.find(i);
        let k = *by_root.entry(r).or_insert_with(|| {
            islands.push(Vec::new());
            islands.len() - 1
        });
        islands[k].push(i);
    }
    Plan { islands }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::mobs::{Mob, sensor_scan_tests::kind};
    use horizon_core::geometry::Vec3;

    fn at(id: u32, x: f64, z: f64) -> Seen {
        Mob::new(id, kind(), Vec3::new(x, 64.0, z), 0.0).seen(None)
    }

    /// Every island of `p`, as sorted id lists.
    fn ids(p: &Plan, s: &[Seen]) -> Vec<Vec<u32>> {
        p.islands
            .iter()
            .map(|i| i.iter().map(|&k| s[k as usize].id).collect())
            .collect()
    }

    #[test]
    fn far_herds_are_separate_islands() {
        let mut s = Vec::new();
        for (h, (x, z)) in [(0.0, 0.0), (200.0, 0.0), (0.0, 200.0)].iter().enumerate() {
            for k in 0..5 {
                s.push(at((h * 10 + k) as u32, x + k as f64, *z));
            }
        }
        let p = plan(&s, 30.0, &[]);
        assert_eq!(p.islands.len(), 3);
        assert_eq!(p.largest(), 5);
        assert_eq!(ids(&p, &s)[1], vec![10, 11, 12, 13, 14]);
    }

    /// The safety property: any two mobs within the radius share an island,
    /// over random herds, radii and positions on and around column edges.
    #[test]
    fn mobs_within_reach_always_share_an_island() {
        let mut seed = 0x1234_5678_9abc_def1u64;
        let mut next = |range: f64| {
            seed = seed.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(1);
            (seed >> 11) as f64 / (1u64 << 53) as f64 * range
        };
        for case in 0..200 {
            let radius = 5.0 + next(40.0);
            let n = 2 + (case % 40);
            let side = next(400.0) + 1.0;
            let s: Vec<Seen> = (0..n)
                .map(|i| at(i as u32, next(side) - side / 2.0, next(side) - side / 2.0))
                .collect();
            let p = plan(&s, radius, &[]);
            let mut island_of = vec![usize::MAX; n];
            for (k, isl) in p.islands.iter().enumerate() {
                for &i in isl {
                    island_of[i as usize] = k;
                }
            }
            assert!(
                island_of.iter().all(|&k| k != usize::MAX),
                "every mob placed"
            );
            for a in 0..n {
                for b in 0..n {
                    let (pa, pb) = (s[a].position, s[b].position);
                    let d = ((pa.x - pb.x).powi(2) + (pa.z - pb.z).powi(2)).sqrt();
                    if d <= radius {
                        assert_eq!(
                            island_of[a], island_of[b],
                            "case {case}: {a}-{b} at {d} <= {radius}"
                        );
                    }
                }
            }
            // Islands are ascending and ordered by their first index.
            for isl in &p.islands {
                assert!(isl.windows(2).all(|w| w[0] < w[1]));
            }
            assert!(p.islands.windows(2).all(|w| w[0][0] < w[1][0]));
        }
    }

    /// A target or attacker joins islands at any distance.
    #[test]
    fn targets_and_attackers_link_far_mobs() {
        let mut s = vec![
            at(1, 0.0, 0.0),
            at(2, 500.0, 0.0),
            at(3, 1000.0, 0.0),
            at(4, 2000.0, 0.0),
        ];
        s[0].target = Some(Who::Mob(3));
        s[3].hurt_by = Some((Who::Mob(2), 7));
        s[1].target = Some(Who::Player);
        let p = plan(&s, 20.0, &[]);
        assert_eq!(ids(&p, &s), vec![vec![1, 3], vec![2, 4]]);
    }

    /// A goal that holds a mob by id, as `BreedGoal` holds its partner.
    #[derive(Clone, Debug)]
    struct Holding(u32);
    impl crate::mobs::goals::Goal for Holding {
        crate::mobs::goals::goal_clone!();
        fn flags(&self) -> u8 {
            0
        }
        fn can_use(&mut self, _: &mut Mob, _: &mut crate::mobs::goals::Cx) -> bool {
            false
        }
        fn held_mob(&self) -> Option<u32> {
            Some(self.0)
        }
    }

    /// A mob a goal holds (a breeding partner) joins islands at any
    /// distance; mobs without one stay apart.
    #[test]
    fn held_mobs_link_far_mobs() {
        let mut mobs: Vec<Mob> = [0.0, 900.0, 1800.0, 2700.0]
            .iter()
            .enumerate()
            .map(|(i, &x)| Mob::new(i as u32 + 1, kind(), Vec3::new(x, 64.0, 0.0), 0.0))
            .collect();
        mobs[0].ai.goals.add(2, Holding(3));
        let held = held_links(&mobs);
        assert_eq!(held, vec![(1, 3)]);
        let s: Vec<Seen> = mobs.iter().map(|m| m.seen(None)).collect();
        let p = plan(&s, 30.0, &held);
        assert_eq!(ids(&p, &s), vec![vec![1, 3], vec![2], vec![4]]);
        // An id that is not in the tick is ignored.
        let p = plan(&s, 30.0, &[(1, 99)]);
        assert_eq!(p.islands.len(), 4);
    }

    /// `BreedGoal` reports the partner it holds.
    #[test]
    fn breed_goal_reports_its_partner() {
        use crate::mobs::goals::{Breed, Goal};
        assert_eq!(Breed::new(1.0).held_mob(), None);
    }

    /// Pairs exactly on a column edge, just inside and just outside the
    /// radius, on both axes and diagonally.
    #[test]
    fn column_edges() {
        let r = 27.0;
        for (dx, dz) in [
            (1.0, 0.0),
            (0.0, 1.0),
            (
                std::f64::consts::FRAC_1_SQRT_2,
                std::f64::consts::FRAC_1_SQRT_2,
            ),
            (-1.0, 0.0),
            (0.0, -1.0),
        ] {
            for base in [0.0, r - 1e-9, r, 2.0 * r + 0.5, -r, -0.0001] {
                let a = at(1, base, base);
                let near = at(2, base + dx * (r - 1e-6), base + dz * (r - 1e-6));
                let p = plan(&[a.clone(), near], r, &[]);
                assert_eq!(p.islands.len(), 1, "within reach at {base} dir ({dx},{dz})");
            }
        }
        // Far beyond two columns: always apart.
        let p = plan(&[at(1, 0.0, 0.0), at(2, 3.0 * r + 1.0, 0.0)], r, &[]);
        assert_eq!(p.islands.len(), 2);
    }

    #[test]
    fn verified_species_are_not_brain_or_global_mobs() {
        for s in [
            Species::Villager,
            Species::WanderingTrader,
            Species::Pillager,
            Species::Vex,
            Species::Llama,
            Species::Warden,
            Species::Bee,
            Species::Axolotl,
            Species::Sniffer,
            Species::Piglin,
        ] {
            assert!(!verified(s), "{s:?}");
        }
    }

    /// What the parallel pass moves per mob when it splits and merges
    /// (`cargo test … islands::tests::sizes -- --ignored --nocapture`).
    #[test]
    #[ignore = "prints sizes for p1-overhead.md"]
    fn sizes() {
        println!(
            "size_of Mob {} B, Seen {} B, MobEvent {} B",
            std::mem::size_of::<Mob>(),
            std::mem::size_of::<Seen>(),
            std::mem::size_of::<crate::mobs::MobEvent>()
        );
    }

    /// P1.1 audit finding: the margin follows the fastest mob. Walking and
    /// knocked-back mobs keep P1's margin; a launched one widens it, so two
    /// mobs that can meet within the tick share an island.
    #[test]
    fn the_margin_grows_with_the_fastest_mob() {
        assert_eq!(reach_margin(step_budget(0.0)), REACH_MARGIN);
        assert_eq!(reach_margin(step_budget(3.0)), REACH_MARGIN);
        assert!(reach_margin(step_budget(10.0)) >= REACH_BOXES + 2.0 * 10.0);
        // Two cows 40 blocks apart, one flying toward the other at 30
        // blocks a tick: apart with P1's fixed radius, together now.
        let r_p1 = 16.0 + REACH_MARGIN;
        let r = 16.0 + reach_margin(step_budget(30.0));
        let s = [at(1, 0.0, 0.0), at(2, 3.0 * r_p1, 0.0)];
        assert_eq!(plan(&s, r_p1, &[]).islands.len(), 2);
        assert_eq!(plan(&s, r, &[]).islands.len(), 1);
        // The check after the pass.
        let o = Vec3::new(0.0, 64.0, 0.0);
        assert!(!exceeds_step(o, Vec3::new(1.0, 30.0, -1.0), 1.0));
        assert!(exceeds_step(o, Vec3::new(0.0, 64.0, -1.5), 1.0));
    }

    fn motion(x: f64, z: f64, base: f64) -> Motion {
        Motion {
            bounds: at(0, x, z).bounds,
            base,
        }
    }

    /// P1.2: the step is a fixed point of "my base plus 0.05 per mob that
    /// can reach me": it grows with the neighbours a step can bring in.
    #[test]
    fn the_motion_step_counts_possible_pushers() {
        // Whatever it returns must hold: every mob's base plus its possible
        // pushers at that step is at most the step.
        let holds = |ms: &[Motion], s: f64| {
            let c = contact_counts(ms, s);
            ms.iter()
                .zip(&c)
                .all(|(m, &n)| m.base + PUSH_PER_CONTACT * f64::from(n) <= s)
        };
        // Alone: the base.
        let one = [motion(0.0, 0.0, 0.3)];
        assert_eq!(motion_step(&one), Some(0.3));
        // Far apart: no pusher.
        let far = [motion(0.0, 0.0, 0.3), motion(50.0, 0.0, 0.2)];
        assert_eq!(motion_step(&far), Some(0.3));
        // Two touching: each can push the other once.
        let pair = [motion(0.0, 0.0, 0.3), motion(0.5, 0.0, 0.3)];
        let s = motion_step(&pair).expect("bounded");
        assert!(holds(&pair, s) && s >= 0.35, "{s}");
        // A step tried directly (the next tick's warm start): any that holds.
        let count = |s: f64| contact_counts(&pair, s);
        assert_eq!(motion_step_holds(&pair, 1.0, &count), Some(1.0));
        assert_eq!(
            motion_step_holds(&pair, 0.31, &count),
            None,
            "0.3 + one push > 0.31"
        );
        assert_eq!(motion_step_holds(&pair, 0.2, &count), None, "below a base");
        assert_eq!(motion_step_holds(&pair, STEP_MAX + 1.0, &count), None);
        // A crowd needing the second count: 12 mobs side by side.
        let row: Vec<Motion> = (0..12)
            .map(|i| motion(f64::from(i) * 0.1, 0.0, 0.3))
            .collect();
        let s = motion_step(&row).expect("bounded");
        assert!(holds(&row, s) && s > 0.35, "{s}");
        // A pile: 100 mobs on one spot push one another 99 times.
        let pile: Vec<Motion> = (0..100).map(|_| motion(0.0, 0.0, 0.3)).collect();
        assert_eq!(motion_step(&pile), None);
        // Nothing moves: zero.
        assert_eq!(motion_step(&[motion(0.0, 0.0, 0.0)]), Some(0.0));
        assert_eq!(motion_step(&[]), Some(0.0));
        // Too fast to plan with.
        assert_eq!(motion_step(&[motion(0.0, 0.0, STEP_MAX + 0.1)]), None);
        assert_eq!(motion_step(&[motion(0.0, 0.0, f64::NAN)]), None);
    }

    /// The proof's contact count is a superset: every mob whose start box
    /// lies within the step of a mob's start box is counted (random herds).
    #[test]
    fn contact_counts_match_a_scan() {
        let mut seed = 0x51ed_2701_3c4b_9a77u64;
        let mut next = |range: f64| {
            seed = seed.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(1);
            (seed >> 11) as f64 / (1u64 << 53) as f64 * range
        };
        use horizon_core::geometry::Axis;
        for case in 0..60 {
            let side = 4.0 + next(40.0);
            let s = next(2.5);
            let mobs: Vec<Motion> = (0..(5 + case % 50))
                .map(|_| motion(next(side), next(side), 0.0))
                .collect();
            let got = contact_counts(&mobs, s);
            for (i, a) in mobs.iter().enumerate() {
                let want = mobs
                    .iter()
                    .enumerate()
                    .filter(|(j, b)| {
                        let gx = (b.bounds.min(Axis::X) - a.bounds.max(Axis::X))
                            .max(a.bounds.min(Axis::X) - b.bounds.max(Axis::X));
                        let gz = (b.bounds.min(Axis::Z) - a.bounds.max(Axis::Z))
                            .max(a.bounds.min(Axis::Z) - b.bounds.max(Axis::Z));
                        *j != i && gx <= s && gz <= s
                    })
                    .count() as u32;
                assert_eq!(got[i], want, "case {case} mob {i}");
            }
        }
    }

    /// A fleeing squid has no bound; a wandering one, spiders and the
    /// nautilus add their own pushes; the walking part grows with speed.
    #[test]
    fn accel_bounds_by_species() {
        use crate::mobs::Who;
        let mut m = Mob::new(1, kind(), Vec3::new(0.0, 64.0, 0.0), 0.0);
        let base = accel_bound(&m).expect("bounded");
        assert!(base >= max_speed_modifier(m.species()) * m.movement_speed());
        m.ai.last_hurt_by = Some(Who::Player);
        assert_eq!(
            accel_bound(&m),
            Some(base),
            "a generic mob ignores its attacker"
        );
    }

    #[test]
    fn contact_clusters_group_touching_mobs() {
        // Two pairs far apart and one loner.
        let ms = [
            motion(0.0, 0.0, 0.1),
            motion(0.5, 0.0, 0.1),
            motion(50.0, 0.0, 0.1),
            motion(50.4, 0.3, 0.1),
            motion(100.0, 0.0, 0.1),
        ];
        assert_eq!(contact_clusters(&ms, 0.1), vec![2, 2, 1]);
        // A pile is one group.
        let pile: Vec<Motion> = (0..30).map(|_| motion(0.0, 0.0, 0.1)).collect();
        assert_eq!(contact_clusters(&pile, 0.1), vec![30]);
    }

    /// P1.2: no species let into the parallel pass can reach the level random.
    #[test]
    fn verified_species_never_reach_the_level_random() {
        for s in crate::mobs::species::ALL.iter().map(|d| d.species) {
            if verified(s) {
                assert!(!crate::mobs::ai::reaches_level_random(s), "{s:?}");
            }
        }
        for s in [
            Species::Villager,
            Species::Bee,
            Species::Axolotl,
            Species::Warden,
        ] {
            assert!(crate::mobs::ai::reaches_level_random(s), "{s:?}");
        }
    }

    #[test]
    fn makespan_is_a_greedy_largest_first_schedule() {
        assert_eq!(makespan(&[], 6), 0);
        assert_eq!(makespan(&[250; 8], 6), 500);
        assert_eq!(makespan(&[250; 8], 12), 250);
        assert_eq!(makespan(&[250; 8], 1), 2000);
        assert_eq!(
            makespan(&[1000, 100, 100, 100, 100, 100, 100, 100], 6),
            1000
        );
        // Greedy, not optimal (the optimum is 9: 5+4 | 3+3+3).
        assert_eq!(makespan(&[5, 4, 3, 3, 3], 2), 10);
        assert_eq!(makespan(&[7], 0), 7);
    }

    #[test]
    fn empty_single_and_dense() {
        assert_eq!(plan(&[], 10.0, &[]), Plan::default());
        let one = [at(9, 3.0, 4.0)];
        assert_eq!(plan(&one, 10.0, &[]).islands, vec![vec![0]]);
        // A pile on one spot is one island, however big.
        let pile: Vec<Seen> = (0..300).map(|i| at(i, 0.5, 0.5)).collect();
        let p = plan(&pile, 46.0, &[]);
        assert_eq!(p.islands.len(), 1);
        assert_eq!(p.largest(), 300);
    }

    #[test]
    fn the_plan_is_deterministic() {
        let s: Vec<Seen> = (0..500)
            .map(|i| at(i, f64::from(i % 50) * 13.0, f64::from(i / 50) * 29.0))
            .collect();
        let a = plan(&s, 27.0, &[]);
        for _ in 0..5 {
            assert_eq!(plan(&s, 27.0, &[]), a);
        }
    }
}
