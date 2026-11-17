//! Voxaryn P3.1: why the parallel mob pass runs or does not (diagnostics only).
//!
//! Off unless `HORIZON_ENTITY_DIAG=1`; then `try_pass` also fills these
//! counters and `report` logs one `DIAGSTATS` line next to every `PARSTATS`
//! line, over the same window (the same calls), so the two can be compared.
//! Nothing here reads a clock the pass decides on, draws random numbers or
//! changes what `try_pass` returns: the counters sit beside the decisions.
//!
//! Units: one *call* is one `try_pass` that got a worker pool, that is one
//! mob pass of one level (a tick that ticks two dimensions makes two calls).
//! Every call ends in exactly one primary state:
//! - refused before the launch (`refused`, one key, the first failed check in
//!   the order `try_pass` makes them),
//! - run in parallel and kept (`ok`),
//! - run in parallel and discarded, then redone sequentially: for a navigation
//!   change (`redo_nav`), because VERIFY found a difference and kept the
//!   sequential result (`redo_verify`), or because a safety check tripped
//!   (`tripped`). A call with several of these is counted once, in the order
//!   tripped, nav, verify; `verify_mismatch` stays a separate signal.
//!
//! `adaptive` is a refusal by the policy, not by eligibility; it is counted in
//! `refused` under its own key. Secondary classification (`masks`, `species`)
//! scans the mobs of a refused call for *every* gate condition, so a call
//! refused for one reason also shows the others that held; it never changes
//! the primary counters.
//!
//! The counters are plain integers in a thread-local of the thread that runs
//! the pass (as `AGG` and the adaptive policy are); the pool's workers never
//! touch them, so there is nothing to race on.
use horizon_client::mobs::species::Species;
use std::{
    cell::RefCell,
    collections::BTreeMap,
    time::{Duration, Instant},
};

/// Refusal keys, in the order `try_pass` checks them.
pub(super) const KEYS: [&str; 14] = [
    "disabled",
    "rng-trace",
    "few",
    "state",
    "species",
    "level-random",
    "links",
    "duplicate-ids",
    "adaptive",
    "motion-backoff",
    "motion-unbounded",
    "motion-unproven",
    "one-island",
    "dominant",
];
/// Bits of the secondary mask: the gate conditions that hold on a call.
pub(super) const M_FEW: u8 = 1;
pub(super) const M_STATE: u8 = 2;
pub(super) const M_SPECIES: u8 = 4;
pub(super) const M_LEVEL_RANDOM: u8 = 8;
pub(super) const M_LINKS: u8 = 16;
pub(super) const M_DUPLICATE: u8 = 32;

#[derive(Default)]
pub(super) struct Diag {
    pub calls: u64,
    ticks: u64,
    last_time: Option<i64>,
    in_tick: u64,
    max_in_tick: u64,
    mobs_sum: u64,
    mobs_max: u64,
    refused: [u64; KEYS.len()],
    /// Time from the start of the call to the refusal, ns.
    refused_ns: [u128; KEYS.len()],
    ok: u64,
    redo_nav: u64,
    redo_verify: u64,
    tripped: u64,
    tripped_rng: u64,
    tripped_moved: u64,
    tripped_stranded: u64,
    verify_mismatch: u64,
    /// Gate conditions that held on refused calls: mask -> calls.
    masks: BTreeMap<u8, u64>,
    /// Unverified or level-random species on refused calls: name -> (calls, mob-ticks).
    species: BTreeMap<String, (u64, u64)>,
    /// Calls with 0 mobs among the `few`.
    few_empty: u64,
    /// Calls that reached the plan: count, islands, largest island (mobs),
    /// ticking mobs of the top island, ticking mobs in all.
    planned: u64,
    planned_islands: u64,
    planned_largest: u64,
    planned_top: u64,
    planned_ticking: u64,
    /// Planned calls by the top island's share of the ticking mobs
    /// (<25%, <50%, <75%, >=75%, no ticking mobs).
    share: [u64; 5],
    /// Cooldown/probe counters of the policy already reported.
    prev_switches: u64,
    prev_probes: u64,
    /// Time spent in the diagnostics themselves, ns.
    self_ns: u128,
}
thread_local! {
    static DIAG: RefCell<Diag> = RefCell::new(Diag::default());
}
pub(super) fn with<R>(f: impl FnOnce(&mut Diag) -> R) -> R {
    DIAG.with(|d| f(&mut d.borrow_mut()))
}

