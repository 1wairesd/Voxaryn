//! The other mobs of a pass, reachable during one mob's tick (P1.3.2).
//!
//! Vanilla applies a melee hit (`Mob.doHurtTarget` -> `target.hurtServer`)
//! inside the attacker's tick: the target's health, hurt cooldown, default
//! knockback, hurt or death sound and death happen there, and the rest of the
//! attacker's tick (its pushes, its goals) sees the target hurt or dead.
//! A mob ticks with `&mut` to itself and `&` to the pass's snapshot, so the
//! pass splits its mob list around the ticking mob and lends the rest here,
//! behind a mutex the tick locks only to land a hit.
//!
//! A target that is not in this pass (another island of the parallel pass,
//! or no mob of this tick) is not reachable: [`hit`] then falls back to the
//! `AttackMob` event, which the pass applies after the tick or, for another
//! island, reports so that the tick is redone sequentially (P1.3.1).
use super::{Damage, Mob, MobEvent, MobWorld, PendingPushes, apply_mob_pushes};
use std::sync::{
    Mutex,
    atomic::{AtomicBool, Ordering},
};

/// What a landed hit did to its target.
#[derive(Debug)]
pub struct Hurt {
    /// `hurtServer` took it (not blocked by the hurt cooldown or immunity).
    pub hurt: bool,
    pub killed: bool,
    /// The target's hurt or death sounds, heard at its centre.
    pub raised: Vec<MobEvent>,
}

/// The pass's other mobs as a ticking mob reaches them.
pub trait PassMobs: Sync {
    /// `target.hurtServer(source, amount)` now, or None when `target` is not
    /// in this pass (nothing changed).
    fn hurt(&self, target: u32, amount: f32, source: Damage) -> Option<Hurt>;
    /// Whether mob `id` was killed during this tick (by the ticking mob): it
    /// is no longer pushable, targetable or nearby for the rest of the tick.
    fn gone(&self, id: u32) -> bool;
}

struct Lent<'m, 'p> {
    before: &'m mut [Mob],
    after: &'m mut [Mob],
    /// The ticking mob's index in the pass (between `before` and `after`).
    at: usize,
    /// Indices in the pass of the mobs a hit changed, in order.
    changed: Vec<usize>,
    killed: Vec<u32>,
    /// Pushes earlier mobs of the pass gave that still wait for their mob's
    /// tick. Vanilla's `Entity.push` already moved the victim, so a hit lands
    /// on the pushed velocity (its knockback reads it).
    pending: &'p mut PendingPushes,
}
impl Lent<'_, '_> {
    /// Adds the pending pushes aimed at mob `j` to its velocity (in push
    /// order, removing them so the pass's own delivery skips them).
    fn take_pushes(&mut self, j: usize) {
        let Self {
            before,
            after,
            at,
            pending,
            ..
        } = self;
        let m = match j.cmp(at) {
            std::cmp::Ordering::Less => before.get_mut(j),
            std::cmp::Ordering::Equal => None,
            std::cmp::Ordering::Greater => after.get_mut(j - *at - 1),
        };
        if let Some(m) = m {
            apply_mob_pushes(m, pending);
        }
    }
    fn get(&mut self, j: usize) -> Option<&mut Mob> {
        match j.cmp(&self.at) {
            std::cmp::Ordering::Less => self.before.get_mut(j),
            std::cmp::Ordering::Equal => None,
            std::cmp::Ordering::Greater => self.after.get_mut(j - self.at - 1),
        }
    }
    fn find(&mut self, id: u32, slot: Option<usize>) -> Option<usize> {
        let at = self.at;
        if let Some(j) = slot
            && j != at
            && self.get(j).is_some_and(|m| m.id == id)
        {
            return Some(j);
        }
        let scan = |s: &[Mob]| s.iter().position(|m| m.id == id);
        scan(self.before).or_else(|| scan(self.after).map(|k| k + at + 1))
    }
}

/// The pass's mobs but one, lent to that mob's tick.
pub struct PassVictims<'m, 's, 'p> {
    lent: Mutex<Lent<'m, 'p>>,
    /// Where mob `id` should be in the pass (checked; a scan otherwise).
    slot: &'s (dyn Fn(u32) -> Option<usize> + Sync),
    any_killed: AtomicBool,
}
impl<'m, 's, 'p> PassVictims<'m, 's, 'p> {
    /// Splits `mobs` around index `at`: the ticking mob, and the rest lent
    /// to it. `pending` holds the pushes not yet added to their mobs; a hit
    /// adds its victim's first.
    pub fn split(
        mobs: &'m mut [Mob],
        at: usize,
        pending: &'p mut PendingPushes,
        slot: &'s (dyn Fn(u32) -> Option<usize> + Sync),
    ) -> (&'m mut Mob, Self) {
        let (before, rest) = mobs.split_at_mut(at);
        let (me, after) = rest
            .split_first_mut()
            .expect("the ticking mob is in the pass");
        let lent = Lent {
            before,
            after,
            at,
            changed: Vec::new(),
            killed: Vec::new(),
            pending,
        };
        (
            me,
            Self {
                lent: Mutex::new(lent),
                slot,
                any_killed: AtomicBool::new(false),
            },
        )
    }
    /// The indices in the pass of the mobs the tick's hits changed, in order
    /// (the pass refreshes what the next mobs see of them).
    pub fn into_changed(self) -> Vec<usize> {
        self.lent
            .into_inner()
            .map_or_else(|p| p.into_inner().changed, |l| l.changed)
    }
}
impl PassMobs for PassVictims<'_, '_, '_> {
    fn hurt(&self, target: u32, amount: f32, source: Damage) -> Option<Hurt> {
        let mut lent = self.lent.lock().unwrap_or_else(|p| p.into_inner());
        let j = lent.find(target, (self.slot)(target))?;
        lent.get(j)?;
        // The pushes before this hit moved the victim already (needsSync
        // too), whether or not the hit is taken.
        let pushed = lent.pending.has(target);
        lent.take_pushes(j);
        if pushed {
            lent.changed.push(j);
        }
        let t = lent.get(j)?;
        if !t.damage(amount, source) {
            return Some(Hurt {
                hurt: false,
                killed: false,
                raised: Vec::new(),
            });
        }
        let center = t.center();
        let killed = !t.alive();
        let raised = t
            .outbox
            .drain(..)
            .map(|e| match e {
                MobEvent::Sound(name, _, volume, pitch) => {
                    MobEvent::Sound(name, center, volume, pitch)
                }
                e => e,
            })
            .collect();
        lent.changed.push(j);
        if killed {
            lent.killed.push(target);
            self.any_killed.store(true, Ordering::Relaxed);
        }
        Some(Hurt {
            hurt: true,
            killed,
            raised,
        })
    }
    fn gone(&self, id: u32) -> bool {
        self.any_killed.load(Ordering::Relaxed)
            && self
                .lent
                .lock()
                .unwrap_or_else(|p| p.into_inner())
                .killed
                .contains(&id)
    }
}