impl Diag {
    /// A call reached the pool: `mobs` in the pass at `game_time`.
    pub fn call(&mut self, mobs: usize, game_time: i64) {
        let t = Instant::now();
        self.calls += 1;
        self.mobs_sum += mobs as u64;
        self.mobs_max = self.mobs_max.max(mobs as u64);
        if self.last_time == Some(game_time) {
            self.in_tick += 1;
        } else {
            self.last_time = Some(game_time);
            self.ticks += 1;
            self.in_tick = 1;
        }
        self.max_in_tick = self.max_in_tick.max(self.in_tick);
        self.self_ns += t.elapsed().as_nanos();
    }
    /// The call was refused before the launch (primary state, one key).
    /// `elapsed`: the call so far; `scan`: what the secondary scan took.
    pub fn refuse(
        &mut self,
        key: &'static str,
        elapsed: Duration,
        scan: Duration,
        mask: u8,
        species: &[(Species, u64)],
        mobs: usize,
    ) {
        let t = Instant::now();
        self.self_ns += scan.as_nanos();
        if let Some(i) = KEYS.iter().position(|k| *k == key) {
            self.refused[i] += 1;
            self.refused_ns[i] += elapsed.as_nanos();
        }
        *self.masks.entry(mask).or_default() += 1;
        if key == "few" && mobs == 0 {
            self.few_empty += 1;
        }
        for (s, n) in species {
            let e = self.species.entry(format!("{s:?}")).or_default();
            e.0 += 1;
            e.1 += n;
        }
        self.self_ns += t.elapsed().as_nanos();
    }
    /// The plan of a call that got that far: `per_island` ticking mobs per island.
    pub fn planned(&mut self, islands: usize, largest: usize, top: usize, ticking: usize) {
        self.planned += 1;
        self.planned_islands += islands as u64;
        self.planned_largest += largest as u64;
        self.planned_top += top as u64;
        self.planned_ticking += ticking as u64;
        let bucket = match (top * 100).checked_div(ticking) {
            None => 4,
            Some(0..25) => 0,
            Some(25..50) => 1,
            Some(50..75) => 2,
            Some(_) => 3,
        };
        self.share[bucket] += 1;
    }
    /// The call ran in parallel. `redone` it was discarded and redone, for
    /// navigation (`nav`) or after a tripped check (`alarm`: rng, moved, stranded).
    pub fn ran(&mut self, nav: bool, tripped: bool, alarm: (bool, bool, bool), mismatch: bool) {
        if tripped {
            self.tripped += 1;
            self.tripped_rng += u64::from(alarm.0);
            self.tripped_moved += u64::from(alarm.1);
            self.tripped_stranded += u64::from(alarm.2);
        } else if nav {
            self.redo_nav += 1;
        } else if mismatch {
            // VERIFY kept the sequential result: not an accepted parallel pass.
            self.redo_verify += 1;
        } else {
            self.ok += 1;
        }
        self.verify_mismatch += u64::from(mismatch);
    }
    /// The `DIAGSTATS` line for the window since the last one; resets it.
    pub fn take_line(&mut self, switches: u64, probes: u64, cooldown: u32) -> String {
        let refused_total: u64 = self.refused.iter().sum();
        // Parallel passes that ran: accepted (`ok`) plus the three ways a result is thrown away, each
        // followed by one sequential redo (the test-only no-guard knob aside).
        let attempted = self.ok + self.redo_nav + self.redo_verify + self.tripped;
        let list = |v: &[u64], ns: &[u128]| {
            KEYS.iter()
                .enumerate()
                .filter(|(i, _)| v[*i] > 0)
                .map(|(i, k)| format!("{k}={}/{:.3}", v[i], ns[i] as f64 / 1e6))
                .collect::<Vec<_>>()
                .join(" ")
        };
        let masks = self
            .masks
            .iter()
            .map(|(m, n)| format!("{m}={n}"))
            .collect::<Vec<_>>()
            .join(" ");
        let species = self
            .species
            .iter()
            .map(|(s, (c, n))| format!("{s}={c}/{n}"))
            .collect::<Vec<_>>()
            .join(" ");
        let p = self.planned.max(1) as f64;
        let line = format!(
            "Horizon: DIAGSTATS calls {}, ticks {}, max_per_tick {}, mobs {}, mobs_max {}, \
             ok {}, redo_nav {}, redo_verify {}, tripped {} (rng {}, moved {}, stranded {}), verify_mismatch {}, \
             attempted {}, accepted {}, rejected {}, sequential_redo {}, \
             refused {} [{}], masks [{}], species [{}], few_empty {}, planned {} (islands {:.2}, \
             largest {:.1}, top {:.1}, ticking {:.1}, share {}/{}/{}/{}/{}), \
             adaptive_switches {}, adaptive_probes {}, cooldown {}, diag_ms {:.3}",
            self.calls,
            self.ticks,
            self.max_in_tick,
            self.mobs_sum,
            self.mobs_max,
            self.ok,
            self.redo_nav,
            self.redo_verify,
            self.tripped,
            self.tripped_rng,
            self.tripped_moved,
            self.tripped_stranded,
            self.verify_mismatch,
            attempted,
            self.ok,
            attempted - self.ok,
            attempted - self.ok,
            refused_total,
            list(&self.refused, &self.refused_ns),
            masks,
            species,
            self.few_empty,
            self.planned,
            self.planned_islands as f64 / p,
            self.planned_largest as f64 / p,
            self.planned_top as f64 / p,
            self.planned_ticking as f64 / p,
            self.share[0],
            self.share[1],
            self.share[2],
            self.share[3],
            self.share[4],
            switches - self.prev_switches.min(switches),
            probes - self.prev_probes.min(probes),
            cooldown,
            self.self_ns as f64 / 1e6,
        );
        let (ps, pp, last_time, in_tick) = (switches, probes, self.last_time, self.in_tick);
        *self = Diag {
            prev_switches: ps,
            prev_probes: pp,
            // The tick in progress continues across the window edge.
            last_time,
            in_tick,
            ..Diag::default()
        };
        line
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Every call lands in exactly one primary state.
    #[test]
    fn primary_states_add_up_to_the_calls() {
        let mut d = Diag::default();
        for t in 0..10 {
            d.call(100, t);
        }
        d.refuse("few", Duration::ZERO, Duration::ZERO, M_FEW, &[], 3);
        d.refuse("adaptive", Duration::ZERO, Duration::ZERO, 0, &[], 100);
        d.refuse("dominant", Duration::ZERO, Duration::ZERO, 0, &[], 100);
        d.ran(false, false, (false, false, false), false);
        d.ran(true, false, (false, false, false), false);
        d.ran(false, true, (true, false, true), false);
        for _ in 0..4 {
            d.ran(false, false, (false, false, false), false);
        }
        let line = d.take_line(2, 1, 60);
        assert!(line.contains("calls 10,"), "{line}");
        assert!(
            line.contains(
                "ok 5, redo_nav 1, redo_verify 0, tripped 1 (rng 1, moved 0, stranded 1)"
            ),
            "{line}"
        );
        assert!(line.contains("refused 3 [few=1/"), "{line}");
        assert!(
            line.contains("adaptive_switches 2, adaptive_probes 1"),
            "{line}"
        );
        // A new window starts empty; policy counters are reported as deltas.
        let line = d.take_line(3, 1, 60);
        assert!(
            line.contains("calls 0,") && line.contains("adaptive_switches 1, adaptive_probes 0"),
            "{line}"
        );
    }

    /// A VERIFY mismatch replaces the parallel result with the sequential one: that call is not an accepted
    /// parallel pass (Codex audit F1). It is counted once, as `redo_verify`; `verify_mismatch` stays a signal.
    #[test]
    fn a_verify_mismatch_is_not_an_accepted_pass() {
        let mut d = Diag::default();
        for t in 0..4 {
            d.call(100, t);
        }
        d.ran(false, false, (false, false, false), true); // mismatch only
        d.ran(true, false, (false, false, false), true); // nav redo and a mismatch: one category, nav
        d.ran(false, true, (true, false, false), true); // tripped and a mismatch: one category, tripped
        d.ran(false, false, (false, false, false), false); // accepted
        let line = d.take_line(0, 0, 30);
        assert!(
            line.contains("ok 1, redo_nav 1, redo_verify 1, tripped 1 "),
            "{line}"
        );
        assert!(line.contains("verify_mismatch 3"), "{line}");
        assert!(line.contains("accepted 1, rejected 3"), "{line}");
        assert!(line.contains("attempted 4,"), "{line}");
    }

    /// Several calls at one game time are one tick; a new time starts a tick.
    #[test]
    fn calls_per_tick_are_counted() {
        let mut d = Diag::default();
        for t in [5, 5, 6, 7, 7, 7] {
            d.call(1, t);
        }
        let line = d.take_line(0, 0, 30);
        assert!(line.contains("ticks 3, max_per_tick 3"), "{line}");
    }

    #[test]
    fn share_buckets() {
        let mut d = Diag::default();
        d.planned(2, 10, 10, 100); // 10%
        d.planned(2, 40, 40, 100); // 40%
        d.planned(2, 60, 60, 100); // 60%
        d.planned(2, 90, 75, 100); // 75%
        d.planned(2, 1, 0, 0); // none ticking
        let line = d.take_line(0, 0, 30);
        assert!(line.contains("share 1/1/1/1/1"), "{line}");
    }
}