/// A melee hit from the ticking mob (`Mob.doHurtTarget`'s `hurtServer`):
/// landed on the target now when the pass lent it, with its sounds and a
/// [`MobEvent::MobHit`] for the game's part; otherwise the `AttackMob` event
/// as before, applied after the tick (`mobs::apply_hit_events`).
pub fn hit(w: &MobWorld, events: &mut Vec<MobEvent>, target: u32, amount: f32, source: Damage) {
    if let Some(v) = w.victims
        && let Some(h) = v.hurt(target, amount, source)
    {
        if h.hurt {
            events.extend(h.raised);
            events.push(MobEvent::MobHit {
                target,
                source,
                killed: h.killed,
            });
        }
        return;
    }
    events.push(MobEvent::AttackMob {
        target,
        amount,
        source,
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use horizon_core::geometry::Vec3;

    fn mobs(n: u32) -> Vec<Mob> {
        (1..=n)
            .map(|id| {
                Mob::new(
                    id,
                    super::super::test_kind(),
                    Vec3::new(f64::from(id), 64.0, 0.0),
                    0.0,
                )
            })
            .collect()
    }

    /// P1.3.2: a hit lands during the tick, on a mob before or after the
    /// ticking one; a kill marks the mob gone; a second hit within the hurt
    /// cooldown is blocked; the ticking mob itself and strangers are not
    /// reachable; the changed slots come back in order.
    #[test]
    fn hits_land_on_the_lent_mobs_now() {
        let mut list = mobs(4);
        list[3].health = 1.0;
        let slot = |id: u32| Some(id as usize - 1);
        let mut pending = PendingPushes::default();
        let (me, v) = PassVictims::split(&mut list, 1, &mut pending, &slot);
        assert_eq!(me.id, 2);
        let src = Damage::mob(2, [2.0, 64.0, 0.0]);
        let h = v.hurt(1, 3.0, src).expect("mob 1 is lent");
        assert!(h.hurt && !h.killed);
        assert!(!v.gone(1));
        let h = v.hurt(4, 3.0, src).expect("mob 4 is lent");
        assert!(h.hurt && h.killed);
        assert!(v.gone(4) && !v.gone(3));
        // Within the hurt cooldown, a weaker hit does nothing.
        let h = v.hurt(1, 1.0, src).expect("mob 1 is lent");
        assert!(!h.hurt);
        assert!(v.hurt(2, 1.0, src).is_none(), "the ticking mob is not lent");
        assert!(v.hurt(99, 1.0, src).is_none(), "a stranger is not lent");
        assert_eq!(v.into_changed(), vec![0, 3]);
        assert_eq!(list[0].health, 7.0);
        assert!(list[3].health <= 0.0);
        // The default knockback (0.4) happened in the hit, not later.
        assert!(list[0].velocity.x < 0.0);
    }

    /// A wrong or missing slot falls back to a scan.
    #[test]
    fn a_wrong_slot_falls_back_to_a_scan() {
        let mut list = mobs(3);
        let slot = |_: u32| Some(0);
        let mut pending = PendingPushes::default();
        let (_, v) = PassVictims::split(&mut list, 0, &mut pending, &slot);
        assert!(v.hurt(3, 1.0, Damage::mob(1, [1.0, 64.0, 0.0])).is_some());
        assert_eq!(v.into_changed(), vec![2]);
    }

    fn bits(m: &Mob) -> [u64; 3] {
        [
            m.velocity.x.to_bits(),
            m.velocity.y.to_bits(),
            m.velocity.z.to_bits(),
        ]
    }

    /// P1.3.2 hotfix: C pushes B, then A hits B in its tick. The hit's
    /// knockback reads B's velocity, which must already hold C's push
    /// (vanilla: Push(C->B) then damage(A->B)), bit for bit; the push is
    /// delivered once; a push after the hit stays pending.
    #[test]
    fn a_hit_lands_on_the_pushed_victim() {
        let push = |x: f64| Vec3::new(x, 0.1, -0.05);
        let src = Damage::mob(2, [2.0, 64.0, 0.0]);

        // Reference: push applied, then the hit (vanilla's order).
        let mut want = mobs(3);
        want[0].velocity = Vec3::new(0.01, -0.02, 0.03);
        let mut p = PendingPushes::default();
        p.push(1, push(0.25));
        p.push(1, push(-0.125));
        apply_mob_pushes(&mut want[0], &mut p);
        assert!(want[0].damage(3.0, src));

        let mut list = mobs(3);
        list[0].velocity = Vec3::new(0.01, -0.02, 0.03);
        let mut pending = PendingPushes::default();
        pending.push(1, push(0.25));
        pending.push(3, push(0.5));
        pending.push(1, push(-0.125));
        let slot = |id: u32| Some(id as usize - 1);
        let (_, v) = PassVictims::split(&mut list, 1, &mut pending, &slot);
        assert!(v.hurt(1, 3.0, src).expect("lent").hurt);
        assert_eq!(v.into_changed(), vec![0, 0]);
        assert_eq!(bits(&list[0]), bits(&want[0]));
        assert_eq!(list[0].health, want[0].health);
        assert!(list[0].needs_sync);
        // Delivered once: nothing left for mob 1, mob 3's push still waits,
        // and the pass's own delivery adds only that.
        assert!(!pending.has(1) && pending.has(3));
        let before = bits(&list[0]);
        apply_mob_pushes(&mut list[0], &mut pending);
        assert_eq!(bits(&list[0]), before);

        // A push after the hit is not applied early.
        let mut list = mobs(3);
        let mut pending = PendingPushes::default();
        let (_, v) = PassVictims::split(&mut list, 1, &mut pending, &slot);
        assert!(v.hurt(1, 3.0, src).expect("lent").hurt);
        let _ = v.into_changed();
        let hit_only = bits(&list[0]);
        pending.push(1, push(0.25));
        assert_eq!(bits(&list[0]), hit_only);
        apply_mob_pushes(&mut list[0], &mut pending);
        assert_ne!(bits(&list[0]), hit_only);
    }

    /// A hit the hurt cooldown refuses still leaves the pushes applied,
    /// needsSync set and the victim registered as changed.
    #[test]
    fn a_refused_hit_still_applies_the_pushes() {
        let src = Damage::mob(2, [2.0, 64.0, 0.0]);
        let mut list = mobs(3);
        let slot = |id: u32| Some(id as usize - 1);
        let mut pending = PendingPushes::default();
        let (_, v) = PassVictims::split(&mut list, 1, &mut pending, &slot);
        assert!(v.hurt(1, 3.0, src).expect("lent").hurt);
        let _ = v.into_changed();
        list[0].needs_sync = false;
        let after_hit = list[0].velocity;
        pending.push(1, Vec3::new(0.25, 0.1, 0.0));
        let (_, v) = PassVictims::split(&mut list, 1, &mut pending, &slot);
        assert!(!v.hurt(1, 1.0, src).expect("lent").hurt);
        assert_eq!(v.into_changed(), vec![0]);
        assert_eq!(list[0].velocity.x, after_hit.x + 0.25);
        assert!(list[0].needs_sync);
        assert!(!pending.has(1));
    }
}
